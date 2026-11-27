"""``ChannelHub``: one ``Channel`` over every enabled adapter (PER-292).

The daemon wires exactly one channel into the router, the runners, the notifier, the pulse
and the monitor: this hub. Adding a channel (Slack, PER-295) means adding an adapter to the
hub (``wiring.build_channels``), never touching the components that send.

Usage::

    hub = ChannelHub({"imessage": imessage, "slack": slack}, principals, conn)
    await hub.start(loop.submit)          # starts every adapter; see "Start" below
    await hub.send("alice", "done", thread=thread_of(task), dedupe_key=...)
    await hub.stop()

**Which adapter sends.** ``send(principal, text, thread=...)`` and ``request_approval``:

1. With a ``thread`` (``prompt.thread`` for an approval) whose ``channel`` is a running
   adapter the principal has an identity on, that adapter (the reply stays in the
   conversation it answers).
2. Otherwise the principal's first identity, in principals.yaml order, whose adapter is
   running. A ``thread`` from another channel is dropped (it means nothing there).
3. No such identity (an unknown principal, or none of its identities is on a running
   adapter): :class:`ChannelRefused`, audited ``channel.send_refused`` with the principal
   and the reason, never the text.

The hub never sends outside a principal's identities: it only picks the adapter, and the
adapter resolves the address from principals.yaml itself (and refuses on its own too).
iMessage-only, the hub always picks iMessage, so every send is what it was before the hub.

**Rooms (PER-295).** :meth:`post_room` is the one send that takes no principal: it posts to
a configured Slack room (``channels.slack.rooms``, by key) through the running adapter that
can (``RoomPosting``: Slack). An unknown key, or no such adapter running, raises
:class:`ChannelRefused`. Jobs and the pulse use it for ``#digest``-style posts.

**Running.** An adapter is running unless it failed to start. Sends stay possible after
:meth:`stop` (the loop drains in-flight steps after the readers stop, and those may still
reply), as they were with the iMessage channel alone.

**Start.** :meth:`start` starts each adapter in order. One that raises is logged, audited
``channel.start_failed`` and left out (``failures`` keeps a short reason for the monitor);
the others run. If none starts, the first error is raised, so an iMessage-only daemon still
exits 1 when iMessage can't start, as before.

**Self-test and health.** :meth:`self_test` runs each running adapter's self-test (the
monitor runs them per adapter, ``HealthMonitor.channels``). :meth:`health` aggregates the
adapters' ``health()``: ``ok`` when every running adapter is ok and none failed,
``last_read_at`` the latest read, ``last_error`` the first error, and ``adapters`` the
per-adapter dicts. With a single adapter its own keys (iMessage: ``fda_ok``) are kept too.

``name`` is the adapter names joined with ``+`` (``"imessage"`` iMessage-only, so audit
rows that carry ``channel=channel.name`` are unchanged).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, NoReturn, Protocol, cast, runtime_checkable

from resonant.gateway.channel import (
    ApprovalPrompt,
    Channel,
    ChannelRefused,
    MessageRef,
    SelfTestResult,
    Submit,
)
from resonant.principals import Principals
from resonant.store.audit import audit
from resonant.store.db import atomic

log = logging.getLogger(__name__)

_MAX_REASON = 120


@runtime_checkable
class HealthReporting(Protocol):
    """An adapter that reports reader health (``IMessageChannel``, ``FakeChannel``)."""

    def health(self) -> dict[str, Any]: ...


@runtime_checkable
class RoomPosting(Protocol):
    """An adapter with named rooms (``SlackChannel``): ``post_room`` refuses unknown keys."""

    async def post_room(
        self,
        key: str,
        text: str,
        *,
        dedupe_key: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> MessageRef: ...


class ChannelHub:
    """The daemon's one ``Channel``, over named adapters. See the module docstring."""

    def __init__(
        self,
        adapters: Mapping[str, Channel],
        principals: Principals,
        conn: sqlite3.Connection,
    ) -> None:
        if not adapters:
            raise ValueError("a channel hub needs at least one adapter")
        self._adapters: dict[str, Channel] = {}
        for name, adapter in adapters.items():
            if name != adapter.name:
                raise ValueError(f"adapter {adapter.name!r} registered as {name!r}")
            if name in self._adapters:
                raise ValueError(f"two adapters named {name!r}")
            self._adapters[name] = adapter
        self.principals = principals
        self.conn = conn
        self.failures: dict[str, str] = {}  # adapter -> why it didn't start (short, no body)
        self._started: list[str] = []

    # --- what's here ---------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "+".join(self._adapters)

    @property
    def adapters(self) -> Mapping[str, Channel]:
        """Every configured adapter by name, in start order (running or not)."""
        return dict(self._adapters)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    @property
    def running(self) -> tuple[str, ...]:
        """The adapters that can send: every one that didn't fail to start."""
        return tuple(n for n in self._adapters if n not in self.failures)

    def adapter(self, name: str) -> Channel | None:
        return self._adapters.get(name)

    # --- lifecycle -----------------------------------------------------------------------

    async def start(self, submit: Submit) -> None:
        """Start every adapter (their events go to ``submit``). See "Start" above."""
        first: Exception | None = None
        for name, adapter in self._adapters.items():
            try:
                await adapter.start(submit)
            except Exception as e:
                first = first or e
                reason = _reason(e)
                self.failures[name] = reason
                log.exception("channel %s failed to start", name)
                self._audit("channel.start_failed", channel=name, error=type(e).__name__)
                continue
            self.failures.pop(name, None)
            self._started.append(name)
        if not self._started and first is not None:
            raise first

    async def stop(self) -> None:
        """Stop every started adapter. Safe to call twice or before :meth:`start`."""
        started, self._started = self._started, []
        for name in started:
            try:
                await self._adapters[name].stop()
            except Exception:
                log.exception("channel %s failed to stop", name)

    # --- outbound ------------------------------------------------------------------------

    def pick(self, principal: str, thread: MessageRef | None = None) -> Channel:
        """The adapter a send to ``principal`` goes through. Raises ``ChannelRefused``."""
        p = self.principals.get(principal)
        if p is None:
            self._refuse(principal, "unknown principal")
        running = set(self.running)
        on = {i.partition(":")[0] for i in p.identities}
        if thread is not None and thread.channel in running and thread.channel in on:
            return self._adapters[thread.channel]
        for identity in p.identities:
            channel = identity.partition(":")[0]
            if channel in running:
                return self._adapters[channel]
        self._refuse(principal, "principal has no identity on a running channel")

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        adapter = self.pick(principal, thread)
        here = thread if thread is not None and thread.channel == adapter.name else None
        return await adapter.send(principal, text, thread=here, dedupe_key=dedupe_key)

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        """The same pick, by ``prompt.thread``; a thread from another channel is dropped."""
        adapter = self.pick(principal, prompt.thread)
        if prompt.thread is not None and prompt.thread.channel != adapter.name:
            prompt = replace(prompt, thread=None)
        return await adapter.request_approval(principal, prompt)

    async def post_room(
        self,
        key: str,
        text: str,
        *,
        dedupe_key: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> MessageRef:
        """Post ``text`` to the room ``key`` (see "Rooms" above). Raises ``ChannelRefused``.
        ``blocks`` (PER-332): optional Slack Block Kit blocks, ``text`` the fallback."""
        for name in self.running:
            adapter = self._adapters[name]
            if isinstance(adapter, RoomPosting):
                return await adapter.post_room(key, text, dedupe_key=dedupe_key, blocks=blocks)
        self._audit("channel.send_refused", room=key, reason="no running channel has rooms")
        raise ChannelRefused("no running channel has rooms")

    # --- health --------------------------------------------------------------------------

    async def self_test(self, *, timeout_s: float | None = None) -> SelfTestResult:
        """Each running adapter's self-test; ok when all pass. One adapter: its own result."""
        results: list[tuple[str, SelfTestResult]] = []
        for name in self.running:
            adapter: Any = self._adapters[name]
            try:
                r = cast(
                    SelfTestResult,
                    await (
                        adapter.self_test()
                        if timeout_s is None
                        else adapter.self_test(timeout_s=timeout_s)
                    ),
                )
            except Exception as e:  # one adapter's self-test never hides the others'
                r = SelfTestResult(False, f"self-test error ({type(e).__name__})")
            results.append((name, r))
        for name, why in self.failures.items():
            results.append((name, SelfTestResult(False, f"not running: {why}")))
        if len(results) == 1:
            return results[0][1]
        return SelfTestResult(
            all(r.ok for _, r in results), "; ".join(f"{n}: {r.detail}" for n, r in results)
        )

    def health(self) -> dict[str, Any]:
        """Aggregated adapter health. See "Self-test and health" above."""
        per: dict[str, dict[str, Any]] = {}
        for name, adapter in self._adapters.items():
            if name in self.failures:
                per[name] = {
                    "ok": False,
                    "last_read_at": None,
                    "last_error": f"not running: {self.failures[name]}",
                }
            elif isinstance(adapter, HealthReporting):
                per[name] = dict(adapter.health())
            else:
                per[name] = {"ok": True, "last_read_at": None, "last_error": None}
        reads = [h["last_read_at"] for h in per.values() if isinstance(h.get("last_read_at"), str)]
        errors = [h["last_error"] for h in per.values() if h.get("last_error") is not None]
        out: dict[str, Any] = dict(next(iter(per.values()))) if len(per) == 1 else {}
        out.update(
            ok=all(h.get("ok") is True for h in per.values()),
            last_read_at=max(reads) if reads else None,
            last_error=errors[0] if errors else None,
            adapters=per,
        )
        return out

    # --- internals -----------------------------------------------------------------------

    def _refuse(self, principal: str, reason: str) -> NoReturn:
        self._audit("channel.send_refused", requested_by=principal, reason=reason)
        raise ChannelRefused(reason)

    def _audit(self, action: str, **detail: Any) -> None:
        try:
            with atomic(self.conn):
                audit(self.conn, action, **detail)
        except sqlite3.Error as e:
            log.warning("channel audit not written: %s", type(e).__name__)


def _reason(e: Exception) -> str:
    text = " ".join(str(e).split()) or type(e).__name__
    return text if len(text) <= _MAX_REASON else text[: _MAX_REASON - 1] + "…"

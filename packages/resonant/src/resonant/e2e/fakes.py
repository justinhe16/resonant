"""Fakes for the end-to-end harness: a scripted ``ModelClient`` and a recording ``OsaRunner``.

PER-256: the local model only routes, so ``ScriptedModel`` answers the triage call and the
history fold, nothing else.

- **Triage** (a ``chat_json`` call with the triage schema, ``brain.triage.SCHEMA``): answers
  the next item of ``triages`` (a dict, or an exception to raise), else ``triage`` (default
  ``DEFAULT_TRIAGE``: a ``claude`` handler and the type ``other``, so the turn is Claude's).
  Each request is kept in ``triage_requests``.
- A history fold (``{summary}``) gets a fixed summary.
- ``chat_tools`` is never called by the runners any more; calling it is a test bug.
- ``probe()``: always ok.

Each call can sleep ``delay_s`` first, to stand in for model latency.

``RecordingOsaRunner`` records every osascript argv (handle, text, monotonic time) instead of
running it, and lets a caller wait for the n-th send.

``FakeChannel`` (PER-292) is an in-memory channel adapter for the hub: a threaded channel
(Slack-shaped) by default. ``inject`` turns ``<name>:<uid>`` text into the message event an
adapter would submit (``payload.identity``, ``ref``, ``thread``; the thread defaults to the
message's own ref, as a Slack message starts its own thread). Sends and approval prompts go
only to principals with an identity on this channel (else ``ChannelRefused``), are deduped
by key, and are recorded in ``sent`` with their thread. It never touches the network.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from resonant.brain.triage import SCHEMA as TRIAGE_SCHEMA
from resonant.gateway.channel import (
    ApprovalPrompt,
    ChannelError,
    ChannelRefused,
    MessageRef,
    SelfTestResult,
    Submit,
    approval_dedupe_key,
)
from resonant.gateway.imessage.sender import OSASCRIPT, OsaResult
from resonant.models import ChatMessage, ModelReply, ProbeResult, SpanFactory
from resonant.principals import Principals
from resonant.store.db import iso, utcnow
from resonant_sdk import Event, ToolSpec

DEFAULT_TRIAGE: dict[str, Any] = {
    "handler": "claude",
    "kind": "general",
    "message_type": "other",
    "say": "",
}


class ScriptedModel:
    """A deterministic ``ModelClient``. ``calls`` counts every chat call by kind."""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls: dict[str, int] = {"triage": 0, "fold": 0, "probe": 0}
        self.triage: dict[str, Any] = dict(DEFAULT_TRIAGE)
        self.triages: list[dict[str, Any] | BaseException] = []
        self.triage_requests: list[ToolsRequest] = []

    async def _wait(self) -> None:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)

    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        max_tokens: int = 512,
        temperature: float = 0.0,
        spans: SpanFactory | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> ModelReply:
        await self._wait()
        if schema == TRIAGE_SCHEMA:
            self.calls["triage"] += 1
            self.triage_requests.append(ToolsRequest(list(messages), [t.name for t in tools or []]))
            item = self.triages.pop(0) if self.triages else self.triage
            if isinstance(item, BaseException):
                raise item
            data: dict[str, Any] = dict(item)
            return ModelReply(json.dumps(data), [], data, 1, 10, 5)
        if _props(schema) == {"summary"}:  # the brain's history fold
            self.calls["fold"] += 1
            data = {"summary": "Earlier: the owner and Resonant talked."}
            return ModelReply(json.dumps(data), [], data, 1, 10, 5)
        raise ValueError("ScriptedModel: unexpected schema")

    async def chat_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        max_tokens: int = 512,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        raise AssertionError("ScriptedModel: the local model no longer calls tools (PER-256)")

    async def probe(self) -> ProbeResult:
        self.calls["probe"] += 1
        return ProbeResult(True, True, True, True, 1, "scripted model")


def _props(schema: dict[str, Any]) -> set[str]:
    return set(cast(dict[str, Any], schema.get("properties") or {}))


@dataclass(frozen=True)
class ToolsRequest:
    """One model call as the scripted model saw it."""

    messages: list[ChatMessage]
    tools: list[str]  # names, in the order sent


@dataclass(frozen=True)
class SentMessage:
    handle: str
    text: str
    at: float  # the runner's clock when osascript would have run


class RecordingOsaRunner:
    """An ``OsaRunner`` that records sends instead of running osascript. Always succeeds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.sent: list[SentMessage] = []
        self._changed = asyncio.Event()

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> OsaResult:
        if len(argv) != 5 or argv[0] != OSASCRIPT:
            raise ValueError("RecordingOsaRunner: unexpected argv shape")
        self.sent.append(SentMessage(handle=argv[3], text=argv[4], at=self.clock()))
        self._changed.set()
        return OsaResult(0, "", "")

    async def wait_for(self, count: int, timeout_s: float) -> SentMessage:
        """Wait until at least ``count`` sends were recorded; return the ``count``-th."""
        async with asyncio.timeout(timeout_s):
            while len(self.sent) < count:
                self._changed.clear()
                await self._changed.wait()
        return self.sent[count - 1]


async def wait_until(
    predicate: Callable[[], bool], timeout_s: float, *, poll_s: float = 0.01
) -> None:
    """Poll ``predicate`` until it is true. Raises ``TimeoutError`` after ``timeout_s``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() >= deadline:
            raise TimeoutError("condition not met in time")
        await asyncio.sleep(poll_s)


# --- a fake channel adapter (PER-292) ----------------------------------------------------------


@dataclass(frozen=True)
class FakeSent:
    principal: str
    text: str
    thread: str | None  # the thread it went to (``MessageRef.thread``), None unthreaded
    dedupe_key: str | None
    ref: str
    mention: tuple[str, ...] = ()  # PER-320: the principals an approval prompt tagged


class FakeChannel:
    """An in-memory ``Channel`` adapter named ``name`` (see the module docstring).

    ``threaded=False`` ignores ``thread`` (iMessage-shaped). ``fail_start`` makes ``start``
    raise (the hub leaves it out). ``fail_selftest`` makes ``self_test`` report a failure.
    """

    def __init__(
        self,
        name: str,
        principals: Principals,
        *,
        threaded: bool = True,
        fail_start: bool = False,
        fail_selftest: bool = False,
        clock: Callable[[], datetime] = utcnow,
        vouch: bool = False,
    ) -> None:
        self._name = name
        self.principals = principals
        self.threaded = threaded
        # PER-293: the adapter vouches every sender is in its workspace (Slack's team_id
        # check, PER-295), so an unlisted one resolves to the workspace default.
        self.vouch = vouch
        self.fail_start = fail_start
        self.fail_selftest = fail_selftest
        self.clock = clock
        self.sent: list[FakeSent] = []
        self.prompts: list[ApprovalPrompt] = []
        # PER-319: an approval prompt to one of these principals raises its error (a DM the
        # Slack app can't post: ``messages_tab_disabled``)
        self.fail_approval: dict[str, ChannelError] = {}
        self.selftests = 0
        self._submit: Submit | None = None
        self._keys: dict[str, MessageRef] = {}
        self._n = 0
        self.injected = 0  # messages injected so far (the last one's ref is <name>-in:<n>)
        self._started_at: datetime | None = None
        self._changed = asyncio.Event()

    @property
    def name(self) -> str:
        return self._name

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]

    async def start(self, submit: Submit) -> None:
        if self.fail_start:
            raise RuntimeError(f"{self._name}: can't connect")
        self._submit = submit
        self._started_at = self.clock()

    async def stop(self) -> None:
        self._submit = None

    def health(self) -> dict[str, Any]:
        at = self._started_at
        return {"ok": at is not None, "last_read_at": iso(at) if at else None, "last_error": None}

    async def self_test(self, *, timeout_s: float = 10.0) -> SelfTestResult:
        self.selftests += 1
        if self.fail_selftest:
            return SelfTestResult(False, "no round trip")
        return SelfTestResult(True, "ok")

    # -- inbound --

    def inject(
        self,
        uid: str,
        text: str,
        *,
        thread: str | None = None,
        at: datetime | None = None,
    ) -> bool:
        """Submit a message from ``<name>:<uid>`` as the adapter would; False when it isn't
        submitted (an unknown identity is dropped, as an adapter does, or not started)."""
        identity = f"{self._name}:{uid}"
        principal = self.principals.resolve(identity, vouched=self.vouch)
        if principal is None or self._submit is None:
            return False
        self.injected += 1
        ref = f"{self._name}-in:{self.injected}"
        event = Event(
            source=self._name,
            type="message",
            principal=principal,
            payload={
                "text": text,
                "identity": identity,
                "ref": ref,
                "thread": (thread or ref) if self.threaded else None,
                "at": iso(at or self.clock()),
                "truncated": False,
            },
            dedupe_key=f"{self._name}:{ref}",
            occurred_at=at or self.clock(),
        )
        return self._submit(event)

    # -- outbound --

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        return self._send(principal, text, thread=thread, dedupe_key=dedupe_key)

    def _send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None,
        dedupe_key: str | None,
        mention: tuple[str, ...] = (),
    ) -> MessageRef:
        p = self.principals.get(principal)
        if p is None or not any(i.startswith(f"{self._name}:") for i in p.identities):
            raise ChannelRefused(f"no {self._name} identity")
        if dedupe_key is not None and (prev := self._keys.get(dedupe_key)) is not None:
            return prev
        self._n += 1
        where = thread.thread if self.threaded and thread is not None else None
        ref = MessageRef(self._name, f"{self._name}-out:{self._n}", where)
        tags = mention if where is not None else ()
        self.sent.append(FakeSent(principal, text, where, dedupe_key, ref.ref, tags))
        if dedupe_key is not None:
            self._keys[dedupe_key] = ref
        self._changed.set()
        return ref

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        if (error := self.fail_approval.get(principal)) is not None:
            raise error
        key = approval_dedupe_key(prompt.approval_id, principal)
        if key not in self._keys:
            self.prompts.append(prompt)
        return self._send(principal, prompt.text(), thread=prompt.thread, dedupe_key=key,
                          mention=prompt.mention)  # fmt: skip

    async def wait_for(self, count: int, timeout_s: float) -> FakeSent:
        """Wait until at least ``count`` sends were recorded; return the ``count``-th."""
        async with asyncio.timeout(timeout_s):
            while len(self.sent) < count:
                self._changed.clear()
                await self._changed.wait()
        return self.sent[count - 1]

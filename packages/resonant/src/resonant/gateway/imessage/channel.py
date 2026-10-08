"""``IMessageChannel``: the iMessage ``Channel`` adapter (reader + osascript sender).

Usage::

    channel = IMessageChannel(settings.channels.imessage, principals, conn)
    await channel.start(loop.submit)        # starts the chat.db reader as a task
    ref = await channel.send("justin", "done", dedupe_key=task.idempotency_key)
    result = await channel.self_test()      # SelfTestResult(ok, detail)
    await channel.stop()

Nothing in the daemon starts it yet (the daemon wiring ticket does).

**Outbound allowlist.** ``send(principal, ...)`` takes a principal *name*, never a handle.
The destination is resolved only from principals.yaml: the handle in
``channels.imessage.preferred_handles[principal]`` if it is one of that principal's
``imessage:`` identities, otherwise the principal's first ``imessage:`` identity. An
unknown principal, or one with no iMessage identity, raises :class:`ChannelRefused` and
is audited ``imessage.send_refused``.

**Dedupe.** With a ``dedupe_key``, a key that was already sent returns the earlier
``MessageRef`` and sends nothing. Records live in kv ``imessage.sent:<sha256(key)>`` as
``{ref, at}``, expire after 7 days, and are swept by :meth:`start`. A send that fails
partway (a later chunk fails) is not recorded, so a retry resends from the first chunk.

**Rate limit.** A token bucket per principal: ``outbound_per_minute`` tokens (default 60),
refilled continuously, one token per chunk. When the bucket is short, ``send`` waits up to
30s for it (audited ``imessage.send_rate_limited`` with ``decision="delayed"``). If it would
need longer, it raises :class:`ChannelRateLimited` without sending anything (audited with
``decision="refused"``). Sends are serialized: one osascript at a time.

**Long text** is split at about 2,000 characters, preferring paragraph, then sentence,
then word boundaries, and each part gets a ``" (i/n)"`` suffix.

**Refs.** ``MessageRef(channel="imessage", ref="imessage-out:<id>")``; the id is generated,
not the chat.db guid. ``thread`` is ignored: iMessage chats here are 1:1 and unthreaded.

**Self-test.** :meth:`self_test` sends ``resonant-selftest:<nonce>`` to ``self_handle``
and waits up to 10s for a reader to see it in chat.db. The nonce is random per run and
held only in memory; a sighting counts only if it carries this nonce *and* comes from
``self_handle``. The ``is_from_me = 1`` row proves the send and the read path; the
incoming copy (``is_from_me = 0``) is reported in the detail but not required. The
result goes to kv ``imessage.selftest`` as ``{ok, at, detail}`` and the audit log as
``imessage.selftest``.

If the channel is started, the self-test listens on its running reader. Otherwise (the
``resonant selftest imessage`` CLI) it polls a private reader with its own cursor
(kv ``imessage.selftest.cursor``, reset to chat.db's newest row at the start of each run),
so it can run next to a live daemon without moving the daemon's cursor. Both readers
drop self-test rows before routing, so self-test messages are never answered. The private
reader drops every event it reads; the daemon's reader still delivers them.

**PII.** Bodies never reach logs or audit rows: audits carry the principal, handle,
length, chunk count, and dedupe key.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NoReturn

from resonant.config import IMessageConfig
from resonant.gateway.channel import (
    ApprovalPrompt,
    ChannelRateLimited,
    ChannelRefused,
    ChannelSendError,
    MessageRef,
    SelfTestResult,
    Submit,
)
from resonant.gateway.imessage.reader import (
    SELFTEST_PREFIX,
    IMessageReader,
    SelfTestSighting,
    UnsupportedSchemaError,
)
from resonant.gateway.imessage.sender import IMessageSender, OsaRunner, SendError
from resonant.principals import Principals
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow
from resonant_sdk import Event, new_id

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Monotonic = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

CHANNEL_NAME = "imessage"
DEDUPE_PREFIX = "imessage.sent:"
DEDUPE_TTL = timedelta(days=7)
SELFTEST_KV = "imessage.selftest"
SELFTEST_CURSOR_KEY = "imessage.selftest.cursor"
MAX_CHUNK = 2000
RATE_MAX_WAIT_S = 30.0
SELFTEST_TIMEOUT_S = 10.0
SELFTEST_COPY_GRACE_S = 2.0  # after the sent row, how long to wait for the incoming copy
SELFTEST_POLL_S = 0.25

_SUFFIX_ROOM = 12  # " (99/99)" plus slack
_BREAKS: tuple[tuple[str, ...], ...] = (("\n\n",), (". ", "! ", "? ", "\n"), (" ",))


# --- text splitting ---------------------------------------------------------------------


def split_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split ``text`` into parts of at most ``limit`` chars, each suffixed ``" (i/n)"``.

    Text that fits is returned unchanged as a single part.
    """
    if len(text) <= limit:
        return [text]
    body_limit = limit - _SUFFIX_ROOM
    parts: list[str] = []
    rest = text
    while len(rest) > body_limit:
        cut = _cut(rest, body_limit)
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    n = len(parts)
    return [f"{p} ({i}/{n})" for i, p in enumerate(parts, 1)]


def _cut(s: str, limit: int) -> int:
    """Where to cut ``s`` (at most ``limit``): the last good boundary past the halfway mark."""
    window = s[:limit]
    floor = limit // 2
    for seps in _BREAKS:
        best = max((i + len(sep) for sep in seps if (i := window.rfind(sep)) >= 0), default=-1)
        if best > floor:
            return best
    return limit


# --- rate limiting ----------------------------------------------------------------------


@dataclass
class TokenBucket:
    """``capacity`` tokens, refilled at ``capacity`` per minute. Tokens may go negative
    when a caller reserves ahead; the next caller then waits for the debt to refill."""

    capacity: float
    tokens: float
    updated: float

    def reserve(self, n: int, now: float, max_wait_s: float) -> float | None:
        """Take ``n`` tokens. Returns the seconds to wait first, or None (nothing taken)."""
        rate = self.capacity / 60.0
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * rate)
        self.updated = now
        wait = max(0.0, (n - self.tokens) / rate)
        if wait > max_wait_s:
            return None
        self.tokens -= n
        return wait


# --- the channel ------------------------------------------------------------------------


@dataclass
class _Sightings:
    sent_at: float | None = None
    copy_seen: bool = False


def _drop(_: Event) -> bool:
    return False


class IMessageChannel:
    """The iMessage ``Channel``. ``conn`` is Resonant's store (kv, audit), not chat.db.

    ``runner`` runs osascript (inject a fake in tests). ``clock`` stamps kv/audit records;
    ``monotonic`` and ``sleep`` drive the rate limiter.
    """

    def __init__(
        self,
        config: IMessageConfig,
        principals: Principals,
        conn: sqlite3.Connection,
        *,
        runner: OsaRunner | None = None,
        reader: IMessageReader | None = None,
        clock: Clock = utcnow,
        monotonic: Monotonic = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.config = config
        self.principals = principals
        self.conn = conn
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.sender = IMessageSender(runner)
        self.reader = (
            reader if reader is not None else IMessageReader(config, principals, conn, clock)
        )
        self._buckets: dict[str, TokenBucket] = {}
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return CHANNEL_NAME

    # --- lifecycle -----------------------------------------------------------------------

    async def start(self, submit: Submit) -> None:
        """Sweep expired dedupe records and start the chat.db reader as a background task."""
        if self._task is not None:
            raise RuntimeError("imessage channel already started")
        swept = sweep_sent(self.conn, self.clock())
        if swept:
            log.info("imessage: swept %d expired send records", swept)
        self._task = asyncio.create_task(self.reader.run(submit), name="imessage-reader")

    async def stop(self) -> None:
        """Stop the reader and wait for it. Safe to call when not started."""
        task, self._task = self._task, None
        if task is None:
            return
        self.reader.stop()
        await task

    def health(self) -> dict[str, Any]:
        """The reader's health: ``{ok, last_read_at, last_error, fda_ok}``."""
        return self.reader.health()

    # --- outbound ------------------------------------------------------------------------

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        """Text ``principal`` at their principals.yaml handle. See the module docstring.

        Raises :class:`ChannelRefused`, :class:`ChannelRateLimited`, or
        :class:`ChannelSendError`. ``thread`` is accepted for the protocol and ignored.
        """
        del thread  # 1:1 chats only; nothing to thread
        if not text:
            raise ValueError("cannot send an empty message")
        handle = self._resolve(principal)
        if dedupe_key is not None and (prev := self._already_sent(dedupe_key)) is not None:
            return prev
        chunks = split_text(text)
        async with self._send_lock:
            if dedupe_key is not None and (prev := self._already_sent(dedupe_key)) is not None:
                return prev  # a concurrent send with the same key finished first
            await self._throttle(principal, len(chunks), dedupe_key)
            for i, chunk in enumerate(chunks, 1):
                try:
                    await self.sender.send(handle, chunk)
                except SendError as e:
                    with atomic(self.conn):
                        audit(
                            self.conn,
                            "imessage.send_failed",
                            requested_by=principal,
                            idempotency_key=dedupe_key,
                            handle=handle,
                            reason=str(e),
                            code=e.code,
                            chunk=i,
                            chunks=len(chunks),
                        )
                    log.warning("imessage send to %s failed: %s", principal, e)
                    raise ChannelSendError(str(e)) from None
            ref = MessageRef(channel=CHANNEL_NAME, ref=f"imessage-out:{new_id()}")
            with atomic(self.conn):
                if dedupe_key is not None:
                    kv_set(
                        self.conn,
                        _dedupe_kv(dedupe_key),
                        {"ref": ref.ref, "at": iso(self.clock())},
                    )
                audit(
                    self.conn,
                    "imessage.sent",
                    requested_by=principal,
                    idempotency_key=dedupe_key,
                    handle=handle,
                    length=len(text),
                    chunks=len(chunks),
                    ref=ref.ref,
                )
        log.info("imessage sent to %s (%d chars, %d part(s))", principal, len(text), len(chunks))
        return ref

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        raise NotImplementedError("Phase 2")

    def _resolve(self, principal: str) -> str:
        p = self.principals.principals.get(principal)
        if p is None:
            self._refuse(principal, "unknown principal")
        handles = [i.partition(":")[2] for i in p.identities if i.startswith("imessage:")]
        if not handles:
            self._refuse(principal, "principal has no imessage identity")
        preferred = self.config.preferred_handles.get(principal)
        if preferred is not None and preferred not in handles:
            log.warning("imessage: preferred handle for %s is not one of its identities", principal)
        return preferred if preferred in handles else handles[0]

    def _refuse(self, principal: str, reason: str) -> NoReturn:
        with atomic(self.conn):
            audit(self.conn, "imessage.send_refused", requested_by=principal, reason=reason)
        raise ChannelRefused(f"imessage: {reason}: {principal!r}")

    def _already_sent(self, dedupe_key: str) -> MessageRef | None:
        record = kv_get(self.conn, _dedupe_kv(dedupe_key))
        if not isinstance(record, dict):
            return None
        ref, at = record.get("ref"), record.get("at")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(ref, str) or not isinstance(at, str):
            return None
        try:
            expired = self.clock() - datetime.fromisoformat(at) >= DEDUPE_TTL
        except ValueError:
            return None
        return None if expired else MessageRef(channel=CHANNEL_NAME, ref=ref)

    async def _throttle(self, principal: str, n: int, dedupe_key: str | None) -> None:
        now = self.monotonic()
        cap = float(self.config.outbound_per_minute)
        bucket = self._buckets.setdefault(principal, TokenBucket(cap, cap, now))
        wait = bucket.reserve(n, now, RATE_MAX_WAIT_S)
        if wait == 0:
            return
        decision = "refused" if wait is None else "delayed"
        with atomic(self.conn):
            audit(
                self.conn,
                "imessage.send_rate_limited",
                requested_by=principal,
                idempotency_key=dedupe_key,
                decision=decision,
                wait_s=None if wait is None else round(wait, 3),
                limit_per_minute=self.config.outbound_per_minute,
            )
        if wait is None:
            raise ChannelRateLimited(
                f"imessage: {principal!r} is over {self.config.outbound_per_minute} "
                f"messages/minute; not sent"
            )
        log.info("imessage: rate limit for %s, waiting %.1fs", principal, wait)
        await self.sleep(wait)

    # --- self-test -----------------------------------------------------------------------

    async def self_test(self, *, timeout_s: float = SELFTEST_TIMEOUT_S) -> SelfTestResult:
        """Send a nonce to ``self_handle`` and read it back. Records kv + audit."""
        result = await self._self_test(timeout_s)
        with atomic(self.conn):
            kv_set(
                self.conn,
                SELFTEST_KV,
                {"ok": result.ok, "at": iso(self.clock()), "detail": result.detail},
            )
            audit(
                self.conn,
                "imessage.selftest",
                decision="ok" if result.ok else "failed",
                detail=result.detail,
            )
        if result.ok:
            log.info("imessage self-test ok: %s", result.detail)
        else:
            log.warning("imessage self-test FAILED: %s", result.detail)
        return result

    async def _self_test(self, timeout_s: float) -> SelfTestResult:
        me = self.config.self_handle
        if me is None:
            return SelfTestResult(
                False,
                "channels.imessage.self_handle is not set: set it to Resonant's Apple ID email",
            )
        nonce = secrets.token_urlsafe(12)  # [A-Za-z0-9_-]{16}: matches SELFTEST_RE
        loop = asyncio.get_running_loop()
        seen = _Sightings()

        def on_sighting(s: SelfTestSighting) -> None:
            # Trust boundary: only this run's nonce, and only from Resonant's own handle.
            if s.nonce != nonce or s.handle != me:
                return
            if s.is_from_me:
                if seen.sent_at is None:
                    seen.sent_at = loop.time()
            else:
                seen.copy_seen = True

        standalone = self._task is None
        reader = self.reader
        if standalone:
            reader = IMessageReader(
                self.config, self.principals, self.conn, self.clock, cursor_key=SELFTEST_CURSOR_KEY
            )
        unregister = reader.register_selftest_callback(on_sighting)
        try:
            if standalone:
                with atomic(self.conn):
                    kv_set(self.conn, SELFTEST_CURSOR_KEY, None)
                try:
                    reader.poll_once(_drop)  # baseline: start at chat.db's newest row
                except Exception as e:
                    return SelfTestResult(False, f"cannot read chat.db: {_reader_error(e)}")
            else:
                # Right after start() the reader hasn't set its baseline yet; a row written
                # before that would be skipped as history. Wait for its first read.
                ready_by = loop.time() + timeout_s
                while reader.health()["last_read_at"] is None:
                    if loop.time() >= ready_by:
                        error = reader.health()["last_error"] or "no successful read yet"
                        return SelfTestResult(False, f"cannot read chat.db: {error}")
                    await asyncio.sleep(SELFTEST_POLL_S / 5)
            started = loop.time()
            try:
                await self.sender.send(me, SELFTEST_PREFIX + nonce)
            except SendError as e:
                return SelfTestResult(False, f"send failed: {e}")
            deadline = started + timeout_s
            last_error: str | None = None
            while True:
                if standalone:
                    try:
                        reader.poll_once(_drop)
                        last_error = None
                    except Exception as e:
                        last_error = _reader_error(e)
                else:
                    last_error = reader.health()["last_error"]
                now = loop.time()
                if seen.sent_at is not None and (
                    seen.copy_seen or now >= seen.sent_at + SELFTEST_COPY_GRACE_S
                ):
                    break
                if now >= deadline:
                    break
                await asyncio.sleep(SELFTEST_POLL_S)
        finally:
            unregister()
            if standalone:
                reader.close()
        if seen.sent_at is None:
            detail = f"sent, but it did not appear in chat.db within {timeout_s:g}s"
            if last_error:
                detail += f": {last_error}"
            else:
                detail += f" (is Messages signed in to iMessage as {me}?)"
            return SelfTestResult(False, detail)
        detail = f"sent to {me} and read back from chat.db in {seen.sent_at - started:.1f}s"
        if not seen.copy_seen:
            detail += "; the incoming copy was not seen"
        return SelfTestResult(True, detail)


# --- helpers ----------------------------------------------------------------------------


def _dedupe_kv(dedupe_key: str) -> str:
    return DEDUPE_PREFIX + hashlib.sha256(dedupe_key.encode()).hexdigest()


def sweep_sent(conn: sqlite3.Connection, now: datetime) -> int:
    """Delete send-dedupe records older than :data:`DEDUPE_TTL`. Returns the count."""
    cutoff = iso(now - DEDUPE_TTL)
    with atomic(conn):
        cur = conn.execute(
            "DELETE FROM kv WHERE key LIKE ? AND "
            "(json_extract(value, '$.at') IS NULL OR json_extract(value, '$.at') < ?)",
            (DEDUPE_PREFIX + "%", cutoff),
        )
    return cur.rowcount


def _reader_error(e: Exception) -> str:
    if isinstance(e, UnsupportedSchemaError):
        return f"unsupported chat.db schema: {e}"
    if isinstance(e, OSError | sqlite3.Error):
        return str(e)  # PermissionError carries the Full Disk Access fix
    return type(e).__name__


__all__ = [
    "DEDUPE_TTL",
    "MAX_CHUNK",
    "SELFTEST_CURSOR_KEY",
    "SELFTEST_KV",
    "IMessageChannel",
    "TokenBucket",
    "split_text",
    "sweep_sent",
]

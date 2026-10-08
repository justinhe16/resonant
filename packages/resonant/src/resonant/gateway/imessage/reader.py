"""iMessage inbound reader: new rows in Messages' ``chat.db`` become Events.

This is the receive half of the iMessage channel. The channel adapter (sender ticket)
composes it; nothing in the daemon starts it yet.

Usage::

    reader = IMessageReader(settings.channels.imessage, principals, conn)
    unregister = reader.register_selftest_callback(on_nonce)
    task = asyncio.create_task(reader.run(loop.submit))
    ...
    reader.stop()          # run() returns at its next wakeup (within ~250ms)
    await task
    reader.health()        # {"ok", "last_read_at", "last_error", "fda_ok"}

**Reading.** ``chat.db`` is opened with ``file:<path>?mode=ro`` and is never written to or
checkpointed. Each read takes up to ``BATCH`` rows with ``ROWID`` above the cursor (kv
``imessage.cursor``). With no cursor yet, the reader starts at the current max ROWID, so
history is never replayed. Reads happen every ``poll_s`` and shortly (debounced 100ms)
after ``chat.db-wal`` changes, which is checked every 250ms.

**Filters**, applied to every row in order:

1. Self-test: a body matching ``SELFTEST_RE`` goes to the registered self-test callbacks
   and never becomes an Event. This runs first, regardless of direction or sender, because
   the self-test's own rows are ``is_from_me = 1`` (the send) and come from Resonant's own
   handle (the received copy).
2. ``is_from_me = 1`` rows are skipped (Resonant's own outgoing messages).
3. Rows whose ``message.service`` isn't ``iMessage`` (SMS, RCS) are skipped.
4. Tapbacks and other associated messages (``associated_message_type != 0``) are skipped.
5. Resonant's own handle (``self_handle``) is skipped, so it is never audited as unknown.
6. Anything not in a 1:1 chat is skipped and audited ``imessage.group_ignored``.
7. A body that is in neither ``text`` nor a parseable ``attributedBody`` is audited
   ``imessage.parse_failed``. Empty bodies (attachments only) are skipped.
8. The handle (emails lowercased, E.164 kept) is resolved with
   ``Principals.resolve("imessage:<handle>")``. Unknown senders are audited
   ``imessage.unknown_sender`` and dropped.
9. More than ``inbound_per_minute`` messages from one principal within 60s are dropped and
   audited ``imessage.rate_limited``.

Surviving rows become ``Event(source="imessage", type="message", principal=...,
dedupe_key="imessage:<guid>")`` and go to ``submit``. The cursor advances only past rows
that were fully handled, once per batch. A crash replays at most one batch, and the
dedupe key makes that harmless.

**PII.** Message bodies never go to logs or audit rows. Audits carry handles, guids and
counts only.

**Failures never escape ``run()``.** A busy or locked database is retried on the next
poll. A permission error opening the file means Full Disk Access is missing
(``fda_ok=False``). A missing column reports "unsupported chat.db schema". All of these
show in ``health()`` and are retried every poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sqlite3
import urllib.parse
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from resonant.config import IMessageConfig
from resonant.gateway.channel import Submit
from resonant.gateway.imessage.attributed_body import parse_attributed_body
from resonant.principals import Principals
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow
from resonant_sdk import Event

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

CURSOR_KEY = "imessage.cursor"
BATCH = 200
SELFTEST_PREFIX = "resonant-selftest:"
SELFTEST_RE = re.compile(r"^resonant-selftest:[A-Za-z0-9_-]{8,}$")
STYLE_DIRECT = 45  # chat.style for a 1:1 chat (43 is a group)
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

WAL_CHECK_S = 0.25
DEBOUNCE_S = 0.1
_RATE_WINDOW = timedelta(seconds=60)
_OBJECT_REPLACEMENT = "\ufffc"

FDA_MESSAGE = (
    "cannot open chat.db: grant Full Disk Access to the daemon's Python interpreter "
    "(System Settings > Privacy & Security > Full Disk Access)"
)

# The columns the reader depends on. Anything missing means a macOS update changed the
# schema, and the reader refuses to guess.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "message": (
        "guid",
        "text",
        "attributedBody",
        "handle_id",
        "service",
        "is_from_me",
        "date",
        "associated_message_type",
    ),
    "handle": ("id",),
    "chat": ("guid", "style"),
    "chat_message_join": ("chat_id", "message_id"),
}

_QUERY = """
SELECT m.ROWID AS rowid, m.guid AS guid, m.text AS text, m.attributedBody AS body,
       m.service AS service, m.is_from_me AS is_from_me, m.date AS date,
       m.associated_message_type AS assoc_type, h.id AS handle,
       c.guid AS chat_guid, c.style AS chat_style
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
WHERE m.ROWID > ?
ORDER BY m.ROWID
LIMIT ?
"""


@dataclass(frozen=True)
class SelfTestSighting:
    """A chat.db row whose body is a self-test nonce message.

    Passed to self-test callbacks instead of becoming an Event. The checker decides what
    counts: typically the ``is_from_me`` row proves the send, and the incoming copy from
    ``self_handle`` proves the receive.
    """

    nonce: str  # the part after SELFTEST_PREFIX
    guid: str
    handle: str | None  # normalized
    is_from_me: bool
    service: str | None
    chat_guid: str | None
    at: datetime


SelfTestCallback = Callable[[SelfTestSighting], None]


class UnsupportedSchemaError(Exception):
    pass


class SubmitError(Exception):
    """``submit`` raised. The row is retried on the next read."""


def normalize_handle(raw: str) -> str:
    """Normalize a chat.db handle to principals.yaml form: lowercase emails, E.164 as-is."""
    handle = raw.strip()
    return handle.lower() if "@" in handle else handle


def from_apple_time(value: int | None) -> datetime | None:
    """Convert ``message.date`` to UTC. Modern macOS stores ns since 2001-01-01; old: s."""
    if not value:
        return None
    seconds = value / 1e9 if abs(value) > 10**11 else float(value)
    return APPLE_EPOCH + timedelta(seconds=seconds)


class IMessageReader:
    """Reads new inbound iMessages from ``chat.db`` and submits them as Events.

    ``conn`` is Resonant's own store (kv cursor and audit rows), not chat.db. ``clock``
    drives rate limiting and ``last_read_at``. All methods run on the event-loop thread.
    """

    def __init__(
        self,
        config: IMessageConfig,
        principals: Principals,
        conn: sqlite3.Connection,
        clock: Clock = utcnow,
    ) -> None:
        self.config = config
        self.principals = principals
        self.conn = conn
        self.clock = clock
        self._chat: sqlite3.Connection | None = None
        self._stop = asyncio.Event()
        self._selftest: list[SelfTestCallback] = []
        self._recent: dict[str, deque[datetime]] = {}
        self._last_read_at: datetime | None = None
        self._last_error: str | None = None
        self._fda_ok = True

    # --- public API --------------------------------------------------------------------

    def register_selftest_callback(self, callback: SelfTestCallback) -> Callable[[], None]:
        """Call ``callback`` for every self-test nonce row seen. Returns an unregister fn.

        Callbacks run synchronously inside a read. Exceptions are logged and swallowed.
        """
        self._selftest.append(callback)

        def unregister() -> None:
            with contextlib.suppress(ValueError):
                self._selftest.remove(callback)

        return unregister

    async def run(self, submit: Submit) -> None:
        """Read until :meth:`stop`. Never raises except ``CancelledError``."""
        wal = Path(f"{self.config.db_path}-wal")
        try:
            while not self._stop.is_set():
                # Snapshot the WAL before reading, so a write that lands mid-read still
                # triggers the next read instead of waiting out poll_s.
                seen = _stat(wal)
                self._safe_poll(submit)
                await self._wait_for_change(wal, seen)
        finally:
            self._close()
            self._stop.clear()  # a stop() before or during run() is honored; reusable after

    def stop(self) -> None:
        """Ask :meth:`run` to return. Safe to call at any time, including before run."""
        self._stop.set()

    def health(self) -> dict[str, Any]:
        """``{ok, last_read_at, last_error, fda_ok}``. ``ok`` needs one successful read."""
        return {
            "ok": self._last_error is None and self._last_read_at is not None,
            "last_read_at": iso(self._last_read_at) if self._last_read_at else None,
            "last_error": self._last_error,
            "fda_ok": self._fda_ok,
        }

    def poll_once(self, submit: Submit) -> int:
        """Read one batch of new rows. Returns the number of Events submitted.

        Raises on database errors; :meth:`run` uses :meth:`_safe_poll`, which records them
        in health instead. Useful for tests and for a self-test that wants a read now.
        """
        chat = self._open()
        cursor = kv_get(self.conn, CURSOR_KEY)
        max_rowid = int(chat.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0])
        if not isinstance(cursor, int) or cursor > max_rowid:
            # First start: begin at the newest row so history is never replayed. A cursor
            # past the end means chat.db was recreated; start over from its current end.
            with atomic(self.conn):
                if cursor is not None:
                    audit(self.conn, "imessage.cursor_reset", before=cursor, after=max_rowid)
                kv_set(self.conn, CURSOR_KEY, max_rowid)
            self._mark_read()
            return 0
        rows = chat.execute(_QUERY, (cursor, BATCH)).fetchall()
        submitted = 0
        pending_audits: list[tuple[str, dict[str, Any]]] = []
        done = cursor
        try:
            for row in rows:
                rowid = int(row["rowid"])
                if rowid <= done:
                    continue  # the same message joined to a second chat
                event = self._handle_row(row, pending_audits)
                if event is not None:
                    try:
                        accepted = submit(event)
                    except Exception as e:
                        raise SubmitError(type(e).__name__) from e
                    submitted += int(accepted)
                done = rowid
        finally:
            if done != cursor or pending_audits:
                with atomic(self.conn):
                    for action, detail in pending_audits:
                        audit(self.conn, action, **detail)
                    kv_set(self.conn, CURSOR_KEY, done)
        self._mark_read()
        if rows:
            log.debug("imessage read: %d rows, %d events", len(rows), submitted)
        return submitted

    # --- internals ---------------------------------------------------------------------

    def _handle_row(
        self, row: sqlite3.Row, audits: list[tuple[str, dict[str, Any]]]
    ) -> Event | None:
        guid = str(row["guid"])
        raw_text: str | None = row["text"]
        parse_failed = False
        if raw_text is None and row["body"] is not None:
            raw_text = parse_attributed_body(bytes(row["body"]))
            parse_failed = raw_text is None
        # U+FFFC marks an attachment's position; an attachment-only message has no text.
        text = (raw_text or "").replace(_OBJECT_REPLACEMENT, "").strip()
        handle = normalize_handle(row["handle"]) if row["handle"] else None
        at = from_apple_time(row["date"]) or self.clock()

        if SELFTEST_RE.match(text):
            self._dispatch_selftest(
                SelfTestSighting(
                    nonce=text[len(SELFTEST_PREFIX) :],
                    guid=guid,
                    handle=handle,
                    is_from_me=bool(row["is_from_me"]),
                    service=row["service"],
                    chat_guid=row["chat_guid"],
                    at=at,
                )
            )
            return None
        if row["is_from_me"]:
            return None
        if row["service"] != "iMessage":
            return None
        if row["assoc_type"]:
            return None
        if handle is None or handle == self.config.self_handle:
            return None
        if row["chat_style"] != STYLE_DIRECT:
            detail = {"guid": guid, "handle": handle, "chat": row["chat_guid"]}
            audits.append(("imessage.group_ignored", detail))
            return None
        if parse_failed:
            audits.append(("imessage.parse_failed", {"guid": guid, "handle": handle}))
            return None
        if not text:
            return None
        principal = self.principals.resolve(f"imessage:{handle}")
        if principal is None:
            audits.append(("imessage.unknown_sender", {"guid": guid, "handle": handle}))
            return None
        if not self._allow(principal):
            audits.append(
                (
                    "imessage.rate_limited",
                    {
                        "requested_by": principal,
                        "limit_per_minute": self.config.inbound_per_minute,
                    },
                )
            )
            return None
        truncated = len(text) > self.config.max_body_chars
        if truncated:
            text = text[: self.config.max_body_chars]
        return Event(
            source="imessage",
            type="message",
            principal=principal,
            payload={
                "text": text,
                "guid": guid,
                "handle": handle,
                "chat_guid": row["chat_guid"],
                "at": iso(at),
                "truncated": truncated,
            },
            dedupe_key=f"imessage:{guid}",
            occurred_at=at,
        )

    def _allow(self, principal: str) -> bool:
        now = self.clock()
        recent = self._recent.setdefault(principal, deque())
        while recent and now - recent[0] >= _RATE_WINDOW:
            recent.popleft()
        if len(recent) >= self.config.inbound_per_minute:
            return False
        recent.append(now)
        return True

    def _dispatch_selftest(self, sighting: SelfTestSighting) -> None:
        for callback in list(self._selftest):
            try:
                callback(sighting)
            except Exception:
                log.exception("imessage self-test callback failed")

    def _open(self) -> sqlite3.Connection:
        if self._chat is not None:
            return self._chat
        path = self.config.db_path
        # Probe with a plain open(2) first: without Full Disk Access, TCC denies it with
        # EPERM/EACCES, which sqlite would only report as "unable to open database file".
        try:
            fd = os.open(path, os.O_RDONLY)
        except PermissionError as e:
            self._fda_ok = False
            raise PermissionError(FDA_MESSAGE) from e
        except FileNotFoundError as e:
            raise FileNotFoundError(f"chat.db not found at {path}") from e
        os.close(fd)
        self._fda_ok = True
        uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
        # timeout=0: never block the event loop on a busy chat.db; retry on the next poll.
        chat = sqlite3.connect(
            uri, uri=True, timeout=0, isolation_level=None, check_same_thread=False
        )
        try:
            chat.row_factory = sqlite3.Row
            _check_schema(chat)
        except BaseException:
            chat.close()
            raise
        self._chat = chat
        return chat

    def _close(self) -> None:
        if self._chat is not None:
            with contextlib.suppress(sqlite3.Error):
                self._chat.close()
            self._chat = None

    def _mark_read(self) -> None:
        self._last_read_at = self.clock()
        if self._last_error is not None:
            log.info("imessage reader recovered")
        self._last_error = None

    def _safe_poll(self, submit: Submit) -> None:
        try:
            self.poll_once(submit)
        except asyncio.CancelledError:
            raise
        except PermissionError as e:
            self._fail(str(e), close=True)
        except UnsupportedSchemaError as e:
            self._fail(f"unsupported chat.db schema: {e}", close=True)
        except SubmitError as e:
            self._fail(f"submit failed: {e}", close=False)
        except sqlite3.OperationalError as e:
            if _is_busy(e):
                self._fail(f"chat.db busy, retrying: {e}", close=False)
            elif _is_permission(e):
                self._fda_ok = False
                self._fail(FDA_MESSAGE, close=True)
            elif "no such column" in str(e) or "no such table" in str(e):
                self._fail(f"unsupported chat.db schema: {e}", close=True)
            else:
                self._fail(f"chat.db error: {e}", close=True)
        except (OSError, sqlite3.Error) as e:
            self._fail(str(e), close=True)
        except Exception as e:
            # A bug, or bad data. The message text may be in str(e), so log the type only.
            self._fail(f"reader error: {type(e).__name__}", close=True)

    def _fail(self, message: str, *, close: bool) -> None:
        if message != self._last_error:
            log.warning("imessage reader: %s", message)
        self._last_error = message
        if close:
            self._close()

    async def _wait_for_change(self, wal: Path, seen: tuple[int, int] | None) -> None:
        """Sleep up to ``poll_s``, returning early (debounced) when ``wal`` changes."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.poll_s
        while not self._stop.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), min(WAL_CHECK_S, remaining))
            if _stat(wal) != seen:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), DEBOUNCE_S)
                return


def _check_schema(chat: sqlite3.Connection) -> None:
    missing: list[str] = []
    for table, columns in REQUIRED_COLUMNS.items():
        have = {str(r[1]) for r in chat.execute(f"PRAGMA table_info({table})")}
        missing += [f"{table}.{c}" for c in columns if c not in have]
    if missing:
        raise UnsupportedSchemaError("missing " + ", ".join(missing))


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _is_busy(e: sqlite3.OperationalError) -> bool:
    code = getattr(e, "sqlite_errorcode", None)
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    msg = str(e)
    return "locked" in msg or "busy" in msg


def _is_permission(e: sqlite3.OperationalError) -> bool:
    code = getattr(e, "sqlite_errorcode", None)
    return code in (sqlite3.SQLITE_PERM, sqlite3.SQLITE_AUTH) or "authorization denied" in str(e)


__all__ = [
    "BATCH",
    "CURSOR_KEY",
    "FDA_MESSAGE",
    "SELFTEST_PREFIX",
    "SELFTEST_RE",
    "IMessageReader",
    "SelfTestCallback",
    "SelfTestSighting",
    "SubmitError",
    "UnsupportedSchemaError",
    "from_apple_time",
    "normalize_handle",
]

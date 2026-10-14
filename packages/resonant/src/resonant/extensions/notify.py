"""Extension notifications, delivered from persisted events (D10).

A job run's events are stored first, as unrouted ``Event(source="ext:<name>",
type="ext.<name>.<type>", dedupe_key="ext:<name>:<key>")`` rows (``scheduler.persist_events``,
in the transaction that records the run). The loop hands every unrouted event to its router
hook; ``wiring.route_event`` sends ``ext:*`` ones here, to :meth:`Notifier.route`, which
returns None (consumed) or raises (the loop retries every tick, dead-letters after 5).

For one event, in order:

1. Not from a loaded extension (removed, disabled, tampered or refused at this start), or
   a type outside the manifest's ``notify.on``: consumed, nothing sent, never retried
   (``digest`` is stored for the briefing, never notified).
2. ``payload.baseline`` (the job's first successful run after install, see
   :func:`take_baseline`): consumed unsent, audit ``ext.notify_held`` ``reason=baseline``.
   State that existed before the install is never announced.
3. Paused (kill switch; unknown counts as paused) or muted (kv
   ``ext.<name>.muted_until`` in the future): consumed unsent, ``ext.notify_held``. Nothing
   is queued, so ``/resume`` or the end of a mute never floods.
4. Over ``extensions.notify.max_per_hour`` sent this clock hour for the extension:
   consumed unsent, ``ext.notify_capped``. Once the hour is over, :meth:`Notifier.flush`
   sends one ``[<name>] +N more updates``.
5. Otherwise ``[<name>] <text>`` goes to each recipient (the gate's approvers for the
   extension, owner first) with ``dedupe_key="<event dedupe key>:<recipient>"``, then
   ``ext.notify_sent``. A recipient the channel refuses is skipped; any other channel error
   raises, and the retry's sends are deduped.

Exactly once. A crash after the event is stored but before the send leaves it unrouted: it
is delivered on the next start. A crash after the send but before the loop consumes it is
caught by the channel's dedupe record (written with the send). The per-hour count is the
distinct event keys audited ``ext.notify_sent``, so a retried event never counts twice.

Anti-spoofing (:func:`render`). The text is untrusted: one line of printable ASCII plus a
few status symbols, ``[ ] { }`` become ``( )`` so ``[A7]`` reads ``(A7)``, a code-like token
next to ``yes``/``no``/``stop`` is masked, every ``reply`` and a leading ``yes``/``no``/
``stop`` is quoted, and the whole is clipped to ``max_chars``. The code-built prefix comes
first, so a notification never starts like an approval prompt (``[A7] …? Reply yes / no``).

Mute (the API ``/ext mute`` calls): :func:`mute` and :func:`unmute` write kv
``ext.<name>.muted_until`` and audit ``ext.notify_muted`` / ``ext.notify_unmuted``;
:func:`muted_until` reads it (None once it has passed).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from resonant import killswitch
from resonant.config import NotifyConfig
from resonant.gateway.channel import ChannelError, ChannelRefused
from resonant.ops import Sender
from resonant.store import extensions as store
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow
from resonant_sdk import Event, Manifest

log = logging.getLogger(__name__)

SOURCE_PREFIX = "ext:"
OVERFLOW_KEY = "ext.notify.overflow"  # kv: {ext: [hour iso, ...]} with capped events
FLUSH_EVERY_S = 60.0

# Non-ASCII symbols extensions use for status; everything else outside printable ASCII
# (zero-width, bidi, look-alike scripts, control characters) becomes a space.
SYMBOLS = frozenset(
    "\u2713\u2717\u2714\u2718\u26a0"  # check and cross marks, warning
    "\u25cf\u2022\u00b7\u2026\u2013\u2014"  # dots, ellipsis, dashes
    "\u2192\u2190\u2191\u2193\u25b6"  # arrows
    "\U0001f440\U0001f534\U0001f7e2\U0001f7e1\u23f0\u270b"  # eyes, red/green/yellow, alarm, hand
)
_BRACKETS = str.maketrans({"[": "(", "]": ")", "{": "(", "}": ")"})
_SEP = r"[\s:,;&'\"()?!.-]+"
_CODE = r"(?<![A-Za-z0-9])[A-Za-z][\s\-_.:/]*[0-9](?![0-9])"
_CODE_AFTER_REPLY = re.compile(rf"(?i)\b(yes|no|stop)({_SEP}){_CODE}")
_CODE_BEFORE_REPLY = re.compile(rf"(?i){_CODE}({_SEP})(yes|no|stop)\b")
_REPLY_WORD = re.compile(r"(?i)\breply\b")
_LEADING = re.compile(r"(?i)^(yes|no|stop|reply)\b")
_CODE_LIKE_NAME = re.compile(r"^[a-z]-*[0-9]")

Recipients = Callable[[str], Sequence[str]]
ManifestOf = Callable[[str], Manifest | None]


def _muted_key(ext: str) -> str:
    return f"ext.{ext}.muted_until"


def _baseline_key(ext: str, job_id: str) -> str:
    return f"ext.{ext}.baseline.{job_id}"


# --- text ----------------------------------------------------------------------------------


def prefix(name: str) -> str:
    """``[github]``; ``(a7)`` for a name that could pass for an approval code."""
    return f"({name})" if _CODE_LIKE_NAME.match(name) else f"[{name}]"


def clean_text(text: str) -> str:
    """The extension's text made safe to forward (see the module docstring), unclipped."""
    norm = unicodedata.normalize("NFKC", text)
    kept = "".join(ch if 32 <= ord(ch) < 127 or ch in SYMBOLS else " " for ch in norm)
    flat = " ".join(kept.translate(_BRACKETS).split())
    flat = _CODE_AFTER_REPLY.sub(lambda m: f"{m.group(1)}{m.group(2)}?", flat)
    flat = _CODE_BEFORE_REPLY.sub(lambda m: f"?{m.group(1)}{m.group(2)}", flat)
    flat = _REPLY_WORD.sub(lambda m: f"'{m.group(0)}'", flat)
    return _LEADING.sub(lambda m: f"'{m.group(1)}'", flat)


def render(name: str, text: str, max_chars: int = 300) -> str:
    """``[<name>] <cleaned text>``, at most ``max_chars`` characters."""
    head = prefix(name) + " "
    body = clean_text(text) or "update"
    room = max_chars - len(head)
    if len(body) > room:
        body = body[: room - 1].rstrip() + "…"
    return head + body


def more_text(name: str, n: int) -> str:
    return f"{prefix(name)} +{n} more update{'' if n == 1 else 's'}"


# --- mute ----------------------------------------------------------------------------------


def muted_until(conn: sqlite3.Connection, ext: str, now: datetime | None = None) -> datetime | None:
    """When ``ext``'s mute ends, or None if it isn't muted (never set, or already past)."""
    raw: object = kv_get(conn, _muted_key(ext))
    if not isinstance(raw, str):
        return None
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if until.utcoffset() is None:
        return None
    return until if until > (now or utcnow()) else None


def mute(conn: sqlite3.Connection, ext: str, until: datetime, *, by: str) -> None:
    """Hold ``ext``'s notifications until ``until`` (timezone-aware). Audited
    ``ext.notify_muted``. Commits on its own (or joins the caller's transaction)."""
    if until.utcoffset() is None:
        raise ValueError("until must be timezone-aware")
    with atomic(conn):
        kv_set(conn, _muted_key(ext), iso(until))
        audit(conn, "ext.notify_muted", extension=ext, requested_by=by, until=iso(until))


def unmute(conn: sqlite3.Connection, ext: str, *, by: str, now: datetime | None = None) -> bool:
    """Lift ``ext``'s mute; False if it wasn't muted (as of ``now``, default the wall
    clock). Audited ``ext.notify_unmuted``."""
    with atomic(conn):
        was = muted_until(conn, ext, now) is not None
        conn.execute("DELETE FROM kv WHERE key = ?", (_muted_key(ext),))
        audit(conn, "ext.notify_unmuted", extension=ext, requested_by=by, was_muted=was)
    return was


# --- baseline ------------------------------------------------------------------------------


def take_baseline(conn: sqlite3.Connection, ext: str, job_id: str) -> bool:
    """True exactly once per job per install: for its first successful run (whose events
    are then stored with ``payload.baseline=true`` and never sent). Call it inside the
    transaction that records the run. The marker is the install time, so a reinstall
    baselines again. Core jobs and unknown extensions: False."""
    record = store.get_extension(conn, ext)
    if record is None:
        return False
    key, mark = _baseline_key(ext, job_id), iso(record.installed_at)
    if kv_get(conn, key) == mark:
        return False
    kv_set(conn, key, mark)
    return True


# --- the notifier --------------------------------------------------------------------------


def paused(conn: sqlite3.Connection) -> bool:
    try:
        return killswitch.is_paused(conn)
    except killswitch.PausedStateUnknownError:
        return True


def clock_hour(at: datetime) -> datetime:
    return at.replace(minute=0, second=0, microsecond=0)


def sent_in_hour(
    conn: sqlite3.Connection, ext: str, hour: datetime, dedupe_key: str | None = None
) -> int:
    """Distinct events sent immediately for ``ext`` in the clock hour ``hour``: notifier
    sends (``ext.notify_sent``) and urgent pulse sends (``pulse.sent``) share one count, the
    per-hour flood cap. ``dedupe_key`` itself (a retry of an event already sent) doesn't
    count against it."""
    row = conn.execute(
        """SELECT COUNT(DISTINCT idempotency_key) FROM audit_log
           WHERE action IN ('ext.notify_sent', 'pulse.sent') AND extension = ?
             AND json_extract(detail, '$.hour') = ? AND idempotency_key IS NOT ?""",
        (ext, iso(hour), dedupe_key),
    ).fetchone()
    return int(row[0])


class Notifier:
    """Delivers ``ext:*`` events (see the module docstring). ``recipients(ext)`` is the
    gate's approvers (owner first); ``manifest_of(ext)`` the loaded manifest or None."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        channel: Sender,
        recipients: Recipients,
        manifest_of: ManifestOf,
        config: NotifyConfig | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.conn = conn
        self.channel = channel
        self.recipients = recipients
        self.manifest_of = manifest_of
        self.config = config or NotifyConfig()
        self.clock = clock
        self.sleep = sleep
        self._stop = asyncio.Event()

    # --- one event ---------------------------------------------------------------------------

    async def route(self, event: Event) -> None:
        """Deliver or hold one ``ext:*`` event. Returns None (consume it); raises a
        ``ChannelError`` to have the loop retry it."""
        ext = event.source.removeprefix(SOURCE_PREFIX)
        type_prefix = f"ext.{ext}."
        if not event.source.startswith(SOURCE_PREFIX) or not event.type.startswith(type_prefix):
            self._audit("ext.notify_dropped", ext, event, reason="malformed")
            return None
        etype = event.type.removeprefix(type_prefix)
        manifest = self.manifest_of(ext)
        if manifest is None:
            self._audit("ext.notify_dropped", ext, event, reason="not loaded")
            return None
        if manifest.notify is None or etype not in manifest.notify.on:
            return None  # stored for other readers (the briefing), never notified
        now = self.clock()
        if event.payload.get("baseline") is True:
            self._audit("ext.notify_held", ext, event, reason="baseline")
            return None
        if paused(self.conn):
            self._audit("ext.notify_held", ext, event, reason="paused")
            return None
        if muted_until(self.conn, ext, now) is not None:
            self._audit("ext.notify_held", ext, event, reason="muted")
            return None
        hour = clock_hour(now)
        if self._sent_since(ext, hour, event.dedupe_key) >= self.config.max_per_hour:
            self._cap(ext, event, hour)
            return None
        text: object = event.payload.get("text")
        message = render(ext, text if isinstance(text, str) else "", self.config.max_chars)
        sent = await self._send_all(ext, message, f"{event.dedupe_key}:")
        action = "ext.notify_sent" if sent else "ext.notify_unreachable"
        self._audit(action, ext, event, recipients=sent, hour=iso(hour))
        return None

    async def _send_all(self, ext: str, text: str, key_prefix: str) -> int:
        """Send to every recipient; returns how many took it. A refused recipient is
        skipped; any other channel error propagates (the dedupe keys make retries safe)."""
        sent = 0
        for recipient in self.recipients(ext) or ():
            try:
                await self.channel.send(recipient, text, dedupe_key=key_prefix + recipient)
            except ChannelRefused:
                log.warning("notification recipient refused", extra={"extension": ext})
                continue
            sent += 1
        return sent

    def _sent_since(self, ext: str, hour: datetime, dedupe_key: str | None) -> int:
        return sent_in_hour(self.conn, ext, hour, dedupe_key)

    def _cap(self, ext: str, event: Event, hour: datetime) -> None:
        with atomic(self.conn):
            pending = _overflow(self.conn)
            hours = pending.setdefault(ext, [])
            if iso(hour) not in hours:
                hours.append(iso(hour))
            kv_set(self.conn, OVERFLOW_KEY, pending)
            self._audit("ext.notify_capped", ext, event, hour=iso(hour))

    # --- "+N more" at the end of the hour ----------------------------------------------------

    async def flush(self) -> int:
        """Send ``[<name>] +N more updates`` for every finished hour that had capped events;
        returns how many hours were flushed. Held (not sent) while paused or muted."""
        now = self.clock()
        current = iso(clock_hour(now))
        done = 0
        for ext, hours in sorted(_overflow(self.conn).items()):
            for hour in sorted(hours):
                if hour >= current:
                    continue  # this hour isn't over yet
                n = self._capped_in(ext, hour)
                if n and not paused(self.conn) and muted_until(self.conn, ext, now) is None:
                    await self._send_all(ext, more_text(ext, n), f"ext-more:{ext}:{hour}:")
                    action = "ext.notify_overflow"
                else:
                    action = "ext.notify_held"
                with atomic(self.conn):
                    pending = _overflow(self.conn)
                    left = [h for h in pending.get(ext, []) if h != hour]
                    if left:
                        pending[ext] = left
                    else:
                        pending.pop(ext, None)
                    kv_set(self.conn, OVERFLOW_KEY, pending)
                    audit(self.conn, action, extension=ext, hour=hour, count=n)
                done += 1
        return done

    def _capped_in(self, ext: str, hour: str) -> int:
        row = self.conn.execute(
            """SELECT COUNT(DISTINCT idempotency_key) FROM audit_log
               WHERE action = 'ext.notify_capped' AND extension = ?
                 AND json_extract(detail, '$.hour') = ?""",
            (ext, hour),
        ).fetchone()
        return int(row[0])

    async def run(self, interval_s: float = FLUSH_EVERY_S) -> None:
        """Flush every ``interval_s`` until :meth:`stop`. One failed round never ends it."""
        while not self._stop.is_set():
            try:
                await self.flush()
            except ChannelError as e:
                log.warning("notification flush failed: %s", type(e).__name__)
            except Exception:
                log.exception("notification flush failed")
            await self.sleep(interval_s)

    def stop(self) -> None:
        self._stop.set()

    def _audit(self, action: str, ext: str, event: Event, **detail: Any) -> None:
        with atomic(self.conn):
            audit(
                self.conn,
                action,
                extension=ext,
                idempotency_key=event.dedupe_key,
                trace_id=event.trace_id,
                event_type=event.type,
                **detail,
            )


def _overflow(conn: sqlite3.Connection) -> dict[str, list[str]]:
    raw: object = kv_get(conn, OVERFLOW_KEY)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in cast(Mapping[object, object], raw).items():
        if isinstance(k, str) and isinstance(v, list):
            out[k] = [h for h in cast(list[object], v) if isinstance(h, str)]
    return out


__all__ = [
    "Notifier",
    "clean_text",
    "clock_hour",
    "more_text",
    "mute",
    "muted_until",
    "paused",
    "prefix",
    "render",
    "sent_in_hour",
    "take_baseline",
    "unmute",
]

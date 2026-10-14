"""Extension reports (``item`` events) and the urgent path (docs/pulse-plan.md D3).

A job run stores each ``report()`` as an unrouted ``ext.<name>.item`` event
(``scheduler.persist_events``: ``payload.text`` is the title, ``payload.data`` holds
``key``, ``urgency``, ``state``, ``clear`` and ``ttl``). ``wiring.route_event`` hands those
to :meth:`Pulse.route` (other ``ext:*`` events go to the notifier). It returns None
(consumed) or raises a ``ChannelError`` (the loop retries every tick, dead-letters after 5).

For one event, in order:

1. Not from a loaded extension, the manifest has no ``report:`` grant, or the payload is bad
   (key, urgency, state, ttl, title): consumed, audit ``pulse.refused`` with a reason. Never
   retried.
2. Urgency is clamped to ``report.max_urgency`` (urgent > normal > low). The title is cleaned
   with ``notify.clean_text`` before it is stored; sending adds the code-built ``[name]``
   prefix through ``notify.render``, so a report never looks like an approval prompt.
3. The item is upserted (``store.pulse``; source = the extension name). A baseline event
   (``payload.baseline``, the job's first run after install) is stored as told and never sent:
   audit ``pulse.held`` ``reason=baseline``.
4. An urgent item the owner hasn't heard (fingerprint != ``told_fingerprint``) is sent now:
   ``[name] 🔴 <title>`` (``✓`` for ``clear``) to each recipient (the gate's approvers), with
   dedupe key ``pulse:urgent:<event id>:<recipient>``, then ``mark_told`` and audit
   ``pulse.sent``. While paused (unknown counts as paused), muted, or over the per-hour cap
   (shared with the notifier: ``notify.sent_in_hour``) it stays pending, audit ``pulse.held``
   with the reason, and the hourly pulse carries it.
5. Normal and low items are only stored: pending for the hourly pulse and the briefing.

Exactly once. The event is stored before it is routed, the send is deduped by event id, and
``mark_told`` runs after it. A crash between the send and ``mark_told`` leaves the event
unrouted: the retry upserts the same item (still untold), the channel drops the repeated
send, and ``mark_told`` runs. A recipient the channel refuses is skipped; any other channel
error raises.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from resonant.config import NotifyConfig
from resonant.extensions.notify import (
    SOURCE_PREFIX,
    ManifestOf,
    Recipients,
    clean_text,
    clock_hour,
    muted_until,
    paused,
    render,
    sent_in_hour,
)
from resonant.gateway.channel import ChannelRefused
from resonant.ops import Sender
from resonant.store import pulse as items
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, utcnow
from resonant_sdk import Event

log = logging.getLogger(__name__)

EVENT_TYPE = "item"
URGENT_MARK = "\U0001f534"  # red circle
CLEAR_MARK = "✓"  # check mark
_RANK: dict[str, int] = {"urgent": 2, "normal": 1, "low": 0}
_TTL = re.compile(r"^(\d+)([smh])$")
_TTL_UNIT_S = {"s": 1, "m": 60, "h": 3600}


def clamp(urgency: items.Urgency, grant: items.Urgency) -> items.Urgency:
    """``urgency``, or ``grant`` when that is lower (urgent > normal > low)."""
    return urgency if _RANK[urgency] <= _RANK[grant] else grant


def _ttl_s(raw: object) -> int:
    if raw is None:
        return items.DEFAULT_TTL_S
    m = _TTL.fullmatch(raw) if isinstance(raw, str) else None
    if m is None:
        raise ValueError("ttl must look like 30m or 24h")
    return int(m.group(1)) * _TTL_UNIT_S[m.group(2)]


class Pulse:
    """Ingests ``ext.<name>.item`` events (see the module docstring). ``recipients(ext)`` is
    the gate's approvers (owner first); ``manifest_of(ext)`` the loaded manifest or None."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        channel: Sender,
        recipients: Recipients,
        manifest_of: ManifestOf,
        config: NotifyConfig | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.conn = conn
        self.channel = channel
        self.recipients = recipients
        self.manifest_of = manifest_of
        self.config = config or NotifyConfig()
        self.clock = clock

    async def route(self, event: Event) -> None:
        """Store one item report and send it now if it is urgent news. Returns None (consume
        it); raises a ``ChannelError`` to have the loop retry it."""
        ext = event.source.removeprefix(SOURCE_PREFIX)
        if not event.source.startswith(SOURCE_PREFIX) or event.type != f"ext.{ext}.{EVENT_TYPE}":
            self._audit("pulse.refused", ext, event, reason="malformed")
            return None
        manifest = self.manifest_of(ext)
        if manifest is None:
            self._audit("pulse.refused", ext, event, reason="not loaded")
            return None
        if manifest.report is None:
            self._audit("pulse.refused", ext, event, reason="no report grant")
            return None
        now = self.clock()
        try:
            item, urgency = self._store(ext, event, manifest.report.max_urgency, now)
        except ValueError as e:
            self._audit("pulse.refused", ext, event, reason=f"bad item: {e}"[:200])
            return None
        if event.payload.get("baseline") is True:
            items.mark_told(self.conn, [item], now)
            self._audit("pulse.held", ext, event, reason="baseline", key=item.key)
            return None
        if urgency != "urgent" or not item.changed:
            return None  # normal and low wait for the pulse; the owner already heard this one
        hour = clock_hour(now)
        reason = None
        if paused(self.conn):
            reason = "paused"
        elif muted_until(self.conn, ext, now) is not None:
            reason = "muted"
        elif sent_in_hour(self.conn, ext, hour, event.dedupe_key) >= self.config.max_per_hour:
            reason = "capped"
        if reason is not None:
            self._audit("pulse.held", ext, event, reason=reason, key=item.key)
            return None
        mark = CLEAR_MARK if item.cleared else URGENT_MARK
        message = render(ext, f"{mark} {item.title}", self.config.max_chars)
        sent = 0
        for recipient in self.recipients(ext) or ():
            try:
                await self.channel.send(
                    recipient, message, dedupe_key=f"pulse:urgent:{event.id}:{recipient}"
                )
            except ChannelRefused:
                log.warning("pulse recipient refused", extra={"extension": ext})
                continue
            sent += 1
        items.mark_told(self.conn, [item], now)
        action = "pulse.sent" if sent else "pulse.unreachable"
        self._audit(action, ext, event, recipients=sent, hour=iso(hour), key=item.key)
        return None

    def _store(
        self, ext: str, event: Event, grant: items.Urgency, now: datetime
    ) -> tuple[items.Item, items.Urgency]:
        """Validate and upsert the report; ``ValueError`` for a bad one."""
        text: object = event.payload.get("text")
        raw: object = event.payload.get("data")
        data = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        key, urgency = data.get("key"), data.get("urgency", "normal")
        clear = data.get("clear", False)
        if not isinstance(key, str):
            raise ValueError("key must be a string")
        if urgency not in items.URGENCIES:
            raise ValueError(f"urgency must be one of {items.URGENCIES}")
        if not isinstance(clear, bool):
            raise ValueError("clear must be true or false")
        if not isinstance(text, str):
            raise ValueError("title must be a string")
        title = clean_text(text)
        granted = clamp(urgency, grant)
        _change, item = items.upsert(
            self.conn,
            ext,
            key,
            title,
            granted,
            data.get("state"),
            clear,
            _ttl_s(data.get("ttl")),
            now,
        )
        return item, granted

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


__all__ = ["EVENT_TYPE", "Pulse", "clamp"]

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
   ``[name] 🔴 <title>`` (``✓`` for ``clear``; 🟢 for ``tone: good``) to each recipient (the gate's
   approvers), with
   dedupe key ``pulse:urgent:<event id>:<recipient>``, then ``mark_told`` and audit
   ``pulse.sent``. While paused (unknown counts as paused), muted, or over the per-hour cap
   (shared with the notifier: ``notify.sent_in_hour``) it stays pending, audit ``pulse.held``
   with the reason, and the hourly pulse carries it.
5. Normal and low items are only stored: pending for the hourly pulse and the briefing.

A ``quiet`` report (``data.quiet: true``, BF-1) says there is nothing worth saying (every
count is 0, an empty digest): the item is stored cleared and marked told at once, so it drops
out of the briefing's Updates and the hourly pulse without a word (audit ``pulse.held``
``reason=quiet``), whatever its urgency. The next report that isn't quiet opens it again as
news. A report without the field (an extension older than BF-1) is not quiet. A quiet report
also replaces an earlier report of the same key that the owner never heard (an urgent one held
while paused, say): the extension's latest word is that there is nothing to say.

A report whose stored row is newer than the event (``updated_at`` > ``occurred_at``: a
retry of an older report after a later one was routed) is stale: audit ``pulse.stale``, no
upsert, no send. Rows are stamped with the event's ``occurred_at`` for this reason.

A ``clear`` of an item last stored urgent is urgent too (clamped to the grant), so "recovered"
is texted at once however the extension rated the clear; a clear of an item the owner never
heard (held) still sends. When no recipient takes an urgent send the item is not marked told
(``pulse.unreachable``) and the hourly pulse carries it.

Exactly once. The event is stored before it is routed, the send is deduped by event id, and
``mark_told`` runs after it. A crash between the send and ``mark_told`` leaves the event
unrouted: the retry upserts the same item (still untold), the channel drops the repeated
send, and ``mark_told`` runs. A recipient the channel refuses is skipped; any other channel
error raises.

Another instance's alerts (PER-301). :meth:`Pulse.ingest_instance` (personal Resonant's
``POST /api/notify-owner``) stores an urgent item of source ``instance:<name>`` and takes the
same urgent path (step 4), rendered ``[Cori] 🔴 <title>``, deduped by the item's fingerprint;
each sent text is also recorded in ``system_texts`` (``outside``), so the brains can answer
about it. On an instance with ``notify_owner`` (``bridge``), :meth:`Pulse.route` forwards each
urgent item it would send to personal Resonant too (``notify_owner.OwnerBridge``).

:func:`render_item` is the one-line form of a stored item (urgent sends and the hourly pulse):
``[name] <mark> <title>``, no prefix for ``core``; the title is already clean.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from resonant.config import NotifyConfig
from resonant.extensions.notify import (
    SOURCE_PREFIX,
    ManifestOf,
    Recipients,
    clean_text,
    clock_hour,
    muted_until,
    paused,
    render_clean,
    sent_in_hour,
)
from resonant.gateway.channel import ChannelRefused
from resonant.ops import Sender
from resonant.store import pulse as items
from resonant.store import system_texts
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, utcnow
from resonant_sdk import Event

if TYPE_CHECKING:
    from resonant.notify_owner import OwnerBridge

log = logging.getLogger(__name__)

EVENT_TYPE = "item"
INSTANCE_PREFIX = "instance:"  # PER-301: items another instance sent (POST /api/notify-owner)
Outcome = Literal["sent", "held", "told", "unreachable"]
URGENT_MARK = "\U0001f534"  # red circle
GOOD_MARK = "\U0001f7e2"  # green circle: an urgent item with ``tone: good``
CLEAR_MARK = "✓"  # check mark
_RANK: dict[str, int] = {"urgent": 2, "normal": 1, "low": 0}
_TTL = re.compile(r"^(\d+)([smh])$")
_TTL_UNIT_S = {"s": 1, "m": 60, "h": 3600}


def clamp(urgency: items.Urgency, grant: items.Urgency) -> items.Urgency:
    """``urgency``, or ``grant`` when that is lower (urgent > normal > low)."""
    return urgency if _RANK[urgency] <= _RANK[grant] else grant


def urgent_mark(item: items.Item) -> str:
    """The marker of an urgent item: green for ``tone: good`` news, else red."""
    return GOOD_MARK if item.tone == "good" else URGENT_MARK


def instance_source(instance: str) -> str:
    """The pulse source of another instance's alerts: ``instance:cori``."""
    return INSTANCE_PREFIX + instance


def source_label(source: str) -> str | None:
    """The code-built prefix name of a source: None for ``core``, the instance's name
    capitalized for ``instance:<name>`` (``[Cori]``, as ``instance.display_name`` defaults),
    else the extension name."""
    if source == "core":
        return None
    if source.startswith(INSTANCE_PREFIX):
        return source.removeprefix(INSTANCE_PREFIX).capitalize()
    return source


def render_item(item: items.Item, mark: str = "", max_chars: int = 300) -> str:
    """``[source] <mark> <title>`` for a stored item (its title is already cleaned, so it is
    not cleaned again). ``core`` items get no prefix; ``instance:cori`` items ``[Cori]``."""
    text = f"{mark} {item.title}" if mark else item.title
    return render_clean(source_label(item.source), text, max_chars)


def _ttl_s(raw: object) -> int:
    if raw is None:
        return items.DEFAULT_TTL_S
    m = _TTL.fullmatch(raw) if isinstance(raw, str) else None
    if m is None:
        raise ValueError("ttl must look like 30m or 24h")
    return int(m.group(1)) * _TTL_UNIT_S[m.group(2)]


class Pulse:
    """Ingests ``ext.<name>.item`` events (see the module docstring). ``recipients(ext)`` is
    the gate's approvers (owner first); ``manifest_of(ext)`` the loaded manifest or None.
    ``bridge`` (PER-301, an instance with ``notify_owner``): urgent news is also forwarded to
    personal Resonant, which texts the owner."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        channel: Sender,
        recipients: Recipients,
        manifest_of: ManifestOf,
        config: NotifyConfig | None = None,
        clock: Callable[[], datetime] = utcnow,
        bridge: OwnerBridge | None = None,
    ) -> None:
        self.conn = conn
        self.channel = channel
        self.recipients = recipients
        self.manifest_of = manifest_of
        self.config = config or NotifyConfig()
        self.clock = clock
        self.bridge = bridge

    async def route(self, event: Event) -> None:
        """Store one item report and send it now if it is urgent news. Returns None (consume
        it); raises a ``ChannelError`` to have the loop retry it."""
        ext = event.source.removeprefix(SOURCE_PREFIX)
        if not event.source.startswith(SOURCE_PREFIX) or event.type != f"ext.{ext}.{EVENT_TYPE}":
            self._audit_event("pulse.refused", ext, event, reason="malformed")
            return None
        manifest = self.manifest_of(ext)
        if manifest is None:
            self._audit_event("pulse.refused", ext, event, reason="not loaded")
            return None
        if manifest.report is None:
            self._audit_event("pulse.refused", ext, event, reason="no report grant")
            return None
        now = self.clock()
        try:
            stored = self._store(ext, event, manifest.report.max_urgency)
        except ValueError as e:
            self._audit_event("pulse.refused", ext, event, reason=f"bad item: {e}"[:200])
            return None
        if stored is None:
            self._audit_event("pulse.stale", ext, event, reason="newer report already stored")
            return None
        item, urgency, quiet = stored
        if quiet or event.payload.get("baseline") is True:
            items.mark_told(self.conn, [item], now)
            reason = "quiet" if quiet else "baseline"
            self._audit_event("pulse.held", ext, event, reason=reason, key=item.key)
            return None
        if urgency != "urgent" or not item.changed:
            return None  # normal and low wait for the pulse; the owner already heard this one
        if self.bridge is not None and self._hold_reason(ext, now, event.dedupe_key) in (
            None,
            "capped",  # personal Resonant keeps its own cap
        ):  # PER-301: another instance's urgent news reaches the owner through personal
            self.bridge.forward(f"ext/{ext}/{item.key}", f"{ext}: {item.title}",
                                clear=item.cleared)  # fmt: skip
        await self._send_urgent(
            ext, item, now, send_key=f"pulse:urgent:{event.id}", idempotency_key=event.dedupe_key,
            trace_id=event.trace_id, event_type=event.type,
        )  # fmt: skip
        return None

    async def ingest_instance(
        self, instance: str, key: str, title: str, *, clear: bool = False
    ) -> tuple[Outcome, str | None]:
        """PER-301: an urgent alert from another instance on this user (``POST
        /api/notify-owner``), stored as an item of source ``instance:<name>`` and sent like an
        extension's urgent report: ``[Cori] 🔴 <title>`` once per fingerprint (``✓`` for
        ``clear``), held while paused or muted (``/ext mute cori``) or over the per-hour cap.
        A sent text is recorded in ``system_texts`` (kind ``pulse``, ``outside``) so the brains
        see it as Resonant's own turn. Returns the outcome and the hold reason."""
        source = instance_source(instance)
        now = self.clock()
        before = items.get(self.conn, source, key)
        if clear and before is None:
            return "told", None  # an all-clear for something never said: nothing to say
        if clear and before is not None and before.told_fingerprint is None:
            # the alert was held and never texted: close it quietly, no ✓ for it
            _change, item = items.upsert(self.conn, source, key, clean_text(title), "urgent",
                                         None, True, items.DEFAULT_TTL_S, now, None)  # fmt: skip
            items.mark_told(self.conn, [item], now)
            return "told", None
        _change, item = items.upsert(
            self.conn, source, key, clean_text(title), "urgent", None, clear,
            items.DEFAULT_TTL_S, now, None,
        )  # fmt: skip
        if not item.changed:
            return "told", None
        idem = f"{source}:{key}:{item.fingerprint}"
        return await self._send_urgent(
            source, item, now, send_key=f"pulse:urgent:{idem}", idempotency_key=idem,
            trace_id=None, event_type=f"{source}.item", record=True,
        )  # fmt: skip

    def _hold_reason(self, source: str, now: datetime, dedupe_key: str | None) -> str | None:
        if paused(self.conn):
            return "paused"
        if muted_until(self.conn, source, now) is not None:
            return "muted"
        if sent_in_hour(self.conn, source, clock_hour(now), dedupe_key) >= self.config.max_per_hour:
            return "capped"
        return None

    async def _send_urgent(
        self,
        source: str,
        item: items.Item,
        now: datetime,
        *,
        send_key: str,
        idempotency_key: str | None,
        trace_id: str | None,
        event_type: str,
        record: bool = False,
    ) -> tuple[Outcome, str | None]:
        """Send an urgent item the owner hasn't heard to each recipient, or hold it (see the
        module docstring, step 4). ``record``: put the text in ``system_texts`` too."""
        detail: dict[str, Any] = {"idempotency_key": idempotency_key, "trace_id": trace_id,
                                  "event_type": event_type}  # fmt: skip
        hour = clock_hour(now)
        reason = self._hold_reason(source, now, idempotency_key)
        if reason is not None:
            self._audit("pulse.held", source, **detail, reason=reason, key=item.key)
            return "held", reason
        mark = CLEAR_MARK if item.cleared else urgent_mark(item)
        message = render_item(item, mark, self.config.max_chars)
        sent = 0
        for recipient in self.recipients(source) or ():
            key = f"{send_key}:{recipient}"
            try:
                await self.channel.send(recipient, message, dedupe_key=key)
            except ChannelRefused:
                log.warning("pulse recipient refused", extra={"extension": source})
                continue
            sent += 1
            if record:
                system_texts.record(self.conn, recipient, "pulse", message, ref=key, now=now,
                                    outside=True)  # fmt: skip
        if not sent:  # nobody took it: stays pending and rides the hourly pulse
            self._audit("pulse.unreachable", source, **detail, key=item.key)
            return "unreachable", None
        items.mark_told(self.conn, [item], now)
        self._audit("pulse.sent", source, **detail, recipients=sent, hour=iso(hour), key=item.key)
        return "sent", None

    def _store(
        self, ext: str, event: Event, grant: items.Urgency
    ) -> tuple[items.Item, items.Urgency, bool] | None:
        """Validate and upsert the report; ``ValueError`` for a bad one, None for a stale
        one (the stored row is newer than this event: a retry of an older report). The bool
        is ``quiet``: the item was stored cleared, for the caller to mark told unsaid."""
        text: object = event.payload.get("text")
        raw: object = event.payload.get("data")
        data = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        key, urgency = data.get("key"), data.get("urgency", "normal")
        clear = data.get("clear", False)
        quiet = data.get("quiet", False)
        tone = "good" if data.get("tone") == "good" else None  # anything else: no tone
        if not isinstance(key, str):
            raise ValueError("key must be a string")
        if urgency not in items.URGENCIES:
            raise ValueError(f"urgency must be one of {items.URGENCIES}")
        if not isinstance(clear, bool):
            raise ValueError("clear must be true or false")
        if not isinstance(quiet, bool):
            raise ValueError("quiet must be true or false")
        if not isinstance(text, str):
            raise ValueError("title must be a string")
        title = clean_text(text)
        before = items.get(self.conn, ext, key)
        if before is not None and before.updated_at > event.occurred_at:
            return None
        if quiet:
            clear = True  # closed without a word: out of every listing once marked told
        elif clear and before is not None and before.urgency == "urgent":
            urgency = "urgent"  # the all-clear for an urgent thing is immediate too
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
            event.occurred_at,
            tone,
        )
        return item, granted, quiet

    def _audit_event(self, action: str, ext: str, event: Event, **detail: Any) -> None:
        self._audit(action, ext, idempotency_key=event.dedupe_key, trace_id=event.trace_id,
                    event_type=event.type, **detail)  # fmt: skip

    def _audit(self, action: str, ext: str, **detail: Any) -> None:
        with atomic(self.conn):
            audit(self.conn, action, extension=ext, **detail)


__all__ = [
    "EVENT_TYPE",
    "INSTANCE_PREFIX",
    "Outcome",
    "Pulse",
    "clamp",
    "instance_source",
    "render_item",
    "source_label",
]

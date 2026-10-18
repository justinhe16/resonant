"""Brain-authored notices (BR-7, docs/brain-plan.md D10 and §1 "Proactive: sparingly").

A notice is something Resonant decides on its own to tell the owner (today: a watch fired, or
a watch stopped because its check kept failing). It goes **through the pulse**, as a core item
``core`` / ``notice/<key>`` (``store/pulse.py``), so it is said once and shows up in the
briefing like any other item. :func:`send_notice` then decides, in order:

1. **Paused** (kill switch; unknown counts as paused) or inside ``pulse.quiet_hours``: held
   as a ``normal`` item; the hourly pulse carries it once it may send (``reason`` ``paused``
   / ``quiet``).
2. **Hourly cap**: ``extensions.notify.max_per_hour`` notices sent this clock hour: held as
   ``normal`` (``capped_hour``), so it rides the next pulse.
3. **Daily cap**: ``brain.proactive_per_day`` (default 3) notices sent since local midnight
   (``settings.timezone``): held as ``low`` (``capped_day``): the next briefing mentions it
   and no other text goes out today. ``0`` holds every notice for the briefing.
4. Otherwise it is sent now to the owner (``dedupe_key=notice:<key>``), marked told, and
   audited ``brain.notice`` ``decision=sent`` with the clock hour. The counts in 2 and 3 are
   the distinct keys audited ``sent``, so a retry never counts twice.

Every hold is audited ``brain.notice`` with ``decision=held`` and the reason. The title is
cleaned with ``notify.clean_text`` (one line, no approval look-alikes) before it is stored;
whatever outside text it quotes (a Claude answer) is data, never a prompt.

The other proactive sources already exist and are not duplicated here: stalls are the
progress rollup's (``extensions/rollup.py``, in the hourly pulse), and health is the
``HealthMonitor``'s, which texts on a degrade and on the recovery itself (it is not capped
by this: a down model matters more than the budget).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from resonant.config import Settings
from resonant.extensions.notify import clean_text, clock_hour, paused
from resonant.extensions.pulse import render_item
from resonant.gateway.channel import ChannelRefused
from resonant.ops import Sender
from resonant.store import pulse as items
from resonant.store.audit import audit
from resonant.store.db import atomic, iso

log = logging.getLogger(__name__)

SOURCE = "core"
KEY_PREFIX = "notice/"
ACTION = "brain.notice"
TTL_S = 24 * 3600
Outcome = Literal["sent", "paused", "quiet", "capped_hour", "capped_day", "unreachable"]


@dataclass(frozen=True)
class Notice:
    outcome: Outcome
    key: str

    @property
    def sent(self) -> bool:
        return self.outcome == "sent"


def _day(now: datetime, tz: str) -> str:
    """The local date (``settings.timezone``) a notice counts against."""
    return now.astimezone(ZoneInfo(tz)).date().isoformat()


def _count(conn: sqlite3.Connection, field: str, value: str, key: str) -> int:
    """Distinct notices audited ``sent`` with ``detail.<field> = value`` (the audit row's own
    time is the wall clock; the detail carries the decision's), apart from ``key``."""
    row = conn.execute(
        f"""SELECT COUNT(DISTINCT idempotency_key) FROM audit_log
           WHERE action = ? AND decision = 'sent' AND json_extract(detail, '$.{field}') = ?
             AND idempotency_key IS NOT ?""",  # noqa: S608 - field is a constant
        (ACTION, value, key),
    ).fetchone()
    return int(row[0])


def sent_today(conn: sqlite3.Connection, now: datetime, tz: str) -> int:
    """Notices sent on ``now``'s local date."""
    return _count(conn, "day", _day(now, tz), "")


def item_key(key: str) -> str:
    """``notice/<key>``, with ``key`` made a valid pulse key."""
    clean = "".join(c if c.isalnum() or c in "/._:-" else "-" for c in key.lower())
    return (KEY_PREFIX + clean)[: items.KEY_MAX]


# One notice decides at a time: the count, the send and the ``sent`` audit row must not
# interleave (two watches firing together would both see room under the cap). One daemon,
# one event loop.
_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = _LOCKS[loop] = asyncio.Lock()
    return lock


async def send_notice(
    conn: sqlite3.Connection,
    channel: Sender,
    owner: str,
    settings: Settings,
    *,
    key: str,
    title: str,
    now: datetime,
) -> Notice:
    """Store the notice as a pulse item and send it now if the caps allow (module
    docstring). Raises a ``ChannelError`` other than a refusal (the caller retries; the
    send is deduped by key)."""
    async with _lock():
        return await _send_notice(conn, channel, owner, settings, key=key, title=title, now=now)


async def _send_notice(
    conn: sqlite3.Connection,
    channel: Sender,
    owner: str,
    settings: Settings,
    *,
    key: str,
    title: str,
    now: datetime,
) -> Notice:
    pkey = item_key(key)
    dedupe = f"notice:{pkey}"
    clean = clean_text(title)

    def store(urgency: items.Urgency) -> items.Item:
        with atomic(conn):
            _change, item = items.upsert(
                conn, SOURCE, pkey, clean, urgency, None, False, TTL_S, now
            )
        return item

    def held(reason: Outcome, urgency: items.Urgency) -> Notice:
        store(urgency)
        with atomic(conn):
            audit(
                conn,
                ACTION,
                requested_by=owner,
                idempotency_key=dedupe,
                decision="held",
                reason=reason,
                key=pkey,
            )
        return Notice(reason, pkey)

    before = items.get(conn, SOURCE, pkey)
    if before is not None and not before.changed:
        return Notice("sent", pkey)  # already told (a retry after the send)
    if paused(conn):
        return held("paused", "normal")
    local = now.astimezone(ZoneInfo(settings.timezone))
    if settings.pulse.in_quiet(local.time()):
        return held("quiet", "normal")
    hour, day = iso(clock_hour(now)), _day(now, settings.timezone)
    if _count(conn, "hour", hour, dedupe) >= settings.extensions.notify.max_per_hour:
        return held("capped_hour", "normal")
    today = _count(conn, "day", day, dedupe)
    if today >= settings.brain.proactive_per_day:
        return held("capped_day", "low")
    item = store("normal")
    text = render_item(item, "", settings.extensions.notify.max_chars)
    try:
        await channel.send(owner, text, dedupe_key=dedupe)
    except ChannelRefused:
        log.warning("brain notice refused by the channel")
        return held("unreachable", "normal")
    with atomic(conn):
        items.mark_told(conn, [item], now)
        audit(
            conn,
            ACTION,
            requested_by=owner,
            idempotency_key=dedupe,
            decision="sent",
            key=pkey,
            hour=hour,
            day=day,
            today=today + 1,
        )
    return Notice("sent", pkey)


__all__ = ["ACTION", "Notice", "item_key", "send_notice", "sent_today"]

"""Panel history (PER-286): which calendar day a panel archives next, and the rules around it.

A panel with ``history: {every: daily, keep: 365d}`` gets one archived copy per calendar day
in its ``tz`` (``ext_panel_days``). The scheduler (``extensions/scheduler.py``) asks
:func:`next_day` on each tick and runs the panel's command plus ``--date <day>`` for it,
through the same gate, runner, timeout, ``max_bytes`` and normalisation as a live run:

- **When.** A day is archivable once it has ended *and* the panel's local clock has passed
  00:15 of the next day (:func:`latest_day`), so the extension has had a moment to close it.
- **Window.** Every tick looks at the ``min(keep, 30)`` latest archivable days
  (:data:`BACKFILL_DAYS_MAX`), newest first: after an install, an update or a daemon start
  the missing ones are filled, the newest first; afterwards it is just yesterday. Days the
  owner queued with ``resonant ext panel-history --rebuild`` (status ``pending``) count too,
  anywhere inside ``keep``.
- **Outcomes.** ``ok`` (a good copy; the relay gets it once), ``none`` (the command printed
  ``{"schema": 1, "none": true}``: never retried, never listed), or a failed attempt: the day
  stays ``pending`` and is retried after :data:`RETRY_BACKOFF`, and after
  :data:`MAX_ATTEMPTS` it is ``failed`` (shown on the panel, never retried automatically, and
  it doesn't degrade the extension's health).
- **Immutable.** An ``ok``, ``none`` or ``failed`` day is never re-run on its own: only the
  rebuild CLI re-queues it.
- **Prune.** Days before ``today - keep`` are deleted (:func:`keep_from`).

Nothing here touches the store or runs anything; the scheduler, the snapshot, the local API,
the pusher and the CLI share these helpers.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

ARCHIVE_AFTER = time(0, 15)  # local time on the next day
BACKFILL_DAYS_MAX = 30
MAX_ATTEMPTS = 3
RETRY_BACKOFF: tuple[timedelta, ...] = (timedelta(minutes=15), timedelta(hours=1))
DEFER = timedelta(minutes=15)  # a run that didn't count (dry run, paused, killed)
PRUNE_EVERY = timedelta(hours=1)
DAY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


@dataclass(frozen=True)
class DayState:
    """What :func:`next_day` needs of a stored day."""

    status: str  # pending | ok | none | failed
    next_try_at: datetime | None


def parse_day(text: str) -> date | None:
    """``YYYY-MM-DD`` as a date, or None (any other shape, or no such day)."""
    if not DAY_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def latest_day(now: datetime, tz: ZoneInfo) -> date:
    """The newest day that may be archived at ``now`` (aware): yesterday in ``tz``, or the day
    before while it is still before 00:15 there."""
    local = now.astimezone(tz)
    back = 1 if local.time() >= ARCHIVE_AFTER else 2
    return local.date() - timedelta(days=back)


def window(now: datetime, tz: ZoneInfo, keep_days: int) -> list[date]:
    """The days the archiver fills on its own, newest first: ``min(keep, 30)`` of them."""
    last = latest_day(now, tz)
    return [last - timedelta(days=i) for i in range(min(keep_days, BACKFILL_DAYS_MAX))]


def keep_from(now: datetime, tz: ZoneInfo, keep_days: int) -> date:
    """The oldest day still kept: ``today - keep`` in ``tz`` (older rows are pruned)."""
    return now.astimezone(tz).date() - timedelta(days=keep_days)


def next_day(
    rows: Mapping[str, DayState], now: datetime, tz: ZoneInfo, keep_days: int
) -> date | None:
    """The day to archive now, newest first: a window day with no row, or a ``pending`` day
    (a retry or a rebuild, anywhere inside ``keep``) whose backoff is over. None: nothing."""
    oldest = keep_from(now, tz, keep_days)
    last = latest_day(now, tz)
    candidates = {d.isoformat() for d in window(now, tz, keep_days)}
    candidates |= {
        day
        for day, st in rows.items()
        if st.status == "pending" and (d := parse_day(day)) is not None and oldest <= d <= last
    }
    for day in sorted(candidates, reverse=True):
        st = rows.get(day)
        if st is None:
            return date.fromisoformat(day)
        if st.status == "pending" and (st.next_try_at is None or st.next_try_at <= now):
            return date.fromisoformat(day)
    return None


def retry_at(attempts: int, now: datetime) -> datetime | None:
    """When a day that has failed ``attempts`` times is tried again (None: it is failed)."""
    if attempts >= MAX_ATTEMPTS:
        return None
    return now + RETRY_BACKOFF[min(attempts, len(RETRY_BACKOFF)) - 1]


def panel_history(manifest: Mapping[str, Any], panel_id: str) -> tuple[int, str | None] | None:
    """``(keep_days, tz)`` of a stored manifest's panel that has ``history``, else None."""
    for spec in cast(list[Any], manifest.get("panels") or []):
        if not isinstance(spec, dict):
            continue
        fields = cast(dict[str, Any], spec)
        if fields.get("id") != panel_id:
            continue
        history = fields.get("history")
        if not isinstance(history, dict):
            return None
        keep = cast(dict[str, Any], history).get("keep", "365d")
        if not (isinstance(keep, str) and re.fullmatch(r"[0-9]{1,4}d", keep)):
            return None
        tz = fields.get("tz")
        return int(keep[:-1]), tz if isinstance(tz, str) else None
    return None


def history_panels(manifest: Mapping[str, Any]) -> list[str]:
    """The ids of a stored manifest's panels that have ``history``."""
    return [
        str(cast(dict[str, Any], p).get("id"))
        for p in cast(list[Any], manifest.get("panels") or [])
        if isinstance(p, dict) and isinstance(cast(dict[str, Any], p).get("history"), dict)
    ]


def days_between(first: date, last: date) -> Iterable[date]:
    """``first`` to ``last``, both included."""
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


__all__ = [
    "ARCHIVE_AFTER",
    "BACKFILL_DAYS_MAX",
    "DAY_RE",
    "DEFER",
    "MAX_ATTEMPTS",
    "PRUNE_EVERY",
    "RETRY_BACKOFF",
    "DayState",
    "days_between",
    "history_panels",
    "keep_from",
    "latest_day",
    "next_day",
    "panel_history",
    "parse_day",
    "retry_at",
    "window",
]

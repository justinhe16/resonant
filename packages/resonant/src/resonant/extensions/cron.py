"""Cron schedules and the scheduler's missed-run decisions, on ``cronsim``.

Pure functions over an explicit aware ``now`` and a zone (``settings.timezone``); no I/O.
The scheduler (``extensions/scheduler.py``) reads state, calls :func:`decide` and writes
what it says.

Expressions are standard five-field cron (``minute hour day-of-month month day-of-week``),
evaluated in local wall time with Debian cron's DST rules, as ``cronsim`` implements them:

- A fixed time (neither minute nor hour starts with ``*``, like ``30 2 * * *``): on spring
  forward, a time in the gap runs once, right after the jump (3:00); on fall back, a
  repeated time runs once, the first time.
- A minute or hour starting with ``*`` (``*/10 * * * *``, ``0 * * * *``) follows wall
  time: the skipped hour's times don't run, and the repeated hour's run twice.

A day-of-month and a day-of-week that are both restricted match either one (cron's OR).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError

# A due time at most this old when the scheduler sees it is on time, not late.
ON_TIME = timedelta(minutes=1)
# The longest ``catchup`` window a job may declare.
MAX_CATCHUP = timedelta(hours=24)
# How far back a decision enumerates due times; older ones are only flagged ``truncated``.
LOOKBACK = timedelta(days=31)
# At most this many missed or paused due times are listed (the most recent ones).
MAX_LISTED = 50


class CronError(ValueError):
    """An expression we can't schedule. ``str(e)`` says why."""


@dataclass(frozen=True)
class Schedule:
    """A validated cron expression in a zone. Build it with :func:`parse_schedule`."""

    expr: str
    tz: ZoneInfo

    def iter_after(self, after: datetime) -> Iterator[datetime]:
        """Due times strictly after ``after``, ascending, in UTC."""
        _aware(after, "after")
        # cronsim walks local wall time; seed it in the zone. In a fall-back fold the wall
        # clock repeats, and a gap time moves to just after the jump, so keep only times
        # really later than the previous one. Compare in UTC: aware datetimes sharing a
        # tzinfo compare by wall time, ignoring ``fold``.
        prev = after.astimezone(UTC)
        # cronsim ends the iteration after 50 years without a match.
        for local in CronSim(self.expr, after.astimezone(self.tz)):
            due = local.astimezone(UTC)
            if due > prev:
                prev = due
                yield due

    def next_after(self, after: datetime) -> datetime | None:
        """The first due time strictly after ``after``, or None if it never fires again."""
        return next(self.iter_after(after), None)

    def due_between(self, after: datetime, until: datetime) -> list[datetime]:
        """Due times in ``(after, until]``, ascending, in UTC."""
        _aware(until, "until")
        end = until.astimezone(UTC)
        out: list[datetime] = []
        for due in self.iter_after(after):
            if due > end:
                break
            out.append(due)
        return out


def parse_schedule(expr: str, tz: ZoneInfo | str) -> Schedule:
    """Validate ``expr`` (five fields) in ``tz``. Raises :class:`CronError` with the reason."""
    try:
        zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    except (ValueError, ZoneInfoNotFoundError) as e:
        raise CronError(f"unknown timezone {tz!r}") from e
    text = " ".join(expr.split())
    if not text:
        raise CronError("empty cron expression")
    if text.startswith("@"):
        raise CronError(f"{text!r}: write five fields, like '0 9 * * *'")
    fields = text.split(" ")
    if len(fields) != 5:
        raise CronError(
            f"{text!r}: want 5 fields (minute hour day month weekday), got {len(fields)}"
        )
    probe = datetime(2000, 1, 1, tzinfo=zone)
    try:
        CronSim(text, probe)
    except CronSimError as e:  # e.g. "Bad minute"
        raise CronError(f"{text!r}: {str(e).lower()}") from e
    schedule = Schedule(text, zone)
    if schedule.next_after(probe) is None:
        raise CronError(f"{text!r}: never fires")
    return schedule


class Outcome(StrEnum):
    IDLE = "idle"  # nothing came due
    RUN = "run"  # run ``due`` now (``late`` if caught up)
    PAUSED = "skipped_paused"  # audit each of ``skipped`` as job.skipped_paused
    OVERLAP = "overlap"  # ``due`` would run, but the previous run is live: audit job.overlap
    MISSED = "missed"  # nothing runs; ``missed`` lists them all


@dataclass(frozen=True)
class Downtime:
    """The daemon wasn't running over ``(since, until]``: the last tick before it stopped,
    and the ``daemon.started_at`` after it."""

    since: datetime
    until: datetime

    def covers(self, t: datetime) -> bool:
        _aware(t, "t")
        return self.since.astimezone(UTC) < t.astimezone(UTC) <= self.until.astimezone(UTC)


@dataclass(frozen=True)
class Decision:
    """One tick's verdict for one job. Every datetime is in UTC."""

    outcome: Outcome
    # The due time that runs (RUN) or was skipped because the previous run is live (OVERLAP).
    due: datetime | None = None
    # True when ``due`` is a catch-up (older than ``on_time``).
    late: bool = False
    # Due times that won't run, for job.missed audits: the most recent MAX_LISTED, ascending.
    missed: tuple[datetime, ...] = ()
    # Due times skipped while paused, for job.skipped_paused: likewise capped and ascending.
    skipped: tuple[datetime, ...] = ()
    # More due times than listed (the cap, or ``last_due`` older than LOOKBACK).
    truncated: bool = False
    # The new ``last_due`` (``store.extensions.mark_due``): the latest due time, or None.
    last_due: datetime | None = None


def decide(
    schedule: Schedule,
    *,
    now: datetime,
    last_due: datetime | None,
    paused: bool,
    catchup: timedelta | None,
    downtime: Downtime | None,
    busy: bool,
    on_time: timedelta = ON_TIME,
) -> Decision:
    """What one scheduler tick does with a job. Precedence: pause, then catch-up, then overlap.

    - The due times considered are ``(last_due, now]``. With no ``last_due`` (the job was
      never claimed) only an on-time one counts and nothing is missed.
    - **Pause wins.** Paused, every due time is skipped and ``last_due`` moves past them,
      so nothing is caught up after a resume.
    - **Catch-up.** Only the latest due time can run. It runs if it is on time (within
      ``on_time`` of ``now``; pass at least the tick interval), or if ``catchup`` is set,
      it is within ``catchup`` of ``now``, **and** ``downtime`` covers it (the daemon
      wasn't running, as opposed to paused or stalled). Every other due time is missed.
    - **Overlap.** ``busy`` (the previous run is still running, or its task is waiting)
      turns a run into OVERLAP; the due time is still consumed.
    """
    _aware(now, "now")
    now = now.astimezone(UTC)
    if catchup is not None and not timedelta(0) <= catchup <= MAX_CATCHUP:
        raise ValueError(f"catchup must be between 0 and {MAX_CATCHUP}")
    truncated = False
    if last_due is None:
        start = now - on_time
    else:
        _aware(last_due, "last_due")
        last_due = last_due.astimezone(UTC)
        start = max(last_due, now - LOOKBACK)
        if start > last_due:  # due times before the lookback are dropped, not listed
            first = schedule.next_after(last_due)
            truncated = first is not None and first <= start
    due_times = schedule.due_between(start, now)
    if not due_times:
        # Due times only before the lookback: move last_due up so later ticks don't rescan.
        return Decision(Outcome.IDLE, truncated=truncated, last_due=start if truncated else None)
    latest = due_times[-1]
    if len(due_times) > MAX_LISTED:
        truncated = True
    if paused:
        return Decision(
            Outcome.PAUSED,
            skipped=tuple(due_times[-MAX_LISTED:]),
            truncated=truncated,
            last_due=latest,
        )
    older = due_times[:-1]
    age = now - latest
    is_on_time = age < on_time
    catchable = (
        catchup is not None and age <= catchup and downtime is not None and downtime.covers(latest)
    )
    if not (is_on_time or catchable):
        return Decision(
            Outcome.MISSED,
            missed=tuple(due_times[-MAX_LISTED:]),
            truncated=truncated,
            last_due=latest,
        )
    outcome = Outcome.OVERLAP if busy else Outcome.RUN
    return Decision(
        outcome,
        due=latest,
        late=not is_on_time,
        missed=tuple(older[-MAX_LISTED:]),
        truncated=truncated,
        last_due=latest,
    )


def _aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")

"""Registered jobs (PER-298): "every Monday at 9, check flaky CI and file tickets".

A job is cron plus a prompt (``store/jobs.py``, migration 030). Approvers add, list, pause,
resume, delete and run them in plain words (``tools/jobs.py``); each firing starts one Claude
worker run (``ClaudeRunner.start_run``) as the job's creator, and the run's end text goes to
the job's output. It is the instance's own runtime state: it works with ``ops.self_update``
off (Cori).

**Firing: ``core/registered_jobs``** (``scheduler.CORE_JOBS``, every minute in
``settings.timezone``, no model). For each active job, ``cron.decide`` over its ``last_due``
(the latest due time is on time within ``ON_TIME``; nothing is caught up, ever):

- **run**: ``last_due`` is claimed first (compare-and-set, ``store.jobs.claim_due``), then the
  run starts: ``start_run(repo, prompt, principal=created_by, report="now",
  channel_ref=<the job's thread, for output thread>, job=RunLink(id, room))``. So a due time
  starts at most one run (a crash between the claim and the start loses that firing; it
  never doubles it).
- **overlap**: the job's previous run (``last_run_code``) is still active: the due time is
  consumed and skipped (audited ``regjob.overlap``).
- **missed** (the daemon was down or paused past it): consumed, nothing runs.

A paused job is skipped and never caught up (resuming moves ``last_due`` to now). While the
kill switch is engaged the core job doesn't run at all, and the due times it slept through
are missed. A job whose repo is no longer registered pauses itself and says so in its output.
A creator who is no longer a principal leaves the job running as the owner (approvers can
delete it). A start the runner refuses (not set up, paused) is recorded as ``last_error`` and
audited ``regjob.refused``; the next due time tries again.

**Output** (through the hub): ``thread`` (the thread the job was added in: its
``channel_ref``), ``dm`` (the creator's first identity) or ``room:<key>``
(``channels.slack.rooms``, ``ChannelHub.post_room``). ``ClaudeRunner`` sends the run's end
text there (a room post falls back to the creator if the room refuses).

**Memory.** The run's first prompt gets the job's memory (its note, at most 4 KB) and the keys
it has already seen (``context_text``); the worker writes back with ``resonant job-note
'{"seen": [...], "note": "..."}'`` (``WorkerDesk``, ``store.jobs.save_note``).

**Limits.** A schedule that fires more often than every ``MIN_INTERVAL`` (15 minutes) is
refused (``check_interval``); at most ``MAX_JOBS`` live jobs. DST follows ``cron.decide``.

An idle tick (no active jobs) records nothing: personal Resonant with no jobs sees no new
rows or audits.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast
from zoneinfo import ZoneInfo

from resonant.extensions.cron import CronError, Outcome, Schedule, decide, parse_schedule
from resonant.gateway.channel import Channel, ChannelError, thread_of
from resonant.store import jobs as store
from resonant.store import repos as repos_store
from resonant.store import runs as runs_store
from resonant.store.audit import audit
from resonant.store.db import atomic, utcnow

if TYPE_CHECKING:
    from resonant.executor import CallContext
    from resonant.extensions.scheduler import CoreDeps, CoreJob
    from resonant.principals import Principals
    from resonant.runners.claude import Refused, Started

log = logging.getLogger(__name__)

JOB_ID = "registered_jobs"
SCHEDULE = "* * * * *"
MIN_INTERVAL = timedelta(minutes=15)
MAX_JOBS = 25
ON_TIME = timedelta(minutes=3)  # the core job runs every minute; a little slack
INTERVAL_PROBES = 200  # due times looked at by check_interval
RUNNER = "claude"

_DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December",
)  # fmt: skip


class JobRunner(Protocol):
    """What firing uses of ``ClaudeRunner``."""

    async def start_run(
        self,
        repo: str | None,
        ask: str,
        principal: str,
        *,
        merge: bool = False,
        report: Any = "digest",
        channel_ref: dict[str, Any] | None = None,
        job: store.RunLink | None = None,
    ) -> Started | Refused: ...


# --- schedules ----------------------------------------------------------------------------


def parse(expr: str, tz: str) -> Schedule:
    """``cron.parse_schedule`` plus the minimum interval. Raises ``CronError``."""
    schedule = parse_schedule(expr, tz)
    check_interval(schedule)
    return schedule


def check_interval(schedule: Schedule, *, start: datetime | None = None) -> None:
    """Refuse a schedule two of whose due times (among the next ``INTERVAL_PROBES``) are
    closer than ``MIN_INTERVAL``. Every pattern of a five-field cron repeats within those."""
    at = start or datetime(2026, 1, 5, tzinfo=schedule.tz)  # a Monday, outside DST changes
    prev: datetime | None = None
    for i, due in enumerate(schedule.iter_after(at)):
        if i >= INTERVAL_PROBES:
            break
        if prev is not None and due - prev < MIN_INTERVAL:
            gap = int((due - prev).total_seconds() // 60)
            raise CronError(
                f"{schedule.expr!r} fires every {gap} minutes; at most every "
                f"{int(MIN_INTERVAL.total_seconds() // 60)} minutes"
            )
        prev = due


def next_times(schedule: Schedule, after: datetime, n: int = 3) -> list[datetime]:
    out: list[datetime] = []
    for due in schedule.iter_after(after):
        out.append(due)
        if len(out) >= n:
            break
    return out


def fmt_time(dt: datetime, tz: str | ZoneInfo) -> str:
    """``Mon 24 Nov 09:00`` in ``tz``."""
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    local = dt.astimezone(zone)
    return f"{local.strftime('%a')} {local.day} {local.strftime('%b %H:%M')}"


def _num(field: str, lo: int, hi: int) -> int | None:
    return int(field) if field.isdigit() and lo <= int(field) <= hi else None


def _nums(field: str, lo: int, hi: int) -> list[int] | None:
    parts = field.split(",")
    out = [_num(p, lo, hi) for p in parts]
    return None if any(v is None for v in out) else [v for v in out if v is not None]


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _days(dow: str) -> str | None:
    """``every Monday``, ``on weekdays``, ``Monday to Wednesday``; None if unusual."""
    if dow in ("1-5", "MON-FRI", "mon-fri"):
        return "on weekdays"
    if dow in ("0,6", "6,0", "6,7", "SAT,SUN", "sat,sun"):
        return "on weekends"
    if "-" in dow and "," not in dow:
        a, _, b = dow.partition("-")
        lo, hi = _num(a, 0, 7), _num(b, 0, 7)
        if lo is not None and hi is not None and lo < hi:
            return f"{_DAYS[lo % 7]} to {_DAYS[hi % 7]}"
        return None
    days = _nums(dow, 0, 7)
    if days is None:
        return None
    return "every " + _and([_DAYS[d % 7] for d in days])


def _clock(hours: list[int], minute: int) -> str:
    return _and([f"{h:02d}:{minute:02d}" for h in hours])


def describe(expr: str) -> str:
    """A cron expression in words: ``every Monday at 09:00``, ``every day at 08:30``,
    ``every 30 minutes``, ``every 2 hours at :15``. Unusual ones are named as cron."""
    fields = expr.split()
    if len(fields) != 5:
        return f"on the schedule {expr!r}"
    minute, hour, dom, month, dow = fields
    fallback = f"on the schedule {expr!r}"
    if month != "*":
        return fallback
    m = _num(minute, 0, 59)
    when: str | None = None
    if minute.startswith("*/") and hour == "*" and dom == "*" and dow == "*":
        step = _num(minute[2:], 1, 59)
        return f"every {step} minutes" if step else fallback
    if m is None:
        return fallback
    if hour == "*":
        when = f"every hour at :{m:02d}"
    elif hour.startswith("*/"):
        step = _num(hour[2:], 1, 23)
        if step is None:
            return fallback
        when = f"every {step} hours at :{m:02d}"
    else:
        hours = _nums(hour, 0, 23)
        if hours is None:
            return fallback
        when = f"at {_clock(hours, m)}"
    if dom == "*" and dow == "*":
        return f"every day {when}" if when.startswith("at ") else when
    if dom != "*" and dow == "*":
        days = _nums(dom, 1, 31)
        if days is None:
            return fallback
        return f"on day {_and([str(d) for d in days])} of each month {when}"
    if dom == "*":
        days_text = _days(dow)
        return fallback if days_text is None else f"{days_text} {when}"
    return fallback


# --- the prompt ---------------------------------------------------------------------------

NOTE_HOWTO = (
    "Before you finish, save what the next run should know (this replaces the memory) and the "
    "keys of what you handled, so the next run skips them:\n"
    '  resonant job-note \'{"seen": ["<key>", ...], "note": "<memory, at most 4 KB>"}\'\n'
    "Both fields are optional; seen keys are added to the list."
)


def context_text(job: store.Job) -> str:
    """What a job's run is told on its first turn, after the job's prompt."""
    lines = [f"This run is registered job {job.id} ({job.name}), started on its schedule."]
    if job.memory:
        lines += ["Its memory from earlier runs:", job.memory]
    else:
        lines.append("It has no memory from earlier runs yet.")
    if job.seen:
        lines.append("Already seen (skip these): " + ", ".join(job.seen))
    lines.append(NOTE_HOWTO)
    return "\n".join(lines)


def run_context(conn: sqlite3.Connection, run_code: str) -> str | None:
    """``context_text`` for ``run_code``'s job, or None for a run that isn't a job's."""
    link = store.run_link(conn, run_code)
    job = None if link is None else store.get(conn, link.job)
    return None if job is None else context_text(job)


# --- firing -------------------------------------------------------------------------------


def principal_for(job: store.Job, principals: Principals) -> str:
    """The job's creator, or the owner once the creator is no longer a principal."""
    return job.created_by if principals.known(job.created_by) else principals.owner


def output_text(job: store.Job) -> str:
    if job.output == "dm":
        return "a DM"
    if job.room is not None:
        return f"#{job.room}"
    return "this thread"


async def post(
    channel: Channel | None, job: store.Job, principal: str, text: str, *, key: str
) -> bool:
    """Say ``text`` in the job's output; False when nobody took it."""
    if channel is None:
        return False
    try:
        if job.room is not None:
            poster: Any = getattr(channel, "post_room", None)
            if poster is not None:
                try:
                    await poster(job.room, text, dedupe_key=key)
                    return True
                except ChannelError:
                    log.warning("job room refused", extra={"job": job.id})
        thread = thread_of(job.channel_ref, to=principal) if job.output == "thread" else None
        await channel.send(principal, text, thread=thread, dedupe_key=key)
        return True
    except ChannelError as e:
        log.warning("job text not sent (%s)", type(e).__name__, extra={"job": job.id})
        return False


def busy(conn: sqlite3.Connection, job: store.Job) -> bool:
    """The job's previous run is still queued, running or waiting."""
    if job.last_run_code is None:
        return False
    run = runs_store.get(conn, job.last_run_code)
    return run is not None and run.active


async def start(
    conn: sqlite3.Connection,
    job: store.Job,
    *,
    runner: JobRunner | None,
    principals: Principals,
    channel: Channel | None,
    now: datetime,
    manual: bool = False,
) -> tuple[bool, str]:
    """Start one run of ``job`` (its due time already claimed, or ``run_job_now``). Returns
    ``(started, sentence)``."""
    principal = principal_for(job, principals)
    if job.repo is not None and repos_store.find(conn, job.repo) is None:
        why = f"its repo {job.repo} is gone"
        store.set_state(conn, job.id, "paused", now, expect=("active",), error=why)
        text = f"{job.id} ({job.name}) paused itself: {why}. Resume it once the repo is back."
        with atomic(conn):
            audit(conn, "regjob.paused", requested_by=principal, job=job.id, reason="repo_gone")
        await post(channel, job, principal, text, key=f"regjob:{job.id}:repo_gone:{now.date()}")
        return False, text
    if runner is None:
        why = "the Claude runner isn't running"
        store.refused(conn, job.id, why, now)
        with atomic(conn):
            audit(conn, "regjob.refused", requested_by=principal, job=job.id, reason=why)
        return False, f"{job.id} didn't start: {why}."
    from resonant.runners.claude import Refused

    ref = job.channel_ref if job.output == "thread" else None
    if ref is not None and ref.get("principal") != principal:
        ref = None  # a thread is only ever its asker's
    result = await runner.start_run(
        job.repo,
        job.prompt,
        principal,
        report="now",
        channel_ref=ref,
        job=store.RunLink(job.id, job.room),
    )
    if isinstance(result, Refused):
        store.refused(conn, job.id, result.reason, now)
        with atomic(conn):
            audit(conn, "regjob.refused", requested_by=principal, job=job.id,
                  reason=result.reason[:120])  # fmt: skip
        return False, f"{job.id} didn't start: {result.reason}"
    store.started(conn, job.id, result.code, now)
    with atomic(conn):
        audit(conn, "regjob.started", requested_by=principal, job=job.id, run=result.code,
              manual=manual)  # fmt: skip
    return True, f"{job.id} ({job.name}) started {result.code}."


async def tick(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    runner: JobRunner | None,
    principals: Principals,
    channel: Channel | None,
) -> dict[str, Any]:
    """One pass over the active jobs (see the module docstring)."""
    jobs = store.active(conn)
    if not jobs:
        return {"quiet": True}
    counts = {"started": 0, "overlap": 0, "missed": 0, "refused": 0}
    for job in jobs:
        try:
            schedule = parse_schedule(job.schedule, job.tz)
        except CronError as e:
            store.set_state(conn, job.id, "paused", now, expect=("active",), error=str(e))
            continue
        d = decide(
            schedule,
            now=now,
            last_due=job.last_due,
            paused=False,
            catchup=None,
            downtime=None,
            busy=busy(conn, job),
            on_time=ON_TIME,
        )
        if d.last_due is None or not store.claim_due(conn, job, d.last_due, now):
            continue
        if d.outcome is Outcome.OVERLAP:
            counts["overlap"] += 1
            with atomic(conn):
                audit(conn, "regjob.overlap", requested_by=job.created_by, job=job.id,
                      run=job.last_run_code)  # fmt: skip
        elif d.outcome is Outcome.MISSED:
            counts["missed"] += 1
        elif d.outcome is Outcome.RUN:
            try:
                ok, _ = await start(conn, job, runner=runner, principals=principals,
                                    channel=channel, now=now)  # fmt: skip
            except Exception:
                log.exception("registered job failed to start", extra={"job": job.id})
                store.refused(conn, job.id, "couldn't start the run", now)
                ok = False
            counts["started" if ok else "refused"] += 1
    if not any(counts.values()):
        return {"quiet": True}
    return counts


def registered_jobs_job(
    deps: CoreDeps, *, clock: Callable[[], datetime] = utcnow
) -> CoreJob | None:
    """The ``CORE_JOBS`` factory. None with no channel or no owner (nothing could run)."""
    from resonant.extensions.scheduler import CoreJob

    if deps.channel is None or deps.owner is None:
        return None
    conn, components, channel = deps.conn, deps.components, deps.channel

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        del args, ctx
        runner = cast("JobRunner | None", components.runners.get(RUNNER))
        return await tick(conn, clock(), runner=runner, principals=components.principals,
                          channel=channel)  # fmt: skip

    return CoreJob(id=JOB_ID, schedule=SCHEDULE, run=run, timeout_s=120.0, quiet_idle=True)


__all__ = [
    "JOB_ID",
    "MAX_JOBS",
    "MIN_INTERVAL",
    "JobRunner",
    "check_interval",
    "context_text",
    "describe",
    "fmt_time",
    "next_times",
    "output_text",
    "parse",
    "post",
    "registered_jobs_job",
    "run_context",
    "start",
    "tick",
]

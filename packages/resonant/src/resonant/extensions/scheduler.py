"""The scheduler: runs extension and core jobs on their cron schedules (D6).

``Scheduler.tick(now)`` is driven by the loop (``Loop.scheduler``) on every tick, and
``next_due`` shortens the loop's sleep, so a job runs within ``tick_s`` of its due time.
Each job's tick calls ``cron.decide`` over its ``ext_jobs`` state, then:

- **Pause wins.** Paused (kv ``paused``; unknown counts as paused), every due time is
  skipped (``job.skipped_paused``) and ``last_due`` moves past it: no catch-up on resume.
  The one exception is a core job with ``runs_while_paused`` (``core/backup``: read-only,
  nothing sent or spawned), which runs as if unpaused.
- **Catch-up.** Only the latest due time can run: on time (within ``tick_s`` plus a
  minute), or within the job's ``catchup`` while the daemon was down (``Downtime`` from
  the previous run's last tick, kv ``scheduler.last_tick``, to this start). Every other one
  is audited ``job.missed``.
- **Overlap.** The previous run still running (inline) or its task still open: skipped,
  audited ``job.overlap``.

Execution by effective level (origin ``scheduled``, principal the owner):

- **L0** (and core jobs): no task row. It runs inline as a background coroutine, at most
  ``extensions.max_concurrent_jobs`` at once. An extension job is gated through the tool
  core (``ToolCore.call`` with ``task_id="job:<ext>/<id>:<due>"``; the gate audits it) over
  an ``InlineExecutor``, which only spends the token: there is no executor row. Its
  secrets come from the ``SecretStore`` here, and its result and events are redacted. When
  it ends, ``mark_due``, ``record_run``, its events and the ``job.run`` (or ``job.failed``,
  ``job.killed``) audit row are written in one transaction. A run cut short by shutdown
  writes nothing, so after a restart its due time is decided again (caught up only within
  ``catchup``). While it runs, its due time is claimed in memory, so a second tick doesn't
  run it twice.
- **L3 / L2 / L1**: ``insert_task(kind="job", runner="job", wake_at=due)`` and ``mark_due``
  with ``open_task_id`` in one transaction, so the due time is claimed exactly once.
  ``runners.job.JobRunner`` runs it (gate, approvals, executor, reports).

Kill switch. ``start()`` registers a ``killswitch`` pause hook: ``/kill`` holds the
``ProcessTracker`` (new runs are refused) and kills every tracked extension process at once;
an inline run killed that way is audited ``job.killed``. A CLI ``resonant kill`` is seen on
the next tick. ``/resume`` (or the next unpaused tick) lets runs start again.

Events. A run's events become unrouted ``Event(source="ext:<name>",
type="ext.<name>.<type>", dedupe_key="ext:<name>:<key>", payload={text, data})`` rows
(``persist_events``); the UNIQUE dedupe key drops repeats. A job's first successful run
after install stores them as ``payload.baseline=true`` (``notify.take_baseline``). Delivery
is ``extensions.notify.Notifier``'s, through the loop's router hook.

Core jobs (``ext="core"``). ``CORE_JOBS`` is a tuple of factories, like ``BUILTIN_MODULES``:
each gets ``CoreDeps`` at daemon start and returns a ``CoreJob`` (or None when disabled).
A core job is trusted core code: L0 only, run inline without the gate, with no secrets.

Panels (PER-280). An extension's dashboard panels (``Manifest.panels``) are L0 reads run
inline like an L0 job, through the same ``_call`` (gate audit, secrets, redaction, timeout),
but with their own state (``ext_panels``) and schedule: the ``idle`` cron, or the ``active``
one while local time is inside its hours, days and months (``cron.in_window``). Each runs once
at start-up too (``PANEL_FIRST_RUN_S`` after the daemon starts, staggered), and never within
55 s of its own previous start. A run's output goes through ``extensions.panels.normalize``:
a good copy is stored (``panel.run``), a refusal or a failure keeps the last good copy and
grows the streak (``panel.failed``); ``panel.denied``, ``panel.killed``, ``panel.dry_run`` and
``panel.skipped_paused`` only audit. Paused, a panel simply doesn't run: its missed due times
are not audited.

Panel history (PER-286). A panel with ``history`` also archives one copy per calendar day
(``extensions/panel_history.py`` has the rules: which day, backoff, prune). At most one archive
run is in flight at a time, and one starts only while no live panel is running, so a backfill
never takes a live panel's place. It goes through the same ``_call`` with ``date`` added (the
handler appends ``--date <day>``) and the same ``normalize``; ``{"schema": 1, "none": true}``
records the day as none. Audited ``panel.archived`` (``status`` ok or none),
``panel.archive_failed`` and ``panel.archive_deferred`` (killed or paused: not an attempt,
and the archiver holds for 15 minutes).

A job whose schedule doesn't parse is skipped at build; a job that raises during a tick is
logged and skipped for that tick. Neither stops the others.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from resonant import killswitch
from resonant.executor import CallContext, ToolFailed
from resonant.extensions import panel_history as history
from resonant.extensions.briefing import briefing_job
from resonant.extensions.cron import (
    ON_TIME,
    CronError,
    Decision,
    Downtime,
    Outcome,
    Schedule,
    decide,
    in_window,
    parse_schedule,
)
from resonant.extensions.notify import take_baseline
from resonant.extensions.panels import Normalized, normalize
from resonant.extensions.process import ExtAmbiguous, ExtFailed, ProcessTracker
from resonant.extensions.pulse_job import pulse_job
from resonant.gate import Gate
from resonant.gate import actions as action_ids
from resonant.gate.policy import APPROVAL_USED
from resonant.gate.types import Deny
from resonant.loop.types import PRIORITY_BACKGROUND, NewTask
from resonant.redact import Redactor
from resonant.secrets import SecretError, SecretStore, values_of
from resonant.store import approvals
from resonant.store import extensions as store
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set, transaction, utcnow
from resonant.store.tasks import insert_event, insert_task
from resonant.toolcore import Asked, CallScope, InlineExecutor, Refused, ToolCore
from resonant.tools import ToolRegistry
from resonant_sdk import Effect, Event, Level, ToolSpec, new_id
from resonant_sdk.panel_schema import is_none

if TYPE_CHECKING:
    from resonant.components import Components
    from resonant.config import Settings
    from resonant.gateway.channel import Channel

log = logging.getLogger(__name__)

LAST_TICK_KEY = "scheduler.last_tick"
JOB_KIND = "job"
JOB_RUNNER = "job"
CORE_EXT = store.CORE_EXT
ACTIVE = frozenset({"queued", "running", "waiting"})
ERROR_MAX = 200

# A job's call: an ExtensionHandler, or a core job's coroutine. Same shape.
JobCall = Callable[[dict[str, Any], CallContext], Awaitable[dict[str, Any]]]


def job_spec_name(job_id: str) -> str:
    return "job_" + job_id.replace("-", "_")


@dataclass(frozen=True)
class CoreJob:
    """A built-in job. ``schedule`` is cron in ``settings.timezone``."""

    id: str
    schedule: str
    run: JobCall
    catchup: timedelta | None = None
    timeout_s: float = 300.0
    # Runs while the kill switch is engaged. Only for a job that reads the store and writes
    # nothing outside Resonant's home, sends nothing and spawns nothing (core/backup).
    runs_while_paused: bool = False


@dataclass(frozen=True)
class CoreDeps:
    """What a core job factory gets at daemon start. ``components.voice`` (PER-273) is how the
    pulse and the briefing get Claude to write their prose (None: code-built)."""

    settings: Settings
    conn: sqlite3.Connection
    components: Components
    channel: Channel | None  # None when iMessage is off
    owner: str | None  # None when principals.yaml is missing


CoreJobFactory = Callable[[CoreDeps], CoreJob | None]


def backup_job(deps: CoreDeps) -> CoreJob | None:
    """``core/backup`` (BK-1, ``resonant.backup``), imported late like ``watches_job``."""
    from resonant.backup import backup_job as factory

    return factory(deps)


def store_upkeep_job(deps: CoreDeps) -> CoreJob | None:
    """``core/prune`` (PER-259, ``resonant.store_upkeep``), imported late like ``backup_job``."""
    from resonant.store_upkeep import store_upkeep_job as factory

    return factory(deps)


def remind_job(deps: CoreDeps) -> CoreJob | None:
    """``core/remind`` (PER-261, ``runners/approve.py``: the expiry reminder), imported late
    like ``watches_job``."""
    from resonant.runners.approve import remind_job as factory

    return factory(deps)


def mcp_drift_job(deps: CoreDeps) -> CoreJob | None:
    """``core/mcp`` (PER-275, ``extensions/mcp_drift.py``: MCP re-pin questions and heads-ups),
    imported late like ``remind_job``."""
    from resonant.extensions.mcp_drift import mcp_drift_job as factory

    return factory(deps)


def watches_job(deps: CoreDeps) -> CoreJob | None:
    """``core/watches`` (BR-7, ``runners/watch.py``), imported late: the runners import the
    gate and the router, which this module must not pull in at import time."""
    from resonant.runners.watch import watches_job as factory

    return factory(deps)


# Built-in jobs, one factory each.
CORE_JOBS: tuple[CoreJobFactory, ...] = (
    briefing_job,
    pulse_job,
    watches_job,
    backup_job,
    store_upkeep_job,
    remind_job,
    mcp_drift_job,
)


@dataclass(frozen=True)
class ScheduledJob:
    ext: str
    job_id: str
    spec: ToolSpec
    schedule: Schedule
    call: JobCall
    catchup: timedelta | None = None
    core: bool = False
    runs_while_paused: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.ext, self.job_id)

    @property
    def label(self) -> str:
        return f"{self.ext}/{self.job_id}"

    @property
    def inline(self) -> bool:
        return self.core or self.spec.effective_level is Level.L0


PANEL_GUARD_S = 55.0  # a panel never starts twice within this many seconds
PANEL_BOOT_STAGGER_S = 3.0
PANEL_CONCURRENCY = 2  # panel runs at once, apart from the jobs' ``max_concurrent``
PANEL_SLACK_S = 120.0  # past its next due + timeout, a panel is stale


@dataclass(frozen=True)
class ScheduledPanel:
    ext: str
    panel_id: str
    spec: ToolSpec
    idle: Schedule
    active_expr: str | None
    active: Schedule | None
    tz: ZoneInfo
    call: JobCall
    max_bytes: int
    tools: frozenset[str]  # this extension's tool names: a link cell may name only these
    keep_days: int | None = None  # PER-286: ``history.keep`` in days; None: no history

    @property
    def key(self) -> tuple[str, str]:
        return (self.ext, f"panel:{self.panel_id}")

    @property
    def label(self) -> str:
        return f"{self.ext}/panel:{self.panel_id}"

    def schedule_at(self, now: datetime) -> Schedule:
        """The governing schedule: ``active`` inside its window, else ``idle``."""
        if (
            self.active is not None
            and self.active_expr
            and in_window(self.active_expr, self.tz, now)
        ):
            return self.active
        return self.idle

    def next_run_after(self, after: datetime) -> datetime | None:
        """The next time a run would really start: the earliest due time of either cron at
        which that cron is the governing one."""
        at = after
        for _ in range(500):
            cands = [
                (due, s)
                for s in (self.idle, self.active)
                if s is not None and (due := s.next_after(at)) is not None
            ]
            if not cands:
                return None
            cands.sort(key=lambda c: c[0])
            for due, which in cands:
                if due == cands[0][0] and self.schedule_at(due) is which:
                    return due
            at = cands[0][0]
        return None


@dataclass(frozen=True)
class _Result:
    outcome: str  # ok | failed | killed | denied | dry_run | skipped_paused
    data: dict[str, Any] | None = None
    events: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    stderr_tail: str | None = None
    duration_ms: int = 0


def persist_events(
    conn: sqlite3.Connection,
    ext: str,
    events: Sequence[Mapping[str, Any]],
    *,
    trace_id: str | None = None,
    baseline: bool = False,
) -> int:
    """Insert a run's (redacted) events as unrouted ``ext:<name>`` events; returns how many
    were new. Call inside the transaction that records the run. Malformed items are
    skipped (the process runner already validated them). ``baseline`` (the job's first
    run after install, ``notify.take_baseline``) marks them ``payload.baseline=true``: stored,
    never notified."""
    n = 0
    for item in events:
        etype, text, key = item.get("type"), item.get("text"), item.get("dedupe_key")
        if not (isinstance(etype, str) and isinstance(text, str) and isinstance(key, str)):
            continue
        data: object = item.get("data")
        payload: dict[str, Any] = {"text": text, "data": data if isinstance(data, dict) else None}
        if baseline:
            payload["baseline"] = True
        event = Event(
            source=f"ext:{ext}",
            type=f"ext.{ext}.{etype}",
            payload=payload,
            principal=None,
            task_id=None,
            dedupe_key=f"ext:{ext}:{key}",
            trace_id=trace_id or new_id(),
        )
        n += insert_event(conn, event)
        if (target := _closed_pr(ext, etype, payload["data"])) is not None:
            # PER-269: a PR merged or closed (by any path): its open asks are moot.
            approvals.obsolete_for_target(conn, target, utcnow())
    return n


# The github watch's events for a PR that is no longer open (``extensions/github``).
_PR_CLOSED_EVENTS = frozenset({"pr_merged", "pr_closed"})


def _closed_pr(ext: str, etype: str, data: object) -> str | None:
    """``jhe/dori#2`` for the github watch's ``pr_merged``/``pr_closed``, else None."""
    if ext != action_ids.GITHUB or etype not in _PR_CLOSED_EVENTS or not isinstance(data, dict):
        return None
    fields = cast(dict[str, object], data)
    return action_ids.pr_target(fields.get("repo"), fields.get("number"))


def read_paused(conn: sqlite3.Connection) -> bool:
    """The kill switch's ``paused`` flag; unknown counts as paused (fail closed)."""
    try:
        return killswitch.is_paused(conn)
    except killswitch.PausedStateUnknownError:
        return True


def _clip(text: str) -> str:
    return text if len(text) <= ERROR_MAX else text[: ERROR_MAX - 3] + "..."


class Scheduler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        jobs: Sequence[ScheduledJob],
        *,
        gate: Gate,
        registry: ToolRegistry,
        owner: str,
        secrets: SecretStore | None = None,
        tracker: ProcessTracker | None = None,
        max_concurrent: int = 4,
        tick_s: float = 30.0,
        started_at: datetime | None = None,
        clock: Callable[[], datetime] = utcnow,
        panels: Sequence[ScheduledPanel] = (),
        panel_first_run_s: float | None = None,
    ) -> None:
        """``started_at`` (the daemon's start) with kv ``scheduler.last_tick`` gives the
        downtime that ``catchup`` may cover. An extension job's spec is the ``registry``'s.
        ``panels`` run once ``panel_first_run_s`` after ``started_at`` (None: not at start-up),
        one every ``PANEL_BOOT_STAGGER_S``, then on their own schedules."""
        self.conn = conn
        self.jobs = tuple(jobs)
        self.panels = tuple(panels)
        self.core = ToolCore(conn, registry=registry, gate=gate, executor=InlineExecutor(gate))
        self.owner = owner
        self.secrets = secrets
        self.tracker = tracker
        self.clock = clock
        self.on_time = ON_TIME + timedelta(seconds=tick_s)
        self.downtime = self._downtime(started_at or clock())
        self._sem = asyncio.Semaphore(max_concurrent)
        # Panels have their own slots: a few hung panels (timeout up to 120 s) must not starve jobs.
        self._panel_sem = asyncio.Semaphore(PANEL_CONCURRENCY)
        self._claimed: dict[tuple[str, str], datetime] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._paused: bool | None = None
        self._unhook: Callable[[], None] | None = None
        begin = started_at or clock()
        self._panel_boot: dict[tuple[str, str], datetime] = (
            {}
            if panel_first_run_s is None
            else {
                p.key: begin + timedelta(seconds=panel_first_run_s + PANEL_BOOT_STAGGER_S * i)
                for i, p in enumerate(self.panels)
            }
        )
        self._panel_due: dict[tuple[str, str], datetime] = {}
        self._panel_started: dict[tuple[str, str], datetime] = {}
        self._archive: asyncio.Task[None] | None = None  # PER-286: one archive run at a time
        self._pruned: dict[tuple[str, str], datetime] = {}
        self._history_hold: datetime | None = None  # after a deferral (dry run, kill): wait

    @classmethod
    def from_components(
        cls,
        settings: Settings,
        conn: sqlite3.Connection,
        components: Components,
        *,
        started_at: datetime,
        channel: Channel | None = None,
        core_jobs: Sequence[CoreJobFactory] = CORE_JOBS,
        clock: Callable[[], datetime] = utcnow,
        panel_first_run_s: float | None = None,
    ) -> Scheduler:
        """Every loaded extension's jobs, then the core jobs, over the components' gate and
        the executor's ``SecretStore``."""
        host = components.extensions
        owner = components.principals.owner
        jobs: list[ScheduledJob] = []
        for hj in host.scheduled_jobs() if host is not None else []:
            catchup = timedelta(seconds=hj.job.catchup_s) if hj.job.catchup_s else None
            schedule = _schedule(conn, hj.ext, hj.job.id, hj.job.schedule, hj.job.tz, settings)
            if schedule is not None:
                jobs.append(ScheduledJob(hj.ext, hj.job.id, hj.spec, schedule, hj.handler, catchup))
        panels: list[ScheduledPanel] = []
        for hp in host.scheduled_panels() if host is not None else []:
            zone = hp.panel.tz or settings.timezone
            pid = f"panel:{hp.panel.id}"
            idle = _schedule(conn, hp.ext, pid, hp.panel.refresh.idle, zone, settings)
            active_expr = hp.panel.refresh.active
            active = (
                None
                if active_expr is None
                else _schedule(conn, hp.ext, pid, active_expr, zone, settings)
            )
            if idle is not None:
                panels.append(
                    ScheduledPanel(
                        hp.ext,
                        hp.panel.id,
                        hp.spec,
                        idle,
                        active_expr if active is not None else None,
                        active,
                        idle.tz,
                        hp.handler,
                        hp.panel.max_bytes,
                        frozenset(s.name for s in host.loaded[hp.ext].tools)
                        if host
                        else frozenset(),
                        hp.panel.history.keep_days if hp.panel.history is not None else None,
                    )
                )
        deps = CoreDeps(
            settings,
            conn,
            components,
            channel,
            owner if components.principals_loaded else None,
        )
        for factory in core_jobs:
            core = factory(deps)
            if core is None:
                continue
            schedule = _schedule(conn, CORE_EXT, core.id, core.schedule, None, settings)
            if schedule is not None:
                spec = ToolSpec(
                    name=job_spec_name(core.id),
                    description="core job",
                    effect=Effect.READ,
                    source=CORE_EXT,
                    extension=CORE_EXT,
                    timeout_s=core.timeout_s,
                )
                jobs.append(
                    ScheduledJob(
                        CORE_EXT,
                        core.id,
                        spec,
                        schedule,
                        core.run,
                        core.catchup,
                        True,
                        runs_while_paused=core.runs_while_paused,
                    )
                )
        return cls(
            conn,
            jobs,
            gate=components.gate,
            registry=components.registry,
            owner=owner,
            secrets=components.executor.secrets,
            tracker=None if host is None else host.tracker,
            max_concurrent=settings.extensions.max_concurrent_jobs,
            tick_s=settings.loop.tick_s,
            started_at=started_at,
            clock=clock,
            panels=panels,
            panel_first_run_s=panel_first_run_s,
        )

    # --- lifecycle ------------------------------------------------------------------------

    def start(self) -> None:
        """Register the kill switch hook (``/kill`` kills tracked processes at once)."""
        if self._unhook is None:
            self._unhook = killswitch.add_pause_hook(self.on_pause)

    async def aclose(self) -> None:
        """Remove the hook and cancel inline runs (their due times are decided again on the
        next start)."""
        if self._unhook is not None:
            self._unhook()
            self._unhook = None
        tasks = [*self._inflight.values(), *self._background]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self) -> None:
        """Test helper: wait for inline runs and kills in flight."""
        while pending := [*self._inflight.values(), *self._background]:
            await asyncio.gather(*pending, return_exceptions=True)

    @property
    def running(self) -> int:
        return len(self._inflight)

    def on_pause(self, conn: sqlite3.Connection, paused: bool) -> None:
        """The kill switch hook, called right after ``engage``/``release`` commits."""
        if conn is not self.conn:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop here: the next tick applies it
        self._apply_pause(paused)

    def _apply_pause(self, paused: bool) -> None:
        if paused == self._paused:
            return
        self._paused = paused
        if self.tracker is None:
            return
        if paused:
            self.tracker.paused = True  # refuse new runs from now on
            self._spawn(self._kill_all())
        else:
            self.tracker.resume()

    async def _kill_all(self) -> None:
        assert self.tracker is not None
        if not self._paused:
            return  # resumed before this ran; the hold was already set and cleared
        # The hold was set synchronously in _apply_pause; don't set it again here, or a
        # /resume handled before this task ran would be undone.
        n = await self.tracker.kill_all("killed")
        if n:
            with atomic(self.conn):
                audit(self.conn, "job.kill_all", count=n)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _downtime(self, started_at: datetime) -> Downtime | None:
        value: object = kv_get(self.conn, LAST_TICK_KEY)
        if not isinstance(value, str):
            return None
        try:
            last = datetime.fromisoformat(value)
        except ValueError:
            return None
        if last.utcoffset() is None or last >= started_at:
            return None
        return Downtime(since=last, until=started_at)

    # --- ticking --------------------------------------------------------------------------

    def next_due(self, after: datetime) -> datetime | None:
        """The earliest due time of any job strictly after ``after`` (``Loop.wait_s``)."""
        times: list[datetime] = []
        for job in self.jobs:
            try:
                if (t := job.schedule.next_after(after)) is not None:
                    times.append(t)
            except ValueError:
                log.exception("next due failed", extra={"job": job.label})
        for panel in self.panels:
            try:
                times += [t for t in self._panel_boot.values() if t > after]
                times += [
                    t
                    for s in (panel.idle, panel.active)
                    if s is not None and (t := s.next_after(after)) is not None
                ]
            except ValueError:
                log.exception("next due failed", extra={"job": panel.label})
        return min(times, default=None)

    async def tick(self, now: datetime) -> None:
        """One scheduler pass over every job (see the module docstring)."""
        paused = read_paused(self.conn)
        self._apply_pause(paused)
        for job in self.jobs:
            try:
                self._tick_job(job, now, paused)
            except Exception:
                log.exception("job tick failed", extra={"job": job.label})
        for panel in self.panels:
            try:
                self._tick_panel(panel, now, paused)
            except Exception:
                log.exception("panel tick failed", extra={"job": panel.label})
        try:
            self._tick_history(now, paused)
        except Exception:
            log.exception("panel history tick failed")
        with atomic(self.conn):
            kv_set(self.conn, LAST_TICK_KEY, iso(now))

    def _tick_job(self, job: ScheduledJob, now: datetime, paused: bool) -> None:
        state = store.get_job(self.conn, job.ext, job.job_id)
        last_due = None if state is None else state.last_due
        claimed = self._claimed.get(job.key)
        if claimed is not None and (last_due is None or claimed > last_due):
            last_due = claimed
        busy = job.key in self._inflight or self._task_open(state)
        d = decide(
            job.schedule,
            now=now,
            last_due=last_due,
            paused=paused and not job.runs_while_paused,
            catchup=job.catchup,
            downtime=self.downtime,
            busy=busy,
            on_time=self.on_time,
        )
        if d.outcome is Outcome.RUN:
            assert d.due is not None
            if job.inline:
                self._start_inline(job, d)
            else:
                self._queue_task(job, d)
            return
        if d.last_due is None:
            return
        with transaction(self.conn):
            store.mark_due(self.conn, job.ext, job.job_id, d.last_due)
            self._audit_missed(job, d)
            for due in d.skipped:
                self._audit(job, "job.skipped_paused", due)
            if d.outcome is Outcome.OVERLAP and d.due is not None:
                self._audit(job, "job.overlap", d.due)

    def _task_open(self, state: store.JobState | None) -> bool:
        if state is None or state.open_task_id is None:
            return False
        row = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (state.open_task_id,)
        ).fetchone()
        return row is not None and row["status"] in ACTIVE

    def _audit(self, job: ScheduledJob, action: str, due: datetime, **detail: Any) -> None:
        audit(
            self.conn,
            action,
            extension=job.ext,
            tool=job.spec.name,
            effect=job.spec.effect.value,
            level=job.spec.effective_level.value,
            job=job.label,
            due=iso(due),
            **detail,
        )

    def _audit_missed(self, job: ScheduledJob, d: Decision) -> None:
        for due in d.missed:
            self._audit(job, "job.missed", due, truncated=d.truncated)

    # --- panels (PER-280) -----------------------------------------------------------------

    def _tick_panel(self, p: ScheduledPanel, now: datetime, paused: bool) -> None:
        key = p.key
        if paused or key in self._inflight:
            return  # silently: a panel's missed due times are never audited
        started = self._panel_started.get(key)
        if started is not None and (now - started).total_seconds() < PANEL_GUARD_S:
            return
        boot = self._panel_boot.get(key)
        if boot is not None and now >= boot:
            del self._panel_boot[key]
            self._start_panel(p, now, now)
            return
        d = decide(
            p.schedule_at(now),
            now=now,
            last_due=self._panel_due.get(key),
            paused=False,
            catchup=None,
            downtime=None,
            busy=False,
            on_time=self.on_time,
        )
        if d.last_due is not None:
            self._panel_due[key] = d.last_due
        if d.outcome is Outcome.RUN:
            assert d.due is not None
            self._start_panel(p, d.due, now)

    def _start_panel(self, p: ScheduledPanel, due: datetime, now: datetime) -> None:
        self._panel_started[p.key] = now
        task = asyncio.get_running_loop().create_task(
            self._run_panel(p, due), name=f"panel:{p.label}"
        )
        self._inflight[p.key] = task
        task.add_done_callback(lambda t, key=p.key: self._inline_done(key, t))

    async def _run_panel(self, p: ScheduledPanel, due: datetime) -> None:
        # The throwaway job is only the shape ``_call`` takes: gate, secrets, redaction, timeout.
        job = ScheduledJob(p.ext, f"panel:{p.panel_id}", p.spec, p.idle, p.call)
        async with self._panel_sem:
            if read_paused(self.conn):
                result = _Result("skipped_paused")
            else:
                result = await self._call(job, due)
            self._finish_panel(p, due, result)

    def _audit_panel(self, p: ScheduledPanel, action: str, due: datetime, **detail: Any) -> None:
        audit(
            self.conn,
            action,
            extension=p.ext,
            tool=p.spec.name,
            effect=p.spec.effect.value,
            level=p.spec.effective_level.value,
            panel=f"{p.ext}/{p.panel_id}",
            due=iso(due),
            **detail,
        )

    def _expect_by(self, p: ScheduledPanel, now: datetime) -> datetime:
        nxt = p.next_run_after(now) or now + timedelta(hours=25)
        return nxt + timedelta(seconds=p.spec.timeout_s + PANEL_SLACK_S)

    def _finish_panel(self, p: ScheduledPanel, due: datetime, r: _Result) -> None:
        now = self.clock()
        with transaction(self.conn):
            match r.outcome:
                case "ok":
                    got = normalize(r.data, max_bytes=p.max_bytes, tools=p.tools, ran_at=now)
                    if isinstance(got, Normalized):
                        store.save_panel_ok(
                            self.conn,
                            p.ext,
                            p.panel_id,
                            data=json.dumps(got.data, separators=(",", ":")),
                            updated_at=got.updated_at,
                            ran_at=now,
                            bytes=got.bytes,
                            expect_by=self._expect_by(p, now),
                        )
                        self._audit_panel(
                            p, "panel.run", due, ok=True, ms=r.duration_ms, bytes=got.bytes,
                            truncated=got.truncated,
                        )  # fmt: skip
                    else:
                        store.record_panel_failure(
                            self.conn, p.ext, p.panel_id, ran_at=now, error=got.error
                        )
                        self._audit_panel(p, "panel.failed", due, error=got.error)
                case "failed" | "denied":
                    error = _clip(r.error or r.outcome)
                    store.record_panel_failure(
                        self.conn, p.ext, p.panel_id, ran_at=now, error=error
                    )
                    detail = {"stderr_tail": r.stderr_tail} if r.stderr_tail else {}
                    action = "panel.failed" if r.outcome == "failed" else "panel.denied"
                    self._audit_panel(p, action, due, error=error, **detail)
                case "killed":
                    self._audit_panel(p, "panel.killed", due, error=r.error)
                case "dry_run":
                    self._audit_panel(p, "panel.dry_run", due)
                case _:
                    self._audit_panel(p, "panel.skipped_paused", due)

    # --- panel history (PER-286) ----------------------------------------------------------

    @property
    def archiving(self) -> bool:
        return self._archive is not None and not self._archive.done()

    def _tick_history(self, now: datetime, paused: bool) -> None:
        kept = [p for p in self.panels if p.keep_days is not None]
        if not kept:
            return
        for p in kept:
            self._prune_history(p, now)
        if paused or self.archiving or any(p.key in self._inflight for p in self.panels):
            return  # one at a time, and live panels first
        if self._history_hold is not None and now < self._history_hold:
            return  # a run was just deferred (dry run, kill): don't walk every day meanwhile
        best: tuple[str, ScheduledPanel] | None = None
        for p in kept:
            assert p.keep_days is not None
            since = history.keep_from(now, p.tz, p.keep_days).isoformat()
            rows = {
                d.day: history.DayState(d.status, d.next_try_at)
                for d in store.list_panel_days(self.conn, p.ext, p.panel_id, since=since)
            }
            day = history.next_day(rows, now, p.tz, p.keep_days)
            if day is not None and (best is None or day.isoformat() > best[0]):
                best = (day.isoformat(), p)
        if best is not None:
            day, p = best
            self._archive = asyncio.get_running_loop().create_task(
                self._run_archive(p, day), name=f"panel-day:{p.label}:{day}"
            )
            self._background.add(self._archive)
            self._archive.add_done_callback(self._background.discard)

    def _prune_history(self, p: ScheduledPanel, now: datetime) -> None:
        assert p.keep_days is not None
        last = self._pruned.get(p.key)
        if last is not None and now - last < history.PRUNE_EVERY:
            return
        self._pruned[p.key] = now
        before = history.keep_from(now, p.tz, p.keep_days).isoformat()
        with atomic(self.conn):
            if n := store.prune_days_before(self.conn, p.ext, p.panel_id, before):
                audit(self.conn, "panel.history_pruned", extension=p.ext,
                      panel=f"{p.ext}/{p.panel_id}", before=before, days=n)  # fmt: skip

    async def _run_archive(self, p: ScheduledPanel, day: str) -> None:
        due = self.clock()
        # The throwaway job is the shape ``_call`` takes; its label names the day.
        job = ScheduledJob(p.ext, f"panel:{p.panel_id}:{day}", p.spec, p.idle, p.call)
        if read_paused(self.conn):
            result = _Result("skipped_paused")
        else:
            result = await self._call(job, due, extra={"date": day})
        self._finish_archive(p, day, due, result)

    def _finish_archive(self, p: ScheduledPanel, day: str, due: datetime, r: _Result) -> None:
        now = self.clock()
        error: str | None = None
        with transaction(self.conn):
            if r.outcome == "ok":
                if is_none(r.data):
                    store.save_day_none(self.conn, p.ext, p.panel_id, day, at=now)
                    self._audit_panel(p, "panel.archived", due, day=day, status="none",
                                      ms=r.duration_ms)  # fmt: skip
                    return
                got = normalize(r.data, max_bytes=p.max_bytes, tools=p.tools, ran_at=now)
                if isinstance(got, Normalized):
                    store.save_day_ok(
                        self.conn,
                        p.ext,
                        p.panel_id,
                        day,
                        data=json.dumps(got.data, separators=(",", ":")),
                        bytes=got.bytes,
                        at=now,
                    )
                    self._audit_panel(p, "panel.archived", due, day=day, status="ok",
                                      ms=r.duration_ms, bytes=got.bytes)  # fmt: skip
                    return
                error = got.error
            elif r.outcome in ("failed", "denied"):
                error = _clip(r.error or r.outcome)
            if error is None:  # killed, dry run, paused: not an attempt
                store.record_day_failure(
                    self.conn, p.ext, p.panel_id, day, at=now, error=None,
                    max_attempts=history.MAX_ATTEMPTS, next_try_at=now + history.DEFER,
                    count=False,
                )  # fmt: skip
                self._history_hold = now + history.DEFER
                self._audit_panel(p, "panel.archive_deferred", due, day=day, why=r.outcome)
                return
            before = store.get_panel_day(self.conn, p.ext, p.panel_id, day)
            attempts = (0 if before is None else before.attempts) + 1
            row = store.record_day_failure(
                self.conn, p.ext, p.panel_id, day, at=now, error=error,
                max_attempts=history.MAX_ATTEMPTS, next_try_at=history.retry_at(attempts, now),
            )  # fmt: skip
            detail = {"stderr_tail": r.stderr_tail} if r.stderr_tail else {}
            self._audit_panel(p, "panel.archive_failed", due, day=day, error=error,
                              attempts=row.attempts, failed=row.status == "failed",
                              **detail)  # fmt: skip

    # --- gated runs: a task for JobRunner -------------------------------------------------

    def _queue_task(self, job: ScheduledJob, d: Decision) -> None:
        assert d.due is not None and d.last_due is not None
        with transaction(self.conn):
            state = store.get_job(self.conn, job.ext, job.job_id)
            if state is not None and state.last_due is not None and state.last_due >= d.due:
                return  # already claimed (a stale decision)
            task_id = insert_task(
                self.conn,
                NewTask(
                    kind=JOB_KIND,
                    runner=JOB_RUNNER,
                    priority=PRIORITY_BACKGROUND,
                    extension=job.ext,
                    principal=self.owner,
                    wake_at=d.due,
                    checkpoint={
                        "ext": job.ext,
                        "job": job.job_id,
                        "due_at": iso(d.due),
                        "late": d.late,
                    },
                ),
            )
            store.mark_due(self.conn, job.ext, job.job_id, d.last_due, open_task_id=task_id)
            self._audit_missed(job, d)
            self._audit(job, "job.queued", d.due, task_id=task_id, late=d.late)

    # --- inline runs (L0 and core) --------------------------------------------------------

    def _start_inline(self, job: ScheduledJob, d: Decision) -> None:
        assert d.due is not None
        self._claimed[job.key] = d.due
        task = asyncio.get_running_loop().create_task(
            self._run_inline(job, d), name=f"job:{job.label}"
        )
        self._inflight[job.key] = task
        task.add_done_callback(lambda t, key=job.key: self._inline_done(key, t))

    def _inline_done(self, key: tuple[str, str], task: asyncio.Task[None]) -> None:
        self._inflight.pop(key, None)
        if not task.cancelled() and (exc := task.exception()) is not None:
            # Recording the run failed (e.g. the store is locked): free the claim so the due
            # time is decided again on the next tick.
            self._claimed.pop(key, None)
            log.error("inline job not recorded: %s", type(exc).__name__, extra={"job": key})

    async def _run_inline(self, job: ScheduledJob, d: Decision) -> None:
        assert d.due is not None
        async with self._sem:
            if not job.runs_while_paused and read_paused(self.conn):
                result = _Result("skipped_paused")
            else:
                result = await self._call(job, d.due)
            self._finish(job, d, result)

    async def _call(
        self, job: ScheduledJob, due: datetime, extra: Mapping[str, Any] | None = None
    ) -> _Result:
        task_id = f"job:{job.label}:{iso(due)}"
        args: dict[str, Any] = {"due_at": iso(due), **(extra or {})}
        if not job.core and (refused := await self._admit(job, task_id, args)) is not None:
            return refused
        env: dict[str, str] = {}
        if job.spec.secrets:
            try:
                if self.secrets is None:
                    raise SecretError(f"{job.ext}/{job.spec.secrets[0]}", "unavailable")
                env = await self.secrets.env_for(job.ext, job.spec.secrets)
            except SecretError as e:
                return _Result("failed", error=f"ToolFailed: {e}")
        redactor = Redactor(values_of(env))
        ctx = CallContext(secrets=env, idempotency_key=task_id, task_id=task_id)
        start = asyncio.get_running_loop().time()
        tail: str | None = None
        try:
            data = await asyncio.wait_for(job.call(dict(args), ctx), timeout=job.spec.timeout_s)
            json.dumps(data, allow_nan=False)
        except ExtFailed as e:
            outcome = "skipped_paused" if e.reason == "paused" else "failed"
            error, tail = f"ToolFailed: {e.reason}", e.stderr_tail
        except ExtAmbiguous as e:
            outcome = "killed" if e.reason == "killed" else "failed"
            error, tail = f"ExtAmbiguous: {e.reason}", e.stderr_tail
        except ToolFailed as e:
            outcome, error = "failed", f"ToolFailed: {e}"
        except TimeoutError:
            outcome, error = "failed", "timeout"
        except Exception as e:
            # Only the type: the message may carry secret values (the audit row is redacted).
            log.warning("inline job failed: %s", type(e).__name__, extra={"job": job.label})
            outcome, error = "failed", f"{type(e).__name__}: {e}"
        else:
            ms = int((asyncio.get_running_loop().time() - start) * 1000)
            events = tuple(redactor.deep(e) for e in ctx.events)
            return _Result("ok", data=redactor.deep(data), events=events, duration_ms=ms)
        ms = int((asyncio.get_running_loop().time() - start) * 1000)
        return _Result(
            outcome,
            error=_clip(redactor.text(error)),
            stderr_tail=None if not tail else redactor.text(tail),
            duration_ms=ms,
        )

    async def _admit(self, job: ScheduledJob, task_id: str, args: dict[str, Any]) -> _Result | None:
        """Gate an extension job through the tool core (origin ``scheduled``, no pin, no
        spans); the ``InlineExecutor`` spends the token. None: allowed, run it here."""
        scope = CallScope(task_id, self.owner, None, 0, 0, "scheduled", check_schema=False)
        called = await self.core.call(job.spec, args, scope)
        if isinstance(called, Asked):
            return _Result("denied", error=type(called.decision).__name__)
        if isinstance(called, Refused):
            if called.why == "dry_run":
                return _Result("dry_run")
            if called.why == "unavailable":
                return _Result("failed", error="job is not installed")
            if called.why == "not_allowed":
                return _Result("denied", error=APPROVAL_USED)
            d = called.decision
            return _Result("denied", error=d.reason if isinstance(d, Deny) else called.why)
        return None

    def _finish(self, job: ScheduledJob, d: Decision, r: _Result) -> None:
        assert d.due is not None and d.last_due is not None
        now = self.clock()
        with transaction(self.conn):
            store.mark_due(self.conn, job.ext, job.job_id, d.last_due)
            self._audit_missed(job, d)
            match r.outcome:
                case "ok":
                    store.record_run(self.conn, job.ext, job.job_id, at=now, outcome="ok", ok=True)
                    first = not job.core and take_baseline(self.conn, job.ext, job.job_id)
                    n = persist_events(self.conn, job.ext, r.events, baseline=first)
                    self._audit(
                        job, "job.run", d.due, ok=True, late=d.late, events=n, ms=r.duration_ms
                    )
                case "failed":
                    store.record_run(
                        self.conn, job.ext, job.job_id, at=now, outcome="failed", ok=False
                    )
                    detail = {"stderr_tail": r.stderr_tail} if r.stderr_tail else {}
                    self._audit(job, "job.failed", d.due, error=r.error, late=d.late, **detail)
                case "killed":
                    self._audit(job, "job.killed", d.due, error=r.error)
                case "denied":
                    self._audit(job, "job.denied", d.due, reason=r.error)
                case "dry_run":
                    self._audit(job, "job.dry_run", d.due)
                case _:
                    self._audit(job, "job.skipped_paused", d.due)
        if self._claimed.get(job.key) == d.due:
            del self._claimed[job.key]


def _schedule(
    conn: sqlite3.Connection,
    ext: str,
    job_id: str,
    expr: str,
    tz: str | None,
    settings: Settings,
) -> Schedule | None:
    try:
        return parse_schedule(expr, tz or settings.timezone)
    except CronError as e:
        log.warning("job not scheduled", extra={"job": f"{ext}/{job_id}", "reason": str(e)})
        with atomic(conn):
            audit(conn, "job.refused", extension=ext, job=f"{ext}/{job_id}", reason=str(e))
        return None


def job_checkpoint(cp: Mapping[str, Any]) -> tuple[str, str, str, bool] | None:
    """``(ext, job id, due_at, late)`` from a job task's checkpoint, or None if malformed."""
    ext, job, due_at, late = cp.get("ext"), cp.get("job"), cp.get("due_at"), cp.get("late")
    if not (isinstance(ext, str) and isinstance(job, str) and isinstance(due_at, str)):
        return None
    return ext, job, due_at, late is True


__all__ = [
    "CORE_JOBS",
    "JOB_KIND",
    "JOB_RUNNER",
    "CoreDeps",
    "CoreJob",
    "CoreJobFactory",
    "JobCall",
    "ScheduledJob",
    "ScheduledPanel",
    "Scheduler",
    "job_checkpoint",
    "job_spec_name",
    "persist_events",
    "read_paused",
]

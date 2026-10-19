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
  ``extensions.max_concurrent_jobs`` at once. An extension job is gated with
  ``task_id="job:<ext>/<id>:<due>"`` (the gate audits it); there is no executor row. Its
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
from typing import TYPE_CHECKING, Any

from resonant import killswitch
from resonant.executor import CallContext, ToolFailed
from resonant.extensions.briefing import briefing_job
from resonant.extensions.cron import (
    ON_TIME,
    CronError,
    Decision,
    Downtime,
    Outcome,
    Schedule,
    decide,
    parse_schedule,
)
from resonant.extensions.notify import take_baseline
from resonant.extensions.process import ExtAmbiguous, ExtFailed, ProcessTracker
from resonant.extensions.pulse_job import pulse_job
from resonant.gate import Allow, DryRun, Gate, Intent
from resonant.gate.types import Deny
from resonant.loop.types import PRIORITY_BACKGROUND, NewTask
from resonant.redact import Redactor
from resonant.secrets import SecretError, SecretStore, values_of
from resonant.store import extensions as store
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set, transaction, utcnow
from resonant.store.tasks import insert_event, insert_task
from resonant_sdk import Effect, Event, Level, ToolSpec, new_id

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
    """What a core job factory gets at daemon start."""

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


def watches_job(deps: CoreDeps) -> CoreJob | None:
    """``core/watches`` (BR-7, ``runners/watch.py``), imported late: the runners import the
    gate and the router, which this module must not pull in at import time."""
    from resonant.runners.watch import watches_job as factory

    return factory(deps)


# Built-in jobs, one factory each.
CORE_JOBS: tuple[CoreJobFactory, ...] = (briefing_job, pulse_job, watches_job, backup_job)


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
    return n


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
        owner: str,
        secrets: SecretStore | None = None,
        tracker: ProcessTracker | None = None,
        max_concurrent: int = 4,
        tick_s: float = 30.0,
        started_at: datetime | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """``started_at`` (the daemon's start) with kv ``scheduler.last_tick`` gives the
        downtime that ``catchup`` may cover."""
        self.conn = conn
        self.jobs = tuple(jobs)
        self.gate = gate
        self.owner = owner
        self.secrets = secrets
        self.tracker = tracker
        self.clock = clock
        self.on_time = ON_TIME + timedelta(seconds=tick_s)
        self.downtime = self._downtime(started_at or clock())
        self._sem = asyncio.Semaphore(max_concurrent)
        self._claimed: dict[tuple[str, str], datetime] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._paused: bool | None = None
        self._unhook: Callable[[], None] | None = None

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
            owner=owner,
            secrets=components.executor.secrets,
            tracker=None if host is None else host.tracker,
            max_concurrent=settings.extensions.max_concurrent_jobs,
            tick_s=settings.loop.tick_s,
            started_at=started_at,
            clock=clock,
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

    async def _call(self, job: ScheduledJob, due: datetime) -> _Result:
        task_id = f"job:{job.label}:{iso(due)}"
        args: dict[str, Any] = {"due_at": iso(due)}
        if not job.core:
            intent = Intent.for_tool(
                job.spec, task_id=task_id, principal=self.owner, args=args, origin="scheduled"
            )
            decision = self.gate.evaluate(intent)
            if isinstance(decision, DryRun):
                return _Result("dry_run")
            if not isinstance(decision, Allow):
                reason = decision.reason if isinstance(decision, Deny) else type(decision).__name__
                return _Result("denied", error=reason)
            self.gate.consume(decision.token, intent.hash)  # no executor: spend it here
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
    "Scheduler",
    "job_checkpoint",
    "job_spec_name",
    "persist_events",
    "read_paused",
]

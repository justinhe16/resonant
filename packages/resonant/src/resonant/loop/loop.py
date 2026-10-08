"""The generic agent loop.

One loop serves every task. It is driven by events (Loop.submit) and by a periodic tick:

- Route: an event with no task_id goes to the router, which may create a task. If the
  router raises, the event is retried on later ticks and dead-lettered after
  ``ROUTE_MAX_FAILURES`` failures. With no router configured, events wait unrouted.
- Wake: waiting tasks whose wake_at has passed are requeued.
- Stuck: steps running longer than the runner's ``max_step_s`` (default
  ``stuck_after_s``) are cancelled and retried.
- Dispatch: queued tasks run in priority order, up to ``max_parallel`` steps at once. One
  parallel slot is kept for on-call. Runners that need a Claude slot must also acquire one
  from the SlotPool.

Each step's outcome, checkpoint, and consumed events are committed in one transaction.
If the daemon crashes mid-step, the task is left 'running' with its last checkpoint and
its events unconsumed. On the next start, ``resume()`` requeues it, counting the crash as
an attempt. A graceful shutdown requeues interrupted steps without spending an attempt.
If persisting itself fails (e.g. the database is unavailable), the loop stops with that
error so the daemon exits and launchd restarts it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from resonant.config import LoopConfig
from resonant.loop.slots import SlotPool
from resonant.loop.types import Continue, Done, Fail, NewTask, Runner, StepOutcome, Task, Wait
from resonant.store import tasks as repo
from resonant.store.audit import audit
from resonant.store.db import transaction, utcnow
from resonant_sdk import Event

log = logging.getLogger(__name__)

Router = Callable[[Event], Awaitable[NewTask | None]]
Clock = Callable[[], datetime]

ROUTE_MAX_FAILURES = 5
_RETRY_BASE_S = 5.0
_RETRY_MAX_S = 300.0
_ONCALL_LANE = "oncall"


def retry_backoff(attempt: int) -> timedelta:
    return timedelta(seconds=min(_RETRY_BASE_S * 2 ** (attempt - 1), _RETRY_MAX_S))


@dataclass
class _StepContext:
    conn: sqlite3.Connection
    task_id: str

    def save(
        self,
        *,
        checkpoint: dict[str, Any] | None = None,
        claude_session_id: str | None = None,
        worktree_path: str | None = None,
    ) -> None:
        if checkpoint is not None:
            json.dumps(checkpoint)  # fail inside the runner, not later in the loop
        with transaction(self.conn):
            repo.save_progress(
                self.conn,
                self.task_id,
                checkpoint=checkpoint,
                claude_session_id=claude_session_id,
                worktree_path=worktree_path,
            )


class Loop:
    def __init__(
        self,
        conn: sqlite3.Connection,
        runners: Mapping[str, Runner],
        config: LoopConfig,
        slots: SlotPool,
        *,
        router: Router | None = None,
        clock: Clock = utcnow,
        max_parallel: int = 8,
    ) -> None:
        if max_parallel < 2:
            raise ValueError("max_parallel must be >= 2 (one slot is kept for on-call)")
        self.conn = conn
        self.runners = dict(runners)
        self.config = config
        self.slots = slots
        self.router = router
        self.clock = clock
        self.max_parallel = max_parallel
        self.last_tick: datetime | None = None
        self._wakeup = asyncio.Event()
        self._stopping = False
        self._fatal: BaseException | None = None
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._lanes: dict[str, str] = {}
        self._started: dict[str, datetime] = {}
        self._limits: dict[str, float] = {}
        self._timed_out: set[str] = set()
        self._route_failures: dict[str, int] = {}

    # --- inputs ------------------------------------------------------------------------

    def submit(self, event: Event) -> bool:
        """Append an event. Wakes its task if it is waiting. Returns False for duplicates."""
        with transaction(self.conn):
            inserted = repo.insert_event(self.conn, event)
            if inserted and event.task_id:
                repo.set_status(self.conn, event.task_id, "queued", expect=("waiting",))
        if inserted:
            self._wakeup.set()
        return inserted

    def create_task(self, spec: NewTask) -> str:
        with transaction(self.conn):
            task_id = repo.insert_task(self.conn, spec)
        self._wakeup.set()
        return task_id

    def cancel(self, task_id: str) -> bool:
        with transaction(self.conn):
            ok = repo.set_status(
                self.conn, task_id, "cancelled", expect=("queued", "running", "waiting")
            )
        if ok and (inflight := self._inflight.get(task_id)):
            inflight.cancel()
        return ok

    # --- lifecycle ---------------------------------------------------------------------

    def resume(self) -> int:
        with transaction(self.conn):
            requeued, failed = repo.requeue_interrupted(self.conn, self.config.max_attempts)
        if requeued or failed:
            log.warning("after crash: requeued %d task(s), failed %d", requeued, failed)
        return requeued

    async def run(self) -> None:
        self.resume()
        try:
            while not self._stopping:
                await self.run_once()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wakeup.wait(), timeout=self.config.tick_s)
                self._wakeup.clear()
        finally:
            await self._shutdown()
        if self._fatal is not None:
            raise self._fatal

    def stop(self) -> None:
        self._stopping = True
        self._wakeup.set()

    async def _shutdown(self, grace_s: float = 10.0) -> None:
        if not self._inflight:
            return
        tasks = dict(self._inflight)
        _, pending = await asyncio.wait(tasks.values(), timeout=grace_s)
        interrupted = [tid for tid, t in tasks.items() if t in pending]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if interrupted and self._fatal is None:
            with transaction(self.conn):
                repo.requeue_running(self.conn, interrupted)

    async def run_once(self) -> None:
        now = self.clock()
        self.last_tick = now
        if self.router is not None:
            await self._route_unrouted()
        with transaction(self.conn):
            repo.wake_due(self.conn, now)
        self._check_stuck(now)
        if not self._stopping:
            self._dispatch(now)

    async def run_until_idle(self, max_rounds: int = 100) -> None:
        """Test helper: run ticks until nothing is in flight and nothing is runnable."""
        for _ in range(max_rounds):
            await self.run_once()
            if self._fatal is not None:
                raise self._fatal
            if self._inflight:
                await asyncio.gather(*self._inflight.values(), return_exceptions=True)
                await asyncio.sleep(0)  # let done-callbacks run
                continue
            routable = self.router is not None and any(
                e.id not in self._route_failures for e in repo.unrouted_events(self.conn)
            )
            if not repo.runnable_tasks(self.conn, self.clock(), 1) and not routable:
                return
        raise RuntimeError("loop did not become idle")

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    # --- routing -----------------------------------------------------------------------

    async def _route_unrouted(self) -> None:
        assert self.router is not None
        for event in repo.unrouted_events(self.conn):
            try:
                spec = await self.router(event)
            except Exception as e:
                failures = self._route_failures.get(event.id, 0) + 1
                self._route_failures[event.id] = failures
                log.exception(
                    "router failed for event %s (%d/%d)", event.id, failures, ROUTE_MAX_FAILURES
                )
                if failures >= ROUTE_MAX_FAILURES:
                    with transaction(self.conn):
                        repo.consume_events(self.conn, [event.id])
                        audit(
                            self.conn,
                            "event.dead_letter",
                            trace_id=event.trace_id,
                            event_id=event.id,
                            error=f"{type(e).__name__}: {e}",
                        )
                    self._route_failures.pop(event.id, None)
                continue
            self._route_failures.pop(event.id, None)
            with transaction(self.conn):
                if spec is None:  # the router explicitly decided: no task for this event
                    repo.consume_events(self.conn, [event.id])
                    continue
                spec = replace(spec, origin_event_id=event.id, trace_id=event.trace_id)
                task_id = repo.insert_task(self.conn, spec)
                # The origin event becomes the new task's first input.
                repo.attach_event(self.conn, event.id, task_id)

    # --- dispatch & steps --------------------------------------------------------------

    def _check_stuck(self, now: datetime) -> None:
        for task_id, started in list(self._started.items()):
            if task_id in self._timed_out or task_id not in self._inflight:
                continue
            limit_s = self._limits.get(task_id, self.config.stuck_after_s)
            if now - started > timedelta(seconds=limit_s):
                log.warning("task %s step exceeded %ss; cancelling", task_id, limit_s)
                self._timed_out.add(task_id)
                self._inflight[task_id].cancel()

    def _dispatch(self, now: datetime) -> None:
        if len(self._inflight) >= self.max_parallel:
            return
        for task in repo.runnable_tasks(self.conn, now, limit=500):
            oncall = task.lane == _ONCALL_LANE
            running_non_oncall = sum(1 for lane in self._lanes.values() if lane != _ONCALL_LANE)
            if len(self._inflight) >= self.max_parallel:
                break
            if not oncall and running_non_oncall >= self.max_parallel - 1:
                continue  # last parallel slot is kept for on-call
            if task.id in self._inflight:
                continue
            runner = self.runners.get(task.runner)
            if runner is None:
                with transaction(self.conn):
                    repo.set_status(
                        self.conn, task.id, "failed", error=f"unknown runner {task.runner!r}"
                    )
                continue
            if runner.uses_claude_slot and not self.slots.try_acquire(task.id, oncall=oncall):
                continue
            with transaction(self.conn):
                claimed = repo.set_status(self.conn, task.id, "running", expect=("queued",))
            if not claimed:
                self.slots.release(task.id)
                continue
            self._started[task.id] = now
            self._limits[task.id] = runner.max_step_s or self.config.stuck_after_s
            self._lanes[task.id] = task.lane
            step = asyncio.create_task(
                self._step(replace(task, status="running"), runner), name=f"step:{task.id}"
            )
            self._inflight[task.id] = step
            # Cleanup lives in a done-callback, not in _step's finally: a step cancelled
            # before it starts never runs its body, but the callback always fires.
            step.add_done_callback(lambda t, tid=task.id: self._step_done(tid, t))

    def _step_done(self, task_id: str, step: asyncio.Task[None]) -> None:
        self._inflight.pop(task_id, None)
        self._lanes.pop(task_id, None)
        self._started.pop(task_id, None)
        self._limits.pop(task_id, None)
        self._timed_out.discard(task_id)
        self.slots.release(task_id)
        if not step.cancelled() and (exc := step.exception()) is not None:
            # Persisting a step failed (e.g. the database is unavailable). The task stays
            # 'running'. Stop the loop so the daemon exits and resume() recovers on restart.
            log.critical("failed to persist step for task %s: %r", task_id, exc)
            if self._fatal is None:
                self._fatal = exc
            self.stop()
        self._wakeup.set()

    async def _step(self, task: Task, runner: Runner) -> None:
        events = repo.pending_events(self.conn, task.id)
        try:
            outcome: StepOutcome = await runner.step(task, events, _StepContext(self.conn, task.id))
        except asyncio.CancelledError:
            if task.id not in self._timed_out:
                raise
            outcome = Fail("step exceeded its time limit", retryable=True)
        except Exception as e:
            log.exception("runner %s failed on task %s", runner.name, task.id)
            outcome = Fail(f"{type(e).__name__}: {e}", retryable=True)
        try:
            _check_serializable(outcome)
        except (TypeError, ValueError) as e:
            outcome = Fail(f"runner returned an unserializable outcome: {e}")
        self._persist(task, events, outcome)

    def _persist(self, task: Task, events: list[Event], outcome: StepOutcome) -> None:
        # A retryable failure leaves its events unconsumed so the retry sees them again.
        retrying = (
            isinstance(outcome, Fail)
            and outcome.retryable
            and task.attempts + 1 < self.config.max_attempts
        )
        with transaction(self.conn):
            if not retrying:
                repo.consume_events(self.conn, [e.id for e in events])
            match outcome:
                case Continue(checkpoint=cp):
                    saved = repo.save_step(
                        self.conn, task.id, status="queued", checkpoint=cp, reset_attempts=True
                    )
                case Wait(reason=reason, checkpoint=cp, wake_at=wake_at):
                    # Events that arrived during the step must not be missed: requeue now.
                    if repo.pending_events(self.conn, task.id):
                        saved = repo.save_step(
                            self.conn,
                            task.id,
                            status="queued",
                            checkpoint=cp,
                            reset_attempts=True,
                        )
                    else:
                        saved = repo.save_step(
                            self.conn,
                            task.id,
                            status="waiting",
                            checkpoint=cp,
                            wait_reason=reason,
                            wake_at=wake_at,
                            reset_attempts=True,
                        )
                case Done(result=result, checkpoint=cp):
                    saved = repo.save_step(
                        self.conn, task.id, status="done", checkpoint=cp, result=result
                    )
                case Fail(error=error, checkpoint=cp, retry_at=retry_at):
                    saved = repo.save_step(
                        self.conn,
                        task.id,
                        status="queued" if retrying else "failed",
                        checkpoint=cp,
                        wake_at=(retry_at or self.clock() + retry_backoff(task.attempts + 1))
                        if retrying
                        else None,
                        error=error,
                        attempts_delta=1,
                    )
        if not saved:
            log.info("task %s changed state during its step (likely cancelled)", task.id)


def _check_serializable(outcome: StepOutcome) -> None:
    match outcome:
        case Continue(checkpoint=cp) | Wait(checkpoint=cp):
            json.dumps(cp)
        case Done(result=result, checkpoint=cp):
            json.dumps(result)
            json.dumps(cp)
        case Fail(checkpoint=cp):
            json.dumps(cp)

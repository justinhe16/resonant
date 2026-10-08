"""The generic agent loop.

One loop serves every task. It is driven by events (Loop.submit) and by a periodic tick:

- Route: an event with no task_id goes to the router, which may create a task.
- Wake: waiting tasks whose wake_at has passed are requeued.
- Stuck: in-flight steps running longer than ``stuck_after_s`` are cancelled and retried.
- Dispatch: queued tasks run in priority order, up to ``max_parallel`` steps at once.
  Runners that need a Claude slot also have to acquire one from the SlotPool.

Each step's outcome, checkpoint, and consumed events are committed in one transaction.
A crash mid-step therefore leaves the task 'running' with its previous checkpoint and its
events unconsumed. ``resume()`` requeues it on the next start.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta

from resonant_sdk import Event

from resonant.config import LoopConfig
from resonant.loop.slots import SlotPool
from resonant.loop.types import Continue, Done, Fail, NewTask, Runner, StepOutcome, Task, Wait
from resonant.store import tasks as repo
from resonant.store.db import transaction, utcnow

log = logging.getLogger(__name__)

Router = Callable[[Event], Awaitable[NewTask | None]]
Clock = Callable[[], datetime]

_RETRY_BASE_S = 5.0
_RETRY_MAX_S = 300.0


def retry_backoff(attempt: int) -> timedelta:
    return timedelta(seconds=min(_RETRY_BASE_S * 2 ** (attempt - 1), _RETRY_MAX_S))


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
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._started: dict[str, datetime] = {}
        self._timed_out: set[str] = set()

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
            n = repo.requeue_interrupted(self.conn)
        if n:
            log.info("resumed %d interrupted task(s)", n)
        return n

    async def run(self) -> None:
        self.resume()
        while not self._stopping:
            await self.run_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.config.tick_s)
            self._wakeup.clear()
        await self._shutdown()

    def stop(self) -> None:
        self._stopping = True
        self._wakeup.set()

    async def _shutdown(self, grace_s: float = 10.0) -> None:
        if not self._inflight:
            return
        _, pending = await asyncio.wait(self._inflight.values(), timeout=grace_s)
        for t in pending:
            t.cancel()  # not persisted: these resume from their checkpoint next start
        await asyncio.gather(*pending, return_exceptions=True)

    async def run_once(self) -> None:
        now = self.clock()
        self.last_tick = now
        await self._route_unrouted()
        with transaction(self.conn):
            repo.wake_due(self.conn, now)
        self._check_stuck(now)
        self._dispatch(now)

    async def run_until_idle(self, max_rounds: int = 100) -> None:
        """Test helper: run ticks until nothing is in flight and nothing is runnable."""
        for _ in range(max_rounds):
            await self.run_once()
            if self._inflight:
                await asyncio.gather(*self._inflight.values(), return_exceptions=True)
                continue
            if not repo.runnable_tasks(self.conn, self.clock(), 1) and not (
                self.router and repo.unrouted_events(self.conn, 1)
            ):
                return
        raise RuntimeError("loop did not become idle")

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    # --- internals ---------------------------------------------------------------------

    async def _route_unrouted(self) -> None:
        for event in repo.unrouted_events(self.conn):
            spec: NewTask | None = None
            if self.router is not None:
                try:
                    spec = await self.router(event)
                except Exception:
                    log.exception("router failed for event %s", event.id)
            with transaction(self.conn):
                if spec is None:
                    repo.consume_events(self.conn, [event.id])
                    continue
                spec = replace(spec, origin_event_id=event.id, trace_id=event.trace_id)
                task_id = repo.insert_task(self.conn, spec)
                # The origin event becomes the new task's first input.
                repo.attach_event(self.conn, event.id, task_id)

    def _check_stuck(self, now: datetime) -> None:
        limit = timedelta(seconds=self.config.stuck_after_s)
        for task_id, started in list(self._started.items()):
            if now - started > limit and task_id not in self._timed_out:
                log.warning("task %s step exceeded %ss; cancelling", task_id, limit.seconds)
                self._timed_out.add(task_id)
                self._inflight[task_id].cancel()

    def _dispatch(self, now: datetime) -> None:
        free = self.max_parallel - len(self._inflight)
        if free <= 0:
            return
        # Over-fetch so tasks blocked on a Claude slot don't starve the ones behind them.
        for task in repo.runnable_tasks(self.conn, now, limit=free * 4):
            if free == 0:
                break
            if task.id in self._inflight:
                continue
            runner = self.runners.get(task.runner)
            if runner is None:
                with transaction(self.conn):
                    repo.set_status(
                        self.conn, task.id, "failed", error=f"unknown runner {task.runner!r}"
                    )
                continue
            if runner.uses_claude_slot and not self.slots.try_acquire(
                task.id, oncall=task.lane == "oncall"
            ):
                continue
            with transaction(self.conn):
                claimed = repo.set_status(self.conn, task.id, "running", expect=("queued",))
            if not claimed:
                self.slots.release(task.id)
                continue
            self._started[task.id] = now
            self._inflight[task.id] = asyncio.create_task(
                self._step(replace(task, status="running"), runner), name=f"step:{task.id}"
            )
            free -= 1

    async def _step(self, task: Task, runner: Runner) -> None:
        try:
            events = repo.pending_events(self.conn, task.id)
            try:
                outcome: StepOutcome = await runner.step(task, events)
            except asyncio.CancelledError:
                if task.id not in self._timed_out:
                    raise
                outcome = Fail("step exceeded stuck_after_s", retryable=True)
            except Exception as e:
                log.exception("runner %s failed on task %s", runner.name, task.id)
                outcome = Fail(f"{type(e).__name__}: {e}", retryable=True)
            self._persist(task, events, outcome)
        finally:
            self._inflight.pop(task.id, None)
            self._started.pop(task.id, None)
            self._timed_out.discard(task.id)
            self.slots.release(task.id)
            self._wakeup.set()

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
                    repo.save_step(
                        self.conn, task.id, status="queued", checkpoint=cp, reset_attempts=True
                    )
                case Wait(reason=reason, checkpoint=cp, wake_at=wake_at):
                    # Events that arrived during the step must not be missed: requeue now.
                    if repo.pending_events(self.conn, task.id):
                        repo.save_step(self.conn, task.id, status="queued", checkpoint=cp)
                    else:
                        repo.save_step(
                            self.conn,
                            task.id,
                            status="waiting",
                            checkpoint=cp,
                            wait_reason=reason,
                            wake_at=wake_at,
                            reset_attempts=True,
                        )
                case Done(result=result, checkpoint=cp):
                    repo.save_step(self.conn, task.id, status="done", checkpoint=cp, result=result)
                case Fail(error=error):
                    if retrying:
                        repo.save_step(
                            self.conn,
                            task.id,
                            status="queued",
                            checkpoint=None,
                            wake_at=self.clock() + retry_backoff(task.attempts + 1),
                            error=error,
                            attempts_delta=1,
                        )
                    else:
                        repo.save_step(
                            self.conn,
                            task.id,
                            status="failed",
                            checkpoint=None,
                            error=error,
                            attempts_delta=1,
                        )

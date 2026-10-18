"""Watch checks (BR-7, docs/brain-plan.md D10; the store and decisions are
``brain/watches.py``).

**Queueing: ``core/watches``** (``scheduler.CORE_JOBS``, every 5 minutes in
``settings.timezone``, no model): an expired watch ends (``expired``, no text); every other
due watch whose previous check task has ended gets a ``NewTask(kind="watch",
runner="watch", priority=background, checkpoint={"watch": id})``, and ``next_at`` moves one
interval on, in one transaction (``claim`` is compare-and-set, so a watch is never queued
twice). Not scheduled with ``brain.watches.enabled: false``, iMessage off or no owner. The
scheduler skips it while paused (no catch-up), so nothing is checked during ``/kill``.

**One check** (``WatchRunner.step``):

- ``tool``: the registry spec is looked up again (still an L0 read the principal may use,
  else the watch fails), gated with ``origin="scheduled"`` on this task and run by the
  executor (recorded under the task's idempotency key). Its result never reaches a model:
  ``watches.judge_tool`` decides in code.
- ``ask``: ``AskRunner.enqueue(question, kind="research", watch=id)``; the ask runner hands
  the answer to :meth:`WatchRunner.ask_answered` instead of texting it (YES/NO first). A
  refused ask (the daily cap, the usage limit, off) is a skipped check, not a failure.

A hit sends one notice (``brain/notices.py``: through the pulse, at most
``brain.proactive_per_day`` texts a day) and, with ``then="stop"``, ends the watch
(``fired``). ``max_fails`` failed checks in a row end it (``failed``) with a notice. The
notice is sent before the check is recorded, deduped by ``watch/<id>/<n>``, so a retry after
a crash never texts twice and never loses the hit.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from resonant import killswitch
from resonant.brain import watches as store
from resonant.brain.notices import send_notice
from resonant.config import Settings
from resonant.gate import Allow, Intent
from resonant.gate.types import Deny
from resonant.gateway.channel import Channel, ChannelError
from resonant.loop.types import PRIORITY_BACKGROUND, Done, Fail, NewTask, StepContext, Task
from resonant.principals import Principals
from resonant.runners.local import RunnerGate, ToolExecutor
from resonant.store.audit import audit
from resonant.store.db import atomic, utcnow
from resonant.store.tasks import insert_task
from resonant.tools import ToolRegistry
from resonant_sdk import Effect, Event, Level

if TYPE_CHECKING:
    from resonant.executor import CallContext
    from resonant.extensions.scheduler import CoreDeps, CoreJob
    from resonant.loop.types import StepOutcome
    from resonant.runners.ask import AskRunner

log = logging.getLogger(__name__)

NAME = "watch"
TASK_KIND = "watch"
JOB_ID = "watches"
SCHEDULE = "*/5 * * * *"
CATCHUP = timedelta(minutes=10)
ERROR_MAX = 120


class WatchRunner:
    """``runner="watch"``. ``asks()`` is the ask runner when there is one (it is built after
    this runner, and absent with ask_claude off)."""

    name = NAME
    uses_claude_slot = False
    max_step_s: float | None = 330.0  # an extension read may take its 300 s

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        gate: RunnerGate,
        executor: ToolExecutor,
        registry: ToolRegistry,
        principals: Principals,
        channel: Channel,
        settings: Settings,
        asks: Callable[[], AskRunner | None] = lambda: None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.conn = conn
        self.gate = gate
        self.executor = executor
        self.registry = registry
        self.principals = principals
        self.channel = channel
        self.settings = settings
        self.asks = asks
        self.clock = clock

    # --- one check -------------------------------------------------------------------------

    async def step(self, task: Task, events: list[Event], ctx: StepContext) -> StepOutcome:
        raw = task.checkpoint.get("watch")
        if not isinstance(raw, int) or isinstance(raw, bool):
            return Fail("watch checkpoint is malformed")
        w = store.get(self.conn, raw)
        if w is None or w.state != "active":
            return Done({"watch": raw, "status": "ended"})
        try:
            return await self._check(task, w)
        finally:
            store.release(self.conn, w, task.id)

    async def _check(self, task: Task, w: store.Watch) -> StepOutcome:
        now = self.clock()
        if _paused(self.conn):
            return Done({"watch": w.id, "status": "paused"})
        if now >= w.expires_at:
            self._end(w, "expired", now)
            return Done({"watch": w.id, "status": "expired"})
        if w.check_kind == "ask":
            return self._ask(task, w)
        spec = self.registry.get(w.extension, w.tool or "")
        if (
            spec is None
            or spec.effect is not Effect.READ
            or spec.effective_level is not Level.L0
            or not self.principals.may_request(w.principal, spec)
        ):
            return await self._failed(w, now, "its tool is gone or no longer a read")
        intent = Intent.for_tool(
            spec,
            task_id=task.id,
            principal=w.principal,
            args=w.args,
            trace_id=task.trace_id,
            origin="scheduled",
        )
        decision = self.gate.evaluate(intent)
        if not isinstance(decision, Allow):
            why = decision.reason if isinstance(decision, Deny) else type(decision).__name__
            return await self._failed(w, now, f"not allowed: {why}")
        result = await self.executor.execute(intent, decision, step_no=task.step_no)
        if not result.ok:
            return await self._failed(w, now, result.error or "failed")
        verdict = store.judge_tool(w, result.data or {})
        return await self._settle(w, verdict, now)

    def _ask(self, task: Task, w: store.Watch) -> StepOutcome:
        asks = self.asks()
        if asks is None:
            store.record(self.conn, w, now=self.clock(), ok=True, met=w.last_met)
            return Done({"watch": w.id, "status": "skipped", "why": "ask_claude isn't running"})
        result = asks.enqueue(
            w.question or w.what,
            kind="research",
            principal=w.principal,
            requested_by=task.id,
            trace_id=task.trace_id,
            watch=w.id,
        )
        queued: object = getattr(result, "task_id", None)
        if isinstance(queued, str):
            return Done({"watch": w.id, "status": "asked", "ask_task": queued})
        store.record(self.conn, w, now=self.clock(), ok=True, met=w.last_met)
        return Done({"watch": w.id, "status": "skipped", "why": getattr(result, "text", "")})

    async def ask_answered(self, watch_id: int, ask_task: str, status: str, reply: str) -> None:
        """``AskRunner.on_watch``: one ask check's answer. Idempotent per ask task."""
        w = store.get(self.conn, watch_id)
        if w is None or w.state != "active" or w.last_ask_task == ask_task:
            return
        now = self.clock()
        if status in ("usage_limit", "paused", "killed", "not_set_up"):
            store.record(self.conn, w, now=now, ok=True, met=w.last_met, ask_task=ask_task)
            return
        if status != "answered":
            await self._failed(w, now, f"Claude: {status}", ask_task=ask_task)
            return
        await self._settle(w, store.judge_ask(w, reply), now, ask_task=ask_task)

    # --- outcomes --------------------------------------------------------------------------

    async def _settle(
        self, w: store.Watch, v: store.Verdict, now: datetime, *, ask_task: str | None = None
    ) -> StepOutcome:
        if v.fire:
            await self._notice(w, f"watch/{w.id}/{w.fired + 1}", store.notice_title(w, v.detail))
        store.record(
            self.conn,
            w,
            now=now,
            ok=True,
            met=v.met,
            baseline=v.baseline,
            fired=v.fire,
            ask_task=ask_task,
        )
        if v.fire and w.then == "stop":
            self._end(w, "fired", now)
        with atomic(self.conn):
            audit(
                self.conn,
                "watch.checked",
                requested_by=w.principal,
                decision="fired" if v.fire else "met" if v.met else "not_met",
                watch=w.id,
            )
        return Done({"watch": w.id, "status": "fired" if v.fire else "checked", "met": v.met})

    async def _failed(
        self, w: store.Watch, now: datetime, error: str, *, ask_task: str | None = None
    ) -> StepOutcome:
        error = " ".join(error.split())[:ERROR_MAX]
        last = w.fails + 1 >= self.settings.brain.watches.max_fails
        if last:
            await self._notice(w, f"watch/{w.id}/failed", store.failed_title(w, error))
        store.record(self.conn, w, now=now, ok=False, error=error, ask_task=ask_task)
        if last:
            self._end(w, "failed", now, error)
        return Done({"watch": w.id, "status": "failed" if last else "check_failed"})

    async def _notice(self, w: store.Watch, key: str, title: str) -> None:
        try:
            await send_notice(
                self.conn,
                self.channel,
                w.principal,
                self.settings,
                key=key,
                title=title,
                now=self.clock(),
            )
        except ChannelError as e:  # held in the pulse already; the next pulse carries it
            log.warning("watch notice not sent (%s)", type(e).__name__)

    def _end(
        self, w: store.Watch, state: store.State, now: datetime, error: str | None = None
    ) -> None:
        if store.end(self.conn, w.id, state, now, error):
            with atomic(self.conn):
                audit(
                    self.conn, "watch.ended", requested_by=w.principal, decision=state, watch=w.id
                )


def _paused(conn: sqlite3.Connection) -> bool:
    try:
        return killswitch.is_paused(conn)
    except killswitch.PausedStateUnknownError:
        return True


# --- core/watches --------------------------------------------------------------------------


def queue_due(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """End the expired watches and queue a check for every other due one."""
    queued = expired = 0
    for w in store.due(conn, now):
        if now >= w.expires_at:
            if store.end(conn, w.id, "expired", now):
                expired += 1
                with atomic(conn):
                    audit(
                        conn,
                        "watch.ended",
                        requested_by=w.principal,
                        decision="expired",
                        watch=w.id,
                    )
            continue
        try:
            with atomic(conn):
                task_id = insert_task(
                    conn,
                    NewTask(
                        kind=TASK_KIND,
                        runner=NAME,
                        priority=PRIORITY_BACKGROUND,
                        principal=w.principal,
                        checkpoint={"watch": w.id, "title": f"watch {w.code}"},
                    ),
                )
                if not store.claim(conn, w, task_id, now):
                    raise _Lost
        except _Lost:
            continue
        queued += 1
    return {"queued": queued, "expired": expired}


class _Lost(Exception):
    """Another tick claimed the watch first (the insert is rolled back)."""


def watches_job(deps: CoreDeps, *, clock: Callable[[], datetime] = utcnow) -> CoreJob | None:
    """The ``CORE_JOBS`` factory. None when watches are off, with no channel or no owner."""
    from resonant.extensions.scheduler import CoreJob

    if not deps.settings.brain.watches.enabled or deps.channel is None or deps.owner is None:
        return None
    conn = deps.conn

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        del args, ctx
        return queue_due(conn, clock())

    return CoreJob(id=JOB_ID, schedule=SCHEDULE, run=run, catchup=CATCHUP, timeout_s=30.0)


__all__ = ["JOB_ID", "NAME", "WatchRunner", "queue_due", "watches_job"]

"""Watch checks (BR-7, docs/brain-plan.md D10; the store and decisions are
``brain/watches.py``).

**Queueing: ``core/watches``** (``scheduler.CORE_JOBS``, every 5 minutes in
``settings.timezone``, no model): an expired watch ends (``expired``, no text; one that
never fired leaves a low item for the briefing, :func:`note_unfired`); every other due
watch whose previous check task has ended gets a ``NewTask(kind="watch",
runner="watch", priority=background, checkpoint={"watch": id})``, and ``next_at`` moves one
interval on, in one transaction (``claim`` is compare-and-set, so a watch is never queued
twice). Not scheduled with ``brain.watches.enabled: false``, iMessage off or no owner. The
scheduler skips it while paused (no catch-up), so nothing is checked during ``/kill``.

**One check** (``WatchRunner.step``):

- ``tool``: the registry spec is looked up again (still an L0 read the principal may use,
  else the watch fails), through ``ToolCore.call`` (TC-1): gated with ``origin="scheduled"``
  on this task and run by the executor (recorded under the task's idempotency key). Its
  result never reaches a model: ``watches.judge_tool`` decides in code.
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
from resonant.brain.notices import TTL_S as NOTICE_TTL_S
from resonant.brain.notices import item_key as notice_key
from resonant.brain.notices import send_notice
from resonant.config import Settings
from resonant.extensions.notify import clean_text
from resonant.gate.types import Deny
from resonant.gateway.channel import Channel, ChannelError
from resonant.loop.types import PRIORITY_BACKGROUND, Done, Fail, NewTask, StepContext, Task
from resonant.principals import Principals
from resonant.store import pulse as pulse_items
from resonant.store.audit import audit
from resonant.store.db import atomic, utcnow
from resonant.store.tasks import insert_task
from resonant.toolcore import Asked, CallScope, Ran, Refused, RunnerGate, ToolCore, ToolExecutor
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
        self.core = ToolCore(conn, registry=registry, gate=gate, executor=executor)
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
        scope = CallScope(
            task.id, w.principal, task.trace_id, task.step_no, 0, "scheduled", check_schema=False
        )
        called = await self.core.call(spec, w.args, scope)
        if isinstance(called, Asked | Refused):
            decision = called.decision
            if isinstance(decision, Deny):
                why = decision.reason
            elif decision is not None:
                why = type(decision).__name__
            else:  # refused before the gate (no decision)
                why = called.why if isinstance(called, Refused) else "refused"
            return await self._failed(w, now, f"not allowed: {why}")
        if not isinstance(called, Ran):
            error = called.result.error if called.result is not None else None
            return await self._failed(w, now, error or "failed")
        verdict = store.judge_tool(w, called.result.data or {})
        return await self._settle(w, verdict, now)

    def _ask(self, task: Task, w: store.Watch) -> StepOutcome:
        asks = self.asks()
        if asks is None:
            store.record(self.conn, w, now=self.clock(), ok=True, met=w.last_met)
            return Done({"watch": w.id, "status": "skipped", "why": "ask_claude isn't running"})
        prior = self.conn.execute(
            "SELECT id FROM tasks WHERE runner = 'ask' "
            "AND json_extract(checkpoint, '$.requested_by') = ? LIMIT 1",
            (task.id,),
        ).fetchone()
        if prior is not None:  # a retry after a crash: this check already asked
            return Done({"watch": w.id, "status": "asked", "ask_task": prior[0]})
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
        ended = store.record(
            self.conn,
            w,
            now=now,
            ok=True,
            met=v.met,
            baseline=v.baseline,
            fired=v.fire,
            ask_task=ask_task,
            end_state="fired" if v.fire and w.then == "stop" else None,
        )
        if ended:
            self._ended(w, "fired")
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
        ended = store.record(self.conn, w, now=now, ok=False, error=error, ask_task=ask_task,
                             end_state="failed" if last else None)  # fmt: skip
        if ended:
            self._ended(w, "failed")
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
            self._ended(w, state)
            if state == "expired":
                note_unfired(self.conn, w, now)

    def _ended(self, w: store.Watch, state: store.State) -> None:
        with atomic(self.conn):
            audit(self.conn, "watch.ended", requested_by=w.principal, decision=state, watch=w.id)


def _paused(conn: sqlite3.Connection) -> bool:
    try:
        return killswitch.is_paused(conn)
    except killswitch.PausedStateUnknownError:
        return True


def note_unfired(conn: sqlite3.Connection, w: store.Watch, now: datetime) -> None:
    """A watch that expired without ever firing leaves a ``low`` core item for the briefing
    (``notice/watch/<id>/ended``, ``W3 … ended without firing``), so it doesn't end in
    silence. It sends nothing and isn't a notice against the daily cap; it expires with its
    TTL. Call only when this call ended the watch (``store.end`` returned True). Never
    raises: the watch has ended either way, and ``queue_due`` goes on with the rest."""
    if w.fired:
        return
    key = notice_key(f"watch/{w.id}/ended")
    title = clean_text(store.ended_title(w))
    try:
        with atomic(conn):
            pulse_items.upsert(conn, "core", key, title, "low", {"ended": "expired"}, False,
                               NOTICE_TTL_S, now)  # fmt: skip
    except (sqlite3.Error, ValueError):
        log.exception("ended-watch item not written", extra={"watch": w.id})


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
                note_unfired(conn, w, now)
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


__all__ = ["JOB_ID", "NAME", "WatchRunner", "note_unfired", "queue_due", "watches_job"]

"""JobRunner (``runner="job"``): runs a scheduled job above L0. No model.

The scheduler creates the task (``kind="job"``, ``wake_at`` = the due time, checkpoint
``{ext, job, due_at, late}``) in the transaction that claims the due time. One step:

1. **Finished already** (``checkpoint.finished``): only (re)send the reports.
2. **Pinned call.** ``job_<id> {due_at}`` with ``origin="scheduled"`` and the task's owner as
   principal, keyed ``idempotency_key(task_id, 0, 0, intent.hash)``, through the shared tool
   call path (TC-1, ``ToolCore``). ``ToolCore.recorded`` first: succeeded is replayed, started
   is ambiguous (never re-run), failed is the definite failure. So a crash anywhere runs the
   job at most once.
3. **Paused** (kill switch): nothing runs; ``job.skipped_paused``.
4. **Gate** (``ToolCore.call``). ``Allow``: the executor runs it. ``NeedsApproval``:
   ``prompt_approvers`` (as
   the local runner: unreachable approvers skipped, none reachable fails, a transient error
   retries), then ``Wait("approval", wake_at=expires_at)``; an unanswered prompt expires
   and the next step gets ``Deny``. ``Deny``/``DryRun``: not run.
5. **Finish**, in one transaction: ``record_run`` (``ok`` / ``failed`` / ``ambiguous``),
   the run's redacted events (``scheduler.persist_events``; the job's first successful run
   after install stores them as baseline, never notified), the audit row (``job.run``,
   ``job.failed``, ``job.ambiguous``, ``job.killed``, ``job.denied``, ``job.dry_run``,
   ``job.skipped_paused``) and ``checkpoint.finished``.
6. **Reports** (code-built, sanitized) to every approver of the extension (the gate's
   ``approvers``: the owner first); an approver the channel refuses is skipped:
   - success: ``example/cleanup: done`` (plus ``: <summary>`` from the result, ≤ 120 chars),
     dedupe ``jobreport:<task id>:<recipient>``;
   - definite failure, ambiguous run, refusal or expiry:
     ``example/cleanup failed: <reason>`` / ``example/cleanup: not done (<reason>)``, at
     most once per job per local day (dedupe ``jobfail:<ext>/<id>:<date>:<recipient>``);
   - dry run, paused, and a run the kill switch killed (``job.killed``): none.

   A refused send is skipped; any other channel error is a retryable ``Fail`` (the dedupe
   keys stop doubles).

**Limit.** The executor commits the result before step 5. A crash in between replays the
result (no re-run) but loses that run's events; extensions whose events derive from
current state (``github watch``) re-emit them on the next run.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from resonant.executor import ToolResult
from resonant.extensions.notify import take_baseline
from resonant.extensions.scheduler import (
    JOB_RUNNER,
    job_checkpoint,
    job_spec_name,
    persist_events,
    read_paused,
)
from resonant.gate import Decision, Deny, Gate, Intent, NeedsApproval
from resonant.gate.codes import clean_external
from resonant.gate.policy import APPROVAL_EXPIRED, APPROVAL_USED, NOT_APPROVED
from resonant.gateway.channel import Channel, ChannelError, ChannelRefused
from resonant.loop.types import Done, Fail, StepContext, StepOutcome, Task, Wait
from resonant.observability.spans import span
from resonant.runners.approve import prompt_approvers
from resonant.store import extensions as store
from resonant.store.audit import audit
from resonant.store.db import transaction, utcnow
from resonant.store.tasks import save_progress
from resonant.toolcore import Asked, CallScope, Refused, ToolCore
from resonant.tools import ToolRegistry
from resonant_sdk import Event
from resonant_sdk.manifest import JOB_TIMEOUT_MAX_S

SUMMARY_MAX = 120
STEP_SLACK_S = 60.0

Recipients = Callable[[str], Sequence[str]]


class JobExecutor(Protocol):
    async def execute(
        self, intent: Intent, decision: Decision, *, step_no: int, call_index: int = 0
    ) -> ToolResult: ...

    def recorded(self, key: str) -> ToolResult | None: ...


_DENY_WORDS = {
    NOT_APPROVED: "not approved",
    APPROVAL_EXPIRED: "approval expired",
    APPROVAL_USED: "approval already used",
}


def _clip(text: str, limit: int = SUMMARY_MAX) -> str:
    flat = clean_external(text)
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def done_text(label: str, data: dict[str, Any] | None) -> str:
    """``example/cleanup: done``, plus ``: <summary>`` when the result has one."""
    summary: object = (data or {}).get("summary")
    if isinstance(summary, str) and (clean := _clip(summary)):
        return f"{label}: done: {clean}"
    return f"{label}: done"


def failed_text(label: str, error: str | None) -> str:
    """``example/cleanup failed: <reason>`` (the exception class prefix dropped)."""
    reason = (error or "unknown error").split(": ", 1)[-1]
    return f"{label} failed: {_clip(reason)}"


def _refusal_reason(refused: Refused) -> str:
    """The ``denied`` reason for a job's refusal: the gate's, ``approval already used`` (the
    executor refused the token), else ``refused``."""
    if refused.why == "not_allowed":
        return APPROVAL_USED
    if isinstance(refused.decision, Deny):
        return refused.decision.reason
    return "refused"


class JobRunner:
    """``runner="job"``. ``recipients(ext)`` is the gate's approvers (owner first)."""

    name = JOB_RUNNER
    uses_claude_slot = False

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        gate: Gate,
        executor: JobExecutor,
        registry: ToolRegistry,
        channel: Channel,
        recipients: Recipients,
        timezone: str = "UTC",
        now: Callable[[], datetime] = utcnow,
        max_step_s: float | None = JOB_TIMEOUT_MAX_S + STEP_SLACK_S,
    ) -> None:
        self.conn = conn
        self.gate = gate
        self.executor = executor
        self.registry = registry
        self.core = ToolCore(conn, registry=registry, gate=gate, executor=executor)
        self.channel = channel
        self.recipients = recipients
        self.tz = ZoneInfo(timezone)
        self.now = now
        self.max_step_s = max_step_s

    async def step(self, task: Task, events: list[Event], ctx: StepContext) -> StepOutcome:
        parsed = job_checkpoint(task.checkpoint)
        if parsed is None or task.principal is None:
            return Fail("job checkpoint is malformed")
        ext, job_id, due_at, _late = parsed
        label = f"{ext}/{job_id}"
        cp = task.checkpoint
        if isinstance(cp.get("finished"), dict):
            return await self._report(task, ext, label, cast(dict[str, Any], cp["finished"]))
        spec = self.registry.get(ext, job_spec_name(job_id))
        if spec is None:
            return await self._done(task, ext, label, "failed", error="job is not installed")
        args: dict[str, Any] = {"due_at": due_at}
        # Step 0, call 0: one key per task. No pin (nothing to resume but this same call),
        # no spans of the tool path's own (``runner.job`` is this runner's).
        scope = CallScope(
            task.id, task.principal, task.trace_id, 0, 0, "scheduled", check_schema=False
        )
        with span(self.conn, "runner.job", trace_id=task.trace_id, task_id=task.id) as s:
            result = self.core.recorded(spec, args, scope)
            s.set(recorded=result is not None)
            if result is None:
                if read_paused(self.conn):
                    s.set(decision="paused")
                    return await self._done(task, ext, label, "skipped_paused")
                called = await self.core.call(spec, args, scope)
                if called.decision is not None:
                    s.set(decision=type(called.decision).__name__)
                if isinstance(called, Asked):
                    return await self._ask(task, called.decision)
                if isinstance(called, Refused):
                    if called.why == "dry_run":
                        return await self._done(task, ext, label, "dry_run")
                    reason = _refusal_reason(called)
                    return await self._done(task, ext, label, "denied", error=reason)
                assert called.result is not None  # unpinned: ran (ok, failed or ambiguous)
                result = called.result
            outcome = _outcome(result)
            s.set(outcome=outcome)
        return await self._done(task, ext, label, outcome, result=result)

    async def _ask(self, task: Task, decision: NeedsApproval) -> Wait | Fail:
        sent = await prompt_approvers(
            self.conn, self.channel, decision, task_id=task.id, now=self.now
        )
        if sent.retry:
            return Fail(f"approval prompt failed: {sent.error}", retryable=True)
        if sent.none_reachable:
            return Fail("approval prompt refused: no approver is reachable")
        return Wait("approval", checkpoint=task.checkpoint, wake_at=decision.expires_at)

    async def _done(
        self,
        task: Task,
        ext: str,
        label: str,
        outcome: str,
        *,
        result: ToolResult | None = None,
        error: str | None = None,
    ) -> StepOutcome:
        finished = self._finish(task, ext, label, outcome, result=result, error=error)
        if isinstance(finished, Fail):
            return finished
        return await self._report(task, ext, label, finished)

    def _finish(
        self,
        task: Task,
        ext: str,
        label: str,
        outcome: str,
        *,
        result: ToolResult | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | Fail:
        """Record the run, its events and audit row, and ``checkpoint.finished``, in one
        transaction. Returns ``finished`` (what to report)."""
        parsed = job_checkpoint(task.checkpoint)
        assert parsed is not None
        _, job_id, due_at, late = parsed
        error = error if error is not None else (None if result is None else result.error)
        report: str | None = None
        daily = False
        match outcome:
            case "ok":
                report = done_text(label, None if result is None else result.data)
            case "failed" | "ambiguous":
                report, daily = failed_text(label, error), True
            case "denied":
                words = _DENY_WORDS.get(error or "", _clip(error or "refused"))
                report, daily = f"{label}: not done ({words})", True
            case _:
                pass
        finished: dict[str, Any] = {"outcome": outcome, "report": report, "daily": daily}
        now = self.now()
        with transaction(self.conn):
            if not save_progress(
                self.conn,
                task.id,
                checkpoint={**task.checkpoint, "finished": finished},
                claude_session_id=None,
                worktree_path=None,
            ):
                return Fail("task is no longer running")
            if outcome in ("ok", "failed", "ambiguous"):
                store.record_run(
                    self.conn, ext, job_id, at=now, outcome=outcome, ok=outcome == "ok"
                )
            n = 0
            if result is not None and result.ok:
                first = take_baseline(self.conn, ext, job_id)
                n = persist_events(
                    self.conn, ext, result.events, trace_id=task.trace_id, baseline=first
                )
            action = {"ok": "job.run", "dry_run": "job.dry_run"}.get(outcome, f"job.{outcome}")
            audit(
                self.conn,
                action,
                task_id=task.id,
                extension=ext,
                tool=job_spec_name(job_id),
                trace_id=task.trace_id,
                job=label,
                due=due_at,
                late=late,
                error=error,
                events=n,
                replayed=bool(result is not None and result.replayed),
                idempotency_key=None if result is None else result.idempotency_key,
            )
        return finished

    async def _report(
        self, task: Task, ext: str, label: str, finished: dict[str, Any]
    ) -> StepOutcome:
        outcome = str(finished.get("outcome"))
        text: object = finished.get("report")
        if isinstance(text, str):
            due: object = task.checkpoint.get("due_at")
            day = self._local_date(due if isinstance(due, str) else None)
            base = f"jobfail:{label}:{day}" if finished.get("daily") else f"jobreport:{task.id}"
            for recipient in self.recipients(ext) or ():
                try:
                    await self.channel.send(recipient, text, dedupe_key=f"{base}:{recipient}")
                except ChannelRefused:
                    continue
                except ChannelError as e:
                    return Fail(f"job report failed: {e}", retryable=True)
        return Done({"outcome": outcome, "reported": isinstance(text, str)})

    def _local_date(self, due_at: str | None) -> str:
        try:
            at = datetime.fromisoformat(due_at) if due_at else self.now()
        except ValueError:
            at = self.now()
        return at.astimezone(self.tz).date().isoformat()


def _outcome(result: ToolResult) -> str:
    """``killed`` is the kill switch (``ExtAmbiguous: killed``): audited, not reported and
    not counted as a failure."""
    if result.ok:
        return "ok"
    if (result.error or "").endswith("ExtAmbiguous: killed"):
        return "killed"
    return "ambiguous" if result.ambiguous else "failed"

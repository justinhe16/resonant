"""Sending approval prompts to every approver (D8). Used by the local runner, and later by
the job runner.

``prompt_approvers`` sends one ``ApprovalPrompt`` to each of ``NeedsApproval.approvers`` in
order and records each sent ref on the approval with its send time (and the prompt as a
system text the brains see, PER-268):
- A ``ChannelRefused`` approver (no identity on the channel) is skipped and audited
  ``approval.unreachable``. The others still get the prompt.
- Any other ``ChannelError`` is transient: the rest are still tried, and the result carries
  the first such error so the caller can retry. Re-prompting is idempotent, because the
  channel dedupes by ``approval_dedupe_key``.
- ``reached`` empty with no transient error means no approver is reachable: the caller
  fails for good.

``remind_due`` (PER-261, the ``core/remind`` job, every minute) sends each prompt one
reminder at half its TTL, to whoever got it: ``[A7] still waiting, expires in 15 min``. A
batch is one prompt (its code, timed from when it was opened). Deduped per approval and
approver (``approval:<id>:remind:<principal>``), recorded on the approvals (``reminded_at``,
once the send is settled: sent, or refused for good) and as a system text the brains see. It
carries the code and the time only, never args, so it never taints a turn. It replaces the
pulse's ``core/approval/<code>`` re-mention.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from resonant.gate import ApprovalBatch, NeedsApproval
from resonant.gateway.channel import (
    ApprovalPrompt,
    Channel,
    ChannelError,
    ChannelRefused,
    approval_dedupe_key,
)
from resonant.store import approvals as ap
from resonant.store import system_texts
from resonant.store.audit import audit
from resonant.store.db import atomic, transaction, utcnow
from resonant.store.outbound import reminder_key
from resonant.store.tasks import get_task

if TYPE_CHECKING:
    from resonant.executor import CallContext
    from resonant.extensions.scheduler import CoreDeps, CoreJob

REMIND_JOB_ID = "remind"
REMIND_SCHEDULE = "* * * * *"
REMIND_CATCHUP = timedelta(minutes=2)


@dataclass(frozen=True)
class PromptResult:
    reached: tuple[str, ...]
    unreachable: tuple[str, ...]
    error: ChannelError | None = None  # the first transient failure, if any

    @property
    def retry(self) -> bool:
        return self.error is not None

    @property
    def none_reachable(self) -> bool:
        return self.error is None and not self.reached


def approval_prompt(decision: NeedsApproval) -> ApprovalPrompt:
    return ApprovalPrompt(
        approval_id=decision.approval_id,
        code=decision.code,
        summary=decision.summary,
        effect=decision.effect.value,
        level=decision.level.value,
        expires_at=decision.expires_at,
        require_code=decision.require_code,
        always=decision.allow_always,
        phrased=decision.phrased,
    )


def batch_prompt(batch: ApprovalBatch) -> ApprovalPrompt:
    """BA-1: one prompt for a batch (deduped per approver by the batch's id)."""
    assert batch.expires_at is not None
    return ApprovalPrompt(
        approval_id=batch.batch_id,
        code=batch.code,
        summary=f"{len(batch.lines)} changes",
        effect=batch.effect.value,
        level=batch.level.value,
        expires_at=batch.expires_at,
        require_code=batch.require_code,
        lines=batch.lines,
        destructive=batch.destructive,
    )


async def prompt_approvers(
    conn: sqlite3.Connection,
    channel: Channel,
    decision: NeedsApproval,
    *,
    task_id: str,
    now: Callable[[], datetime],
) -> PromptResult:
    """Prompt every approver; see the module docstring for the failure rules."""
    return await _prompt(
        conn, channel, approval_prompt(decision), decision.approvers, (decision.approval_id,),
        task_id=task_id, now=now,
    )  # fmt: skip


async def prompt_batch(
    conn: sqlite3.Connection,
    channel: Channel,
    batch: ApprovalBatch,
    *,
    now: Callable[[], datetime],
) -> PromptResult:
    """BA-1: send a batch's one prompt to its approver (the owner), recording the sent ref on
    every approval in it (the sent-before rule reads each). Same failure rules."""
    return await _prompt(
        conn, channel, batch_prompt(batch), batch.approvers, batch.approval_ids,
        task_id=batch.task_id, now=now,
    )  # fmt: skip


async def _prompt(
    conn: sqlite3.Connection,
    channel: Channel,
    prompt: ApprovalPrompt,
    approvers: Sequence[str],
    approval_ids: Sequence[str],
    *,
    task_id: str,
    now: Callable[[], datetime],
) -> PromptResult:
    reached: list[str] = []
    unreachable: list[str] = []
    error: ChannelError | None = None
    for approver in approvers:
        try:
            ref = await channel.request_approval(approver, prompt)
        except ChannelRefused as e:
            unreachable.append(approver)
            with transaction(conn):
                audit(
                    conn,
                    "approval.unreachable",
                    task_id=task_id,
                    requested_by=approver,
                    approval_id=prompt.approval_id,
                    channel=channel.name,
                    error=type(e).__name__,
                )
            continue
        except ChannelError as e:
            error = error or e
            continue
        reached.append(approver)
        at = now()
        with transaction(conn):
            for approval_id in approval_ids:
                ap.add_channel_ref(conn, approval_id, ref=ref.ref, at=at, principal=approver)
        # PER-268: the brains see what was asked ("approve it?" is the prompt's job); a
        # tainted task's args were chosen after outside content, so only a fixed line
        key = approval_dedupe_key(prompt.approval_id, approver)
        task = get_task(conn, task_id) if task_id else None
        outside = task is not None and system_texts.tainted(task.checkpoint)
        system_texts.record(conn, approver, "approval", prompt.text(), ref=key, task_id=task_id,
                            now=at, outside=outside)  # fmt: skip
    return PromptResult(tuple(reached), tuple(unreachable), error)


def reminder_text(code: str, left: timedelta) -> str:
    """``[A7] still waiting, expires in 15 min`` (whole minutes, at least 1)."""
    minutes = max(1, round(left.total_seconds() / 60))
    return f"[{code}] still waiting, expires in {minutes} min"


async def remind_due(conn: sqlite3.Connection, channel: Channel, now: datetime) -> int:
    """Send every due expiry reminder (``store.approvals.reminders_due``) once; how many
    went out. A prompt is marked reminded once each of its approvers got the reminder or is
    unreachable for good (``ChannelRefused``); after a transient failure it is tried again on
    the next run (the ones already sent are deduped)."""
    with atomic(conn):
        ap.expire_due(conn, now)
        due = ap.reminders_due(conn, now)
    sent = 0
    for r in due:
        settled = True
        text = reminder_text(r.code, r.expires_at - now)
        for principal in r.to:
            key = reminder_key(r.approval_ids[0], principal)
            try:
                await channel.send(principal, text, dedupe_key=key)
            except ChannelRefused:
                continue
            except ChannelError:
                settled = False
                continue
            sent += 1
            system_texts.record(conn, principal, "approval", text, ref=key, task_id=r.task_id,
                                now=now)  # fmt: skip
        if settled:
            with atomic(conn):
                ap.mark_reminded(conn, r.approval_ids, now)
                audit(conn, "approval.reminded", task_id=r.task_id, code=r.code,
                      approval_ids=list(r.approval_ids), to=list(r.to),
                      expires_at=r.expires_at.isoformat())  # fmt: skip
    return sent


def remind_job(deps: CoreDeps, *, clock: Callable[[], datetime] = utcnow) -> CoreJob | None:
    """The ``CORE_JOBS`` factory for ``core/remind``. None with no channel or no owner."""
    from resonant.extensions.scheduler import CoreJob

    channel = deps.channel
    if channel is None or deps.owner is None:
        return None
    conn = deps.conn

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        del args, ctx
        return {"reminded": await remind_due(conn, channel, clock())}

    return CoreJob(id=REMIND_JOB_ID, schedule=REMIND_SCHEDULE, run=run, catchup=REMIND_CATCHUP,
                   timeout_s=30.0)  # fmt: skip

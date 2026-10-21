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
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

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
from resonant.store.db import transaction
from resonant.store.tasks import get_task


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

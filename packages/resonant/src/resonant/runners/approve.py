"""Sending approval prompts to every approver (D8). Used by the local runner, and later by
the job runner.

``prompt_approvers`` sends one ``ApprovalPrompt`` to each of ``NeedsApproval.approvers`` in
order and records each sent ref on the approval with its send time:
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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from resonant.gate import NeedsApproval
from resonant.gateway.channel import ApprovalPrompt, Channel, ChannelError, ChannelRefused
from resonant.store import approvals as ap
from resonant.store.audit import audit
from resonant.store.db import transaction


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
    prompt = approval_prompt(decision)
    reached: list[str] = []
    unreachable: list[str] = []
    error: ChannelError | None = None
    for approver in decision.approvers:
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
                    approval_id=decision.approval_id,
                    channel=channel.name,
                    error=type(e).__name__,
                )
            continue
        except ChannelError as e:
            error = error or e
            continue
        reached.append(approver)
        with transaction(conn):
            ap.add_channel_ref(
                conn, decision.approval_id, ref=ref.ref, at=now(), principal=approver
            )
    return PromptResult(tuple(reached), tuple(unreachable), error)

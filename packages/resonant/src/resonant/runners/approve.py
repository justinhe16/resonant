"""Sending approval prompts to every approver (D8). Used by the local runner, and later by
the job runner.

``prompt_approvers`` sends one ``ApprovalPrompt`` to each of ``NeedsApproval.approvers`` in
order and records each sent ref on the approval with its send time (and the prompt as a
system text the brains see, PER-268):
- A ``ChannelRefused`` approver (no identity on the channel) is skipped and audited
  ``approval.unreachable``. The others still get the prompt.
- Any other ``ChannelError`` is transient: the rest are still tried. PER-319: when at least
  one copy was delivered, each such failure is audited ``approval.send_failed`` (approver,
  error) and skipped (``PromptResult.failed``): one approver's bad DM (Slack's Messages tab
  off) doesn't fail the ask, which waits for an answer from those who got it. Only when no
  copy was delivered does the result carry the first such error so the caller can retry
  (and, once the retries are spent, the asking conversation hears ``Couldn't finish that:
  …``, ``runners.status``). Re-prompting is idempotent, because the channel dedupes by
  ``approval_dedupe_key`` (a skipped approver is tried again on the next pending step).
- ``reached`` empty with no transient error means no approver is reachable: the caller
  fails for good.
- ``PromptResult.here`` (PER-319): the asking conversation got a copy (the shared thread's
  one copy, or the asker's own prompt). When it didn't, the caller says who it waits on
  (``runners.status.waiting_line``).
- PER-292: the prompt to the asking task's own principal goes to that task's thread
  (``thread_of``); every other approver gets it in their own conversation. That stays so
  for a thread nobody else sees (a Slack DM) and with no thread (iMessage, a scheduled job).
- PER-320: in a shared thread (``shared_thread``: a team channel's), **one** copy goes
  there, addressed to the requester, tagging the approvers (``ApprovalPrompt.mention``) when
  the requester isn't one, and it is recorded for every approver (with the thread): each
  may answer it there (a button or ``yes <code>``), and none gets a DM. A requester the
  channel refuses is skipped (the first approver it reaches gets it, in the thread); if the
  adapter can't post in the thread any more, the rest get their own, as before.
- PER-318: a prompt that offers ``yes always`` offers it to ``NeedsApproval.offer_to`` (the
  owner) alone: every other approver gets it as a plain yes / no, the summary's code-built
  ``yes always`` note cut (``gate.codes.without_offer``). Only the owner widens trust.
- PER-294: ``in_thread`` overrides that pair, ``(requester, thread)``: a worker's trust
  check goes to the run's thread, addressed to the run's requester (``trust.checks``); a
  worker's ``resonant request`` too (PER-296, ``runners.worker_requests``).

``remind_due`` (PER-261, the ``core/remind`` job, every minute) sends each prompt one
reminder at half its TTL, to whoever got it: ``[A7] still waiting, expires in 15 min``
(PER-320: the approvers who got the one copy in a shared thread get one reminder there). A
batch is one prompt (its code, timed from when it was opened). Deduped per approval and
approver (``approval:<id>:remind:<principal>``), recorded on the approvals (``reminded_at``,
once the send is settled: sent, or refused for good) and as a system text the brains see. It
carries the code and the time only, never args, so it never taints a turn. It replaces the
pulse's ``core/approval/<code>`` re-mention.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from resonant.gate import ApprovalBatch, NeedsApproval
from resonant.gate.codes import without_offer
from resonant.gateway.channel import (
    ApprovalPrompt,
    Channel,
    ChannelError,
    ChannelRefused,
    MessageRef,
    approval_dedupe_key,
    shared_thread,
    thread_of,
)
from resonant.redact import redact_secrets
from resonant.store import approvals as ap
from resonant.store import system_texts
from resonant.store.audit import audit
from resonant.store.db import atomic, transaction, utcnow
from resonant.store.outbound import reminder_key
from resonant.store.tasks import get_task

if TYPE_CHECKING:
    from resonant.executor import CallContext
    from resonant.extensions.scheduler import CoreDeps, CoreJob
    from resonant.loop.types import Task

REMIND_JOB_ID = "remind"
REMIND_SCHEDULE = "* * * * *"
REMIND_CATCHUP = timedelta(minutes=2)


@dataclass(frozen=True)
class PromptResult:
    reached: tuple[str, ...]
    unreachable: tuple[str, ...]
    error: ChannelError | None = None  # the first transient failure, when nobody got a copy
    # PER-319: approvers whose copy failed (not refused) and was skipped: another got one
    failed: tuple[str, ...] = ()
    here: bool = False  # PER-319: the asking conversation got a copy

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
        detail=decision.detail,
        plain=decision.plain,
    )


def batch_prompt(
    batch: ApprovalBatch, plains: Mapping[str, str | None] | None = None
) -> ApprovalPrompt:
    """BA-1: one prompt for a batch (deduped per approver by the batch's id). ``plains``
    (PER-324): each approval's plain sentence by approval id."""
    assert batch.expires_at is not None
    by_id = plains or {}
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
        plains=tuple(by_id.get(i) for i in batch.approval_ids) if by_id else (),
    )


async def prompt_approvers(
    conn: sqlite3.Connection,
    channel: Channel,
    decision: NeedsApproval,
    *,
    task_id: str,
    now: Callable[[], datetime],
    in_thread: tuple[str, MessageRef] | None = None,
    summary: str | None = None,
) -> PromptResult:
    """Prompt every approver; see the module docstring for the failure rules. ``in_thread``
    (PER-294): ``(requester, thread)``, who asked and where (default: the asking task's
    principal, in its thread); PER-320: a shared thread gets the one copy, addressed to
    them. ``summary`` (PER-296, code-built) replaces the gate's in the prompt text (a
    worker's request says which run asks)."""
    prompt = approval_prompt(decision)
    if summary is not None:
        prompt = replace(prompt, summary=summary, phrased=False)
    others = None
    if prompt.always:  # PER-318: the offer is the owner's alone
        others = replace(prompt, always=False, summary=without_offer(prompt.summary))
    return await _prompt(
        conn, channel, prompt, decision.approvers, (decision.approval_id,),
        task_id=task_id, now=now, in_thread=in_thread,
        others=None if others is None else (decision.offer_to, others),
    )  # fmt: skip


async def prompt_batch(
    conn: sqlite3.Connection,
    channel: Channel,
    batch: ApprovalBatch,
    *,
    now: Callable[[], datetime],
    plains: Mapping[str, str | None] | None = None,
) -> PromptResult:
    """BA-1: send a batch's one prompt to its approver (the owner), recording the sent ref on
    every approval in it (the sent-before rule reads each). Same failure rules. ``plains``
    (PER-324): each change's plain sentence by approval id."""
    return await _prompt(
        conn, channel, batch_prompt(batch, plains), batch.approvers, batch.approval_ids,
        task_id=batch.task_id, now=now,
    )  # fmt: skip


async def prompt_row(
    conn: sqlite3.Connection, channel: Channel, row: ap.Approval, now: datetime
) -> PromptResult:
    """PER-275: send a stored, task-less approval (an MCP re-pin question) to its approvers.
    Same failure rules as ``prompt_approvers``; the channel dedupes by ``approval_dedupe_key``."""
    assert row.code is not None
    prompt = ApprovalPrompt(
        approval_id=row.id,
        code=row.code,
        summary=row.summary,
        effect=row.effect.value,
        level=row.level.value,
        expires_at=row.expires_at,
        require_code=row.require_code,
        phrased=row.phrased,
    )
    return await _prompt(conn, channel, prompt, row.approvers, (row.id,), task_id=None,
                         now=lambda: now)  # fmt: skip


async def _prompt(
    conn: sqlite3.Connection,
    channel: Channel,
    prompt: ApprovalPrompt,
    approvers: Sequence[str],
    approval_ids: Sequence[str],
    *,
    task_id: str | None,
    now: Callable[[], datetime],
    in_thread: tuple[str, MessageRef] | None = None,
    others: tuple[str | None, ApprovalPrompt] | None = None,
) -> PromptResult:
    """``others`` (PER-318): ``(offer_to, plain)``: everyone but ``offer_to`` gets ``plain``.
    PER-320: a shared thread gets the one copy (``_prompt_shared``); else, or when that copy
    couldn't stay in the thread, each approver gets their own."""
    asking = get_task(conn, task_id) if task_id else None
    thread = thread_of(asking, to=None if asking is None else asking.principal)
    whose = None if asking is None else asking.principal
    if in_thread is not None:
        whose, thread = in_thread
    sent = _Sent(conn, channel, prompt, approval_ids, task_id=task_id, now=now, asking=asking)

    def variant(approver: str) -> ApprovalPrompt:
        if others is not None and approver != others[0]:
            return others[1]
        return prompt

    sent.whose = whose
    if whose is not None and shared_thread(thread):
        assert thread is not None
        if await _prompt_shared(sent, approvers, whose, thread, variant):
            return sent.result()
    for approver in approvers:
        if approver in sent.reached or approver in sent.unreachable:
            continue  # the shared copy went to them, or the channel refused them
        mine = thread is not None and approver == whose
        theirs = variant(approver)
        await sent.send(approver, replace(theirs, thread=thread) if mine else theirs)
    return sent.result()


async def _prompt_shared(
    sent: _Sent,
    approvers: Sequence[str],
    whose: str,
    thread: MessageRef,
    variant: Callable[[str], ApprovalPrompt],
) -> bool:
    """PER-320: one copy in the shared ``thread``, addressed to ``whose`` (who asked), else
    to the first approver the channel can reach. It tags the approvers when ``whose`` isn't
    one, and is recorded for every approver the channel didn't refuse: each sees it in the
    thread and may answer it there (a button, or ``yes <code>``). True when that settled the
    prompt (or a transient error: the caller retries, deduped); False when nobody could be
    addressed, or the channel couldn't post in the thread (no longer one it posts in: the
    copy went to the addressee's own conversation): the rest then get their own, as before."""
    refused: set[str] = set()
    for addressee in dict.fromkeys([whose, *approvers]):
        mention = () if whose in approvers else tuple(approvers)  # incl. a fallback addressee
        theirs = replace(variant(addressee), thread=thread, mention=mention)
        ref = await sent.send(addressee, theirs, record=False, approver=addressee in approvers)
        if ref is None:
            if sent.error is not None:
                return True
            refused.add(addressee)
            continue
        if ref.thread is None:
            if addressee in approvers:
                sent.record(addressee, ref, theirs)
            return False
        for approver in approvers:
            if approver not in refused:
                sent.record(approver, ref, theirs, shared=thread)
        sent.here = True
        return True
    return False


class _Sent:
    """One prompt's sends and records (the failure rules in the module docstring)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        channel: Channel,
        prompt: ApprovalPrompt,
        approval_ids: Sequence[str],
        *,
        task_id: str | None,
        now: Callable[[], datetime],
        asking: Task | None,
    ) -> None:
        self.conn, self.channel, self.prompt = conn, channel, prompt
        self.approval_ids, self.task_id, self.now = approval_ids, task_id, now
        self.outside = asking is not None and system_texts.tainted(asking.checkpoint)
        self.reached: list[str] = []
        self.unreachable: list[str] = []
        self.error: ChannelError | None = None
        self.failed: dict[str, ChannelError] = {}  # transient failures, by approver
        self.whose: str | None = None  # who asked (their conversation is the asking one)
        self.here = False  # the shared thread's one copy went out

    def result(self) -> PromptResult:
        """PER-319: with a copy delivered, a transient failure is audited and skipped."""
        here = self.here or (self.whose is not None and self.whose in self.reached)
        if not self.reached or not self.failed:
            return PromptResult(tuple(self.reached), tuple(self.unreachable), self.error,
                                here=here)  # fmt: skip
        with transaction(self.conn):
            for approver, e in self.failed.items():
                audit(
                    self.conn,
                    "approval.send_failed",
                    task_id=self.task_id,
                    approval_id=self.prompt.approval_id,
                    channel=self.channel.name,
                    approver=approver,
                    error=_error_text(e),
                )
        return PromptResult(tuple(self.reached), tuple(self.unreachable), None,
                            failed=tuple(self.failed), here=here)  # fmt: skip

    async def send(
        self,
        principal: str,
        prompt: ApprovalPrompt,
        *,
        record: bool = True,
        approver: bool = True,
    ) -> MessageRef | None:
        """``request_approval`` to ``principal``; the ref, or None (refused: audited, and
        ``unreachable`` when ``approver``; or transient: ``error``). ``record``: record it
        for ``principal`` (:meth:`record`) at once."""
        conn = self.conn
        try:
            ref = await self.channel.request_approval(principal, prompt)
        except ChannelRefused as e:
            if approver:
                self.unreachable.append(principal)
            with transaction(conn):
                audit(
                    conn,
                    "approval.unreachable",
                    task_id=self.task_id,
                    requested_by=principal,
                    approval_id=self.prompt.approval_id,
                    channel=self.channel.name,
                    error=type(e).__name__,
                )
            return None
        except ChannelError as e:
            self.error = self.error or e
            self.failed.setdefault(principal, e)
            return None
        if record:
            self.record(principal, ref, prompt)
        return ref

    def record(
        self,
        principal: str,
        ref: MessageRef,
        prompt: ApprovalPrompt,
        *,
        shared: MessageRef | None = None,
    ) -> None:
        """The sent ref on every approval (the sent-before rule reads it), and the prompt as
        a system text the brains see (PER-268)."""
        conn = self.conn
        self.reached.append(principal)
        at = self.now()
        with transaction(conn):
            for approval_id in self.approval_ids:
                ap.add_channel_ref(
                    conn, approval_id, ref=ref.ref, at=at, principal=principal,
                    channel=None if shared is None else shared.channel,
                    thread=None if shared is None else shared.thread,
                )  # fmt: skip
        # PER-268: the brains see what was asked ("approve it?" is the prompt's job); a
        # tainted task's args were chosen after outside content, so only a fixed line
        key = approval_dedupe_key(self.prompt.approval_id, principal)
        system_texts.record(conn, principal, "approval", prompt.text(), ref=key,
                            task_id=self.task_id, now=at, outside=self.outside)  # fmt: skip


def _error_text(e: ChannelError) -> str:
    """A channel error for the audit row: its type and body-free reason, redacted, clipped."""
    return redact_secrets(f"{type(e).__name__}: {e}")[:200]


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
        head = ap.get_approval(conn, r.approval_ids[0])
        if head is None or head.status != "pending":
            continue  # answered while the earlier reminders went out: nothing waits
        for thread, group in _reminder_groups(head, r.to):
            for principal in group:
                key = reminder_key(r.approval_ids[0], principal)
                try:
                    if thread is None:
                        await channel.send(principal, text, dedupe_key=key)
                    else:
                        await channel.send(principal, text, thread=thread, dedupe_key=key)
                except ChannelRefused:
                    continue
                except ChannelError:
                    settled = False
                    if thread is not None:
                        break  # the next run tries the same one again (deduped)
                    continue
                sent += 1
                seen = group if thread is not None else (principal,)
                for who in seen:
                    system_texts.record(conn, who, "approval", text, ref=key,
                                        task_id=r.task_id, now=now)  # fmt: skip
                if thread is not None:
                    break  # one reminder in the shared thread, which they all see
        if settled:
            with atomic(conn):
                ap.mark_reminded(conn, r.approval_ids, now)
                audit(conn, "approval.reminded", task_id=r.task_id, code=r.code,
                      approval_ids=list(r.approval_ids), to=list(r.to),
                      expires_at=r.expires_at.isoformat())  # fmt: skip
    return sent


def _reminder_groups(
    head: ap.Approval, to: Sequence[str]
) -> list[tuple[MessageRef | None, tuple[str, ...]]]:
    """PER-320: who gets an approval's reminder, and where. The approvers who got the one
    copy in a shared thread get one reminder there (sent to the first of them the channel
    reaches); everyone else gets their own, as before."""
    shared: dict[MessageRef, list[str]] = {}
    own: list[tuple[MessageRef | None, tuple[str, ...]]] = []
    for principal in to:
        ref = next(
            (c for c in head.channel_refs if c.principal == principal and c.thread is not None),
            None,
        )
        if ref is None or ref.channel is None or ref.thread is None:
            own.append((None, (principal,)))
            continue
        shared.setdefault(MessageRef(ref.channel, ref.ref, ref.thread), []).append(principal)
    return [(t, tuple(g)) for t, g in shared.items()] + own


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

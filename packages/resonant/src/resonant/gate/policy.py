"""PolicyGate: the Phase 2 gate. It weighs the risk (registered level) against who chose
the action (``Intent.origin``).

Evaluation order:
1. Registration: the intent's spec must be the one registered for (extension, tool).
2. Permission: ``can(principal, "request", extension)``.
3. Unsafe args (control, bidi, zero-width characters): Deny, so what runs always matches
   what was shown.
4. dry_run: a tool that is neither an L0 read nor store-internal
   (``tools.is_store_internal``) becomes ``DryRun(would=<summary>)``.
5. Level x origin x autonomy (``rule``). ``scheduled`` is never explicit:

   | level | autonomy on, explicit | autonomy on, inferred / scheduled | autonomy off |
   |-------|-----------------------|-----------------------------------|--------------|
   | L0    | Allow                 | Allow                             | Allow        |
   | L3    | Allow                 | Allow                             | ask          |
   | L2    | Allow                 | ask                               | ask          |
   | L1    | ask with code         | ask with code                     | ask with code|

Kill switch: kv ``autonomy`` is read on every call, and anything but ``"on"`` (missing or
garbage) counts as off. ``evaluate`` never returns ``Veto`` yet.

Approvers (``approvers``): a builtin (``extension=None``) is owner-only. An extension tool's
approvers are the owner plus each principal its manifest names (``approvers_for``) that
holds ``approve`` for that extension in principals.yaml. The grant check runs here, so a
manifest can't grant itself approvers: a name without the grant, or unknown, is dropped
and logged.

Approvals, per (task, intent hash):
- "ask" reuses the pending approval if there is one, else inserts a new one (approvers as
  above; ``expires_at`` = now + ``approval_ttl_s``).
- Approved and unused: ``Allow(token, approval_id)``. The executor marks it used in the
  same transaction as its ``started`` row, so an approval runs at most once.
- Rejected, expired, or already used: Deny with ``NOT_APPROVED``, ``APPROVAL_EXPIRED`` or
  ``APPROVAL_USED``. A rejection also beats a later Allow for the same intent.

Text replies (``resolve_reply``): the actor must be an approver with ``approve``, the reply an
iMessage parser match, and the approval's prompt sent to the actor before the reply's
``occurred_at`` (the sent-before rule). A plain yes/no binds only when exactly one approval
is pending for the actor, now and when the reply was sent. ``require_code`` needs
``yes <code>``; a plain no may deny. Resolution is idempotent per reply event
(``resolved_event_id``).

Expiry is lazy: every entry point first runs ``expire_due``. Every ``evaluate`` writes one
``gate.evaluate`` audit row; every resolution attempt writes one ``approval.resolve`` row.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Literal

from resonant.gate.codes import new_code
from resonant.gate.summary import summarize, unsafe_args
from resonant.gate.types import (
    Allow,
    ApprovalBatch,
    ApprovalReply,
    ApprovalResolution,
    Decision,
    Deny,
    DryRun,
    Intent,
    NeedsApproval,
    Origin,
    PendingApproval,
    ReplyRefusal,
    ResolveResult,
)
from resonant.principals import Principals
from resonant.store import approvals as ap
from resonant.store.audit import audit
from resonant.store.db import atomic, kv_get, utcnow
from resonant.store.tasks import insert_event
from resonant.tools import ToolRegistry, is_store_internal
from resonant.tools.registry import ToolKey
from resonant_sdk import Effect, Event, Level

log = logging.getLogger(__name__)

# Deny reasons the runner maps to its templated replies.
NOT_APPROVED = "not approved"
APPROVAL_EXPIRED = "approval expired"
APPROVAL_USED = "approval already used"
NO_TASK = "approvals need a task"

# Channels whose text replies may bind approvals. The iMessage reader already drops SMS.
REPLY_CHANNELS = frozenset({"imessage"})

Rule = Literal["allow", "ask", "ask_code"]

# A code-built heading for a tool's approval summary (e.g. "delete all 4 notes"). It must
# never come from a model; ``summarize`` cleans it anyway.
SummaryAction = Callable[[Intent], str | None]

# The approver names an extension's manifest declares (``Manifest.approvers``). Untrusted:
# the gate keeps only names that hold the approve grant for that extension.
ApproversFor = Callable[[str], Sequence[str]]


def no_manifest_approvers(_extension: str) -> Sequence[str]:
    """The default until the extension host supplies manifests: nobody but the owner."""
    return ()


def rule(level: Level, origin: Origin, autonomy_on: bool) -> Rule:
    """The D1/D7 table: risk (level) x who chose it (origin) x the kill switch. Only
    ``explicit`` is consent; ``inferred`` and ``scheduled`` ask before L2."""
    if level is Level.L0:
        return "allow"
    if level is Level.L1:
        return "ask_code"
    if not autonomy_on:
        return "ask"
    if level is Level.L3 or origin == "explicit":
        return "allow"
    return "ask"


def autonomy_on(conn: sqlite3.Connection) -> bool:
    """The kill switch. Fails closed: a missing or unreadable value is off."""
    try:
        return kv_get(conn, "autonomy") == "on"
    except (json.JSONDecodeError, TypeError, ValueError):
        return False


class PolicyGate:
    def __init__(
        self,
        conn: sqlite3.Connection,
        principals: Principals,
        registry: ToolRegistry,
        *,
        dry_run: bool,
        approval_ttl_s: float,
        actions: Mapping[ToolKey, SummaryAction] | None = None,
        approvers_for: ApproversFor = no_manifest_approvers,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.conn = conn
        self.principals = principals
        self.registry = registry
        self.dry_run = dry_run
        self.ttl = timedelta(seconds=approval_ttl_s)
        self.actions = dict(actions or {})
        self.approvers_for = approvers_for
        self.clock = clock
        self._tokens: dict[str, str] = {}

    # --- evaluate ----------------------------------------------------------------------

    def evaluate(self, intent: Intent) -> Decision:
        now = self.clock()
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            autonomy = autonomy_on(self.conn)
            decision = self._decide(intent, now, autonomy)
            audit(
                self.conn,
                "gate.evaluate",
                task_id=intent.task_id,
                requested_by=intent.principal,
                extension=intent.extension,
                tool=intent.tool,
                effect=intent.effect.value,
                level=intent.level.value,
                intent_hash=intent.hash,
                decision=type(decision).__name__,
                trace_id=intent.trace_id,
                origin=intent.origin,
                autonomy="on" if autonomy else "off",
                dry_run=self.dry_run,
                approval_id=(
                    decision.approval_id if isinstance(decision, Allow | NeedsApproval) else None
                ),
                reason=decision.reason if isinstance(decision, Deny) else None,
                args=intent.args,
            )
        return decision

    def _decide(self, intent: Intent, now: datetime, autonomy: bool) -> Decision:
        if self.registry.get(intent.extension, intent.tool) != intent.spec:
            # Static reasons: the audit row already names the tool, extension and principal.
            return Deny("tool is not registered with this spec")
        if not self.principals.can(intent.principal, "request", intent.extension):
            return Deny("not permitted to request this tool")
        unsafe = unsafe_args(intent.args)
        if unsafe:
            # Never echo the offending keys or values: they are what's unsafe to show.
            return Deny(f"args contain unsafe characters ({len(unsafe)} found)")
        l0_read = intent.effect is Effect.READ and intent.level is Level.L0
        if self.dry_run and not l0_read and not is_store_internal(intent.spec):
            return DryRun(would=self._summary(intent))
        verdict = rule(intent.level, intent.origin, autonomy)
        if verdict == "allow" and intent.level is Level.L0:
            return self._allow(intent)
        if ap.was_rejected(self.conn, intent.task_id, intent.hash):
            return Deny(NOT_APPROVED)  # the owner's "no" holds for this intent
        last = ap.latest_for_intent(self.conn, intent.task_id, intent.hash)
        if verdict == "allow":
            return self._allow(intent)
        return self._ask(intent, now, last, require_code=verdict == "ask_code")

    def _summary(self, intent: Intent) -> str:
        """The code-built summary, with the tool's action heading if it has one."""
        action = self.actions.get((intent.extension, intent.tool))
        return summarize(intent, action=action(intent) if action else None)

    def approvers(self, extension: str | None) -> tuple[str, ...]:
        """Who may approve a call to this extension's tools: the owner first, then each
        manifest-named principal that holds ``approve`` for it. Builtins are owner-only."""
        owner = self.principals.owner
        if extension is None:
            return (owner,)
        named = self.approvers_for(extension)
        if isinstance(named, str):  # a bare name is a seam bug, never a list of letters
            raise TypeError("approvers_for must return a sequence of names, not a str")
        found = [owner]
        for name in named:
            if name in found:
                continue
            if not self.principals.can(name, "approve", extension):
                log.warning("dropping approver %r: no approve grant for %r", name, extension)
                continue
            found.append(name)
        return tuple(found)

    def _allow(self, intent: Intent, approval_id: str | None = None) -> Allow:
        token = secrets.token_urlsafe(16)
        self._tokens[token] = intent.hash
        return Allow(token=token, approval_id=approval_id)

    def _ask(
        self, intent: Intent, now: datetime, last: ap.Approval | None, *, require_code: bool
    ) -> Decision:
        approved = ap.approved_unused(self.conn, intent.task_id, intent.hash)
        if approved is not None:
            return self._allow(intent, approved.id)
        pending = ap.pending_for_intent(self.conn, intent.task_id, intent.hash, now)
        if pending is not None:
            return _needs(pending)
        if last is not None:
            return Deny(APPROVAL_USED if last.status == "approved" else APPROVAL_EXPIRED)
        task = self.conn.execute("SELECT 1 FROM tasks WHERE id = ?", (intent.task_id,))
        if task.fetchone() is None:
            return Deny(NO_TASK)  # e.g. the router's fast path: nothing could wait
        spec = ap.NewApproval(
            task_id=intent.task_id,
            intent_hash=intent.hash,
            tool=intent.tool,
            effect=intent.effect,
            level=intent.level,
            summary=self._summary(intent),
            approvers=self.approvers(intent.extension),
            code=new_code(ap.pending_codes(self.conn)),
            expires_at=now + self.ttl,
            require_code=require_code,
            extension=intent.extension,
            requested_by=intent.principal,
        )
        approval_id = ap.insert_approval(self.conn, spec, now)
        created = ap.get_approval(self.conn, approval_id)
        assert created is not None
        return _needs(created)

    def consume(self, token: str, intent_hash: str) -> bool:
        return self._tokens.pop(token, None) == intent_hash

    # --- approvals ---------------------------------------------------------------------

    def pending_for(self, principal: str) -> Sequence[PendingApproval]:
        now = self.clock()
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            rows = ap.pending_for(self.conn, principal, now)
        return tuple(
            PendingApproval(
                approval_id=a.id,
                code=a.code or "",
                task_id=a.task_id or "",
                summary=a.summary,
                effect=a.effect,
                level=a.level,
                require_code=a.require_code,
                expires_at=a.expires_at,
            )
            for a in rows
        )

    def resolve(self, resolution: ApprovalResolution) -> ResolveResult:
        """Resolve one approval by id (CLI or button) and wake its task, in one transaction."""
        now = self.clock()
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            approval = ap.get_approval(self.conn, resolution.approval_id)
            reason: str | None
            if approval is None or approval.task_id is None:
                reason = "not_found"
            elif resolution.actor not in approval.approvers or not self.principals.can(
                resolution.actor, "approve", approval.extension
            ):
                reason = "not an approver"
            else:
                reason = ap.resolve(
                    self.conn,
                    approval.id,
                    approved=resolution.approved,
                    decided_by=resolution.actor,
                    now=now,
                )
            # A non-approver can't tell an existing approval from a missing one.
            shown = "not_found" if reason == "not an approver" else reason
            result = ResolveResult(ok=False, reason=shown)
            if reason is None and approval is not None:
                done = ap.get_approval(self.conn, approval.id)
                assert done is not None
                result = ap.resolve_result([done])
                ap.wake_resolved(self.conn, result, now, principal=resolution.actor)
            audit(
                self.conn,
                "approval.resolve",
                task_id=approval.task_id if approval else None,
                requested_by=resolution.actor,
                approved_by=resolution.actor if result.ok else None,
                tool=approval.tool if approval else None,
                extension=approval.extension if approval else None,
                intent_hash=approval.intent_hash if approval else None,
                decision=("approved" if resolution.approved else "rejected")
                if result.ok
                else "refused",
                requested="approve" if resolution.approved else "deny",
                approval_id=resolution.approval_id,
                actor_identity=resolution.actor_identity,
                path=resolution.path,
                comments=resolution.comments,
                ok=result.ok,
                reason=reason,
            )
        return result

    def resolve_reply(self, reply: ApprovalReply) -> ResolveResult:
        """Bind a text reply to one approval, resolve it and wake its task, in one
        transaction. See ``ApprovalReply`` for the binding rules and ``ReplyRefusal`` for
        the closed set of refusal reasons.

        Idempotent per event: if ``reply.event_id`` already resolved an approval, this
        returns that result again with no new effect (no write, wake or audit).
        """
        now = self.clock()
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            earlier = ap.by_resolved_event(self.conn, reply.event_id)
            if earlier:
                if all(a.decided_by == reply.actor for a in earlier):
                    return ap.resolve_result(earlier)
                return ResolveResult(ok=False, reason="nothing_pending")
            target, reason = self._bind(reply, now)
            if target is not None:
                reason = ap.resolve(
                    self.conn,
                    target.id,
                    approved=reply.decision == "approve",
                    decided_by=reply.actor,
                    now=now,
                    event_id=reply.event_id,
                )
            result = ResolveResult(ok=False, reason=reason)
            if reason is None and target is not None:
                done = ap.get_approval(self.conn, target.id)
                assert done is not None
                result = ap.resolve_result([done])
                if reply.comments and reply.decision == "approve":
                    _add_comments(self.conn, done, reply)
                ap.wake_resolved(self.conn, result, now, principal=reply.actor)
            audit(
                self.conn,
                "approval.resolve",
                task_id=target.task_id if target else None,
                requested_by=reply.actor,
                approved_by=reply.actor if result.ok and reply.decision == "approve" else None,
                tool=target.tool if target else None,
                extension=target.extension if target else None,
                intent_hash=target.intent_hash if target else None,
                decision=("approved" if reply.decision == "approve" else "rejected")
                if result.ok
                else "refused",
                requested=reply.decision,
                approval_id=target.id if target else None,
                actor_identity=reply.actor_identity,
                channel=reply.channel,
                path=reply.path,
                code=reply.code,
                event_id=reply.event_id,
                occurred_at=reply.occurred_at.isoformat(),
                raw_text=reply.raw_text,
                comments=reply.comments,
                ok=result.ok,
                reason=reason,
            )
        return result

    def _bind(
        self, reply: ApprovalReply, now: datetime
    ) -> tuple[ap.Approval | None, ReplyRefusal | None]:
        """The approval this reply binds to, or why it binds none."""
        if (
            reply.channel not in REPLY_CHANNELS
            or reply.path != "parser"
            or reply.all_in_batch
            or reply.batch_id is not None
        ):
            return None, "unsupported"
        mine = [
            a for a in ap.pending_for(self.conn, reply.actor, now) if self._may_approve(reply, a)
        ]
        if not mine:
            return None, "nothing_pending"  # also what a non-approver always gets
        if reply.code is not None:
            code = reply.code.upper()
            target = next((a for a in mine if a.code == code), None)
            if target is None:
                return None, "unknown_code"
        elif len(mine) > 1:
            return None, "ambiguous"
        else:
            target = mine[0]
        if not _sent_before(target, reply):
            return None, "not_sent_before"
        if reply.code is None:
            # Judge a plain reply against what the actor had been shown when they sent it,
            # so an old reply re-routed later can't bind what has since become the only one.
            seen = [
                a.id
                for a in ap.open_at(self.conn, reply.actor, reply.occurred_at)
                if self._may_approve(reply, a) and _sent_before(a, reply)
            ]
            if seen != [target.id]:
                return None, "ambiguous"
            if target.require_code and reply.decision == "approve":
                return None, "code_required"
        return target, None

    def _may_approve(self, reply: ApprovalReply, a: ap.Approval) -> bool:
        return (
            a.task_id is not None
            and reply.actor in a.approvers
            and self.principals.can(reply.actor, "approve", a.extension)
        )

    def open_batch(self, principal: str, approval_ids: Sequence[str]) -> ApprovalBatch:
        raise NotImplementedError("batch approvals are deferred")


def _sent_before(a: ap.Approval, reply: ApprovalReply) -> bool:
    """The sent-before rule: the prompt reached the actor before the reply was sent."""
    sent = a.sent_at(reply.actor)
    return sent is not None and sent < reply.occurred_at


def _add_comments(conn: sqlite3.Connection, a: ap.Approval, reply: ApprovalReply) -> None:
    """``yes with comments: ...``: a ``human_input`` event for the task, at most once."""
    assert a.task_id is not None
    row = conn.execute("SELECT trace_id FROM tasks WHERE id = ?", (a.task_id,)).fetchone()
    assert row is not None  # resolve just checked the task is waiting or running
    insert_event(
        conn,
        Event(
            source="gate",
            type="human_input",
            task_id=a.task_id,
            principal=reply.actor,
            payload={"approval_id": a.id, "comments": reply.comments},
            dedupe_key=f"approval:{a.id}:comments",
            occurred_at=reply.occurred_at,
            trace_id=row["trace_id"],
        ),
    )


def _needs(a: ap.Approval) -> NeedsApproval:
    assert a.code is not None
    return NeedsApproval(
        approval_id=a.id,
        code=a.code,
        approvers=a.approvers,
        expires_at=a.expires_at,
        summary=a.summary,
        effect=a.effect,
        level=a.level,
        require_code=a.require_code,
    )

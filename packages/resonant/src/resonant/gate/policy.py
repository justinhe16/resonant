"""PolicyGate: the Phase 2 gate. It weighs the risk (registered level) against who chose
the action (``Intent.origin``).

Evaluation order:
1. Registration: the intent's spec must be the one registered for (extension, tool).
2. Permission: ``can(principal, "request", extension)``.
3. Unsafe args (control, bidi, zero-width characters): Deny, so what runs always matches
   what was shown.
4. dry_run: a tool that is neither an L0 read nor store-internal
   (``tools.is_store_internal``) becomes ``DryRun(would=<summary>)``.
5. Level x origin x autonomy (``rule``):

   | level | autonomy on, explicit | autonomy on, inferred | autonomy off |
   |-------|-----------------------|-----------------------|--------------|
   | L0    | Allow                 | Allow                 | Allow        |
   | L3    | Allow                 | Allow                 | ask          |
   | L2    | Allow                 | ask                   | ask          |
   | L1    | ask with code         | ask with code         | ask with code|

Kill switch: kv ``autonomy`` is read on every call, and anything but ``"on"`` (missing or
garbage) counts as off. ``evaluate`` never returns ``Veto`` in Phase 2.

Approvals, per (task, intent hash):
- "ask" reuses the pending approval if there is one, else inserts a new one (approvers:
  the owner; ``expires_at`` = now + ``approval_ttl_s``).
- Approved and unused: ``Allow(token, approval_id)``. The executor marks it used in the
  same transaction as its ``started`` row, so an approval runs at most once.
- Rejected, expired, or already used: Deny with ``NOT_APPROVED``, ``APPROVAL_EXPIRED`` or
  ``APPROVAL_USED``. A rejection also beats a later Allow for the same intent.

Expiry is lazy: every entry point first runs ``expire_due``. Every ``evaluate`` writes one
``gate.evaluate`` audit row.
"""

from __future__ import annotations

import json
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
    ResolveResult,
)
from resonant.principals import Principals
from resonant.store import approvals as ap
from resonant.store.audit import audit
from resonant.store.db import atomic, kv_get, utcnow
from resonant.tools import ToolRegistry, is_store_internal
from resonant.tools.registry import ToolKey
from resonant_sdk import Effect, Level

# Deny reasons the runner maps to its templated replies.
NOT_APPROVED = "not approved"
APPROVAL_EXPIRED = "approval expired"
APPROVAL_USED = "approval already used"
NO_TASK = "approvals need a task"

Rule = Literal["allow", "ask", "ask_code"]

# A code-built heading for a tool's approval summary (e.g. "delete all 4 notes"). It must
# never come from a model; ``summarize`` cleans it anyway.
SummaryAction = Callable[[Intent], str | None]


def rule(level: Level, origin: Origin, autonomy_on: bool) -> Rule:
    """The D1 table: risk (level) x who chose it (origin) x the kill switch."""
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
    except json.JSONDecodeError:
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
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.conn = conn
        self.principals = principals
        self.registry = registry
        self.dry_run = dry_run
        self.ttl = timedelta(seconds=approval_ttl_s)
        self.actions = dict(actions or {})
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
            return Deny(f"tool {intent.tool!r} is not registered with this spec")
        if not self.principals.can(intent.principal, "request", intent.extension):
            return Deny(f"{intent.principal} may not request {intent.extension or 'global'}")
        unsafe = unsafe_args(intent.args)
        if unsafe:
            # Never echo the offending keys or values: they are what's unsafe to show.
            return Deny(f"args contain unsafe characters ({len(unsafe)} found)")
        l0_read = intent.effect is Effect.READ and intent.level is Level.L0
        if self.dry_run and not l0_read and not is_store_internal(intent.spec):
            return DryRun(would=summarize(intent))
        verdict = rule(intent.level, intent.origin, autonomy)
        if verdict == "allow" and intent.level is Level.L0:
            return self._allow(intent)
        last = ap.latest_for_intent(self.conn, intent.task_id, intent.hash)
        if last is not None and last.status == "rejected":
            return Deny(NOT_APPROVED)  # the owner's "no" holds for this intent
        if verdict == "allow":
            return self._allow(intent)
        return self._ask(intent, now, last, require_code=verdict == "ask_code")

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
        action = self.actions.get((intent.extension, intent.tool))
        spec = ap.NewApproval(
            task_id=intent.task_id,
            intent_hash=intent.hash,
            tool=intent.tool,
            effect=intent.effect,
            level=intent.level,
            summary=summarize(intent, action=action(intent) if action else None),
            approvers=(self.principals.owner,),  # global scope is owner-only
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
            result = ResolveResult(ok=False, reason=reason)
            if reason is None and approval is not None:
                done = ap.get_approval(self.conn, approval.id)
                assert done is not None
                result = ap.resolve_result([done])
                ap.wake_resolved(self.conn, result, now, principal=resolution.actor)
            audit(
                self.conn,
                "approval.resolve",
                task_id=approval.task_id if approval else None,
                approved_by=resolution.actor,
                tool=approval.tool if approval else None,
                extension=approval.extension if approval else None,
                intent_hash=approval.intent_hash if approval else None,
                decision="approved" if resolution.approved else "rejected",
                approval_id=resolution.approval_id,
                actor_identity=resolution.actor_identity,
                path=resolution.path,
                comments=resolution.comments,
                ok=result.ok,
                reason=reason,
            )
        return result

    def resolve_reply(self, reply: ApprovalReply) -> ResolveResult:
        """Binding a text reply (sent-before, single pending, per-event idempotency) is not
        implemented yet, so a reply never resolves anything."""
        return ResolveResult(ok=False, reason="text replies are not bound yet")

    def open_batch(self, principal: str, approval_ids: Sequence[str]) -> ApprovalBatch:
        raise NotImplementedError("batch approvals are deferred")


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

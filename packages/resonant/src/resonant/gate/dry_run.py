"""DryRunGate: the Phase 0 gate.

It checks registration and permissions, allows L0 reads with a single-use token, and logs
everything else as DryRun. It has no approvals.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Sequence

from resonant.gate.types import (
    Allow,
    ApprovalBatch,
    ApprovalReply,
    ApprovalResolution,
    Decision,
    Deny,
    DryRun,
    Intent,
    PendingApproval,
    ResolveResult,
)
from resonant.principals import Principals
from resonant.store.audit import audit
from resonant.store.db import atomic
from resonant.tools import ToolRegistry
from resonant_sdk import Effect, Level

_UNAVAILABLE = "approvals are not available in dry-run (Phase 2)"


class DryRunGate:
    def __init__(
        self, conn: sqlite3.Connection, principals: Principals, registry: ToolRegistry
    ) -> None:
        self.conn = conn
        self.principals = principals
        self.registry = registry
        self._tokens: dict[str, str] = {}

    def evaluate(self, intent: Intent) -> Decision:
        decision: Decision
        registered = self.registry.get(intent.extension, intent.tool)
        if registered is None or registered != intent.spec:
            decision = Deny(f"tool {intent.tool!r} is not registered with this spec")
        elif not self.principals.can(intent.principal, "request", intent.extension):
            decision = Deny(f"{intent.principal} may not request {intent.extension or 'global'}")
        elif intent.effect is Effect.READ and intent.level is Level.L0:
            token = secrets.token_urlsafe(16)
            self._tokens[token] = intent.hash
            decision = Allow(token=token)
        else:
            would = {
                Level.L0: "Allow",
                Level.L1: "NeedsApproval",
                Level.L2: "Veto",
                Level.L3: "Allow",
            }
            decision = DryRun(would=would[intent.level])
        with atomic(self.conn):
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
                args=intent.args,
            )
        return decision

    def consume(self, token: str, intent_hash: str) -> bool:
        return self._tokens.pop(token, None) == intent_hash

    def pending_for(self, principal: str) -> Sequence[PendingApproval]:
        return ()

    def open_batch(self, principal: str, approval_ids: Sequence[str]) -> ApprovalBatch:
        raise NotImplementedError(_UNAVAILABLE)

    def resolve(self, resolution: ApprovalResolution) -> ResolveResult:
        return ResolveResult(ok=False, reason=_UNAVAILABLE)

    def resolve_reply(self, reply: ApprovalReply) -> ResolveResult:
        return ResolveResult(ok=False, reason=_UNAVAILABLE)

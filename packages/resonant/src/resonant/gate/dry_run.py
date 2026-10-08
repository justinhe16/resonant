"""DryRunGate: the Phase 0 gate. Checks permissions, allows plain reads, and logs the rest."""

from __future__ import annotations

import sqlite3

from resonant.gate.types import (
    Allow,
    ApprovalResolution,
    Decision,
    Deny,
    DryRun,
    Intent,
    ResolveResult,
)
from resonant.principals import Principals
from resonant.store.audit import audit
from resonant.store.db import transaction
from resonant_sdk import Effect, Level


class DryRunGate:
    def __init__(self, conn: sqlite3.Connection, principals: Principals) -> None:
        self.conn = conn
        self.principals = principals

    def evaluate(self, intent: Intent) -> Decision:
        decision: Decision
        if not self.principals.can(intent.principal, "request", intent.extension):
            decision = Deny(f"{intent.principal} may not request {intent.extension or 'global'}")
        elif intent.effect is Effect.READ and intent.level is Level.L0:
            decision = Allow()
        else:
            would = {
                Level.L0: "Allow",
                Level.L1: "NeedsApproval",
                Level.L2: "Veto",
                Level.L3: "Allow",
            }
            decision = DryRun(would=would[intent.level])
        with transaction(self.conn):
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

    def resolve(self, resolution: ApprovalResolution) -> ResolveResult:
        return ResolveResult(ok=False, reason="approvals are not available in dry-run (Phase 2)")

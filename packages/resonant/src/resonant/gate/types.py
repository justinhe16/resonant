"""Gate contract: intents, decisions, and approval resolution.

Order of evaluation (Phase 2 implements it; DryRunGate approximates it):
1. Permission: can(principal, "request", extension)? If not, Deny. This runs before any
   level logic.
2. Kill switch: if autonomy is off, anything above L0 needs approval.
3. Level: L0 Allow, L1 NeedsApproval, L2 Veto(until), L3 Allow (then report).
   Inside an approved computer-use scope, commit tools still need approval.
4. dry_run: anything that isn't a plain read becomes DryRun.

Approvals are bound to ``intent_hash`` and expire. The executor refuses an intent whose
hash doesn't match its approval.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from resonant_sdk import Effect, Level


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def intent_hash(tool: str, args: dict[str, Any], extension: str | None) -> str:
    payload = canonical_json({"tool": tool, "args": args, "extension": extension})
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class Intent:
    task_id: str
    principal: str
    extension: str | None
    tool: str
    args: dict[str, Any]
    effect: Effect
    level: Level
    commit: bool = False
    scope_id: str | None = None
    trace_id: str | None = None
    hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "hash", intent_hash(self.tool, self.args, self.extension))


# --- decisions -------------------------------------------------------------------------


@dataclass(frozen=True)
class Allow:
    approval_id: str | None = None


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class NeedsApproval:
    approval_id: str
    code: str
    approvers: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True)
class Veto:
    approval_id: str
    until: datetime


@dataclass(frozen=True)
class DryRun:
    would: str  # what the real gate would have decided, e.g. "NeedsApproval"


Decision = Allow | Deny | NeedsApproval | Veto | DryRun


# --- approval resolution ---------------------------------------------------------------

ApprovalPath = Literal["parser", "classifier", "button", "cli"]


@dataclass(frozen=True)
class ApprovalResolution:
    """One attempt to resolve an approval, from a channel reply, a button, or the CLI.

    The audit log records every field, including the raw reply text.
    """

    approval_id: str
    approved: bool
    actor: str  # principal name, resolved from an authenticated channel identity
    actor_identity: str  # e.g. "imessage:+15551234567"
    path: ApprovalPath
    raw_reply: str | None = None
    confidence: float | None = None
    comments: str | None = None


@dataclass(frozen=True)
class ResolveResult:
    ok: bool
    reason: str | None = (
        None  # why it was refused: expired, not an approver, pay needs exact code, ...
    )


class Gate(Protocol):
    def evaluate(self, intent: Intent) -> Decision: ...

    def resolve(self, resolution: ApprovalResolution) -> ResolveResult: ...

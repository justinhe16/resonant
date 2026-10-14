"""Gate contract: intents, decisions, tokens, and approval resolution.

Evaluation order (``PolicyGate`` implements it; ``DryRunGate`` is the Phase 0 stand-in):
1. Registration: the intent's ToolSpec must be the one registered for (extension, tool).
2. Permission: can(principal, "request", extension)? If not, Deny. This runs before any
   level logic.
3. Unsafe args (control, bidi, zero-width characters): Deny.
4. dry_run: anything that is neither an L0 read nor store-internal becomes DryRun.
5. Level x origin x autonomy: L0 Allow; L1 NeedsApproval with a code; L2 Allow when
   explicit, else NeedsApproval; L3 Allow. With autonomy off, everything above L0 needs
   approval. No Veto in Phase 2.

Every Allow carries a single-use ``token`` that the gate issued for that exact intent hash.
The executor calls ``gate.consume(token, intent.hash)`` before running anything, so an
Allow can't be forged, reused, or applied to different args.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from resonant_sdk import Effect, Level, ToolSpec


def _check_json(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for k, v in value.items():  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(k, str):
                raise TypeError(f"{path}: non-string key {k!r}")
            _check_json(v, f"{path}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            _check_json(v, f"{path}[{i}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path}: non-finite number")
    elif not isinstance(value, str | int | float | bool | type(None)):
        raise TypeError(f"{path}: unsupported type {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Deterministic JSON. Rejects non-string keys, NaN/Infinity, and non-JSON types."""
    _check_json(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# Who chose the action. "explicit": the owner typed a slash command, so the call is
# consent; code sets it only on the local runner's direct_call path. "inferred": the model
# (or, later, a scheduled or extension job) chose it. Never taken from model output.
Origin = Literal["explicit", "inferred"]
ORIGINS: tuple[Origin, ...] = ("explicit", "inferred")


@dataclass(frozen=True)
class Intent:
    """A request to run one registered tool with specific args. Build it with ``for_tool``.

    Effect, level, and commit come from the ToolSpec, never from the caller or the model.
    ``hash`` is recomputed on every access over everything that matters, so changing
    ``args`` after approval changes the hash and the approval no longer matches.
    ``origin`` is deliberately not hashed: an approval binds to what runs, not to who
    asked for it.
    """

    task_id: str
    principal: str
    spec: ToolSpec
    args: dict[str, Any]
    scope_id: str | None = None
    trace_id: str | None = None
    origin: Origin = "inferred"

    def __post_init__(self) -> None:
        if self.origin not in ORIGINS:
            raise ValueError(f"unknown origin {self.origin!r}")
        canonical_json(self.args)
        object.__setattr__(self, "args", copy.deepcopy(self.args))

    @classmethod
    def for_tool(
        cls,
        spec: ToolSpec,
        *,
        task_id: str,
        principal: str,
        args: dict[str, Any],
        scope_id: str | None = None,
        trace_id: str | None = None,
        origin: Origin = "inferred",
    ) -> Intent:
        return cls(task_id, principal, spec, args, scope_id, trace_id, origin)

    @property
    def tool(self) -> str:
        return self.spec.name

    @property
    def extension(self) -> str | None:
        return self.spec.extension

    @property
    def effect(self) -> Effect:
        return self.spec.effect

    @property
    def level(self) -> Level:
        return self.spec.effective_level

    @property
    def commit(self) -> bool:
        return self.spec.commit

    @property
    def hash(self) -> str:
        payload = canonical_json(
            {
                "tool": self.tool,
                "extension": self.extension,
                "args": self.args,
                "effect": self.effect.value,
                "level": self.level.value,
                "commit": self.commit,
            }
        )
        return hashlib.sha256(payload.encode()).hexdigest()


# --- decisions -------------------------------------------------------------------------


@dataclass(frozen=True)
class Allow:
    token: str  # single-use, issued by the gate for one intent hash
    approval_id: str | None = None


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class NeedsApproval:
    """Everything a channel needs to build an ApprovalPrompt."""

    approval_id: str
    code: str
    approvers: tuple[str, ...]
    expires_at: datetime
    summary: str  # built by code from intent fields; external strings sanitized
    effect: Effect
    level: Level
    require_code: bool  # L1 (destructive or pay): only "yes <code>" approves


@dataclass(frozen=True)
class Veto:
    approval_id: str
    until: datetime
    summary: str


@dataclass(frozen=True)
class DryRun:
    # PolicyGate: the code-built summary of what would have run, e.g. "restart ollama".
    # DryRunGate: the decision the real gate would have made, e.g. "NeedsApproval".
    would: str


Decision = Allow | Deny | NeedsApproval | Veto | DryRun


# --- approvals -------------------------------------------------------------------------

ApprovalPath = Literal["parser", "classifier", "button", "cli"]


@dataclass(frozen=True)
class PendingApproval:
    approval_id: str
    code: str
    task_id: str
    summary: str
    effect: Effect
    level: Level
    require_code: bool
    expires_at: datetime


@dataclass(frozen=True)
class ApprovalBatch:
    """A batch message listing several codes. "yes all" approves exactly these, never pay."""

    batch_id: str
    principal: str
    approval_ids: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True)
class ApprovalResolution:
    """Resolve one known approval by id (a Slack button or the CLI)."""

    approval_id: str
    approved: bool
    actor: str  # principal, resolved from an authenticated identity
    actor_identity: str  # e.g. "slack:U01" or "cli"
    path: ApprovalPath
    comments: str | None = None


@dataclass(frozen=True)
class ApprovalReply:
    """A text reply from a channel. The gate binds it to approvals.

    Binding rules: the actor must be an approver with the approve grant. ``code`` picks one
    approval. ``all_in_batch`` with ``batch_id`` picks that batch's snapshot. With neither,
    the reply binds only if exactly one non-pay approval is pending for the actor. Pay
    approvals bind only via path="parser" with an exact code. Anything else is ambiguous,
    so Resonant re-asks.
    """

    actor: str
    actor_identity: str  # e.g. "imessage:+15551234567"
    channel: str
    raw_text: str
    decision: Literal["approve", "deny"]
    path: Literal["parser", "classifier"]
    code: str | None = None
    all_in_batch: bool = False
    batch_id: str | None = None
    confidence: float | None = None
    comments: str | None = None


@dataclass(frozen=True)
class ResolvedApproval:
    approval_id: str
    task_id: str
    intent_hash: str
    approved: bool


@dataclass(frozen=True)
class ResolveResult:
    ok: bool
    # Why it was refused: expired, not an approver, ambiguous, pay needs the exact code, ...
    reason: str | None = None
    resolved: tuple[ResolvedApproval, ...] = field(default=())


class TokenVerifier(Protocol):
    def consume(self, token: str, intent_hash: str) -> bool:
        """True exactly once for a token the gate issued for this intent hash."""
        ...


class Gate(TokenVerifier, Protocol):
    def evaluate(self, intent: Intent) -> Decision: ...

    def pending_for(self, principal: str) -> Sequence[PendingApproval]: ...

    def open_batch(self, principal: str, approval_ids: Sequence[str]) -> ApprovalBatch: ...

    def resolve(self, resolution: ApprovalResolution) -> ResolveResult: ...

    def resolve_reply(self, reply: ApprovalReply) -> ResolveResult: ...

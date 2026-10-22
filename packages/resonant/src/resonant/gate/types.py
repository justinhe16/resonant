"""Gate contract: intents, decisions, tokens, and approval resolution.

Evaluation order (``PolicyGate`` implements it; ``DryRunGate`` is the Phase 0 stand-in):
1. Registration: the intent's ToolSpec must be the one registered for (extension, tool).
2. Permission: can(principal, "request", extension)? If not, Deny. This runs before any
   level logic.
3. Unsafe args (control, bidi, zero-width characters): Deny.
4. dry_run: anything that is neither an L0 read nor store-internal becomes DryRun.
5. Level x origin x autonomy: L0 Allow; L1 NeedsApproval with a code; L2 Allow when
   explicit, else NeedsApproval (``scheduled`` is never explicit); L3 Allow, except a
   ``tainted`` call, which asks. With autonomy off, everything above L0 needs approval. No
   Veto yet.

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
from typing import Any, Literal, Protocol, get_args

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
# chose it. "scheduled": a job fired on its schedule; only the scheduler and the job runner
# set it, and the gate treats it like "inferred" (never explicit). "tainted": the model
# chose it in a task that has read untrusted content (files, logs, transcripts, the web,
# email, extension results, ``ask_claude`` output; docs/brain-plan.md D6); the gate asks
# before L2 and L3 alike and no standing allow applies. Only runner code that tracks taint
# sets it (the brain loop, BR-1). Never taken from model output.
Origin = Literal["explicit", "inferred", "scheduled", "tainted"]
ORIGINS: tuple[Origin, ...] = get_args(Origin)


@dataclass(frozen=True)
class OwnerAsk:
    """PER-272: the owner's own message this turn, for a call the brain marked
    ``owner_asked`` (``trust/asked.py``). Set only by code: the Claude bridge reads ``text``
    from the inbound message event that started the turn (``event_id``), never from the model.
    ``lookups``: the Resonant reads that tainted the turn (``list_runs``), when nothing else
    did; empty when the turn is untainted. The gate decides whether it is the approval."""

    text: str
    event_id: str
    lookups: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Intent:
    """A request to run one registered tool with specific args. Build it with ``for_tool``.

    Effect, level, and commit come from the ToolSpec, never from the caller or the model.
    ``hash`` is recomputed on every access over everything that matters, so changing
    ``args`` after approval changes the hash and the approval no longer matches.
    ``origin`` is deliberately not hashed: an approval binds to what runs, not to who
    asked for it. Nor is ``asked`` (PER-272: the owner's own text, when the brain marked the
    call as the owner's direct ask; only ``ToolCore.call`` sets it, from the bridge).
    """

    task_id: str
    principal: str
    spec: ToolSpec
    args: dict[str, Any]
    scope_id: str | None = None
    trace_id: str | None = None
    origin: Origin = "inferred"
    asked: OwnerAsk | None = None

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
        asked: OwnerAsk | None = None,
    ) -> Intent:
        return cls(task_id, principal, spec, args, scope_id, trace_id, origin, asked)

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
    # PER-272: a change that runs on nobody's word for this exact call (no approval, not a
    # slash command): an L3, a standing allow, a trust rule. ``merge_run`` merges stricter
    # then (green CI, no flagged paths, CLEAN); the executor passes it on (``CallContext``).
    unattended: bool = False


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
    allow_always: bool = False  # a worker's trust check: "yes always" also adds a rule
    phrased: bool = False  # PER-268: the summary is the tool's own phrase (a question prompt)


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
    changes: int = 1  # a batch's number of changes (BA-1); 1 for a lone approval


@dataclass(frozen=True)
class ApprovalBatch:
    """One prompt for several pending approvals of one task (BA-1, ``open_batch``).

    ``code`` (its first approval's) answers all of them: ``yes <code>`` approves every one,
    ``no`` rejects every one, ``yes 1,3 <code>`` (PER-255) approves only those and rejects the
    rest, and they expire together at ``expires_at``. It takes the strictest of its approvals
    (of the picked ones, for ``yes 1,3``): ``require_code`` when any needs the code (L1, pay),
    ``destructive`` when any is an L1 write. ``lines`` are the approvals' code-built
    summaries, in order. Asked only of ``principal`` (the owner)."""

    batch_id: str
    principal: str
    approval_ids: tuple[str, ...]
    created_at: datetime
    code: str = ""
    task_id: str = ""
    expires_at: datetime | None = None
    lines: tuple[str, ...] = ()
    require_code: bool = False
    destructive: bool = False
    level: Level = Level.L2
    effect: Effect = Effect.WRITE

    @property
    def approvers(self) -> tuple[str, ...]:
        return (self.principal,)


@dataclass(frozen=True)
class ApprovalResolution:
    """Resolve one known approval by id (a Slack button or the CLI)."""

    approval_id: str
    approved: bool
    actor: str  # principal, resolved from an authenticated identity
    actor_identity: str  # e.g. "slack:U01" or "cli"
    path: ApprovalPath
    comments: str | None = None
    # PER-261 (``resonant approvals approve --always``): approve and save the ``yes always``
    # rule the prompt offered; refused (``always_unsupported``) for one that offered none.
    always: bool = False

    def __post_init__(self) -> None:
        if self.always and not self.approved:
            raise ValueError("always is an approval")


@dataclass(frozen=True)
class ApprovalReply:
    """A text reply from a channel. The gate binds it to approvals (``resolve_reply``).

    ``event_id`` and ``occurred_at`` come from the reply's ``Event``. ``occurred_at`` drives
    the sent-before rule (a reply never binds an approval whose prompt was sent after it),
    and ``event_id`` makes resolution idempotent per event: routing the same event again
    returns the original result with no new effect.

    Binding rules: the actor must be an approver with the approve grant, and the reply
    must come from iMessage via the parser. ``code`` picks one approval. Without a code,
    the reply binds only if exactly one approval is pending for the actor. A
    ``require_code`` approval (L1, pay) is approved only by ``yes <code>``; a plain ``no``
    may deny it. ``always`` (``yes always``) binds like a plain or coded yes, but only to an
    approval that offered it (``allow_always``: a worker's trust check, never
    ``require_code``); for any other it is refused, never read as a plain yes. ``picks``
    (``yes 1,3``, PER-255) binds only to a batch: those changes are approved and the rest
    rejected, and the code is required when any picked change needs it. Anything else is
    refused with a ``ReplyRefusal`` and Resonant re-asks.
    ``all_in_batch``, ``batch_id``, ``confidence`` and ``path="classifier"`` are deferred
    (C4) and refused.
    """

    actor: str
    actor_identity: str  # e.g. "imessage:+15551234567"
    channel: str
    raw_text: str
    decision: Literal["approve", "deny"]
    path: Literal["parser", "classifier"]
    event_id: str  # the reply Event's id
    occurred_at: datetime  # the reply Event's occurred_at; timezone-aware
    code: str | None = None
    all_in_batch: bool = False
    batch_id: str | None = None
    confidence: float | None = None
    comments: str | None = None
    always: bool = False  # "yes always": approve and add a trust rule (trust checks only)
    # PER-255: "yes 1,3": the batch's change numbers (1-based) to approve; the rest are
    # rejected. None: the reply answers the whole approval or batch.
    picks: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.always and self.decision != "approve":
            raise ValueError("always is an approval")
        if self.picks is not None and (self.decision != "approve" or self.always or not self.picks):
            raise ValueError("picks are a plain approval of some changes")


# Why ``resolve_reply`` refused a reply. A closed set the router maps to its replies:
# - nothing_pending: no approval is pending for the actor. A non-approver always gets
#   this, whatever it sends, so pending approvals are never revealed.
# - ambiguous: no code and more than one approval pending (or was, when the reply was
#   sent). Re-ask listing the codes.
# - unknown_code: the code matches none of the actor's pending approvals.
# - code_required: a plain "yes" for a require_code approval.
# - not_sent_before: the approval's prompt was not sent to the actor before the reply.
# - unsupported: not an iMessage parser reply, or a ``yes all`` reply (not offered).
# - always_unsupported: "yes always" for an approval that doesn't offer it (anything but a
#   worker's trust check, and never a require_code one).
# - not_a_batch: "yes 1,3" for a lone approval (PER-255).
# - unknown_pick: "yes 1,3" with a number that isn't one of the batch's changes (PER-255).
# - not_found, already_resolved, expired, task_not_waiting: the store refused (a race).
ReplyRefusal = Literal[
    "nothing_pending",
    "ambiguous",
    "unknown_code",
    "code_required",
    "not_sent_before",
    "unsupported",
    "always_unsupported",
    "not_a_batch",
    "unknown_pick",
    "not_found",
    "already_resolved",
    "expired",
    "task_not_waiting",
]
REPLY_REFUSALS: tuple[ReplyRefusal, ...] = get_args(ReplyRefusal)


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

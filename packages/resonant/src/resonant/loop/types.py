"""Task envelope and runner contract.

The loop owns the lifecycle. A runner owns the work:

    queued -> running -> waiting(reason) -> done | failed | cancelled

Each ``Runner.step`` call is one iteration. Its outcome and checkpoint are persisted
atomically, together with marking the events it saw as consumed. If the daemon crashes
mid-step, nothing from that step is persisted and the step runs again from the last
checkpoint. So steps must be idempotent: side effects go through the executor, which
dedupes them by idempotency key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from resonant_sdk import Event

TaskStatus = Literal["queued", "running", "waiting", "done", "failed", "cancelled"]
WaitReason = Literal["approval", "human_reply", "usage_reset", "ci", "veto"]
TERMINAL: frozenset[TaskStatus] = frozenset({"done", "failed", "cancelled"})

# Priority comes from lane/principal, never from the model. Lower runs first.
PRIORITY_ONCALL = 0
PRIORITY_OWNER = 1
PRIORITY_MEMBER = 2
PRIORITY_BACKGROUND = 3


@dataclass(frozen=True)
class Task:
    id: str
    kind: str
    runner: str
    priority: int
    status: TaskStatus
    trace_id: str
    created_at: datetime
    updated_at: datetime
    extension: str | None = None
    principal: str | None = None
    lane: str = "default"
    wait_reason: WaitReason | None = None
    # For waiting tasks: when to wake. For queued tasks: not before (retry backoff).
    wake_at: datetime | None = None
    attempts: int = 0
    step_no: int = 0
    checkpoint: dict[str, Any] = field(default_factory=dict[str, Any])
    claude_session_id: str | None = None
    worktree_path: str | None = None
    scope: dict[str, Any] | None = None
    origin_event_id: str | None = None
    channel_ref: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class NewTask:
    kind: str
    runner: str
    priority: int
    extension: str | None = None
    principal: str | None = None
    lane: str = "default"
    checkpoint: dict[str, Any] = field(default_factory=dict[str, Any])
    scope: dict[str, Any] | None = None
    origin_event_id: str | None = None
    channel_ref: dict[str, Any] | None = None
    trace_id: str | None = None


# --- step outcomes -------------------------------------------------------------------


@dataclass(frozen=True)
class Continue:
    """More work to do. The task is requeued right away."""

    checkpoint: dict[str, Any]


@dataclass(frozen=True)
class Wait:
    """Park until an event for this task arrives or ``wake_at`` passes."""

    reason: WaitReason
    checkpoint: dict[str, Any]
    wake_at: datetime | None = None


@dataclass(frozen=True)
class Done:
    result: dict[str, Any]
    checkpoint: dict[str, Any] | None = None


@dataclass(frozen=True)
class Fail:
    """``retry_at`` overrides the default exponential backoff for retryable failures."""

    error: str
    retryable: bool = False
    checkpoint: dict[str, Any] | None = None
    retry_at: datetime | None = None


StepOutcome = Continue | Wait | Done | Fail


class StepContext(Protocol):
    """Lets a runner persist progress mid-step, before the step returns.

    ``save`` commits right away and leaves the task 'running', without consuming any events.
    Use it for anything a retry must not lose. For example, a Claude runner saves
    ``claude_session_id`` as soon as the session starts, so a crash resumes that session
    instead of starting a duplicate.
    """

    def save(
        self,
        *,
        checkpoint: dict[str, Any] | None = None,
        claude_session_id: str | None = None,
        worktree_path: str | None = None,
    ) -> None: ...


class Runner(Protocol):
    """Implemented by each kind of worker: local (Phase 1), claude (Phase 5), cu (Phase 7).

    Contract:
    - ``step`` must be idempotent with respect to its last persisted checkpoint. Side effects
      go through the executor, which dedupes them by idempotency key.
    - ``step`` must clean up any subprocess it started when it gets ``CancelledError``
      (cancel, stuck timeout, or shutdown) and then re-raise.
    - A Claude slot is held only while a step is in flight. A runner that needs to wait
      (approval, human reply, usage reset, CI) returns ``Wait`` and frees the slot. It
      resumes the session later by ``claude_session_id``.
    - ``max_step_s`` overrides the loop's ``stuck_after_s`` for this runner (None = default).
    """

    @property
    def name(self) -> str: ...

    @property
    def uses_claude_slot(self) -> bool: ...

    @property
    def max_step_s(self) -> float | None: ...

    async def step(self, task: Task, events: list[Event], ctx: StepContext) -> StepOutcome: ...

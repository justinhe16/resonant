"""Tool specification: what a tool does and how dangerous it is.

Effect and level come from registration (this spec, built from code or a manifest),
never from the model. The model only ever supplies a tool name and arguments.

Invariants, enforced here at registration:
- ``pay`` is always L1. Nothing can lower or raise it, including future trust promotion.
- ``read`` with no secrets is always L0.
- ``commit=True`` marks irreversible user-visible actions (send, pay, delete). These always
  pause for approval inside a computer-use scope, so they must have a side effect.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Effect(StrEnum):
    READ = "read"
    WRITE = "write"
    PAY = "pay"


class Level(StrEnum):
    """Policy levels, by reversibility x blast radius."""

    L0 = "L0"  # act silently (reads, triage)
    L1 = "L1"  # draft -> approver approves first
    L2 = "L2"  # act after a veto window unless stopped
    L3 = "L3"  # act, then report


DEFAULT_LEVEL: dict[Effect, Level] = {
    Effect.READ: Level.L0,
    Effect.WRITE: Level.L1,
    Effect.PAY: Level.L1,
}

TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = ""
    args_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    effect: Effect
    level: Level | None = None
    source: str = "builtin"  # "builtin" | "ext:<name>"
    extension: str | None = None
    timeout_s: float = Field(default=30.0, gt=0)
    secrets: tuple[str, ...] = ()
    commit: bool = False

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.effect is Effect.PAY and self.level not in (None, Level.L1):
            raise ValueError(f"{self.name}: pay tools are always L1")
        if self.effect is Effect.READ and not self.secrets and self.level not in (None, Level.L0):
            raise ValueError(f"{self.name}: read tools without secrets are always L0")
        if self.commit and self.effect is Effect.READ:
            raise ValueError(f"{self.name}: commit tools must have a side effect")
        return self

    @property
    def effective_level(self) -> Level:
        if self.effect is Effect.PAY:
            return Level.L1
        return self.level or DEFAULT_LEVEL[self.effect]

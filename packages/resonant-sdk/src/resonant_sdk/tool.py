"""Tool specification: what a tool does and how dangerous it is.

Effect and level come from registration (this spec, built from code or a manifest),
never from the model. The model only ever supplies a tool name and arguments.

Invariants, enforced here at registration:
- ``pay`` is always L1. Nothing can lower or raise it, including future trust promotion.
- ``read`` with no secrets is always L0.
- ``write`` with no level declared is L2 (GT-1): it runs when the owner explicitly asked for
  it and asks a plain yes otherwise. A tool that can't be undone declares L1 (yes <code>).
- ``commit=True`` marks irreversible user-visible actions (send, pay, delete). These always
  pause for approval inside a computer-use scope, so they must have a side effect.
- ``untrusted=True`` marks a tool whose result carries outside content (file text, logs, a
  worker's transcript): a loop that sees one treats the task as tainted (docs/brain-plan.md
  D6). Extension and MCP results are outside content whatever the flag says.
- ``taint_exempt=True`` (PER-245) marks a low-risk, owner-scoped builtin write (a note, a
  reminder, a fact) that runs on its own level's rule even in a tainted task: ToolCore gates
  it as ``inferred``. Only builtin writes below L1 can carry it; never an extension's.
- ``approval_summary`` (PER-268) is a short phrase an approval prompt reads instead of the
  arg list, filled from the call's args: ``merge PR #{number} in {repo}`` makes ``OK to merge
  PR #121 in justinhe16/resonant? [P3] yes / no``. ``{name}`` is an arg's value (cleaned and
  clipped by the gate); ``[...]`` is a part shown only when every arg in it is set (a boolean
  arg prints nothing: it is only the condition), e.g. ``[{merge} (merges if clean)]``. An arg
  outside ``[...]`` that is missing falls back to the arg list. Write it as what the tool
  would do, lower case: it follows "OK to", "Couldn't" and "Dry run: would".
- ``grant`` names a builtin a member may request through ``grants: {"builtin:<grant>":
  [request]}`` (``resonant.principals``). Only global L0 reads can carry one.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal, Self

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
    Effect.WRITE: Level.L2,
    Effect.PAY: Level.L1,
}

TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
SUMMARY_MAX = 120
_SUMMARY_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}|\[|\]|[^{}\[\]]+")

# One part of an approval summary template: literal text, or an arg's name.
SummaryPiece = tuple[Literal["text", "arg"], str]


def parse_summary(template: str) -> list[tuple[bool, tuple[SummaryPiece, ...]]]:
    """``approval_summary`` as segments ``(optional, pieces)``: a ``[...]`` part is optional,
    the rest is not. Raises ``ValueError`` on anything else: a stray or nested bracket, a
    brace that isn't ``{arg_name}``, a control character, over ``SUMMARY_MAX`` characters."""
    if len(template) > SUMMARY_MAX or not template.isprintable() or not template.strip():
        raise ValueError(f"approval_summary must be one printable line of <= {SUMMARY_MAX} chars")
    segments: list[tuple[bool, tuple[SummaryPiece, ...]]] = []
    pieces: list[SummaryPiece] = []
    optional = False
    pos = 0
    while pos < len(template):
        m = _SUMMARY_TOKEN.match(template, pos)
        if m is None:
            raise ValueError("approval_summary: a brace must be a plain {arg_name}")
        part, pos = m.group(0), m.end()
        if part in ("[", "]"):
            if (part == "[") == optional:
                raise ValueError("approval_summary: [ ] parts can't nest or stray")
            if pieces:
                segments.append((optional, tuple(pieces)))
            pieces, optional = [], part == "["
        elif m.group(1) is not None:
            pieces.append(("arg", m.group(1)))
        else:
            pieces.append(("text", part))
    if optional:
        raise ValueError("approval_summary: a [ part isn't closed")
    if pieces:
        segments.append((False, tuple(pieces)))
    return segments


def summary_args(template: str) -> set[str]:
    """The arg names an ``approval_summary`` uses."""
    return {name for _, pieces in parse_summary(template) for kind, name in pieces if kind == "arg"}


GRANT_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"


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
    untrusted: bool = False
    taint_exempt: bool = False
    grant: str | None = Field(default=None, pattern=GRANT_PATTERN)
    approval_summary: str | None = None

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.approval_summary is not None:
            parse_summary(self.approval_summary)
        if self.effect is Effect.PAY and self.level not in (None, Level.L1):
            raise ValueError(f"{self.name}: pay tools are always L1")
        if self.effect is Effect.READ and not self.secrets and self.level not in (None, Level.L0):
            raise ValueError(f"{self.name}: read tools without secrets are always L0")
        if self.commit and self.effect is Effect.READ:
            raise ValueError(f"{self.name}: commit tools must have a side effect")
        if self.taint_exempt and (
            self.extension is not None
            or self.source != "builtin"
            or self.effect is not Effect.WRITE
            or self.level in (None, Level.L1)
        ):
            raise ValueError(f"{self.name}: only builtin L2/L3 writes can be taint exempt")
        if self.grant is not None and (
            self.extension is not None or self.effect is not Effect.READ or self.secrets
        ):
            raise ValueError(f"{self.name}: only builtin L0 reads can carry a member grant")
        return self

    @property
    def effective_level(self) -> Level:
        if self.effect is Effect.PAY:
            return Level.L1
        return self.level or DEFAULT_LEVEL[self.effect]

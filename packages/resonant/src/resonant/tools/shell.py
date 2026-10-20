"""The Claude brain's own tool calls as the gate sees them (PER-258, ``gate/shell.py``).

Two internal builtins that no model is ever offered (``runners.brain.NOT_IN_BRAIN``, the
local model's toolsets name tools explicitly): ``shell_read`` (L0) and ``shell`` (a change,
registered L2; the gate's ``levels`` hook picks L3 for local branch work, a feature-branch
push or a worktree edit, and its ``refusals`` hook refuses the never-list). Only the Claude
bridge's permission tool calls them (``runners/ask_tools.py``), with args it builds from
Claude Code's permission request: ``tool``, ``command`` (the command, or the file path for
an edit), ``cwd``, ``shown`` (redacted, clipped), ``verb``, and ``repo`` and ``digest`` when
known.

Their handlers run nothing: Claude Code runs the command once the bridge answers ``allow``.
A handler's run is the gate's grant, recorded by the executor exactly once: live, the bridge
then answers ``allow``; on an approval's resume (the owner said yes after the turn ended),
the ask runner turns it into a one-shot grant and a new turn that runs it
(``runners/ask.py``).
"""

from __future__ import annotations

from typing import Any

from resonant.executor import ToolFailed, ToolHandler
from resonant.gate.shell import TOOL_READ, TOOL_WRITE
from resonant.tools.base import BuiltinContext
from resonant_sdk import Effect, Level, ToolSpec

_STR = {"type": "string", "maxLength": 20000}
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "minLength": 1, "maxLength": 64},
        "command": _STR,
        "cwd": {"type": "string", "maxLength": 4096},
        "shown": {"type": "string", "maxLength": 400},
        "verb": {"type": "string", "maxLength": 64},
        "repo": {"type": "string", "maxLength": 200},
        "digest": {"type": "string", "maxLength": 64},
        "dir": {"type": "string", "maxLength": 4096},
        "script": {"type": "string", "maxLength": 8},
    },
    "required": ["tool", "command", "cwd", "shown", "verb"],
    "additionalProperties": False,
}

SHELL = ToolSpec(
    name=TOOL_WRITE,
    description="Internal: a change the Claude brain's own tools make (a command or an edit).",
    args_schema=_SCHEMA,
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=5.0,
)
SHELL_READ = ToolSpec(
    name=TOOL_READ,
    description="Internal: a read the Claude brain's own tools make.",
    args_schema=_SCHEMA,
    effect=Effect.READ,
    timeout_s=5.0,
)
SPECS: tuple[ToolSpec, ...] = (SHELL, SHELL_READ)
INTERNAL: frozenset[str] = frozenset(s.name for s in SPECS)


_KEYS = frozenset(_SCHEMA["properties"])
_REQUIRED = frozenset(_SCHEMA["required"])


def _check(args: dict[str, Any]) -> None:
    keys = set(args)
    if not keys <= _KEYS or not keys >= _REQUIRED:
        raise ToolFailed("shell: unexpected or missing args")
    if not all(isinstance(args[k], str) for k in keys):
        raise ToolFailed("shell: args must be strings")


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    async def _permit(args: dict[str, Any]) -> dict[str, Any]:
        _check(args)
        return {
            "permitted": True,
            "tool": args.get("tool"),
            "shown": args.get("shown"),
            "cwd": args.get("cwd"),
        }

    return {SHELL.name: _permit, SHELL_READ.name: _permit}

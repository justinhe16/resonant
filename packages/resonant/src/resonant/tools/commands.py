"""``list_commands``: the slash commands, from the command registry (``router/commands.py``).

The model's answer to "what can you do?" comes from here, so it can't invent commands.
It is a global builtin, so the gate lets only principals who can request global tools (the
owner) call it; it lists what the owner may use. ``/help`` renders the same registry for
whoever asks, without a tool call, so members get it too.
"""

from __future__ import annotations

from typing import Any

from resonant.executor import ToolFailed, ToolHandler
from resonant.router import commands
from resonant.tools.base import BuiltinContext
from resonant_sdk import Effect, ToolSpec

LIST_COMMANDS = ToolSpec(
    name="list_commands",
    description=(
        "List Resonant's slash commands with usage, what each does and its level. "
        "Optionally only those matching a word."
    ),
    args_schema={
        "type": "object",
        "properties": {"query": {"type": "string", "maxLength": commands.HELP_QUERY_MAX}},
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=5.0,
)

SPECS: tuple[ToolSpec, ...] = (LIST_COMMANDS,)


def list_commands(ctx: BuiltinContext, args: dict[str, Any], *, principal: str) -> dict[str, Any]:
    """The commands ``principal`` may use (``commands.available``), optionally searched."""
    unknown = set(args) - {"query"}
    if unknown:
        raise ToolFailed(f"unknown arguments: {sorted(unknown)}")
    query = args.get("query")
    if query is not None and (not isinstance(query, str) or len(query) > commands.HELP_QUERY_MAX):
        raise ToolFailed(f"query must be a string of at most {commands.HELP_QUERY_MAX} chars")
    cmds = commands.available(principal, principals=ctx.principals, registry=ctx.registry)
    if query:
        cmds = commands.search(query, cmds)
    out = [commands.describe(c, ctx.registry) for c in cmds]
    return {"commands": out, "count": len(out), "query": query or None}


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    async def _list_commands(args: dict[str, Any]) -> dict[str, Any]:
        # Handlers don't see the requester; the gate already limited callers to the owner.
        return list_commands(ctx, args, principal=ctx.principals.owner)

    return {LIST_COMMANDS.name: _list_commands}

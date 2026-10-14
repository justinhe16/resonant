"""Command registry (``router/commands.py``), the ``list_commands`` tool, and builtin modules."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import pytest

from resonant.api import DaemonState
from resonant.config import load_settings
from resonant.executor import ToolFailed
from resonant.loop import Loop, SlotPool
from resonant.principals import Principal, Principals
from resonant.router import commands
from resonant.router.commands import Command, UsageError
from resonant.router.fast_path import COMMAND_RENDERS
from resonant.store.db import utcnow
from resonant.tools import ToolRegistry, builtin
from resonant.tools.base import BuiltinContext
from resonant.tools.commands import list_commands
from resonant_sdk import Effect, ToolSpec

OWNER, MEMBER = "justin", "cofounder"


@pytest.fixture
def principals() -> Principals:
    return Principals(
        principals={
            OWNER: Principal(role="owner", identities=("imessage:+15550000000",)),
            MEMBER: Principal(identities=("imessage:+15551111111",), grants={"dori": ("request",)}),
        }
    )


@pytest.fixture
def state(db: sqlite3.Connection) -> DaemonState:
    settings = load_settings()
    return DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)), utcnow())


@pytest.fixture
def ctx(db: sqlite3.Connection, state: DaemonState, principals: Principals) -> BuiltinContext:
    registry = ToolRegistry()
    builtin.register_builtins(registry, db, state, principals=principals)
    return BuiltinContext(conn=db, state=state, registry=registry, principals=principals)


# --- registry ----------------------------------------------------------------------------


def test_every_command_tool_is_a_registered_builtin() -> None:
    builtins = {s.name: s for s in builtin.BUILTIN_SPECS}
    for c in commands.COMMANDS:
        if c.tool is not None:
            assert c.tool in builtins, c.name
            assert builtins[c.tool].extension is None
            assert c.name in COMMAND_RENDERS, f"{c.name} has a tool but no reply template"


def test_command_names_are_unique_slash_words() -> None:
    names = [c.name for c in commands.COMMANDS]
    assert len(set(names)) == len(names)
    assert all(re.fullmatch(r"/[a-z][a-z0-9_-]*", n) for n in names)
    assert all(c.usage.split()[0] == c.name for c in commands.COMMANDS)
    assert commands.get("/help") is commands.HELP


def test_parsers() -> None:
    assert commands.no_args("") == {}
    assert commands.no_args("?") == {}
    with pytest.raises(UsageError):
        commands.no_args("now")
    assert commands.help_args("") == {}
    assert commands.help_args("  /ReMind? ") == {"query": "remind"}
    with pytest.raises(UsageError, match="too long"):
        commands.help_args("x" * 51)


def test_usage_reply() -> None:
    cmd = Command("/remind", "/remind <when> <text>", "set a reminder", "unknown")
    assert cmd.usage_reply() == "Usage: /remind <when> <text>"
    assert cmd.usage_reply(UsageError()) == "Usage: /remind <when> <text>"
    assert cmd.usage_reply(UsageError("say 3pm or 15:00")) == (
        "say 3pm or 15:00. Usage: /remind <when> <text>"
    )


# Later tickets add these; the matching logic must already handle them.
REMIND = Command("/remind", "/remind <when> <text>", "set a reminder", "unknown")
REMINDERS = Command("/reminders", "/reminders", "pending reminders, soonest first", "unknown")
NOTE = Command("/note", "/note <text>", "save a note", "unknown")
FUTURE = (*commands.COMMANDS, REMIND, REMINDERS, NOTE)


def test_suggest() -> None:
    assert commands.suggest("/remnd", FUTURE)[0] == "/remind"
    assert commands.suggest("/stauts") == ["/status"]
    assert commands.suggest("/zzzz") == []
    assert len(commands.suggest("/re", FUTURE)) <= commands.SUGGEST_N


def test_unknown_reply() -> None:
    assert commands.unknown_reply(None) == "Unknown command. Try /help."
    assert commands.unknown_reply("/stauts") == "Unknown command /stauts. Did you mean /status?"
    assert commands.unknown_reply("/zzzz") == "Unknown command /zzzz. Try /help."


def test_search_substring_then_fuzzy() -> None:
    assert [c.name for c in commands.search("remind", FUTURE)] == ["/remind", "/reminders"]
    assert [c.name for c in commands.search("note", FUTURE)] == ["/note"]
    assert [c.name for c in commands.search("remnd", FUTURE)] == ["/remind", "/reminders"]
    assert commands.search("qqqq", FUTURE) == []


def test_help_text() -> None:
    cmds = [commands.HELP, NOTE]
    assert commands.help_text(cmds, keywords=False) == (
        "Commands:\n/help [word]: list commands, or the ones matching a word\n"
        "/note <text>: save a note"
    )
    assert commands.help_text(cmds, keywords=True).endswith("\n" + commands.KEYWORDS_HINT)
    assert commands.help_text(cmds, "note", keywords=True) == (
        'Commands matching "note":\n/note <text>: save a note'
    )


def test_available_filters_by_principal(principals: Principals) -> None:
    registry = ToolRegistry()
    for spec in builtin.BUILTIN_SPECS:
        registry.register(spec)
    owner = commands.available(OWNER, principals=principals, registry=registry)
    member = commands.available(MEMBER, principals=principals, registry=registry)
    stranger = commands.available("nobody", principals=principals, registry=registry)
    assert [c.name for c in owner] == [c.name for c in commands.COMMANDS]
    assert [c.name for c in member] == ["/help"]
    assert [c.name for c in stranger] == ["/help"]
    # A command whose tool isn't registered is hidden, even from the owner.
    empty = commands.available(OWNER, principals=principals, registry=ToolRegistry())
    assert [c.name for c in empty] == ["/approvals", "/help", "/kill", "/resume"]


def test_describe_takes_the_level_from_the_tool_registry() -> None:
    registry = ToolRegistry()
    registry.register(ToolSpec(name="add_note", effect=Effect.WRITE))
    cmd = Command("/note", "/note <text>", "save a note", "unknown", tool="add_note")
    assert commands.describe(cmd, registry)["level"] == "L1"  # the write default, not L3
    assert commands.describe(commands.HELP, registry) == {
        "name": "/help",
        "usage": "/help [word]",
        "description": "list commands, or the ones matching a word",
        "tool": None,
        "level": None,
        "owner_only": False,
    }


# --- list_commands -----------------------------------------------------------------------


async def test_list_commands_tool_lists_the_owner_view(ctx: BuiltinContext) -> None:
    handlers = builtin.register_builtins(
        ToolRegistry(), ctx.conn, ctx.state, principals=ctx.principals
    )
    out = await handlers[(None, "list_commands")]({})
    json.dumps(out, allow_nan=False)
    assert out["count"] == len(commands.COMMANDS) and out["query"] is None
    by_name = {c["name"]: c for c in out["commands"]}
    assert by_name["/status"]["level"] == "L0"
    assert by_name["/status"]["tool"] == "daemon_status"
    assert by_name["/kill"] == {
        "name": "/kill",
        "usage": "/kill",
        "description": "stop autonomy and pause",
        "tool": None,
        "level": None,
        "owner_only": True,
    }
    filtered = await handlers[(None, "list_commands")]({"query": "task"})
    assert [c["name"] for c in filtered["commands"]] == ["/projects", "/tasks"]
    assert filtered["query"] == "task"


def test_list_commands_respects_the_principal(ctx: BuiltinContext) -> None:
    owner = list_commands(ctx, {}, principal=OWNER)
    member = list_commands(ctx, {}, principal=MEMBER)
    assert {c["name"] for c in owner["commands"]} == {c.name for c in commands.COMMANDS}
    assert [c["name"] for c in member["commands"]] == ["/help"]
    assert list_commands(ctx, {"query": "kill"}, principal=MEMBER)["commands"] == []


@pytest.mark.parametrize("args", [{"x": 1}, {"query": 3}, {"query": "x" * 51}])
def test_list_commands_bad_args(ctx: BuiltinContext, args: dict[str, Any]) -> None:
    with pytest.raises(ToolFailed):
        list_commands(ctx, args, principal=OWNER)


# --- builtin modules ---------------------------------------------------------------------


def test_builtin_modules_match_their_specs(ctx: BuiltinContext) -> None:
    names = [s.name for s in builtin.BUILTIN_SPECS]
    assert len(set(names)) == len(names)
    for module in builtin.BUILTIN_MODULES:
        assert set(module.handlers(ctx)) == {s.name for s in module.SPECS}
        for spec in module.SPECS:
            assert spec.extension is None and spec.source == "builtin"

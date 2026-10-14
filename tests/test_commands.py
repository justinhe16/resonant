"""Command registry (``router/commands.py``), the ``list_commands`` tool, and builtin modules."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
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
from resonant.runners.local import REPORTS
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
    """A read has a fast-path template; a side effect has the local runner's report line."""
    builtins = {s.name: s for s in builtin.BUILTIN_SPECS}
    for c in commands.COMMANDS:
        for tool in c.tools:
            assert tool in builtins, c.name
            spec = builtins[tool]
            assert spec.extension is None
            if spec.effect is Effect.READ:
                assert c.name in COMMAND_RENDERS, f"{c.name} reads {tool} but has no template"
            else:
                assert tool in REPORTS, f"{c.name} runs {tool} but has no report line"


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


NOTE = Command("/note", "/note <text>", "save a note", "unknown")


def test_suggest() -> None:
    assert commands.suggest("/remnd")[0] == "/remind"
    assert commands.suggest("/stauts") == ["/status"]
    assert commands.suggest("/remindr") == ["/remind", "/reminders"]  # both about as close
    assert commands.suggest("/stauts", [commands.HELP]) == []
    assert commands.suggest("/zzzz") == []
    assert len(commands.suggest("/re")) <= commands.SUGGEST_N


def test_unknown_reply() -> None:
    assert commands.unknown_reply(None) == "Unknown command. Try /help."
    assert commands.unknown_reply("/remnd") == "Unknown command /remnd. Did you mean /remind?"
    assert commands.unknown_reply("/zzzz") == "Unknown command /zzzz. Try /help."
    # Only the commands passed in (the asker's) are suggested.
    assert commands.unknown_reply("/restrt", [commands.HELP]) == (
        "Unknown command /restrt. Try /help."
    )


def test_search_substring_then_fuzzy() -> None:
    cmds = commands.COMMANDS
    assert [c.name for c in commands.search("remind", cmds)] == [
        "/remind",
        "/reminders",
        "/unremind",
    ]
    assert [c.name for c in commands.search("unnote", cmds)] == ["/unnote"]
    assert [c.name for c in commands.search("remnd", cmds)] == ["/remind", "/unremind"]
    assert commands.search("qqqq", cmds) == []


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
        "subcommands": [],
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
        "subcommands": [],
    }
    assert by_name["/notes"]["level"] == "L0"
    assert by_name["/notes"]["subcommands"] == [
        {"word": "clear", "tool": "clear_notes", "level": "L1"}
    ]
    assert [(s["tool"], s["level"]) for s in by_name["/restart"]["subcommands"]] == [
        ("restart_ollama", "L3"),
        ("restart_resonant", "L2"),
    ]
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


# --- P2-15: argument parsers ---------------------------------------------------------------

# Tue 2026-10-13 3:00pm in Los Angeles (PDT, UTC-7).
CTX = commands.ParseContext(datetime(2026, 10, 13, 22, 0, tzinfo=UTC), "America/Los_Angeles")


def resolve(text: str) -> tuple[str | None, str | None, dict[str, Any]]:
    """``/name rest`` through the registry, the way the fast path splits it."""
    name, _, rest = text.partition(" ")
    cmd = commands.get(name)
    assert cmd is not None, name
    return cmd.resolve(rest.strip(), CTX)


def usage(text: str) -> str:
    name = text.split()[0]
    cmd = commands.get(name)
    assert cmd is not None
    with pytest.raises(UsageError) as e:
        resolve(text)
    return cmd.usage_reply(e.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/note buy milk", "buy milk"),
        ("/note   Buy Milk!  ", "Buy Milk!"),  # original case; punctuation is text
        ('/note "buy milk"', "buy milk"),  # one pair of surrounding quotes dropped
        ("/note “buy milk”", "buy milk"),
        ("/note 'it's fine'", "it's fine"),
        ('/note say "hi" to Bo', 'say "hi" to Bo'),  # inner quotes kept
        ("/note line one\nline two", "line one\nline two"),  # newlines kept
        ("/note " + "x" * 200, "x" * 200),
    ],
)
def test_note_text(text: str, expected: str) -> None:
    assert resolve(text) == (None, "add_note", {"text": expected})


@pytest.mark.parametrize(
    ("text", "reply"),
    [
        ("/note", "Usage: /note <text>"),
        ("/note   ", "Usage: /note <text>"),
        ('/note ""', "Usage: /note <text>"),
        ("/note " + "x" * 201, "that's 201 characters (at most 200). Usage: /note <text>"),
        ("/unnote", "say which note. Usage: /unnote <n>"),
        ("/unnote abc", "that's not a note number. Usage: /unnote <n>"),
        ("/unnote 0", "that's not a note number. Usage: /unnote <n>"),
        ("/unnote -1", "that's not a note number. Usage: /unnote <n>"),
        ("/unnote 1 2", "that's not a note number. Usage: /unnote <n>"),
        ("/unnote 1.5", "that's not a note number. Usage: /unnote <n>"),
        ("/unnote 1234567890", "that's not a note number. Usage: /unnote <n>"),
        ("/unremind x", "that's not a reminder number. Usage: /unremind <n>"),
        ("/notes delete", "Usage: /notes [clear]"),
        ("/notes clear all", "Usage: /notes [clear]"),
        ("/reminders soon", "Usage: /reminders"),
        ("/restart", "Usage: /restart ollama|resonant"),
        ("/restart everything", "Usage: /restart ollama|resonant"),
        ("/restart ollama now", "Usage: /restart ollama|resonant"),
        ("/update now", "Usage: /update"),
        ("/disk /", "Usage: /disk"),
        ("/logs 0", "say how many lines, like /logs 5. Usage: /logs [n]"),
        ("/logs -3", "say how many lines, like /logs 5. Usage: /logs [n]"),
        ("/logs many", "say how many lines, like /logs 5. Usage: /logs [n]"),
    ],
)
def test_bad_args_get_usage(text: str, reply: str) -> None:
    assert usage(text) == reply


Resolved = tuple[str | None, str | None, dict[str, Any]]
RESOLVED: list[tuple[str, Resolved]] = [
    ("/unnote 3", (None, "delete_note", {"id": 3})),
    ("/unnote #3", (None, "delete_note", {"id": 3})),
    ("/unnote 3.", (None, "delete_note", {"id": 3})),
    ("/notes", (None, "list_notes", {})),
    ("/notes clear", ("clear", "clear_notes", {})),
    ("/notes CLEAR", ("clear", "clear_notes", {})),
    ("/reminders", (None, "list_reminders", {})),
    ("/unremind 2", (None, "cancel_reminder", {"id": 2})),
    ("/restart ollama", ("ollama", "restart_ollama", {})),
    ("/restart Resonant", ("resonant", "restart_resonant", {})),
    ("/update", (None, "update_resonant", {})),
    ("/disk", (None, "disk", {})),
    ("/logs", (None, "recent_logs", {})),
    ("/logs 5", (None, "recent_logs", {"n": 5})),
    ("/logs 500", (None, "recent_logs", {"n": 20})),  # capped
]


@pytest.mark.parametrize(("text", "expected"), RESOLVED)
def test_resolve(text: str, expected: Resolved) -> None:
    assert resolve(text) == expected


@pytest.mark.parametrize(
    ("text", "at", "body"),
    [
        ("/remind in 20 min stretch", "2026-10-13T15:20:00-07:00", "stretch"),
        ("/remind 5pm call mom", "2026-10-13T17:00:00-07:00", "call mom"),
        ("/remind 17:30 call mom", "2026-10-13T17:30:00-07:00", "call mom"),
        (
            "/remind tomorrow 9am Take   the bins out",
            "2026-10-14T09:00:00-07:00",
            "Take the bins out",
        ),
        ("/remind fri at 3:30 pm pay rent", "2026-10-16T15:30:00-07:00", "pay rent"),
        ('/remind in 2h "stretch"', "2026-10-13T17:00:00-07:00", "stretch"),
    ],
)
def test_remind(text: str, at: str, body: str) -> None:
    sub, tool, args = resolve(text)
    assert (sub, tool) == (None, "add_reminder")
    assert args == {"at": at, "text": body, "routed_at": CTX.now.isoformat()}
    parsed = datetime.fromisoformat(args["at"])
    assert parsed.utcoffset() is not None and parsed > CTX.now


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("/remind", "say when, like 3pm, in 20 min or tomorrow 9am"),
        ("/remind call mom", "say when, like 3pm, in 20 min or tomorrow 9am"),
        ("/remind 3 call mom", "say 3pm or 15:00"),
        ("/remind 9:30 call mom", "say 3pm or 15:00"),
        ("/remind 9am call mom", "that time has passed"),  # 9am today, it's 3pm
        ("/remind yesterday 9am x", "that time has passed"),
        ("/remind in 20 min", "say what to remind you about"),
        ("/remind in 20 min " + "x" * 201, "that's 201 characters (at most 200)"),
    ],
)
def test_remind_bad_times_get_a_hint(text: str, hint: str) -> None:
    assert usage(text) == f"{hint}. Usage: /remind <when> <text>"


def test_remind_in_a_dst_gap_gets_a_hint() -> None:
    # Clocks spring forward at 2am on Sun 2027-03-14 in Los Angeles: 2:30am never happens.
    ctx = commands.ParseContext(datetime(2027, 3, 13, 20, 0, tzinfo=UTC), "America/Los_Angeles")
    cmd = commands.get("/remind")
    assert cmd is not None
    with pytest.raises(UsageError, match="doesn't exist"):
        cmd.resolve("sun 2:30am x", ctx)


def test_owner_only_commands() -> None:
    owner_only = {c.name for c in commands.COMMANDS if c.owner_only}
    assert owner_only == {
        "/restart",
        "/update",
        "/disk",
        "/logs",
        "/approvals",
        "/kill",
        "/resume",
    }

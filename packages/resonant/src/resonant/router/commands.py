"""Command registry: every slash command, defined once.

The single source for the fast path's slash commands, ``/help`` and ``/help <word>``, the
"Did you mean" suggestion for an unknown ``/word``, and the ``list_commands`` tool.

- A command names at most one tool. The level shown for a command always comes from the
  tool registry, never from the command.
- ``parse(rest)`` gets the text after the command word (stripped, original case) and returns
  the tool's args, or raises ``UsageError``. A bad argument gets the usage line and runs
  nothing.
- Fuzzy matching (``difflib``) only ever suggests. An unknown command never executes, even
  one letter away from a real one.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from resonant.gate.codes import CODE_DIGITS, CODE_LETTERS
from resonant.principals import Principals
from resonant.router.intents import IntentLabel
from resonant.tools.registry import ToolRegistry

SUGGEST_N = 3
SUGGEST_CUTOFF = 0.6
HELP_QUERY_MAX = 50
KEYWORDS_HINT = "Or just ask: what's running, how's the mini, ping."


class UsageError(ValueError):
    """Bad command arguments. The message, if any, is a short hint shown before the usage."""


Parse = Callable[[str], dict[str, Any]]


def no_args(rest: str) -> dict[str, Any]:
    if rest.rstrip("?!. "):
        raise UsageError
    return {}


def projects_args(rest: str) -> dict[str, Any]:
    word = _word(rest)
    if word == "all":
        return {"all": True}
    if word:
        raise UsageError
    return {}


def _word(text: str) -> str:
    """Lowercased, whitespace collapsed, a leading ``/`` and trailing ``?!.`` dropped."""
    t = " ".join(text.lower().split()).rstrip("?!. ")
    return t.removeprefix("/")


_CODE = re.compile(f"[{CODE_LETTERS}][{CODE_DIGITS}]", re.ASCII | re.IGNORECASE)


def approvals_args(rest: str) -> dict[str, Any]:
    """``/approvals [code]``: a code from the approval alphabet, uppercased."""
    word = _word(rest)
    if not word:
        return {}
    if not _CODE.fullmatch(word):
        raise UsageError("that's not an approval code")
    return {"code": word.upper()}


def help_args(rest: str) -> dict[str, Any]:
    query = _word(rest)
    if len(query) > HELP_QUERY_MAX:
        raise UsageError("that's too long to search for")
    return {"query": query} if query else {}


@dataclass(frozen=True)
class Command:
    name: str  # "/status"
    usage: str  # "/help [word]"
    description: str
    intent: IntentLabel
    tool: str | None = None  # a builtin (extension None); None: handled by the fast path
    parse: Parse = no_args
    owner_only: bool = False

    def usage_reply(self, err: UsageError | None = None) -> str:
        hint = str(err) if err is not None else ""
        usage = f"Usage: {self.usage}"
        return f"{hint}. {usage}" if hint else usage


HELP = Command(
    "/help",
    "/help [word]",
    "list commands, or the ones matching a word",
    "question",
    parse=help_args,
)

COMMANDS: tuple[Command, ...] = (
    Command("/status", "/status", "Resonant daemon status", "system", tool="daemon_status"),
    Command(
        "/projects",
        "/projects [all]",
        "open work and recent tasks",
        "job_control",
        tool="projects",
        parse=projects_args,
    ),
    Command(
        "/tasks",
        "/tasks [all]",
        "same as /projects",
        "job_control",
        tool="projects",
        parse=projects_args,
    ),
    Command(
        "/approvals",
        "/approvals [code]",
        "pending approvals; a code shows its details",
        "job_control",
        parse=approvals_args,
        owner_only=True,
    ),
    HELP,
    Command("/kill", "/kill", "stop autonomy and pause", "system", owner_only=True),
    Command("/resume", "/resume", "turn autonomy back on", "system", owner_only=True),
)
BY_NAME: dict[str, Command] = {c.name: c for c in COMMANDS}


def get(name: str) -> Command | None:
    return BY_NAME.get(name)


def suggest(word: str, commands: Iterable[Command] = COMMANDS) -> list[str]:
    """Up to three command names close to ``word`` ("/remnd" -> ["/remind"])."""
    names = {c.name.removeprefix("/"): c.name for c in commands}
    close = difflib.get_close_matches(_word(word), names, n=SUGGEST_N, cutoff=SUGGEST_CUTOFF)
    return [names[n] for n in close]


def unknown_reply(word: str | None) -> str:
    """The reply to an unknown ``/word``. It suggests; it never runs anything."""
    if word is None:
        return "Unknown command. Try /help."
    close = suggest(word)
    if not close:
        return f"Unknown command {word}. Try /help."
    return f"Unknown command {word}. Did you mean {' or '.join(close)}?"


def available(
    principal: str,
    *,
    principals: Principals,
    registry: ToolRegistry,
    commands: Iterable[Command] = COMMANDS,
) -> list[Command]:
    """The commands ``principal`` may use, in registry order.

    Owner-only commands need ``admin``. A command with a tool needs the tool registered and
    ``request`` on its extension. Commands with neither (``/help``) are for everyone.
    """
    out: list[Command] = []
    for c in commands:
        if c.owner_only and not principals.can(principal, "admin", None):
            continue
        if c.tool is not None:
            spec = registry.get(None, c.tool)
            if spec is None or not principals.can(principal, "request", spec.extension):
                continue
        out.append(c)
    return out


def search(query: str, commands: Iterable[Command]) -> list[Command]:
    """Commands matching ``query``: a substring of the name or description, else ``difflib``
    over names and description words. Order is kept."""
    cmds = list(commands)
    q = _word(query)
    hits = [c for c in cmds if q in c.name.removeprefix("/") or q in c.description.lower()]
    if hits or not q:
        return hits
    words = {c.name: [c.name.removeprefix("/"), *c.description.lower().split()] for c in cmds}
    vocab = sorted({w for ws in words.values() for w in ws})
    close = set(difflib.get_close_matches(q, vocab, n=SUGGEST_N, cutoff=SUGGEST_CUTOFF))
    return [c for c in cmds if close.intersection(words[c.name])]


def describe(cmd: Command, registry: ToolRegistry) -> dict[str, Any]:
    """JSON for ``list_commands``. ``level`` is the tool's registered level (None: no tool)."""
    spec = registry.get(None, cmd.tool) if cmd.tool is not None else None
    return {
        "name": cmd.name,
        "usage": cmd.usage,
        "description": cmd.description,
        "tool": cmd.tool,
        "level": spec.effective_level.value if spec is not None else None,
        "owner_only": cmd.owner_only,
    }


def help_text(commands: list[Command], query: str | None = None, *, keywords: bool) -> str:
    """``/help`` (all of ``commands``) or ``/help <word>`` (the matching ones)."""
    if query:
        commands = search(query, commands)
        if not commands:
            return f'No command matches "{query}". Try /help.'
    lines = [f"{c.usage}: {c.description}" for c in commands]
    head = "Commands:" if not query else f'Commands matching "{query}":'
    text = "\n".join([head, *lines])
    return f"{text}\n{KEYWORDS_HINT}" if keywords and not query else text

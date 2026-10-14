"""Command registry: every slash command, defined once.

The single source for the fast path's slash commands, ``/help`` and ``/help <word>``, the
"Did you mean" suggestion for an unknown ``/word``, and the ``list_commands`` tool.

- A command names at most one tool. The level shown for a command always comes from the
  tool registry, never from the command.
- ``parse(rest, ctx)`` gets the text after the command word (stripped, original case) and a
  ``ParseContext`` (the clock and the owner's timezone, for ``/remind``), and returns the
  tool's args, or raises ``UsageError``. A bad argument gets the usage line and runs nothing.
- A command may have subcommands (``/notes clear``, ``/restart ollama``): a fixed first word
  that picks another tool, with its own parser. ``resolve(rest, ctx)`` returns the tool and
  args. A subcommand word is code's, so it may appear in a task title; free text never does.
- Fuzzy matching (``difflib``) only ever suggests. An unknown command never executes, even
  one letter away from a real one.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from resonant.gate.codes import CODE_DIGITS, CODE_LETTERS
from resonant.principals import Principals
from resonant.router.intents import IntentLabel
from resonant.store.db import utcnow
from resonant.tools.notes import TEXT_MAX as NOTE_TEXT_MAX
from resonant.tools.ops import LOGS_MAX
from resonant.tools.registry import ToolRegistry
from resonant.tools.reminders import TEXT_MAX as REMINDER_TEXT_MAX
from resonant.tools.when import WhenError, split_when

SUGGEST_N = 3
SUGGEST_CUTOFF = 0.6
SUGGEST_MARGIN = 0.1  # "/remnd": /remind, not also /unremind and /reminders
HELP_QUERY_MAX = 50
KEYWORDS_HINT = "Or just ask: what's running, how's the mini, ping."


class UsageError(ValueError):
    """Bad command arguments. The message, if any, is a short hint shown before the usage."""


@dataclass(frozen=True)
class ParseContext:
    """What a parser may need besides the text: the clock (aware, UTC) and the owner's zone."""

    now: datetime = field(default_factory=utcnow)
    tz: str = "UTC"


Parse = Callable[[str, ParseContext], dict[str, Any]]


def no_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    if rest.rstrip("?!. "):
        raise UsageError
    return {}


def projects_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
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


def approvals_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    """``/approvals [code]``: a code from the approval alphabet, uppercased."""
    word = _word(rest)
    if not word:
        return {}
    if not _CODE.fullmatch(word):
        raise UsageError("that's not an approval code")
    return {"code": word.upper()}


def help_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    query = _word(rest)
    if len(query) > HELP_QUERY_MAX:
        raise UsageError("that's too long to search for")
    return {"query": query} if query else {}


_QUOTES = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}


def _text(rest: str, limit: int) -> str:
    """Free text as typed (inner newlines kept), one pair of surrounding quotes dropped."""
    text = rest.strip()
    if len(text) >= 2 and _QUOTES.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    if not text:
        raise UsageError
    if len(text) > limit:
        raise UsageError(f"that's {len(text)} characters (at most {limit})")
    return text


def note_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    """``/note <text>``: the text, at most 200 characters."""
    return {"text": _text(rest, NOTE_TEXT_MAX)}


_NUMBER = re.compile(r"#?(\d{1,9})")


def _number(rest: str, what: str) -> int:
    """A positive number (``3`` or ``#3``)."""
    word = _word(rest)
    m = _NUMBER.fullmatch(word)
    if not m or int(m.group(1)) < 1:
        raise UsageError(f"that's not a {what} number" if word else f"say which {what}")
    return int(m.group(1))


def unnote_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    """``/unnote <n>``: a note number (``3`` or ``#3``)."""
    return {"id": _number(rest, "note")}


def unremind_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    """``/unremind <n>``: a reminder number (``3`` or ``#3``)."""
    return {"id": _number(rest, "reminder")}


def remind_args(rest: str, ctx: ParseContext) -> dict[str, Any]:
    """``/remind <when> <text>``: the longest leading words that parse as a time
    (``tools/when.py``, in the owner's zone), then the text. ``at`` is ISO with an offset."""
    try:
        at, text = split_when(rest, now=ctx.now, tz=ctx.tz)
    except WhenError as e:
        raise UsageError(str(e)) from None
    return {"at": at.isoformat(), "text": _text(text, REMINDER_TEXT_MAX)}


def logs_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    """``/logs [n]``: any positive number of lines, capped at 20 (the handler caps too)."""
    if not _word(rest):
        return {}
    m = _NUMBER.fullmatch(_word(rest))
    if not m or int(m.group(1)) < 1:
        raise UsageError("say how many lines, like /logs 5")
    return {"n": min(int(m.group(1)), LOGS_MAX)}


@dataclass(frozen=True)
class Sub:
    """A subcommand: a fixed word after the command (``clear`` in ``/notes clear``)."""

    word: str
    tool: str
    parse: Parse = no_args


@dataclass(frozen=True)
class Command:
    name: str  # "/status"
    usage: str  # "/help [word]"
    description: str
    intent: IntentLabel
    tool: str | None = None  # a builtin (extension None); None: handled by the fast path
    parse: Parse = no_args
    owner_only: bool = False
    subs: tuple[Sub, ...] = ()

    @property
    def tools(self) -> tuple[str, ...]:
        """Every tool the command can call: its own, then its subcommands'."""
        own = (self.tool,) if self.tool is not None else ()
        return (*own, *(s.tool for s in self.subs))

    def resolve(
        self, rest: str, ctx: ParseContext
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        """``(subcommand word, tool, args)`` for ``rest``, or ``UsageError``.

        The first word picks a subcommand (case-insensitive) and the rest goes to its parser.
        Otherwise the command's own parser gets all of ``rest``; a command with no tool of
        its own needs a subcommand.
        """
        if self.subs:
            parts = rest.split(maxsplit=1)
            first = _word(parts[0]) if parts else ""
            for sub in self.subs:
                if first == sub.word:
                    return sub.word, sub.tool, sub.parse(parts[1] if len(parts) == 2 else "", ctx)
            if self.tool is None:
                raise UsageError
        return None, self.tool, self.parse(rest, ctx)

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

# Notes and reminders have no intent label of their own yet (P2-16 adds them).
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
    Command("/note", "/note <text>", "save a note", "unknown", tool="add_note", parse=note_args),
    Command(
        "/notes",
        "/notes [clear]",
        "your notes; clear deletes them all (asks first)",
        "unknown",
        tool="list_notes",
        subs=(Sub("clear", "clear_notes"),),
    ),
    Command(
        "/unnote", "/unnote <n>", "delete note n", "unknown", tool="delete_note", parse=unnote_args
    ),
    Command(
        "/remind",
        "/remind <when> <text>",
        "set a reminder: 3pm, in 20 min, tomorrow 9am, fri 5pm",
        "unknown",
        tool="add_reminder",
        parse=remind_args,
    ),
    Command(
        "/reminders",
        "/reminders",
        "pending reminders, soonest first",
        "unknown",
        tool="list_reminders",
    ),
    Command(
        "/unremind",
        "/unremind <n>",
        "cancel reminder n",
        "unknown",
        tool="cancel_reminder",
        parse=unremind_args,
    ),
    Command(
        "/restart",
        "/restart ollama|resonant",
        "restart the model server or Resonant itself",
        "system",
        owner_only=True,
        subs=(Sub("ollama", "restart_ollama"), Sub("resonant", "restart_resonant")),
    ),
    Command(
        "/update",
        "/update",
        "pull Resonant from GitHub and restart",
        "system",
        tool="update_resonant",
        owner_only=True,
    ),
    Command("/disk", "/disk", "free disk space", "system", tool="disk", owner_only=True),
    Command(
        "/logs",
        "/logs [n]",
        f"the last n log lines (at most {LOGS_MAX})",
        "system",
        tool="recent_logs",
        parse=logs_args,
        owner_only=True,
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
    """Up to three command names close to ``word`` ("/remnd" -> ["/remind"]): the
    ``difflib`` matches within ``SUGGEST_MARGIN`` of the closest, best first."""
    names = {c.name.removeprefix("/"): c.name for c in commands}
    w = _word(word)
    close = difflib.get_close_matches(w, names, n=SUGGEST_N, cutoff=SUGGEST_CUTOFF)
    if not close:
        return []
    best = difflib.SequenceMatcher(None, w, close[0]).ratio()
    near = [
        n for n in close if difflib.SequenceMatcher(None, w, n).ratio() >= best - SUGGEST_MARGIN
    ]
    return [names[n] for n in near]


def unknown_reply(word: str | None, commands: Iterable[Command] = COMMANDS) -> str:
    """The reply to an unknown ``/word``. It suggests from ``commands`` (pass the asker's
    ``available`` ones, so a member is never pointed at an owner-only command); it never
    runs anything."""
    if word is None:
        return "Unknown command. Try /help."
    close = suggest(word, commands)
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

    Owner-only commands need ``admin``. A command with tools needs each of them registered
    and ``request`` on its extension. Commands with neither (``/help``) are for everyone.
    """
    out: list[Command] = []
    for c in commands:
        if c.owner_only and not principals.can(principal, "admin", None):
            continue
        if not all(
            can_request(principal, t, principals=principals, registry=registry) for t in c.tools
        ):
            continue
        out.append(c)
    return out


def can_request(
    principal: str, tool: str, *, principals: Principals, registry: ToolRegistry
) -> bool:
    """``tool`` is a registered builtin that ``principal`` may request."""
    spec = registry.get(None, tool)
    return spec is not None and principals.can(principal, "request", spec.extension)


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
    """JSON for ``list_commands``. ``level`` is the tool's registered level (None: no tool);
    each subcommand lists its own."""

    def level(tool: str | None) -> str | None:
        spec = registry.get(None, tool) if tool is not None else None
        return spec.effective_level.value if spec is not None else None

    return {
        "name": cmd.name,
        "usage": cmd.usage,
        "description": cmd.description,
        "tool": cmd.tool,
        "level": level(cmd.tool),
        "owner_only": cmd.owner_only,
        "subcommands": [{"word": s.word, "tool": s.tool, "level": level(s.tool)} for s in cmd.subs],
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

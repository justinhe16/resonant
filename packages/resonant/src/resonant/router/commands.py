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

Extensions (Phase 3). ``commands_for(extensions, registry)`` is ``COMMANDS`` plus one
``/<ext>`` command per loaded extension, built once at start like the intents. Each of the
extension's chat tools (never a job spec, never a name that shadows a builtin tool) is a
subcommand: ``/github pr resonant 40``. Arguments are positional in the manifest's declared
order, quote-aware and typed (``ext_tool_args``); a one-argument tool takes the rest of the
text. ``/help <ext>`` lists the tools with their descriptions (cleaned) and levels. ``/ext``
(owner-only) lists extensions, runs a job now and mutes notifications.
"""

from __future__ import annotations

import difflib
import logging
import math
import re
import shlex
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, cast

from resonant.gate.codes import CODE_DIGITS, CODE_LETTERS, clean_external
from resonant.principals import Principals
from resonant.router.intents import ExtensionCatalog, IntentLabel, extension_intent
from resonant.store.db import utcnow
from resonant.tools.notes import TEXT_MAX as NOTE_TEXT_MAX
from resonant.tools.ops import LOGS_MAX
from resonant.tools.registry import ToolRegistry
from resonant.tools.reminders import TEXT_MAX as REMINDER_TEXT_MAX
from resonant.tools.runs import ASK_MAX, RUN_CODE
from resonant.tools.when import WhenError, split_when, zone
from resonant_sdk import ToolSpec
from resonant_sdk.manifest import SLUG

log = logging.getLogger(__name__)

SUGGEST_N = 3
SUGGEST_CUTOFF = 0.6
SUGGEST_MARGIN = 0.1  # "/remnd": /remind, not also /unremind and /reminders
HELP_QUERY_MAX = 50
KEYWORDS_HINT = "Instant, no model: /status, /tasks, ping. Or just ask in plain words."
EXT_ARG_MAX = 1000  # the process runner's limit for one value
EXT_DESCRIPTION_MAX = 120
MUTE_DEFAULT = timedelta(hours=1)
MUTE_MAX = timedelta(days=7)
MUTE_TOMORROW_AT = time(8, 0)  # "until tomorrow": 8:00am tomorrow, local


class UsageError(ValueError):
    """Bad command arguments. The message, if any, is a short hint shown before the usage.

    ``usage`` overrides the command's usage line (a subcommand's own); ``reply`` replaces
    the whole reply (an unknown extension tool's suggestion)."""

    def __init__(self, hint: str = "", *, usage: str | None = None, reply: str | None = None):
        super().__init__(hint)
        self.usage = usage
        self.reply = reply


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


_NUMBER = re.compile(r"#?(\d{1,9})", re.ASCII)


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
    (``tools/when.py``, in the owner's zone), then the text. ``at`` is ISO with an offset;
    ``routed_at`` is the time it was resolved against, so a late-running task still takes
    it (``tools/reminders.py``)."""
    try:
        at, text = split_when(rest, now=ctx.now, tz=ctx.tz)
    except WhenError as e:
        raise UsageError(str(e)) from None
    return {
        "at": at.isoformat(),
        "text": _text(text, REMINDER_TEXT_MAX),
        "routed_at": ctx.now.isoformat(),
    }


def logs_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    """``/logs [n]``: any positive number of lines, capped at 20 (the handler caps too)."""
    if not _word(rest):
        return {}
    m = _NUMBER.fullmatch(_word(rest))
    if not m or int(m.group(1)) < 1:
        raise UsageError("say how many lines, like /logs 5")
    return {"n": min(int(m.group(1)), LOGS_MAX)}


_REPO = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.ASCII)


def run_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    """``/run <repo> <ask>``: a repo name (lowercased) and the ask as typed. Whether the repo
    is configured is the tool's to say."""
    parts = rest.split(maxsplit=1)
    if not parts:
        raise UsageError
    repo = parts[0].lower()
    if not _REPO.fullmatch(repo):
        raise UsageError("that's not a repo name")
    if len(parts) < 2:
        raise UsageError("say what to do")
    return {"repo": repo, "ask": _text(parts[1], ASK_MAX)}


def _run_code(rest: str) -> str:
    word = _word(rest)
    m = RUN_CODE.fullmatch(word)
    if not m:
        raise UsageError("that's not a run code like R3" if word else "say which run, like R3")
    return f"R{int(m.group(1))}"


def runs_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    """``/runs [code]``."""
    return {"code": _run_code(rest)} if _word(rest) else {}


def run_code_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    """``/cancel <code>`` and ``/merge <code>``."""
    return {"code": _run_code(rest)}


@dataclass(frozen=True)
class Sub:
    """A subcommand: a fixed word after the command (``clear`` in ``/notes clear``).

    ``tool`` None: the fast path handles it (``/ext run``). ``usage`` is its own usage line;
    ``description`` and ``level`` are shown by ``/help <ext>`` (an extension tool's)."""

    word: str
    tool: str | None
    parse: Parse = no_args
    usage: str = ""
    description: str = ""
    level: str = ""


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
    extension: str | None = None  # an ``/<ext>`` command: every tool is that extension's

    @property
    def tools(self) -> tuple[str, ...]:
        """Every tool the command can call: its own, then its subcommands'."""
        own = (self.tool,) if self.tool is not None else ()
        return (*own, *(s.tool for s in self.subs if s.tool is not None))

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
                    try:
                        args = sub.parse(parts[1] if len(parts) == 2 else "", ctx)
                    except UsageError as e:
                        if e.usage is None and sub.usage:
                            e.usage = sub.usage
                        raise
                    return sub.word, sub.tool, args
            if self.extension is not None and first:
                raise UsageError(reply=self._unknown_sub(first))
            if self.tool is None:
                raise UsageError
        return None, self.tool, self.parse(rest, ctx)

    def _unknown_sub(self, word: str) -> str:
        """``Unknown command /github prz. Did you mean /github prs?``: suggests only."""
        shown = f"{self.name} {word}" if _SUB_WORD.fullmatch(word) else self.name
        close = _closest(word, [s.word for s in self.subs])
        if not close:
            return f"Unknown command {shown}. Try /help {self.name.removeprefix('/')}."
        options = " or ".join(f"{self.name} {w}" for w in close)
        return f"Unknown command {shown}. Did you mean {options}?"

    def usage_reply(self, err: UsageError | None = None) -> str:
        if err is not None and err.reply is not None:
            return err.reply
        hint = str(err) if err is not None else ""
        usage = f"Usage: {(err.usage if err is not None else None) or self.usage}"
        return f"{hint}. {usage}" if hint else usage


_SUB_WORD = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.ASCII)


# --- /ext -----------------------------------------------------------------------------------

_SLUG = re.compile(SLUG, re.ASCII)
_JOB_REF = re.compile(r"([a-z][a-z0-9-]{0,47})/([a-z][a-z0-9-]{0,47})", re.ASCII)
_DURATION = re.compile(r"(\d{1,4}) ?(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)", re.ASCII)
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def ext_run_args(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
    """``/ext run <ext>/<job>``: two slugs (``core/briefing`` is a core job)."""
    m = _JOB_REF.fullmatch(_word(rest))
    if not m:
        raise UsageError("say which job, like github/watch" if not _word(rest) else "")
    return {"extension": m.group(1), "job": m.group(2)}


def mute_args(rest: str, ctx: ParseContext) -> dict[str, Any]:
    """``/ext mute <ext> [1h|30m|2d|until tomorrow|off]``: ``until`` is computed now (ISO,
    UTC), at most ``MUTE_MAX`` ahead; no duration is ``MUTE_DEFAULT``. ``until tomorrow``
    is 8:00am tomorrow in the owner's zone."""
    words = _word(rest).split(" ", 1)
    name = words[0]
    if not name:
        raise UsageError("say which extension")
    if not _SLUG.fullmatch(name):
        raise UsageError("that's not an extension name")
    when = words[1].removeprefix("for ").strip() if len(words) == 2 else ""
    if when == "off":
        return {"extension": name, "off": True}
    if when in ("until tomorrow", "tomorrow"):
        tz = zone(ctx.tz)
        day = ctx.now.astimezone(tz).date() + timedelta(days=1)
        until = datetime.combine(day, MUTE_TOMORROW_AT, tzinfo=tz)
    elif not when:
        until = ctx.now + MUTE_DEFAULT
    else:
        m = _DURATION.fullmatch(when)
        if not m or int(m.group(1)) < 1:
            raise UsageError("say how long, like 1h, 30m or until tomorrow")
        delta = timedelta(**{_UNITS[m.group(2)[0]]: int(m.group(1))})
        if delta > MUTE_MAX:
            raise UsageError("at most 7 days")
        until = ctx.now + delta
    return {"extension": name, "until": until.astimezone(ctx.now.tzinfo).isoformat()}


EXT = Command(
    "/ext",
    "/ext list|run|mute",
    "extensions; run <ext>/<job> now; mute <ext> [1h|until tomorrow|off]",
    "system",
    owner_only=True,
    subs=(
        Sub("list", "list_extensions", usage="/ext list"),
        Sub("run", None, ext_run_args, usage="/ext run <ext>/<job>"),
        Sub("mute", "mute_extension", mute_args, usage="/ext mute <ext> [1h|until tomorrow|off]"),
    ),
)


PULSE = Command(
    "/pulse",
    "/pulse [now]",
    "what the next update would say and what's open; now sends it",
    "updates",
    tool="pulse_preview",
    owner_only=True,
    subs=(Sub("now", None, usage="/pulse now"),),
)

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
    Command("/note", "/note <text>", "save a note", "notes", tool="add_note", parse=note_args),
    Command(
        "/notes",
        "/notes [clear]",
        "your notes; clear deletes them all (asks first)",
        "notes",
        tool="list_notes",
        subs=(Sub("clear", "clear_notes"),),
    ),
    Command(
        "/unnote", "/unnote <n>", "delete note n", "notes", tool="delete_note", parse=unnote_args
    ),
    Command(
        "/remind",
        "/remind <when> <text>",
        "set a reminder: 3pm, in 20 min, tomorrow 9am, fri 5pm",
        "reminders",
        tool="add_reminder",
        parse=remind_args,
    ),
    Command(
        "/reminders",
        "/reminders",
        "pending reminders, soonest first",
        "reminders",
        tool="list_reminders",
    ),
    Command(
        "/unremind",
        "/unremind <n>",
        "cancel reminder n",
        "reminders",
        tool="cancel_reminder",
        parse=unremind_args,
    ),
    Command(
        "/restart",
        "/restart ollama|resonant",
        "restart the model server or Resonant itself",
        "ops",
        owner_only=True,
        subs=(Sub("ollama", "restart_ollama"), Sub("resonant", "restart_resonant")),
    ),
    Command(
        "/update",
        "/update",
        "pull Resonant from GitHub and restart",
        "ops",
        tool="update_resonant",
        owner_only=True,
    ),
    Command("/disk", "/disk", "free disk space", "ops", tool="disk", owner_only=True),
    Command(
        "/logs",
        "/logs [n]",
        f"the last n log lines (at most {LOGS_MAX})",
        "ops",
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
    Command(
        "/run",
        "/run <repo> <ask>",
        "have Claude work on a repo and open a PR",
        "run_project",
        tool="start_run",
        parse=run_args,
        owner_only=True,
    ),
    Command(
        "/runs",
        "/runs [R3]",
        "Claude runs, active first; a code shows one",
        "run_project",
        tool="list_runs",
        parse=runs_args,
        owner_only=True,
    ),
    Command(
        "/cancel",
        "/cancel <R3>",
        "stop a run and discard its work",
        "run_project",
        tool="cancel_run",
        parse=run_code_args,
        owner_only=True,
    ),
    Command(
        "/merge",
        "/merge <R3>",
        "squash-merge a finished run's PR (CI must be green)",
        "run_project",
        tool="merge_run",
        parse=run_code_args,
        owner_only=True,
    ),
    EXT,
    PULSE,
    HELP,
    Command("/kill", "/kill", "stop autonomy and pause", "system", owner_only=True),
    Command("/resume", "/resume", "turn autonomy back on", "system", owner_only=True),
)
BY_NAME: dict[str, Command] = {c.name: c for c in COMMANDS}


def get(name: str, by_name: dict[str, Command] = BY_NAME) -> Command | None:
    return by_name.get(name)


def _closest(word: str, options: Iterable[str]) -> list[str]:
    """Up to three of ``options`` close to ``word``: the ``difflib`` matches within
    ``SUGGEST_MARGIN`` of the closest, best first."""
    w = _word(word)
    close = difflib.get_close_matches(w, list(options), n=SUGGEST_N, cutoff=SUGGEST_CUTOFF)
    if not close:
        return []
    best = difflib.SequenceMatcher(None, w, close[0]).ratio()
    return [
        n for n in close if difflib.SequenceMatcher(None, w, n).ratio() >= best - SUGGEST_MARGIN
    ]


def suggest(word: str, commands: Iterable[Command] = COMMANDS) -> list[str]:
    """Up to three command names close to ``word`` ("/remnd" -> ["/remind"])."""
    names = {c.name.removeprefix("/"): c.name for c in commands}
    return [names[n] for n in _closest(word, names)]


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
            can_request(
                principal, t, principals=principals, registry=registry, extension=c.extension
            )
            for t in c.tools
        ):
            continue
        out.append(c)
    return out


def can_request(
    principal: str,
    tool: str,
    *,
    principals: Principals,
    registry: ToolRegistry,
    extension: str | None = None,
) -> bool:
    """``tool`` (a builtin, or ``extension``'s) is registered and ``principal`` may request it."""
    spec = registry.get(extension, tool)
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
        spec = registry.get(cmd.extension, tool) if tool is not None else None
        return spec.effective_level.value if spec is not None else None

    return {
        "name": cmd.name,
        "usage": cmd.usage,
        "description": cmd.description,
        "tool": cmd.tool,
        "level": level(cmd.tool),
        "owner_only": cmd.owner_only,
        "extension": cmd.extension,
        "subcommands": [{"word": s.word, "tool": s.tool, "level": level(s.tool)} for s in cmd.subs],
    }


def help_text(commands: list[Command], query: str | None = None, *, keywords: bool) -> str:
    """``/help`` (all of ``commands``) or ``/help <word>`` (the matching ones). ``/help
    <ext>`` for an extension command the asker may use lists its tools."""
    if query:
        ext = next((c for c in commands if c.extension and c.name == f"/{_word(query)}"), None)
        if ext is not None:
            return extension_help(ext)
        commands = search(query, commands)
        if not commands:
            return f'No command matches "{query}". Try /help.'
    lines = [f"{c.usage}: {c.description}" for c in commands]
    head = "Commands:" if not query else f'Commands matching "{query}":'
    text = "\n".join([head, *lines])
    return f"{text}\n{KEYWORDS_HINT}" if keywords and not query else text


def extension_help(cmd: Command) -> str:
    """``/help github``: the extension's description, then one line per tool:
    ``/github pr <repo> <number>: One pull request with its checks. (L0)``."""
    lines = [
        f"{s.usage}: {s.description} ({s.level})" if s.description else f"{s.usage} ({s.level})"
        for s in cmd.subs
    ]
    return "\n".join([f"{cmd.name}: {cmd.description}", *lines])


# --- /<ext> <tool> --------------------------------------------------------------------------

_SMART_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})
_INT = re.compile(r"[+-]?#?\d{1,15}", re.ASCII)
_TRUE = {"true", "yes", "y", "on", "1"}
_FALSE = {"false", "no", "n", "off", "0"}


def _typed(name: str, kind: object, value: str) -> object:
    """``value`` as the manifest type ``kind`` (``string``, ``integer``, ``number``,
    ``boolean``), or ``UsageError`` naming the argument."""
    if kind == "integer":
        if not _INT.fullmatch(value):
            raise UsageError(f"{name} must be a whole number")
        return int(value.replace("#", ""))
    if kind == "number":
        try:
            number = float(value)
        except ValueError:
            number = math.nan
        if not math.isfinite(number):
            raise UsageError(f"{name} must be a number")
        return number
    if kind == "boolean":
        if value.lower() in _TRUE | _FALSE:
            return value.lower() in _TRUE
        raise UsageError(f"{name} must be yes or no")
    if not value or "\x00" in value:
        raise UsageError(f"{name} can't be empty")
    if len(value) > EXT_ARG_MAX:
        raise UsageError(f"{name} is {len(value)} characters (at most {EXT_ARG_MAX})")
    return value


def arg_names(spec: ToolSpec) -> list[tuple[str, object]]:
    """``(name, type)`` per argument, in the manifest's declared order."""
    props: object = spec.args_schema.get("properties")
    if not isinstance(props, dict):
        return []
    out: list[tuple[str, object]] = []
    for name, schema in cast(dict[object, object], props).items():
        kind = cast(dict[object, object], schema).get("type") if isinstance(schema, dict) else None
        out.append((str(name), kind))
    return out


def ext_tool_args(spec: ToolSpec) -> Parse:
    """The parser for ``/<ext> <tool> [args]``: positional values in declared order, quoted
    with ``"`` or ``'`` (curly quotes too), typed per the manifest. A one-argument tool takes
    the rest of the text (one pair of surrounding quotes dropped)."""
    names = arg_names(spec)
    required: object = spec.args_schema.get("required")
    needed = set(cast(list[str], required)) if isinstance(required, list) else set[str]()
    # Values fill the declared order; the fewest is up to the last required argument.
    least = max((i + 1 for i, (n, _) in enumerate(names) if n in needed), default=0)

    def parse(rest: str, ctx: ParseContext | None = None) -> dict[str, Any]:
        text = rest.strip()
        if not names or (not text and least == 0):
            return no_args(text)
        if not text:
            raise UsageError
        if len(names) == 1:
            name, kind = names[0]
            value = _text(text, EXT_ARG_MAX) if kind == "string" else text
            return {name: _typed(name, kind, value)}
        words = _split(text)
        if not least <= len(words) <= len(names):
            want = str(len(names)) if least == len(names) else f"{least} to {len(names)}"
            raise UsageError(f"want {want} values, got {len(words)}")
        return {n: _typed(n, k, w) for (n, k), w in zip(names, words, strict=False)}

    return parse


def _split(text: str) -> list[str]:
    """Words, with ``"..."`` or ``'...'`` (curly quotes too) grouping; no backslash escapes."""
    lexer = shlex.shlex(text.translate(_SMART_QUOTES), posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        raise UsageError("a quote isn't closed") from None


def ext_tool_usage(ext: str, spec: ToolSpec) -> str:
    """``/github pr <repo> <number>``."""
    required: object = spec.args_schema.get("required")
    needed = set(cast(list[str], required)) if isinstance(required, list) else set[str]()
    args = (f"<{n}>" if n in needed else f"[<{n}>]" for n, _ in arg_names(spec))
    return " ".join([f"/{ext} {spec.name}", *args])


def _clean(text: str, limit: int = EXT_DESCRIPTION_MAX) -> str:
    flat = clean_external(text)
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def extension_command(
    ext: str, description: str, specs: Sequence[ToolSpec], builtin_tools: frozenset[str]
) -> Command | None:
    """``/<ext>`` with one subcommand per chat tool, by name. A tool named like a builtin
    tool is left out (as in chat); no tools left: None."""
    subs: list[Sub] = []
    for spec in sorted(specs, key=lambda s: s.name):
        if spec.extension != ext:
            continue
        if spec.name in builtin_tools or not _SUB_WORD.fullmatch(spec.name):
            log.warning(
                "extension tool not offered as a command",
                extra={"extension": ext, "tool": spec.name},
            )
            continue
        subs.append(
            Sub(
                spec.name,
                spec.name,
                ext_tool_args(spec),
                usage=ext_tool_usage(ext, spec),
                description=_clean(spec.description or ""),
                level=spec.effective_level.value,
            )
        )
    if not subs:
        return None
    return Command(
        f"/{ext}",
        f"/{ext} <tool> [args]",
        _clean(description) or f"the {ext} extension",
        extension_intent(ext),
        subs=tuple(subs),
        extension=ext,
    )


def commands_for(
    extensions: ExtensionCatalog | None, registry: ToolRegistry
) -> tuple[Command, ...]:
    """``COMMANDS`` plus one ``/<ext>`` per loaded extension with chat tools, by name. Job
    specs never become commands. An extension named like a builtin command is skipped
    (``ext validate`` refuses that name already)."""
    if extensions is None:
        return COMMANDS
    builtin_tools = frozenset(s.name for s in registry.specs() if s.extension is None)
    specs = [s for s in extensions.tool_specs() if not extensions.is_job(s)]
    out = list(COMMANDS)
    for ext in sorted({s.extension for s in specs if s.extension is not None}):
        if f"/{ext}" in BY_NAME:
            log.warning(
                "extension shadows a builtin command; not offered", extra={"extension": ext}
            )
            continue
        manifest = extensions.manifest(ext)
        cmd = extension_command(ext, manifest.description if manifest else "", specs, builtin_tools)
        if cmd is not None:
            out.append(cmd)
    return tuple(out)


def by_name(commands: Iterable[Command]) -> dict[str, Command]:
    return {c.name: c for c in commands}

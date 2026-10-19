"""Deterministic fast path: slash commands and the ``status`` / ``ping`` keywords, no model call.

Code computes and text narrates: a read command runs one builtin read tool here, and the
reply is rendered from a template over the tool's output. A side-effecting command runs
nothing here: it becomes a ``direct_call`` task for the local runner (below).

- Slash commands come from the command registry (``router/commands.py``): the first word
  names the command and the rest is its argument text, parsed deterministically (with the
  clock and the owner's zone, for ``/remind``). A bad argument gets the usage line.
  ``/help [word]`` lists the commands the asker may use. Any other ``/word`` gets "Unknown
  command", with a "Did you mean" suggestion from the commands the asker may use. None of
  these reach the model, and a suggestion never runs anything.
- Permission comes before arguments: an owner-only command from anyone else gets "not
  allowed" (audited), and so does a command whose tools the asker can't request, whatever
  the arguments.
- **Side-effecting commands** (``/note``, ``/remind``, ``/restart``, ``/update``, ...) return
  ``FastPathResult.direct_call = {tool, args}``; the router turns it into
  ``NewTask(kind="command", runner="local", checkpoint={"direct_call": ..., "title": ...})``.
  The local runner pins exactly that call and gates it with ``origin="explicit"``, the only
  way a call gets that origin. So explicit consent comes only from this deterministic
  parser, never from a fuzzy match or the model. The gate still decides: L3 runs, L2 runs
  for the owner, L1 asks for a code, the kill switch asks for everything, dry_run holds back
  the ops tools.
- Keywords (the whole message, case-insensitive, trailing ``?!.`` ignored): ``status`` and
  ``ping``, nothing else. Every other plain-words message goes through an intent, so each
  read command also has a chat tool (``what's running`` is ``job_control``, ``any updates``
  is ``updates``); a test checks that no read command lacks one.

``/approvals`` lists the asker's pending approvals (code, summary, expiry);
``/approvals <code>`` adds the intent's full args, for the owner only. Approval *replies*
(``yes``, ``no A7``) are not fast-path commands: the router hands them to
``router/approvals.py`` after slash commands.

**Extensions** (Phase 3). ``/<ext> <tool> [args]`` (from ``commands.commands_for``) always
becomes ``direct_call = {extension, tool, args}``, reads included: extension tools run
through the executor, which injects their secrets and redacts. A member needs ``request``
on the extension, checked before arguments. ``/ext`` is owner-only: ``/ext list`` reads
inline (``list_extensions``); ``/ext mute`` is a direct call of ``mute_extension`` (L3);
``/ext run <ext>/<job>`` is a direct call of the registered ``job_<id>`` spec with
``{due_at: now}``, so the runner gates it as explicit at the job's own level. A job spec
is never reachable as ``/<ext> job_x``. ``/ext run core/<job>`` calls the core job's
on-demand entry (``core_jobs``, filled by ``wiring``: ``core/briefing`` is
``briefing.run_briefing_now``) here, with no task; nothing runs while paused.

``/pulse`` (owner-only) is a read of ``pulse_preview``, the same tool the ``updates`` intent
offers: what the next pulse would say plus the open items per source (read-only; core
re-mentions appear once a pulse run computes them). ``/pulse now`` is
``core/pulse`` on demand, like ``/ext run core/pulse``. An unknown ``/word`` that names an
extension shipped in ``<repo>/extensions/<word>/resonant.yaml`` gets, for the owner, "<word>
isn't installed. On the Mini: resonant ext add repo:<word>" (or "is disabled ... ext update");
nothing runs.

``/kill`` and ``/resume`` are never decided by the LLM. A non-owner gets "not allowed".
A slash command whose tool the principal can't request gets "not allowed"; a keyword in
that case falls through to the labeler (``run`` returns None).

Tool reads go through the gate (registration and permission checks, audited) like any other
call. No task exists yet at route time, so the fast path consumes the Allow token and calls
the builtin read handler itself instead of going through the executor: builtins are L0
reads with no side effect, so there is nothing for the executor's idempotency keys to
protect. The intent's ``task_id`` is ``route:<event id>`` in audit rows.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from resonant import killswitch
from resonant.executor import HandlerKey, ToolHandler
from resonant.extensions.briefing import run_briefing_now
from resonant.extensions.pulse_job import run_pulse_now
from resonant.gate import Allow, Decision, Intent, PendingApproval
from resonant.models import SpanFactory
from resonant.models.client import null_spans
from resonant.principals import Principals
from resonant.router import commands
from resonant.router.approvals import approvals_reply
from resonant.router.commands import Command, ParseContext, UsageError
from resonant.router.intents import ExtensionCatalog, IntentLabel
from resonant.router.render import as_dict as _as_dict
from resonant.router.render import as_list as _as_list
from resonant.router.render import duration as _duration
from resonant.router.render import (
    render_disk,
    render_extensions,
    render_logs,
    render_notes,
    render_projects,
    render_reminders,
    render_repos,
    render_rules,
    render_runs,
)
from resonant.store import extensions as ext_store
from resonant.store import repos as repo_store
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, utcnow
from resonant.tools import ToolRegistry
from resonant_sdk import Effect

if TYPE_CHECKING:
    from resonant.extensions.scheduler import CoreDeps

log = logging.getLogger(__name__)

# A Remote Control link as RC-1 stores it (runners/live.py), checked again before /status shows it.
_SESSION_URL = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_-]{1,200}")

NOT_ALLOWED = "not allowed"
KILL_REPLY = "kill switch engaged"
RESUME_REPLY = "resumed: autonomy on, unpaused"
UNKNOWN_COMMAND_REPLY = commands.unknown_reply(None)
PAUSED_REPLY = "Paused: nothing runs until /resume."
CORE_EXT = "core"
CORE_TIMEOUT_S = 60.0

Render = Callable[[dict[str, Any]], str]
# A core job's on-demand entry (``/ext run core/<job>``): runs it now, returns the reply.
CoreRun = Callable[[], Awaitable[str]]


class FastGate(Protocol):
    """The part of the gate the fast path uses (``DryRunGate`` and the Phase 2 gate fit)."""

    def evaluate(self, intent: Intent) -> Decision: ...

    def consume(self, token: str, intent_hash: str) -> bool: ...

    def pending_for(self, principal: str) -> Sequence[PendingApproval]: ...


@dataclass(frozen=True)
class FastPathResult:
    reply_text: str
    tool_used: str | None
    command: str = ""  # the task title: the command name, plus a subcommand word
    intent: IntentLabel = "system"
    # A side-effecting command: ``{tool, args}`` for the local runner; ``reply_text`` is "".
    direct_call: dict[str, Any] | None = None


# --- templates ---------------------------------------------------------------------------


def kill_reply(runs_ended: int) -> str:
    """``kill switch engaged``, plus ``, 2 runs ended`` when Claude runs were stopped."""
    if runs_ended <= 0:
        return KILL_REPLY
    return f"{KILL_REPLY}, {runs_ended} run{'' if runs_ended == 1 else 's'} ended"


def _pct(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "?"
    return f"{round(value)}%"


def _no_render(d: dict[str, Any]) -> str:
    raise ValueError("fast-path command has a tool but no template")


def render_status(d: dict[str, Any]) -> str:
    parts = [
        f"Resonant up {_duration(d.get('uptime_s'))}",
        f"{d.get('inflight', '?')} in flight",
        f"autonomy {d.get('autonomy', '?')}",
        "paused" if d.get("paused") is True else "not paused",
    ]
    if d.get("dry_run") is True:
        parts.append("dry run")
    text = " · ".join(parts)
    lines = [text]
    if runs := _runs_line(d.get("runs")):
        lines.append(runs)
    if brain := _claude_brain_line(d.get("claude_brain")):
        lines.append(brain)
    if backup := _backup_line(d.get("backup")):
        lines.append(backup)
    return "\n".join(lines)


def _backup_line(value: Any) -> str:
    """BK-1: ``Last backup: 6h ago`` (``none yet``; ``· last try failed`` after a failure
    newer than the last good copy). Nothing when the status carries no backup section."""
    d = _as_dict(value)
    if not d:
        return ""
    age = d.get("age_h")
    if isinstance(age, int) and not isinstance(age, bool):
        text = "Last backup: under 1h ago" if age < 1 else f"Last backup: {_hours(age)} ago"
    else:
        text = "Last backup: none yet"
    err_at, ok_at = d.get("last_error_at"), d.get("last_ok_at")
    if isinstance(err_at, str) and (not isinstance(ok_at, str) or err_at > ok_at):
        text += " · last try failed"
    return text


def _hours(h: int) -> str:
    return f"{h}h" if h < 48 else f"{h // 24}d"


def _claude_brain_line(value: Any) -> str:
    """BR-11: ``Claude brain: up · 12 turns · Remote Control: <url>`` (nothing before its
    first start). Only a claude.ai session URL is shown (``live.session_url`` checked it
    when it was stored; checked again here)."""
    d = _as_dict(value)
    if not d:
        return ""
    state = "up" if d.get("state") == "up" else "down"
    bits = [f"Claude brain: {state}"]
    turns = d.get("turns")
    if isinstance(turns, int) and not isinstance(turns, bool):
        bits.append(f"{turns} turn{'' if turns == 1 else 's'}")
    url = d.get("remote_url")
    if state == "up" and isinstance(url, str) and _SESSION_URL.fullmatch(url):
        bits.append(f"Remote Control: {url}")
    return " · ".join(bits)


def _runs_line(value: Any) -> str:
    """``runs: 1 running, 1 queued, 2 ready to merge`` (nothing when none is open or ready)."""
    counts = _as_dict(value)
    bits: list[str] = []
    for status, label in (
        ("running", "running"),
        ("waiting", "waiting"),
        ("queued", "queued"),
        ("ready", "ready to merge"),
    ):
        n = counts.get(status)
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            bits.append(f"{n} {label}")
    return f"runs: {', '.join(bits)}" if bits else ""


def render_text(d: dict[str, Any]) -> str:
    """A tool whose output is already the reply (``pulse_preview``)."""
    return str(d.get("text", ""))


def render_ping(d: dict[str, Any]) -> str:
    return f"pong (up {_duration(d.get('uptime_s'))})"


def _task_lines(d: dict[str, Any], *, with_status: bool) -> list[str]:
    lines: list[str] = []
    for t in _as_list(d.get("tasks")):
        task = _as_dict(t)
        if not task:
            continue
        bits = [str(task.get("kind", "?"))]
        if with_status:
            status = str(task.get("status", "?"))
            if task.get("wait_reason"):
                status += f" ({task['wait_reason']})"
            bits.append(status)
        bits.append(_duration(task.get("age_s")))
        lines.append("- " + " · ".join(bits))
    return lines


def render_tasks(d: dict[str, Any]) -> str:
    lines = _task_lines(d, with_status=True)
    if not lines:
        return "No tasks yet."
    return "Recent tasks:\n" + "\n".join(lines)


def render_mini(d: dict[str, Any]) -> str:
    load = _as_list(d.get("load_avg"))
    disks = _as_list(d.get("disk"))
    parts = [f"CPU {_pct(d.get('cpu_percent'))}"]
    if load:
        parts.append(f"load {load[0]}")
    parts.append(f"mem {_pct(_as_dict(d.get('memory')).get('percent'))}")
    parts.append(f"pressure {_as_dict(d.get('memory_pressure')).get('level', '?')}")
    if disks:
        parts.append(f"disk {_pct(_as_dict(disks[0]).get('used_pct'))} used")
    parts.append(f"up {_duration(d.get('uptime_s'))}")
    return " · ".join(parts)


# --- matching ----------------------------------------------------------------------------


@dataclass(frozen=True)
class FastCommand:
    name: str  # "/status", "status", "what's running", ...
    intent: IntentLabel
    slash: bool
    tool: str | None = None  # a builtin tool (extension None)
    args: dict[str, Any] = field(default_factory=dict[str, Any])
    render: Render | None = None
    reply: str | None = None  # static reply (no tool): a usage line
    admin: bool = False  # owner-only (the registry's ``owner_only``)
    help: bool = False  # /help: rendered from the registry for the asking principal
    tools: tuple[str, ...] = ()  # every tool the command can call: checked before ``reply``
    title: str = ""  # the task title ("/restart ollama"); defaults to ``name``
    unknown: str | None = None  # an unknown ``/word``: suggestions depend on the asker
    extension: str | None = None  # an ``/<ext>`` command: ``tool`` is that extension's


# The reply template for each registry command with a read tool (side effects are reported
# by the local runner instead).
COMMAND_RENDERS: dict[str, Render] = {
    "/status": render_status,
    "/projects": render_projects,
    "/tasks": render_projects,
    "/notes": render_notes,
    "/reminders": render_reminders,
    "/disk": render_disk,
    "/logs": render_logs,
    "/ext": render_extensions,
    "/pulse": render_text,
    "/runs": render_runs,
    "/repos": render_repos,
    "/rules": render_rules,
}

# The only keywords: everything else in plain words goes through an intent (the labeler).
KEYWORDS: dict[str, FastCommand] = {
    "status": FastCommand("status", "system", False, tool="daemon_status", render=render_status),
    "ping": FastCommand("ping", "system", False, tool="daemon_status", render=render_ping),
}

_SLUG = re.compile(r"[a-z][a-z0-9-]{0,47}", re.ASCII)  # the manifest's name pattern
_SLASH = re.compile(r"^/[a-z][a-z0-9_-]{0,31}$")  # capped: an unknown word is echoed
_APOSTROPHES = str.maketrans(dict.fromkeys((0x2018, 0x2019, 0x02BC), "'"))  # curly quotes


def normalize(text: str) -> str:
    """Lowercase, straight apostrophes, collapsed whitespace, trailing ``?!.`` dropped."""
    t = " ".join(text.translate(_APOSTROPHES).lower().split())
    return t.rstrip("?!. ")


def _unknown(word: str | None) -> FastCommand:
    return FastCommand(
        "/unknown", "unknown", True, reply=commands.unknown_reply(word), unknown=word
    )


def _from_command(cmd: Command, rest: str, ctx: ParseContext) -> FastCommand:
    base = FastCommand(
        cmd.name,
        cmd.intent,
        True,
        admin=cmd.owner_only,
        tools=cmd.tools,
        extension=cmd.extension,
    )
    try:
        sub, tool, args = cmd.resolve(rest, ctx)
    except UsageError as e:
        return replace(base, reply=cmd.usage_reply(e))
    return replace(
        base,
        tool=tool,
        args=args,
        render=COMMAND_RENDERS.get(cmd.name),
        help=cmd is commands.HELP,
        title=f"{cmd.name} {sub}" if sub else cmd.name,
    )


def match_fast_path(
    text: str,
    ctx: ParseContext | None = None,
    table: Mapping[str, Command] = commands.BY_NAME,
) -> FastCommand | None:
    """The fast-path command for ``text``, or None. No I/O and no principal needed; ``ctx``
    (the clock and zone, for ``/remind``) defaults to now in UTC. ``table`` is the command
    table by name (builtins only by default; the router passes ``commands_for``'s)."""
    t = normalize(text)
    if t.startswith("/"):
        word = t.split(" ", 1)[0]
        if not _SLASH.match(word):
            return _unknown(None)
        cmd = table.get(word)
        if cmd is None:
            return _unknown(word)
        parts = text.split(maxsplit=1)  # the rest keeps its original case
        rest = parts[1].strip() if len(parts) == 2 else ""
        return _from_command(cmd, rest, ctx or ParseContext())
    return KEYWORDS.get(t)


# --- execution ---------------------------------------------------------------------------


class FastPath:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        principals: Principals,
        registry: ToolRegistry,
        gate: FastGate,
        handlers: Mapping[HandlerKey, ToolHandler],
        clock: Callable[[], datetime] = utcnow,
        timezone: str = "UTC",
        extensions: ExtensionCatalog | None = None,
        command_table: Sequence[Command] = commands.COMMANDS,
    ) -> None:
        self.conn = conn
        self.principals = principals
        self.registry = registry
        self.gate = gate
        self.handlers = handlers
        self.clock = clock
        self.timezone = timezone
        self.extensions = extensions
        self.commands: tuple[Command, ...] = tuple(command_table)
        self.table: dict[str, Command] = commands.by_name(self.commands)
        # ``/ext run core/<job>``: filled by the daemon once the core jobs exist.
        self.core_jobs: dict[str, CoreRun] = {}
        # The checkout whose ``extensions/<name>/`` ships extensions (the "isn't installed" reply).
        self.repo_path: Path | None = None
        # How many runs the last ``/kill`` ended (``ClaudeRunner.last_killed``; wiring sets it).
        self.runs_killed: Callable[[], int] = lambda: 0

    def match(self, text: str) -> FastCommand | None:
        """``match_fast_path`` over this fast path's command table (extensions included)."""
        return match_fast_path(text, self.context(), self.table)

    def available(self, principal: str) -> list[Command]:
        return commands.available(
            principal, principals=self.principals, registry=self.registry, commands=self.commands
        )

    def context(self) -> ParseContext:
        """What ``match_fast_path`` needs to parse ``/remind`` (now, and the owner's zone)
        and ``/run`` (whether a word names a repo, from the store)."""
        return ParseContext(self.clock(), self.timezone, self.is_repo)

    def is_repo(self, word: str) -> bool:
        """``/run``'s first word is a repo: ``owner/name`` or a GitHub URL, a registered name
        or alias, or an enabled extension that links repos. No GitHub call: a word only
        GitHub knows needs ``/run <word>: …``."""
        from resonant.runner.resolve import parse_slug

        if parse_slug(word) is not None:
            return True
        try:
            return repo_store.find(self.conn, word) is not None or bool(
                repo_store.enabled_links(self.conn, word.lower())
            )
        except sqlite3.Error:
            return False

    async def run(
        self,
        cmd: FastCommand,
        *,
        principal: str,
        event_id: str,
        trace_id: str | None = None,
        spans: SpanFactory | None = None,
    ) -> FastPathResult | None:
        """Answer ``cmd`` for ``principal``; None means "not mine, let the labeler try"."""
        start = time.monotonic()
        with (spans or null_spans)("router.fast_path", command=cmd.name) as s:
            result = await self._run(cmd, principal, event_id, trace_id)
            s.set(
                handled=result is not None,
                tool_used=result.tool_used if result else None,
                latency_ms=round((time.monotonic() - start) * 1000, 2),
            )
        return result

    def _result(self, cmd: FastCommand, text: str, tool: str | None = None) -> FastPathResult:
        return FastPathResult(text, tool, command=cmd.title or cmd.name, intent=cmd.intent)

    def _can_request(self, principal: str, tool: str, extension: str | None = None) -> bool:
        return commands.can_request(
            principal, tool, principals=self.principals, registry=self.registry, extension=extension
        )

    async def _run(
        self, cmd: FastCommand, principal: str, event_id: str, trace_id: str | None
    ) -> FastPathResult | None:
        # Permission before arguments: a refused asker never sees a usage line.
        if cmd.admin and not self.principals.can(principal, "admin", None):
            with atomic(self.conn):
                audit(
                    self.conn,
                    "router.admin_refused",
                    requested_by=principal,
                    trace_id=trace_id,
                    command=cmd.name,
                )
            return self._result(cmd, NOT_ALLOWED)
        if cmd.slash and not all(self._can_request(principal, t, cmd.extension) for t in cmd.tools):
            return self._result(cmd, NOT_ALLOWED)
        if cmd.unknown is not None:
            if (hint := self._not_installed(cmd.unknown, principal)) is not None:
                return self._result(cmd, hint)
            available = self.available(principal)
            return self._result(cmd, commands.unknown_reply(cmd.unknown, available))
        if cmd.reply is not None:  # a usage line
            return self._result(cmd, cmd.reply)
        if cmd.admin and cmd.name == "/kill":
            killswitch.engage(self.conn, actor=principal, reason="fast path /kill")
            return self._result(cmd, kill_reply(self.runs_killed()), "killswitch.engage")
        if cmd.admin and cmd.name == "/resume":
            killswitch.release(self.conn, actor=principal)
            return self._result(cmd, RESUME_REPLY, "killswitch.release")
        if cmd.name == "/approvals":
            return self._approvals(cmd, principal)
        if cmd.admin and cmd.title == "/ext run":
            return await self._ext_run(cmd, principal, trace_id)
        if cmd.admin and cmd.title == "/pulse now":
            return await self._core_run(cmd, "pulse", principal, trace_id, "/pulse now")
        if cmd.extension is not None:
            return self._extension_call(cmd)
        if cmd.help:
            available = self.available(principal)
            keywords = self.principals.can(principal, "request", None)
            text = commands.help_text(available, cmd.args.get("query"), keywords=keywords)
            return self._result(cmd, text)
        if cmd.tool is None:
            return self._result(cmd, UNKNOWN_COMMAND_REPLY)

        spec = self.registry.get(None, cmd.tool)
        handler = self.handlers.get((None, cmd.tool))
        if spec is None or handler is None or not self.principals.may_request(principal, spec):
            return self._result(cmd, NOT_ALLOWED) if cmd.slash else None
        if spec.effect is not Effect.READ:
            # Only reads run here. A side effect never runs inline: a slash command becomes a
            # direct_call task (explicit origin, gated by the runner); a keyword never may.
            if not cmd.slash:
                log.error("fast path refused non-read keyword tool %s", cmd.tool)
                return None
            return FastPathResult(
                "",
                None,
                command=cmd.title or cmd.name,
                intent=cmd.intent,
                direct_call={"tool": cmd.tool, "args": dict(cmd.args)},
            )
        intent = Intent.for_tool(
            spec, task_id=f"route:{event_id}", principal=principal, args=cmd.args, trace_id=trace_id
        )
        decision = self.gate.evaluate(intent)
        if not isinstance(decision, Allow) or not self.gate.consume(decision.token, intent.hash):
            return self._result(cmd, NOT_ALLOWED) if cmd.slash else None
        try:
            data = await asyncio.wait_for(handler(dict(cmd.args)), timeout=spec.timeout_s)
            text = (cmd.render or _no_render)(data)
        except Exception as e:
            log.warning("fast path %s failed: %s", cmd.tool, type(e).__name__)
            text = f"Couldn't read {cmd.tool} right now."
        return self._result(cmd, text, cmd.tool)

    def _direct(
        self, cmd: FastCommand, extension: str | None, tool: str, args: dict[str, Any], title: str
    ) -> FastPathResult:
        return FastPathResult(
            "",
            None,
            command=title,
            intent=cmd.intent,
            direct_call={"extension": extension, "tool": tool, "args": args},
        )

    def _extension_call(self, cmd: FastCommand) -> FastPathResult:
        """``/<ext> <tool> [args]``: a direct call of that extension's chat tool, reads
        included (the executor injects secrets and redacts). Never a job spec."""
        ext, tool = cmd.extension, cmd.tool
        spec = self.registry.get(ext, tool) if tool is not None else None
        if (
            ext is None
            or tool is None
            or spec is None
            or (self.extensions is not None and self.extensions.is_job(spec))
        ):
            return self._result(cmd, NOT_ALLOWED)
        return self._direct(cmd, ext, tool, dict(cmd.args), cmd.title or cmd.name)

    async def _ext_run(
        self, cmd: FastCommand, principal: str, trace_id: str | None
    ) -> FastPathResult:
        """``/ext run <ext>/<job>`` (owner-only, checked by the caller)."""
        ext, job = str(cmd.args.get("extension")), str(cmd.args.get("job"))
        label = f"{ext}/{job}"
        if ext == CORE_EXT:  # its entry checks the kill switch itself
            return await self._core_run(cmd, job, principal, trace_id)
        if _paused(self.conn):
            return self._result(cmd, PAUSED_REPLY)
        manifest = self.extensions.manifest(ext) if self.extensions is not None else None
        entry = next((j for j in manifest.jobs if j.id == job), None) if manifest else None
        spec = self.registry.get(ext, entry.spec_name) if entry is not None else None
        if spec is None:
            return self._result(cmd, f"No job {label}. Try /ext list.")
        args = {"due_at": iso(self.clock())}
        return self._direct(cmd, ext, spec.name, args, f"/ext run {label}")

    def _not_installed(self, word: str, principal: str) -> str | None:
        """For an unknown ``/word`` that names an extension shipped in the repo checkout
        (``extensions/<word>/resonant.yaml``) but isn't running: how to install or update it.
        Owner only, slug-valid words only; reads one file's existence and runs nothing."""
        name = word.removeprefix("/")
        if self.repo_path is None or not _SLUG.fullmatch(name):
            return None
        if not self.principals.can(principal, "admin", None):
            return None
        try:
            base = (self.repo_path / "extensions").resolve()
            manifest = (base / name / "resonant.yaml").resolve()
            if not manifest.is_relative_to(base) or not manifest.is_file():
                return None
            record = ext_store.get_extension(self.conn, name)
        except (OSError, sqlite3.Error):
            return None
        if record is None:
            return f"{name} isn't installed. On the Mini: resonant ext add repo:{name}"
        if record.status == "enabled":
            return None
        if record.status == "disabled_tampered":
            return (
                f"{name} is disabled (files changed since install). "
                f"On the Mini: resonant ext update {name}"
            )
        return f"{name} is turned off."

    async def _core_run(
        self,
        cmd: FastCommand,
        job: str,
        principal: str,
        trace_id: str | None,
        title: str | None = None,
    ) -> FastPathResult:
        """A core job now, inline (trusted core code, L0, no gate), audited ``job.run``."""
        run = self.core_jobs.get(job)
        if run is None:
            return self._result(cmd, f"No job core/{job}. Try /ext list.")
        label = f"{CORE_EXT}/{job}"
        try:
            text = await asyncio.wait_for(run(), timeout=CORE_TIMEOUT_S)
            ok = True
        except Exception as e:
            log.warning("core job %s failed: %s", label, type(e).__name__)
            text, ok = f"{label} failed.", False
        with atomic(self.conn):
            audit(
                self.conn,
                "job.run" if ok else "job.failed",
                requested_by=principal,
                trace_id=trace_id,
                extension=CORE_EXT,
                job=label,
                manual=True,
                reply=text,  # code-built: what happened (sent, paused, unreachable)
            )
        return FastPathResult(text, label, command=title or f"/ext run {label}", intent=cmd.intent)

    def _approvals(self, cmd: FastCommand, principal: str) -> FastPathResult:
        """``/approvals [code]``: pending approvals, or one's full args (owner only)."""
        code = cmd.args.get("code")
        text = approvals_reply(
            self.conn,
            self.gate.pending_for(principal),
            code if isinstance(code, str) else None,
            is_owner=self.principals.can(principal, "admin", None),
            now=self.clock(),
        )
        return self._result(cmd, NOT_ALLOWED if text is None else text)


BRIEFING_SENT = "Briefing sent."
BRIEFING_PAUSED = "Paused: briefing not sent."
BRIEFING_UNREACHABLE = "Can't send: no channel/owner."


def briefing_reply(result: Mapping[str, Any]) -> str:
    """The reply to ``/ext run core/briefing`` from ``run_briefing_now``'s result."""
    if result.get("sent") is True:
        return BRIEFING_SENT
    return BRIEFING_PAUSED if result.get("skipped") == "paused" else BRIEFING_UNREACHABLE


def core_briefing(deps: CoreDeps) -> CoreRun:
    """``/ext run core/briefing``: the briefing now (``briefing.run_briefing_now``)."""

    async def run() -> str:
        return briefing_reply(await run_briefing_now(deps))

    return run


PULSE_SENT = "Pulse sent."
PULSE_NOTHING = "Nothing new to send."
PULSE_PAUSED = "Paused: pulse not sent."


def pulse_reply(result: Mapping[str, Any]) -> str:
    """The reply to ``/ext run core/pulse`` from ``run_pulse_now``'s result."""
    if result.get("sent") is True:
        return PULSE_SENT
    skipped = result.get("skipped")
    if skipped == "paused":
        return PULSE_PAUSED
    return PULSE_NOTHING if skipped in ("nothing", "duplicate") else BRIEFING_UNREACHABLE


def core_pulse(deps: CoreDeps) -> CoreRun:
    """``/ext run core/pulse``: the pulse now (``pulse_job.run_pulse_now``)."""

    async def run() -> str:
        return pulse_reply(await run_pulse_now(deps))

    return run


def _paused(conn: sqlite3.Connection) -> bool:
    """The kill switch's ``paused``; unknown counts as paused (fail closed)."""
    try:
        return killswitch.is_paused(conn)
    except killswitch.PausedStateUnknownError:
        return True

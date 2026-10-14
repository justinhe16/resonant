"""Deterministic fast path: slash commands and a few anchored keywords, no model call.

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
- Keywords (the whole message, case-insensitive, trailing ``?!.`` ignored): ``status``,
  ``what's running``, ``how's the mini``, ``ping``.

``/approvals`` lists the asker's pending approvals (code, summary, expiry);
``/approvals <code>`` adds the intent's full args, for the owner only. Approval *replies*
(``yes``, ``no A7``) are not fast-path commands: the router hands them to
``router/approvals.py`` after slash commands.

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from resonant import killswitch
from resonant.executor import HandlerKey, ToolHandler
from resonant.gate import Allow, Decision, Intent, PendingApproval
from resonant.models import SpanFactory
from resonant.models.client import null_spans
from resonant.principals import Principals
from resonant.router import commands
from resonant.router.approvals import approvals_reply
from resonant.router.commands import Command, ParseContext, UsageError
from resonant.router.intents import IntentLabel
from resonant.router.render import as_dict as _as_dict
from resonant.router.render import as_list as _as_list
from resonant.router.render import duration as _duration
from resonant.router.render import (
    render_disk,
    render_logs,
    render_notes,
    render_projects,
    render_reminders,
)
from resonant.store.audit import audit
from resonant.store.db import atomic, utcnow
from resonant.tools import ToolRegistry
from resonant_sdk import Effect

log = logging.getLogger(__name__)

NOT_ALLOWED = "not allowed"
KILL_REPLY = "kill switch engaged"
RESUME_REPLY = "resumed: autonomy on, unpaused"
UNKNOWN_COMMAND_REPLY = commands.unknown_reply(None)

Render = Callable[[dict[str, Any]], str]


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
    return " · ".join(parts)


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


def render_running(d: dict[str, Any]) -> str:
    lines = _task_lines(d, with_status=False)
    if not lines:
        return "Nothing running."
    return f"Running ({len(lines)}):\n" + "\n".join(lines)


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
}

_RUNNING = FastCommand(
    "what's running",
    "job_control",
    False,
    tool="list_tasks",
    args={"status": "running"},
    render=render_running,
)
_MINI = FastCommand("how's the mini", "system", False, tool="system_status", render=render_mini)
KEYWORDS: dict[str, FastCommand] = {
    "status": FastCommand("status", "system", False, tool="daemon_status", render=render_status),
    "ping": FastCommand("ping", "system", False, tool="daemon_status", render=render_ping),
    "what's running": _RUNNING,
    "whats running": _RUNNING,
    "what is running": _RUNNING,
    "how's the mini": _MINI,
    "hows the mini": _MINI,
    "how is the mini": _MINI,
}

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
    base = FastCommand(cmd.name, cmd.intent, True, admin=cmd.owner_only, tools=cmd.tools)
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


def match_fast_path(text: str, ctx: ParseContext | None = None) -> FastCommand | None:
    """The fast-path command for ``text``, or None. No I/O and no principal needed; ``ctx``
    (the clock and zone, for ``/remind``) defaults to now in UTC."""
    t = normalize(text)
    if t.startswith("/"):
        word = t.split(" ", 1)[0]
        if not _SLASH.match(word):
            return _unknown(None)
        cmd = commands.get(word)
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
    ) -> None:
        self.conn = conn
        self.principals = principals
        self.registry = registry
        self.gate = gate
        self.handlers = handlers
        self.clock = clock
        self.timezone = timezone

    def context(self) -> ParseContext:
        """What ``match_fast_path`` needs to parse ``/remind``: now, and the owner's zone."""
        return ParseContext(self.clock(), self.timezone)

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

    def _can_request(self, principal: str, tool: str) -> bool:
        return commands.can_request(
            principal, tool, principals=self.principals, registry=self.registry
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
        if cmd.slash and not all(self._can_request(principal, t) for t in cmd.tools):
            return self._result(cmd, NOT_ALLOWED)
        if cmd.unknown is not None:
            available = commands.available(
                principal, principals=self.principals, registry=self.registry
            )
            return self._result(cmd, commands.unknown_reply(cmd.unknown, available))
        if cmd.reply is not None:  # a usage line
            return self._result(cmd, cmd.reply)
        if cmd.admin and cmd.name == "/kill":
            killswitch.engage(self.conn, actor=principal, reason="fast path /kill")
            return self._result(cmd, KILL_REPLY, "killswitch.engage")
        if cmd.admin and cmd.name == "/resume":
            killswitch.release(self.conn, actor=principal)
            return self._result(cmd, RESUME_REPLY, "killswitch.release")
        if cmd.name == "/approvals":
            return self._approvals(cmd, principal)
        if cmd.help:
            available = commands.available(
                principal, principals=self.principals, registry=self.registry
            )
            keywords = self.principals.can(principal, "request", None)
            text = commands.help_text(available, cmd.args.get("query"), keywords=keywords)
            return self._result(cmd, text)
        if cmd.tool is None:
            return self._result(cmd, UNKNOWN_COMMAND_REPLY)

        spec = self.registry.get(None, cmd.tool)
        handler = self.handlers.get((None, cmd.tool))
        if (
            spec is None
            or handler is None
            or not self.principals.can(principal, "request", spec.extension)
        ):
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

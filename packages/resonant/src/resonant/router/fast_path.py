"""Deterministic fast path: slash commands and a few anchored keywords, no model call.

Code computes and text narrates: each command maps to at most one builtin read tool, and
the reply is rendered from a template over the tool's output.

- Slash commands come from the command registry (``router/commands.py``): the first word
  names the command and the rest is its argument text, parsed deterministically. A bad
  argument gets the usage line. ``/help [word]`` lists the commands the asker may use. Any
  other ``/word`` gets "Unknown command", with a "Did you mean" suggestion when one is
  close. None of these reach the model, and a suggestion never runs anything.
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
from dataclasses import dataclass, field
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
from resonant.router.commands import Command, UsageError
from resonant.router.intents import IntentLabel
from resonant.router.render import as_dict as _as_dict
from resonant.router.render import as_list as _as_list
from resonant.router.render import duration as _duration
from resonant.router.render import render_projects
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
    command: str = ""
    intent: IntentLabel = "system"


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
    tool: str | None = None  # a builtin read tool (extension None)
    args: dict[str, Any] = field(default_factory=dict[str, Any])
    render: Render | None = None
    reply: str | None = None  # static reply (no tool)
    admin: bool = False  # owner-only (the registry's ``owner_only``)
    help: bool = False  # /help: rendered from the registry for the asking principal


# The reply template for each registry command that has a tool.
COMMAND_RENDERS: dict[str, Render] = {
    "/status": render_status,
    "/projects": render_projects,
    "/tasks": render_projects,
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
    return FastCommand("/unknown", "unknown", True, reply=commands.unknown_reply(word))


def _from_command(cmd: Command, rest: str) -> FastCommand:
    try:
        args = cmd.parse(rest)
    except UsageError as e:
        return FastCommand(cmd.name, cmd.intent, True, reply=cmd.usage_reply(e))
    return FastCommand(
        cmd.name,
        cmd.intent,
        True,
        tool=cmd.tool,
        args=args,
        render=COMMAND_RENDERS.get(cmd.name),
        admin=cmd.owner_only,
        help=cmd is commands.HELP,
    )


def match_fast_path(text: str) -> FastCommand | None:
    """The fast-path command for ``text``, or None. Pure: no I/O, no principal needed."""
    t = normalize(text)
    if t.startswith("/"):
        word = t.split(" ", 1)[0]
        if not _SLASH.match(word):
            return _unknown(None)
        cmd = commands.get(word)
        if cmd is None:
            return _unknown(word)
        parts = text.split(maxsplit=1)  # the rest keeps its original case
        return _from_command(cmd, parts[1].strip() if len(parts) == 2 else "")
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
    ) -> None:
        self.conn = conn
        self.principals = principals
        self.registry = registry
        self.gate = gate
        self.handlers = handlers
        self.clock = clock

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
        return FastPathResult(text, tool, command=cmd.name, intent=cmd.intent)

    async def _run(
        self, cmd: FastCommand, principal: str, event_id: str, trace_id: str | None
    ) -> FastPathResult | None:
        if cmd.admin:
            if not self.principals.can(principal, "admin", None):
                with atomic(self.conn):
                    audit(
                        self.conn,
                        "router.admin_refused",
                        requested_by=principal,
                        trace_id=trace_id,
                        command=cmd.name,
                    )
                return self._result(cmd, NOT_ALLOWED)
            if cmd.name == "/kill":
                killswitch.engage(self.conn, actor=principal, reason="fast path /kill")
                return self._result(cmd, KILL_REPLY, "killswitch.engage")
            if cmd.name == "/resume":
                killswitch.release(self.conn, actor=principal)
                return self._result(cmd, RESUME_REPLY, "killswitch.release")
        if cmd.name == "/approvals" and cmd.reply is None:  # else: a usage reply
            return self._approvals(cmd, principal)
        if cmd.help:
            available = commands.available(
                principal, principals=self.principals, registry=self.registry
            )
            keywords = self.principals.can(principal, "request", None)
            text = commands.help_text(available, cmd.args.get("query"), keywords=keywords)
            return self._result(cmd, text)
        if cmd.tool is None:
            return self._result(cmd, cmd.reply or UNKNOWN_COMMAND_REPLY)

        spec = self.registry.get(None, cmd.tool)
        handler = self.handlers.get((None, cmd.tool))
        if (
            spec is None
            or handler is None
            or not self.principals.can(principal, "request", spec.extension)
        ):
            return self._result(cmd, NOT_ALLOWED) if cmd.slash else None
        if spec.effect is not Effect.READ:
            # Only reads skip the executor here. Side-effecting commands become direct_call
            # tasks (P2-15, plan D5); until then they are refused, never run inline.
            log.error("fast path refused non-read tool %s", cmd.tool)
            return self._result(cmd, NOT_ALLOWED) if cmd.slash else None
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

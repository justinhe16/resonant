"""Deterministic fast path: slash commands and a few anchored keywords, no model call.

Code computes and text narrates: each command maps to at most one builtin read tool, and
the reply is rendered from a template over the tool's output.

- Slash commands (the whole message): ``/status``, ``/tasks``, ``/help``, ``/kill`` and
  ``/resume`` (owner only). Any other ``/word`` gets "Unknown command" and never reaches the
  model.
- Keywords (the whole message, case-insensitive, trailing ``?!.`` ignored): ``status``,
  ``what's running``, ``how's the mini``, ``ping``.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from resonant import killswitch
from resonant.executor import HandlerKey, ToolHandler
from resonant.gate import Allow, Decision, Intent
from resonant.models import SpanFactory
from resonant.models.client import null_spans
from resonant.principals import Principals
from resonant.router.intents import IntentLabel
from resonant.tools import ToolRegistry

log = logging.getLogger(__name__)

NOT_ALLOWED = "not allowed"
KILL_REPLY = "kill switch engaged"
RESUME_REPLY = "resumed: autonomy on, unpaused"
UNKNOWN_COMMAND_REPLY = "Unknown command. Try /help."
HELP_REPLY = (
    "Commands: /status (daemon), /tasks (recent tasks), /help, "
    "/kill (stop autonomy, owner only), /resume (owner only). "
    "Or just ask: what's running, how's the mini, ping."
)

Render = Callable[[dict[str, Any]], str]


class FastGate(Protocol):
    """The part of the gate the fast path uses (``DryRunGate`` and the Phase 2 gate fit)."""

    def evaluate(self, intent: Intent) -> Decision: ...

    def consume(self, token: str, intent_hash: str) -> bool: ...


@dataclass(frozen=True)
class FastPathResult:
    reply_text: str
    tool_used: str | None
    command: str = ""
    intent: IntentLabel = "system"


# --- templates ---------------------------------------------------------------------------


def _duration(seconds: Any) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        return "?"
    s = max(0, int(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


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


def _as_dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


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
    admin: bool = False  # owner-only kill switch commands


SLASH_COMMANDS: dict[str, FastCommand] = {
    c.name: c
    for c in (
        FastCommand("/status", "system", True, tool="daemon_status", render=render_status),
        FastCommand("/tasks", "job_control", True, tool="list_tasks", render=render_tasks),
        FastCommand("/help", "question", True, reply=HELP_REPLY),
        FastCommand("/kill", "system", True, admin=True),
        FastCommand("/resume", "system", True, admin=True),
    )
}
UNKNOWN_COMMAND = FastCommand("/unknown", "unknown", True, reply=UNKNOWN_COMMAND_REPLY)

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

_SLASH = re.compile(r"^/[a-z][a-z0-9_-]*$")
_APOSTROPHES = str.maketrans(dict.fromkeys((0x2018, 0x2019, 0x02BC), "'"))  # curly quotes


def normalize(text: str) -> str:
    """Lowercase, straight apostrophes, collapsed whitespace, trailing ``?!.`` dropped."""
    t = " ".join(text.translate(_APOSTROPHES).lower().split())
    return t.rstrip("?!. ")


def match_fast_path(text: str) -> FastCommand | None:
    """The fast-path command for ``text``, or None. Pure: no I/O, no principal needed."""
    t = normalize(text)
    if t.startswith("/"):
        if " " in t or not _SLASH.match(t):
            return UNKNOWN_COMMAND
        return SLASH_COMMANDS.get(t, UNKNOWN_COMMAND)
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
    ) -> None:
        self.conn = conn
        self.principals = principals
        self.registry = registry
        self.gate = gate
        self.handlers = handlers

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
                return self._result(cmd, NOT_ALLOWED)
            if cmd.name == "/kill":
                killswitch.engage(self.conn, actor=principal, reason="fast path /kill")
                return self._result(cmd, KILL_REPLY, "killswitch.engage")
            killswitch.release(self.conn, actor=principal)
            return self._result(cmd, RESUME_REPLY, "killswitch.release")
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

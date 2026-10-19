"""Resonant's tools for a Claude brain session (BR-10): the daemon side of the MCP bridge.

An ask the brain hands over (``AskRunner.enqueue(..., tools=True)``) gets, for the length of its
``claude -p`` session, a :class:`ClaudeTools`: a unix socket in a fresh ``0700`` temp dir, a
random token, and an MCP config that starts ``brain/mcp_stdio.py`` (a stdlib relay) with both.
The relay forwards ``tools/list`` and ``tools/call`` here; this side checks the token and runs
every call **bound to the ask task** (its principal, id, trace, step and checkpoint), so one
task's session can't act as another, whatever Claude or the relay sends.

Every call goes through :class:`ToolCaller`, which *is* the local runner's call path
(``LocalRunner._call_tool``): the registry spec (never the model's), ``gate.evaluate`` with
the same levels, trust rules, standing allows, principal checks and kill switch, the pinned
gated call, the executor and its idempotency keys, the code-built report lines. Nothing here
decides whether a call may run.

- **Tools.** What the brain loop offers the principal (``BrainRunner.tools_for``: filtered by
  ``principals.may_request`` first; never job specs, ``mute_extension`` or ``ask_claude``).
  Each with its full schema; a call's args are checked against it before the gate.
- **Origin and taint** (D6), as in the brain loop: model-chosen calls are ``inferred``, and
  ``tainted`` once the session has read outside content: the ask started from a tainted turn,
  a Resonant tool that taints (``brain.taints``), or any tool of Claude's own (Read, Grep,
  Glob, the web, connectors) seen in the session's stream. A change waits until the stream
  shows Claude's ``tool_use`` for it, so every read before it is counted; if the stream
  doesn't show it within ``SYNC_S`` the call counts as tainted (fails closed).
- **Approvals.** A call the gate asks about is pinned (``checkpoint.call``) but the owner is
  not prompted during the session: Claude gets ``asked the owner: <code-built summary>`` and
  never an approval code. When the session ends its answer goes out first, then the prompt,
  then the task waits; ``yes`` runs the pinned call exactly once (the executor's key), ``no``
  or expiry drops it (``AskRunner``, the local runner's resume). One approval per task: after
  it, every other change is refused. At most ``MAX_SIDE_EFFECTS`` changes run per task.
- **Kill switch.** Paused: every call is refused here (the session itself is killed by the
  ask runner); autonomy off: the gate asks for everything above L0, as for the local model.

Results go back as text: a read's JSON (clipped), a change's code-built report line, or a
``not run: …`` refusal. Approval codes never appear: the gate's summary is code-built without
one, and the read tools keep them out as they do for the local model (``/approvals`` shows
them). Message bodies are PII: spans carry names and counts only.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, cast

from resonant import killswitch
from resonant.brain.text import flat
from resonant.gate import NeedsApproval, canonical_json
from resonant.gate.types import Origin
from resonant.gateway.channel import Channel
from resonant.loop.types import Fail, StepContext, Task, Wait
from resonant.models import (
    ChatMessage,
    ModelReply,
    ModelUnavailableError,
    ProbeResult,
    SpanFactory,
)
from resonant.observability.spans import span
from resonant.router.intents import ExtensionCatalog
from resonant.runners.local import (
    MAX_SIDE_EFFECTS,
    Answer,
    CallResult,
    LocalRunner,
    PinnedCall,
    RunnerGate,
    ToolExecutor,
    is_explicit,
)
from resonant.store.tasks import get_task
from resonant.tools import ToolRegistry
from resonant_sdk import Effect, ToolSpec

log = logging.getLogger(__name__)

SERVER = "resonant"
PREFIX = f"mcp__{SERVER}__"  # how Claude Code names our tools
SOCKET_ENV = "RESONANT_MCP_SOCKET"  # brain/mcp_stdio.py
TOKEN_ENV = "RESONANT_MCP_TOKEN"  # noqa: S105 - an env var name
STDIO = Path(__file__).resolve().parent.parent / "brain" / "mcp_stdio.py"
SYNC_S = 3.0  # how long a change waits for its tool_use in the stream
CLOSE_WAIT_S = 30.0  # a call in flight when the session ends
RESULT_MAX_CHARS = 16_000  # a read's result, as Claude sees it
LINE_MAX = 1024 * 1024
DESCRIPTION_CHARS = 600

PAUSED = "not run: Resonant is paused (/kill); nothing runs until the owner resumes it."
PENDING = (
    "not run: this task is already waiting on the owner's answer for {what}, and a task "
    "asks once. Tell the owner they can ask for it again after."
)
CAPPED = (
    f"not run: at most {MAX_SIDE_EFFECTS} changes per task, and they already ran. Tell the "
    "owner what ran and that they can ask for the rest separately."
)
ASKED = (
    "asked the owner: {what}. Nothing ran yet: it runs only if they say yes, after your "
    "answer. Tell them it's waiting on their yes; don't call it again."
)
NO_SUCH_TOOL = "not run: there is no Resonant tool named {name}"
BAD_ARGS = "not run: invalid arguments ({why}). {name} takes: {schema}"
CHANGE_NOTE = " (A change: Resonant's approval rules apply; it may ask the owner first.)"


@dataclass(frozen=True)
class ToolAccess:
    """What the bridge needs from the daemon: the gate, the executor, the registry and the
    brain's tool list for a principal (``BrainRunner.tools_for``, flattened, minus
    ``ask_claude``)."""

    gate: RunnerGate
    executor: ToolExecutor
    registry: ToolRegistry
    tools_for: Callable[[str], Sequence[ToolSpec]]
    taints: Callable[[ToolSpec], bool]
    extensions: ExtensionCatalog | None = None
    timezone: str = "UTC"


class _NoModel:
    """The caller never asks a model; a stray call fails like a model that's down."""

    async def chat_json(self, messages: list[ChatMessage], **kw: Any) -> ModelReply:
        raise ModelUnavailableError("no model in the Claude tool bridge")

    async def chat_tools(self, messages: list[ChatMessage], **kw: Any) -> ModelReply:
        raise ModelUnavailableError("no model in the Claude tool bridge")

    async def probe(self) -> ProbeResult:
        return ProbeResult(False, False, False, False, None, "no model", False)


class ToolCaller(LocalRunner):
    """The local runner's call path for Claude's calls. Two differences: a call the gate asks
    about while its task's session is live is pinned and recorded, not prompted (the prompt
    goes out after Claude's answer, ``live``/``deferred``); and a pinned call keeps the taint
    it was pinned with, as in the brain loop."""

    name = "ask"
    tool_error_detail = True  # a read's own reason goes back (Claude can fix a path)

    def __init__(self, conn: sqlite3.Connection, access: ToolAccess, channel: Channel) -> None:
        super().__init__(
            conn,
            model=cast(Any, _NoModel()),
            channel=channel,
            gate=access.gate,
            executor=access.executor,
            registry=access.registry,
            max_step_s=None,
            timezone=access.timezone,
            extensions=access.extensions,
        )
        self.live: set[str] = set()  # task ids with a session running
        self.deferred: dict[str, NeedsApproval] = {}

    async def _ask(
        self, task_id: str, decision: NeedsApproval, cp: dict[str, Any], spans: SpanFactory
    ) -> Wait | Fail:
        if task_id in self.live:
            self.deferred[task_id] = decision
            return Wait("approval", checkpoint=cp, wake_at=decision.expires_at)
        return await super()._ask(task_id, decision, cp, spans)

    def _pinned_origin(self, cp: dict[str, Any], pinned: PinnedCall) -> Origin:
        if is_explicit(cp, pinned):  # never for a Claude call: no direct_call here
            return "explicit"
        return "tainted" if cp.get("tainted") is True else "inferred"


@dataclass(frozen=True)
class McpSetup:
    """What the session's argv needs: the config file and the tool names to allow."""

    config: Path
    allowed: tuple[str, ...]


class ClaudeTools:
    """One session's bridge, bound to one ask task. ``start`` before the session, ``aclose``
    after; ``observe`` every stream-json message; ``call`` serves the relay."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        access: ToolAccess,
        caller: ToolCaller,
        task: Task,
        ctx: StepContext,
        *,
        tainted: bool,
        sync_s: float = SYNC_S,
        python: str = sys.executable,
    ) -> None:
        assert task.principal is not None
        self.conn = conn
        self.access = access
        self.caller = caller
        self.task = task
        self.principal: str = task.principal
        self.ctx = ctx
        self.tainted = tainted
        self.sync_s = sync_s
        self.python = python
        specs = [s for s in access.tools_for(self.principal) if s.name != "ask_claude"]
        self.specs: dict[str, ToolSpec] = {s.name: s for s in sorted(specs, key=lambda s: s.name)}
        self.reports: list[str] = []
        self.pending: str | None = None  # the code-built summary of the call waiting
        self.calls = 0
        self.outside = False  # a tool of Claude's own ran (outside content)
        self._index = 0
        self._token = secrets.token_urlsafe(32)
        self._dir: Path | None = None
        self._server: asyncio.Server | None = None
        self._lock = asyncio.Lock()
        self._seen: Counter[str] = Counter()
        self._used: Counter[str] = Counter()
        self._stream = asyncio.Event()
        self._closed = False  # after aclose: every call is refused (review: late calls)
        self._writers: set[asyncio.StreamWriter] = set()
        self._spans: SpanFactory = partial(span, conn, trace_id=task.trace_id, task_id=task.id)

    # -- lifecycle --

    async def start(self) -> McpSetup:
        """The socket, its server and the MCP config (both in a fresh 0700 dir)."""
        self._dir = Path(tempfile.mkdtemp(prefix="rsn-mcp-"))
        os.chmod(self._dir, 0o700)
        sock = self._dir / "s"
        self._server = await asyncio.start_unix_server(self._serve, path=str(sock), limit=LINE_MAX)
        os.chmod(sock, 0o600)
        config = {
            "mcpServers": {
                SERVER: {
                    "command": self.python,
                    "args": ["-I", str(STDIO)],
                    "env": {SOCKET_ENV: str(sock), TOKEN_ENV: self._token},
                }
            }
        }
        path = self._dir / "mcp.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)
        self.caller.live.add(self.task.id)
        return McpSetup(path, tuple(PREFIX + n for n in self.specs))

    async def aclose(self) -> None:
        """Stop serving. A call still running (the session was killed mid-call) finishes
        first, up to ``CLOSE_WAIT_S``, so its report line isn't lost; any call after that is
        refused, and open connections are closed (a late call must never prompt the owner
        directly or run outside the session's accounting)."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._lock.acquire(), CLOSE_WAIT_S)
            self._closed = True
            self._lock.release()
        self._closed = True
        for writer in list(self._writers):
            writer.close()
        self.caller.live.discard(self.task.id)
        self.caller.deferred.pop(self.task.id, None)
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
            self._server = None
        if self._dir is not None:
            await asyncio.to_thread(shutil.rmtree, self._dir, True)
            self._dir = None

    # -- the stream (taint) --

    def observe(self, msg: dict[str, Any]) -> None:
        """One stream-json message of the session: count Claude's tool uses. A tool of its own
        (anything but ours) reads outside content: the session is tainted from then on."""
        if msg.get("type") != "assistant":
            return
        inner = msg.get("message")
        content = cast(dict[str, Any], inner).get("content") if isinstance(inner, dict) else None
        if not isinstance(content, list):
            return
        for block in cast(list[Any], content):
            if not isinstance(block, dict):
                continue
            b = cast(dict[str, Any], block)
            if b.get("type") != "tool_use":
                continue
            name = b.get("name")
            if isinstance(name, str) and name.startswith(PREFIX):
                self._seen[name] += 1
            elif not self.outside:
                self.outside = True
                with self._spans("runner.ask.taint", source="claude_tool"):
                    pass
        self._stream.set()

    async def _synced(self, name: str) -> None:
        """Wait until the stream shows Claude's ``tool_use`` for this call, so every tool it
        used before is counted. Not seen in time: tainted (fail closed)."""
        key = PREFIX + name
        deadline = time.monotonic() + self.sync_s
        while self._seen[key] <= self._used[key]:
            left = deadline - time.monotonic()
            if left <= 0:
                if not self.tainted:
                    self.tainted = True
                    with self._spans("runner.ask.taint", source="unsynced"):
                        pass
                return
            self._stream.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stream.wait(), left)
        self._used[key] += 1

    # -- the relay's requests --

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closed:
            writer.close()
            return
        self._writers.add(writer)
        try:
            hello = _loads(await reader.readline())
            token = hello.get("token") if hello is not None else None
            if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
                with self._spans("runner.ask.mcp", refused=True):
                    pass
                return
            await _write(writer, {"ok": True})
            while line := await reader.readline():
                req = _loads(line)
                if req is None:
                    break
                rid, op = req.get("id"), req.get("op")
                if op == "list":
                    await _write(writer, {"id": rid, "tools": self.listing()})
                elif op == "call":
                    try:
                        text, error = await self.call(req.get("name"), req.get("args"))
                    except Exception as e:  # one bad call never kills the session's relay
                        log.warning("ask: tool call failed", extra={"error": type(e).__name__})
                        text, error = "not run: Resonant hit an error running that", True
                    await _write(writer, {"id": rid, "text": text, "error": error})
                else:
                    await _write(writer, {"id": rid, "text": "unknown op", "error": True})
        except (OSError, ValueError, asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def listing(self) -> list[dict[str, Any]]:
        """The MCP ``tools`` list: name, a cleaned one-paragraph description, full schema."""
        out: list[dict[str, Any]] = []
        for spec in self.specs.values():
            text = flat(spec.description, DESCRIPTION_CHARS)
            if spec.effect is not Effect.READ:
                text += CHANGE_NOTE
            out.append({"name": spec.name, "description": text, "inputSchema": spec.args_schema})
        return out

    async def call(self, name: Any, args: Any) -> tuple[str, bool]:
        """One tool call from Claude: ``(text, is_error)``. One at a time (pins and call
        indexes stay in order)."""
        async with self._lock:
            with self._spans("runner.ask.mcp_call", tool=flat(str(name), 64)) as s:
                text, error = await self._call(name, args)
                s.set(error=error, tainted=self.tainted)
                return text, error

    async def _call(self, name: Any, args: Any) -> tuple[str, bool]:
        if self._closed:
            return "not run: the session ended", True
        spec = self.specs.get(name) if isinstance(name, str) else None
        if spec is None:
            return NO_SUCH_TOOL.format(name=flat(str(name), 64)), True
        change = spec.effect is not Effect.READ
        if change:  # first, so every tool_use of a change is consumed even if refused
            await self._synced(spec.name)
        if not isinstance(args, dict):
            return "not run: invalid arguments: pass a JSON object", True
        args = cast(dict[str, Any], args)
        from resonant.runners.brain import args_error  # no import cycle at module load

        if (why := args_error(spec, args)) is not None:
            schema = json.dumps(spec.args_schema, sort_keys=True, separators=(",", ":"))
            return BAD_ARGS.format(why=why, name=spec.name, schema=schema), True
        if self._paused():
            return PAUSED, True
        if change:
            if self.pending is not None:
                return PENDING.format(what=self.pending), True
            if len(self.reports) >= MAX_SIDE_EFFECTS:
                return CAPPED, True
        if self.outside:
            self.tainted = True
        origin: Origin = "tainted" if self.tainted else "inferred"
        self.calls += 1
        index = self._index
        self._index += 1
        done = await self.caller._call_tool(  # pyright: ignore[reportPrivateUsage]
            self._view(),
            self.principal,
            [spec],
            spec.name,
            args,
            index,
            self.ctx,
            self._spans,
            list(self.reports),
            origin=origin,
            pin={"tainted": self.tainted},
        )
        if self.access.taints(spec):
            self.tainted = True
        return self._result(done)

    def _result(self, done: CallResult | Wait | Fail | Answer) -> tuple[str, bool]:
        if isinstance(done, Wait):
            decision = self.caller.deferred.pop(self.task.id, None)
            what = decision.summary if decision is not None else "the change"
            self.pending = what
            return ASKED.format(what=what), False
        if isinstance(done, Fail):
            return f"not run: {flat(done.error, 200)}", True
        if isinstance(done, Answer):  # refused, dry run, failed, ambiguous: code-built
            return f"not run: {done.reply}", True
        if done.report is not None:
            self.reports.append(done.report)
            return f"done: {done.report}", False
        if not done.ok:
            return f"error: {done.error or 'the tool failed'}", True
        body = canonical_json(done.data or {})
        if len(body) > RESULT_MAX_CHARS:
            body = body[: RESULT_MAX_CHARS - 13] + "…[truncated]"
        return body, False

    def _view(self) -> Task:
        """The ask task as it is stored now (the session's pid and marker included), so a
        pinned call's checkpoint keeps them."""
        latest = get_task(self.conn, self.task.id)
        return replace(self.task, checkpoint=latest.checkpoint if latest else self.task.checkpoint)

    def _paused(self) -> bool:
        try:
            return killswitch.is_paused(self.conn)
        except killswitch.PausedStateUnknownError:
            return True


def _loads(line: bytes) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return cast(dict[str, Any], obj) if isinstance(obj, dict) else None


async def _write(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write(json.dumps(obj).encode() + b"\n")
    await writer.drain()


__all__ = [
    "PREFIX",
    "SERVER",
    "ClaudeTools",
    "McpSetup",
    "ToolAccess",
    "ToolCaller",
]

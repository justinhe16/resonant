"""Resonant's tools for a Claude brain session (BR-10): the daemon side of the MCP bridge.

An ask the brain hands over (``AskRunner.enqueue(..., tools=True)``) gets, for the length of its
``claude -p`` session, a :class:`ClaudeTools`: a unix socket in a fresh ``0700`` temp dir, a
random token, and an MCP config that starts ``brain/mcp_stdio.py`` (a stdlib relay) with both.
The relay forwards ``tools/list`` and ``tools/call`` here; this side checks the token and runs
every call **bound to the ask task** (its principal, id, trace, step and checkpoint), so one
task's session can't act as another, whatever Claude or the relay sends.

Every call goes through ``ToolCore.call`` (TC-1, ``resonant.toolcore``), the same path as the
local model's: the registry spec (never the model's), the schema check, ``gate.evaluate`` with
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
  or expiry drops it (``AskRunner``: a ``LocalRunner`` named ``ask`` resumes the pinned call
  through ``ToolCore.resume``). One approval per task: after
  it, every other change is refused. At most ``MAX_SIDE_EFFECTS`` changes run per task.
- **Kill switch.** Paused: every call is refused here (the session itself is killed by the
  ask runner); autonomy off: the gate asks for everything above L0, as for the local model.

**One server, many turns** (BR-11). The socket and its token are an :class:`McpServer`. A
one-shot ask's ``ClaudeTools`` owns one for its session. The Claude brain's long-lived session
keeps one server for its whole life (the relay Claude Code started holds its socket and token)
and each turn's ``ClaudeTools`` **binds** to it for that turn only (``McpServer.bind``): every
call is served by the binding current when it arrives (that turn's task, principal, trace and
origin), a call between turns is refused (``NOT_BOUND``), and a binding that ended refuses
too. One owner turn's calls can't be attributed to another's.

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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, cast

from resonant import killswitch
from resonant.brain.text import flat
from resonant.gate import canonical_json
from resonant.gate.types import Origin
from resonant.loop.types import Fail, StepContext, Task
from resonant.models import (
    ChatMessage,
    ModelReply,
    ModelUnavailableError,
    ProbeResult,
    SpanFactory,
)
from resonant.observability.spans import span
from resonant.router.intents import ExtensionCatalog
from resonant.runners.local import gated_answer, model_result
from resonant.store.tasks import get_task
from resonant.toolcore import MAX_SIDE_EFFECTS, Asked, CallScope, ToolCore, args_error
from resonant_sdk import Effect, Level, ToolSpec

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

UNSURE = "error: Resonant hit an error; it may or may not have run. Check before retrying."
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
NOT_BOUND = "not run: no message from the owner is being answered right now"
BAD_ARGS = "not run: invalid arguments ({why}). {name} takes: {schema}"
CHANGE_NOTE = " (A change: Resonant's approval rules apply; it may ask the owner first.)"


@dataclass(frozen=True)
class ToolAccess:
    """What the bridge needs from the daemon: the shared tool call path (``ToolCore``: the
    registry, gate and executor) and the brain's tool list for a principal
    (``BrainRunner.tools_for``, flattened, minus ``ask_claude``)."""

    core: ToolCore
    tools_for: Callable[[str], Sequence[ToolSpec]]
    taints: Callable[[ToolSpec], bool]
    extensions: ExtensionCatalog | None = None
    timezone: str = "UTC"


class NoModel:
    """The ask runner's approval resume never asks a model; a stray call fails like a model
    that's down."""

    async def chat_json(self, messages: list[ChatMessage], **kw: Any) -> ModelReply:
        raise ModelUnavailableError("no model in the Claude tool bridge")

    async def chat_tools(self, messages: list[ChatMessage], **kw: Any) -> ModelReply:
        raise ModelUnavailableError("no model in the Claude tool bridge")

    async def probe(self) -> ProbeResult:
        return ProbeResult(False, False, False, False, None, "no model", False)


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
        core: ToolCore,
        task: Task,
        ctx: StepContext,
        *,
        tainted: bool,
        sync_s: float = SYNC_S,
        python: str = sys.executable,
        server: McpServer | None = None,
    ) -> None:
        """``server`` (BR-11): the Claude brain's long-lived server, bound to this turn by
        :meth:`start` and released by :meth:`aclose`; None: this bridge owns its own."""
        assert task.principal is not None
        self.conn = conn
        self.access = access
        self.core = core
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
        # BR-11: outside content came in during this session itself (one of Claude's own
        # tools, a tainting Resonant tool, an unsynced change), as opposed to ``tainted``
        # carried in from the asking turn or an earlier turn: the session's taint hops.
        self.read_outside = False
        self.changes = 0  # change calls that reached the gate (a crash retry must not repeat)
        self._index = 0
        self._shared = server is not None
        self.server = server if server is not None else McpServer(python=python)
        self._lock = asyncio.Lock()
        self._seen: Counter[str] = Counter()
        self._used: Counter[str] = Counter()
        self._stream = asyncio.Event()
        self._closed = False  # after aclose: every call is refused (review: late calls)
        self._spans: SpanFactory = partial(span, conn, trace_id=task.trace_id, task_id=task.id)

    # -- lifecycle --

    async def start(self) -> McpSetup:
        """Serve this task: its own server (socket and MCP config in a fresh 0700 dir), or
        the shared one, bound to it until :meth:`aclose`."""
        config = await self.server.start()
        self.server.bind(self)
        return McpSetup(config, tuple(PREFIX + n for n in self.specs))

    async def aclose(self) -> None:
        """Stop serving. A call still running (the session was killed mid-call) finishes
        first, up to ``CLOSE_WAIT_S``, so its report line isn't lost; any call after that is
        refused, and (own server) open connections are closed (a late call must never run
        outside the session's accounting). The bridge never prompts the owner itself: a call
        the gate asks about is only pinned, and the ask runner prompts after the answer. A
        shared server stays up for the next turn, unbound from this one."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._lock.acquire(), CLOSE_WAIT_S)
            self._closed = True
            self._lock.release()
        self._closed = True
        self.server.unbind(self)
        if not self._shared:
            await self.server.aclose()

    # -- the stream (taint) --

    def taint(self, source: str, **attrs: Any) -> None:
        """Taint the session from now on (once; a span says why)."""
        if self.tainted:
            return
        self.tainted = True
        with self._spans("runner.ask.taint", source=source, **attrs):
            pass

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
                self.outside = self.read_outside = True
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
                self.read_outside = True  # fail closed: as if it read something
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

    def listing(self) -> list[dict[str, Any]]:
        """The MCP ``tools`` list: name, a cleaned one-paragraph description, full schema."""
        return listing(self.specs.values())

    def refused(self) -> None:
        """A relay connection with a bad token (a span on this task's trace)."""
        with self._spans("runner.ask.mcp", refused=True):
            pass

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
        if (why := args_error(spec, args)) is not None:
            schema = json.dumps(spec.args_schema, sort_keys=True, separators=(",", ":"))
            return BAD_ARGS.format(why=why, name=spec.name, schema=schema), True
        if self._paused():
            return PAUSED, True
        # a pending call's pin must stay: nothing else that could be pinned runs after it
        if self.pending is not None and (change or spec.effective_level is not Level.L0):
            return PENDING.format(what=self.pending), True
        if change and len(self.reports) >= MAX_SIDE_EFFECTS:
            return CAPPED, True
        if self.outside:
            self.tainted = True
        if change:
            self.changes += 1
        origin: Origin = "tainted" if self.tainted else "inferred"
        self.calls += 1
        index = self._index
        self._index += 1
        view = self._view()
        scope = CallScope(
            view.id,
            self.principal,
            view.trace_id,
            view.step_no,
            index,
            origin,
            spans=self._spans,
            checkpoint=view.checkpoint,
            steps=self.ctx,
            effects=list(self.reports),
            pin={"tainted": self.tainted},
        )
        outcome = await self.core.call(spec, args, scope)
        if self.access.taints(spec):
            self.tainted = self.read_outside = True
        if isinstance(outcome, Asked):
            # Pinned, not prompted: the owner is asked after Claude's answer goes out.
            self.pending = outcome.summary
            return ASKED.format(what=outcome.summary), False
        if outcome.gated:
            done = gated_answer(outcome)
            if isinstance(done, Fail):
                return f"not run: {flat(done.error, 200)}", True
            if done.path not in ("done", "replay"):  # refused, dry run, failed, ambiguous
                return f"not run: {done.reply}", True
            self.reports.append(done.reply)
            return f"done: {done.reply}", False
        read = model_result(outcome, spec.name, detail=True)
        if not read.ok:
            return f"error: {read.error or 'the tool failed'}", True
        body = canonical_json(read.data or {})
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


def listing(specs: Iterable[ToolSpec]) -> list[dict[str, Any]]:
    """The MCP ``tools`` list of ``specs``: name, a cleaned one-paragraph description and the
    full schema; a change says Resonant's approvals apply."""
    out: list[dict[str, Any]] = []
    for spec in specs:
        text = flat(spec.description, DESCRIPTION_CHARS)
        if spec.effect is not Effect.READ:
            text += CHANGE_NOTE
        out.append({"name": spec.name, "description": text, "inputSchema": spec.args_schema})
    return out


class McpServer:
    """The daemon end of the relay: a unix socket (0600) in a fresh 0700 temp dir, a random
    token, and the MCP config that starts ``brain/mcp_stdio.py`` with both. Each request is
    served by the :class:`ClaudeTools` bound when it arrives (:meth:`bind`). None bound: a
    ``list`` gets ``idle_listing`` (the Claude brain may list its tools between turns) and a
    ``call`` is refused (``NOT_BOUND``). :meth:`start` is idempotent; :meth:`aclose` removes
    it all."""

    def __init__(
        self,
        *,
        python: str = sys.executable,
        idle_listing: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.python = python
        self.idle_listing = idle_listing
        self.bound: ClaudeTools | None = None
        self.config: Path | None = None
        self._token = secrets.token_urlsafe(32)
        self._dir: Path | None = None
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    def bind(self, tools: ClaudeTools) -> None:
        self.bound = tools

    def unbind(self, tools: ClaudeTools) -> None:
        """Release ``tools``, only if it is still the one bound."""
        if self.bound is tools:
            self.bound = None

    async def start(self) -> Path:
        """The socket, its server and the MCP config (both in a fresh 0700 dir): the
        config's path."""
        if self.config is not None:
            return self.config
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
        self.config = path
        return path

    async def aclose(self) -> None:
        self.bound = None
        server, self._server = self._server, None
        for writer in list(self._writers):
            writer.close()
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2)
        if self._dir is not None:
            await asyncio.to_thread(shutil.rmtree, self._dir, True)
            self._dir = None
        self.config = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._server is None:
            writer.close()
            return
        self._writers.add(writer)
        try:
            hello = _loads(await reader.readline())
            token = hello.get("token") if hello is not None else None
            if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
                if (bound := self.bound) is not None:
                    bound.refused()
                log.warning("ask: an MCP connection with a bad token was refused")
                return
            await _write(writer, {"ok": True})
            while line := await reader.readline():
                req = _loads(line)
                if req is None:
                    break
                rid, op = req.get("id"), req.get("op")
                bound = self.bound  # the turn current when the request arrives
                if op == "list":
                    if bound is not None:
                        tools = bound.listing()
                    else:
                        tools = self.idle_listing() if self.idle_listing is not None else []
                    await _write(writer, {"id": rid, "tools": tools})
                elif op == "call":
                    if bound is None:
                        text, error = NOT_BOUND, True
                    else:
                        try:
                            text, error = await bound.call(req.get("name"), req.get("args"))
                        except Exception as e:  # one bad call never kills the relay
                            log.warning("ask: tool call failed", extra={"error": type(e).__name__})
                            text, error = UNSURE, True
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
    "McpServer",
    "McpSetup",
    "NoModel",
    "ToolAccess",
]

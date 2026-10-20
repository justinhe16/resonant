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
- **Approvals** (BA-1: batches). A call the gate asks about joins the task's batch
  (``checkpoint.batch``, :attr:`ClaudeTools.asked`) but the owner is not prompted during the
  session: Claude gets ``asked the owner: <code-built summary>`` (never an approval code) and
  goes on. When the session ends its answer goes out first, then one prompt for every change
  in the batch (one change: the plain prompt), then the task waits; ``yes`` runs each once,
  in order (the executor's keys), ``no`` or expiry drops them all (``AskRunner``: a
  ``LocalRunner`` named ``ask`` runs the batch step through ``ToolCore.resume``). At most
  ``gate.batch_max`` changes wait and ``MAX_SIDE_EFFECTS`` run unasked per task.
- **Kill switch.** Paused: every call is refused here (the session itself is killed by the
  ask runner); autonomy off: the gate asks for everything above L0, as for the local model.

**Claude's own tools** (PER-258, the Claude brain only). The session runs Claude Code's full
built-ins with ``--permission-prompt-tool mcp__resonant__permission``: a read on the
pre-allowed list runs without asking; anything else is a permission request to this bridge
(``{tool_name, input, tool_use_id}``; probed on 2.1.304). It must match a ``tool_use`` the
stream showed (same id, name and input), so Claude can't call the tool itself to get a grant,
and every read before it has been counted for taint. Then ``ShellPolicy.request``
(``gate/shell.py``) builds the gate's args and the call goes through ``ToolCore`` as
``shell_read`` (L0) or ``shell`` (the gate's levels, never-list, taint, standing allows and
batches), and the answer is Claude Code's ``{"behavior": "allow", "updatedInput": ...}`` or
``{"behavior": "deny", "message": ...}``. A change the gate asks about joins the batch like
any other (the turn ends; ``AskRunner`` runs it in a new turn after the owner's yes, through
a one-shot grant, ``store.claude_brain.take_grant``). Every tool call in the stream (read or
write, Claude's own or Resonant's) gets a ``brain.tool`` audit row (tool, the command
redacted and clipped, cwd, decision, status) and a span; the changes that ran make the
turn's progress line (``progress``). The session's shell directory is tracked from the
stream (``ShellCwd``: a ``cd`` persists in Claude Code's Bash).

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
import re
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
from resonant.gate.shell import Call, ShellPolicy
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
from resonant.redact import Redactor
from resonant.router.intents import ExtensionCatalog
from resonant.runners.local import gated_answer, model_result
from resonant.store.audit import audit
from resonant.store.claude_brain import TOOL_ACTION, take_grant
from resonant.store.db import atomic, utcnow
from resonant.store.tasks import get_task
from resonant.toolcore import (
    MAX_SIDE_EFFECTS,
    Asked,
    BatchItem,
    CallOutcome,
    CallScope,
    Ran,
    Refused,
    ToolCore,
    args_error,
)
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

UNSURE = "error: Resonant hit an error; it may or may not have run. Check before retrying."
PAUSED = "not run: Resonant is paused (/kill); nothing runs until the owner resumes it."
CAPPED = (
    f"not run: at most {MAX_SIDE_EFFECTS} changes per task, and they already ran. Tell the "
    "owner what ran and that they can ask for the rest separately."
)
ASKED = (
    "asked the owner: {what}. Nothing ran yet: it runs only if they say yes, after your "
    "answer (every change that asks goes into one prompt, so go on with the rest). Tell "
    "them it's waiting on their yes; don't call it again."
)
NO_SUCH_TOOL = "not run: there is no Resonant tool named {name}"
NOT_BOUND = "not run: no message from the owner is being answered right now"
BAD_ARGS = "not run: invalid arguments ({why}). {name} takes: {schema}"
CHANGE_NOTE = " (A change: Resonant's approval rules apply; it may ask the owner first.)"
# PER-258: Claude Code's permission prompt tool (``--permission-prompt-tool``).
PERMISSION = "permission"
PERMISSION_TOOL = PREFIX + PERMISSION
PERMISSION_LISTING: dict[str, Any] = {
    "name": PERMISSION,
    "description": (
        "Internal: Claude Code's permission check for Claude's own tools; Resonant's approval "
        "rules decide. Never call it yourself."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}
SHELL_ASKED = (
    "asked the owner: {what}. Nothing ran. If they say yes, Resonant sends you a new message "
    "to run it then. Until then don't run it, retry it or work around it (another command, a "
    "script, an edit): tell them it's waiting on their yes."
)
SHELL_REFUSED = "not run: {why} Never try another way to do it."
SHELL_UNMATCHED = "not run: that request doesn't match a tool call in this turn"
SHELL_OFF = "not run: Claude's own changes aren't available in this session"
SHELL_RESULT_CHARS = 4000  # of a tool result, for the cwd and the progress line
# Claude's own tools whose use reads nothing first: Bash taints once its output comes back
# (a ``git log`` reads commit messages), an edit or a write not at all (its input is
# Claude's own; Claude Code makes it Read a file before editing it, and that taints).
OWN_CHANGES = frozenset({"Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"})


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
    shell: ShellPolicy | None = None  # PER-258: the Claude brain's own tool calls


@dataclass
class ShellCwd:
    """The Claude brain's shell directory (PER-258): where its Bash is now. The session owns
    it (reset at each start: the CLI starts in its cwd); each turn's bridge updates it."""

    path: str


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
        shell: ShellCwd | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        """``server`` (BR-11): the Claude brain's long-lived server, bound to this turn by
        :meth:`start` and released by :meth:`aclose`; None: this bridge owns its own.
        ``shell`` (PER-258): the session's shell directory; with ``access.shell`` it serves
        Claude Code's permission requests (None: every one is denied). ``redactor``: the
        runner's secrets, out of every command shown or audited."""
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
        # BA-1: the changes the gate asked about, pinned in ``checkpoint.batch`` (in order);
        # after the answer the owner gets one prompt for all of them.
        self.asked: list[BatchItem] = []
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
        # PER-258: Claude's own tool calls.
        self.shell = shell if access.shell is not None else None
        self.policy = access.shell
        self.redactor = redactor or Redactor(())
        self.progress: list[str] = []  # the changes of Claude's own that ran, in words
        self._uses: dict[str, tuple[str, dict[str, Any]]] = {}  # tool_use id -> name, input
        self._decided: dict[str, tuple[str, Call | None]] = {}  # id -> decision, the call

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
        (anything but ours) reads outside content: the session is tainted from then on. A
        tool result closes its call's audit row (PER-258)."""
        if msg.get("type") == "user":
            self._results(msg)
            return
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
            bid, given = b.get("id"), b.get("input")
            if isinstance(bid, str) and isinstance(name, str):
                self._uses[bid] = (name, cast(dict[str, Any], given) if isinstance(given, dict)
                                   else {})  # fmt: skip
            if isinstance(name, str) and name.startswith(PREFIX):
                self._seen[name] += 1
            elif name not in OWN_CHANGES:
                self._outside_read()
        self._stream.set()

    def _outside_read(self) -> None:
        """One of Claude's own tools read something: tainted from now on (once, a span)."""
        if not self.outside:
            self.outside = self.read_outside = True
            with self._spans("runner.ask.taint", source="claude_tool"):
                pass

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
        """The MCP ``tools`` list: name, a cleaned one-paragraph description, full schema
        (and the permission tool, for the Claude brain)."""
        out = listing(self.specs.values())
        return [*out, PERMISSION_LISTING] if self.shell is not None else out

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
        if name == PERMISSION:
            return await self._permission(args)
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
            batch=self.asked,
        )
        outcome = await self.core.call(spec, args, scope)
        if self.access.taints(spec) and _shows_outside(spec, outcome):
            self.tainted = self.read_outside = True
        if isinstance(outcome, Asked):
            # Pinned in the batch, not prompted: the owner is asked after Claude's answer.
            item = outcome.item
            if item is not None and all(i.approval_id != item.approval_id for i in self.asked):
                self.asked.append(item)
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

    # -- Claude's own tools (PER-258) --

    async def _permission(self, raw: Any) -> tuple[str, bool]:
        """One permission request from Claude Code: the gate's decision, as Claude Code's
        answer (never an MCP error: an error would read as a denial with no reason)."""
        if self._closed:
            return _deny("not run: the session ended")
        req = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        tool, given, tid = req.get("tool_name"), req.get("input"), req.get("tool_use_id")
        if not isinstance(tool, str) or not isinstance(given, dict) or not isinstance(tid, str):
            return _deny("not run: a malformed permission request")
        given = cast(dict[str, Any], given)
        policy, shell = self.policy, self.shell
        if policy is None or shell is None:
            return _deny(SHELL_OFF)
        if not await self._use_seen(tid, tool, given):
            return _deny(SHELL_UNMATCHED)
        if self._paused():
            self._decided[tid] = ("refused", None)
            return _deny(PAUSED)
        call = policy.request(tool, given, shell.path, self._shown)
        spec = self.core.registry.get(None, call.spec)
        if spec is None:
            return _deny(SHELL_OFF)
        if self.outside:
            self.tainted = True
        write = call.verdict.kind != "read"
        if call.verdict.kind == "write" and take_grant(self.conn, call.args, now=utcnow()):
            self._decided[tid] = ("granted", call)
            with self._spans("runner.ask.shell", decision="granted", verb=call.args["verb"]):
                pass
            return _allow(given)
        if write:
            self.changes += 1
        origin: Origin = "tainted" if self.tainted else "inferred"
        self.calls += 1
        index = self._index
        self._index += 1
        view = self._view()
        scope = CallScope(view.id, self.principal, view.trace_id, view.step_no, index, origin,
                          spans=self._spans, checkpoint=view.checkpoint, steps=self.ctx,
                          effects=list(self.reports), pin={"tainted": self.tainted},
                          batch=self.asked)  # fmt: skip
        outcome = await self.core.call(spec, call.args, scope)
        if isinstance(outcome, Asked):
            item = outcome.item
            if item is not None and all(i.approval_id != item.approval_id for i in self.asked):
                self.asked.append(item)
            self._decided[tid] = ("asked", call)
            return _deny(SHELL_ASKED.format(what=outcome.summary))
        if isinstance(outcome, Ran) and outcome.result.ok:
            self._decided[tid] = ("gate" if write else "read", call)
            return _allow(given)
        self._decided[tid] = ("refused", call)
        why = outcome.text if isinstance(outcome, Refused) else "Resonant couldn't check it."
        why = why.removeprefix("Not done: ")
        return _deny(SHELL_REFUSED.format(why=flat(why, 300)))

    async def _use_seen(self, tid: str, tool: str, given: dict[str, Any]) -> bool:
        """The stream showed this ``tool_use`` (same id, tool and input), within ``sync_s``."""
        deadline = time.monotonic() + self.sync_s
        while tid not in self._uses:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self._stream.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stream.wait(), left)
        name, seen = self._uses[tid]
        return name == tool and canonical_json(seen) == canonical_json(given)

    def _shown(self, text: str) -> str:
        return self.redactor.text(text)

    def _results(self, msg: dict[str, Any]) -> None:
        inner = msg.get("message")
        content = cast(dict[str, Any], inner).get("content") if isinstance(inner, dict) else None
        if not isinstance(content, list):
            return
        for block in cast(list[Any], content):
            if not isinstance(block, dict):
                continue
            b = cast(dict[str, Any], block)
            tid = b.get("tool_use_id")
            if b.get("type") != "tool_result" or not isinstance(tid, str) or tid not in self._uses:
                continue
            name, given = self._uses.pop(tid)
            text = _result_text(b.get("content"))
            decided = self._decided.pop(tid, None)
            ran = decided is None or decided[0] not in ("asked", "refused")
            if name == "Bash" and ran:  # its output is outside content (a commit message...)
                self._outside_read()
            self._record(name, given, text, b.get("is_error") is True, decided)

    def _record(
        self,
        name: str,
        given: dict[str, Any],
        text: str,
        error: bool,
        decided: tuple[str, Call | None] | None,
    ) -> None:
        """One tool call's audit row and span, the shell directory, the progress line."""
        call = decided[1] if decided is not None else None
        if name.startswith(PREFIX):
            decision, shown, cwd = "resonant", name.removeprefix(PREFIX), ""
        else:
            denied = error and "has been denied" in text[:300]
            decision = decided[0] if decided is not None else ("denied" if denied else "allowed")
            raw = call.args.get("command") if call is not None else _what(name, given)
            shown = flat(self._shown(str(raw)), 200)
            cwd = str(call.args.get("cwd")) if call is not None else (
                self.shell.path if self.shell is not None else "")  # fmt: skip
        write = call is not None and call.verdict.kind == "write"
        status = "error" if error else "ok"
        exit_code = _EXIT.match(text)
        with atomic(self.conn):
            audit(self.conn, TOOL_ACTION, task_id=self.task.id, requested_by=self.principal,
                  trace_id=self.task.trace_id, tool=flat(name, 64), decision=decision,
                  command=shown, cwd=cwd, status=status, write=write,
                  exit=int(exit_code.group(1)) if exit_code else None)  # fmt: skip
        with self._spans("runner.ask.brain_tool", tool=flat(name, 64), decision=decision,
                         status=status, write=write):  # fmt: skip
            pass
        policy, shell = self.policy, self.shell
        if name == "Bash" and policy is not None and shell is not None:
            command = given.get("command")
            if isinstance(command, str):
                if not error:
                    shell.path = policy.cwd_after(command, shell.path, text)
                elif m := _RESET.search(text):
                    shell.path = m.group(1).strip()
        ran = write and not error and decision in ("gate", "granted")
        if ran and call is not None and policy is not None:
            self.progress.append(policy.did(call.args, text))

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


_EXIT = re.compile(r"Exit code (\d+)")
_RESET = re.compile(r"Shell cwd was reset to (/[^\n]+)")


def _allow(given: dict[str, Any]) -> tuple[str, bool]:
    """Claude Code's answer to a permission request: run it as asked."""
    return json.dumps({"behavior": "allow", "updatedInput": given}), False


def _deny(message: str) -> tuple[str, bool]:
    return json.dumps({"behavior": "deny", "message": message}), False


def _result_text(content: Any) -> str:
    """A tool result's text (a string, or text blocks), clipped."""
    if isinstance(content, str):
        return content[:SHELL_RESULT_CHARS]
    parts: list[str] = []
    if isinstance(content, list):
        for c in cast(list[Any], content):
            if isinstance(c, dict):
                t = cast(dict[str, Any], c).get("text")
                if isinstance(t, str):
                    parts.append(t)
    return "\n".join(parts)[:SHELL_RESULT_CHARS]


def _what(name: str, given: dict[str, Any]) -> str:
    """What a pre-allowed tool call touched: a command, a path, a pattern or a URL."""
    for key in ("command", "file_path", "notebook_path", "path", "pattern", "url", "query"):
        v = given.get(key)
        if isinstance(v, str):
            return v
    return name


def _shows_outside(spec: ToolSpec, outcome: CallOutcome) -> bool:
    """Whether what Claude got back from this tainting tool carries outside content (D6). A
    read does (its result). A call that didn't run (asked, refused) doesn't. An extension's
    change that ran doesn't either (BA-1): Claude gets only its code-built report line
    (``Done: linear: save issue id=…``, from the args Claude chose), never the result; a
    failed one does (its reason may be the extension's own text)."""
    if isinstance(outcome, Asked | Refused):
        return False
    change = spec.effect is not Effect.READ
    return not (change and spec.extension is not None and isinstance(outcome, Ran))


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
                    if bound is None and req.get("name") == PERMISSION:
                        text, error = _deny(NOT_BOUND)
                    elif bound is None:
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

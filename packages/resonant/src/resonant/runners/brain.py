"""BrainRunner (``runner="brain"``): the general agent loop on the local model (docs/brain-plan.md
D1, D4, D6, D7). With ``brain.mode: loop`` the router sends every non-fast-path chat message
here instead of to the labeler; the fast path, approval replies, the relay and ``/kill`` are
unchanged.

It is a ``LocalRunner`` with a different answer step, so everything after "the model picked a
tool" is the local runner's: ``_call_tool`` (registry spec, ``gate.evaluate``, executor), the
pinned gated call and its resume after an approval, the code-built report lines
(``REPORTS``), at most ``MAX_SIDE_EFFECTS`` side effects without asking and one approval per
task, and the resend of a saved reply. A checkpoint that holds ``call`` (or any of the local
runner's keys) is handed to ``LocalRunner._answer`` as is.

**One turn** (a ``kind="chat"`` task with no ``call``):

1. **Tools** (D4). The principal's tools, filtered by ``principals.may_request`` before
   anything is listed, then made unique (``router.chat_toolset``). ``CORE_TOOLS`` are
   offered with their full schemas; every other tool (builtins and extension chat tools,
   never job specs or ``NOT_IN_BRAIN``) is offered as a **stub** (``stub``): its exact name,
   its one-line summary and its argument names, with an open schema. A stub call is checked
   against the real schema here and runs like any call; a bad one gets the schema back. The
   toolset is fixed for the whole task (sorted by name), so the prompt prefix stays cached.
   Why stubs and not a name-only catalog: Ollama constrains a call's name to the offered
   tools, so a call to a tool that isn't offered comes back as a *different* tool
   (``list_repos`` became ``list_reminders``).
2. **Context** (``brain.build_context``, D2): system, the tools, facts, history, then the
   briefing (with the Claude runner's real set-up gaps, ``runner_gaps``), the "since we last
   talked" notes, the time and the message.
3. **Rounds.** At most ``MAX_ROUNDS`` ``chat_tools`` calls. A reply with no tool calls is the
   answer (clipped to ``MAX_REPLY_CHARS``). Calls and results are **native tool turns** (an
   assistant turn with ``tool_calls``, then a ``tool`` turn holding the result, labeled
   untrusted data), so the model never sees a call as text to imitate; a call the model
   still writes as text (``{"tool_call": …}``, ``<tool_call>``) is run as a call, never sent.
   A failed read's ``ToolFailed`` reason goes back to the model (``tool_error_detail``). Each
   call goes through ``_call_tool`` with the
   task's origin: ``inferred`` until a tool result came from outside content, ``tainted``
   from then on (D6). Outside content is a spec with ``untrusted=True`` (the read tools,
   ``ask_claude``) or any extension or MCP tool. A gated call is pinned with ``tainted``, so
   its resume after an approval is gated as ``tainted`` too (``_pinned_origin``). Reads are
   free inside the rounds (``MAX_CALLS`` is only a backstop).
   **Target check** (once per task): a side effect that picks its target by an identifier
   the owner's message doesn't contain (``cancel_run{code: R2}`` for "cancel the run")
   isn't run; the model gets ``TARGET_CHECK`` back (ask which if several fit, else call
   again), since an L3 write runs without asking.
   **Guess check** (BR-9, once per task): a read that picks its target by an identifier
   found nowhere the model could have read it (the message, history, briefing, tool
   results; a ``number`` must be written as one, ``#104``) isn't run; the model gets
   ``GUESS_CHECK`` back (list them first, or ``ask_claude``). "Most recent PRs?" had
   become ``pr{number: 1}``.
   **Corrective round** (once per task): an answer that claims a change (``claims_change``)
   while no side effect ran goes back to the model with a short check (``CLAIM_NUDGE``)
   instead of out to the owner; so does (BR-9) an answer to a live-state question
   (``asks_live_state``: commits, PRs, what changed, a status) when no tool ran at all
   (``LOOKUP_NUDGE``), unless it is a question back.
4. **ask_claude.** When the model calls it and it queued, its text (``Looking into it…``) is
   the reply and the turn ends: the ask task answers later. A refusal goes back to the model.
5. **Stuck.** The round cap with no answer, or two bad model outputs, escalates on its own:
   ``ask_claude`` with the message and a short note of what was tried, through the same gate
   and executor. When the principal may not use it, or it refuses (the daily cap, off,
   paused), the round cap replies ``TOO_MANY_REPLY`` and bad output falls back to the
   intents path.
6. **Number guard** (D7): log-only here. Numbers in the reply that appear nowhere in the
   prompt or the tool results are counted (``number_guard`` on the span and in
   ``Done.result``); the reply is kept. The system prompt says to state only what it read.
7. **Progress.** If the turn is still going after ``progress_after_s`` (6 s), ``On it…`` goes
   out once (dedupe ``progress:<task id>``), before the reply.
8. **Model down.** ``ModelUnavailableError`` (or bad output that can't escalate) runs the
   intents path for this turn (the labeler, then ``LocalRunner._answer``), unless a side
   effect already ran: then its report lines are the reply.
9. **After the reply** (``step``): when ``history.fold_due``, the history fold runs as a
   background task, never on the reply path.

Message bodies are PII: spans and logs carry lengths, names and counts, never text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
from typing import Any, cast

import jsonschema

from resonant.brain.context import build_context, catalog_entries
from resonant.brain.facts import FactStore
from resonant.brain.history import fold, fold_due
from resonant.brain.text import flat
from resonant.config import Settings
from resonant.gate.types import Origin
from resonant.gateway.channel import Channel
from resonant.loop.slots import SlotPool
from resonant.loop.types import Done, StepContext, StepOutcome, Task
from resonant.models import (
    ChatMessage,
    ModelClient,
    ModelOutputError,
    ModelReply,
    ModelUnavailableError,
    SpanFactory,
)
from resonant.models.client import text_tool_calls
from resonant.observability.spans import span
from resonant.principals import Principals
from resonant.router.intents import ExtensionCatalog
from resonant.router.labeler import Labeler
from resonant.router.router import builtin_names, chat_toolset, toolset_entries, toolset_for
from resonant.runners.local import (
    MAX_SIDE_EFFECTS,
    MODEL_RESULT_MAX_CHARS,
    NO_ANSWER_REPLY,
    REPLY_SCHEMA,
    TOO_MANY_REPLY,
    Answer,
    CallResult,
    LocalRunner,
    PinnedCall,
    RunnerGate,
    StepResult,
    ToolExecutor,
    after_effects,
    clip_reply,
    is_explicit,
    tool_turn,
    unsupported_numbers,
    used_tools,
    with_report,
)
from resonant.store.db import utcnow
from resonant.tools import ToolRegistry
from resonant.tools.ask import ASK_CLAUDE, QUESTION_MAX
from resonant_sdk import Effect, Event, ToolSpec

log = logging.getLogger(__name__)

NAME = "brain"
MAX_ROUNDS = 8  # chat_tools calls per task (D7)
MAX_CALLS = 24  # tool calls per task, any kind: a backstop, reads are free inside the rounds
MAX_REPLY_CHARS = 1500  # D7
MAX_MODEL_ERRORS = 2  # bad outputs before the loop gives up (escalates)
TOOLS_MAX_TOKENS = 1024  # a reply may be the answer, up to MAX_REPLY_CHARS
FINAL_MAX_TOKENS = 700
PROGRESS_AFTER_S = 6.0
PROGRESS_TEXT = "On it…"
RUNNER_GAPS_TTL_S = 60.0  # as the dashboard's runner check (api.RUNNER_CHECK_TTL_S)
RUNNER_GAPS_TIMEOUT_S = 30.0  # a background check; never on the reply path
TRIED_MAX_CHARS = 300  # the "what was tried" note in an escalated question

# Always loaded (D4): the read tools, facts, ask_claude, notes and reminders, runs, the Mini.
CORE_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "list_dir",
        "search",
        "tail_log",
        "read_transcript",
        "remember",
        "forget",
        "list_facts",
        "ask_claude",
        "add_note",
        "list_notes",
        "add_reminder",
        "list_reminders",
        "list_runs",
        "run_activity",
        "start_run",
        "system_status",
        "recent_changes",
    }
)
# Builtins whose results carry text others wrote (a Claude worker's notes, questions and
# transcript lines, log lines, extension items): they taint like ``untrusted`` specs (D6).
# Workers read PRs and web pages, so their text is outside content too.
TAINTING_BUILTINS: frozenset[str] = frozenset(
    {
        "list_runs",
        "run_activity",
        "answer_worker",
        "message_worker",
        "recent_logs",
        "open_items",
        "pulse_preview",
        "briefing_preview",
    }
)
# Control-plane tools that stay slash-only (``/ext mute``), as on the intents path.
NOT_IN_BRAIN: frozenset[str] = frozenset({"mute_extension"})

TOOL_ERROR_CHARS = 300  # a failed tool's reason, as the model sees it

FINAL_INSTRUCTION = (
    "Now answer the owner's message from what you read. Reply with only a JSON object: "
    f'{{"reply": "<plain text, at most {MAX_REPLY_CHARS} characters>"}}.'
)
NO_SUCH_TOOL = "there is no tool named {name}; use one of the tools you were given"
BAD_STUB_ARGS = "invalid arguments ({why}). {name} takes: {schema}. Call it again with those."

# The corrective round (once per task): an answer that claims a change no tool made.
CLAIM_NUDGE = (
    "Check: no tool ran in this task, so nothing was changed. If the owner asked for a "
    "change, call the tool for it now (Resonant asks the owner first when it must). If you "
    "can't, say so plainly. Never say something was done unless a tool result says so."
)
# A change's target the owner didn't name: once per task, the call goes back to the model
# unrun (``unnamed_target``).
TARGET_CHECK = (
    "not run yet: the owner's message doesn't name {target}. If more than one item could "
    "fit, don't guess: ask the owner which one, in a short question ending in '?'. If only "
    "this one fits, call {name} again."
)
TARGET_KEYS = frozenset({"code", "id", "number"})
# A read whose identifier appears nowhere the model could have got it from: once per task,
# the call goes back unrun (``guessed_arg``). BR-9: "most recent PRs?" became pr{number: 1}.
GUESS_CHECK = (
    "not run: {target} isn't in the owner's message or anything you've read, so it would be "
    "a guess. Don't guess: find it first with a tool that lists them (recent_changes or "
    "recent_prs for the newest commits and PRs, prs for open PRs, list_runs for runs), or "
    "call ask_claude. If the owner did give it, call {name} again."
)
# A PR or issue number counts as read only where it is written as one ("#104", "PR 104",
# ".../pull/104", a result's "number": 104); a bare digit is everywhere ("slots 0/1").
_NUMBER_CONTEXT = r'(?:#|\bpr\s*#?|\bpull request\s*#?|/pull/|"number":\s*|"pr":\s*|\bnumber\s*)'


def unnamed_target(spec: ToolSpec, args: dict[str, Any], message: str) -> str | None:
    """``code=R2`` when a side effect picks its target by an identifier (``code``, ``id``,
    ``number`` or ``*_id``) that the owner's message doesn't contain, else None. Reads, and
    calls whose target is named ("cancel R2", "merge PR 41"), pass."""
    if spec.effect is Effect.READ:
        return None
    lowered = message.lower()
    words = set(re.findall(r"[a-z0-9]+", lowered))
    for key, value in args.items():
        if not _key_of(key):
            continue
        if isinstance(value, bool) or not isinstance(value, str | int):
            continue
        token = str(value).strip().lower().lstrip("#")
        named = token in words or (not token.isalnum() and token in lowered)
        if token and not named:
            return f"{flat(key, 32)}={flat(str(value), 40)}"
    return None


def _key_of(key: str) -> bool:
    return key in TARGET_KEYS or key.endswith("_id")


def guessed_arg(spec: ToolSpec, args: dict[str, Any], message: str, seen: str) -> str | None:
    """``number=1`` when a read picks its target by an identifier (``code``, ``id``,
    ``number`` or ``*_id``) that is in neither the owner's ``message`` nor ``seen`` (the rest
    of the prompt: history, briefing, tool results), else None. A ``number`` must appear
    written as one (``#1``, ``PR 1``, ``"number": 1``) unless the message has it. Side
    effects are ``unnamed_target``'s."""
    if spec.effect is not Effect.READ:
        return None
    words = set(re.findall(r"[a-z0-9]+", message.lower()))
    lowered = seen.lower()
    for key, value in args.items():
        if not _key_of(key) or isinstance(value, bool) or not isinstance(value, str | int):
            continue
        token = str(value).strip().lower().lstrip("#")
        if not token or token in words or (not token.isalnum() and token in message.lower()):
            continue
        tok = re.escape(token)
        if key == "number":
            found = re.search(rf"{_NUMBER_CONTEXT}{tok}(?![0-9])", lowered)
        else:
            found = re.search(rf"(?<![a-z0-9]){tok}(?![a-z0-9])", lowered)
        if found is None:
            return f"{flat(key, 32)}={flat(str(value), 40)}"
    return None


# A question about live state (BR-9: "status on resonant repo changes", "most recent
# commits/PRs?") answered with no tool at all gets one corrective round (``LOOKUP_NUDGE``).
_LIVE_STATE = re.compile(
    r"\b(?:commits?|changes|changed|changelog|newest|latest|most recent|merged|prs|"
    r"pull requests)\b",
    re.IGNORECASE,
)
LOOKUP_NUDGE = (
    "Check: you answered without calling any tool, but this asks about live state (a repo's "
    "commits or PRs, what changed, a status). Your history and summary may be out of date. "
    "Call the tool that reads it now (recent_changes, recent_prs, prs, list_runs, "
    "system_status), or ask_claude if none fits. If it really needs no lookup, give the "
    "same answer again."
)


# The answer is a question back to the owner (a trailing quote, bracket or emoji allowed).
_ASKS_BACK = re.compile(r"\?[^\w?]*$")


def asks_live_state(message: str) -> bool:
    """The owner's message asks about something that changes (commits, PRs, a status)."""
    return _LIVE_STATE.search(message) is not None


# "Added note: …", "I've saved that…", "Done, merged #41", "Cancelled R2": a first-person or
# leading past-tense claim of a change. Reads that quote a log ("R1 started at …") don't
# match: the verb must open the reply (after an optional "ok"/"done"/"sure") or follow "I".
_CHANGE_VERBS = (
    r"(?:added|saved|created|made|merged|started|launched|kicked off|deleted|removed|cleared|"
    r"sent|scheduled|cancell?ed|stopped|killed|restarted|remembered|noted|updated|"
    r"forgot(?:ten)?|opened|filed|closed|approved|logged)"
)
# A promise ("I'll remember that") with no call is as empty as a claim.
_PROMISE_VERBS = (
    r"(?:add|save|create|make|merge|start|launch|delete|remove|clear|send|schedule|set|cancel|"
    r"stop|kill|restart|remember|note|update|forget|open|file|close|approve|remind)"
)
# "set" only after "I": a reply that opens "Set a reminder …?" is a proposal, not a claim.
# A leading past participle that describes something ("Created 3 days ago by sam", "Merged
# 2h ago", "Logged in as …", "Sent from the Mini") is a read, not a claim.
_NOT_A_CLAIM = r"(?!\s+(?:\d|ago\b|by\b|from\b|in\b|at\b|on\b|yesterday\b|today\b|earlier\b))"
_CLAIM = re.compile(
    rf"(?:^\W*(?:resonant\W+)?(?:(?:ok(?:ay)?|done|sure|got it)\W+)?{_CHANGE_VERBS}\b"
    rf"{_NOT_A_CLAIM}"
    rf"|\bI(?:'ve| have| just)?\s+(?:just\s+|already\s+)?(?:set|{_CHANGE_VERBS})\b"
    rf"|\bI(?:'ll| will)\s+{_PROMISE_VERBS}\b"
    rf"|\b{_CHANGE_VERBS}\s+(?:it\s+)?(?:as an?|to your|to the)\s+"
    r"(?:notes?|reminders?|facts?)\b"
    rf"|\b(?:reminder|note|ticket|issue|run)\s+(?:is\s+|has been\s+)?"
    r"(?:set|saved|added|created|started|filed)\b)",
    re.IGNORECASE,
)


def claims_change(reply: str) -> bool:
    """A reply that says it changed something (to check against what actually ran)."""
    return _CLAIM.search(reply) is not None


_SLIM_KEYS = ("type", "enum", "items", "properties", "required")


def slim_schema(schema: Any, depth: int = 0) -> dict[str, Any]:
    """``schema`` with only what shapes a call: ``type``, ``enum``, ``items``, ``properties``
    and ``required`` (no descriptions, limits or formats), three levels deep. An object
    always has ``properties`` (llama.cpp's grammar needs it, and allows no others)."""
    if not isinstance(schema, dict) or depth > 2:
        return {}
    src = cast(dict[str, Any], schema)
    out: dict[str, Any] = {}
    for key in _SLIM_KEYS:
        if key not in src:
            continue
        value: Any = src[key]
        if key == "properties" and isinstance(value, dict):
            props = cast(dict[str, Any], value)
            out[key] = {flat(str(k), 64): slim_schema(v, depth + 1) for k, v in props.items()}
        elif key == "items":
            out[key] = slim_schema(value, depth + 1)
        elif key == "required" and isinstance(value, list):
            out[key] = [flat(str(r), 64) for r in cast(list[Any], value)]
        elif key == "enum" and isinstance(value, list):
            out[key] = [v for v in cast(list[Any], value) if isinstance(v, str | int | float)]
        elif key == "type" and isinstance(value, str | list):
            out[key] = value
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


def stub(spec: ToolSpec) -> ToolSpec:
    """A deferred tool as the model sees it (D4): its exact name, its one-line summary
    (``catalog_entries``) and a slim schema (``slim_schema``). Offering every tool by name
    lets the decoder name it exactly: Ollama constrains a call's name (and args) to the
    offered tools, so a call to a tool that isn't offered comes back as a different one. The
    call is checked against the real schema in ``_one``; the toolset never changes inside a
    task, so the prompt prefix stays cached."""
    (entry,) = catalog_entries([spec])
    schema = slim_schema(spec.args_schema) or {"type": "object", "properties": {}}
    return ToolSpec(
        name=spec.name, description=entry.summary, args_schema=schema, effect=Effect.READ
    )


def args_error(spec: ToolSpec, args: dict[str, Any]) -> str | None:
    """Why ``args`` don't fit ``spec`` (a short reason, no values), or None."""
    try:
        jsonschema.validate(args, spec.args_schema)
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.path)
        return f"{e.validator}{' at ' + where if where else ''}"
    return None


@dataclass
class _Turn:
    """One brain turn's state: the prompt so far, the tools and what ran."""

    messages: list[ChatMessage]
    events: list[Event]  # the step's events, for the intents fallback
    core: list[ToolSpec]
    deferred: dict[str, ToolSpec]  # by model-facing name; offered as stubs
    calls: list[CallResult] = field(default_factory=list[CallResult])
    tainted: bool = False
    errors: int = 0
    executed: int = 0  # tool calls run through the gate (their call_index)
    nudged: bool = False  # the claim corrective round was used
    looked: bool = False  # the lookup corrective round was used (BR-9)
    text: str = ""  # the owner's message (the target check)
    target_checked: bool = False  # the one target check was used
    guess_checked: bool = False  # the one guessed-identifier check was used
    tools: list[ToolSpec] = field(default_factory=list[ToolSpec])

    def __post_init__(self) -> None:
        stubs = [stub(s) for s in self.deferred.values()]
        self.tools = sorted([*self.core, *stubs], key=lambda s: s.name)

    def toolset(self) -> list[ToolSpec]:
        """What the model is offered: the core tools and a stub per deferred tool, by name.
        The same for the whole task (the prompt prefix stays cached)."""
        return self.tools

    def seen(self) -> str:
        """Everything the model has read in this task but the system prompt: history, the
        briefing and the message, and every tool result so far."""
        return "\n".join(str(m.get("content") or "") for m in self.messages[1:])

    def lookup(self, name: str) -> ToolSpec | None:
        """The real spec for a model-facing name (core or deferred)."""
        return next((s for s in self.core if s.name == name), None) or self.deferred.get(name)

    @property
    def origin(self) -> Origin:
        return "tainted" if self.tainted else "inferred"

    @property
    def reports(self) -> list[str]:
        return [c.report for c in self.calls if c.report is not None]


def taints(spec: ToolSpec) -> bool:
    """D6: a result from outside content (``untrusted`` specs, extension and MCP tools, and
    ``TAINTING_BUILTINS``). Resonant's own state reads don't taint."""
    return spec.untrusted or spec.extension is not None or spec.name in TAINTING_BUILTINS


class BrainRunner(LocalRunner):
    """``runner="brain"``. The local runner's collaborators, plus what the context needs."""

    name = NAME
    tool_error_detail = True  # a failed read's reason goes back to the model

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        model: ModelClient,
        channel: Channel,
        gate: RunnerGate,
        executor: ToolExecutor,
        registry: ToolRegistry,
        settings: Settings,
        principals: Principals,
        facts: FactStore | None = None,
        started_at: datetime | None = None,
        last_tick: Callable[[], datetime | None] = lambda: None,
        slots: SlotPool | None = None,
        max_step_s: float | None = None,
        now: Callable[[], datetime] = utcnow,
        extensions: ExtensionCatalog | None = None,
        model_result_max_chars: int = MODEL_RESULT_MAX_CHARS,
        progress_after_s: float = PROGRESS_AFTER_S,
        runner_gaps: Callable[[], Awaitable[list[str] | None]] | None = None,
    ) -> None:
        super().__init__(
            conn,
            model=model,
            channel=channel,
            gate=gate,
            executor=executor,
            registry=registry,
            max_step_s=max_step_s,
            now=now,
            timezone=settings.timezone,
            extensions=extensions,
            model_result_max_chars=model_result_max_chars,
        )
        self.settings = settings
        self.principals = principals
        self.facts = facts
        self.started_at = started_at or now()
        self.last_tick = last_tick
        self.slots = slots
        self.progress_after_s = progress_after_s
        # The Claude runner's set-up gaps for the briefing (the daemon passes the dashboard's
        # cached check, ``api.runner_missing``); None, or a check that returns None, fails or
        # is slow: the briefing says nothing about workers.
        self.runner_gaps = runner_gaps
        self._gaps: tuple[float, list[str]] | None = None
        self._gaps_job: asyncio.Task[None] | None = None
        self._closed = False
        self.labeler = Labeler(model, self.intents)  # the intents fallback
        self._folds: set[asyncio.Task[None]] = set()
        self._folding: set[str] = set()  # principals with a fold in flight

    # --- step: the local runner's, then the fold ------------------------------------------

    async def step(self, task: Task, events: list[Event], ctx: StepContext) -> StepOutcome:
        out = await super().step(task, events, ctx)
        if isinstance(out, Done) and task.principal is not None:
            self._maybe_fold(task.principal, task.trace_id)
        return out

    def _maybe_fold(self, principal: str, trace_id: str) -> None:
        """Fold the principal's history in the background once it is due (D3): after the
        reply went out, never on the reply path."""
        if principal in self._folding:
            return  # one fold per principal at a time (the model has one slot)
        try:
            if not fold_due(self.conn, principal):
                return
        except sqlite3.Error:
            log.exception("brain: fold check failed")
            return
        spans = self._spans(trace_id)
        self._folding.add(principal)
        job = asyncio.get_running_loop().create_task(self._fold(principal, spans))
        self._folds.add(job)
        job.add_done_callback(self._folds.discard)

    async def _fold(self, principal: str, spans: SpanFactory) -> None:
        try:
            await fold(self.conn, self.model, principal, spans=spans, now=self.now)
        except Exception:
            log.exception("brain: history fold failed")
        finally:
            self._folding.discard(principal)

    async def idle(self) -> None:
        """Wait for background folds and the runner check (tests). Only unfinished jobs are
        awaited: a gather over finished tasks never yields, so a loop on them would stall
        the event loop (and the watchdog ends the process)."""
        while True:
            gaps = [self._gaps_job] if self._gaps_job is not None else []
            jobs = [j for j in (*self._folds, *gaps) if not j.done()]
            if not jobs:
                return
            await asyncio.gather(*jobs, return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel background folds and the runner check (shutdown, before the model client
        closes). The fold's cursor moves by compare-and-set, so a cancelled fold just runs
        again later."""
        self._closed = True
        jobs = [*self._folds, *([self._gaps_job] if self._gaps_job is not None else [])]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self._gaps_job = None

    def _spans(self, trace_id: str) -> SpanFactory:
        return partial(span, self.conn, trace_id=trace_id)

    def _pinned_origin(self, cp: dict[str, Any], pinned: PinnedCall) -> Origin:
        """A pinned call keeps the taint it was pinned with (D6), across an approval wait."""
        if is_explicit(cp, pinned):
            return "explicit"
        return "tainted" if cp.get("tainted") is True else "inferred"

    # --- answering -------------------------------------------------------------------------

    async def _answer(
        self,
        task: Task,
        principal: str,
        events: list[Event],
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult:
        cp = task.checkpoint
        if any(k in cp for k in ("call", "direct_call", "fast_reply", "fallback", "intent")):
            return await super()._answer(task, principal, events, ctx, spans)
        text = self._current_text(task, events)
        if text is None:
            return Answer(NO_ANSWER_REPLY, "fallback", [])
        stop = asyncio.Event()
        progress = asyncio.get_running_loop().create_task(self._progress(task, principal, stop))
        try:
            return await self._turn(task, principal, text, events, ctx, spans)
        finally:
            stop.set()
            try:
                await progress  # a send in flight finishes before the reply goes out
            except asyncio.CancelledError:
                progress.cancel()
                raise

    async def _progress(self, task: Task, principal: str, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), self.progress_after_s)
            return
        except TimeoutError:
            pass
        try:
            await self.channel.send(principal, PROGRESS_TEXT, dedupe_key=f"progress:{task.id}")
        except Exception as e:  # never fails the turn: the reply matters, not the ack
            log.warning("brain: progress text not sent (%s)", type(e).__name__)

    def _runner_missing(self) -> list[str]:
        """The workers' set-up gaps for the briefing: the last answer of ``runner_gaps``,
        refreshed in the background once it is ``RUNNER_GAPS_TTL_S`` old, never awaited on
        the reply path (the check asks the Keychain and may run ``claude``). No answer yet,
        or a check that failed or timed out: no gaps, so the briefing never claims workers
        are broken on a guess (the model refuses runs and asks because of it)."""
        if self.runner_gaps is None:
            return []
        now = time.monotonic()
        stale = self._gaps is None or now - self._gaps[0] >= RUNNER_GAPS_TTL_S
        if stale and self._gaps_job is None and not self._closed:
            job = asyncio.get_running_loop().create_task(self._refresh_gaps(self.runner_gaps))
            self._gaps_job = job
            job.add_done_callback(self._gaps_done)
        return [] if self._gaps is None else self._gaps[1]

    def _gaps_done(self, job: asyncio.Task[None]) -> None:
        if self._gaps_job is job:  # also when cancelled before it ran
            self._gaps_job = None

    async def _refresh_gaps(self, check: Callable[[], Awaitable[list[str] | None]]) -> None:
        """A failed or slow check keeps the last answer (only its age moves), so a transient
        Keychain error never turns known gaps into "all set"."""
        try:
            missing = await asyncio.wait_for(check(), RUNNER_GAPS_TIMEOUT_S)
        except Exception as e:
            log.warning("brain: runner set-up check failed (%s)", type(e).__name__)
            last = [] if self._gaps is None else self._gaps[1]
            self._gaps = (time.monotonic(), last)
            return
        self._gaps = (time.monotonic(), list(missing or []))

    def tools_for(self, principal: str) -> tuple[list[ToolSpec], list[ToolSpec]]:
        """``(core, deferred)``: the tools ``principal`` may request, unique by name, by name.
        Job specs and ``NOT_IN_BRAIN`` are never offered."""
        host = self.extensions
        specs = [
            s
            for s in self.registry.specs()
            if s.name not in NOT_IN_BRAIN
            and not (s.extension is not None and (host is None or host.is_job(s)))
            and self.principals.may_request(principal, s)
        ]
        unique = chat_toolset(specs, builtin_names(self.registry))
        core = [s for s in unique if s.extension is None and s.name in CORE_TOOLS]
        deferred = [s for s in unique if s not in core]
        return core, deferred

    async def _turn(
        self,
        task: Task,
        principal: str,
        text: str,
        events: list[Event],
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult:
        core, deferred = self.tools_for(principal)
        missing = self._runner_missing()
        with spans("runner.brain.context", core=len(core), catalog=len(deferred)) as s:
            context = build_context(
                self.conn,
                self.settings,
                principal=principal,
                principals=self.principals,
                text=text,
                event_id=task.origin_event_id,
                started_at=self.started_at,
                facts=self.facts,
                now=self.now(),
                last_tick=self.last_tick(),
                slots=self.slots,
                runner_missing=missing,
            )
            st = _Turn(context.messages(), events, core, {d.name: d for d in deferred}, text=text)
            s.set(history_turns=len(context.history), prefix_hash=context.prefix_hash(st.toolset()))
        try:
            return await self._rounds(task, principal, text, st, ctx, spans)
        except ModelUnavailableError:
            log.warning("model unavailable for brain task %s", task.id)
            if st.reports:
                return Answer(
                    clip_reply("\n".join(st.reports), MAX_REPLY_CHARS), "done", used_tools(st.calls)
                )
            return await self._intents(task, principal, text, events, ctx, spans)

    async def _rounds(
        self,
        task: Task,
        principal: str,
        text: str,
        st: _Turn,
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult:
        for n in range(MAX_ROUNDS):
            reply = await self._round(n, st, spans)
            calls = self._calls(reply, st, spans)
            if reply is None or not calls:
                answer = (reply.text or "").strip() if reply is not None else ""
                if answer:
                    nudge = self._nudge(answer, st)
                    if nudge is None:
                        return self._answered(answer, st, spans)
                    if nudge[0] == "claim":
                        st.nudged = True
                    else:
                        st.looked = True
                    with spans("runner.brain.nudge", kind=nudge[0]):
                        pass
                    st.messages.append({"role": "assistant", "content": answer})
                    st.messages.append({"role": "user", "content": nudge[1]})
                    continue
                st.errors += 1  # bad output, or neither a call nor an answer
                if st.errors >= MAX_MODEL_ERRORS:
                    return await self._stuck(task, principal, text, st, ctx, spans, cap=False)
                continue
            for name, args in calls:
                if len(st.calls) >= MAX_CALLS:
                    return await self._stuck(task, principal, text, st, ctx, spans, cap=True)
                ended = await self._one(task, principal, name, args, st, ctx, spans)
                if ended is not None:
                    return ended
                if len(st.reports) >= MAX_SIDE_EFFECTS:
                    return await self._narrate(st, spans)
        return await self._stuck(task, principal, text, st, ctx, spans, cap=True)

    def _calls(
        self, reply: ModelReply | None, st: _Turn, spans: SpanFactory
    ) -> list[tuple[str, Any]]:
        """The round's tool calls as ``(name, args)``: the native ones, or calls the model
        wrote as text (``{"tool_call": …}`` or ``<tool_call>``), which are never sent as the
        answer. A text call to a tool that doesn't exist, or with args that aren't an object,
        still goes to ``_one`` so the model gets an error back."""
        if reply is None:
            return []
        if reply.tool_calls:
            return [(c.name, c.args) for c in reply.tool_calls]
        raw = text_tool_calls(reply.text, {t.name for t in st.toolset()})
        if not raw:
            return []
        out: list[tuple[str, Any]] = []
        for r in raw:
            args: Any = r.arguments
            if args is None or args == "":
                args = {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = None
            out.append((r.name, args))
        with spans("runner.brain.text_calls", tools=[flat(n, 64) for n, _ in out]):
            pass
        return out

    def _nudge(self, answer: str, st: _Turn) -> tuple[str, str] | None:
        """The corrective rounds, each once per task: an answer that claims a change while no
        side effect ran (``claim``), and a live-state question answered with no tool at all
        (``lookup``, BR-9). ``(kind, text)`` or None."""
        # Once each: a second answer goes out as is, even if it still claims (the code-built
        # report lines are the owner's record of what ran).
        if st.reports:
            return None
        if not st.nudged and claims_change(answer):
            return ("claim", CLAIM_NUDGE)
        # A question back to the owner is fine as is.
        if (
            not st.looked
            and not st.calls
            and asks_live_state(st.text)
            and not _ASKS_BACK.search(answer)
        ):
            return ("lookup", LOOKUP_NUDGE)
        return None

    async def _round(self, n: int, st: _Turn, spans: SpanFactory) -> ModelReply | None:
        """One ``chat_tools`` call; None on bad output. ``ModelUnavailableError`` propagates."""
        with spans("runner.brain.round", round=n, tools=len(st.toolset())) as s:
            try:
                reply = await self.model.chat_tools(
                    st.messages, tools=st.toolset(), max_tokens=TOOLS_MAX_TOKENS, spans=spans
                )
            except ModelOutputError:
                s.set(outcome="bad_output")
                return None
            if reply.tool_calls:
                s.set(outcome="tools", calls=len(reply.tool_calls))
            else:
                s.set(outcome="answer" if (reply.text or "").strip() else "empty")
            return reply

    async def _one(
        self,
        task: Task,
        principal: str,
        name: str,
        args: Any,
        st: _Turn,
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult | None:
        """One model tool call into ``st``. Returns a result when it ends the turn (a wait,
        a refusal, an ask that queued), else None. A deferred tool is called through its stub:
        its args are checked against the real schema here, and a bad call gets the schema
        back instead of running."""
        spec = st.lookup(name)
        if not isinstance(args, dict):
            call = CallResult(name, False, None, "invalid arguments: pass a JSON object")
            args = {}
        elif spec is None:
            call = CallResult(name, False, None, NO_SUCH_TOOL.format(name=flat(name, 64)))
        elif (why := args_error(spec, cast(dict[str, Any], args))) is not None:
            schema = json.dumps(spec.args_schema, sort_keys=True, separators=(",", ":"))
            call = CallResult(
                name, False, None, BAD_STUB_ARGS.format(why=why, name=name, schema=schema)
            )
        elif not st.target_checked and (
            target := unnamed_target(spec, cast(dict[str, Any], args), st.text)
        ):
            st.target_checked = True
            with spans("runner.brain.target_check", tool=name):
                pass
            call = CallResult(name, False, None, TARGET_CHECK.format(target=target, name=name))
        elif not st.guess_checked and (
            target := guessed_arg(spec, cast(dict[str, Any], args), st.text, st.seen())
        ):
            st.guess_checked = True
            with spans("runner.brain.guess_check", tool=name):
                pass
            call = CallResult(name, False, None, GUESS_CHECK.format(target=target, name=name))
        else:
            args = cast(dict[str, Any], args)
            done = await self._call_tool(
                task,
                principal,
                [spec],
                name,
                args,
                st.executed,
                ctx,
                spans,
                st.reports,
                origin=st.origin,
                pin={"tainted": st.tainted},
            )
            st.executed += 1
            if taints(spec) and not st.tainted:
                st.tainted = True
                with spans("runner.brain.taint", tool=name, extension=spec.extension):
                    pass
            if not isinstance(done, CallResult):
                return after_effects(st.reports, done, MAX_REPLY_CHARS)
            call = done
            data = call.data or {}
            if name == ASK_CLAUDE.name and data.get("queued") is True:
                st.calls.append(call)
                said = data.get("text")
                reply = said if isinstance(said, str) and said else "Looking into it…"
                answer = Answer(reply, "ask", used_tools(st.calls), escalate=True)
                return after_effects(st.reports, answer, MAX_REPLY_CHARS)
        st.calls.append(call)
        # Native tool turns: the assistant's call, then its result as a ``tool`` message
        # (still labeled untrusted data), so the chat template renders them as it was trained.
        st.messages.append(
            {"role": "assistant", "content": "", "tool_calls": [{"name": name, "args": args}]}
        )
        st.messages.append(
            {
                "role": "tool",
                "tool_name": name,
                "content": tool_turn(call, self.model_result_max_chars),
            }
        )
        return None

    def _answered(self, answer: str, st: _Turn, spans: SpanFactory) -> Answer:
        """The model's answer: clipped, the number guard counted (log-only), and the code-built
        report lines of any side effects in it."""
        reply = clip_reply(answer, MAX_REPLY_CHARS)
        sources = [m["content"] for m in st.messages]
        bad = unsupported_numbers(reply, sources)
        meta: dict[str, Any] = {"tool_calls": len(st.calls), "tainted": st.tainted}
        if bad:
            meta["number_guard"] = len(bad)
            log.info("brain: %d number(s) in the reply not found in the context", len(bad))
        with spans("runner.brain.answer", reply_len=len(reply), number_guard=len(bad)):
            pass
        narration = Answer(reply, "llm", used_tools(st.calls), meta=meta)
        if not st.reports:
            return narration
        done = Answer("\n".join(st.reports), "done", used_tools(st.calls))
        return with_report(narration, done, MAX_REPLY_CHARS)

    async def _narrate(self, st: _Turn, spans: SpanFactory) -> Answer:
        """At the side-effect cap: one ``chat_json`` for the answer, with the report lines."""
        reports = "\n".join(st.reports)
        done = Answer(clip_reply(reports, MAX_REPLY_CHARS), "done", used_tools(st.calls))
        try:
            reply = await self.model.chat_json(
                [*st.messages, {"role": "user", "content": FINAL_INSTRUCTION}],
                schema=REPLY_SCHEMA,
                max_tokens=FINAL_MAX_TOKENS,
                spans=spans,
            )
        except ModelOutputError:
            return done
        except ModelUnavailableError:
            return done
        raw: Any = (reply.json or {}).get("reply")
        if not isinstance(raw, str) or not raw.strip():
            return done
        return self._answered(raw, st, spans)

    async def _stuck(
        self,
        task: Task,
        principal: str,
        text: str,
        st: _Turn,
        ctx: StepContext,
        spans: SpanFactory,
        *,
        cap: bool,
    ) -> StepResult:
        """Auto-escalation (D8): hand the message to ``ask_claude`` with a note of what was
        tried. When that isn't possible: the round cap says so, bad output takes the intents
        path (unless a side effect ran, then its report lines)."""
        why = "ran out of steps" if cap else "couldn't produce an answer"
        with spans("runner.brain.stuck", cap=cap, calls=len(st.calls)) as s:
            spec = self.registry.get(None, ASK_CLAUDE.name)
            queued: CallResult | None = None
            if spec is not None and self.principals.may_request(principal, spec):
                tried = ", ".join(dict.fromkeys(c.tool for c in st.calls)) or "nothing"
                ran = "; changes already made, don't repeat them" if st.reports else ""
                note = f"\n\n(Resonant's local model {why}; it tried: {tried}{ran})"
                note = note[:TRIED_MAX_CHARS]
                question = text[: QUESTION_MAX - len(note)] + note
                done = await self._call_tool(
                    task,
                    principal,
                    [spec],
                    spec.name,
                    {"question": question},
                    st.executed,
                    ctx,
                    spans,
                    st.reports,
                    origin=st.origin,
                    pin={"tainted": st.tainted},
                )
                st.executed += 1
                if (
                    isinstance(done, CallResult)
                    and done.ok
                    and (done.data or {}).get("queued") is True
                ):
                    queued = done
            s.set(escalated=queued is not None)
        if queued is not None:
            st.calls.append(queued)
            said = (queued.data or {}).get("text")
            reply = said if isinstance(said, str) and said else "Looking into it…"
            answer = Answer(reply, "escalate", used_tools(st.calls), escalate=True)
            return after_effects(st.reports, answer, MAX_REPLY_CHARS)
        if st.reports:
            reports = clip_reply("\n".join(st.reports), MAX_REPLY_CHARS)
            return Answer(reports, "done", used_tools(st.calls), escalate=True)
        if cap:
            return Answer(TOO_MANY_REPLY, "escalate", used_tools(st.calls), escalate=True)
        return await self._intents(task, principal, text, st.events, ctx, spans)

    async def _intents(
        self,
        task: Task,
        principal: str,
        text: str,
        events: list[Event],
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult:
        """D1's fallback: this turn takes the intents path (the labeler, then the local
        runner's answer step) as if the router had labeled it."""
        label = await self.labeler.label(text, spans=spans)
        toolset = toolset_for(
            label.intent,
            principal,
            registry=self.registry,
            principals=self.principals,
            extensions=self.extensions,
        )
        cp = {
            **task.checkpoint,
            "intent": label.intent,
            "confidence": label.confidence,
            "toolset": toolset_entries(toolset),
            "fallback": label.fallback,
            "brain_fallback": True,
        }
        with spans("runner.brain.fallback", intent=label.intent, fallback=label.fallback):
            pass
        result = await LocalRunner._answer(
            self, replace(task, checkpoint=cp), principal, events, ctx, spans
        )
        if isinstance(result, Answer):
            result.meta.setdefault("brain_fallback", True)
        return result


__all__ = [
    "CORE_TOOLS",
    "MAX_REPLY_CHARS",
    "MAX_ROUNDS",
    "NAME",
    "NOT_IN_BRAIN",
    "PROGRESS_TEXT",
    "BrainRunner",
    "asks_live_state",
    "claims_change",
    "guessed_arg",
    "slim_schema",
    "stub",
    "taints",
    "unnamed_target",
]

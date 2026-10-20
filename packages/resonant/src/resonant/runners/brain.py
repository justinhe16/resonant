"""BrainRunner (``runner="brain"``): the general agent loop on the local model (docs/brain-plan.md
D1, D4, D6, D7). With ``brain.mode: loop`` the router sends every non-fast-path chat message
here instead of to the labeler; the fast path, approval replies, the relay and ``/kill`` are
unchanged.

It is a ``LocalRunner`` with a different answer step, so everything after "the model picked a
tool" is the shared tool call path (TC-1, ``resonant.toolcore``, through ``_call_tool``):
registry spec, schema check, ``gate.evaluate``, executor, the pinned gated call and its resume
after an approval, the code-built report lines (``REPORTS``), at most ``MAX_SIDE_EFFECTS``
side effects without asking; plus the resend of a saved reply. Changes the gate asks about
join the turn's batch (BA-1) and the model goes on; when the turn ends the owner gets one
prompt for all of them (``LocalRunner.close_batch``). A checkpoint that holds ``batch`` or
``call`` (or any of the local runner's keys) is handed to ``LocalRunner._answer`` as is.

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
import contextlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
from typing import Any, cast

from resonant.brain import triage
from resonant.brain.context import build_context, catalog_entries
from resonant.brain.facts import FactStore
from resonant.brain.history import fold, fold_due
from resonant.brain.text import flat
from resonant.config import Settings
from resonant.gate.types import NeedsApproval, Origin
from resonant.gateway.channel import Channel
from resonant.loop.slots import SlotPool
from resonant.loop.types import Done, Fail, StepContext, StepOutcome, Task
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
    GATED_PATHS,
    MODEL_RESULT_MAX_CHARS,
    NO_ANSWER_REPLY,
    REPLY_SCHEMA,
    TOO_MANY_REPLY,
    Answer,
    CallResult,
    LocalRunner,
    StepResult,
    after_effects,
    clip_reply,
    tool_turn,
    unsupported_numbers,
    used_tools,
    with_report,
)
from resonant.store.db import utcnow
from resonant.toolcore import MAX_SIDE_EFFECTS, BatchItem, RunnerGate, ToolExecutor
from resonant.toolcore import args_error as args_error  # moved here (TC-1); re-exported
from resonant.tools import ToolRegistry
from resonant.tools.ask import ASK_CLAUDE, ASKING_HOPS, QUESTION_MAX
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
MAX_GUARDS = 1  # BR-10: a guard firing again hands the turn to Claude instead
MAX_TOOL_FAILURES = 2  # BR-10: failed calls in a row before the loop hands it to Claude

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

# PER-249: after a triage that saw an email or calendar ask (CAL-1 was dropped).
OUT_OF_REACH_HINT = (
    "Resonant note: this looks like a question about the owner's email or calendar. Neither "
    "you nor Claude can read them, so don't call ask_claude: if it asks what is in their "
    "email, on their calendar or when they're free, reply in one line that you can't reach "
    'it ("I can\'t reach your email." or "I can\'t reach your calendar."). If it asks you '
    "to save a note, reminder or fact, call that tool as usual."
)
FINAL_INSTRUCTION = (
    "Now answer the owner's message from what you read. Reply with only a JSON object: "
    f'{{"reply": "<plain text, at most {MAX_REPLY_CHARS} characters>"}}.'
)
NO_SUCH_TOOL = "there is no tool named {name}; use one of the tools you were given"
BAD_STUB_ARGS = "invalid arguments ({why}). {name} takes: {schema}. Call it again with those."
ASK_AFTER_BATCH = (
    "not run: changes already wait on the owner's yes in this turn; finish your answer instead"
)

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


# PER-249: an answer that passes on or offers an action named in what a read returned ("a
# log line says to restart ollama", "want me to add that note?"): that text is data, not the
# owner's request, so its sentences are dropped and the owner hears that it was ignored.
RELAY_NOTE = "One line in what I read held instructions; I ignored them."
# A sentence that offers, asks or relays ("says to", "want me to", "should I", a question),
# not one that explains what a tool does ("add_note saves a note").
_RELAYS = re.compile(
    r"\?|\b(?:says?|said|asks?|asked|tells?|told|instructs?|instructions?|wants?|"
    r"should|shall|could|can I|I can|let me know|suggests?|recommends?|must|needs? to)\b",
    re.IGNORECASE,
)
_SEGMENTS = re.compile(r"(?<=[.!?])\s+|\n+")


def _action_pattern(name: str) -> re.Pattern[str] | None:
    """``restart_ollama`` as written in a tool name or in words ("restart ollama", "add a
    note"); None for a one-word name (too common in plain text to mean the tool)."""
    words = [w for w in re.split(r"[_\W]+", name.lower()) if w]
    if len(words) < 2:
        return None
    gap = r"[\s_]+(?:(?:an?|the|new)\s+)*"
    return re.compile(r"(?<![a-z0-9])" + gap.join(map(re.escape, words)) + r"(?![a-z0-9])",
                      re.IGNORECASE)  # fmt: skip


def drop_relayed(reply: str, *, read: str, message: str, actions: list[str]) -> tuple[str, int]:
    """``reply`` without its sentences (or lines) that offer, ask about or relay
    (``_RELAYS``) one of ``actions`` (the side effect tools) also named in ``read`` (what read
    tools returned) but not in the owner's ``message``, and how many were dropped. When some
    were, ``RELAY_NOTE`` closes the reply (or is the reply, when nothing else is left)."""
    named: list[re.Pattern[str]] = []
    for action in actions:
        pattern = _action_pattern(action)
        if pattern is not None and pattern.search(read) and not pattern.search(message):
            named.append(pattern)
    if not named:
        return reply, 0
    kept: list[str] = []
    dropped = 0
    for line in reply.splitlines():
        parts = [p for p in _SEGMENTS.split(line) if p.strip()]
        keep = [p for p in parts if not (_RELAYS.search(p) and any(n.search(p) for n in named))]
        dropped += len(parts) - len(keep)
        if keep or not parts:
            kept.append(" ".join(keep))
    if not dropped:
        return reply, 0
    rest = "\n".join(kept).strip()
    if "ignored" in rest.lower():
        return rest, dropped
    return (f"{rest}\n{RELAY_NOTE}" if rest else RELAY_NOTE), dropped


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


@dataclass
class _Turn:
    """One brain turn's state: the prompt so far, the tools and what ran."""

    messages: list[ChatMessage]
    events: list[Event]  # the step's events, for the intents fallback
    core: list[ToolSpec]
    deferred: dict[str, ToolSpec]  # by model-facing name; offered as stubs
    calls: list[CallResult] = field(default_factory=list[CallResult])
    batch: list[BatchItem] = field(default_factory=list[BatchItem])  # BA-1: waits on the owner
    decisions: dict[str, NeedsApproval] = field(default_factory=dict[str, NeedsApproval])
    tainted: bool = False
    hops: int = 0  # BR-10b: replays from the outside text (0: this task read it itself)
    errors: int = 0
    executed: int = 0  # tool calls run through the gate (their call_index)
    nudged: bool = False  # the claim corrective round was used
    looked: bool = False  # the lookup corrective round was used (BR-9)
    text: str = ""  # the owner's message (the target check)
    target_checked: bool = False  # the one target check was used
    guess_checked: bool = False  # the one guessed-identifier check was used
    guards: int = 0  # BR-10: claim, lookup and guess checks fired (the next one escalates)
    failures: int = 0  # BR-10: failed tool calls since the last one that worked
    meta: dict[str, Any] = field(default_factory=dict[str, Any])  # route, handoff, ack
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

    def outside(self) -> None:
        """This task read outside content itself: tainted, zero hops from it."""
        self.tainted, self.hops = True, 0

    def pin(self) -> dict[str, Any]:
        """The taint a gated call is pinned with (its resume and the next turn read it)."""
        return {"tainted": True, "taint_hops": self.hops} if self.tainted else {"tainted": False}

    @property
    def reports(self) -> list[str]:
        return [c.report for c in self.calls if c.report is not None]


@contextlib.contextmanager
def _asking(st: _Turn) -> Generator[None]:
    """BR-11: this turn's taint hops for an ``ask_claude`` it calls (``tools.ask.ASKING_HOPS``)."""
    token = ASKING_HOPS.set(st.hops if st.tainted else None)
    try:
        yield
    finally:
        ASKING_HOPS.reset(token)


def _is_read(spec: ToolSpec | None) -> bool:
    return spec is not None and spec.effect is Effect.READ


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
            taint_hops=settings.brain.taint_hops,
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

    # A pinned call keeps the taint it was pinned with (D6), across an approval wait:
    # ``LocalRunner._pinned_origin`` reads ``checkpoint.tainted``.

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
        if any(
            k in cp for k in ("batch", "call", "direct_call", "fast_reply", "fallback", "intent")
        ):
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
        if context.untrusted is not None:  # BR-10b: the history replays outside text
            st.tainted, st.hops = True, context.taint_hops
            with spans("runner.brain.taint", tool="history", why=context.untrusted,
                       hops=st.hops):  # fmt: skip
                pass
        result = await self._route(task, principal, text, st, ctx, spans)
        gate = self.core.batch_gate
        if st.batch and isinstance(result, Fail) and not result.retryable and gate is not None:
            # the turn failed for good before asking: nothing can bind what it pinned
            gate.drop([i.approval_id for i in st.batch], reason="turn_ended")
        if st.batch and isinstance(result, Answer):
            # BA-1: changes wait on the owner: one prompt for all of them, then the task waits.
            # The model's own words give way to the code-built lines (what ran, why it
            # stopped); the reply after the owner's answer is code-built too.
            lines = [result.reply] if result.path in GATED_PATHS else st.reports
            with spans("runner.brain.batch", changes=len(st.batch), ran=len(st.reports)):
                pass
            return await self.close_batch(
                task,
                principal,
                st.batch,
                list(lines),
                ctx,
                spans,
                pin=st.pin(),
                decision=st.decisions.get(st.batch[0].approval_id),
            )
        if isinstance(result, Answer):  # the next turn's history reads it (BR-10b)
            result.meta["tainted"] = result.meta.get("tainted") is True or st.tainted
            if st.tainted:
                result.meta["taint_hops"] = st.hops
            if context.untrusted is not None:
                result.meta["history_taint"] = context.untrusted
        return result

    async def _route(
        self,
        task: Task,
        principal: str,
        text: str,
        st: _Turn,
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult:
        """Triage, then Claude or the local rounds (the intents path when the model is down)."""
        routed = await self._triage(task, principal, text, st, spans)
        if routed is not None:
            handed = await self._hand_off(
                task, principal, text, st, ctx, spans, why="triage", kind=routed.kind,
                ack=routed.ack,
            )  # fmt: skip
            if handed is not None:
                return handed
            st.meta["route"] = "local"  # Claude refused (cap, off, paused): answer here
        elif st.meta.get("triage", {}).get("why") == "out_of_reach":
            # PER-249: told only on these turns, so the rule doesn't sit in every prompt
            st.messages.append({"role": "user", "content": OUT_OF_REACH_HINT})
        try:
            return await self._rounds(task, principal, text, st, ctx, spans)
        except ModelUnavailableError:
            log.warning("model unavailable for brain task %s", task.id)
            if st.reports or st.batch:
                return Answer(
                    clip_reply("\n".join(st.reports), MAX_REPLY_CHARS), "done", used_tools(st.calls)
                )
            return await self._intents(task, principal, text, st, ctx, spans)

    # --- triage and hand-off (BR-10) -------------------------------------------------------

    def _claude_spec(self, principal: str) -> ToolSpec | None:
        """``ask_claude``'s spec when ``principal`` may use it and it is on, else None."""
        if not self.settings.brain.ask_claude.enabled:
            return None
        spec = self.registry.get(None, ASK_CLAUDE.name)
        if spec is None or not self.principals.may_request(principal, spec):
            return None
        return spec

    async def _triage(
        self, task: Task, principal: str, text: str, st: _Turn, spans: SpanFactory
    ) -> triage.Triage | None:
        """BR-10: who answers this turn. None: the local loop. A ``Triage`` with route
        ``claude``: hand it over, with its ack (None: a canned one). One ``chat_json`` call
        over the turn's own messages and toolset, so the prompt cache holds. A bad or late
        answer, or a model that's down, goes to Claude (in doubt, Claude). An email or calendar
        kind stays local whatever the route says (PER-249): Claude can't reach them either."""
        brain = self.settings.brain
        st.meta["route"] = "local"
        if brain.route == "local" or self._claude_spec(principal) is None:
            return None
        ask: ChatMessage = {"role": "user", "content": triage.instruction(brain.triage.bias)}
        messages: list[ChatMessage] = [*st.messages, ask]
        start = time.monotonic()
        decided: triage.Triage | None = None
        why = "model"
        with spans("runner.brain.triage", route_mode=brain.route) as s:
            try:
                reply = await asyncio.wait_for(
                    self.model.chat_json(
                        messages,
                        schema=triage.SCHEMA,
                        max_tokens=triage.TRIAGE_MAX_TOKENS,
                        spans=spans,
                        tools=st.toolset(),
                    ),
                    brain.triage.timeout_s,
                )
                decided = triage.parse(reply.json)
                if decided is None:
                    why = "bad_output"
            except ModelOutputError:
                why = "bad_output"
            except ModelUnavailableError:
                why = "model_down"
            except TimeoutError:
                why = "timeout"
            if decided is None:  # in doubt: Claude, with a canned ack
                decided = triage.Triage("claude", "general", None)
            if brain.route == "claude" and decided.route != "claude":
                decided = triage.Triage("claude", decided.kind, None)
            if (
                decided.kind in triage.OUT_OF_REACH
                and not triage.has_link(text)
                and not triage.looks_hard(text)  # "why does the email parser fail?"
            ):
                # PER-249: Claude can't reach email or calendar either: say so here.
                # Even with brain.route claude: Claude would only say the same. Photos
                # never get here (runners/media.py hands them to Claude directly).
                decided = triage.Triage("local", decided.kind, None)
                why = "out_of_reach"
            elif decided.route == "local" and triage.has_link(text):
                decided = triage.Triage("claude", "link", None)  # links need WebFetch
                why = "link"
            elif decided.route == "local" and triage.looks_hard(text):
                decided = triage.Triage("claude", decided.kind, None)  # why/how, debugging
                why = "hard_shape"
            ms = round((time.monotonic() - start) * 1000)
            s.set(route=decided.route, kind=decided.kind, why=why, ack=decided.ack is not None,
                  latency_ms=ms)  # fmt: skip
        st.meta["triage"] = {"route": decided.route, "why": why, "ms": ms}
        return decided if decided.route == "claude" else None

    async def _hand_off(
        self,
        task: Task,
        principal: str,
        text: str,
        st: _Turn,
        ctx: StepContext,
        spans: SpanFactory,
        *,
        why: str,
        kind: str = "general",
        ack: str | None = None,
        note: str = "",
    ) -> StepResult | None:
        """Hand the turn to Claude: ``ask_claude`` through the gate and executor (origin as
        the task's, so a tainted turn starts a tainted session), and the ack is the reply.
        None when it couldn't be queued (the principal may not, it is off, refused), or when
        changes already wait on the owner in this turn's batch (BA-1: the turn asks first)."""
        spec = self._claude_spec(principal)
        if spec is None or st.batch:
            return None
        question = text[: QUESTION_MAX - len(note)] + note
        args: dict[str, Any] = {"question": question}
        if kind != "general":
            args["kind"] = kind
        with spans("runner.brain.handoff", why=why, kind=kind) as s, _asking(st):
            done = await self._call_tool(
                task,
                principal,
                [spec],
                spec.name,
                args,
                st.executed,
                ctx,
                spans,
                st.reports,
                origin=st.origin,
                pin=st.pin(),
            )
            st.executed += 1
            queued = (
                isinstance(done, CallResult) and done.ok and (done.data or {}).get("queued") is True
            )
            s.set(queued=queued)
        data = (done.data or {}) if isinstance(done, CallResult) else {}
        if not queued:
            if data.get("busy") is True and isinstance(said := data.get("text"), str):
                # BR-11: the Claude brain's queue is full: say so (code-built) and stop
                assert isinstance(done, CallResult)
                st.calls.append(done)
                st.meta.update(route="claude", handoff=why, ack="busy")
                busy = Answer(said, "claude", used_tools(st.calls), escalate=True,
                              meta=dict(st.meta))  # fmt: skip
                return after_effects(st.reports, busy, MAX_REPLY_CHARS)
            return None
        assert isinstance(done, CallResult)
        st.calls.append(done)
        if data.get("session") is True:
            # BR-11: the Claude brain records its own turn's taint with the answer, so this
            # turn's reply (the ack) carries only what the turn itself had seen
            st.meta["claude_session"] = True
        else:
            st.outside()  # its answer is outside content (D6)
        reply = ack or triage.canned_ack(task.id)
        if isinstance(after := data.get("after"), str):  # BR-11: others are ahead of it
            reply = f"{reply} ({flat(after, 120)})"
        st.meta.update(route="claude", handoff=why, ack="model" if ack else "canned")
        answer = Answer(reply, "claude", used_tools(st.calls), escalate=True, meta=dict(st.meta))
        return after_effects(st.reports, answer, MAX_REPLY_CHARS)

    def _guard_fired(self, kind: str, st: _Turn, spans: SpanFactory) -> bool:
        """Count a guard (claim, lookup, guess). True when one already fired in this task: the
        turn then goes to Claude instead of trying again."""
        with spans("runner.brain.guard", kind=kind, again=st.guards >= MAX_GUARDS):
            pass
        st.guards += 1
        return st.guards > MAX_GUARDS

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
                    # gave up after failed calls (a question back to the owner is fine)
                    if st.failures >= MAX_TOOL_FAILURES and not _ASKS_BACK.search(answer):
                        handed = await self._hand_off(
                            task, principal, text, st, ctx, spans, why="tool_errors",
                            note=self._tried(st, "couldn't recover from tool errors"),
                        )  # fmt: skip
                        if handed is not None:
                            return handed
                    nudge = self._nudge(answer, st)
                    if nudge is None:
                        return self._answered(answer, st, spans)
                    if self._guard_fired(nudge[0], st, spans):
                        handed = await self._hand_off(
                            task, principal, text, st, ctx, spans, why="guard",
                            note=self._tried(st, "kept guessing"),
                        )  # fmt: skip
                        if handed is not None:
                            return handed
                    if (nudge[0] == "claim" and st.nudged) or (nudge[0] == "lookup" and st.looked):
                        return self._answered(answer, st, spans)  # once each, as before
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
                    if st.batch:  # the batch's reply is code-built: no narration
                        return Answer("\n".join(st.reports), "done", used_tools(st.calls))
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
        """The corrective rounds: an answer that claims a change while no side effect ran
        (``claim``), and a live-state question answered with no tool at all (``lookup``,
        BR-9). ``(kind, text)`` or None. Each round runs once per task; a guard that fires
        again hands the turn to Claude (BR-10, ``_rounds``), and only when that can't happen
        does the second answer go out as is (the code-built report lines are the owner's
        record of what ran). Not with a batch waiting (BA-1): the answer gives way to it."""
        if st.reports or st.batch:
            return None
        if claims_change(answer):
            return ("claim", CLAIM_NUDGE)
        # A question back to the owner is fine as is.
        if not st.calls and asks_live_state(st.text) and not _ASKS_BACK.search(answer):
            return ("lookup", LOOKUP_NUDGE)
        return None

    @staticmethod
    def _tried(st: _Turn, why: str) -> str:
        """The note an escalated question carries: why, and which tools ran."""
        tried = ", ".join(dict.fromkeys(c.tool for c in st.calls)) or "nothing"
        ran = "; changes already made, don't repeat them" if st.reports else ""
        return f"\n\n(Resonant's local model {why}; it tried: {tried}{ran})"[:TRIED_MAX_CHARS]

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
            st.failures += 1
        elif spec is None:
            call = CallResult(name, False, None, NO_SUCH_TOOL.format(name=flat(name, 64)))
            st.failures += 1
        elif (why := args_error(spec, cast(dict[str, Any], args))) is not None:
            schema = json.dumps(spec.args_schema, sort_keys=True, separators=(",", ":"))
            call = CallResult(
                name, False, None, BAD_STUB_ARGS.format(why=why, name=name, schema=schema)
            )
            st.failures += 1
        elif not st.target_checked and (
            target := unnamed_target(spec, cast(dict[str, Any], args), st.text)
        ):
            st.target_checked = True
            with spans("runner.brain.target_check", tool=name):
                pass
            call = CallResult(name, False, None, TARGET_CHECK.format(target=target, name=name))
        elif target := guessed_arg(spec, cast(dict[str, Any], args), st.text, st.seen()):
            if self._guard_fired("guess", st, spans):  # BR-10: a guard again: Claude
                handed = await self._hand_off(
                    task, principal, st.text, st, ctx, spans, why="guard",
                    note=self._tried(st, "kept guessing"),
                )  # fmt: skip
                if handed is not None:
                    return handed
            if st.guess_checked:  # once per task, as before: run it
                return await self._run_one(task, principal, name, args, spec, st, ctx, spans)
            st.guess_checked = True
            with spans("runner.brain.guess_check", tool=name):
                pass
            call = CallResult(name, False, None, GUESS_CHECK.format(target=target, name=name))
        else:
            return await self._run_one(task, principal, name, args, spec, st, ctx, spans)
        st.calls.append(call)
        self._tool_turns(name, args, call, st)
        return await self._still_failing(task, principal, st, ctx, spans)

    async def _still_failing(
        self, task: Task, principal: str, st: _Turn, ctx: StepContext, spans: SpanFactory
    ) -> StepResult | None:
        """BR-10: more than ``MAX_TOOL_FAILURES`` failed calls in a row hands the turn over."""
        if st.failures <= MAX_TOOL_FAILURES:
            return None
        return await self._hand_off(
            task, principal, st.text, st, ctx, spans, why="tool_errors",
            note=self._tried(st, "couldn't recover from tool errors"),
        )  # fmt: skip

    async def _run_one(
        self,
        task: Task,
        principal: str,
        name: str,
        args: Any,
        spec: ToolSpec,
        st: _Turn,
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult | None:
        """Run one checked call through the gate and executor into ``st``."""
        args = cast(dict[str, Any], args)
        if name == ASK_CLAUDE.name and st.batch:
            # BA-1: changes already wait on the owner: this turn asks first (a hand-off's ack
            # would give way to the batch prompt).
            call = CallResult(name, False, None, ASK_AFTER_BATCH)
            st.calls.append(call)
            self._tool_turns(name, args, call, st)
            return None
        with _asking(st):
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
                pin=st.pin(),
                check_schema=True,  # checked above (``_one``); ToolCore checks again
                batch=st.batch,
            )
        st.executed += 1
        session = (
            name == ASK_CLAUDE.name
            and isinstance(done, CallResult)
            and (done.data or {}).get("session") is True
        )  # BR-11: a Claude brain turn records its own taint with its answer
        asked = done.asked if isinstance(done, CallResult) else None
        if asked is not None:  # BA-1: it waits on the owner; nothing ran, nothing was read
            if all(i.approval_id != asked.approval_id for i in st.batch):
                st.batch.append(asked)
                if isinstance(done, CallResult) and done.decision is not None:
                    st.decisions[asked.approval_id] = done.decision
            with spans("runner.brain.asked", tool=name, batch=len(st.batch)):
                pass
        elif taints(spec) and not session:
            if not st.tainted or st.hops:
                with spans("runner.brain.taint", tool=name, extension=spec.extension):
                    pass
            st.outside()
        if not isinstance(done, CallResult):
            return after_effects(st.reports, done, MAX_REPLY_CHARS)
        call = done
        data = call.data or {}
        if name == ASK_CLAUDE.name and data.get("queued") is True:
            st.calls.append(call)
            said = data.get("text")
            reply = said if isinstance(said, str) and said else "Looking into it…"
            if isinstance(after := data.get("after"), str):  # BR-11: others are ahead
                reply = f"{reply} ({flat(after, 120)})"
            if session:
                st.meta["claude_session"] = True
            st.meta.update(route="claude", handoff="ask", ack="tool")
            meta = dict(st.meta)
            answer = Answer(reply, "ask", used_tools(st.calls), escalate=True, meta=meta)
            return after_effects(st.reports, answer, MAX_REPLY_CHARS)
        # BR-10: a tool that failed (not a gate refusal: Claude has the same gate) counts
        failed = not call.ok and (call.error or "").startswith("the tool failed")
        st.failures = 0 if call.ok else st.failures + int(failed)
        st.calls.append(call)
        self._tool_turns(name, args, call, st)
        return await self._still_failing(task, principal, st, ctx, spans)

    def _tool_turns(self, name: str, args: Any, call: CallResult, st: _Turn) -> None:
        """Native tool turns: the assistant's call, then its result as a ``tool`` message
        (still labeled untrusted data), so the chat template renders them as trained."""
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

    def _answered(self, answer: str, st: _Turn, spans: SpanFactory) -> Answer:
        """The model's answer: clipped, the number guard counted (log-only), and the code-built
        report lines of any side effects in it."""
        read = "\n".join(
            str(m.get("content") or "")
            for m in st.messages
            if m.get("role") == "tool" and _is_read(st.lookup(str(m.get("tool_name") or "")))
        )
        actions = [s.name for s in (*st.core, *st.deferred.values()) if s.effect is not Effect.READ]
        reply, relayed = drop_relayed(answer, read=read, message=st.text, actions=actions)
        reply = clip_reply(reply, MAX_REPLY_CHARS)
        sources = [m["content"] for m in st.messages]
        bad = unsupported_numbers(reply, sources)
        meta: dict[str, Any] = {"tool_calls": len(st.calls), "tainted": st.tainted, **st.meta}
        if relayed:
            meta["relay_guard"] = relayed
            log.info("brain: dropped %d sentence(s) relaying an action from read content", relayed)
        if bad:
            meta["number_guard"] = len(bad)
            log.info("brain: %d number(s) in the reply not found in the context", len(bad))
        with spans("runner.brain.answer", reply_len=len(reply), number_guard=len(bad),
                   relay_guard=relayed):  # fmt: skip
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
        """Auto-escalation (D8): hand the message to Claude with a note of what was tried
        (``_hand_off``, a canned ack). When that isn't possible: the round cap says so, bad
        output takes the intents path (unless a side effect ran, then its report lines)."""
        why = "ran out of steps" if cap else "couldn't produce an answer"
        with spans("runner.brain.stuck", cap=cap, calls=len(st.calls)) as s:
            handed = await self._hand_off(
                task, principal, text, st, ctx, spans, why="cap" if cap else "bad_output",
                note=self._tried(st, why),
            )  # fmt: skip
            s.set(escalated=handed is not None)
        if handed is not None:
            return handed
        if st.reports or st.batch:
            reports = clip_reply("\n".join(st.reports), MAX_REPLY_CHARS)
            return Answer(reports, "done", used_tools(st.calls), escalate=True)
        if cap:
            return Answer(TOO_MANY_REPLY, "escalate", used_tools(st.calls), escalate=True)
        return await self._intents(task, principal, text, st, ctx, spans)

    async def _intents(
        self,
        task: Task,
        principal: str,
        text: str,
        st: _Turn,
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult:
        """D1's fallback: this turn takes the intents path (the labeler, then the local
        runner's answer step) as if the router had labeled it. Its prompt doesn't hold the
        loop's tool results or Claude's answers; it is tainted when the replies it replays
        are (``LocalRunner._context``, BR-10b)."""
        events = st.events
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

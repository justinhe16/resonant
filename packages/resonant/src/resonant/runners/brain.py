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
   anything is listed, then made unique (``router.chat_toolset``). ``CORE_TOOLS`` are always
   loaded; every other tool (builtins and extension chat tools, never job specs or
   ``NOT_IN_BRAIN``) is in the deferred catalog, one line each. ``load_tools{names}`` is a
   loop-internal pseudo-tool (never gated, never executed): it adds up to ``MAX_LOADED``
   catalog tools for the rest of the task, sent in name order like every toolset. It is
   offered only when the catalog isn't empty.
2. **Context** (``brain.build_context``, D2): system, core tools, catalog and facts, history,
   then the briefing, the "since we last talked" notes, the time and the message.
3. **Rounds.** At most ``MAX_ROUNDS`` ``chat_tools`` calls. A reply with no tool calls is the
   answer (clipped to ``MAX_REPLY_CHARS``). Each call goes through ``_call_tool`` with the
   task's origin: ``inferred`` until a tool result came from outside content, ``tainted``
   from then on (D6). Outside content is a spec with ``untrusted=True`` (the read tools,
   ``ask_claude``) or any extension or MCP tool. A gated call is pinned with ``tainted``, so
   its resume after an approval is gated as ``tainted`` too (``_pinned_origin``). Reads are
   free inside the rounds (``MAX_CALLS`` is only a backstop).
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
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
from typing import Any, cast

from resonant.brain.context import build_context, catalog_entries
from resonant.brain.facts import FactStore
from resonant.brain.history import fold, fold_due
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
from resonant_sdk import Effect, Event, Level, ToolSpec

log = logging.getLogger(__name__)

NAME = "brain"
MAX_ROUNDS = 8  # chat_tools calls per task (D7)
MAX_CALLS = 24  # tool calls per task, any kind: a backstop, reads are free inside the rounds
MAX_LOADED = 8  # catalog tools load_tools may add per task (D4)
MAX_REPLY_CHARS = 1500  # D7
MAX_MODEL_ERRORS = 2  # bad outputs before the loop gives up (escalates)
TOOLS_MAX_TOKENS = 1024  # a reply may be the answer, up to MAX_REPLY_CHARS
FINAL_MAX_TOKENS = 700
PROGRESS_AFTER_S = 6.0
PROGRESS_TEXT = "On it…"
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

LOAD_TOOLS = ToolSpec(
    name="load_tools",
    description=(
        f"Load tools listed under More tools so you can call them (up to {MAX_LOADED} per "
        "conversation turn). Pass their exact names."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string", "maxLength": 64},
                "minItems": 1,
                "maxItems": MAX_LOADED,
            }
        },
        "required": ["names"],
        "additionalProperties": False,
    },
    effect=Effect.READ,
    level=Level.L0,
)

FINAL_INSTRUCTION = (
    "Now answer the owner's message from what you read. Reply with only a JSON object: "
    f'{{"reply": "<plain text, at most {MAX_REPLY_CHARS} characters>"}}.'
)
NOT_LOADED = "not loaded: call load_tools with its name first"


@dataclass
class _Turn:
    """One brain turn's state: the prompt so far, the tools and what ran."""

    messages: list[ChatMessage]
    events: list[Event]  # the step's events, for the intents fallback
    core: list[ToolSpec]
    deferred: dict[str, ToolSpec]  # by model-facing name
    loaded: list[ToolSpec] = field(default_factory=list[ToolSpec])
    calls: list[CallResult] = field(default_factory=list[CallResult])
    tainted: bool = False
    errors: int = 0
    executed: int = 0  # tool calls run through the gate (their call_index)

    def toolset(self) -> list[ToolSpec]:
        tools = [*self.core, *self.loaded]
        if self.deferred:
            tools.append(LOAD_TOOLS)
        return sorted(tools, key=lambda s: s.name)

    def lookup(self, name: str) -> ToolSpec | None:
        return next((s for s in (*self.core, *self.loaded) if s.name == name), None)

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
        """Wait for background folds (tests)."""
        while self._folds:
            await asyncio.gather(*list(self._folds), return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel background folds (shutdown, before the model client closes). The fold's
        cursor moves by compare-and-set, so a cancelled fold just runs again later."""
        for job in list(self._folds):
            job.cancel()
        await asyncio.gather(*list(self._folds), return_exceptions=True)

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

    def tools_for(self, principal: str) -> tuple[list[ToolSpec], list[ToolSpec]]:
        """``(core, deferred)``: the tools ``principal`` may request, unique by name, by name.
        Job specs and ``NOT_IN_BRAIN`` are never offered."""
        host = self.extensions
        specs = [
            s
            for s in self.registry.specs()
            if s.name not in NOT_IN_BRAIN
            and s.name != LOAD_TOOLS.name
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
        with spans("runner.brain.context", core=len(core), catalog=len(deferred)) as s:
            context = build_context(
                self.conn,
                self.settings,
                principal=principal,
                principals=self.principals,
                text=text,
                event_id=task.origin_event_id,
                started_at=self.started_at,
                catalog=catalog_entries(deferred),
                facts=self.facts,
                now=self.now(),
                last_tick=self.last_tick(),
                slots=self.slots,
            )
            st = _Turn(context.messages(), events, core, {d.name: d for d in deferred})
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
            if reply is None or not reply.tool_calls:
                answer = (reply.text or "").strip() if reply is not None else ""
                if answer:
                    return self._answered(answer, st, spans)
                st.errors += 1  # bad output, or neither a call nor an answer
                if st.errors >= MAX_MODEL_ERRORS:
                    return await self._stuck(task, principal, text, st, ctx, spans, cap=False)
                continue
            for tc in reply.tool_calls:
                if len(st.calls) >= MAX_CALLS:
                    return await self._stuck(task, principal, text, st, ctx, spans, cap=True)
                ended = await self._one(task, principal, tc.name, tc.args, st, ctx, spans)
                if ended is not None:
                    return ended
                if len(st.reports) >= MAX_SIDE_EFFECTS:
                    return await self._narrate(st, spans)
        return await self._stuck(task, principal, text, st, ctx, spans, cap=True)

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
        args: dict[str, Any],
        st: _Turn,
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult | None:
        """One model tool call into ``st``. Returns a result when it ends the turn (a wait,
        a refusal, an ask that queued), else None."""
        if name == LOAD_TOOLS.name and st.deferred:
            call = self._load(args, st, spans)
        else:
            spec = st.lookup(name)
            if spec is None:
                why = NOT_LOADED if name in st.deferred else "tool not available"
                call = CallResult(name, False, None, why)
            else:
                origin = st.origin
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
                    origin=origin,
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
        st.messages.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {"tool_call": {"name": name, "args": args}},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
        st.messages.append(
            {"role": "user", "content": tool_turn(call, self.model_result_max_chars)}
        )
        return None

    def _load(self, args: dict[str, Any], st: _Turn, spans: SpanFactory) -> CallResult:
        """``load_tools``: never gated, never executed; it only widens this turn's toolset."""
        raw: Any = args.get("names")
        names = (
            [n for n in cast(list[Any], raw) if isinstance(n, str)] if isinstance(raw, list) else []
        )
        loaded: list[str] = []
        unknown: list[str] = []
        full: list[str] = []
        for n in dict.fromkeys(names):
            spec = st.deferred.get(n)
            if spec is None:
                if st.lookup(n) is None:
                    unknown.append(n)
                continue
            if spec in st.loaded:
                continue
            if len(st.loaded) >= MAX_LOADED:
                full.append(n)
                continue
            st.loaded.append(spec)
            loaded.append(n)
        with spans("runner.brain.load", loaded=loaded, unknown=len(unknown), full=len(full)):
            pass
        data: dict[str, Any] = {"loaded": loaded}
        if unknown:
            data["unknown"] = unknown
        if full:
            data["not_loaded"] = full
            data["note"] = f"at most {MAX_LOADED} tools can be loaded"
        return CallResult(LOAD_TOOLS.name, True, data, None)

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
    "LOAD_TOOLS",
    "MAX_LOADED",
    "MAX_REPLY_CHARS",
    "MAX_ROUNDS",
    "NAME",
    "NOT_IN_BRAIN",
    "PROGRESS_TEXT",
    "BrainRunner",
    "taints",
]

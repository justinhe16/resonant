"""BrainRunner (``runner="brain"``): the router (docs/brain-plan.md D1, D4, D6; PER-256).

The local model only routes. Claude is the one brain: every substantive reply comes from the
Claude session (``runners/ask.py``, ``runners/claude_session.py``), which holds the shared
transcript and the told state. This runner is the front door of a chat task (a
``kind="chat"`` task with no ``call``); the fast path, approval replies, the relay and
``/kill`` come before it, in the router. It is a ``LocalRunner`` with a different answer
step, so a pinned call or batch (a resume after an approval) is the local runner's, through
the shared tool call path (``resonant.toolcore``, ``_call_tool``).

**One turn**:

1. **Triage** (``brain/triage.py``). One ``chat_json`` call over a small prompt (a fixed
   system line and the message; no tools, no history). It types the message; it never
   answers it. A bad or late answer, or a model that is down, is Claude's (in doubt,
   Claude).
2. **Decide** (``triage.decide``, pure, in code):
   - *small talk*: a greeting or an ack, short and plain. The reply is the model's one line
     when ``triage.clean_small_talk`` accepts it (no digit, question, claim or state word),
     else a canned one (``Hey!``, a thumbs up).
   - *news*: "anything new?" with no topic. Built from code over current state
     (``brain/news.py``: what changed since the owner's last message and they weren't texted
     already, what waits on them). If that can't be read, Claude.
   - *Claude*: everything else, questions, requests, changes, notes, reminders, facts,
     email and calendar asks (Claude says it can't reach them). ``brain.route: claude``
     sends small talk and news too.
3. **Hand-off** (``_hand_off``). ``ask_claude`` through the gate and executor (origin as the
   task's, so a tainted turn starts a tainted session). The turn's own reply is silent
   (``meta.silent``): Claude's answer arrives as its own message from the ask task, one
   voice, no prefix. At most one short ack goes out, only when the answer takes longer than
   ``claude_session.ack_after_s`` (the ask runner sends it: ``ASKING_ACK``); queued behind
   others it goes at once with what is ahead.
4. **Claude can't take it** (``AskRefused.reason``, code, never parsed from text): the
   usage limit says so once per reset (``LIMIT_OWNER``: "I'm at my Claude limit until 3pm;
   I can still do approvals and /status") and then ``LIMIT_SHORT``; a full queue says so;
   anything else (off, paused, not running, caps) is ``UNAVAILABLE_REPLY``. A member who may
   not use Claude gets ``MEMBER_REPLY`` (small talk still works). Never a model-written
   answer in its place.
5. **After the reply** (``step``): when ``history.fold_due``, the history fold runs as a
   background task, never on the reply path.

Taint (D6, BR-10b): the history replays outside text (a Claude answer, a tainted task's
reply) -> the turn starts tainted and the ask it hands over does too. Message bodies are
PII: spans and logs carry lengths, names and counts, never text.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Generator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

from resonant.brain import triage
from resonant.brain.context import build_context
from resonant.brain.facts import FactStore
from resonant.brain.history import fold, fold_due
from resonant.brain.news import news_reply
from resonant.config import Settings
from resonant.gate.types import Origin
from resonant.gateway.channel import Channel
from resonant.loop.slots import SlotPool
from resonant.loop.types import Done, StepContext, StepOutcome, Task
from resonant.models import (
    ChatMessage,
    ModelClient,
    ModelOutputError,
    ModelUnavailableError,
    SpanFactory,
)
from resonant.observability.spans import span
from resonant.principals import Principals
from resonant.router.intents import ExtensionCatalog
from resonant.router.prefixes import wrap_untrusted
from resonant.router.router import builtin_names, chat_toolset
from resonant.runners.ask import (
    PAUSED,
    UNAVAILABLE_REPLY,
    limit_notice,
)
from resonant.runners.local import (
    MODEL_RESULT_MAX_CHARS,
    NO_ANSWER_REPLY,
    Answer,
    CallResult,
    LocalRunner,
    StepResult,
    after_effects,
    used_tools,
)
from resonant.store import pulse as pulse_store
from resonant.store.db import utcnow
from resonant.store.tasks import get_event
from resonant.toolcore import RunnerGate, ToolExecutor
from resonant.tools import ToolRegistry
from resonant.tools.ask import ASK_CLAUDE, ASKING_ACK, ASKING_HOPS, QUESTION_MAX
from resonant_sdk import ToolSpec

log = logging.getLogger(__name__)

NAME = "brain"
MAX_REPLY_CHARS = 1500  # D7
TRIAGE_MESSAGE_CHARS = 2000  # of the message, as the triage call reads it
RUNNER_GAPS_TTL_S = 60.0  # as the dashboard's runner check (api.RUNNER_CHECK_TTL_S)
RUNNER_GAPS_TIMEOUT_S = 30.0  # a background check; never on the reply path

# What a member hears for a text that needs Claude (members may not use ask_claude).
MEMBER_REPLY = "I can only take that from the owner right now."

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
# Control-plane tools that stay slash-only (``/ext mute``).
# ``shell`` and ``shell_read`` (PER-258): the Claude brain's own tool calls, gated by the bridge.
NOT_IN_BRAIN: frozenset[str] = frozenset({"mute_extension", "shell", "shell_read"})


_OUTCOME = re.compile(
    r"\b(?:fail\w*|merged|ready|done|finished|ended|stopped|cancel\w*|no changes|made no|"
    r"complete\w*|crash\w*|timed out)\b",
    re.IGNORECASE,
)


def _reports(sentence: str, code: str) -> bool:
    """The sentence names the run ``code`` (``r8``) and says how it went."""
    return re.search(rf"\b{re.escape(code)}\b", sentence, re.IGNORECASE) is not None and (
        _OUTCOME.search(sentence) is not None
    )


def asked_back(history: Sequence[ChatMessage]) -> bool:
    """Resonant's last message in the replayed thread asks the owner something (a "?"), so
    a short reply to it ("sure", "yes") answers that question."""
    said = next((m for m in reversed(history) if m.get("role") == "assistant"), None)
    return said is not None and "?" in str(said.get("content") or "")


@dataclass
class _Turn:
    """One brain turn's state: what ran and the taint."""

    text: str  # the owner's message
    principal: str
    calls: list[CallResult] = field(default_factory=list[CallResult])
    tainted: bool = False
    hops: int = 0  # BR-10b: replays from the outside text (0: this task read it itself)
    executed: int = 0  # tool calls run through the gate (their call_index)
    meta: dict[str, Any] = field(default_factory=dict[str, Any])  # route, handoff, triage
    asked_back: bool = False  # Resonant's last message in the thread asked the owner something

    @property
    def origin(self) -> Origin:
        return "tainted" if self.tainted else "inferred"

    def pin(self) -> dict[str, Any]:
        """The taint a gated call is pinned with (its resume and the next turn read it)."""
        return {"tainted": True, "taint_hops": self.hops} if self.tainted else {"tainted": False}

    def outside(self) -> None:
        """This task read outside content itself: tainted, zero hops from it."""
        self.tainted, self.hops = True, 0

    @property
    def reports(self) -> list[str]:
        return [c.report for c in self.calls if c.report is not None]


@contextlib.contextmanager
def _asking(st: _Turn, ack: str | None) -> Generator[None]:
    """BR-11: this turn's taint hops (``tools.ask.ASKING_HOPS``) and the ack to send if the
    answer is slow (``ASKING_ACK``, PER-256) for the ``ask_claude`` call it makes."""
    hops = ASKING_HOPS.set(st.hops if st.tainted else None)
    pending = ASKING_ACK.set(ack)
    try:
        yield
    finally:
        ASKING_ACK.reset(pending)
        ASKING_HOPS.reset(hops)


def taints(spec: ToolSpec) -> bool:
    """D6: a result from outside content (``untrusted`` specs, extension and MCP tools, and
    ``TAINTING_BUILTINS``). Resonant's own state reads don't taint."""
    return spec.untrusted or spec.extension is not None or spec.name in TAINTING_BUILTINS


class BrainRunner(LocalRunner):
    """``runner="brain"``. The local runner's collaborators, plus what the turn needs."""

    name = NAME
    tool_error_detail = True  # a failed read's reason goes back to Claude's tool bridge

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
        # The Claude runner's set-up gaps for the context's briefing (the daemon passes the
        # dashboard's cached check, ``api.runner_missing``); None, or a check that returns
        # None, fails or is slow: the briefing says nothing about workers.
        self.runner_gaps = runner_gaps
        self._gaps: tuple[float, list[str]] | None = None
        self._gaps_job: asyncio.Task[None] | None = None
        self._closed = False
        self._folds: set[asyncio.Task[None]] = set()
        self._folding: set[str] = set()  # principals with a fold in flight

    # --- step: the local runner's, then the fold ------------------------------------------

    async def step(self, task: Task, events: list[Any], ctx: StepContext) -> StepOutcome:
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
        events: list[Any],
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult:
        cp = task.checkpoint
        # A pinned call or batch (a resume after an approval) is the local runner's. A chat
        # checkpoint from before PER-256 (``intent``, ``fallback``) gets its no-answer reply.
        if any(
            k in cp for k in ("batch", "call", "direct_call", "fast_reply", "fallback", "intent")
        ):
            return await super()._answer(task, principal, events, ctx, spans)
        text = self._current_text(task, events)
        if text is None:
            return Answer(NO_ANSWER_REPLY, "fallback", [])
        return await self._turn(task, principal, text, ctx, spans)

    def _runner_missing(self) -> list[str]:
        """The workers' set-up gaps for the context's briefing: the last answer of
        ``runner_gaps``, refreshed in the background once it is ``RUNNER_GAPS_TTL_S`` old,
        never awaited on the reply path (the check asks the Keychain and may run
        ``claude``). No answer yet, or a check that failed or timed out: no gaps."""
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

    def tools_for(self, principal: str) -> list[ToolSpec]:
        """The tools ``principal`` may request, unique by name, sorted: what Claude's bridge
        offers (never job specs, ``mute_extension`` or the shell's own specs)."""
        host = self.extensions
        specs = [
            s
            for s in self.registry.specs()
            if s.name not in NOT_IN_BRAIN
            and not (s.extension is not None and (host is None or host.is_job(s)))
            and self.principals.may_request(principal, s)
        ]
        return chat_toolset(specs, builtin_names(self.registry))

    async def _turn(
        self,
        task: Task,
        principal: str,
        text: str,
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult:
        self._ack_told(task, principal)
        with spans("runner.brain.context") as s:
            # The context is only read for its taint (BR-10b): the history replays outside
            # text. The local model reads none of it any more.
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
                runner_missing=self._runner_missing(),
            )
            s.set(history_turns=len(context.history))
        st = _Turn(text, principal, asked_back=asked_back(context.history))
        if context.untrusted is not None:  # BR-10b: the history replays outside text
            st.tainted, st.hops = True, context.taint_hops
            with spans("runner.brain.taint", tool="history", why=context.untrusted,
                       hops=st.hops):  # fmt: skip
                pass
        result = await self._route(task, principal, text, st, ctx, spans)
        if isinstance(result, Answer):  # the next turn's history reads it (BR-10b)
            result.meta["tainted"] = result.meta.get("tainted") is True or st.tainted
            if st.tainted:
                result.meta["taint_hops"] = st.hops
            if context.untrusted is not None:
                result.meta["history_taint"] = context.untrusted
        return result

    def _ack_told(self, task: Task, principal: str) -> None:
        """PER-270: the owner wrote, so what they were told before this message is
        acknowledged (``pulse.mark_acked``: any later message counts). Best effort."""
        if principal != self.principals.owner or task.origin_event_id is None:
            return
        with contextlib.suppress(sqlite3.Error):
            event = get_event(self.conn, task.origin_event_id)
            if event is not None:
                pulse_store.mark_acked(self.conn, event.occurred_at, self.now())

    async def _send(
        self,
        task: Task,
        principal: str,
        answer: Answer,
        start: float,
        spans: SpanFactory,
        *,
        resend: bool = False,
    ) -> StepOutcome:
        if answer.meta.get("silent") is True:
            # PER-256: a hand-off says nothing itself. Claude's answer is its own message
            # (the ask task), with at most one ack when it is slow.
            return Done(
                {
                    "reply_len": 0,
                    "tools_used": answer.tools_used,
                    "latency_ms": round((self.clock() - start) * 1000),
                    "path": answer.path,
                    "escalate": answer.escalate,
                    **answer.meta,
                }
            )
        outcome = await super()._send(task, principal, answer, start, spans, resend=resend)
        if isinstance(outcome, Done) and principal == self.principals.owner:
            self._mark_told_by(answer)
        return outcome

    def _mark_told_by(self, answer: Answer) -> None:
        """PER-270: a code-built news reply that says how an ended run the owner hadn't been
        told about ended (its code and an outcome word in one line) tells them, so the pulse
        and the briefing skip it. Best effort."""
        if answer.meta.get("news") is not True:
            return
        sentences = [x for x in re.split(r"[.!?\n]+", answer.reply) if x.strip()]
        try:
            now = self.now()
            urgencies = pulse_store.URGENCIES
            named = [
                i
                for i in pulse_store.pending(self.conn, urgencies, now)
                if i.source == "core" and i.cleared and i.key.startswith("run/")
                and any(_reports(x, i.key.removeprefix("run/")) for x in sentences)
            ]  # fmt: skip
            if named:
                pulse_store.mark_told(self.conn, named, now)
        except sqlite3.Error:
            log.warning("brain: told state not recorded", exc_info=True)

    # --- triage, decide, hand-off ------------------------------------------------------------

    async def _route(
        self,
        task: Task,
        principal: str,
        text: str,
        st: _Turn,
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult:
        """Triage, then decide (``triage.decide``): small talk and news are answered here,
        from code; everything else is Claude's."""
        brain = self.settings.brain
        routed = await self._triage(text, st, spans)
        decision = triage.decide(text, routed, brain.route, asked_back=st.asked_back)
        owner = principal == self.principals.owner
        st.meta["triage"] = {**st.meta.get("triage", {}), "decision": decision}
        if decision == "small_talk" and routed is not None:
            reply = triage.clean_small_talk(routed.say, routed.message_type)
            with spans("runner.brain.small_talk", message_type=routed.message_type):
                pass
            return Answer(reply, "small_talk", [], meta={**st.meta, "route": "local"})
        if decision == "news" and owner:
            news = self._news(task, principal)
            if news is not None:
                with spans("runner.brain.news", lines=news.count("\n") + 1):
                    pass
                return Answer(news, "news", [], meta={**st.meta, "route": "local",
                                                      "system": True, "news": True})  # fmt: skip
        kind = routed.kind if routed is not None else "general"
        if triage.has_link(text):
            kind = "link"  # Claude fetches it (WebFetch), whatever the model said
        ack = routed.ack if routed is not None else None
        return await self._hand_off(
            task, principal, text, st, ctx, spans, why="triage", kind=kind, ack=ack
        )

    def _news(self, task: Task, principal: str) -> str | None:
        return news_reply(
            self.conn, self.settings, principal, event_id=task.origin_event_id, now=self.now()
        )

    async def _triage(self, text: str, st: _Turn, spans: SpanFactory) -> triage.Triage | None:
        """PER-256: the local model's one call, over a small prompt (no tools, no history).
        None for a bad or late answer, or a model that's down: the turn is Claude's."""
        brain = self.settings.brain
        messages: list[Any] = [
            {"role": "system", "content": triage.SYSTEM},
            {
                "role": "user",
                "content": wrap_untrusted(text, max_chars=TRIAGE_MESSAGE_CHARS)
                + "\n\n"
                + triage.instruction(),
            },
        ]
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
            ms = round((time.monotonic() - start) * 1000)
            s.set(
                handler=decided.route if decided else None,
                kind=decided.kind if decided else None,
                message_type=decided.message_type if decided else None,
                why=why,
                latency_ms=ms,
            )
        st.meta["triage"] = {
            "why": why,
            "ms": ms,
            "message_type": decided.message_type if decided else None,
        }
        return decided

    def _claude_spec(self, principal: str) -> ToolSpec | None:
        """``ask_claude``'s spec when ``principal`` may use it and it is on, else None."""
        if not self.settings.brain.ask_claude.enabled:
            return None
        spec = self.registry.get(None, ASK_CLAUDE.name)
        if spec is None or not self.principals.may_request(principal, spec):
            return None
        return spec

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
    ) -> StepResult:
        """Hand the turn to Claude: ``ask_claude`` through the gate and executor (origin as
        the task's, so a tainted turn starts a tainted session). The turn itself says nothing
        (``silent``); the ask task sends Claude's answer, and ``ack`` (else a canned one) only
        if that takes a while. When Claude can't take it the reply is code-built
        (``_cannot``)."""
        spec = self._claude_spec(principal)
        if spec is None:
            return self._cannot(
                st, why, "off" if not self.settings.brain.ask_claude.enabled else "member"
            )
        question = text[:QUESTION_MAX]
        args: dict[str, Any] = {"question": question}
        if kind != "general":
            args["kind"] = kind
        ack_text = ack or triage.canned_ack(task.id)
        with spans("runner.brain.handoff", why=why, kind=kind) as s, _asking(st, ack_text):
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
        if not isinstance(done, CallResult):  # a wait or a refusal of the gate: its own reply
            return after_effects(st.reports, done, MAX_REPLY_CHARS)
        data = done.data or {}
        if not queued:
            reason = data.get("reason")
            return self._cannot(
                st, why, reason if isinstance(reason, str) else "unavailable", data=data
            )
        st.calls.append(done)
        if data.get("session") is True:
            # BR-11: the Claude brain records its own turn's taint with the answer, so this
            # turn's record carries only what the turn itself had seen
            st.meta["claude_session"] = True
        else:
            st.outside()  # its answer is outside content (D6)
        st.meta.update(route="claude", handoff=why, ack="model" if ack else "canned",
                       silent=True)  # fmt: skip
        return Answer("", "claude", used_tools(st.calls), escalate=True, meta=dict(st.meta))

    def _cannot(
        self,
        st: _Turn,
        why: str,
        reason: str,
        data: dict[str, Any] | None = None,
    ) -> Answer:
        """Claude can't take the turn: a canned reply from the reason (code, never parsed
        text), and nothing in place of Claude's answer. The usage limit says so once per
        reset, then briefly; approvals and /status still work."""
        said = UNAVAILABLE_REPLY
        if reason == "member":
            said = MEMBER_REPLY
        elif reason == "paused":
            said = PAUSED
        elif reason == "busy" and data is not None and isinstance(text := data.get("text"), str):
            said = text  # BR-11: the Claude brain's queue is full (code-built)
        elif reason == "limit":
            said = self._limit_reply(data)
        st.meta.update(route="claude", handoff=why, refused=reason)
        return Answer(said, "refused", used_tools(st.calls), escalate=True, meta=dict(st.meta))

    def _limit_reply(self, data: dict[str, Any] | None) -> str:
        """The usage limit's reply: ``LIMIT_OWNER`` the first time for this reset, then
        ``LIMIT_SHORT`` (kv ``brain.limit_told``)."""
        raw = (data or {}).get("until")
        try:
            until = datetime.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            until = None
        if until is None or until.tzinfo is None:
            return UNAVAILABLE_REPLY
        try:
            text, _first = limit_notice(self.conn, until, ZoneInfo(self.settings.timezone),
                                        self.now())  # fmt: skip
        except sqlite3.Error:
            return UNAVAILABLE_REPLY
        return text


__all__ = [
    "MAX_REPLY_CHARS",
    "MEMBER_REPLY",
    "NAME",
    "NOT_IN_BRAIN",
    "BrainRunner",
    "taints",
]

"""The daemon side of the worker CLI (docs/employee-plan.md D3, EM-3): questions, notes,
checks and owner messages for running Claude workers.

``WorkerDesk.handle`` answers one CLI request from a run's socket (``worker_cli``):

- ``ask``: records the question, emits a ``question`` event, texts the requester
  ``R4 asks: <question>`` (the run replies' path, until EM-5 routes replies) and waits up to
  ``block_s`` (8 minutes; the session's Bash tool allows 10) for ``answer_question``. Answered:
  the answer. Not yet: ``NO_ANSWER_YET`` (exit 3) and the question is deferred, so the run
  waits for it (``waiting``, reason ``question``) once the session's turn ends, and resumes
  the session with the answer (or, after ``runner.ask_timeout_minutes``, with
  ``ASK_TIMEOUT``). The same question asked again while it is open (a retry after a Bash
  timeout) joins the open one; nothing is texted twice.
- ``note``: becomes the run's ``last_note`` (its pulse item) and a ``note`` event.
- ``check``: ``TrustChecker.request_check`` (EM-4's API; ``AskOwnerTrust`` until it is
  wired), waited for like an ask. A decision that comes after the CLI gave up is delivered
  on resume.

Owner messages (``deliver_message``) are appended to the session's next CLI reply as
``Message from the owner: …``, or open its next turn.

Everything a delivery rests on is in the store (``store.questions``), with conditional
updates, so an answer is delivered exactly once whichever path (a blocked CLI call, a resume
prompt) gets there first, and a restart loses nothing but the CLI call in flight.

Events (``WorkerEvents``) are the hook EM-5 (relay) and EM-6 (digest, urgency) subscribe to:
``question``, ``note`` (``urgent`` set for ``--urgent``) and ``check``. For now they are logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from resonant.gate.codes import clean_external
from resonant.gateway.channel import Channel, ChannelError
from resonant.redact import Redactor
from resonant.runners.trust_seam import CHECK_TOOL, TrustChecker, slug
from resonant.runners.worker_cli import (
    EXIT_DENIED,
    EXIT_OK,
    EXIT_PENDING,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
    NO_ANSWER_YET,
    Ask,
    Check,
    Note,
    Reply,
    Request,
)
from resonant.store import questions as qs
from resonant.store import runs
from resonant.store.audit import audit
from resonant.store.db import atomic
from resonant.store.runs import Run

log = logging.getLogger(__name__)

BLOCK_S = 480.0  # how long one ask or check call waits (Claude's Bash tool allows 600 s)
MAX_OPEN_ASKS = 1  # so an answer can't reach the wrong one of a run's questions
OWNER_TEXT_MAX = 600
NOTE_MAX = 200
ASK_TIMEOUT = "No answer from the owner; use your judgment or stop."
CHECK_TIMEOUT = "No decision from the owner; treat it as denied: don't do it."
RESTARTED = (
    "Resonant restarted before the owner decided; run `resonant check` again if you still need it."
)
CHECK_OK = "ok"
CHECK_DENIED = "denied"
RUN_OVER = "This run is over; stop."

EventKind = Literal["question", "note", "check"]


@dataclass(frozen=True)
class WorkerEvent:
    """Something a worker told the owner. ``urgent``: ``--urgent`` (always for a question,
    which blocks work; docs/employee-plan.md D5)."""

    kind: EventKind
    run_code: str
    text: str
    urgent: bool
    at: datetime
    question_id: int | None = None
    options: tuple[str, ...] = ()


Subscriber = Callable[[WorkerEvent], Awaitable[None] | None]


class WorkerEvents:
    """The in-process hook for worker events. ``subscribe`` returns the unsubscribe."""

    def __init__(self) -> None:
        self._subs: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        self._subs.append(fn)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subs.remove(fn)

        return unsubscribe

    async def emit(self, event: WorkerEvent) -> None:
        log.info(
            "worker event",
            extra={"kind": event.kind, "run": event.run_code, "urgent": event.urgent,
                   "question_id": event.question_id},
        )  # fmt: skip
        for fn in list(self._subs):
            try:
                result = fn(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("worker event subscriber failed")


@dataclass(frozen=True)
class OpenQuestion:
    """An open ``resonant ask``, for the relay (EM-5)."""

    id: int
    run_code: str
    question: str
    options: tuple[str, ...]
    asked_at: datetime
    urgent: bool


@dataclass(frozen=True)
class Relayed:
    """An answer or message accepted for a run. ``text`` is a code-built line for the owner."""

    run_code: str
    text: str


class RelayRefused(Exception):
    """``answer_question``/``deliver_message`` refused: the message is a sentence."""


def clip(text: str, limit: int) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _quote(text: str, limit: int = 200) -> str:
    return '"' + clip(text, limit) + '"'


class WorkerDesk:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        channel: Channel,
        trust: TrustChecker,
        clock: Callable[[], datetime],
        ask_timeout_s: float,
        wake: Callable[[Run, str], None],
        redactor_for: Callable[[str], Redactor] = lambda _code: Redactor(),
        on_note: Callable[[str], None] = lambda _code: None,
        block_s: float = BLOCK_S,
    ) -> None:
        """``wake(run, key)`` tells the run's task something arrived (a no-op unless it is
        waiting). ``redactor_for(code)`` knows the run's secrets; ``on_note(code)`` refreshes
        its pulse item."""
        self.conn = conn
        self.channel = channel
        self.trust = trust
        self.clock = clock
        self.ask_timeout = timedelta(seconds=ask_timeout_s)
        self.block_s = block_s
        self._wake = wake
        self._redactor_for = redactor_for
        self._on_note = on_note
        self.events = WorkerEvents()
        self._waiters: dict[int, asyncio.Event] = {}  # question id -> set when it leaves open
        # (run, system, action, irreversible, cost, what) -> (question id, the decision task)
        self._checks: dict[tuple[Any, ...], tuple[int, asyncio.Task[None]]] = {}

    # -- the CLI ---------------------------------------------------------------------------

    async def handle(self, code: str, req: Request, gone: asyncio.Future[None]) -> Reply | None:
        """One CLI request from ``code``'s session. None: the caller hung up, send nothing."""
        run = runs.get(self.conn, code)
        if run is None or not run.active:
            return Reply(RUN_OVER, EXIT_UNREACHABLE)
        match req:
            case Ask():
                return await self._ask(run, req, gone)
            case Note():
                return await self._note(run, req)
            case Check():
                return await self._check(run, req, gone)

    async def _ask(self, run: Run, req: Ask, gone: asyncio.Future[None]) -> Reply | None:
        open_asks = qs.open_for_run(self.conn, run.code, "ask")
        q = next(
            (o for o in open_asks if o.question == req.question and o.options == req.options),
            None,
        )
        if q is None:
            if len(open_asks) >= MAX_OPEN_ASKS:
                return self._reply(run.code, (
                    f"You already asked the owner: {_quote(open_asks[0].question)}. Only one "
                    "question can be open at a time: finish what you can, then end your turn; "
                    "you'll be resumed with the answer."
                ), EXIT_PENDING)  # fmt: skip
            q = qs.ask(self.conn, run.code, "ask", req.question, options=req.options,
                       urgent=req.urgent, at=self.clock())  # fmt: skip
            with atomic(self.conn):
                audit(self.conn, "run.ask", run=run.code, question_id=q.id)
            await self.events.emit(WorkerEvent("question", run.code, req.question, True,
                                               q.asked_at, q.id, req.options))  # fmt: skip
            await self._tell_owner(run, q)
        await self._wait_for(self._waiter(q.id), gone)
        return self._ask_reply(run.code, q.id, hung_up=gone.done())

    def _ask_reply(self, code: str, qid: int, *, hung_up: bool) -> Reply | None:
        q = qs.get(self.conn, qid)
        assert q is not None
        if q.status == "open":
            qs.defer(self.conn, qid)
            q = qs.get(self.conn, qid)
            assert q is not None
        if q.status == "open":
            return None if hung_up else self._reply(code, NO_ANSWER_YET, EXIT_PENDING)
        if q.status == "cancelled":
            return Reply(RUN_OVER, EXIT_UNREACHABLE)
        if hung_up:
            return None  # still undelivered: the next turn's prompt carries it
        return self._reply(code, _answer_line(q), EXIT_OK, qid=qid)

    async def _tell_owner(self, run: Run, q: qs.Question) -> None:
        """``R4 asks: …`` to the requester, on the run replies' path (EM-5 refines routing)."""
        red = self._redactor_for(run.code)
        text = f"{run.code} asks: {clean_external(red.text(q.question))}"
        if q.options:
            text += "\nOptions: " + ", ".join(clean_external(red.text(o)) for o in q.options)
        if len(text) > OWNER_TEXT_MAX:
            text = text[: OWNER_TEXT_MAX - 3].rstrip() + "..."
        try:
            await self.channel.send(run.principal, text, dedupe_key=f"run:{run.code}:ask:{q.id}")
        except ChannelError as e:  # the question stays open; /runs and EM-5 can still show it
            log.warning("question not texted", extra={"run": run.code, "reason": str(e)})

    async def _note(self, run: Run, req: Note) -> Reply:
        red = self._redactor_for(run.code)
        shown = clip(clean_external(red.text(req.text)), NOTE_MAX)
        runs.update(self.conn, run.code, last_note=shown)
        with atomic(self.conn):
            audit(self.conn, "run.note", run=run.code, urgent=req.urgent)
        await self.events.emit(WorkerEvent("note", run.code, req.text, req.urgent, self.clock()))
        with contextlib.suppress(Exception):
            self._on_note(run.code)
        return self._reply(run.code, "Noted.", EXIT_OK)

    async def _check(self, run: Run, req: Check, gone: asyncio.Future[None]) -> Reply | None:
        try:
            system, action = slug(req.system), slug(req.action)
        except ValueError as e:
            return Reply(f"Usage error: {e}.", EXIT_USAGE)
        key = (run.code, system, action, req.irreversible, req.cost_usd, req.what)
        entry = self._checks.get(key)
        if entry is None:
            text = f"{action} on {system}: {req.what}"
            if req.irreversible:
                text += " (irreversible)"
            if req.cost_usd > 0:
                text += f" (costs ${req.cost_usd:g})"
            q = qs.ask(self.conn, run.code, "check", text, at=self.clock())
            await self.events.emit(WorkerEvent("check", run.code, text, False, q.asked_at, q.id))
            task = asyncio.ensure_future(self._decide(q.id, run.code, system, action, req))
            entry = (q.id, task)
            self._checks[key] = entry
            task.add_done_callback(lambda _t, k=key: self._checks.pop(k, None))
        qid, task = entry
        decided = asyncio.ensure_future(asyncio.shield(task))
        try:
            await asyncio.wait({decided, gone}, timeout=self.block_s,
                               return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
        finally:
            decided.cancel()
        q = qs.get(self.conn, qid)
        assert q is not None
        if q.status == "open":
            qs.defer(self.conn, qid)
            q = qs.get(self.conn, qid)
            assert q is not None
        if q.status == "open":
            return None if gone.done() else self._reply(run.code, NO_ANSWER_YET, EXIT_PENDING)
        if q.status == "cancelled":
            return Reply(RUN_OVER, EXIT_UNREACHABLE)
        if gone.done():
            return None
        ok = q.status == "answered" and q.answer == CHECK_OK
        return self._reply(run.code, _check_line(q, short=True), EXIT_OK if ok else EXIT_DENIED,
                           qid=qid)  # fmt: skip

    async def _decide(self, qid: int, code: str, system: str, action: str, req: Check) -> None:
        """The trust decision for one check; recorded (and the run woken) when it comes."""
        try:
            answer = await self.trust.request_check(
                code, system, action, irreversible=req.irreversible, cost_usd=req.cost_usd,
                what=req.what,
            )  # fmt: skip
        except asyncio.CancelledError:
            raise
        except ValueError:
            answer = CHECK_DENIED
        except Exception:
            log.exception("trust check failed", extra={"run": code})
            answer = CHECK_DENIED
        with atomic(self.conn):
            audit(self.conn, "run.check", run=code, system=system, verb=action, answer=answer)
        if qs.answer(self.conn, qid, CHECK_OK if answer == "ok" else CHECK_DENIED,
                     at=self.clock()):  # fmt: skip
            self._notify(code, qid)

    async def _wait_for(self, event: asyncio.Event, gone: asyncio.Future[None]) -> None:
        waiter = asyncio.ensure_future(event.wait())
        try:
            await asyncio.wait({waiter, gone}, timeout=self.block_s,
                               return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
        finally:
            waiter.cancel()

    def _waiter(self, qid: int) -> asyncio.Event:
        return self._waiters.setdefault(qid, asyncio.Event())

    def _reply(self, code: str, text: str, exit: int, *, qid: int | None = None) -> Reply:
        """``text`` plus what the session hasn't seen (earlier answers, owner messages). All
        of it, and question ``qid``, is marked delivered once the reply is written."""
        answers = [q for q in qs.undelivered(self.conn, code) if q.id != qid]
        messages = qs.pending_messages(self.conn, code)
        parts = [text, *(_delivery_line(q) for q in answers)]
        parts += [f"Message from the owner: {m.text}" for m in messages]

        def sent() -> None:
            now = self.clock()
            for q in ([] if qid is None else [qid]) + [a.id for a in answers]:
                qs.mark_delivered(self.conn, q, at=now)
            qs.mark_messages_delivered(self.conn, [m.id for m in messages], at=now)

        return Reply("\n\n".join(parts), exit, on_sent=sent)

    # -- the owner's side (EM-5's API) -------------------------------------------------------

    def open_questions(self) -> list[OpenQuestion]:
        """Every open ``resonant ask`` of an active run, oldest first."""
        return [
            OpenQuestion(q.id, q.run_code, q.question, q.options, q.asked_at, q.urgent)
            for q in qs.open_asks(self.conn)
        ]

    def answer_question(self, run_code: str, text: str) -> Relayed:
        """The owner's answer to the run's oldest open question. Delivered to a CLI call
        still waiting, else on the session's next turn (the run resumes if it was waiting)."""
        text = text.strip()
        if not text:
            raise RelayRefused("An answer needs some text.")
        run = runs.get(self.conn, run_code)
        if run is None:
            raise RelayRefused(f"No run {run_code}.")
        if not run.active:
            raise RelayRefused(f"{run.code} isn't running ({run.status}).")
        for q in qs.open_for_run(self.conn, run.code, "ask"):
            if qs.answer(self.conn, q.id, text, at=self.clock()):
                with atomic(self.conn):
                    audit(self.conn, "run.answer", run=run.code, question_id=q.id)
                self._notify(run.code, q.id)
                return Relayed(run.code, f"Sent to {run.code}.")
        raise RelayRefused(f"{run.code} has no open question.")

    def deliver_message(self, run_code: str, text: str) -> Relayed:
        """An owner message for a running worker: on its next CLI reply, or its next turn (a
        run waiting for an answer resumes with it)."""
        text = text.strip()
        if not text:
            raise RelayRefused("A message needs some text.")
        run = runs.get(self.conn, run_code)
        if run is None:
            raise RelayRefused(f"No run {run_code}.")
        if not run.active:
            raise RelayRefused(f"{run.code} isn't running ({run.status}).")
        m = qs.add_message(self.conn, run.code, text, at=self.clock())
        with atomic(self.conn):
            audit(self.conn, "run.message", run=run.code, message_id=m.id)
        self._wake(run, f"message:{m.id}")
        return Relayed(run.code, f"Passed to {run.code}.")

    def _notify(self, code: str, qid: int) -> None:
        """A question left ``open``: release the CLI calls waiting on it and wake the run."""
        if (ev := self._waiters.pop(qid, None)) is not None:
            ev.set()
        run = runs.get(self.conn, code)
        if run is not None and run.active:
            self._wake(run, f"answer:{qid}")

    # -- the run's lifecycle -------------------------------------------------------------------

    def resume_text(self, code: str) -> str | None:
        """What the session hasn't seen (answers, timeouts, owner messages), marked delivered
        now: the prompt of its next turn. None when there is nothing."""
        now = self.clock()
        parts = [_delivery_line(q) for q in qs.take_answers(self.conn, code, at=now)]
        parts += [f"Message from the owner: {m.text}"
                  for m in qs.take_messages(self.conn, code, at=now)]  # fmt: skip
        if not parts:
            return None
        return "\n\n".join(parts)

    def has_deliveries(self, code: str) -> bool:
        return bool(qs.undelivered(self.conn, code) or qs.pending_messages(self.conn, code))

    def time_out_due(self, code: str) -> None:
        """Open questions past ``ask_timeout`` get their timeout text instead of an answer."""
        now = self.clock()
        for q in qs.open_for_run(self.conn, code):
            if q.asked_at + self.ask_timeout > now:
                continue
            if qs.time_out(self.conn, q.id, ASK_TIMEOUT if q.kind == "ask" else CHECK_TIMEOUT,
                           at=now):  # fmt: skip
                with atomic(self.conn):
                    audit(self.conn, "run.ask_timeout", run=code, question_id=q.id)
                self._stop_check(q.id)
                if (ev := self._waiters.pop(q.id, None)) is not None:
                    ev.set()

    def wait_until(self, code: str) -> datetime | None:
        """When the run's earliest open question times out; None when none is open."""
        open_qs = qs.open_for_run(self.conn, code)
        return min((q.asked_at + self.ask_timeout for q in open_qs), default=None)

    def close_run(self, code: str) -> None:
        """The run ended: its open questions are closed and any decision in flight dropped."""
        qs.cancel_open(self.conn, code)
        for key, (qid, task) in list(self._checks.items()):
            if key[0] == code:
                task.cancel()
                if (ev := self._waiters.pop(qid, None)) is not None:
                    ev.set()
        for q in qs.for_run(self.conn, code):
            if (ev := self._waiters.pop(q.id, None)) is not None:
                ev.set()

    def startup(self) -> None:
        """Daemon start: no CLI call survived, so open asks are deferred (the run waits for
        them) and open checks, whose decision was in this process, are closed with
        ``RESTARTED`` (their approvals expired, so a late yes doesn't look accepted)."""
        qs.defer_all(self.conn)
        now = self.clock()
        for row in self.conn.execute(
            "SELECT id, run_code FROM run_questions WHERE status = 'open' AND kind = 'check'"
        ).fetchall():
            qs.time_out(self.conn, row["id"], RESTARTED, at=now)
            run = runs.get(self.conn, row["run_code"])
            if run is not None and run.task_id is not None:
                self.conn.execute(
                    "UPDATE approvals SET status = 'expired', decided_at = ? WHERE task_id = ?"
                    " AND tool = ? AND status = 'pending'",
                    (now.isoformat(), run.task_id, CHECK_TOOL),
                )

    async def aclose(self) -> None:
        tasks = [t for _qid, t in self._checks.values()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _stop_check(self, qid: int) -> None:
        for _key, (q, task) in list(self._checks.items()):
            if q == qid:
                task.cancel()


def _answer_line(q: qs.Question) -> str:
    if q.status == "timed_out":
        return q.answer or ASK_TIMEOUT
    return f"The owner answered: {q.answer}"


def _check_line(q: qs.Question, *, short: bool = False) -> str:
    if q.status == "timed_out":
        return q.answer or CHECK_TIMEOUT
    if q.answer == CHECK_OK:
        return "ok" if short else f"Your check ({clip(q.question, 200)}) is ok: go ahead."
    denied = "denied: don't do it. Find another way, or stop and say why in your summary."
    return denied if short else f"Your check ({clip(q.question, 200)}) was {denied}"


def _delivery_line(q: qs.Question) -> str:
    """An answer the session gets after the CLI call that asked has returned."""
    if q.kind == "check":
        return _check_line(q)
    if q.status == "timed_out":
        return f"Your question {_quote(q.question)}: {q.answer or ASK_TIMEOUT}"
    return f"The owner answered your question {_quote(q.question)}: {q.answer}"

"""The daemon side of the worker CLI (docs/employee-plan.md D3, EM-3): questions, notes,
checks and owner messages for running Claude workers.

``WorkerDesk.handle`` answers one CLI request from a run's socket (``worker_cli``):

- ``ask``: records the question, emits a ``question`` event, texts the requester
  ``R4 asks: <question>`` (``worker_text.owner_question_text``; the owner's reply comes back
  through the router's relay, EM-5), opens its pulse item, and waits up to
  ``block_s`` (8 minutes; the session's Bash tool allows 10) for ``answer_question``. Answered:
  the answer. Not yet: ``NO_ANSWER_YET`` (exit 3) and the question is deferred, so the run
  waits for it (``waiting``, reason ``question``) once the session's turn ends, and resumes
  the session with the answer (or, after ``runner.ask_timeout_minutes``, with
  ``ASK_TIMEOUT``). The same question asked again while it is open (a retry after a Bash
  timeout) joins the open one; nothing is texted twice.
- ``note``: becomes the run's ``last_note`` (its pulse item) and a ``note`` event.
- ``check``: EM-4's ``TrustChecks.request_check``, waited for like an ask. The call (and so
  the owner's approval) outlives the CLI's wait and the turn; a decision that comes later
  wakes the run and is delivered on resume.

- ``restart``/``update`` (GD-1): Resonant itself, through the same ``Ops`` as ``/restart
  resonant`` and ``/update`` (``SelfOps``). Allowed without asking when the run's requester is
  the owner and the ask mentions it (``asked_for``: restart, redeploy, deploy, …; for update
  also "update resonant"), once per run; otherwise it is a check like any other (system
  ``resonant``, action ``restart`` or ``update``: net new the first time, so ``yes always``
  covers the next). The
  reply goes out before the restart (the helper waits 3 s); the restart stops the session and
  the run resumes it afterwards, with a note saying why (``on_restart``).

Owner messages (``deliver_message``) are appended to the session's next CLI reply as
``Message from the owner: …``, or open its next turn.

Everything a delivery rests on is in the store (``store.questions``), with conditional
updates, so an answer is delivered whichever path (a blocked CLI call, a resume
prompt) gets there first (at least once: a reply is marked delivered once it is written),
and a restart loses nothing but the CLI call in flight.

Events (``WorkerEvents``) are the hook EM-6 (digest, urgency) subscribes to: ``question``,
``note`` (``urgent`` set for ``--urgent``) and ``check``. The relay (EM-5) reads the store
through ``open_questions``/``answer_question``/``deliver_message`` instead.
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
from typing import Any, Literal, Protocol

from resonant.gate.codes import clean_external
from resonant.gateway.channel import Channel, ChannelError
from resonant.redact import Redactor
from resonant.runners.worker_cli import (
    EXIT_BUSY,
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
    SelfOp,
)
from resonant.store import pulse as pulse_store
from resonant.store import questions as qs
from resonant.store import runs
from resonant.store.audit import audit
from resonant.store.db import atomic
from resonant.store.runs import Run
from resonant.store.trust import ANY, slug
from resonant.trust.checks import CHECK_TOOL, MAX_PENDING
from resonant.worker_text import owner_question_text

log = logging.getLogger(__name__)

BLOCK_S = 480.0  # how long one ask or check call waits (Claude's Bash tool allows 600 s)
MAX_OPEN_ASKS = 1  # so an answer can't reach the wrong one of a run's questions
OWNER_TEXT_MAX = 600
TWIN_S = 600.0  # an ask repeated this soon after its answer is a retry: the same answer
NOTE_MAX = 200
ASK_TIMEOUT = "No answer from the owner; use your judgment or stop."
CHECK_TIMEOUT = "No decision from the owner; treat it as denied: don't do it."
RESTARTED = (
    "Resonant restarted before the owner decided; run `resonant check` again if you still need it."
)
CHECK_OK = "ok"
CHECK_DENIED = "denied"
CHECK_BUSY = "busy"  # not asked: MAX_PENDING checks already wait for the owner
BUSY = (
    "Too many pending checks; wait for the owner's answers to those, then run this check "
    "again. This is not a no."
)
RUN_OVER = "This run is over; stop."
PULSE_SOURCE = "core"
QUESTION_ITEM_TTL_S = 24 * 3600  # the pulse re-mentions an open question each hour until then

SELF_SYSTEM = "resonant"  # the trust system of a worker's restart or update
NO_SELF_OPS = "Not done: restarting Resonant from a worker isn't set up here."
RESTARTING = (
    "Resonant restarts in about {delay} s. The restart stops this session; the run resumes it "
    "afterwards (same session, with a note that it restarted). Don't start anything else now: "
    "run `sleep 120` and let the restart stop you."
)
_DEPLOY_WORDS = re.compile(r"\b(?:re-?start|re-?deploy|deploy|re-?launch|re-?boot|bounce)", re.I)
_UPDATE_WORDS = re.compile(
    r"\bupdate\s+(?:resonant|the\s+daemon|itself|yourself)\b|(?<!\w)/update\b", re.I
)

EventKind = Literal["question", "note", "check"]
NO_TRUST = "denied: checks aren't set up here, so don't do it."


class TrustChecker(Protocol):
    """``resonant.trust.checks.TrustChecks`` (EM-4), as the desk uses it."""

    async def request_check(
        self,
        run_code: str,
        system: str,
        action: str,
        irreversible: bool = False,
        cost_usd: float = 0.0,
        what: str = "",
    ) -> Literal["ok", "denied", "busy"]: ...


class SelfOps(Protocol):
    """``resonant.tools.ops.Ops``: what a worker's ``restart``/``update`` runs. Failures raise
    with a code-built message (``ToolFailed``)."""

    async def restart_resonant(self) -> dict[str, Any]: ...

    async def update_resonant(self) -> dict[str, Any]: ...


def asked_for(ask: str, op: Literal["restart", "update"]) -> bool:
    """Whether the run's ask mentions this (a mention is enough: the owner asked for it)."""
    if _DEPLOY_WORDS.search(ask):
        return True
    return op == "update" and _UPDATE_WORDS.search(ask) is not None


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
        trust: TrustChecker | None,
        clock: Callable[[], datetime],
        ask_timeout_s: float,
        wake: Callable[[Run, str], None],
        redactor_for: Callable[[str], Redactor] = lambda _code: Redactor(),
        on_note: Callable[[str], None] = lambda _code: None,
        block_s: float = BLOCK_S,
        self_ops: SelfOps | None = None,
        owner: str | None = None,
        on_restart: Callable[[str], None] = lambda _code: None,
    ) -> None:
        """``wake(run, key)`` tells the run's task something arrived (a no-op unless it is
        waiting). ``redactor_for(code)`` knows the run's secrets; ``on_note(code)`` refreshes
        its pulse item. ``self_ops`` restarts or updates Resonant (None: refused), ``owner``
        is the principal whose ask can include that, and ``on_restart(code)`` runs just
        before a restart this run asked for."""
        self.conn = conn
        self.channel = channel
        self.trust = trust
        self.clock = clock
        self.ask_timeout = timedelta(seconds=ask_timeout_s)
        self.block_s = block_s
        self._wake = wake
        self._redactor_for = redactor_for
        self._on_note = on_note
        self.self_ops = self_ops
        self.owner = owner
        self._on_restart = on_restart
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
            case SelfOp():
                return await self._self_op(run, req, gone)

    async def _self_op(self, run: Run, req: SelfOp, gone: asyncio.Future[None]) -> Reply | None:
        """``resonant restart|update``: allowed by the ask, else checked like a net-new
        action; then the same ``Ops`` as ``/restart resonant`` and ``/update``."""
        if self.self_ops is None:
            return Reply(NO_SELF_OPS, EXIT_DENIED)
        checked: Reply | None = None
        # The ask covers one restart or update per run: a resumed session that asks again
        # (a loop) goes through the check like anything else.
        done = self.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'run.self_op'"
            " AND json_extract(detail, '$.run') = ?",
            (run.code,),
        ).fetchone()[0]
        if not (self.owner is not None and run.principal == self.owner and done == 0
                and asked_for(run.ask, req.op)):  # fmt: skip
            what = f"{req.op} Resonant (the daemon itself)"
            checked = await self._check(run, Check(SELF_SYSTEM, req.op, what), gone)
            if checked is None or checked.exit != EXIT_OK:
                return checked
        with atomic(self.conn):
            audit(self.conn, "run.self_op", run=run.code, op=req.op, by_ask=checked is None)
        try:
            if req.op == "update":
                result = await self.self_ops.update_resonant()
            else:
                result = await self.self_ops.restart_resonant()
            ok, text = True, self._self_op_text(run.code, req.op, result)
        except Exception as e:
            from resonant.executor import ToolFailed

            why = str(e) if isinstance(e, ToolFailed) else f"it failed ({type(e).__name__})"
            log.warning("worker self op failed", extra={"run": run.code, "op": req.op})
            ok, text = False, f"Not done: {req.op} Resonant: {why}."
        rc = EXIT_OK if ok else EXIT_DENIED
        if checked is None:
            return self._reply(run.code, text, rc)
        _first, _, rest = checked.text.partition("\n\n")  # what the check reply delivered
        return Reply(text + (f"\n\n{rest}" if rest else ""), rc, on_sent=checked.on_sent)

    def _self_op_text(self, code: str, op: str, result: dict[str, Any]) -> str:
        """The reply to a restart or update that went through, from its result only."""
        delay = result.get("delay_s", 3)
        if result.get("restarting"):
            with contextlib.suppress(Exception):
                self._on_restart(code)
            if op == "update":
                head = f"Updated {result.get('old')} -> {result.get('new')}, synced. "
                return head + RESTARTING.format(delay=delay)
            return RESTARTING.format(delay=delay)
        if op == "update" and result.get("synced") is False:
            return (
                f"Not restarted: pulled {result.get('new')} but uv sync failed "
                f"({result.get('sync_error')}); the running Resonant is unchanged."
            )
        return f"Already up to date ({result.get('new')}): nothing to deploy, no restart."

    async def _ask(self, run: Run, req: Ask, gone: asyncio.Future[None]) -> Reply | None:
        open_asks = qs.open_for_run(self.conn, run.code, "ask")
        q = next(
            (o for o in open_asks if o.question == req.question and o.options == req.options),
            None,
        )
        twin = None if q is not None else _answered_twin(self.conn, run.code, req, self.clock())
        if twin is not None:
            # A retry of the run's last question just after the owner answered it (the
            # session lost the reply): the same answer again, and the owner isn't re-texted.
            return self._reply(run.code, f"You asked this already. {_answer_line(twin)}",
                               EXIT_OK, qid=twin.id)  # fmt: skip
        if q is None:
            if len(open_asks) >= MAX_OPEN_ASKS:
                return self._reply(run.code, (
                    f"You already asked the owner: {_quote(open_asks[0].question)}. Only one "
                    "question can be open at a time: finish what you can, then end your turn; "
                    "you'll be resumed with the answer."
                ), EXIT_PENDING)  # fmt: skip
            q = qs.ask(self.conn, run.code, "ask", req.question, options=req.options,
                       urgent=req.urgent, at=self.clock())  # fmt: skip
            self._waiter(q.id)  # before anything awaits: an answer can come at once
            with atomic(self.conn):
                audit(self.conn, "run.ask", run=run.code, question_id=q.id)
            await self.events.emit(WorkerEvent("question", run.code, req.question, True,
                                               q.asked_at, q.id, req.options))  # fmt: skip
            await self._tell_owner(run, q)
        current = qs.get(self.conn, q.id)
        if current is not None and current.status == "open":
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
        """``R4 asks: <q>`` (with ``(a) … (b) …`` options; ``⚠`` first when urgent) to the
        requester, on the run replies' path. With other questions open it ends with how to
        answer this one by code, since a plain reply would be ambiguous (EM-5)."""
        others = any(
            o.run_code != run.code and _principal(self.conn, o.run_code) == run.principal
            for o in qs.open_asks(self.conn)
        )
        text = owner_question_text(
            run.code, q.question, q.options, urgent=q.urgent, others_open=others,
            redactor=self._redactor_for(run.code),
        )  # fmt: skip
        self._pulse_question(run, q)
        try:
            await self.channel.send(run.principal, text, dedupe_key=f"run:{run.code}:ask:{q.id}")
        except ChannelError as e:  # the question stays open; the pulse re-mentions it
            log.warning("question not texted", extra={"run": run.code, "reason": str(e)})

    def _pulse_question(self, run: Run, q: qs.Question) -> None:
        """The question as a core pulse item, already told (it is texted now). The hourly
        pulse re-mentions it while it stays open (``pulse_job.refresh_core_items``)."""
        title = owner_question_text(
            run.code, q.question, q.options, urgent=q.urgent,
            redactor=self._redactor_for(run.code),
        ).replace("\n", " ")  # fmt: skip
        now = self.clock()
        with atomic(self.conn):
            _change, item = pulse_store.upsert(
                self.conn, PULSE_SOURCE, question_item_key(run.code, q.id), title, "normal",
                {"open": True}, False, QUESTION_ITEM_TTL_S, now,
            )  # fmt: skip
            pulse_store.mark_told(self.conn, [item], now)

    def _pulse_question_done(self, code: str, qid: int) -> None:
        """Clear the question's pulse item silently: answered, timed out or cancelled."""
        key, now = question_item_key(code, qid), self.clock()
        with atomic(self.conn):
            item = pulse_store.get(self.conn, PULSE_SOURCE, key)
            if item is None or item.cleared:
                return
            _change, cleared = pulse_store.upsert(
                self.conn, PULSE_SOURCE, key, item.title, "normal", {"open": False}, True,
                QUESTION_ITEM_TTL_S, now,
            )  # fmt: skip
            pulse_store.mark_told(self.conn, [cleared], now)

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
            if ANY in (system, action):
                raise ValueError("name one system and one action, not *")
        except ValueError as e:
            return Reply(f"Usage error: --system/--action: {e}.", EXIT_USAGE)
        if self.trust is None:  # fail closed
            return Reply(NO_TRUST, EXIT_DENIED)
        key = (run.code, system, action, req.irreversible, req.cost_usd, req.what)
        entry = self._checks.get(key)
        if entry is None and len(qs.open_for_run(self.conn, run.code, "check")) >= MAX_PENDING:
            return self._reply(run.code, BUSY, EXIT_BUSY)  # not a "no": nothing was asked
        if entry is None:
            text = self._check_text(system, action, req)
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
        busy = q.status == "answered" and q.answer == CHECK_BUSY
        rc = EXIT_OK if ok else EXIT_BUSY if busy else EXIT_DENIED
        return self._reply(run.code, _check_line(q, short=True), rc, qid=qid)

    @staticmethod
    def _check_text(system: str, action: str, req: Check, *, daemon: bool = False) -> str:
        # A daemon-made check reads "<action> on <system> (asked by Resonant): …". A worker's
        # can't: its system is a slug (no spaces), so ": " always follows it directly.
        by = " (asked by Resonant)" if daemon else ""
        text = f"{action} on {system}{by}: {req.what}"
        if req.irreversible:
            text += " (irreversible)"
        if req.cost_usd > 0:
            text += f" (costs ${req.cost_usd:g})"
        return text

    def find_check(self, code: str, req: Check) -> qs.Question | None:
        """The latest daemon-made check row of ``code`` for exactly this check, or None. A
        check the worker asked itself (``resonant check``) never matches."""
        text = self._check_text(slug(req.system), slug(req.action), req, daemon=True)
        found = [q for q in qs.for_run(self.conn, code) if q.kind == "check" and q.question == text]
        return max(found, key=lambda q: q.id, default=None)

    async def request_check(self, run: Run, req: Check) -> qs.Question:
        """A check the daemon makes for a run itself (EM-7: ``external_pr`` before a fork
        run's upstream PR), with the lifecycle of a worker's ``resonant check``: a check row,
        the decision in the background, the run woken when it comes, closed by
        ``ask_timeout``, a restart or the run ending. Joins an open one for the same check.
        Returns the row as it stands (already answered when a rule decided at once). Without
        ``trust`` the row is answered ``denied`` (fail closed)."""
        system, action = slug(req.system), slug(req.action)
        if ANY in (system, action):
            raise ValueError("name one system and one action, not *")
        key = (run.code, system, action, req.irreversible, req.cost_usd, req.what, "daemon")
        entry = self._checks.get(key)
        if entry is None:
            text = self._check_text(system, action, req, daemon=True)
            q = qs.ask(self.conn, run.code, "check", text, at=self.clock())
            if self.trust is None:
                qs.answer(self.conn, q.id, CHECK_DENIED, at=self.clock())
                return qs.get(self.conn, q.id) or q
            await self.events.emit(WorkerEvent("check", run.code, text, False, q.asked_at, q.id))
            task = asyncio.ensure_future(self._decide(q.id, run.code, system, action, req))
            entry = (q.id, task)
            self._checks[key] = entry
            task.add_done_callback(lambda _t, k=key: self._checks.pop(k, None))
        qid, task = entry
        # a rule (or the net-new defaults) may decide at once: give it a moment
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.5)
        q = qs.get(self.conn, qid)
        assert q is not None
        return q

    async def _decide(self, qid: int, code: str, system: str, action: str, req: Check) -> None:
        """The trust decision for one check; recorded (and the run woken) when it comes."""
        assert self.trust is not None
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
        recorded = {"ok": CHECK_OK, "busy": CHECK_BUSY}.get(answer, CHECK_DENIED)
        if qs.answer(self.conn, qid, recorded, at=self.clock()):
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

    def answer_question(
        self, run_code: str, text: str, *, question_id: int | None = None
    ) -> Relayed:
        """The owner's answer to the run's oldest open question (or exactly question
        ``question_id``: the one the owner was shown, so an answer can't land on a newer
        question). Delivered to a CLI call still waiting, else on the session's next turn
        (the run resumes if it was waiting)."""
        text = text.strip()
        if not text:
            raise RelayRefused("An answer needs some text.")
        run = runs.get(self.conn, run_code)
        if run is None:
            raise RelayRefused(f"No run {run_code}.")
        if not run.active:
            raise RelayRefused(f"{run.code} isn't running ({run.status}).")
        for q in qs.open_for_run(self.conn, run.code, "ask"):
            if question_id is not None and q.id != question_id:
                continue
            if qs.answer(self.conn, q.id, text, at=self.clock()):
                with atomic(self.conn):
                    audit(self.conn, "run.answer", run=run.code, question_id=q.id)
                self._pulse_question_done(run.code, q.id)
                self._notify(run.code, q.id)
                return Relayed(run.code, f"\u2192 {run.code}")
        if question_id is not None:
            raise RelayRefused(f"{run.code}'s question was already answered or timed out.")
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
        return Relayed(run.code, f"\u2192 {run.code} (message)")

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
                if q.kind == "ask":
                    self._pulse_question_done(code, q.id)
                self._stop_check(q.id)
                if (ev := self._waiters.pop(q.id, None)) is not None:
                    ev.set()

    def wait_until(self, code: str) -> datetime | None:
        """When the run's earliest open question times out; None when none is open."""
        open_qs = qs.open_for_run(self.conn, code)
        return min((q.asked_at + self.ask_timeout for q in open_qs), default=None)

    def close_run(self, code: str) -> None:
        """The run ended: its open questions are closed and any decision in flight dropped."""
        open_asks = qs.open_for_run(self.conn, code, "ask")
        qs.cancel_open(self.conn, code)
        for q in open_asks:
            self._pulse_question_done(code, q.id)
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


def question_item_key(code: str, qid: int) -> str:
    """The core pulse item of one open question: ``question/r4-12``."""
    return f"question/{code.lower()}-{qid}"


def _principal(conn: sqlite3.Connection, code: str) -> str | None:
    run = runs.get(conn, code)
    return None if run is None else run.principal


def _answered_twin(
    conn: sqlite3.Connection, code: str, req: Ask, now: datetime
) -> qs.Question | None:
    """The run's last ask, when it is this same question (and options), answered, and either
    not yet delivered or answered within ``TWIN_S``: a retry. An older answer, or one from
    before another question, is not reused: asking again then asks the owner again, so an
    earlier yes can't stand in for a later decision."""
    asks = [q for q in qs.for_run(conn, code) if q.kind == "ask"]
    if not asks:
        return None
    last = asks[-1]
    if last.status != "answered" or (last.question, last.options) != (req.question, req.options):
        return None
    fresh = last.answered_at is not None and now - last.answered_at <= timedelta(seconds=TWIN_S)
    return last if last.delivered_at is None or fresh else None


def _answer_line(q: qs.Question) -> str:
    if q.status == "timed_out":
        return q.answer or ASK_TIMEOUT
    return f"The owner answered: {q.answer}"


def _check_line(q: qs.Question, *, short: bool = False) -> str:
    if q.status == "timed_out":
        return q.answer or CHECK_TIMEOUT
    if q.answer == CHECK_OK:
        return "ok" if short else f"Your check ({clip(q.question, 200)}) is ok: go ahead."
    if q.answer == CHECK_BUSY:
        return BUSY if short else f"Your check ({clip(q.question, 200)}) wasn't asked. {BUSY}"
    denied = "denied: don't do it. Find another way, or stop and say why in your summary."
    return denied if short else f"Your check ({clip(q.question, 200)}) was {denied}"


def _delivery_line(q: qs.Question) -> str:
    """An answer the session gets after the CLI call that asked has returned."""
    if q.kind == "check":
        return _check_line(q)
    if q.status == "timed_out":
        return f"Your question {_quote(q.question)}: {q.answer or ASK_TIMEOUT}"
    return f"The owner answered your question {_quote(q.question)}: {q.answer}"

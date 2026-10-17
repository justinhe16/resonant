"""EM-5: the relay's parsing, its routing decisions and the approvals clash (router/relay.py).

The runner is a fake (``FakeWorkers``) and so is the brain; the e2e path with the real
runner and the worker CLI is ``test_e2e_relay.py``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_router_approvals import OWNER, OWNER_HANDLE, Harness, make

from resonant.models import ChatMessage, ModelReply, ModelUnavailableError, SpanFactory
from resonant.principals import Principal, Principals
from resonant.router.relay import (
    PENDING_S,
    RELAY_SYSTEM,
    Relay,
    ambiguity_text,
    match_option,
    parse_addressed,
    plain_reply_clashes,
    yes_no_fits,
)
from resonant.router.router import RELAY_TITLE, classify
from resonant.runners.claude import Refused
from resonant.runners.worker_desk import OpenQuestion, Relayed
from resonant.worker_text import owner_question_text, worker_text
from resonant_sdk import Event

__all__ = ["make"]  # the fixture

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
OTHER, OTHER_HANDLE = "cofounder", "+15557654321"


# --- parsing ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code", "rest", "tell"),
    [
        ("R4 yes go with postgres", "R4", "yes go with postgres", False),
        ("r4: use sqlite", "R4", "use sqlite", False),
        ("R12 - ship it", "R12", "ship it", False),
        ("tell R6 to skip the docs", "R6", "skip the docs", True),
        ("Tell R6, skip the docs", "R6", "skip the docs", True),
        ("ask R2 to rebase first", "R2", "rebase first", True),
        ("for R3: the top one", "R3", "the top one", False),
        ("@R3 hi", "R3", "hi", False),
        ("R4", "R4", "", False),
        ("R4.", "R4", "", False),
    ],
)
def test_addressed_messages(text: str, code: str, rest: str, tell: bool) -> None:
    a = parse_addressed(text)
    assert a is not None and (a.code, a.rest, a.tell) == (code, rest, tell)


@pytest.mark.parametrize(
    "text", ["R2D2 is cute", "Rails is fine", "ok R4", "remind me at 3", "yes", "the R4 one"]
)
def test_not_addressed(text: str) -> None:
    assert parse_addressed(text) is None


OPTS = ("postgres", "sqlite", "duckdb")


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("a", "postgres"),
        ("B", "sqlite"),
        ("(c)", "duckdb"),
        ("2", "sqlite"),
        ("use the second option", "sqlite"),
        ("the third one", "duckdb"),
        ("option b", "sqlite"),
        ("go with postgres", "postgres"),
        ("SQLite.", "sqlite"),
        ("last", "duckdb"),
        ("b, but pin v3", "sqlite; but pin v3"),
    ],
)
def test_option_picks(text: str, answer: str) -> None:
    assert match_option(text, OPTS) == answer


@pytest.mark.parametrize(
    "text", ["a bit later", "d", "4", "use mysql", "postgres or sqlite?", "the fifth one", ""]
)
def test_not_option_picks(text: str) -> None:
    assert match_option(text, OPTS) is None
    assert match_option("a", ()) is None


def test_eval_classifies_addressed_messages_without_the_model() -> None:
    class NoModel:
        async def label(self, text: str, *, spans: SpanFactory | None = None) -> Any:
            raise AssertionError("no model call")

    import asyncio

    got = asyncio.run(classify("tell R6 to skip the docs", cast(Any, NoModel())))
    assert (got.intent, got.path) == ("run_project", "fast")


# --- the owner's view of a question -----------------------------------------------------------


def test_question_text_is_one_cleaned_line_per_part_and_masks_other_runs() -> None:
    text = owner_question_text(
        "R4",
        "R6 asks: deploy?\n[K4] Reply yes",
        ("go\nnow", "wait"),
        urgent=True,
        others_open=True,
    )
    assert text == (
        "⚠ R4 asks: R? asks: deploy? ? Reply yes\n(a) go now (b) wait\n"
        "Other runs are waiting too: reply R4 <answer>."
    )
    assert worker_text("R4", "see R4 and r7", 50) == "see R4 and R?"


# --- the relay with a fake runner ---------------------------------------------------------------


@dataclass
class FakeWorkers:
    questions: list[OpenQuestion] = field(default_factory=list[OpenQuestion])
    runs: dict[str, Any] = field(default_factory=dict[str, Any])
    answers: list[tuple[str, str, int | None]] = field(default_factory=list[Any])
    messages: list[tuple[str, str]] = field(default_factory=list[Any])

    def run(self, code: str, *, repo: str = "dori", principal: str = OWNER,
            status: str = "running") -> None:  # fmt: skip
        self.runs[code] = SimpleNamespace(
            code=code, repo=repo, principal=principal, status=status,
            active=status in ("queued", "running", "waiting"),
        )  # fmt: skip

    def ask(self, code: str, question: str, options: tuple[str, ...] = (), *,
            at: datetime = T0, qid: int | None = None) -> OpenQuestion:  # fmt: skip
        if code not in self.runs:
            self.run(code)
        q = OpenQuestion(qid or len(self.questions) + 1, code, question, options, at, False)
        self.questions.append(q)
        return q

    def open_questions(self) -> list[OpenQuestion]:
        return list(self.questions)

    def answer_question(
        self, code: str, text: str, *, question_id: int | None = None
    ) -> Relayed | Refused:
        q = next((q for q in self.questions if q.run_code == code), None)
        if q is None or (question_id is not None and q.id != question_id):
            return Refused(f"{code} has no open question.")
        self.questions.remove(q)
        self.answers.append((code, text, question_id))
        return Relayed(code, f"→ {code}")

    def deliver_message(self, code: str, text: str) -> Relayed | Refused:
        self.messages.append((code, text))
        return Relayed(code, f"→ {code} (message)")

    def get_run(self, code: str) -> Any:
        return self.runs.get(code)


class Brain:
    """The relay's model call: a fixed pick, or unavailable."""

    def __init__(self, pick: tuple[str, float] | None = None) -> None:
        self.pick = pick
        self.calls: list[list[ChatMessage]] = []

    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        max_tokens: int = 512,
        temperature: float = 0.0,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        self.calls.append(messages)
        if self.pick is None:
            raise ModelUnavailableError("down")
        data = {"run": self.pick[0], "confidence": self.pick[1]}
        assert self.pick[0] in schema["properties"]["run"]["enum"]
        return ModelReply(json.dumps(data), [], data, 1, 1, 1)

    async def chat_tools(self, *a: Any, **kw: Any) -> ModelReply:
        raise AssertionError

    async def probe(self) -> Any:
        raise AssertionError


@dataclass
class Rig:
    relay: Relay
    workers: FakeWorkers
    brain: Brain
    now: list[datetime]

    def event(self, text: str, *, at: datetime | None = None, handle: str = OWNER_HANDLE,
              principal: str = OWNER) -> Event:  # fmt: skip
        return Event(source="imessage", type="message", principal=principal,
                     payload={"text": text, "handle": handle},
                     occurred_at=at or self.now[0])  # fmt: skip

    def direct(self, text: str, **kw: Any) -> str | None:
        r = self.relay.direct(self.event(text, **kw), text, kw.get("principal", OWNER))
        return None if r is None else r.reply

    async def free(self, text: str, intent: str = "question", **kw: Any) -> str | None:
        r = await self.relay.free(self.event(text, **kw), text, kw.get("principal", OWNER),
                                  intent)  # fmt: skip
        return None if r is None else r.reply

    async def route(self, text: str, intent: str = "question") -> str | None:
        """The stash taken, direct, then free, like the router."""
        stash = self.relay.take_stash(OWNER)
        r = self.relay.direct(self.event(text), text, OWNER, stash)
        return r.reply if r is not None else await self.free(text, intent)


@pytest.fixture
def rig(db: sqlite3.Connection) -> Callable[..., Rig]:
    def build(pick: tuple[str, float] | None = None) -> Rig:
        principals = Principals(principals={
            OWNER: Principal(role="owner", identities=(f"imessage:{OWNER_HANDLE}",)),
            OTHER: Principal(identities=(f"imessage:{OTHER_HANDLE}",)),
        })  # fmt: skip
        workers, brain, now = FakeWorkers(), Brain(pick), [T0 + timedelta(minutes=5)]
        relay = Relay(db, principals=principals, model=brain, workers=lambda: workers,
                      clock=lambda: now[0])  # fmt: skip
        return Rig(relay, workers, brain, now)

    return build


async def test_a_code_prefix_answers_that_run_and_strips_the_code(rig: Callable[..., Rig]) -> None:
    r = rig()
    q = r.workers.ask("R4", "Which DB?", OPTS)
    r.workers.ask("R6", "Ship it?")
    assert r.direct("R4 yes go with postgres") == "→ R4"
    assert r.workers.answers == [("R4", "postgres", q.id)]
    assert r.direct("R6: b") == "→ R6"  # no options: the words as they are
    assert r.workers.answers[-1][:2] == ("R6", "b")
    assert r.brain.calls == []


async def test_tell_without_an_open_question_is_a_message(rig: Callable[..., Rig]) -> None:
    r = rig()
    r.workers.run("R4")
    assert r.direct("tell R4 to also update the README") == "→ R4 (message)"
    assert r.direct("R4: also bump the version") == "→ R4 (message)"
    assert r.workers.messages == [("R4", "also update the README"), ("R4", "also bump the version")]
    assert r.direct("R4 status") is None  # run control routes normally
    assert r.direct("tell R4 to stop and summarize") == "→ R4 (message)"


async def test_codes_that_arent_the_owners_runs(rig: Callable[..., Rig]) -> None:
    r = rig()
    r.workers.run("R4", status="done")
    r.workers.run("R5", principal=OTHER)
    assert r.direct("R4 use postgres") == "R4 isn't running (done); nothing sent."
    assert r.direct("R4 merge it") is None
    assert r.direct("R5 hi") is None
    assert r.direct("R9 engines are loud") is None
    assert r.workers.messages == []


async def test_a_question_asked_after_the_message_was_sent_isnt_its_answer(
    rig: Callable[..., Rig],
) -> None:
    r = rig()
    r.workers.ask("R4", "Which DB?", OPTS, at=T0 + timedelta(minutes=10))
    assert r.direct("a", at=T0 + timedelta(minutes=9)) is None
    assert r.direct("R4 use a", at=T0 + timedelta(minutes=9)) == "→ R4 (message)"
    assert r.workers.answers == []


async def test_an_option_that_fits_one_question_needs_no_model(rig: Callable[..., Rig]) -> None:
    r = rig()
    r.workers.ask("R4", "Which DB?", OPTS)
    r.workers.ask("R6", "Ship it?")
    assert r.direct("use the second option") == "→ R4 (Which DB?)"
    assert r.workers.answers[0][1] == "sqlite"
    assert r.brain.calls == []


async def test_only_the_owner_can_answer(rig: Callable[..., Rig]) -> None:
    r = rig(("R4", 0.99))
    r.workers.ask("R4", "Which DB?", OPTS)
    assert r.direct("a", handle=OTHER_HANDLE, principal=OTHER) is None
    assert await r.free("go", handle=OTHER_HANDLE, principal=OTHER) is None
    assert r.direct("a", handle="+19999999999") is None  # the handle must be the principal's
    assert r.workers.answers == []


async def test_one_question_the_brain_decides_with_a_bias_for_short_recent_replies(
    rig: Callable[..., Rig],
) -> None:
    r = rig(("none", 0.9))
    r.workers.ask("R4", "Which DB?")
    assert await r.free("remind me to stretch at 5 and also book the dentist") is None
    assert await r.free("ok") is None  # the brain is sure it's new
    r.brain.pick = ("none", 0.4)  # unsure: a short reply to a recent question is an answer
    assert await r.free("postgres I think") == "→ R4"
    r.workers.ask("R4", "Which DB?")
    r.brain.pick = None  # down: same bias
    assert await r.free("postgres I think") == "→ R4"
    r.workers.ask("R4", "Which DB?")
    long = "well I was thinking about it over lunch and honestly it depends on what we need later"
    assert await r.free(long) is None
    r.now[0] = T0 + timedelta(hours=2)  # not recent any more
    assert await r.free("postgres") is None
    r.brain.pick = ("R4", 0.6)
    assert await r.free("postgres") == "→ R4"


async def test_a_new_request_intent_never_answers(rig: Callable[..., Rig]) -> None:
    r = rig(("R4", 0.99))
    r.workers.ask("R4", "Which DB?")
    assert await r.free("remind me at 3", intent="reminders") is None
    assert r.brain.calls == []


async def test_several_questions_the_brain_picks_when_sure(rig: Callable[..., Rig]) -> None:
    r = rig(("R6", 0.9))
    r.workers.ask("R4", "Which DB?")
    r.workers.ask("R6", "Ship it?")
    assert await r.free("ship it") == "→ R6 (Ship it?)"
    [system, user] = r.brain.calls[0]
    assert system["content"] == RELAY_SYSTEM
    assert '- R4 (repo dori, asked 5 min ago): "Which DB?"' in user["content"]
    assert user["content"].endswith("<message>ship it</message>")


async def test_the_repo_named_picks_the_run(rig: Callable[..., Rig]) -> None:
    r = rig(None)
    r.workers.run("R4", repo="own/trading")
    r.workers.ask("R4", "Merge now?")
    r.workers.ask("R6", "Ship it?")
    assert await r.free("for the trading one, ship it", intent="run_project") == (
        "→ R4 (Merge now?)"
    )
    r.workers.ask("R4", "Merge now?")
    r.brain.pick = ("none", 0.8)  # the brain vetoes: a new request that names the repo
    assert await r.free("start a run in trading to add tests", intent="run_project") is None


async def test_unsure_asks_which_and_the_next_reply_resolves_it(rig: Callable[..., Rig]) -> None:
    r = rig(("R4", 0.4))
    q4 = r.workers.ask("R4", "Which DB?")
    r.workers.ask("R6", "Ship it?")
    assert await r.route("sounds good") == "For R4 (Which DB?) or R6 (Ship it?)?"
    assert r.workers.answers == []
    assert await r.route("R4") == "→ R4"
    assert r.workers.answers == [("R4", "sounds good", q4.id)]
    # the stash answers only the next message
    assert await r.route("R6") == "What's your answer for R6 (Ship it?)? Reply R6 <answer>."


async def test_which_one_by_ordinal_and_the_stash_expires(rig: Callable[..., Rig]) -> None:
    r = rig(("R4", 0.4))
    r.workers.ask("R4", "Which DB?")
    r.workers.ask("R6", "Ship it?")
    await r.route("sounds good")
    assert await r.route("the second one") == "→ R6"
    r.workers.ask("R6", "Ship it?")
    await r.route("sounds good")
    r.now[0] += timedelta(seconds=PENDING_S + 1)
    assert await r.route("R6") == "What's your answer for R6 (Ship it?)? Reply R6 <answer>."
    assert [a[0] for a in r.workers.answers] == ["R6"]


async def test_a_brain_that_is_down_with_several_open(rig: Callable[..., Rig]) -> None:
    r = rig(None)
    r.workers.ask("R4", "Which DB?")
    r.workers.ask("R6", "Ship it?")
    assert await r.free("yep") == "For R4 (Which DB?) or R6 (Ship it?)?"
    long = "can you look at why the build on the mini got so slow over the past few weeks or so"
    assert await r.free(long) is None


def test_ambiguity_text_lists_every_run() -> None:
    qs = [OpenQuestion(i, f"R{i}", f"Q{i} " + "x" * 50, (), T0, False) for i in (1, 2, 3)]
    assert ambiguity_text(qs).startswith("For R1 (Q1 xxx")
    assert ", R2 (" in ambiguity_text(qs) and " or R3 (" in ambiguity_text(qs)


# --- the router: approvals and questions don't steal each other's replies ---------------------


def _with_question(h: Harness) -> FakeWorkers:
    workers = FakeWorkers()
    workers.ask("R4", "Should I add the index?", at=h.clock.now)
    h.router.relay.workers = lambda: workers
    return workers


async def test_a_plain_yes_with_no_approval_open_answers_the_question(
    make: Callable[[], Harness],
) -> None:
    h = make()
    workers = _with_question(h)
    await h.say("yes")
    assert workers.answers == [("R4", "yes", 1)]
    assert h.channel.replies() == ["→ R4"]
    assert h.tasks("reply")[-1]["title"] == RELAY_TITLE


async def test_a_plain_yes_with_both_open_binds_neither(make: Callable[[], Harness]) -> None:
    h = make()
    _task, approval = await h.ask()
    workers = _with_question(h)
    await h.say("yes")
    assert h.status(approval) == "pending"
    assert workers.answers == []
    [which] = h.channel.replies()
    assert which.startswith(f"Which one? Approval {approval.code} (")
    assert which.endswith(
        f"Reply yes {approval.code} / no {approval.code} for the approval, or R4 yes for the "
        "question."
    )
    await h.say(f"yes {approval.code}")  # the code binds the approval
    assert h.status(approval) == "approved"
    assert workers.answers == []


async def test_a_plain_yes_goes_to_the_approval_when_the_question_takes_options(
    make: Callable[[], Harness],
) -> None:
    # GT-1: one approval, one question whose options have no yes/no: "yes" can't answer it.
    h = make()
    _task, approval = await h.ask()
    workers = FakeWorkers()
    workers.ask("R4", "Which DB?", ("Postgres", "SQLite"), at=h.clock.now)
    h.router.relay.workers = lambda: workers
    await h.say("yes")
    assert h.status(approval) == "approved"
    assert workers.answers == []


async def test_a_plain_yes_still_clashes_when_an_option_is_yes(
    make: Callable[[], Harness],
) -> None:
    h = make()
    _task, approval = await h.ask()
    workers = FakeWorkers()
    workers.ask("R4", "Ship it?", ("Yes, ship it", "Wait"), at=h.clock.now)
    h.router.relay.workers = lambda: workers
    await h.say("yes")
    assert h.status(approval) == "pending"
    [which] = h.channel.replies()
    assert which.startswith("Which one?")


async def test_a_plain_yes_never_goes_to_a_workers_own_check(
    make: Callable[[], Harness],
) -> None:
    # A worker writes its question's options, so it can't steer the owner's "yes" onto its
    # own check: with a check open, the clash reply stands whatever the options.
    h = make()
    _task, approval = await h.ask()
    h.db.execute("UPDATE approvals SET tool = 'trust_check' WHERE id = ?", (approval.id,))
    workers = FakeWorkers()
    workers.ask("R4", "Which DB?", ("Postgres", "SQLite"), at=h.clock.now)
    h.router.relay.workers = lambda: workers
    await h.say("yes")
    assert h.status(approval) == "pending"
    [which] = h.channel.replies()
    assert which.startswith("Which one?")


def test_yes_no_fits() -> None:
    def q(*options: str) -> OpenQuestion:
        return OpenQuestion(1, "R4", "?", options, T0, False)

    assert yes_no_fits(q())
    assert yes_no_fits(q("No.", "later"))
    assert not yes_no_fits(q("a) Postgres", "b) SQLite"))
    assert not plain_reply_clashes([], [q()])


async def test_r4_yes_answers_the_question_while_an_approval_waits(
    make: Callable[[], Harness],
) -> None:
    h = make()
    _task, approval = await h.ask()
    workers = _with_question(h)
    await h.say("R4 yes")
    assert workers.answers == [("R4", "yes", 1)]
    assert h.status(approval) == "pending"
    assert h.channel.replies() == ["→ R4"]


async def test_run_control_and_questions_about_a_run_route_normally(
    rig: Callable[..., Rig],
) -> None:
    r = rig()
    r.workers.ask("R4", "Which DB?")
    assert r.direct("R4 cancel") is None  # even with a question open
    r.workers.run("R6")
    assert r.direct("R6 done yet?") is None
    assert r.direct("R6's PR looks good") is None
    assert r.direct("R6.5 is out") is None
    assert r.direct("R6 yes") == (
        "R6 has no open question (it may have timed out), so nothing was sent. "
        "To tell it something, say: tell R6 ..."
    )
    assert r.direct("tell R6 yes") == "→ R6 (message)"
    assert r.workers.answers == [] and r.workers.messages == [("R6", "yes")]


async def test_commands_never_answer_by_the_short_reply_bias(rig: Callable[..., Rig]) -> None:
    r = rig(None)  # the brain is down
    r.workers.ask("R4", "Which DB?")
    assert await r.free("cancel R4") is None
    assert await r.free("merge R3") is None
    r.workers.ask("R6", "Ship it?")
    assert await r.free("stop R6") is None  # no "which one?" either
    assert r.workers.answers == []


async def test_any_other_message_drops_the_stash(rig: Callable[..., Rig]) -> None:
    r = rig(("R4", 0.4))
    r.workers.ask("R4", "Which DB?")
    r.workers.ask("R6", "Ship it?")
    assert (await r.route("sounds good") or "").startswith("For ")
    r.relay.take_stash(OWNER)  # what the router does for an approval reply or /command
    assert (await r.route("the second one") or "").startswith("For ")  # asks again
    assert r.workers.answers == []  # "second" no longer resolves the old stash


async def test_the_stash_doesnt_answer_a_newer_question(rig: Callable[..., Rig]) -> None:
    r = rig(("R4", 0.4))
    r.workers.ask("R4", "Which DB?", qid=1)
    r.workers.ask("R6", "Ship it?", qid=2)
    await r.route("sounds good")
    r.workers.questions[0] = OpenQuestion(3, "R4", "Which port?", (), T0, False)
    assert await r.route("R4") == "What's your answer for R4 (Which port?)? Reply R4 <answer>."
    assert r.workers.answers == []


async def test_an_option_that_is_a_control_word_is_still_an_answer(
    rig: Callable[..., Rig],
) -> None:
    r = rig(("R4", 0.99))
    r.workers.ask("R4", "Keep going?", ("stop", "continue"))
    assert r.direct("R4 stop") == "→ R4"
    assert r.workers.answers[-1][1] == "stop"
    r.workers.ask("R4", "Which DB?")
    assert await r.free("cancel R4") is None  # a control verb vetoes a confident pick
    r.workers.ask("R6", "Ship it?")
    assert await r.free("stop the docs run") is None


async def test_a_status_question_is_never_an_answer(rig: Callable[..., Rig]) -> None:
    """TR-1: "what's R4 doing" goes to run_activity, even when the brain picks R4."""
    r = rig(("R4", 0.99))
    r.workers.ask("R4", "Which DB?")
    assert await r.free("what's R4 doing") is None
    assert await r.free("status on the trading run") is None
    assert await r.free("is the dori worker stuck?") is None
    assert r.workers.answers == []


async def test_answers_that_mention_progress_are_still_answers(rig: Callable[..., Rig]) -> None:
    r = rig(("R4", 0.99))
    r.workers.ask("R4", "Which DB?")
    assert await r.free("up to you, pick one") == "→ R4"
    r.workers.ask("R4", "Which DB?")
    assert await r.free("doing it with sqlite is fine") == "→ R4"
    r.workers.ask("R4", "Which DB?")
    long = "use sqlite, and whatever is going on in the db is fine, what's the worker doing anyway"
    assert await r.free(long) == "→ R4"

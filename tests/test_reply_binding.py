"""PolicyGate.resolve_reply: binding text replies to approvals (P2-10)."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from resonant.config import IMessageConfig
from resonant.gate import (
    Allow,
    ApprovalReply,
    Deny,
    Intent,
    NeedsApproval,
    PolicyGate,
    ResolveResult,
)
from resonant.gate.policy import NOT_APPROVED
from resonant.gate.types import REPLY_REFUSALS
from resonant.gateway.imessage import IMessageReader
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.gateway.imessage.reader import FUTURE_SKEW
from resonant.loop import NewTask
from resonant.principals import Principals
from resonant.store import approvals as ap
from resonant.store import tasks as repo
from resonant.store.db import kv_set, open_db
from resonant.tools import ToolRegistry
from resonant_sdk import Effect, Event, Level, ToolSpec

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
SENT = NOW + timedelta(seconds=10)  # when prompts go out
REPLY = NOW + timedelta(seconds=20)  # when the owner replies
TTL_S = 1800.0

RESTART = ToolSpec(name="restart_resonant", effect=Effect.WRITE, level=Level.L2)
WIPE = ToolSpec(name="wipe", effect=Effect.WRITE, level=Level.L1)
PAY = ToolSpec(name="pay", effect=Effect.PAY)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def principals() -> Principals:
    return Principals.model_validate(
        {
            "principals": {
                "justin": {"role": "owner"},
                "cofounder": {"grants": {"dori": ["request", "approve"]}},
            }
        }
    )


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    for spec in (RESTART, WIPE, PAY):
        reg.register(spec)
    return reg


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_gate(
    conn: sqlite3.Connection, principals: Principals, registry: ToolRegistry, clock: Clock
) -> PolicyGate:
    return PolicyGate(conn, principals, registry, dry_run=False, approval_ttl_s=TTL_S, clock=clock)


@pytest.fixture
def gate(
    db: sqlite3.Connection, principals: Principals, registry: ToolRegistry, clock: Clock
) -> Iterator[PolicyGate]:
    kv_set(db, "autonomy", "on")
    yield make_gate(db, principals, registry, clock)


def ask(
    db: sqlite3.Connection,
    gate: PolicyGate,
    spec: ToolSpec = RESTART,
    *,
    sent: datetime | None = SENT,
    **args: Any,
) -> NeedsApproval:
    """A waiting task with one pending approval, its prompt sent at ``sent``."""
    tid = repo.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    repo.set_status(db, tid, "running")
    i = Intent.for_tool(spec, task_id=tid, principal="justin", args=args or {"x": 1})
    asked = gate.evaluate(i)
    assert isinstance(asked, NeedsApproval)
    repo.set_status(db, tid, "waiting", wait_reason="approval", wake_at=asked.expires_at)
    if sent is not None:
        ap.add_channel_ref(
            db, asked.approval_id, ref=f"r-{asked.code}", at=sent, principal="justin"
        )
    return asked


def reply(
    gate: PolicyGate,
    decision: Literal["approve", "deny"] = "approve",
    *,
    code: str | None = None,
    event_id: str = "e1",
    at: datetime = REPLY,
    actor: str = "justin",
    channel: str = "imessage",
    path: Literal["parser", "classifier"] = "parser",
    comments: str | None = None,
    all_in_batch: bool = False,
) -> ResolveResult:
    clock = gate.clock
    assert isinstance(clock, Clock)
    clock.now = max(clock.now, at + timedelta(seconds=1))  # processed just after it is sent
    text = ("yes" if decision == "approve" else "no") + (f" {code}" if code else "")
    return gate.resolve_reply(
        ApprovalReply(
            actor=actor,
            actor_identity=f"{channel}:+15551234567",
            channel=channel,
            raw_text=text,
            decision=decision,
            path=path,
            event_id=event_id,
            occurred_at=at,
            code=code,
            comments=comments,
            all_in_batch=all_in_batch,
        )
    )


def status(db: sqlite3.Connection, a: NeedsApproval) -> str:
    row = ap.get_approval(db, a.approval_id)
    assert row is not None
    return row.status


def task_of(db: sqlite3.Connection, a: NeedsApproval) -> str:
    row = ap.get_approval(db, a.approval_id)
    assert row is not None and row.task_id is not None
    return row.task_id


def wakes(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM events WHERE type = 'approval.reply'").fetchall()


def resolve_audits(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT * FROM audit_log WHERE action = 'approval.resolve' ORDER BY id"
    ).fetchall()
    return [{**json.loads(r["detail"]), "decision": r["decision"]} for r in rows]


# --- binding ---------------------------------------------------------------------------


def test_plain_yes_binds_the_single_pending_approval(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    asked = ask(db, gate)
    result = reply(gate)
    assert result.ok and result.reason is None
    assert [(r.approval_id, r.approved) for r in result.resolved] == [(asked.approval_id, True)]
    assert status(db, asked) == "approved"
    task = repo.get_task(db, task_of(db, asked))
    assert task is not None and task.status == "queued"
    assert len(wakes(db)) == 1
    row = ap.get_approval(db, asked.approval_id)
    assert row is not None and row.resolved_event_id == "e1" and row.decided_by == "justin"
    assert resolve_audits(db)[-1]["decision"] == "approved"


def test_plain_no_denies_and_the_denial_holds(db: sqlite3.Connection, gate: PolicyGate) -> None:
    asked = ask(db, gate)
    result = reply(gate, "deny")
    assert result.ok and [r.approved for r in result.resolved] == [False]
    assert status(db, asked) == "rejected"
    tid = task_of(db, asked)
    repo.set_status(db, tid, "running")
    again = gate.evaluate(Intent.for_tool(RESTART, task_id=tid, principal="justin", args={"x": 1}))
    assert again == Deny(NOT_APPROVED)


def test_several_pending_need_the_code(db: sqlite3.Connection, gate: PolicyGate) -> None:
    first = ask(db, gate)
    second = ask(db, gate, x=2)
    plain = reply(gate)
    assert not plain.ok and plain.reason == "ambiguous"
    assert status(db, first) == status(db, second) == "pending"

    lower = reply(gate, code=second.code.lower(), event_id="e2")
    assert lower.ok and [r.approval_id for r in lower.resolved] == [second.approval_id]
    assert status(db, first) == "pending"

    other = "A2" if "A2" not in (first.code, second.code) else "B3"
    unknown = reply(gate, code=other, event_id="e3")
    assert not unknown.ok and unknown.reason == "unknown_code"


@pytest.mark.parametrize("spec", [WIPE, PAY])
def test_require_code(db: sqlite3.Connection, gate: PolicyGate, spec: ToolSpec) -> None:
    asked = ask(db, gate, spec)
    assert asked.require_code
    plain = reply(gate)
    assert not plain.ok and plain.reason == "code_required"
    assert status(db, asked) == "pending"
    coded = reply(gate, code=asked.code, event_id="e2")
    assert coded.ok and coded.resolved[0].approved


def test_require_code_plain_no_denies(db: sqlite3.Connection, gate: PolicyGate) -> None:
    asked = ask(db, gate, WIPE)
    assert reply(gate, "deny").ok
    assert status(db, asked) == "rejected"


def test_require_code_coded_no_denies(db: sqlite3.Connection, gate: PolicyGate) -> None:
    asked = ask(db, gate, WIPE)
    assert reply(gate, "deny", code=asked.code).ok
    assert status(db, asked) == "rejected"


# --- the sent-before rule --------------------------------------------------------------


@pytest.mark.parametrize("with_code", [False, True])
@pytest.mark.parametrize(
    "sent",
    [REPLY + timedelta(seconds=1), REPLY, None],
    ids=["sent-after", "sent-same-instant", "never-sent"],
)
def test_reply_never_binds_a_prompt_sent_after_it(
    db: sqlite3.Connection, gate: PolicyGate, sent: datetime | None, with_code: bool
) -> None:
    asked = ask(db, gate, sent=sent)
    result = reply(gate, code=asked.code if with_code else None)
    assert not result.ok and result.reason == "not_sent_before"
    assert status(db, asked) == "pending"
    assert wakes(db) == []


def test_a_yes_from_a_fast_phone_clock_never_binds_a_later_prompt(
    db: sqlite3.Connection, gate: PolicyGate, clock: Clock, tmp_path: Path
) -> None:
    """The owner's phone runs 10 minutes fast. A "yes" sent (and read) before the prompt
    goes out carries a date after it; the reader clamps it to its read time, so the
    sent-before rule still refuses it."""
    owner = "+15551234567"
    inbound = Principals.model_validate(
        {"principals": {"justin": {"role": "owner", "identities": [f"imessage:{owner}"]}}}
    )
    chat = FakeChatDB(tmp_path / "chat.db")
    try:
        config = IMessageConfig(self_handle="resonant@example.com", db_path=chat.path)
        reader = IMessageReader(config, inbound, db, clock)
        events: list[Event] = []

        def submit(event: Event) -> bool:
            events.append(event)
            return True

        reader.poll_once(submit)  # first start: the cursor goes to the end
        read_at = SENT - timedelta(seconds=5)
        clock.now = read_at
        chat.add_message(handle=owner, text="yes", at=read_at + timedelta(minutes=10))
        assert reader.poll_once(submit) == 1
    finally:
        chat.close()
    [event] = events
    assert event.occurred_at == read_at + FUTURE_SKEW < SENT  # unclamped it'd be after SENT
    asked = ask(db, gate, sent=SENT)
    result = reply(gate, at=event.occurred_at)
    assert not result.ok and result.reason == "not_sent_before"
    assert status(db, asked) == "pending"
    assert wakes(db) == []


def test_sent_before_is_per_actor(db: sqlite3.Connection, gate: PolicyGate) -> None:
    asked = ask(db, gate, sent=None)
    ap.add_channel_ref(db, asked.approval_id, ref="r", at=SENT, principal="someone-else")
    result = reply(gate)
    assert not result.ok and result.reason == "not_sent_before"


def test_a_prompt_sent_after_the_reply_still_makes_it_ambiguous(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    ask(db, gate)
    ask(db, gate, sent=REPLY + timedelta(seconds=5), x=2)
    result = reply(gate)
    assert not result.ok and result.reason == "ambiguous"


def test_an_old_plain_reply_rerouted_later_stays_ambiguous(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    first = ask(db, gate)
    second = ask(db, gate, x=2)
    assert reply(gate, event_id="e1").reason == "ambiguous"
    later = REPLY + timedelta(seconds=30)
    assert reply(gate, code=first.code, event_id="e2", at=later).ok
    # Only `second` is pending now, but two were when e1 was sent.
    again = reply(gate, event_id="e1")
    assert not again.ok and again.reason == "ambiguous"
    assert status(db, second) == "pending"


# --- idempotency and races -------------------------------------------------------------


def test_same_event_twice_returns_the_original_result(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    asked = ask(db, gate)
    first = reply(gate, comments="after 5pm")
    tid = task_of(db, asked)
    repo.set_status(db, tid, "waiting", wait_reason="approval")  # as if it waited again
    second = reply(gate, comments="after 5pm")
    assert first == second and first.ok
    assert len(wakes(db)) == 1
    task = repo.get_task(db, tid)
    assert task is not None and task.status == "waiting"  # not requeued again
    comments = db.execute("SELECT * FROM events WHERE type = 'human_input'").fetchall()
    assert len(comments) == 1
    assert json.loads(comments[0]["payload"]) == {
        "approval_id": asked.approval_id,
        "comments": "after 5pm",
    }
    assert len(resolve_audits(db)) == 1


def test_replayed_event_from_another_actor_reveals_nothing(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    ask(db, gate)
    assert reply(gate).ok
    other = reply(gate, actor="cofounder")
    assert other == ResolveResult(ok=False, reason="nothing_pending")


def test_two_replies_racing_on_two_connections(
    db: sqlite3.Connection,
    gate: PolicyGate,
    tmp_path: Path,
    principals: Principals,
    registry: ToolRegistry,
) -> None:
    asked = ask(db, gate)
    other_conn = open_db(tmp_path / "test.db")
    try:
        other = make_gate(other_conn, principals, registry, Clock())
        start = threading.Barrier(2)
        results: dict[str, ResolveResult] = {}

        def run(g: PolicyGate, event_id: str) -> None:
            start.wait()
            results[event_id] = reply(g, event_id=event_id)

        threads = [
            threading.Thread(target=run, args=(gate, "e1")),
            threading.Thread(target=run, args=(other, "e2")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        other_conn.close()
    assert sorted(r.ok for r in results.values()) == [False, True]
    assert status(db, asked) == "approved"
    assert len(wakes(db)) == 1


def test_the_conditional_update_decides_a_race(
    db: sqlite3.Connection,
    gate: PolicyGate,
    tmp_path: Path,
    principals: Principals,
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loser read the approval as pending before the winner committed."""
    asked = ask(db, gate)
    stale = ap.pending_for(db, "justin", NOW)
    assert reply(gate, event_id="e1").ok
    other_conn = open_db(tmp_path / "test.db")
    try:
        other = make_gate(other_conn, principals, registry, Clock())

        def stale_pending(
            conn: sqlite3.Connection, principal: str, now: datetime
        ) -> list[ap.Approval]:
            return stale

        monkeypatch.setattr(ap, "pending_for", stale_pending)
        lost = reply(other, event_id="e2")
    finally:
        other_conn.close()
    assert not lost.ok and lost.reason == "already_resolved"
    assert status(db, asked) == "approved" and len(wakes(db)) == 1


# --- refusals --------------------------------------------------------------------------


def test_expired_approval_binds_nothing(
    db: sqlite3.Connection, gate: PolicyGate, clock: Clock
) -> None:
    asked = ask(db, gate)
    clock.now = asked.expires_at
    assert reply(gate).reason == "nothing_pending"
    assert reply(gate, code=asked.code, event_id="e2").reason == "nothing_pending"
    assert status(db, asked) == "expired"


def test_task_not_waiting(db: sqlite3.Connection, gate: PolicyGate) -> None:
    asked = ask(db, gate)
    repo.set_status(db, task_of(db, asked), "queued")
    result = reply(gate)
    assert not result.ok and result.reason == "task_not_waiting"
    assert status(db, asked) == "pending" and wakes(db) == []


@pytest.mark.parametrize("actor", ["cofounder", "stranger"])
def test_non_approvers_get_the_generic_answer(
    db: sqlite3.Connection, gate: PolicyGate, actor: str
) -> None:
    asked = ask(db, gate)
    ap.add_channel_ref(db, asked.approval_id, ref="r", at=SENT, principal=actor)
    generic = ResolveResult(ok=False, reason="nothing_pending")
    assert reply(gate, actor=actor) == generic
    assert reply(gate, actor=actor, code=asked.code, event_id="e2") == generic
    assert reply(gate, "deny", actor=actor, event_id="e3") == generic
    assert status(db, asked) == "pending"


@pytest.mark.parametrize(
    "kw",
    [{"channel": "sms"}, {"channel": "slack"}, {"path": "classifier"}, {"all_in_batch": True}],
)
def test_unsupported_replies(db: sqlite3.Connection, gate: PolicyGate, kw: dict[str, Any]) -> None:
    asked = ask(db, gate)
    result = reply(gate, **kw)
    assert not result.ok and result.reason == "unsupported"
    assert status(db, asked) == "pending"


def test_refusals_are_audited_with_the_reply(db: sqlite3.Connection, gate: PolicyGate) -> None:
    ask(db, gate)
    ask(db, gate, x=2)
    reply(gate)
    row = resolve_audits(db)[-1]
    assert row["decision"] == "refused" and row["reason"] == "ambiguous"
    assert row["raw_text"] == "yes" and row["event_id"] == "e1" and row["path"] == "parser"
    assert row["reason"] in REPLY_REFUSALS


def test_occurred_at_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ApprovalReply(
            "justin",
            "imessage:+1",
            "imessage",
            "yes",
            "approve",
            "parser",
            "e1",
            datetime(2026, 10, 13, 15, 0),
        )


def test_approved_reply_then_evaluate_allows(db: sqlite3.Connection, gate: PolicyGate) -> None:
    asked = ask(db, gate)
    assert reply(gate).ok
    tid = task_of(db, asked)
    repo.set_status(db, tid, "running")
    allow = gate.evaluate(Intent.for_tool(RESTART, task_id=tid, principal="justin", args={"x": 1}))
    assert isinstance(allow, Allow) and allow.approval_id == asked.approval_id

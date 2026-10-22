"""PER-261: approvals polish, at the gate, the store and the runner's batch step.

The expiry reminder (``runners.approve.remind_due``), a change past the side-effect cap
joining the batch, the code after a Claude answer (``store.outbound``), the turn's answer
before its batch prompt, and ``decision="partial"`` on a ``yes 1,3``. The real PolicyGate,
Executor and LocalRunner over a fake channel, as in ``test_batch_approvals.py``; the daemon
flows are in ``test_e2e_batch.py``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_batch_approvals import CLOSE, MEMBER, OWNER, TTL_S, Ctx, H, principals
from test_local_runner_gated import Clock, FakeChannel

from resonant.brain.history import EARLY_ANSWER, replayed
from resonant.e2e.fakes import ScriptedModel
from resonant.executor import Executor, HandlerKey, ToolHandler
from resonant.gate import Intent, NeedsApproval, PolicyGate
from resonant.gateway.channel import ChannelRefused, ChannelSendError, approval_dedupe_key
from resonant.loop.types import Wait
from resonant.models.client import null_spans
from resonant.router.approvals import refusal_reply
from resonant.runners.approve import remind_due, reminder_text
from resonant.runners.local import LocalRunner
from resonant.store import approvals as ap
from resonant.store import outbound
from resonant.store.db import kv_set, transaction
from resonant.store.outbound import reminder_key
from resonant.toolcore import MAX_SIDE_EFFECTS, Asked, BatchItem, CallScope, Ran, Refused
from resonant.tools import ToolRegistry
from resonant_sdk import Effect, Level, ToolSpec

TOUCH = ToolSpec(  # an L3 change: runs without asking, then reports
    name="touch_ticket",
    description="Mark a ticket seen.",
    args_schema={"type": "object", "properties": {"id": {"type": "string"}}},
    effect=Effect.WRITE,
    level=Level.L3,
)
RAN = [f'Done: touch ticket id="{n}"' for n in range(MAX_SIDE_EFFECTS)]


@pytest.fixture
def h(db: sqlite3.Connection) -> H:
    clock = Clock(datetime(2026, 10, 19, 15, 0, tzinfo=UTC))
    registry = ToolRegistry()
    for spec in (CLOSE, TOUCH):
        registry.register(spec)
    ran: list[str] = []

    def handler(name: str) -> ToolHandler:
        async def run(args: dict[str, Any]) -> dict[str, Any]:
            ran.append(f"{name}:{args.get('id')}")
            return {"id": args.get("id")}

        return run

    handlers: dict[HandlerKey, ToolHandler] = {
        (None, "close_ticket"): handler("close"),
        (None, "touch_ticket"): handler("touch"),
    }
    with transaction(db):
        kv_set(db, "autonomy", "on")
        kv_set(db, "paused", False)
    gate = PolicyGate(db, principals(), registry, dry_run=False, approval_ttl_s=TTL_S,
                      approvers_for=lambda _e: [OWNER, MEMBER], clock=clock,
                      batch_max=3)  # fmt: skip
    executor = Executor(db, gate, handlers)
    channel = FakeChannel()
    runner = LocalRunner(db, model=ScriptedModel(), channel=channel, gate=gate, executor=executor,
                         registry=registry, now=clock)  # fmt: skip
    return H(db, clock, gate, runner, channel, ran, set(), set())


def scope(
    h: H, task_id: str, i: int, *, effects: list[str], batch: list[BatchItem] | None
) -> CallScope:
    task = h.task(task_id)
    return CallScope(task.id, OWNER, task.trace_id, task.step_no, i, "inferred",
                     checkpoint=h.task(task_id).checkpoint, steps=Ctx(h.db, task_id),
                     effects=effects, batch=batch)  # fmt: skip


def evaluated(h: H) -> list[dict[str, Any]]:
    rows = h.db.execute(
        "SELECT decision, tool, detail FROM audit_log WHERE action = 'gate.evaluate' ORDER BY id"
    ).fetchall()
    return [{"decision": r["decision"], "tool": r["tool"], **json.loads(r["detail"])}
            for r in rows]  # fmt: skip


async def lone(h: H) -> tuple[str, ap.Approval]:
    """A task waiting on one prompted approval (an L2 close)."""
    task_id = h.new_task()
    items = await h.propose(task_id, [(CLOSE, "A")])
    out = await h.close(task_id, items)
    assert isinstance(out, Wait)
    approval = ap.get_approval(h.db, items[0].approval_id)
    assert approval is not None and approval.code is not None
    return task_id, approval


async def batch_of(h: H, keys: tuple[str, ...] = ("A", "B", "C")) -> tuple[str, list[str]]:
    task_id = h.new_task()
    items = await h.propose(task_id, [(CLOSE, k) for k in keys])
    assert isinstance(await h.close(task_id, items), Wait)
    return task_id, [i.approval_id for i in items]


# --- capped changes join the batch ------------------------------------------------------------


async def test_a_change_past_the_cap_asks_and_joins_the_batch(h: H) -> None:
    task_id = h.new_task()
    under = await h.runner.core.call(TOUCH, {"id": "T0"}, scope(h, task_id, 0, effects=[],
                                                                  batch=[]))  # fmt: skip
    assert isinstance(under, Ran)  # under the cap an L3 change runs and reports
    batch: list[BatchItem] = []
    sixth = await h.runner.core.call(TOUCH, {"id": "RES-6"}, scope(h, task_id, 1, effects=RAN,
                                                                  batch=batch))  # fmt: skip
    assert isinstance(sixth, Asked) and sixth.item is not None, sixth
    batch.append(sixth.item)
    asked = await h.runner.core.call(CLOSE, {"id": "B"}, scope(h, task_id, 2, effects=RAN,
                                                                 batch=batch))  # fmt: skip
    assert isinstance(asked, Asked) and asked.item is not None
    batch.append(asked.item)
    assert h.ran == ["touch:T0"]
    last = evaluated(h)[-2]
    assert (last["decision"], last["capped"], last["tool"]) == (
        "NeedsApproval",
        True,
        "touch_ticket",
    )
    approval = ap.get_approval(h.db, sixth.item.approval_id)
    assert approval is not None and not approval.allow_always  # a capped ask offers no always
    # one prompt for both, numbered as the batch's changes
    out = await h.close(task_id, batch)
    assert isinstance(out, Wait)
    (_, prompt) = h.channel.prompts[-1]
    assert '1. touch ticket id="RES-6"' in prompt and '2. close ticket id="B"' in prompt
    h.clock.advance(1)
    assert h.reply(f"yes 1 {h.code()}", code=h.code(), picks=(1,)).ok
    await h.step(task_id)
    assert h.ran == ["touch:T0", "touch:RES-6"]  # approved, it runs once; the other was dropped


async def test_without_a_batch_the_cap_still_refuses(h: H) -> None:
    task_id = h.new_task()
    out = await h.runner.core.call(TOUCH, {"id": "RES-6"}, scope(h, task_id, 0, effects=RAN,
                                                                batch=None))  # fmt: skip
    assert isinstance(out, Refused) and out.why == "capped" and not out.gated
    assert h.ran == [] and ap.pending_all(h.db, h.clock()) == []


async def test_a_capped_change_still_respects_batch_max(h: H) -> None:
    task_id = h.new_task()
    batch = await h.propose(task_id, [(CLOSE, k) for k in ("A", "B", "C")])  # batch_max is 3
    out = await h.runner.core.call(TOUCH, {"id": "RES-6"}, scope(h, task_id, 3, effects=RAN,
                                                                batch=batch))  # fmt: skip
    assert isinstance(out, Refused) and out.why == "capped" and out.gated
    assert out.text == "Not done: at most 3 changes can wait on one yes."
    statuses = [a.status for a in ap.pending_all(h.db, h.clock())]
    assert statuses == ["pending"] * 3  # the capped change's approval was dropped


def test_the_gate_turns_only_an_allow_into_an_ask(h: H) -> None:
    task_id = h.new_task()
    touch = Intent.for_tool(TOUCH, task_id=task_id, principal=OWNER, args={"id": "X"})
    assert isinstance(h.gate.evaluate(touch, capped=True), NeedsApproval)
    read = ToolSpec(name="peek", effect=Effect.READ, level=Level.L0)
    h.gate.registry.register(read)
    peek = Intent.for_tool(read, task_id=task_id, principal=OWNER, args={})
    assert type(h.gate.evaluate(peek, capped=True)).__name__ == "Allow"  # a read still runs


# --- the expiry reminder -----------------------------------------------------------------------


def reminders(h: H) -> list[tuple[str, str, str | None]]:
    return [s for s in h.channel.sent if s[2] is not None and ":remind:" in s[2]]


async def test_a_lone_approval_is_reminded_once_at_half_its_ttl(h: H) -> None:
    task_id, needs = await lone(h)
    h.clock.advance(TTL_S / 2 - 60)
    assert await remind_due(h.db, h.channel, h.clock()) == 0  # not yet
    h.clock.advance(60)
    assert await remind_due(h.db, h.channel, h.clock()) == 1
    expected = f"[{needs.code}] still waiting, expires in 15 min"
    assert [(p, t) for p, t, _ in reminders(h)] == [(OWNER, expected)]  # who got the prompt
    h.clock.advance(120)
    assert await remind_due(h.db, h.channel, h.clock()) == 0  # once
    a = ap.get_approval(h.db, needs.id)
    assert a is not None and a.reminded_at is not None
    ref = reminder_key(needs.id, OWNER)
    row = h.db.execute("SELECT * FROM system_texts WHERE ref = ?", (ref,)).fetchone()
    assert row is not None and (row["kind"], row["task_id"], row["outside"]) == (
        "approval", task_id, 0,
    )  # fmt: skip
    (audited,) = h.db.execute(
        "SELECT detail FROM audit_log WHERE action = 'approval.reminded'"
    ).fetchall()
    assert json.loads(audited["detail"])["approval_ids"] == [needs.id]


async def test_a_batch_gets_one_reminder_timed_from_when_it_was_opened(h: H) -> None:
    task_id = h.new_task()
    items = await h.propose(task_id, [(CLOSE, k) for k in ("A", "B")])
    h.clock.advance(600)  # the turn went on for 10 minutes before it asked
    assert isinstance(await h.close(task_id, items), Wait)
    h.clock.advance(TTL_S / 2 - 1)
    assert await remind_due(h.db, h.channel, h.clock()) == 0
    h.clock.advance(1)
    assert await remind_due(h.db, h.channel, h.clock()) == 1
    assert [(p, t) for p, t, _ in reminders(h)] == [
        (OWNER, f"[{h.code()}] still waiting, expires in 15 min")
    ]  # the batch's code, to the owner only
    rows = [ap.get_approval(h.db, i.approval_id) for i in items]
    assert all(r is not None and r.reminded_at is not None for r in rows)


async def test_no_reminder_unprompted_answered_or_nearly_expired(h: H) -> None:
    task_id = h.new_task()
    await h.propose(task_id, [(CLOSE, "P")])  # pinned in a turn that hasn't asked yet
    _, needs = await lone(h)
    h.clock.advance(1)
    assert h.reply("no", code=needs.code, decision="deny").ok
    _, late = await lone(h)
    h.clock.advance(TTL_S - 30)  # half the TTL passed long ago, but under a minute is left
    assert await remind_due(h.db, h.channel, h.clock()) == 0
    assert reminders(h) == []
    assert late.code


async def test_a_failed_reminder_is_tried_again_and_a_refused_one_settles(h: H) -> None:
    _, needs = await lone(h)
    h.clock.advance(TTL_S / 2)
    h.channel.fail_send.append(ChannelSendError("Messages is not running"))
    assert await remind_due(h.db, h.channel, h.clock()) == 0
    a = ap.get_approval(h.db, needs.id)
    assert a is not None and a.reminded_at is None
    h.clock.advance(60)
    assert await remind_due(h.db, h.channel, h.clock()) == 1  # the next run sends it
    assert len(reminders(h)) == 1
    _, other = await lone(h)
    h.clock.advance(TTL_S / 2)
    h.channel.fail_send.append(ChannelRefused("no identity"))
    assert await remind_due(h.db, h.channel, h.clock()) == 0
    b = ap.get_approval(h.db, other.id)
    assert b is not None and b.reminded_at is not None  # unreachable for good: settled


def test_reminder_text() -> None:
    assert reminder_text("A7", timedelta(minutes=15)) == "[A7] still waiting, expires in 15 min"
    assert reminder_text("A7", timedelta(seconds=20)) == "[A7] still waiting, expires in 1 min"


# --- the code after a Claude answer ------------------------------------------------------------


async def test_a_bare_yes_right_after_a_claude_answer_needs_the_code(h: H) -> None:
    task_id, needs = await lone(h)
    h.clock.advance(5)
    outbound.note(h.db, OWNER, h.clock(), "ask:t1")  # a Claude answer: its key marks it
    refused = h.reply("yes")
    assert (refused.ok, refused.reason) == (False, "code_required")
    sent = [p for p in h.gate.pending_for(OWNER)]
    assert refusal_reply(refused.reason, None, sent) == f"Reply yes {needs.code} to confirm."
    assert ap.get_approval(h.db, needs.id).status == "pending"  # type: ignore[union-attr]
    assert h.reply(f"yes {needs.code}", code=needs.code, event="e2").ok
    await h.step(task_id)
    assert h.ran == ["close:A"]


def test_only_the_ask_runners_answers_are_claude_sends() -> None:
    """PER-256: no visible prefix; the dedupe key marks a Claude answer. The ack and every
    other text are not; the ask runner's other texts (limit notices) fail closed."""
    assert outbound.is_claude_send("ask:t1")
    assert outbound.is_claude_send("ask:limit:2026-10-22T22:00:00+00:00")  # fails closed
    assert outbound.is_claude_send("ask:down:t1")  # Claude-down and cap notices count too
    assert outbound.is_claude_send("ask:cap:2026-10-22")
    assert not outbound.is_claude_send("ask:ack:t1")
    assert not outbound.is_claude_send("reply:t1")
    assert not outbound.is_claude_send("approval:a7:prompt:justin")
    assert not outbound.is_claude_send(None)


async def test_an_ack_does_not_make_a_bare_yes_need_the_code(h: H) -> None:
    _, needs = await lone(h)
    outbound.note(h.db, OWNER, h.clock(), approval_dedupe_key(needs.id, OWNER))
    h.clock.advance(5)
    outbound.note(h.db, OWNER, h.clock(), "ask:ack:t1")  # "On it": not Claude's answer
    assert h.reply("yes").ok


async def test_a_later_text_keeps_it_until_the_prompt_is_shown_again(h: H) -> None:
    _, needs = await lone(h)
    outbound.note(h.db, OWNER, h.clock(), approval_dedupe_key(needs.id, OWNER))
    h.clock.advance(5)
    outbound.note(h.db, OWNER, h.clock(), "ask:t1")
    h.clock.advance(5)
    outbound.note(h.db, OWNER, h.clock(), "ask:ack:t1")  # an ack after the answer
    assert h.reply("yes").reason == "code_required"  # Claude's text still came after the prompt
    h.clock.advance(5)
    outbound.note(h.db, OWNER, h.clock(), reminder_key(needs.id, OWNER))
    assert h.reply("yes", event="e2").ok  # the reminder showed the prompt again
    _, other = await lone(h)
    h.clock.advance(5)
    outbound.note(h.db, OWNER, h.clock(), "ask:t2")
    assert h.reply("no", decision="deny", event="e3").ok
    assert other.code


async def test_a_yes_sent_before_the_claude_answer_still_binds(h: H) -> None:
    _, _needs = await lone(h)
    later = h.clock.now + timedelta(seconds=30)
    outbound.note(h.db, OWNER, later, "ask:t1")
    assert h.reply("yes").ok  # judged by what had been sent when the reply was


async def test_yes_some_right_after_a_claude_answer_needs_the_code(h: H) -> None:
    task_id, _ = await batch_of(h)
    h.clock.advance(5)
    outbound.note(h.db, OWNER, h.clock(), "ask:t1")
    refused = h.reply("yes 1,3", picks=(1, 3))
    assert refused.reason == "code_required"
    code = h.code()
    assert refusal_reply(refused.reason, None, h.gate.pending_for(OWNER), (1, 3)) == (
        f"Reply yes 1,3 {code} to confirm."
    )
    assert h.reply(f"yes 1,3 {code}", code=code, picks=(1, 3), event="e2").ok
    await h.step(task_id)
    assert h.ran == ["close:A", "close:C"]


def test_outbound_fails_closed_when_the_prompt_fell_out_of_the_window(
    db: sqlite3.Connection,
) -> None:
    t0 = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
    later = t0 + timedelta(hours=1)
    outbound.note(db, OWNER, t0, "approval:a7:prompt:justin")
    outbound.note(db, OWNER, t0 + timedelta(seconds=1), "ask:t1")
    for n in range(1, outbound.KEEP + 3):
        outbound.note(db, OWNER, t0 + timedelta(minutes=n), f"reply:{n}")
    kept = db.execute("SELECT value FROM kv WHERE key = ?", (outbound.KV_PREFIX + OWNER,))
    assert len(json.loads(kept.fetchone()[0])) == outbound.KEEP
    # both the prompt and Claude's answer were pushed out: the last Claude answer still counts
    assert outbound.claude_since(db, OWNER, {"approval:a7:prompt:justin"}, later) is True
    outbound.note(db, OWNER, later, "approval:a7:remind:justin")
    assert outbound.claude_since(db, OWNER, {"approval:a7:remind:justin"}, later) is False
    assert outbound.claude_since(db, "nobody", set(), later) is False


# --- answers before batches, and the partial decision ------------------------------------------


async def test_the_answer_goes_out_before_the_batch_prompt_and_stays_in_history(h: H) -> None:
    task_id = h.new_task()
    items = await h.propose(task_id, [(CLOSE, "A"), (CLOSE, "B")])
    task = h.task(task_id)
    out = await h.runner.close_batch(task, OWNER, items, [], Ctx(h.db, task_id), null_spans,
                                     answer="RES-12 is blocked on review.")  # fmt: skip
    h.settle(task_id, out)
    assert isinstance(out, Wait)
    texts = [t for _, t, _ in h.channel.sent]
    assert texts[0] == "RES-12 is blocked on review."
    assert texts[1].startswith(f"[{h.code()}] OK to make these 2 changes?")
    assert h.task(task_id).checkpoint[EARLY_ANSWER] == "RES-12 is blocked on review."
    h.clock.advance(1)
    await h.step(task_id)  # woken before an answer (a re-prompt): nothing is sent twice
    assert [t for _, t, _ in h.channel.sent] == texts
    history = replayed(h.db, OWNER, before_event_id=None)
    said = [m["content"] for m in history.messages if m["role"] == "assistant"]
    assert said == ["RES-12 is blocked on review."]
    assert h.reply("yes").ok
    await h.step(task_id)
    history = replayed(h.db, OWNER, before_event_id=None)
    said = [m["content"] for m in history.messages if m["role"] == "assistant"]
    # PER-270: the early answer and the batch's result are separate turns of the thread
    assert said == [
        "RES-12 is blocked on review.",
        'Done: close ticket id="A" / Done: close ticket id="B"',
    ]


async def test_a_partial_yes_is_audited_as_partial(h: H) -> None:
    await batch_of(h)
    h.clock.advance(1)
    code = h.code()
    assert h.reply(f"yes 1 {code}", code=code, picks=(1,)).ok
    (row,) = h.db.execute(
        "SELECT decision FROM audit_log WHERE action = 'approval.resolve'"
    ).fetchall()
    assert row["decision"] == "partial"

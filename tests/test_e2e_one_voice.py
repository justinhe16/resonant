"""PER-256: qwen only routes, Claude is the one brain, end to end.

The same in-process daemon as ``test_e2e_claude_session.py`` (a fake ``claude`` session over
the real MCP relay, bridge, gate and executor; a ScriptedModel that only answers triage). Here:
the canned replies when Claude can't answer (down, limit, off, a member), the one deferred ack,
small talk and news answered from code, and the 2026-10-22 replays: a reply the guards stop
is never sent, the turn is redone once with the reason, and a second failure sends a short
honest fallback.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from test_e2e_claude_session import (
    ACK,
    CLAUDE,
    MEMBER_PHONE,
    OWNER_PHONE,
    World,
    chat,
    start,
)

from resonant.brain import guard, triage
from resonant.brain.news import NEWS_NOTHING
from resonant.config import ClaudeSessionConfig
from resonant.e2e import wait_until
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.runners.ask import (
    LIMIT_OWNER,
    UNAVAILABLE_REPLY,
    UNAVAILABLE_RETRY,
    clock_text,
)
from resonant.runners.ask_tools import McpServer
from resonant.runners.brain import MEMBER_REPLY
from resonant.runners.claude import usage_limit
from resonant.runners.claude_session import LIMIT_KNOWN_KV, LIMIT_KV, ClaudeSession
from resonant.store import trust as trust_store
from resonant.store.db import iso, kv_get, kv_set, utcnow
from resonant.tools import notes as notes_tools

__all__ = ["chat"]

GREETING = {"handler": "local", "kind": "general", "message_type": "greeting", "say": ""}
WATCH: dict[str, Any] = {
    "what": "notes change",
    "tool": "list_notes",
    "args": {},
    "when": "changed",
}
REDO = "was not sent"  # in the last line of every redo message (``guard.*_REDO``)


async def prompt_recorded(w: World) -> None:
    """The approval prompt's send is recorded (its channel refs are saved)."""

    def recorded() -> bool:
        rows = w.rows("SELECT channel_refs FROM approvals WHERE status = 'pending'")
        return bool(rows) and all(json.loads(r["channel_refs"]) for r in rows)

    await wait_until(recorded, 10)
    await asyncio.sleep(0.01)


async def ask_claude(w: World, text: str, **plan: Any) -> list[str]:
    """One text triage hands to Claude, with the plan its session acts out; the texts it
    causes, once its task has settled."""
    w.model.triage = CLAUDE
    w.claude(**plan)
    n = len(w.osa.sent)
    w.chat.add_message(handle=OWNER_PHONE, text=text)
    await w.osa.wait_for(n + 1, 20)
    await w.settled()
    return [m.text for m in w.osa.sent[n:]]


# --- Claude can't answer: canned replies, never the local model ------------------------------


async def test_ask_claude_off_says_unavailable(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path, ask={"enabled": False}) as w:
        w.model.triage = CLAUDE
        assert await w.say("why is R4 slow?") == [UNAVAILABLE_REPLY]
        assert w.ask_tasks() == [] and w.model.calls["triage"] == 1
        assert w.model.calls.get("answer", 0) == 0
        # slash commands still work
        assert "Resonant up" in (await w.say("/status"))[0]


async def test_a_member_gets_small_talk_or_a_canned_no(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        assert await w.say("what is the status of R4", phone=MEMBER_PHONE) == [MEMBER_REPLY]
        w.model.triage = GREETING
        assert await w.say("hi", phone=MEMBER_PHONE) == ["Hey!"]
        assert w.ask_tasks() == []


async def test_claude_down_says_so_retries_once_and_never_answers_itself(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path, session={"retry_after_s": 0.2}) as w:
        w.model.triage = CLAUDE
        w.claude(crash="always", answer="never")
        (first,) = await w.say("why is R4 slow?")
        assert first == UNAVAILABLE_RETRY
        assert await w.more(1) == [UNAVAILABLE_REPLY]
        await w.settled()
        (ask,) = w.ask_tasks()
        assert ask["status"] == "done" and json.loads(ask["checkpoint"])["retried"] is True
        assert w.sends_with("ask:down:") == [f"ask:down:{ask['id']}"]  # told once
        assert w.model.calls.get("answer", 0) == 0


async def test_a_retry_that_works_sends_only_the_answer_after_the_notice(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(
        resonant_home, chat, tmp_path, session={"retry_after_s": 0.2, "turn_timeout_s": 2}
    ) as w:
        w.model.triage = CLAUDE
        w.claude(turns=[{"when": "slow", "hang": True}], answer="ok")
        (first,) = await w.say("why is R4 slow?")
        assert first == UNAVAILABLE_RETRY
        # the second try hangs too; now the plan answers, and the next text is served
        w.claude(answer="back")
        assert await w.more(1) == ["back"]


async def test_the_usage_limit_is_said_once_then_short_and_approvals_still_work(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    reset = utcnow().timestamp() + 3 * 3600
    async with start(resonant_home, chat, tmp_path) as w:
        (first,) = await ask_claude(w, "why is R4 slow?", limit=int(reset))
        until = datetime.fromtimestamp(int(reset) + 60, UTC)
        when = clock_text(until, ZoneInfo(w.daemon.settings.timezone), utcnow())
        assert first == LIMIT_OWNER.format(until=when)
        assert kv_get(w.conn, "brain.limit_told") == iso(until)
        (second,) = await w.say("and R5?")
        assert second == f"Still at my Claude limit until {when}."
        assert await w.say("anything else?") == [second]
        assert w.model.calls.get("answer", 0) == 0 and len(w.ask_tasks()) == 1
        # code-built answers go on: a slash command, an approval reply
        assert "Claude brain" in (await w.say("/status"))[0]


# --- the one deferred ack ----------------------------------------------------------------------


async def test_a_fast_answer_arrives_alone_and_a_slow_one_gets_one_ack_first(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path, session={"ack_after_s": 0.3}) as w:
        w.model.triage = CLAUDE
        w.claude(answer="quick")
        assert await w.say("why is R4 slow?") == ["quick"]
        await w.settled()
        assert w.sends_with("ask:ack:") == []
        w.claude(delay=1.5, answer="slow one")
        ack, answer = await w.say("and why is R5 slow?", replies=2)
        assert ack == ACK and answer == "slow one"
        await w.settled()
        assert len(w.sends_with("ask:ack:")) == 1 and len(w.osa.sent) == 3


async def test_the_ack_is_not_an_answer_for_the_approval_rule(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    """PER-261: only Claude's answer sent after the prompt makes a bare yes need the code.
    An ack that follows a prompt doesn't."""
    async with start(resonant_home, chat, tmp_path, session={"ack_after_s": 0.0}) as w:
        w.model.triage = CLAUDE
        w.claude(delay=0.5, calls=[{"name": "create_watch", "args": WATCH}], answer="Asked you.")
        ack, answer, prompt = await w.say("watch my notes for changes", replies=3)
        assert ack == ACK and answer.startswith("Asked you.") and "Reply yes" in prompt
        await prompt_recorded(w)
        (done,) = await w.say("yes")  # the ack went out before the prompt: nothing to refuse
        assert done.startswith("Watching W1")
        assert len(w.executions("create_watch")) == 1


# --- small talk and news: from code ------------------------------------------------------------


def test_decide_is_pure_and_defaults_to_claude() -> None:
    hi = triage.Triage("local", "general", "", "greeting")
    news = triage.Triage("local", "general", "", "news_check")
    other = triage.Triage("claude", "general", "", "other")
    assert triage.decide("Hi", hi) == "small_talk"
    assert triage.decide("anything new?", news) == "news"
    assert triage.decide("why is R4 slow?", other) == "claude"
    assert triage.decide("Hi", None) == "claude"  # a bad or late triage is Claude's
    assert triage.decide("Hi", hi, "claude") == "claude"  # brain.route: claude
    # a link, a hard shape, a run question or a long message is Claude's whatever it said
    assert triage.decide("hi https://example.com", hi) == "claude"
    assert triage.decide("why does it fail", hi) == "claude"
    assert triage.decide("ok merge it", hi) == "claude"
    assert triage.decide("thanks, R4 passed?", hi) == "claude"
    assert triage.decide(
        "what is new on the whole of the resonant project this week " * 3, news
    ) == ("claude")


def test_a_small_talk_reply_is_one_clean_line_or_a_canned_one() -> None:
    assert triage.clean_small_talk("Hey there!", "greeting") == "Hey there!"
    for bad in (
        "Hi! You have 3 runs going.",  # a digit
        "Hi, how are you?",  # a question
        "Hi! Everything is running fine.",  # a state word
        "Added a note.",  # a claim
        "x" * 100,  # too long
        "",
    ):
        assert triage.clean_small_talk(bad, "greeting") == triage.CANNED_SMALL_TALK["greeting"]
    assert triage.clean_small_talk("whatever 7", "ack") == triage.CANNED_SMALL_TALK["ack"]


async def test_small_talk_and_news_never_reach_claude(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = {**GREETING, "say": "Hey, good morning!"}
        assert await w.say("good morning") == ["Hey, good morning!"]
        w.model.triage = {**GREETING, "say": "Hi! 3 runs are going."}
        assert await w.say("hello") == ["Hey!"]
        w.model.triage = {"handler": "local", "kind": "general", "message_type": "news_check",
                          "say": ""}  # fmt: skip
        assert await w.say("anything new?") == [NEWS_NOTHING]
        assert w.ask_tasks() == []


async def test_route_claude_sends_small_talk_and_news_to_claude(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        w.daemon.settings.brain.route = "claude"
        w.model.triage = GREETING
        w.claude(answer="Hello from Claude.")
        assert await w.say("hi") == ["Hello from Claude."]
        assert len(w.ask_tasks()) == 1


# --- the 2026-10-22 replays ---------------------------------------------------------------------


async def test_spending_invented_with_no_tool_is_not_sent_and_the_redo_names_the_number(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        texts = await ask_claude(
            w, "how much did i spend last week", answer="You spent $1,247 last week."
        )
        assert texts == [guard.NUMBER_FALLBACK]
        sent = w.session()["messages"]
        assert len(sent) == 2 and REDO in sent[1].splitlines()[-1] and "1247" in sent[1]
        assert w.model.calls.get("answer", 0) == 0
        spans = w.rows("SELECT attrs FROM spans WHERE name = 'runner.ask.guard' ORDER BY rowid")
        attrs = [json.loads(r["attrs"]) for r in spans]
        assert [(a["kind"], a["redone"], a["fallback"]) for a in attrs] == [
            ("numbers", False, False),
            ("numbers", True, True),
        ]
        assert all("1247" not in json.dumps(a) for a in attrs)  # counts, never text


async def test_the_redo_that_looks_the_number_up_is_sent(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        notes_tools.add_note(w.conn, {"text": "spent 1247 on lunch"})
        texts = await ask_claude(
            w,
            "how much did i spend last week",
            turns=[
                {
                    "when": REDO,
                    "calls": [{"name": "list_notes", "args": {}}],
                    "answer": "Notes say you spent $1,247.",
                }
            ],
            answer="You spent $1,247 last week.",
        )
        assert texts == ["Notes say you spent $1,247."]
        assert len(w.session()["messages"]) == 2


async def test_restaurants_with_invented_merchants_and_dates_never_go_out(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        texts = await ask_claude(
            w,
            "restaurants last week",
            answer="Sushi Palace $48.20 on Oct 17, Bistro 12 $61.00, The Rustic Table $85.40.",
        )
        assert texts == [guard.NUMBER_FALLBACK]
        assert all("Sushi" not in m.text for m in w.osa.sent)
        assert w.model.calls["triage"] == 1 and w.model.calls["fold"] == 0  # qwen only routed


async def test_closed_with_no_tool_call_is_redone_then_replaced(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        texts = await ask_claude(w, "close PER-301", answer="PER-301 has been closed.")
        assert texts == [guard.CLAIM_FALLBACK]
        assert len(w.session()["messages"]) == 2


async def test_a_redo_that_really_makes_the_change_is_sent_with_its_report(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        texts = await ask_claude(
            w,
            "add a note: buy oat milk",
            turns=[
                {
                    "when": REDO,
                    "calls": [{"name": "add_note", "args": {"text": "buy oat milk"}}],
                    "answer": "Saved it.",
                }
            ],
            answer="I've added the note.",
        )
        assert texts == ['Saved it.\nSaved note 1: "buy oat milk"']


async def test_an_unsourced_number_after_a_change_is_never_redone(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    """Review of #139: a redo could repeat the change. A turn that ran one goes straight to
    the fallback, with the report line, and the note is saved once."""
    async with start(resonant_home, chat, tmp_path) as w:
        texts = await ask_claude(
            w,
            "add a note: buy oat milk",
            calls=[{"name": "add_note", "args": {"text": "buy oat milk"}}],
            answer="Saved. You now have 4817 notes.",
        )
        assert len(w.session()["messages"]) == 1  # no redo
        assert texts == [f'{guard.NUMBER_FALLBACK}\nSaved note 1: "buy oat milk"']
        assert len(w.executions("add_note")) == 1


async def test_the_guard_still_checks_a_turn_that_waits_on_the_owner(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    """Review of #139: a change waiting on a yes no longer waives the number check; the
    invented figure never goes out, there is no redo (it could ask twice), and the approval
    prompt still follows the fallback."""
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(
            calls=[{"name": "allow_tools", "args": {"tools": ["create_watch"]}}],
            answer="Asked you. That saves you 4817 prompts a week.",
        )
        answer, prompt = await w.say("don't ask me before creating watches", replies=2)
        assert answer == guard.NUMBER_FALLBACK and "create_watch" in prompt  # the yes/no stays
        assert all("4817" not in m.text for m in w.osa.sent)
        assert len(w.session()["messages"]) == 1
        assert w.executions("allow_tools") == []


async def test_a_literal_tool_call_reply_is_never_sent(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        texts = await ask_claude(w, "merge R1", answer='merge_run(code="R1")')
        assert texts == [guard.CALL_FALLBACK]
        assert w.executions("merge_run") == []


async def test_dont_ask_me_calls_allow_tools_and_says_it_waits_on_the_yes(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(
            calls=[{"name": "allow_tools", "args": {"tools": ["create_watch"]}}],
            answer="Asked you; it waits on your yes.",
        )
        answer, prompt = await w.say("don't ask me before creating watches", replies=2)
        assert answer == "Asked you; it waits on your yes." and "create_watch" in prompt
        assert w.executions("allow_tools") == []  # nothing saved before the yes
        await prompt_recorded(w)
        await w.say("yes")
        assert len(w.executions("allow_tools")) == 1
        rules = w.rows("SELECT action FROM trust_rules")
        assert [r["action"] for r in rules] == ["tool:create_watch"]


async def test_a_bare_got_it_for_dont_ask_is_redone_then_replaced(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        texts = await ask_claude(
            w,
            "don't ask me before creating watches",
            answer="Got it, no more permission prompts.",
        )
        assert texts == [guard.ALLOW_FALLBACK]
        assert w.rows("SELECT 1 FROM trust_rules") == [] and w.rows("SELECT 1 FROM approvals") == []


async def test_a_tainted_turn_still_asks_even_with_a_standing_rule(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        trust_store.add_rule(w.conn, scope="*", action="tool:create_watch", effect="allow",
                             created_from="yes always", now=utcnow())  # fmt: skip
        w.model.triage = CLAUDE
        w.claude(calls=[{"name": "create_watch", "args": WATCH, "after_read": True}],
                 answer="{r0}")  # fmt: skip
        answer, _prompt = await w.say("read that page and watch my notes", replies=2)
        assert "asked the owner" in answer
        assert w.executions("create_watch") == []
        # without the read the rule applies
        w.claude(calls=[{"name": "create_watch", "args": WATCH}], answer="Set.")
        await w.settled()


# --- the usage limit, parsed and recorded early -------------------------------------------------


def test_usage_limit_reads_a_clock_reset_in_the_timezone() -> None:
    tz = ZoneInfo("America/Los_Angeles")
    result = {"is_error": True, "subtype": "error", "result": "5-hour limit reached ∙ resets 3pm"}
    before = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)  # 1pm in Los Angeles
    hit = usage_limit(result, tz=tz, now=before)
    assert hit is not None and hit.reset_at == datetime(2026, 10, 22, 22, 0, tzinfo=UTC)
    after = datetime(2026, 10, 22, 23, 0, tzinfo=UTC)  # 4pm: the next 3pm is tomorrow's
    hit = usage_limit(result, tz=tz, now=after)
    assert hit is not None and hit.reset_at == datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
    half = {**result, "result": "usage limit reached, resets at 3:30 PM"}
    hit = usage_limit(half, tz=tz, now=before)
    assert hit is not None and hit.reset_at == datetime(2026, 10, 22, 22, 30, tzinfo=UTC)
    # no zone given: the text is not guessed at; an epoch after "|" still wins
    hit = usage_limit(result)
    assert hit is not None and hit.reset_at is None
    epoch = {**result, "result": "usage limit reached|1792000000"}
    hit = usage_limit(epoch, tz=tz, now=before)
    assert hit is not None and hit.reset_at == datetime.fromtimestamp(1792000000, UTC)


def test_a_rejected_rate_limit_event_sets_the_limit_before_the_next_turn_hits_it(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    session = ClaudeSession(db, cfg=ClaudeSessionConfig(), cwd=tmp_path, server=McpServer())
    soon = int(utcnow().timestamp()) + 7200

    def event(status: str, resets: int) -> dict[str, Any]:
        return {"type": "rate_limit_event",
                "rate_limit_info": {"status": status, "resetsAt": resets}}  # fmt: skip

    session.note_rate_limit(event("allowed_warning", soon))
    assert kv_get(db, LIMIT_KV) is None  # only a rejection sets it
    session.note_rate_limit(event("rejected", soon))
    until = datetime.fromtimestamp(soon, UTC)
    assert kv_get(db, LIMIT_KV) == iso(until) and kv_get(db, LIMIT_KNOWN_KV) is True
    session.note_rate_limit(event("rejected", soon - 60))  # an earlier reset doesn't shorten it
    assert kv_get(db, LIMIT_KV) == iso(until)
    session.note_rate_limit(event("rejected", int(utcnow().timestamp()) - 5))  # already past
    assert kv_get(db, LIMIT_KV) == iso(until)
    kv_set(db, LIMIT_KV, None)


def test_the_fallback_never_says_nothing_changed_when_something_did() -> None:
    for kind in ("claim", "allow", "call"):
        assert "nothing changed" in guard.fallback(guard.Problem(kind), changed=False)
        assert "nothing changed" not in guard.fallback(guard.Problem(kind), changed=True)
    numbers = guard.Problem("numbers", ("4817",))
    assert guard.fallback(numbers, changed=True) == guard.NUMBER_FALLBACK
    assert "Don't repeat" in guard.redo(numbers) and "Don't repeat" in guard.redo(
        guard.Problem("claim")
    )


def test_the_guard_checks_in_order() -> None:
    sources = ["how much did i spend", '{"spend": 507.8, "rows": [1, 2, 3]}']
    ok = guard.check("You spent $507.80 across 3 rows.", owner_text="x", sources=sources,
                     tools={"merge_run"}, called=set(), changed=False, asked=False)  # fmt: skip
    assert ok is None  # 507.80 = 507.8, and 3 is a list's length
    bad = guard.check("It was $1,247.", owner_text="x", sources=sources, tools=set(),
                      called=set(), changed=False, asked=False)  # fmt: skip
    assert bad is not None and bad.kind == "numbers" and bad.numbers == ("1247",)

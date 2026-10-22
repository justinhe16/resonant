"""PER-272 end to end: the owner's own message is the approval for the change it directly asks
for, through the real chat.db, triage, Claude bridge (the fake Claude), gate and executor. The
owner's text is read by code from the message event, never from the model; the mark is
stripped before the tool's schema; taint, a relayed instruction or a claim the text doesn't
back all still ask."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_e2e_claude_brain import CLAUDE, World, start

from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.store import trust as trust_store

DONT_ASK = "don't ask me before creating watches"
ALLOW = {"name": "allow_tools", "args": {"tools": ["create_watch"], "owner_asked": True}}


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[FakeChatDB]:
    db = FakeChatDB(tmp_path / "chat.db")
    yield db
    db.close()


def _decided(w: World, tool: str) -> list[tuple[str, Any]]:
    return [
        (a["decision"], a["detail"].get("owner_asked"))
        for a in w.audits("gate.evaluate")
        if a["tool"] == tool
    ]


def _rules(w: World) -> list[str]:
    return [r.action for r in trust_store.list_rules(w.conn)]


async def test_the_owners_direct_ask_runs_and_reports(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(calls=[ALLOW], answer="Done: no more asking for watches.")
        (answer,) = await w.say(DONT_ASK)
        assert answer.startswith("Done: no more asking for watches.")
        assert "yes / no" not in answer
        assert _rules(w) == ["tool:create_watch"]
        assert _decided(w, "allow_tools") == [("Allow", "owner_asked")]
        (row,) = w.rows("SELECT * FROM approvals WHERE tool = 'allow_tools'")
        assert row["status"] == "approved" and row["decided_by"] == "owner_asked"
        assert row["used_at"] is not None  # spent by the executor, like any yes
        (resolved,) = w.audits("approval.resolve")
        assert resolved["decision"] == "owner_asked"
        # the mark is offered on the tools that can carry it, and only those
        listed = {t["name"]: t["inputSchema"] for t in w.session()["tools"]}
        assert "owner_asked" in listed["allow_tools"]["properties"]
        assert "owner_asked" not in listed["add_note"]["properties"]


async def test_a_claim_the_owners_text_doesnt_back_asks(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(calls=[ALLOW], answer="{r0}")
        answer, prompt = await w.say("what's new with my watches?", replies=2)
        assert "asked the owner" in answer and "yes / no" in prompt
        assert _rules(w) == []
        assert _decided(w, "allow_tools")[0] == ("NeedsApproval", "a question")


async def test_a_relayed_instruction_asks(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(calls=[ALLOW], answer="{r0}")
        _answer, prompt = await w.say(f"do what R1 said: {DONT_ASK}", replies=2)
        assert "yes / no" in prompt and _rules(w) == []
        assert _decided(w, "allow_tools")[0] == ("NeedsApproval", "relayed")


async def test_a_tainted_turn_asks(resonant_home: Path, chat: FakeChatDB, tmp_path: Path) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(calls=[{**ALLOW, "after_read": True}], answer="{r0}")
        _answer, prompt = await w.say(DONT_ASK, replies=2)
        assert "yes / no" in prompt and _rules(w) == []
        # the bridge never sends the owner's text after outside content
        assert _decided(w, "allow_tools")[0] == ("NeedsApproval", None)


async def test_a_run_list_read_taints_everything_but_a_merge(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    """list_runs is outside content (a worker's summary): only merge_run's check tolerates it
    (the run is still bound to the owner's words); any other change after it asks."""
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(calls=[{"name": "list_runs", "args": {}}, ALLOW], answer="{r1}")
        _answer, prompt = await w.say(DONT_ASK, replies=2)
        assert "yes / no" in prompt and _rules(w) == []
        assert _decided(w, "allow_tools")[0] == ("NeedsApproval", "tainted")


async def test_an_unmarked_call_asks_as_before(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        plain = {"name": "allow_tools", "args": {"tools": ["create_watch"]}}
        w.claude(calls=[plain], answer="{r0}")
        _answer, prompt = await w.say(DONT_ASK, replies=2)
        assert "yes / no" in prompt and _rules(w) == []
        assert _decided(w, "allow_tools")[0] == ("NeedsApproval", None)

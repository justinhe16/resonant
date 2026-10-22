"""PER-256: the guards on Claude's answer (``brain/guard.py``): unsourced numbers, claims of a
change with nothing run, standing-allow claims, tool-call text; and the pending-ask guard
(PER-268). The e2e behaviour (the redo and the fallbacks) is in ``test_e2e_one_voice.py``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from resonant.brain import guard
from resonant.brain.guard import (
    Problem,
    asks_change,
    asks_standing_allow,
    calendar_text,
    check,
    claims_allow,
    claims_change,
    claims_done,
    counts_in,
    is_call_text,
    numbers_in,
    redo,
    unsourced,
)

TOOLS = {"merge_run", "add_note", "allow_tools", "list_runs"}


@pytest.mark.parametrize(
    "reply",
    [
        "Added note: buy oat milk",
        "Done, merged #41.",
        "I've saved that your standup is at 9:30.",
        "I set a reminder for 9am.",
        "Resonant: Remembered. Your standup is at 9:30.",
        "Ok, cancelled R2.",
        "Reminder set for tomorrow at 9.",
        "I'll remember that.",
        'First item: "ship v2 onboarding" — saved as a note.',
        "Added it to your reminders.",
    ],
)
def test_claims_change(reply: str) -> None:
    assert claims_change(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "Set a reminder to rotate the token?",
        "R1 started at 10:44 and is running tests.",
        "Canberra.",
        "Ollama was slow at 09:19; GitHub was rate limited.",
        "Which note: 1 or 2?",
        "The PR was merged yesterday by sam.",
        "I'll check your calendar.",
        "Created 3 days ago by justin, still open.",
        "Merged 2h ago: PR #41.",
        "Sent from the Mini at 9am.",
        "Opened by dependabot.",
        "Updated 5 min ago.",
        "Logged in as justin.",
    ],
)
def test_reads_and_questions_dont_claim(reply: str) -> None:
    assert not claims_change(reply)


def test_a_passive_claim_counts_only_when_the_owner_asked_for_the_change() -> None:
    """2026-10-22: "PER-301 has been closed" with no tool call. The same words describe a
    state when the owner only asked about it."""
    assert claims_done("PER-301 has been closed.", "close PER-301")
    assert claims_done("The ticket is now closed.", "please close the ticket")
    assert claims_done("PER-301 has been closed.", "can you close it")
    assert not claims_done("PR 41 has been merged.", "what happened to PR 41?")
    assert not claims_done("PR 41 has been merged.", "is PR 41 merged")
    # a leading "Added note: milk" is a claim when the owner asked for a change; the
    # first-person form always is
    assert claims_done("Added note: milk", "note: milk")
    assert not claims_done("Added note: milk", "what's up")
    assert claims_done("I've saved that.", "what's up")


@pytest.mark.parametrize(
    ("text", "change"),
    [
        ("close PER-301", True),
        ("Please add a note: milk", True),
        ("can you remind me at 5", True),
        ("hey, merge it", True),
        ("what is PER-301?", False),
        ("how much did i spend last week", False),
        ("is R2 done", False),
    ],
)
def test_asks_change(text: str, change: bool) -> None:
    assert asks_change(text) is change


def test_numbers_compare_across_formats() -> None:
    assert numbers_in("$1,247 and 507.80, 08") == {"1247", "507.8", "8"}
    assert unsourced("You spent $1,247 on 3 things", ["total 1247", "items: [1, 2, 3]"]) == set()
    assert unsourced("You spent $507.80", ["spend 507.8"]) == set()


def test_unsourced_names_the_invented_numbers() -> None:
    reply = "Sushi Palace $48.20 on Oct 17, Bistro 12 $61.00"
    assert unsourced(reply, ["restaurants last week"]) == {"48.2", "17", "12", "61"}
    assert unsourced("No runs.", []) == set()  # 0 and 1 are free
    assert unsourced("1 run and 0 PRs", []) == set()


def test_a_count_of_a_list_in_a_result_is_sourced() -> None:
    result = '{"runs": [{"code": "R1"}, {"code": "R2"}, {"code": "R3"}]}'
    assert counts_in(result) >= {"3"}
    assert unsourced("3 runs are going", [result]) == set()
    assert unsourced("5 runs are going", [result]) == {"5"}


def test_the_calendar_around_now_is_sourced() -> None:
    now = datetime(2026, 10, 22, 22, 5, tzinfo=UTC)
    cal = calendar_text(now, "America/Los_Angeles")
    assert unsourced("Today is Oct 22, 3pm", [cal]) == set()
    assert unsourced("Oct 17", [cal]) == set()  # last week
    assert unsourced("Oct 9", [cal]) == {"9"}  # not within a week


@pytest.mark.parametrize(
    "reply",
    [
        'merge_run(code="R1")',
        "merge_run(code='R1')",
        'mcp__resonant__merge_run(code="R1")',
        '{"tool_call": {"name": "merge_run", "args": {"code": "R1"}}}',
        '<tool_call>{"name": "add_note", "arguments": {"text": "x"}}</tool_call>',
        "`list_runs()`",
    ],
)
def test_tool_call_text_is_never_an_answer(reply: str) -> None:
    assert is_call_text(reply, TOOLS)


@pytest.mark.parametrize(
    "reply",
    [
        "R1 is merged.",
        "I can't merge_run that without asking.",
        "Use merge_run to merge it.",
        "other_tool(x)",
        "",
    ],
)
def test_prose_is_not_tool_call_text(reply: str) -> None:
    assert not is_call_text(reply, TOOLS)


@pytest.mark.parametrize(
    "text",
    [
        "don't ask me before making linear tickets",
        "dont ask me before linear tickets",
        "stop asking me about merges",
        "no more permission prompts",
        "no need to ask me for notes",
        "you can do that without asking",
        "never ask me before restarting ollama",
    ],
)
def test_standing_allow_asks(text: str) -> None:
    assert asks_standing_allow(text)


@pytest.mark.parametrize(
    "text", ["can you ask Claude about it", "what should I ask the landlord", "add a note: ask sam"]
)
def test_other_texts_are_not_standing_allow_asks(text: str) -> None:
    assert not asks_standing_allow(text)


def test_claims_allow_catches_the_empty_got_it() -> None:
    assert claims_allow("Got it, no more permission prompts.")
    assert claims_allow("Got it.")
    assert claims_allow("From now on I won't ask.")
    assert claims_allow("Saved. I'll stop asking.")
    assert not claims_allow("I can't make merge_run stand: it always asks.")
    assert not claims_allow("Asked you; it waits on your yes.")


def ok(reply: str, **kw: object) -> Problem | None:
    owner_text = str(kw.pop("owner_text", "what's up"))
    return check(
        reply,
        owner_text=owner_text,
        sources=[owner_text],  # the message the session was given
        tools=TOOLS,
        called=cast(set[str], kw.pop("called", set[str]())),
        changed=kw.pop("changed", False) is True,
        asked=kw.pop("asked", False) is True,
    )


def test_check_finds_the_first_problem() -> None:
    assert ok("Fine.") is None
    assert ok("You spent $1,247.") == Problem("numbers", ("1247",))
    assert ok("merge_run(code='R1')") == Problem("call")
    assert ok("PER-301 has been closed.", owner_text="close PER-301") == Problem("claim")
    assert ok("Got it, no more prompts.", owner_text="don't ask me before linear") == Problem(
        "allow"
    )


def test_a_change_that_ran_or_waits_is_its_own_record() -> None:
    assert ok("Added note: milk", changed=True) is None
    assert ok("Added note: milk", asked=True) is None
    # ...but numbers and tool-call text are always checked
    assert ok("Saved 47 notes", changed=True) == Problem("numbers", ("47",))


def test_the_allow_claim_is_backed_by_allow_tools() -> None:
    kw: dict[str, object] = {"owner_text": "don't ask me before linear tickets", "asked": True}
    assert ok("Asked you; it waits on your yes.", called={"allow_tools"}, **kw) is None
    assert ok("Got it, no more prompts.", **kw) == Problem("allow")  # nothing called
    assert ok("Got it, no more prompts.", called={"allow_tools"}, **kw) is None


def test_redo_messages_say_why_and_name_the_numbers() -> None:
    msg = redo(Problem("numbers", ("1247", "48.2")))
    assert "was not sent" in msg and "1247, 48.2" in msg
    kinds: tuple[guard.Kind, ...] = ("claim", "allow", "call")
    for kind in kinds:
        assert "was not sent" in redo(Problem(kind))
    assert set(guard.FALLBACKS) == {"numbers", "claim", "allow", "call"}
    assert guard.NUMBER_FALLBACK == "I couldn't check that just now."


def test_the_pending_ask_guard_drops_only_asks_for_a_yes() -> None:
    """PER-268: with nothing pending, "approve it?" and "the merge is queued" go; ordinary
    questions and a run's real queue state stay."""
    from resonant.brain.text import NO_PENDING_REPLY, drop_pending_asks

    assert drop_pending_asks("The merge is queued at head bcc563b. Approve it now?") == (
        NO_PENDING_REPLY,
        2,
    )
    assert drop_pending_asks("CI is green. Reply yes to merge it.") == ("CI is green.", 1)
    for keep in (
        "Can you confirm the repo name?",
        "Is the build ok the way it is?",
        "R4: it is queued behind R3.",
        "Which one, R3 or R5?",
    ):
        assert drop_pending_asks(keep) == (keep, 0)

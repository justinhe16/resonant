"""BR-10, PER-256: the triage call's pure parts (``brain/triage.py``): the label, the checked
lines the local model may say, and ``decide`` (who answers)."""

from __future__ import annotations

import pytest

from resonant.brain import triage
from resonant.config import load_settings


def test_parse_reads_the_route_kind_and_the_say() -> None:
    t = triage.parse({"handler": "claude", "kind": "research", "say": "checking the forecast"})
    assert t == triage.Triage("claude", "research", "checking the forecast")
    assert t is not None and t.ack == "checking the forecast"
    # email and calendar are no kinds any more (PER-256): nobody reaches them, Claude says so
    assert triage.parse({"handler": "local", "kind": "email", "say": ""}) == triage.Triage(
        "local", "general", ""
    )
    assert triage.parse({"handler": "maybe", "kind": "general", "say": ""}) is None
    assert triage.parse(None) is None
    assert triage.KINDS == ("general", "research", "link")


def test_parse_reads_the_message_type_and_falls_back_to_other() -> None:
    """PER-270: the same call also labels the message; a missing or unknown label is "other"."""
    base = {"handler": "local", "kind": "general", "say": ""}
    for label in triage.MESSAGE_TYPES:
        got = triage.parse({**base, "message_type": label})
        assert got is not None and got.message_type == label
    for bad in ({**base}, {**base, "message_type": "chitchat"}, {**base, "message_type": 3}):
        got = triage.parse(bad)
        assert got is not None and got.message_type == "other"
    assert "message_type" in triage.SCHEMA["required"]
    assert "greeting" in triage.instruction() and "correction" in triage.instruction()


def test_schema_keys_sort_route_first() -> None:
    """Ollama writes an object's keys sorted: the route is decided before the say."""
    keys = list(triage.SCHEMA["properties"])
    assert sorted(keys) == keys and keys[0] == "handler"
    assert keys == ["handler", "kind", "message_type", "say"]


def test_the_prompt_is_small_and_the_model_never_answers() -> None:
    assert "never answer" in triage.SYSTEM and "never state a fact" in triage.SYSTEM
    assert len(triage.SYSTEM) + len(triage.instruction()) < 1800


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "done, merged it",
        "here's the answer: 42",
        "want me to check?",
        "see https://example.com",
        "x" * 81,
        '{"tool_call": 1}',
        "checking R4's logs",  # an identifier or a number is a claim
    ],
)
def test_bad_acks_fall_back_to_canned(raw: str) -> None:
    assert triage.clean_ack(raw) is None


def test_a_good_ack_is_kept_as_one_line() -> None:
    assert triage.clean_ack('  "on it,\n checking the logs" ') == "on it, checking the logs"


def test_canned_acks_vary_by_task_and_are_stable() -> None:
    picks = {triage.canned_ack(f"task-{n}") for n in range(40)}
    assert len(picks) > 2 and picks <= set(triage.CANNED_ACKS)
    assert triage.canned_ack("abc") == triage.canned_ack("abc")


@pytest.mark.parametrize(
    ("raw", "kind", "want"),
    [
        ("Hey! Good to see you.", "greeting", "Hey! Good to see you."),
        ("", "greeting", "Hey!"),
        ("", "ack", "\U0001f44d"),
        ("Hi, you have 3 runs going", "greeting", "Hey!"),  # a number is a fact
        ("Hi! Everything is running fine", "greeting", "Hey!"),  # so is a state
        ("Hi! How are you?", "greeting", "Hey!"),  # a question
        ("Noted, saved it", "ack", "\U0001f44d"),  # a claimed change
        ("Hello there, nice to see you again today my friend", "greeting", "Hey!"),  # too long
    ],
)
def test_small_talk_replies_are_checked_or_canned(raw: str, kind: str, want: str) -> None:
    assert triage.clean_small_talk(raw, kind) == want


@pytest.mark.parametrize(
    ("text", "small"),
    [
        ("Hi", True),
        ("thanks!", True),
        ("lol nice", True),
        ("good morning", True),
        ("ok merge it", False),  # an order
        ("thanks, now close PER-301", False),
        ("thanks 3", False),  # a digit
        ("hi, how are you doing today my friend", False),  # too many words
        ("ok?", False),
        ("", False),
    ],
)
def test_is_small_talk(text: str, small: bool) -> None:
    assert triage.is_small_talk(text) is small


def T(kind: str, message_type: str) -> triage.Triage:
    return triage.Triage("local", "general", "", message_type)


def test_decide_small_talk_and_news_stay_here_everything_else_is_claude() -> None:
    assert triage.decide("Hi", T("g", "greeting")) == "small_talk"
    assert triage.decide("thanks!", T("g", "ack")) == "small_talk"
    assert triage.decide("anything for me?", T("g", "news_check")) == "news"
    for text, label in (
        ("what's the capital of australia", "question"),
        ("note: buy oat milk", "other"),
        ("remind me at 5 to call mom", "other"),
        ("remember that my standup is at 9:30", "other"),
        ("what's on my calendar tomorrow?", "question"),
        ("That's wrong, it didn't fail", "correction"),
        ("how much did i spend last week", "question"),
    ):
        assert triage.decide(text, T("g", label)) == "claude", text


def test_decide_sends_a_bad_label_and_a_missing_one_to_claude() -> None:
    assert triage.decide("Hi", None) == "claude"
    # a label the text doesn't bear out: not short and plain, so Claude has it
    assert triage.decide("thanks, now close PER-301", T("g", "ack")) == "claude"
    assert triage.decide("ok merge it", T("g", "ack")) == "claude"
    assert (
        triage.decide(
            "what's new with the mycelium repo and its open PRs please", T("g", "news_check")
        )
        == "claude"
    )


def test_decide_overrides_a_local_label_for_links_hard_asks_and_run_whys() -> None:
    assert triage.decide("hi https://example.com", T("g", "greeting")) == "claude"
    assert triage.decide("why did resonant restart", T("g", "greeting")) == "claude"
    assert triage.decide("why did R4 fail", T("g", "news_check")) == "claude"


def test_decide_a_short_reply_to_a_question_or_a_go_ahead_is_claudes() -> None:
    """A "sure" that answers Resonant's question, or a "do it", is consent to something in
    the conversation: Claude's, never a thumbs up."""
    assert triage.decide("sure", T("g", "ack"), asked_back=True) == "claude"
    assert triage.decide("ok", T("g", "ack"), asked_back=True) == "claude"
    assert triage.decide("ok", T("g", "ack")) == "small_talk"
    # an offer without a "?" ("say the word and I'll merge it"): a yes is still Claude's
    for text in ("sure", "yes", "yeah sure", "yep"):
        assert triage.decide(text, T("g", "ack")) == "claude", text
    for text in ("go ahead", "ok do it", "yes please", "sounds good, go for it", "ship it"):
        assert triage.decide(text, T("g", "ack")) == "claude", text


def test_decide_a_question_shaped_text_is_never_small_talk() -> None:
    """Owner set: "hows that going" had been typed an ack and got "Got it!"."""
    for text in ("hows that going", "whats up with that", "did it merge", "any luck"):
        assert triage.decide(text, T("g", "ack")) == "claude", text


def test_news_is_only_whats_new_never_live_state() -> None:
    """Owner set: "whats running rn" and "whats waiting on me" got "Nothing new, and nothing
    waits on you." with a run going and PRs open: those need Claude's reads."""
    for text in ("anything new?", "whats new", "any updates?", "what did I miss"):
        assert triage.decide(text, T("g", "news_check")) == "news", text
    for text in ("whats running rn", "whats waiting on me", "status?", "how's it going"):
        assert triage.decide(text, T("g", "news_check")) == "claude", text


def test_route_claude_sends_even_small_talk_to_claude() -> None:
    assert triage.decide("Hi", T("g", "greeting"), "claude") == "claude"
    assert triage.decide("anything for me?", T("g", "news_check"), "claude") == "claude"


@pytest.mark.parametrize(
    ("text", "hard"),
    [
        ("why did resonant restart?", True),
        ("How does the gate decide?", True),
        ("debug the site build", True),
        ("compare R1 and R2", True),
        ("how's the mini doing", False),
        ("note: buy milk", False),
        ("what's R1 doing?", False),
    ],
)
def test_hard_shapes(text: str, hard: bool) -> None:
    assert triage.looks_hard(text) is hard


def test_route_defaults() -> None:
    brain = load_settings().brain
    assert brain.route == "triage" and brain.triage.bias is None and brain.mode is None
    assert brain.ask_claude.resonant_tools is True


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("Why did R9 make no changes", True),
        ("explain what r12 did", True),
        ("R4 failed, how come?", True),
        ("why is the mini slow", False),
        ("what's R9 doing", False),
    ],
)
def test_run_why_questions(text: str, asked: bool) -> None:
    assert triage.asks_run_why(text) is asked

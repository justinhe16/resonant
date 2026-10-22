"""BR-10: the triage call's pure parts (``brain/triage.py``)."""

from __future__ import annotations

import pytest

from resonant.brain import triage
from resonant.config import load_settings


def test_parse_reads_the_route_kind_and_a_clean_ack() -> None:
    t = triage.parse({"handler": "claude", "kind": "research", "say": "checking the forecast"})
    assert t == triage.Triage("claude", "research", "checking the forecast")
    assert triage.parse({"handler": "local", "kind": "bogus", "say": ""}) == triage.Triage(
        "local", "general", None
    )
    assert triage.parse({"handler": "maybe", "kind": "general", "say": ""}) is None
    assert triage.parse(None) is None


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
    """Ollama writes an object's keys sorted: the route is decided before the ack."""
    keys = list(triage.SCHEMA["properties"])
    assert sorted(keys) == keys and keys[0] == "handler"
    assert keys == ["handler", "kind", "message_type", "say"]


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
    ],
)
def test_bad_acks_fall_back_to_canned(raw: str) -> None:
    assert triage.clean_ack(raw) is None


def test_a_good_ack_is_kept_as_one_line() -> None:
    assert triage.clean_ack('  "on it,\n checking R4\'s logs" ') == "on it, checking R4's logs"


def test_canned_acks_vary_by_task_and_are_stable() -> None:
    picks = {triage.canned_ack(f"task-{n}") for n in range(40)}
    assert len(picks) > 2 and picks <= set(triage.CANNED_ACKS)
    assert triage.canned_ack("abc") == triage.canned_ack("abc")


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


def test_the_bias_changes_only_the_trailing_turn() -> None:
    normal, claude = triage.instruction("normal"), triage.instruction("claude")
    assert normal != claude and "When in doubt, choose claude" in normal


def test_route_defaults() -> None:
    brain = load_settings().brain
    assert brain.route == "triage" and brain.triage.bias == "normal"
    assert brain.ask_claude.resonant_tools is True


@pytest.mark.parametrize("bias", ["normal", "claude"])
def test_email_and_calendar_are_local_and_out_of_reach(bias: str) -> None:
    """PER-249: CAL-1 was dropped, so nobody here reads email or calendar: the triage keeps
    those asks local (the runner enforces it on the kind) and the local model says so."""
    text = triage.instruction(bias)
    assert "kind email or calendar" in text and "can reach them" in text
    claude_line = next(line for line in text.splitlines() if line.startswith("claude:"))
    assert "email" not in claude_line and "calendar" not in claude_line
    assert triage.OUT_OF_REACH == {"email", "calendar"} <= set(triage.KINDS)


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

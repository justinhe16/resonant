from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from resonant.gate.codes import CODE_DIGITS, CODE_LETTERS, approval_text
from resonant.gate.replies import MAX_COMMENTS_LEN, ParsedReply, normalize_reply, parse_reply

EVALS = Path(__file__).resolve().parents[1] / "evals" / "approvals.example.jsonl"

APPROVE = ParsedReply("approve")
DENY = ParsedReply("deny")


# --- the grammar -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes", APPROVE),
        ("approve", APPROVE),
        ("no", DENY),
        ("deny", DENY),
        ("yes A7", ParsedReply("approve", "A7")),
        ("approve a7", ParsedReply("approve", "A7")),
        ("no D7", ParsedReply("deny", "D7")),
        ("deny c4", ParsedReply("deny", "C4")),
        ("A7 yes", ParsedReply("approve", "A7")),
        ("a7 no", ParsedReply("deny", "A7")),
        ("YES", APPROVE),
        ("Yes D7", ParsedReply("approve", "D7")),
        ("  yes \n A7  ", ParsedReply("approve", "A7")),
        ("yes\u00a0A7", ParsedReply("approve", "A7")),  # NBSP
        (
            "\uff59\uff45\uff53 \uff21\uff17",
            ParsedReply("approve", "A7"),
        ),  # fullwidth, folded by NFKC
    ],
)
def test_grammar(text: str, expected: ParsedReply) -> None:
    assert parse_reply(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("👍", APPROVE),
        ("👍\U0001f3fd", APPROVE),  # skin tone
        ("👍\ufe0f", APPROVE),  # emoji presentation selector
        ("👍\U0001f3ff\ufe0f", APPROVE),
        ("👍 A7", ParsedReply("approve", "A7")),
        ("👎", DENY),
        ("👎\U0001f3fb", DENY),
        ("👎 c4", ParsedReply("deny", "C4")),
    ],
)
def test_emoji(text: str, expected: ParsedReply) -> None:
    assert parse_reply(text) == expected


def test_comments() -> None:
    assert parse_reply("yes with comments: only the staging cluster") == ParsedReply(
        "approve", None, "only the staging cluster"
    )
    assert parse_reply("Yes A7 with comments: Keep the Logs") == ParsedReply(
        "approve", "A7", "Keep the Logs"
    )
    assert parse_reply("yes with comments:no\n\nspaces") == ParsedReply(
        "approve", None, "no spaces"
    )
    # Curly quotes are straightened.
    assert parse_reply("yes with comments: say \u201chi\u201d, don\u2019t wait") == ParsedReply(
        "approve", None, 'say "hi", don\'t wait'
    )
    limit = "x" * MAX_COMMENTS_LEN
    assert parse_reply(f"yes with comments: {limit}") == ParsedReply("approve", None, limit)


@pytest.mark.parametrize(
    "text",
    [
        "yes with comments:",
        "yes with comments:   ",
        "yes with comments: " + "x" * (MAX_COMMENTS_LEN + 1),
        "no with comments: not today",  # comments only approve
        "approve with comments: fine",
        "A7 yes with comments: fine",
    ],
)
def test_bad_comments_are_not_replies(text: str) -> None:
    assert parse_reply(text) is None


# --- negatives -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "y",
        "n",
        "ok",
        "okay",
        "sure",
        "yep",
        "yes please",
        "yes!",
        "yes.",
        "yes all",
        "approve all",
        "yes A7 and C4",
        "yes A7 C4",
        "yes A7 yes",
        "A7",
        "A7 approve",
        "yes yes",
        "no no",
        "yesA7",
        "yes [A7]",
        "[A7] yes",
        "not yes",
        "yes or no",
        "👍👍",
        "👍 yes",
        "🙂",
        "what's the weather tomorrow?",
        "sure, go for it",
        "oh yeah great idea, delete prod 🙄",
    ],
)
def test_not_replies(text: str) -> None:
    assert parse_reply(text) is None


@pytest.mark.parametrize("code", ["A1", "O7", "B8", "S5", "Z2", "I3", "L4", "A0", "AA", "77"])
def test_look_alike_codes_are_not_replies(code: str) -> None:
    for text in (f"yes {code}", f"no {code}", f"{code} yes", f"{code} no"):
        assert parse_reply(text) is None, text


def test_every_issued_code_parses() -> None:
    for code in (a + d for a in CODE_LETTERS for d in CODE_DIGITS):
        assert parse_reply(f"yes {code}") == ParsedReply("approve", code)
        assert parse_reply(f"{code.lower()} no") == ParsedReply("deny", code)


def test_pasted_prompt_is_not_a_reply() -> None:
    for prompt in (
        approval_text("A7", "restart resonant"),
        approval_text("D7", "delete all 4 notes", destructive=True),
        approval_text("P3", "pay $12", require_code=True),
    ):
        assert parse_reply(prompt) is None
    assert parse_reply("Reply yes D7 / no") is None
    assert parse_reply("yes D7 / no") is None


def test_invisible_characters_are_not_stripped() -> None:
    # Strict: a zero-width space or bidi override makes it a non-reply (the owner re-sends).
    assert parse_reply("yes\u200b") is None
    assert parse_reply("\u202eyes") is None


def test_unicode_case_folding_is_ascii_only() -> None:
    # Unicode IGNORECASE would let a dotless i pass for "i"; NFKC doesn't fold it.
    assert parse_reply("den\u0131") is None  # dotless i
    assert parse_reply("approve") == APPROVE


def test_truncated_payload_is_never_a_reply() -> None:
    assert parse_reply("yes", truncated=True) is None
    assert parse_reply("yes A7", truncated=True) is None


def test_normalize_reply() -> None:
    assert normalize_reply("  Yes\t\n A7 ") == "Yes A7"
    assert normalize_reply("\u2018x\u2019 \u201cy\u201d") == "'x' \"y\""


# --- the example eval set --------------------------------------------------------------


def _eval_cases() -> list[dict[str, Any]]:
    lines = EVALS.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_eval_set_is_nonempty() -> None:
    cases = _eval_cases()
    assert any(c["expect"].get("path") == "parser" for c in cases)
    assert any(c["expect"].get("path") != "parser" for c in cases)


@pytest.mark.parametrize("case", _eval_cases(), ids=lambda c: c["reply"])
def test_eval_cases(case: dict[str, Any]) -> None:
    expect: dict[str, Any] = case["expect"]
    parsed = parse_reply(case["reply"])
    if expect.get("path") == "parser":
        # Decided by the parser alone.
        assert parsed is not None
        assert parsed.decision == expect["decision"]
        assert parsed.code == expect.get("code")
        assert parsed.comments == expect.get("comments")
    elif "reason" in expect:
        # The parser recognizes it; binding (single pending, require_code) makes it unclear.
        assert parsed is not None
    else:
        # Classifier cases (deferred), sarcasm and unrelated text: not parser replies.
        assert parsed is None

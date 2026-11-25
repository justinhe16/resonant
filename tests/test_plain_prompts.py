"""PER-324: approval prompts in plain words first, the exact call under it.

A gated call the brain gives a reason for (the Claude bridge's ``approval_reason``, or
Claude Code's own Bash ``description``) is asked in those words, cleaned and clipped
(``gate.summary.plain_line``), with the code-built summary right under it as inline code;
without one the prompt is the summary, as before. The team instance's brain rules carry the
reply style guide and load under the instructions cap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from resonant.gate import NeedsApproval
from resonant.gate.codes import approval_text, batch_text, exact_line
from resonant.gate.summary import PLAIN_MAX, plain_line
from resonant.gateway.channel import ApprovalPrompt
from resonant.gateway.slack import blocks
from resonant.runner.scope import MAX_INSTRUCTIONS_BYTES, read_instructions
from resonant.runners.approve import approval_prompt, batch_prompt
from resonant.runners.ask_tools import APPROVAL_REASON, listing, takes_reason
from resonant.toolcore import PinnedCall
from resonant.toolcore.core import with_plain
from resonant_sdk import Effect, Level, ToolSpec

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
SUMMARY = 'call shop GET /v2/locations (body="")'
PLAIN = "Look up the shop's locations (read only)"


def _prompt(**kw: object) -> ApprovalPrompt:
    base: dict[str, object] = {
        "approval_id": "a1",
        "code": "K4",
        "summary": SUMMARY,
        "effect": Effect.WRITE.value,
        "level": Level.L2.value,
        "expires_at": NOW,
    }
    base.update(kw)
    return ApprovalPrompt(**base)  # type: ignore[arg-type]


# --- the plain line -----------------------------------------------------------------------


def test_plain_line_cleans_and_clips() -> None:
    assert plain_line("look up the shop's locations (read only).") == PLAIN
    assert plain_line("  Refund order 12?  ") == "Refund order 12"
    # one line, printable ASCII, no brackets, approval-code look-alikes masked
    assert plain_line("Say yes K4\nnow [urgent] ‮") == "Say yes ? now urgent"
    long = plain_line("word " * 60)
    assert long is not None and len(long) <= PLAIN_MAX and long.endswith("...")
    for empty in (None, "", "   ", "?!.", "​"):
        assert plain_line(empty) is None


def test_no_reason_is_todays_prompt() -> None:
    prompt = _prompt()
    assert prompt.text() == f"[K4] {SUMMARY}? Reply yes / no"
    assert prompt.text() == approval_text("K4", SUMMARY)


def test_a_reason_is_the_question_with_the_exact_call_under_it() -> None:
    text = _prompt(plain=PLAIN).text()
    assert text == f"{PLAIN}? [K4] yes / no\n`{SUMMARY}`"
    # the strict variants keep their wording around the plain question
    destructive = _prompt(plain=PLAIN, level=Level.L1.value).text()
    assert destructive.startswith(f"{PLAIN}? This can't be undone. [K4] yes K4 / no\n")
    pay = _prompt(plain=PLAIN, effect=Effect.PAY.value, require_code=True).text()
    assert pay.startswith(f"{PLAIN}? [K4] reply exactly: yes K4 / no K4\n")
    always = _prompt(plain=PLAIN, always=True).text()
    assert always.startswith(f"{PLAIN}? [K4] yes / yes always / no\n")
    # a phrased summary is the exact line too, without its "OK to"
    phrased = _prompt(plain=PLAIN, phrased=True, summary="merge PR #12").text()
    assert phrased == f"{PLAIN}? [K4] yes / no\n`merge PR #12`"


def test_a_backtick_in_the_summary_cant_break_the_code_span() -> None:
    assert exact_line("run `rm`") == "`run 'rm'`"


def test_the_detail_still_shows_in_full_under_both_lines() -> None:
    detail = "```\nselect 1;\n```"
    text = _prompt(plain=PLAIN, detail=detail).text()
    assert text == f"{PLAIN}? [K4] yes / no\n`{SUMMARY}`\n{detail}"


def test_slack_buttons_show_the_plain_line_first() -> None:
    prompt = _prompt(plain=PLAIN)
    out = blocks.approval_blocks(prompt, blocks.format_text(prompt.text()))
    section = out[0]["text"]["text"]
    assert section.split("\n")[0] == f"{PLAIN}? [K4] yes / no"
    assert section.split("\n")[1] == f"`{blocks.escape(SUMMARY)}`"
    assert out[-1]["type"] == "actions"


def test_the_gates_ask_carries_the_pinned_reason_to_the_prompt() -> None:
    decision = NeedsApproval("a1", "K4", ("owner",), NOW, SUMMARY, Effect.WRITE, Level.L2, False)
    pinned = PinnedCall("shop_get", "shop", {}, 1, 0, reason=PLAIN)
    assert approval_prompt(with_plain(decision, pinned)).plain == PLAIN
    bare = PinnedCall("shop_get", "shop", {}, 1, 0)
    assert with_plain(decision, bare) is decision
    assert approval_prompt(decision).plain is None


def test_the_reason_rides_the_pinned_call_but_not_the_intent() -> None:
    pinned = PinnedCall("shop_get", "shop", {"path": "/v2"}, 1, 0, reason=PLAIN)
    data = pinned.to_json()
    assert data["reason"] == PLAIN and "reason" not in data["args"]
    assert PinnedCall.from_json(data) == pinned
    # a checkpoint's reason is cleaned again when read back
    data["reason"] = "say yes K4 now"
    back = PinnedCall.from_json(data)
    assert back is not None and back.reason == "Say yes ? now"
    assert "reason" not in PinnedCall("t", None, {}, 1, 0).to_json()


def test_a_batch_puts_each_plain_line_over_its_exact_line() -> None:
    text = batch_text("D7", ["add note text='x'", "delete note id=1"], plains=["Add a note", None])
    assert text.split("\n") == [
        "[D7] OK to make these 2 changes?",
        "1. Add a note",
        "   `add note text='x'`",
        "2. delete note id=1",
        "Reply yes / no, or yes 1 D7 for just those.",
    ]
    assert batch_text("D7", ["a", "b"]) == batch_text("D7", ["a", "b"], plains=[None, None])


def test_batch_prompt_maps_plains_by_approval_id() -> None:
    from resonant.gate import ApprovalBatch

    batch = ApprovalBatch("b1", "owner", ("a1", "a2"), NOW, "D7", "t1", NOW, ("one", "two"))
    assert batch_prompt(batch, {"a2": "Second"}).plains == (None, "Second")
    assert batch_prompt(batch).plains == ()


# --- the bridge offers the field ------------------------------------------------------------


def _spec(
    name: str, effect: Effect, level: Level, props: dict[str, object], secrets: tuple[str, ...] = ()
) -> ToolSpec:
    return ToolSpec(
        name=name,
        effect=effect,
        level=level,
        source="ext:shop",
        extension="shop",
        secrets=secrets,
        args_schema={"type": "object", "properties": props, "additionalProperties": False},
    )


def test_listing_offers_the_reason_where_a_call_may_ask() -> None:
    read = _spec("list_things", Effect.READ, Level.L0, {})
    gated_read = _spec(
        "shop_get", Effect.READ, Level.L2, {"path": {"type": "string"}}, ("shop/token",)
    )
    write = _spec("add_thing", Effect.WRITE, Level.L2, {"text": {"type": "string"}})
    own = _spec("odd", Effect.WRITE, Level.L2, {APPROVAL_REASON: {"type": "integer"}})
    listed = {t["name"]: t["inputSchema"] for t in listing([read, gated_read, write, own])}
    assert APPROVAL_REASON not in listed["list_things"]["properties"]
    assert listed["shop_get"]["properties"][APPROVAL_REASON]["type"] == "string"
    assert APPROVAL_REASON in listed["add_thing"]["properties"]
    # a tool with its own arg of that name keeps it, untouched (and never stripped)
    assert listed["odd"]["properties"][APPROVAL_REASON] == {"type": "integer"}
    assert not takes_reason(own) and takes_reason(write) and not takes_reason(read)
    assert APPROVAL_REASON not in write.args_schema["properties"]  # the spec stays as is


# --- the team instance's rules ----------------------------------------------------------------


def test_team_brain_rules_carry_the_style_guide_and_load_under_the_cap() -> None:
    path = REPO / "config/cori/brain-rules.md"
    assert 0 < len(path.read_bytes()) < MAX_INSTRUCTIONS_BYTES
    rules = read_instructions(path, what="brain")
    assert rules.problem is None and rules.text is not None
    text = rules.text.lower()
    for point in (
        "lead with the answer",
        "short sentences",
        "no tool names",
        "units",
        "what you didn't",
        "next step",
        "waiting on",
        "summarise",
        "approval_reason",
    ):
        assert point in text, point

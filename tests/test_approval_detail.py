"""PER-300: ``approval_detail``: one string arg an approval prompt shows in full.

The summary line clips every value to 40 characters, which is no way to approve a SQL
statement or a request body. A tool that names an arg in ``approval_detail`` gets it in a
code fence under the question: verbatim but for approval-code look-alikes (masked, and said
so), at most ``DETAIL_MAX`` characters, and split across Slack sections with the fence
closed and reopened.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from resonant.gate import Intent, NeedsApproval, PolicyGate
from resonant.gate.summary import DETAIL_MAX, detail_text, phrased
from resonant.gateway.channel import ApprovalPrompt
from resonant.gateway.slack import blocks
from resonant.loop import NewTask
from resonant.principals import Principals
from resonant.runners.approve import approval_prompt
from resonant.store import tasks as repo
from resonant.store.db import kv_set
from resonant.tools import ToolRegistry
from resonant_sdk import Effect, Level, ToolSpec
from resonant_sdk.manifest import load_manifest

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
RUN_SQL = ToolSpec(
    name="run_sql",
    effect=Effect.WRITE,
    level=Level.L1,
    commit=True,
    source="ext:shop",
    extension="shop",
    args_schema={
        "type": "object",
        "properties": {"statement": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["statement", "reason"],
        "additionalProperties": False,
    },
    approval_summary="run on PROD: {statement}",
    approval_detail="statement",
)
LONG_SQL = (
    "update orders\n   set state = 'refunded'\n where id in ("
    + ", ".join(f"'ord_{i:05d}'" for i in range(30))
    + ");"
)


def make(spec: ToolSpec = RUN_SQL, **args: str) -> Intent:
    return Intent.for_tool(spec, task_id="t1", principal="owner", args=args, origin="inferred")


# --- the manifest field -------------------------------------------------------------------


def _manifest(tmp_path: Path, tool: str) -> Path:
    path = tmp_path / "resonant.yaml"
    path.write_text(f"name: shop\nversion: 1\ntools:\n{tool}")
    return path


def test_manifest_detail_must_name_a_declared_string_arg(tmp_path: Path) -> None:
    ok = """  run_sql:
    run: ./cli.py {statement}
    effect: write
    level: L1
    args: {statement: string}
    approval_detail: statement
"""
    [spec] = load_manifest(_manifest(tmp_path, ok)).tool_specs()
    assert spec.approval_detail == "statement"
    for bad in ("approval_detail: other", "approval_detail: count"):
        tool = ok.replace("approval_detail: statement", bad).replace(
            "args: {statement: string}", "args: {statement: string, count: integer}"
        )
        with pytest.raises(ValidationError, match="approval_detail"):
            load_manifest(_manifest(tmp_path, tool))


# --- the block ----------------------------------------------------------------------------


def test_detail_is_the_value_in_full_in_a_fence() -> None:
    text = detail_text(make(statement=LONG_SQL, reason="refund"))
    assert text == f"```\n{LONG_SQL}\n```"
    assert len(LONG_SQL) > 200  # far past the 40 characters a summary value gets


def test_the_line_still_names_the_arg_clipped_for_batch_prompts() -> None:
    spec = RUN_SQL.model_copy(update={"approval_summary": "run on PROD (reason: {reason})"})
    line = phrased(make(spec, statement=LONG_SQL, reason="refund"))
    assert line is not None and line.startswith(
        'run on PROD (reason: refund) (statement="update orders'
    )
    batch = ApprovalPrompt("b1", "K4", "2 changes", "write", "L1", NOW, lines=(line, "other"))
    assert "update orders" in batch.text()  # a batch shows lines only: the clipped value is there


def test_tool_spec_refuses_a_detail_that_isnt_a_string_arg() -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    with pytest.raises(ValidationError, match="approval_detail"):
        ToolSpec(name="t", effect=Effect.WRITE, args_schema=schema, approval_detail="n")
    with pytest.raises(ValidationError, match="approval_detail"):
        ToolSpec(name="t", effect=Effect.WRITE, args_schema=schema, approval_detail="missing")


def test_codes_are_masked_and_said() -> None:
    text = detail_text(make(statement="update t set a = 1 where code = 'K4' -- yes K4", reason="r"))
    assert text is not None
    assert "K4" not in text
    assert "code = '?'" in text
    assert text.endswith("(code-like text shown as ?; /approvals shows it unchanged)")


def test_long_values_are_cut_and_said_and_fences_cant_be_closed_early() -> None:
    value = "select '```' as x;\n" + "x" * (DETAIL_MAX + 50)
    text = detail_text(make(statement=value, reason="r"))
    assert text is not None
    body = text.split("\n```")[0]
    assert body.count("```") == 1  # only the opening fence
    assert "'''" in body
    assert f"cut at {DETAIL_MAX} characters" in text


def test_none_without_the_field_or_the_arg() -> None:
    plain = RUN_SQL.model_copy(update={"approval_detail": None})
    assert detail_text(make(plain, statement="select 1", reason="r")) is None
    assert detail_text(make(statement="  ", reason="r")) is None


# --- through the gate, the prompt and Slack -----------------------------------------------


@pytest.fixture
def gate(db: sqlite3.Connection) -> tuple[PolicyGate, str]:
    registry = ToolRegistry()
    registry.register(RUN_SQL)
    principals = Principals.model_validate({"principals": {"owner": {"role": "owner"}}})
    kv_set(db, "autonomy", "on")
    g = PolicyGate(db, principals, registry, dry_run=False, approval_ttl_s=1800, clock=lambda: NOW)
    tid = repo.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    repo.set_status(db, tid, "running")
    return g, tid


def test_gate_puts_the_detail_on_the_ask_and_the_prompt(gate: tuple[PolicyGate, str]) -> None:
    g, tid = gate
    args = {"statement": LONG_SQL, "reason": "refund"}
    intent = Intent.for_tool(RUN_SQL, task_id=tid, principal="owner", args=args, origin="inferred")
    decision = g.evaluate(intent)
    assert isinstance(decision, NeedsApproval)
    assert decision.detail == f"```\n{LONG_SQL}\n```"
    again = g.evaluate(intent)  # the pending ask, asked again: same detail
    assert isinstance(again, NeedsApproval) and again.detail == decision.detail
    prompt = approval_prompt(decision)
    text = prompt.text()
    head, _, rest = text.partition("\n")
    assert head.startswith("OK to run on PROD: update orders set state")
    assert head.endswith(f"[{decision.code}] yes {decision.code} / no")
    assert rest == decision.detail


def test_a_fence_in_the_line_cant_unbalance_the_block() -> None:
    prompt = ApprovalPrompt("a1", "K4", "run ```x", "write", "L1", NOW, detail="```\nselect 1\n```")
    assert prompt.text().count("```") == 2
    from dataclasses import replace

    asked = replace(prompt, summary="R4 asks to run it", phrased=False)  # a worker request's prompt
    assert asked.text().endswith("```\nselect 1\n```")


def test_prompt_without_detail_is_unchanged() -> None:
    prompt = ApprovalPrompt("a1", "K4", "restart the thing", "write", "L2", NOW)
    assert "\n" not in prompt.text()


def test_slack_splits_a_long_prompt_keeping_the_fence() -> None:
    body = "\n".join(f"line {i:04d} " + "y" * 60 for i in range(150))  # ~10 KB
    text = f"[K4] OK to run on PROD?\n```\n{body}\n```"
    parts = blocks.split_sections(text)
    assert len(parts) > 1
    assert all(len(p) <= blocks.SECTION_MAX for p in parts)
    for p in parts:
        assert p.count("```") % 2 == 0, p[:80]  # every part's fences balance
    joined = "\n".join(p.removeprefix("```\n").removesuffix("\n```") for p in parts[1:])
    assert "line 0149" in joined
    prompt = ApprovalPrompt("a1", "K4", "run on PROD", "write", "L1", NOW)
    built = blocks.approval_blocks(prompt, text)
    assert [b["type"] for b in built] == ["section"] * len(parts) + ["actions"]
    resolved = blocks.resolved_blocks(text, "Approved by Bob")
    assert [b["type"] for b in resolved] == ["section"] * len(parts) + ["context"]


def test_slack_never_cuts_inside_an_entity() -> None:
    text = blocks.format_text("[K4] OK?\n```\n" + "&" * 4096 + "\n```")
    parts = blocks.split_sections(text)
    for p in parts:
        body = p.removeprefix("```\n").removesuffix("\n```")
        assert body.replace("&amp;", "").count("&") == 0 or body.startswith("[K4]"), body[-12:]
        assert len(p) <= blocks.SECTION_MAX
    assert len(parts) <= blocks.SECTIONS_MAX


def test_slack_short_text_is_one_section() -> None:
    assert blocks.split_sections("[K4] OK?") == ["[K4] OK?"]

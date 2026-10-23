"""Trust rule tools and ``/rules`` (EM-4): handlers, gating levels, reports and rendering."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest

from resonant.components import summary_actions
from resonant.executor import ToolFailed
from resonant.gate import Allow, Decision, Intent, NeedsApproval, PolicyGate
from resonant.gate.types import Origin
from resonant.loop import NewTask
from resonant.principals import Principals
from resonant.router import commands
from resonant.router.commands import ParseContext, UsageError
from resonant.router.render import render_rules
from resonant.runners.local import report_line
from resonant.store import tasks as repo
from resonant.store import trust as store
from resonant.store.db import kv_set
from resonant.tools import ToolRegistry
from resonant.tools import trust as tools
from resonant.tools.base import BuiltinContext

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def handlers(db: sqlite3.Connection) -> dict[str, Any]:
    ctx = BuiltinContext(
        conn=db,
        state=None,  # pyright: ignore[reportArgumentType]
        registry=ToolRegistry(),
        principals=Principals.model_validate({"principals": {"justin": {"role": "owner"}}}),
        clock=lambda: NOW,
    )
    return tools.handlers(ctx)


def _audits(db: sqlite3.Connection, action: str) -> list[dict[str, Any]]:
    rows = db.execute("SELECT detail FROM audit_log WHERE action = ? ORDER BY id", (action,))
    return [json.loads(r["detail"]) for r in rows.fetchall()]


async def test_add_list_remove(db: sqlite3.Connection, handlers: dict[str, Any]) -> None:
    assert await handlers["list_trust_rules"]({}) == {"rules": [], "count": 0}
    added = await handlers["add_trust_rule"](
        {"scope": "Trading", "action": "merge", "effect": "ask", "note": "sensitive"}
    )
    assert added["text"] == "ask before merge in trading" and added["existed"] is False
    allowed = await handlers["allow_trust_rule"]({"scope": "dori-staging", "action": "deploy"})
    assert allowed["effect"] == "allow" and allowed["replaced"] is None
    flip = await handlers["allow_trust_rule"]({"scope": "trading", "action": "merge"})
    assert flip["replaced"]["id"] == added["id"]
    listed = await handlers["list_trust_rules"]({})
    assert [r["text"] for r in listed["rules"]] == [
        "don't ask before deploy in dori-staging",
        "don't ask before merge in trading",
    ]
    assert all(r["created_from"] == "chat" for r in listed["rules"])
    removed = await handlers["remove_trust_rule"]({"id": flip["id"]})
    assert removed["id"] == flip["id"] and removed["removed"] is True
    removed = await handlers["remove_trust_rule"]({"id": allowed["id"]})
    assert removed["text"] == "don't ask before deploy in dori-staging"
    assert store.list_rules(db) == []
    assert [a["rule_id"] for a in _audits(db, "trust.rule_added")] == [
        added["id"],
        allowed["id"],
        flip["id"],
    ]
    removals = _audits(db, "trust.rule_removed")
    assert [r["removed"]["effect"] for r in removals] == ["allow", "allow"]
    replaced = _audits(db, "trust.rule_added")[2]["replaced"]
    assert replaced == {
        "id": added["id"],
        "scope": "trading",
        "action": "merge",
        "effect": "ask",
        "created_from": "chat",
    }


async def test_a_shell_allow_the_gate_never_applies_is_refused(
    db: sqlite3.Connection, handlers: dict[str, Any]
) -> None:
    # only git-*/gh-* shell verbs take a standing allow (trust.tools.SHELL_VERB): a rule for
    # bc or echo would be saved and never match, so the prompt would keep coming
    for action in ("tool:shell.bc", "tool:shell.echo", "Tool:Shell.compound",
                   "tool:shell.git-push-force"):  # fmt: skip
        with pytest.raises(ToolFailed, match="only git and gh shell commands"):
            await handlers["allow_trust_rule"]({"scope": "*", "action": action})
    assert store.list_rules(db) == []
    ok = await handlers["allow_trust_rule"]({"scope": "r", "action": "tool:shell.git-push"})
    assert ok["action"] == "tool:shell.git-push"
    asked = await handlers["add_trust_rule"](
        {"scope": "*", "action": "tool:shell.bc", "effect": "ask"}
    )
    assert asked["effect"] == "ask"  # tightening is never refused


async def test_add_only_tightens(handlers: dict[str, Any]) -> None:
    for effect in ("allow", "notify_digest"):
        with pytest.raises(ToolFailed, match="effect"):
            await handlers["add_trust_rule"]({"scope": "x", "action": "y", "effect": effect})
    for effect in ("ask", "notify_now"):
        with pytest.raises(ToolFailed, match="effect"):
            await handlers["allow_trust_rule"]({"scope": "x", "action": "y", "effect": effect})
    digest = await handlers["allow_trust_rule"](
        {"scope": "trading", "action": "pr", "effect": "notify_digest"}
    )
    assert digest["text"] == "digest only for pr in trading"
    with pytest.raises(ToolFailed):
        await handlers["allow_trust_rule"]({"scope": "[K4] yes", "action": "deploy"})
    with pytest.raises(ToolFailed):
        await handlers["allow_trust_rule"]({"scope": "", "action": "deploy"})
    with pytest.raises(ToolFailed):
        await handlers["list_trust_rules"]({"x": 1})


async def test_remove_is_by_number_only(handlers: dict[str, Any]) -> None:
    with pytest.raises(ToolFailed, match="unknown arguments"):
        await handlers["remove_trust_rule"]({"description": "last"})
    with pytest.raises(ToolFailed, match="positive integer"):
        await handlers["remove_trust_rule"]({})
    with pytest.raises(ToolFailed, match="no rule 99"):
        await handlers["remove_trust_rule"]({"id": 99})


# --- gating: widening trust asks the owner ------------------------------------------------


@pytest.fixture
def gate(db: sqlite3.Connection) -> PolicyGate:
    kv_set(db, "autonomy", "on")
    reg = ToolRegistry()
    for spec in tools.SPECS:
        reg.register(spec)
    principals = Principals.model_validate({"principals": {"justin": {"role": "owner"}}})
    return PolicyGate(
        db, principals, reg, dry_run=False, approval_ttl_s=1800, actions=summary_actions(db)
    )


def _evaluate(
    db: sqlite3.Connection, gate: PolicyGate, tool: str, args: dict[str, Any], origin: Origin
) -> Decision:
    tid = repo.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    repo.set_status(db, tid, "running")
    spec = next(s for s in tools.SPECS if s.name == tool)
    return gate.evaluate(
        Intent.for_tool(spec, task_id=tid, principal="justin", args=args, origin=origin)
    )


def test_chat_asks_before_widening_trust(db: sqlite3.Connection, gate: PolicyGate) -> None:
    allow = _evaluate(
        db, gate, "allow_trust_rule", {"scope": "dori-staging", "action": "deploy"}, "inferred"
    )
    assert isinstance(allow, NeedsApproval) and not allow.require_code and not allow.allow_always
    assert allow.summary.startswith("allow deploy in dori-staging without asking")
    tighten = {"scope": "trading", "action": "merge", "effect": "ask"}
    assert isinstance(_evaluate(db, gate, "add_trust_rule", tighten, "inferred"), Allow)
    rule = store.add_rule(
        db, scope="trading", action="merge", effect="ask", created_from="chat", now=NOW
    ).rule
    forget = _evaluate(db, gate, "remove_trust_rule", {"id": rule.id}, "inferred")
    assert isinstance(forget, NeedsApproval)
    assert forget.summary.startswith(f"forget rule {rule.id}: ask before merge in trading")
    # /rules forget <n> is the owner's explicit command: it runs.
    assert isinstance(_evaluate(db, gate, "remove_trust_rule", {"id": rule.id}, "explicit"), Allow)
    assert isinstance(_evaluate(db, gate, "list_trust_rules", {}, "inferred"), Allow)
    # Lowering urgency from chat asks too; raising it runs.
    digest = {"scope": "*", "action": "failed", "effect": "notify_digest"}
    lower = _evaluate(db, gate, "allow_trust_rule", digest, "inferred")
    assert isinstance(lower, NeedsApproval)
    assert lower.summary.startswith("stop texting right away on failed everywhere (digest only)")
    now = {"scope": "trading", "action": "done", "effect": "notify_now"}
    assert isinstance(_evaluate(db, gate, "add_trust_rule", now, "inferred"), Allow)


def test_summary_action_ignores_other_tools(db: sqlite3.Connection) -> None:
    spec = tools.ALLOW_TRUST_RULE.model_copy(update={"extension": "x", "source": "ext:x"})
    intent = Intent.for_tool(spec, task_id="t", principal="p", args={"scope": "a", "action": "b"})
    assert tools.summary_action(db, intent) is None
    bad = Intent.for_tool(
        tools.ALLOW_TRUST_RULE, task_id="t", principal="p", args={"scope": "[", "action": "b"}
    )
    assert tools.summary_action(db, bad) is None
    wide = Intent.for_tool(
        tools.ALLOW_TRUST_RULE, task_id="t", principal="p", args={"scope": "*", "action": "*"}
    )
    assert tools.summary_action(db, wide) == "allow anything everywhere without asking"


# --- reports, rendering, intent and /rules -------------------------------------------------


def _intent(tool: str) -> Intent:
    spec = next(s for s in tools.SPECS if s.name == tool)
    return Intent.for_tool(spec, task_id="t", principal="p", args={})


def test_report_lines() -> None:
    added = {"id": 3, "text": "ask before merge in trading", "existed": False, "replaced": None}
    assert report_line(_intent("add_trust_rule"), added) == (
        "Rule added: ask before merge in trading (rule 3)."
    )
    flip = {
        "id": 4,
        "text": "don't ask before merge in trading",
        "existed": False,
        "replaced": {"id": 3, "text": "ask before merge in trading"},
    }
    assert report_line(_intent("allow_trust_rule"), flip) == (
        "Rule added: don't ask before merge in trading (rule 4), replacing rule 3 "
        "(ask before merge in trading)."
    )
    assert report_line(_intent("add_trust_rule"), {**added, "existed": True}) == (
        "Rule already there: ask before merge in trading (rule 3)."
    )
    gone = {"id": 3, "text": "ask before merge in trading", "removed": True}
    assert report_line(_intent("remove_trust_rule"), gone) == (
        "Rule removed: ask before merge in trading (rule 3)."
    )


def test_render_rules() -> None:
    assert render_rules({"rules": [], "count": 0}).startswith("No rules")
    text = render_rules(
        {"rules": [{"id": 1, "text": "ask before merge in trading"}, {"id": 4, "text": "x"}]}
    )
    assert text == "Rules (2):\n1. ask before merge in trading\n4. x\n/rules forget <n> drops one."


def test_rules_command() -> None:
    cmd = commands.get("/rules")
    assert cmd is not None and cmd.owner_only and cmd.tool == "list_trust_rules"
    ctx = ParseContext()
    assert cmd.resolve("", ctx) == (None, "list_trust_rules", {})
    assert cmd.resolve("forget 3", ctx) == ("forget", "remove_trust_rule", {"id": 3})
    assert cmd.resolve("forget #12", ctx) == ("forget", "remove_trust_rule", {"id": 12})
    for bad in ("forget", "forget x", "forget 0", "everything"):
        with pytest.raises(UsageError):
            cmd.resolve(bad, ctx)


def test_rules_allow_command() -> None:
    # GT-1: the explicit path for widening. allow_trust_rule is L2, so the slash command runs.
    cmd = commands.get("/rules")
    assert cmd is not None
    ctx = ParseContext()
    assert cmd.resolve("allow deploy in dori-staging", ctx) == (
        "allow",
        "allow_trust_rule",
        {"scope": "dori-staging", "action": "deploy"},
    )
    assert cmd.resolve("allow merge", ctx) == (
        "allow",
        "allow_trust_rule",
        {"scope": "*", "action": "merge"},
    )
    assert cmd.resolve("allow running github.merge_pr in jhe/dori", ctx) == (
        "allow",
        "allow_trust_rule",
        {"scope": "jhe/dori", "action": "tool:github.merge_pr"},
    )
    for bad in ("allow", "allow [x]", "allow deploy in [x]"):
        with pytest.raises(UsageError):
            cmd.resolve(bad, ctx)


def test_the_claude_brain_is_told_how_to_call_the_rules_tools() -> None:
    """PER-256: what the rules intent's prompt taught the local model is in the Claude
    brain's tool notes now."""
    from resonant.runners.ask import TOOL_NOTES

    for tool in ("allow_trust_rule", "add_trust_rule", "allow_tools", "remove_trust_rule"):
        assert tool in TOOL_NOTES
    assert "allow_trust_rule(scope=dori-staging, action=deploy)" in TOOL_NOTES
    assert "add_trust_rule(scope=trading, action=merge, effect=ask)" in TOOL_NOTES
    assert "effect=notify_now" in TOOL_NOTES

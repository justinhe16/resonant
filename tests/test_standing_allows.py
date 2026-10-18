"""Standing allows for chat tools (GT-1): ``yes always`` on an owner's L2 chat call saves an
``allow`` rule for that tool, after which the owner's chat calls of it run as L3."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from resonant.gate import Allow, ApprovalReply, Intent, NeedsApproval, PolicyGate
from resonant.gate.types import Origin
from resonant.loop import NewTask
from resonant.principals import Principals
from resonant.router.approvals import ALWAYS_UNSUPPORTED, refusal_reply
from resonant.store import approvals as ap
from resonant.store import repos as repos_store
from resonant.store import tasks as repo
from resonant.store import trust as store
from resonant.store.db import kv_set
from resonant.tools import ToolRegistry
from resonant.trust.tools import StandingAllows, tool_action
from resonant_sdk import Effect, Level, ToolSpec

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)

RESTART = ToolSpec(name="restart_resonant", effect=Effect.WRITE, level=Level.L2)
ALLOW_RULE = ToolSpec(name="allow_trust_rule", effect=Effect.WRITE, level=Level.L2)
WIPE = ToolSpec(name="wipe", effect=Effect.WRITE, level=Level.L1)
MERGE_PR = ToolSpec(
    name="merge_pr",
    effect=Effect.WRITE,
    level=Level.L2,
    source="ext:github",
    extension="github",
    args_schema={
        "type": "object",
        "properties": {"repo": {"type": "string"}, "number": {"type": "integer"}},
    },
)
DORI_SYNC = ToolSpec(
    name="sync", effect=Effect.WRITE, level=Level.L2, source="ext:dori", extension="dori"
)
SPECS = (RESTART, ALLOW_RULE, WIPE, MERGE_PR, DORI_SYNC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def principals() -> Principals:
    return Principals.model_validate(
        {
            "principals": {
                "justin": {"role": "owner"},
                "cofounder": {"grants": {"dori": ["request", "approve"]}},
            }
        }
    )


@pytest.fixture
def gate(db: sqlite3.Connection, principals: Principals) -> PolicyGate:
    reg = ToolRegistry()
    for spec in SPECS:
        reg.register(spec)
    kv_set(db, "autonomy", "on")
    return PolicyGate(
        db,
        principals,
        reg,
        dry_run=False,
        approval_ttl_s=1800,
        approvers_for=lambda ext: ["cofounder"] if ext == "dori" else [],
        standing=StandingAllows(db),
        clock=Clock(),
    )


def new_task(db: sqlite3.Connection) -> str:
    tid = repo.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    repo.set_status(db, tid, "running")
    return tid


def call(
    db: sqlite3.Connection,
    spec: ToolSpec,
    *,
    principal: str = "justin",
    origin: Origin = "inferred",
    **args: Any,
) -> Intent:
    return Intent.for_tool(
        spec, task_id=new_task(db), principal=principal, args=args, origin=origin
    )


def allow(db: sqlite3.Connection, scope: str, action: str) -> None:
    store.add_rule(
        db, scope=scope, action=action, effect="allow", created_from="yes always", now=NOW
    )


def sent(db: sqlite3.Connection, approval_id: str, principal: str = "justin") -> None:
    ap.add_channel_ref(db, approval_id, ref="r", at=NOW, principal=principal)


def always(gate: PolicyGate, code: str, actor: str = "justin", event: str = "e1") -> Any:
    return gate.resolve_reply(
        ApprovalReply(
            actor=actor,
            actor_identity=f"imessage:{actor}",
            channel="imessage",
            raw_text="yes always",
            decision="approve",
            path="parser",
            event_id=event,
            occurred_at=NOW + timedelta(seconds=5),
            code=code,
            always=True,
        )
    )


def test_tool_action_names() -> None:
    assert tool_action(None, "restart_resonant") == "tool:restart_resonant"
    assert tool_action("github", "merge_pr") == "tool:github.merge_pr"
    assert tool_action("x" * 40, "y" * 40) is None  # too long for a slug: no standing allow


def test_rule_text_for_a_tool() -> None:
    assert (
        store.describe("jhe/dori", "tool:github.merge_pr", "allow")
        == "don't ask before running github.merge_pr in jhe/dori"
    )


def test_first_call_asks_and_offers_yes_always_with_its_scope(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    d = gate.evaluate(call(db, MERGE_PR, repo="jhe/dori", number=3))
    assert isinstance(d, NeedsApproval) and d.allow_always
    row = ap.get_approval(db, d.approval_id)
    assert row is not None
    assert (row.always_scope, row.always_action) == ("jhe/dori", "tool:github.merge_pr")
    assert "(yes always = don't ask before running github.merge_pr in jhe/dori)" in d.summary


def test_registered_repo_names_the_scope(db: sqlite3.Connection, gate: PolicyGate) -> None:
    repos_store.add(db, "jhe/dori-app", base="main", source="chat", name="dori")
    d = gate.evaluate(call(db, MERGE_PR, repo="jhe/dori-app", number=3))
    assert isinstance(d, NeedsApproval)
    row = ap.get_approval(db, d.approval_id)
    assert row is not None and row.always_scope == "dori"


def test_yes_always_saves_the_rule_and_later_calls_run_as_l3(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    d = gate.evaluate(call(db, MERGE_PR, repo="jhe/dori", number=3))
    assert isinstance(d, NeedsApproval)
    sent(db, d.approval_id)
    assert always(gate, d.code).ok
    (rule,) = store.list_rules(db)
    assert (rule.scope, rule.action, rule.effect, rule.created_from) == (
        "jhe/dori",
        "tool:github.merge_pr",
        "allow",
        "yes always",
    )
    # Another PR in the same repo: no ask.
    assert isinstance(gate.evaluate(call(db, MERGE_PR, repo="jhe/dori", number=4)), Allow)
    # Another repo: still asks (the first ask for that scope).
    assert isinstance(gate.evaluate(call(db, MERGE_PR, repo="jhe/other", number=1)), NeedsApproval)
    # Audited with who approved it.
    row = db.execute(
        "SELECT approved_by FROM audit_log WHERE action = 'trust.rule_added'"
    ).fetchone()
    assert row["approved_by"] == "justin"


def test_an_unscoped_extension_tool_is_scoped_to_its_extension(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    d = gate.evaluate(call(db, DORI_SYNC))
    assert isinstance(d, NeedsApproval)
    row = ap.get_approval(db, d.approval_id)
    assert row is not None and row.always_scope == "dori"


def test_standing_allow_is_never_for_members(db: sqlite3.Connection, gate: PolicyGate) -> None:
    allow(db, "dori", "tool:dori.sync")
    # The owner's chat call runs; the co-founder's still asks, without yes always.
    assert isinstance(gate.evaluate(call(db, DORI_SYNC)), Allow)
    d = gate.evaluate(call(db, DORI_SYNC, principal="cofounder"))
    assert isinstance(d, NeedsApproval) and not d.allow_always


def test_only_the_owner_can_answer_yes_always(db: sqlite3.Connection, gate: PolicyGate) -> None:
    d = gate.evaluate(call(db, DORI_SYNC))
    assert isinstance(d, NeedsApproval) and d.allow_always
    sent(db, d.approval_id, "cofounder")
    result = always(gate, d.code, actor="cofounder")
    assert not result.ok and result.reason == "always_unsupported"
    assert store.list_rules(db) == []


def test_never_for_l1_or_trust_loosening_tools(db: sqlite3.Connection, gate: PolicyGate) -> None:
    allow(db, "*", "tool:wipe")
    allow(db, "*", "tool:allow_trust_rule")
    d = gate.evaluate(call(db, WIPE))
    assert isinstance(d, NeedsApproval) and d.require_code and not d.allow_always
    d = gate.evaluate(call(db, ALLOW_RULE, scope="x", action="y"))
    assert isinstance(d, NeedsApproval) and not d.allow_always


def test_kill_switch_and_explicit_and_scheduled(db: sqlite3.Connection, gate: PolicyGate) -> None:
    allow(db, "*", "tool:restart_resonant")
    assert isinstance(gate.evaluate(call(db, RESTART)), Allow)
    d = gate.evaluate(call(db, RESTART, origin="scheduled"))
    assert isinstance(d, NeedsApproval) and not d.allow_always  # never for scheduled
    kv_set(db, "autonomy", "off")
    d = gate.evaluate(call(db, RESTART))
    assert isinstance(d, NeedsApproval) and not d.allow_always


def test_never_for_a_tainted_call(db: sqlite3.Connection, gate: PolicyGate) -> None:
    """D6: a standing allow needs ``inferred``, so a tainted call of the same tool asks, and
    its prompt offers no ``yes always`` (text the task read can't widen trust)."""
    allow(db, "*", "tool:restart_resonant")
    assert isinstance(gate.evaluate(call(db, RESTART)), Allow)
    d = gate.evaluate(call(db, RESTART, origin="tainted"))
    assert isinstance(d, NeedsApproval) and not d.allow_always
    row = db.execute(
        "SELECT detail FROM audit_log WHERE action = 'gate.evaluate' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    detail = json.loads(row["detail"])
    assert detail["origin"] == "tainted" and detail["standing"] is None


def test_dry_run_wins(db: sqlite3.Connection, gate: PolicyGate) -> None:
    allow(db, "*", "tool:restart_resonant")
    gate.dry_run = True
    assert type(gate.evaluate(call(db, RESTART))).__name__ == "DryRun"


def test_an_ask_rule_still_asks_and_offers_nothing(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    allow(db, "*", "tool:github.merge_pr")
    store.add_rule(db, scope="jhe/dori", action="*", effect="ask", created_from="chat", now=NOW)
    d = gate.evaluate(call(db, MERGE_PR, repo="jhe/dori", number=1))
    assert isinstance(d, NeedsApproval) and not d.allow_always
    # Elsewhere the tool rule holds.
    assert isinstance(gate.evaluate(call(db, MERGE_PR, repo="jhe/x", number=1)), Allow)


def test_a_broad_worker_allow_never_widens_a_tool(db: sqlite3.Connection, gate: PolicyGate) -> None:
    allow(db, "*", "*")
    allow(db, "github", "merge")
    d = gate.evaluate(call(db, MERGE_PR, repo="jhe/dori", number=1))
    assert isinstance(d, NeedsApproval)


def test_rules_forget_removes_a_standing_allow(db: sqlite3.Connection, gate: PolicyGate) -> None:
    allow(db, "*", "tool:restart_resonant")
    assert isinstance(gate.evaluate(call(db, RESTART)), Allow)
    (rule,) = store.list_rules(db)
    store.remove_rule(db, rule.id)
    assert isinstance(gate.evaluate(call(db, RESTART)), NeedsApproval)


def test_always_unsupported_reply_text() -> None:
    assert refusal_reply("always_unsupported", None, []) == f"{ALWAYS_UNSUPPORTED} Reply yes or no."

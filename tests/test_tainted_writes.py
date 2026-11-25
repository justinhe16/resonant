"""PER-335: ``gate.tainted_writes``. ``ask`` (the default) keeps D6: a change chosen after the
turn read outside content asks at L2 and L3. ``allow`` (a self-sufficient team instance) runs
the owner's and an approver's tainted L2/L3 writes as an untainted L3 would; L1 and pay still
ask with the code, an ``ask`` trust rule still asks, a member's call is unchanged, the kill
switch still asks for everything, and ``merge_run`` runs only for a ``merge_standable`` run."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest

from resonant.components import level_overrides, standable
from resonant.config import GateConfig
from resonant.gate import Allow, Decision, Deny, DryRun, Intent, NeedsApproval, PolicyGate
from resonant.gate.policy import TAINT_LIFTABLE_BUILTINS
from resonant.gate.types import Origin
from resonant.loop import NewTask
from resonant.principals import OWNER_TOOLS, Principals
from resonant.store import repos as repos_store
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.store import trust as trust_store
from resonant.store.db import kv_set
from resonant.tools import ToolRegistry
from resonant.tools import runs as runs_tools
from resonant.tools import trust as trust_tools
from resonant.tools.builtin import BUILTIN_MODULES
from resonant.trust.tools import StandingAllows
from resonant_sdk import Effect, Level, ToolSpec

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
OWNER, APPROVER, MEMBER, OUTSIDER = "lead", "second", "member", "outsider"


def _ext(name: str, effect: Effect, level: Level | None) -> ToolSpec:
    return ToolSpec(name=name, effect=effect, level=level, source="ext:ops", extension="ops")


TICKET = _ext("file_ticket", Effect.WRITE, Level.L3)
STAGING = _ext("deploy_staging", Effect.WRITE, Level.L3)
SYNC = _ext("sync", Effect.WRITE, Level.L2)
PROD = _ext("prod_deploy", Effect.WRITE, Level.L1)
CHARGE = _ext("charge", Effect.PAY, None)
# a read with a secret that asks: never lifted
LOOKUP = ToolSpec(name="lookup", effect=Effect.READ, level=Level.L2, secrets=("ops/token",),
                  source="ext:ops", extension="ops")  # fmt: skip
EXT_SPECS = (TICKET, STAGING, SYNC, PROD, CHARGE, LOOKUP)


def principals() -> Principals:
    return Principals.model_validate(
        {
            "principals": {
                OWNER: {"role": "owner"},
                APPROVER: {"role": "approver"},
                MEMBER: {"grants": {"ops": {"request": ["file_ticket"]}}},
                OUTSIDER: {"grants": {"elsewhere": ["request"]}},
            }
        }
    )


def make_gate(
    db: sqlite3.Connection, *, tainted_writes: Any = "allow", dry_run: bool = False,
    standing: bool = True,
) -> PolicyGate:  # fmt: skip
    kv_set(db, "autonomy", "on")
    reg = ToolRegistry()
    for spec in (*trust_tools.SPECS, *runs_tools.SPECS, *EXT_SPECS):
        reg.register(spec)
    return PolicyGate(
        db,
        principals(),
        reg,
        dry_run=dry_run,
        approval_ttl_s=1800,
        levels=level_overrides(db),
        standing=StandingAllows(db, stands=standable(db)) if standing else None,
        tainted_writes=tainted_writes,
    )


@pytest.fixture
def gate(db: sqlite3.Connection) -> PolicyGate:
    return make_gate(db)


def evaluate(
    db: sqlite3.Connection,
    gate: PolicyGate,
    spec: ToolSpec,
    args: dict[str, Any] | None = None,
    *,
    origin: Origin = "tainted",
    principal: str = OWNER,
    capped: bool = False,
) -> Decision:
    tid = task_store.insert_task(db, NewTask(kind="chat", runner="ask", priority=1))
    task_store.set_status(db, tid, "running")
    intent = Intent.for_tool(spec, task_id=tid, principal=principal, args=args or {"x": 1},
                             origin=origin)  # fmt: skip
    return gate.evaluate(intent, capped=capped)


def evaluated(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM audit_log WHERE action = 'gate.evaluate' ORDER BY id")
    return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]


def asks(d: Decision, *, code: bool = False) -> bool:
    return isinstance(d, NeedsApproval) and d.require_code is code and not d.allow_always


def test_the_default_is_ask() -> None:
    assert GateConfig().tainted_writes == "ask"
    assert GateConfig.model_validate({"tainted_writes": "allow"}).tainted_writes == "allow"
    with pytest.raises(ValueError):
        GateConfig.model_validate({"tainted_writes": "yes"})


@pytest.mark.parametrize("setting", ["ask", "garbage", None])
def test_ask_keeps_every_tainted_write_asking(db: sqlite3.Connection, setting: Any) -> None:
    gate = make_gate(db, tainted_writes=setting)
    assert gate.tainted_writes == "ask"  # anything but "allow" fails closed
    assert asks(evaluate(db, gate, STAGING))
    assert asks(evaluate(db, gate, SYNC))
    assert asks(evaluate(db, gate, PROD), code=True)
    # the default's audit rows are as before: no tainted_allowed key at all
    assert all("tainted_allowed" not in r["detail"] for r in evaluated(db))


def test_allow_runs_tainted_l3_and_l2_writes(db: sqlite3.Connection, gate: PolicyGate) -> None:
    l3 = evaluate(db, gate, STAGING)
    assert isinstance(l3, Allow) and l3.unattended and l3.approval_id is None
    l2 = evaluate(db, gate, SYNC)  # runs as L3
    assert isinstance(l2, Allow) and l2.unattended
    rows = evaluated(db)
    assert [(r["decision"], r["detail"]["origin"], r["detail"]["tainted_allowed"])
            for r in rows] == [("Allow", "tainted", True)] * 2  # fmt: skip
    assert [r["level"] for r in rows] == ["L3", "L2"]  # the registered level is recorded


def test_l1_and_pay_always_ask_with_the_code(db: sqlite3.Connection, gate: PolicyGate) -> None:
    for principal in (OWNER, APPROVER):
        for origin in ("tainted", "inferred"):
            assert asks(evaluate(db, gate, PROD, origin=origin, principal=principal), code=True)
            assert asks(evaluate(db, gate, CHARGE, origin=origin, principal=principal),
                        code=True)  # fmt: skip
    assert all("tainted_allowed" not in r["detail"] for r in evaluated(db))


def test_a_read_that_asks_is_not_lifted(db: sqlite3.Connection, gate: PolicyGate) -> None:
    assert asks(evaluate(db, gate, LOOKUP))


def test_an_ask_rule_still_wins(db: sqlite3.Connection, gate: PolicyGate) -> None:
    trust_store.add_rule(db, scope="ops", action="*", effect="ask", created_from="chat", now=NOW)
    assert asks(evaluate(db, gate, STAGING))
    assert asks(evaluate(db, gate, SYNC))
    # an untainted L3 still runs: the rule only stops the lift (as before for standing allows)
    assert isinstance(evaluate(db, gate, STAGING, origin="inferred"), Allow)


def test_autonomy_off_dry_run_and_the_cap_still_ask(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    assert isinstance(evaluate(db, gate, STAGING, capped=True), NeedsApproval)
    kv_set(db, "autonomy", "off")
    assert asks(evaluate(db, gate, STAGING))
    assert asks(evaluate(db, gate, SYNC))
    dry = make_gate(db, dry_run=True)  # make_gate turns autonomy back on
    assert isinstance(evaluate(db, dry, STAGING), DryRun)


def test_approvers_run_members_are_unchanged(db: sqlite3.Connection, gate: PolicyGate) -> None:
    assert isinstance(evaluate(db, gate, STAGING, principal=APPROVER), Allow)
    # a member's tainted write asks as before; an ungranted tool is denied as before
    assert asks(evaluate(db, gate, TICKET, principal=MEMBER))
    denied = evaluate(db, gate, STAGING, principal=MEMBER)
    assert isinstance(denied, Deny) and denied.reason == "not permitted to request this tool"
    assert isinstance(evaluate(db, gate, STAGING, principal=OUTSIDER), Deny)


def test_owner_only_builtins_still_ask(db: sqlite3.Connection, gate: PolicyGate) -> None:
    """Trust and admin builtins stay the owner's word per change, tainted or not."""
    d = evaluate(db, gate, trust_tools.ALLOW_TOOLS, {"tools": ["deploy_staging"]})
    assert isinstance(d, NeedsApproval)
    assert trust_store.list_rules(db) == []


def test_without_the_rules_view_it_asks(db: sqlite3.Connection) -> None:
    gate = make_gate(db, standing=False)  # can't check the ask rules: never lift
    assert asks(evaluate(db, gate, STAGING))


def test_an_untainted_call_is_unchanged(db: sqlite3.Connection, gate: PolicyGate) -> None:
    assert asks(evaluate(db, gate, SYNC, origin="inferred")) is False  # it offers yes always
    assert isinstance(evaluate(db, gate, SYNC, origin="inferred"), NeedsApproval)
    assert isinstance(evaluate(db, gate, SYNC, origin="scheduled"), NeedsApproval)
    assert all("tainted_allowed" not in r["detail"] for r in evaluated(db))


# --- merge_run -------------------------------------------------------------------------------


def _run(db: sqlite3.Connection, pr: int, *, repo: str = "site", ci: bool | None = True) -> str:
    if repos_store.find(db, repo) is None:
        repos_store.add(db, f"owner/{repo}", base="main", source="test", name=repo)
        if ci is not None:
            repos_store.set_ci(db, repo, ci)
    run = runs_store.create(db, repo=repo, ask="add a toggle", principal=OWNER,
                            session_id=f"s-{pr}", max_seconds=60)  # fmt: skip
    runs_store.update(db, run.code, status="ready", pr_number=pr, head_sha="a" * 40,
                      branch=f"resonant/{run.code}")  # fmt: skip
    return run.code


@pytest.mark.parametrize("ci", [True, None])
def test_a_clean_run_merges_unattended(
    db: sqlite3.Connection, gate: PolicyGate, ci: bool | None
) -> None:
    """Unattended: the handler merges only on green CI and a CLEAN state (PER-272)."""
    code = _run(db, 41, ci=ci)
    d = evaluate(db, gate, runs_tools.MERGE_RUN, {"code": code})
    assert isinstance(d, Allow) and d.unattended


def test_a_flagged_run_or_an_ask_merge_rule_asks(db: sqlite3.Connection, gate: PolicyGate) -> None:
    flagged = _run(db, 44)
    runs_store.update(db, flagged, flagged_paths=[".github/workflows/ci.yml"])
    assert asks(evaluate(db, gate, runs_tools.MERGE_RUN, {"code": flagged}))
    clean = _run(db, 45)
    trust_store.add_rule(db, scope="site", action="merge", effect="ask", created_from="chat",
                         now=NOW)  # fmt: skip
    assert asks(evaluate(db, gate, runs_tools.MERGE_RUN, {"code": clean}))
    assert asks(evaluate(db, gate, runs_tools.MERGE_RUN, {"code": "R99"}))  # no such run


def test_start_run_is_lifted_but_an_ask_rule_on_its_repo_asks(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    _run(db, 41)
    args = {"repo": "site", "ask": "fix the typo"}
    assert isinstance(evaluate(db, gate, runs_tools.START_RUN, args), Allow)
    trust_store.add_rule(db, scope="site", action="*", effect="ask", created_from="chat", now=NOW)
    assert asks(evaluate(db, gate, runs_tools.START_RUN, args))


# --- which builtins may be lifted (review: nothing that outlives the turn untainted) --------

# Every builtin L2/L3 write outside OWNER_TOOLS, and whether ``allow`` lifts it. A new builtin
# fails this test until it is placed here on purpose (TAINT_LIFTABLE_BUILTINS is an allowlist).
BUILTIN_WRITES: dict[str, bool] = {
    "start_run": True, "cancel_run": True, "merge_run": True,
    "answer_worker": True, "message_worker": True,
    # a stored prompt that later fires as ``scheduled`` (untainted): never lifted
    "add_job": False, "create_watch": False, "pause_job": False, "resume_job": False,
    "delete_job": False, "run_job_now": False, "cancel_watch": False,
    # a CI workflow onto the default branch (no flagged-paths check), the repo set the trust
    # scopes name, restarts, deleting the owner's notes and reminders
    "add_ci": False, "merge_ci_pr": False, "create_repo": False, "add_repo": False,
    "forget_repo": False, "restart_resonant": False, "restart_ollama": False,
    "delete_note": False, "cancel_reminder": False,
    # taint_exempt: ToolCore sends them as ``inferred`` already (PER-245)
    "add_note": False, "add_reminder": False, "remember": False, "forget": False,
}  # fmt: skip


def test_the_builtin_allowlist_is_pinned() -> None:
    found = {
        s.name
        for m in BUILTIN_MODULES
        for s in m.SPECS
        if s.effect is Effect.WRITE
        and s.effective_level in (Level.L2, Level.L3)
        and s.name not in OWNER_TOOLS
    }
    assert found == set(BUILTIN_WRITES)
    assert {n for n, lift in BUILTIN_WRITES.items() if lift} == TAINT_LIFTABLE_BUILTINS
    assert not TAINT_LIFTABLE_BUILTINS & OWNER_TOOLS


@pytest.mark.parametrize("name", sorted(n for n, lift in BUILTIN_WRITES.items() if not lift))
def test_a_builtin_off_the_list_still_asks_when_tainted(db: sqlite3.Connection, name: str) -> None:
    (spec,) = [s for m in BUILTIN_MODULES for s in m.SPECS if s.name == name]
    kv_set(db, "autonomy", "on")
    reg = ToolRegistry()
    reg.register(spec)
    gate = PolicyGate(db, principals(), reg, dry_run=False, approval_ttl_s=1800,
                      standing=StandingAllows(db), tainted_writes="allow")  # fmt: skip
    d = evaluate(db, gate, spec, {"x": 1})
    assert isinstance(d, NeedsApproval), name
    assert all("tainted_allowed" not in r["detail"] for r in evaluated(db))


def test_the_audit_says_tainted_allowed_only_when_it_ran(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    assert isinstance(evaluate(db, gate, STAGING, capped=True), NeedsApproval)
    assert isinstance(evaluate(db, gate, STAGING), Allow)
    assert [r["detail"].get("tainted_allowed") for r in evaluated(db)] == [None, True]

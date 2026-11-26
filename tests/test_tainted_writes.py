"""PER-335: ``gate.tainted_writes``. ``ask`` (the default) keeps D6: a change chosen after the
turn read outside content asks at L2 and L3. ``allow`` (a self-sufficient team instance) runs
the owner's and an approver's tainted L2/L3 writes as an untainted L3 would; L1 and pay still
ask with the code, an ``ask`` trust rule still asks, a member's call is unchanged, the kill
switch still asks for everything, and ``merge_run`` runs only for a ``merge_standable`` run.
PER-348: ``allow`` also runs the same writes untainted (``inferred``, still L2), audited
``staff_allowed``; ``cancel_watch`` lifts either way, ``create_watch`` only untainted."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest

from resonant.components import level_overrides, standable
from resonant.config import GateConfig
from resonant.gate import Allow, Decision, Deny, DryRun, Intent, NeedsApproval, PolicyGate
from resonant.gate.policy import STAFF_LIFTABLE_BUILTINS, TAINT_LIFTABLE_BUILTINS
from resonant.gate.types import Origin, OwnerAsk
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
from resonant.tools import watches as watch_tools
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
                MEMBER: {"grants": {"ops": {"request": ["file_ticket", "sync"]}}},
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
    for spec in (*trust_tools.SPECS, *runs_tools.SPECS, *watch_tools.SPECS, *EXT_SPECS):
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


def test_a_scheduled_call_is_unchanged(db: sqlite3.Connection, gate: PolicyGate) -> None:
    """A job's stored prompt (``scheduled``) is never lifted, tainted or not."""
    assert isinstance(evaluate(db, gate, SYNC, origin="scheduled"), NeedsApproval)
    assert isinstance(evaluate(db, gate, SYNC, origin="scheduled", principal=APPROVER),
                      NeedsApproval)  # fmt: skip
    rows = evaluated(db)
    assert all("tainted_allowed" not in r["detail"] for r in rows)
    assert all("staff_allowed" not in r["detail"] for r in rows)


# --- untainted staff writes (PER-348) --------------------------------------------------------

MERGE_PR = ToolSpec(name="merge_pr", effect=Effect.WRITE, level=Level.L2, source="ext:github",
                    extension="github")  # fmt: skip
SAVE_ISSUE = ToolSpec(name="save_issue", effect=Effect.WRITE, level=Level.L2,
                      source="ext:linear", extension="linear")  # fmt: skip


def _staff_gate(db: sqlite3.Connection, tainted_writes: Any = "allow") -> PolicyGate:
    gate = make_gate(db, tainted_writes=tainted_writes)
    gate.registry.register(MERGE_PR)
    gate.registry.register(SAVE_ISSUE)
    return gate


def staff_allowed(db: sqlite3.Connection) -> list[bool | None]:
    return [r["detail"].get("staff_allowed") for r in evaluated(db)]


@pytest.mark.parametrize("principal", [OWNER, APPROVER])
def test_allow_runs_an_untainted_staff_l2_write(db: sqlite3.Connection, principal: str) -> None:
    """Brian's fresh "merge #159" and "move DORI-1 to In Review" run with no prompt."""
    gate = _staff_gate(db)
    merge = evaluate(db, gate, MERGE_PR, {"repo": "jhe/dori", "number": 159},
                     origin="inferred", principal=principal)  # fmt: skip
    assert isinstance(merge, Allow) and merge.unattended and merge.approval_id is None
    save = evaluate(db, gate, SAVE_ISSUE, {"id": "DORI-1", "state": "In Review"},
                    origin="inferred", principal=principal)  # fmt: skip
    assert isinstance(save, Allow) and save.unattended
    rows = evaluated(db)
    assert [(r["decision"], r["detail"]["origin"], r["level"]) for r in rows] == [
        ("Allow", "inferred", "L2")
    ] * 2
    assert staff_allowed(db) == [True, True]
    assert all("tainted_allowed" not in r["detail"] for r in rows)


def test_ask_keeps_an_untainted_staff_l2_write_asking(db: sqlite3.Connection) -> None:
    """The default: the owner's fresh L2 asks and offers yes always, the audit row as before."""
    gate = _staff_gate(db, tainted_writes="ask")
    d = evaluate(db, gate, MERGE_PR, {"repo": "jhe/dori", "number": 159}, origin="inferred")
    assert isinstance(d, NeedsApproval) and d.allow_always and not d.require_code
    assert asks(evaluate(db, gate, SAVE_ISSUE, {"id": "DORI-1"}, origin="inferred",
                         principal=APPROVER))  # fmt: skip
    rows = evaluated(db)
    assert all("staff_allowed" not in r["detail"] and "tainted_allowed" not in r["detail"]
               for r in rows)  # fmt: skip


def test_an_untainted_member_write_still_asks(db: sqlite3.Connection, gate: PolicyGate) -> None:
    assert asks(evaluate(db, gate, SYNC, origin="inferred", principal=MEMBER))
    assert staff_allowed(db) == [None]


def test_an_untainted_l1_still_asks_with_the_code(db: sqlite3.Connection, gate: PolicyGate) -> None:
    for principal in (OWNER, APPROVER):
        assert asks(evaluate(db, gate, PROD, origin="inferred", principal=principal), code=True)
    assert staff_allowed(db) == [None, None]


def test_untainted_owner_tools_still_ask(db: sqlite3.Connection, gate: PolicyGate) -> None:
    for tool in (trust_tools.ALLOW_TOOLS, trust_tools.ALLOW_TRUST_RULE):
        d = evaluate(db, gate, tool, {"tools": ["deploy_staging"]}, origin="inferred")
        assert isinstance(d, NeedsApproval), tool.name
    assert trust_store.list_rules(db) == []
    assert all(v is None for v in staff_allowed(db))


def test_untainted_kill_switch_dry_run_cap_and_ask_rule(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    assert isinstance(evaluate(db, gate, SYNC, origin="inferred", capped=True), NeedsApproval)
    trust_store.add_rule(db, scope="ops", action="*", effect="ask", created_from="chat", now=NOW)
    assert isinstance(evaluate(db, gate, SYNC, origin="inferred"), NeedsApproval)
    assert isinstance(evaluate(db, gate, SYNC, origin="inferred", principal=APPROVER),
                      NeedsApproval)  # fmt: skip
    trust_store.remove_rule(db, trust_store.list_rules(db)[0].id)
    kv_set(db, "autonomy", "off")
    assert asks(evaluate(db, gate, SYNC, origin="inferred"))
    dry = make_gate(db, dry_run=True)
    assert isinstance(evaluate(db, dry, SYNC, origin="inferred"), DryRun)
    assert all(v is None for v in staff_allowed(db))


def test_an_untainted_l3_and_a_standing_allow_are_not_marked(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    """Both already ran before PER-348: the row says so as before."""
    assert isinstance(evaluate(db, gate, STAGING, origin="inferred"), Allow)
    trust_store.add_rule(db, scope="ops", action="tool:ops.sync", effect="allow",
                         created_from="yes always", now=NOW)  # fmt: skip
    assert isinstance(evaluate(db, gate, SYNC, origin="inferred"), Allow)
    rows = evaluated(db)
    assert staff_allowed(db) == [None, None]
    assert [r["detail"].get("standing") for r in rows] == [None, True]


def test_an_untainted_merge_run_follows_merge_standable(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    clean = _run(db, 51)
    d = evaluate(db, gate, runs_tools.MERGE_RUN, {"code": clean}, origin="inferred",
                 principal=APPROVER)  # fmt: skip
    assert isinstance(d, Allow) and d.unattended
    flagged = _run(db, 52)
    runs_store.update(db, flagged, flagged_paths=[".github/workflows/ci.yml"])
    d = evaluate(db, gate, runs_tools.MERGE_RUN, {"code": flagged}, origin="inferred",
                 principal=APPROVER)  # fmt: skip
    assert isinstance(d, NeedsApproval)
    assert staff_allowed(db) == [True, None]


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
    "start_run": True, "cancel_run": True, "continue_run": True, "merge_run": True,
    "answer_worker": True, "message_worker": True,
    # only stops a watch (PER-348)
    "cancel_watch": True,
    # a stored prompt that later fires as ``scheduled`` (untainted), or an ``ask`` watch's
    # question run as an untainted research ask: never lifted when tainted
    "add_job": False, "create_watch": False, "pause_job": False, "resume_job": False,
    "delete_job": False, "run_job_now": False,
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
    # PER-348: untainted, create_watch too (what staff asked to watch, not planted text)
    assert TAINT_LIFTABLE_BUILTINS | {"create_watch"} == STAFF_LIFTABLE_BUILTINS
    assert not STAFF_LIFTABLE_BUILTINS & OWNER_TOOLS


def _builtin(name: str) -> ToolSpec:
    (spec,) = [s for m in BUILTIN_MODULES for s in m.SPECS if s.name == name]
    return spec


@pytest.mark.parametrize("name", sorted(n for n, lift in BUILTIN_WRITES.items() if not lift))
def test_a_builtin_off_the_list_still_asks_when_tainted(db: sqlite3.Connection, name: str) -> None:
    spec = _builtin(name)
    kv_set(db, "autonomy", "on")
    reg = ToolRegistry()
    reg.register(spec)
    gate = PolicyGate(db, principals(), reg, dry_run=False, approval_ttl_s=1800,
                      standing=StandingAllows(db), tainted_writes="allow")  # fmt: skip
    d = evaluate(db, gate, spec, {"x": 1})
    assert isinstance(d, NeedsApproval), name
    assert all("tainted_allowed" not in r["detail"] for r in evaluated(db))


@pytest.mark.parametrize(
    "name",
    sorted(n for n in BUILTIN_WRITES
           if n not in STAFF_LIFTABLE_BUILTINS and _builtin(n).level is Level.L2),
)  # fmt: skip
def test_a_builtin_off_the_list_still_asks_when_untainted(
    db: sqlite3.Connection, name: str
) -> None:
    spec = _builtin(name)
    kv_set(db, "autonomy", "on")
    reg = ToolRegistry()
    reg.register(spec)
    gate = PolicyGate(db, principals(), reg, dry_run=False, approval_ttl_s=1800,
                      standing=StandingAllows(db), tainted_writes="allow")  # fmt: skip
    for principal in (OWNER, APPROVER):
        d = evaluate(db, gate, spec, {"x": 1}, origin="inferred", principal=principal)
        assert isinstance(d, NeedsApproval), name
    assert all(v is None for v in staff_allowed(db))


WATCH = {"what": "PR 159 merged", "ask": "Is PR 159 merged?"}


def test_an_untainted_create_watch_runs(db: sqlite3.Connection, gate: PolicyGate) -> None:
    for principal in (OWNER, APPROVER):
        made = evaluate(db, gate, watch_tools.CREATE_WATCH, WATCH, origin="inferred",
                        principal=principal)  # fmt: skip
        assert isinstance(made, Allow) and made.unattended
    assert staff_allowed(db) == [True, True]
    # a member's still asks
    assert isinstance(evaluate(db, gate, watch_tools.CREATE_WATCH, WATCH, origin="inferred",
                               principal=MEMBER), Deny | NeedsApproval)  # fmt: skip


def test_a_tainted_create_watch_still_asks(db: sqlite3.Connection, gate: PolicyGate) -> None:
    """Outside text could plant an ``ask`` watch's question, later run as an untainted ask."""
    for principal in (OWNER, APPROVER):
        made = evaluate(db, gate, watch_tools.CREATE_WATCH, WATCH, principal=principal)
        assert asks(made)
    assert all("tainted_allowed" not in r["detail"] for r in evaluated(db))


@pytest.mark.parametrize("origin", ["tainted", "inferred"])
def test_cancel_watch_runs(db: sqlite3.Connection, gate: PolicyGate, origin: Origin) -> None:
    for principal in (OWNER, APPROVER):
        gone = evaluate(db, gate, watch_tools.CANCEL_WATCH, {"id": "W3"}, origin=origin,
                        principal=principal)  # fmt: skip
        assert isinstance(gone, Allow) and gone.unattended
    # L3: untainted it already ran (not marked); tainted it is lifted
    key = "tainted_allowed" if origin == "tainted" else "staff_allowed"
    marks = [True, True] if origin == "tainted" else [None, None]
    assert [r["detail"].get(key) for r in evaluated(db)] == marks


def test_an_owners_direct_ask_is_lifted_first(db: sqlite3.Connection, gate: PolicyGate) -> None:
    """PER-272's ask is not reached: the row says ``lifted``, and no approval row is made."""
    tid = task_store.insert_task(db, NewTask(kind="chat", runner="ask", priority=1))
    task_store.set_status(db, tid, "running")
    intent = Intent.for_tool(SYNC, task_id=tid, principal=OWNER, args={"x": 1},
                             origin="inferred", asked=OwnerAsk("sync it", "evt-1"))  # fmt: skip
    assert isinstance(gate.evaluate(intent), Allow)
    (row,) = evaluated(db)
    assert row["detail"]["owner_asked"] == "lifted" and row["detail"]["staff_allowed"] is True
    assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_an_explicit_call_is_not_marked(db: sqlite3.Connection, gate: PolicyGate) -> None:
    assert isinstance(evaluate(db, gate, SYNC, origin="explicit"), Allow)
    assert staff_allowed(db) == [None]


def test_watch_tools_scope_for_an_ask_rule(db: sqlite3.Connection, gate: PolicyGate) -> None:
    """``Standing.tainted_allows`` scopes a watch tool at ``*``: an ask rule there asks."""
    trust_store.add_rule(db, scope="*", action="tool:create_watch", effect="ask",
                         created_from="chat", now=NOW)  # fmt: skip
    d = evaluate(db, gate, watch_tools.CREATE_WATCH, WATCH, origin="inferred")
    assert isinstance(d, NeedsApproval)
    trust_store.add_rule(db, scope="*", action="tool:cancel_watch", effect="ask",
                         created_from="chat", now=NOW)  # fmt: skip
    assert isinstance(evaluate(db, gate, watch_tools.CANCEL_WATCH, {"id": "W3"}), NeedsApproval)


def test_the_audit_says_tainted_allowed_only_when_it_ran(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    assert isinstance(evaluate(db, gate, STAGING, capped=True), NeedsApproval)
    assert isinstance(evaluate(db, gate, STAGING), Allow)
    assert [r["detail"].get("tainted_allowed") for r in evaluated(db)] == [None, True]

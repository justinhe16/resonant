from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from resonant.api import DaemonState
from resonant.components import build_components
from resonant.config import load_settings
from resonant.executor import Executor, NotAllowedError, ToolFailed, idempotency_key
from resonant.gate import (
    Allow,
    ApprovalResolution,
    Decision,
    Deny,
    DryRun,
    Gate,
    Intent,
    NeedsApproval,
    PolicyGate,
    Veto,
)
from resonant.gate.policy import (
    APPROVAL_EXPIRED,
    APPROVAL_USED,
    NO_TASK,
    NOT_APPROVED,
    autonomy_on,
    rule,
)
from resonant.gate.types import ORIGINS, Origin
from resonant.loop import Loop, NewTask, SlotPool
from resonant.principals import Principals
from resonant.store import approvals as ap
from resonant.store import tasks as repo
from resonant.store.db import kv_set, utcnow
from resonant.tools import STORE_INTERNAL_TOOLS, ToolRegistry, is_store_internal
from resonant_sdk import Effect, Level, ToolSpec

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
TTL_S = 1800.0

PEEK = ToolSpec(name="peek", effect=Effect.READ)
LABEL = ToolSpec(name="label", effect=Effect.WRITE, level=Level.L3)
RESTART = ToolSpec(name="restart_resonant", effect=Effect.WRITE, level=Level.L2)
WIPE = ToolSpec(name="wipe", effect=Effect.WRITE, level=Level.L1)
PAY = ToolSpec(name="pay", effect=Effect.PAY)
# Fixture specs carrying exempt names; P2-7/P2-8 register the real tools.
ADD_NOTE = ToolSpec(name="add_note", effect=Effect.WRITE, level=Level.L3)
DELETE_NOTE = ToolSpec(name="delete_note", effect=Effect.WRITE, level=Level.L2)
CLEAR_NOTES = ToolSpec(name="clear_notes", effect=Effect.WRITE, level=Level.L1)
EXT_ADD_NOTE = ToolSpec(
    name="add_note", effect=Effect.WRITE, level=Level.L3, source="ext:dori", extension="dori"
)
SPECS = (PEEK, LABEL, RESTART, WIPE, PAY, ADD_NOTE, DELETE_NOTE, CLEAR_NOTES, EXT_ADD_NOTE)


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
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    for spec in SPECS:
        reg.register(spec)
    return reg


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_gate(
    db: sqlite3.Connection,
    principals: Principals,
    registry: ToolRegistry,
    clock: Clock,
    *,
    dry_run: bool = False,
    **kw: Any,
) -> PolicyGate:
    return PolicyGate(
        db, principals, registry, dry_run=dry_run, approval_ttl_s=TTL_S, clock=clock, **kw
    )


@pytest.fixture
def gate(
    db: sqlite3.Connection, principals: Principals, registry: ToolRegistry, clock: Clock
) -> PolicyGate:
    kv_set(db, "autonomy", "on")
    return make_gate(db, principals, registry, clock)


@pytest.fixture
def task_id(db: sqlite3.Connection) -> str:
    tid = repo.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    repo.set_status(db, tid, "running")
    return tid


def intent(
    spec: ToolSpec,
    task_id: str,
    *,
    origin: Origin = "inferred",
    principal: str = "justin",
    **args: Any,
) -> Intent:
    return Intent.for_tool(
        spec, task_id=task_id, principal=principal, args=args or {"x": 1}, origin=origin
    )


def audits(db: sqlite3.Connection, action: str) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,)).fetchall()


def approve(gate: PolicyGate, approval_id: str, *, approved: bool = True) -> None:
    result = gate.resolve(ApprovalResolution(approval_id, approved, "justin", "cli", path="cli"))
    assert result.ok, result.reason


# --- the D1 / D7 table -----------------------------------------------------------------

# (level, origin, autonomy on) -> expected verdict. Written out, not derived from `rule`.
TABLE: dict[tuple[Level, Origin, bool], str] = {
    (Level.L0, "explicit", True): "allow",
    (Level.L0, "inferred", True): "allow",
    (Level.L0, "scheduled", True): "allow",
    (Level.L0, "explicit", False): "allow",
    (Level.L0, "inferred", False): "allow",
    (Level.L0, "scheduled", False): "allow",
    (Level.L3, "explicit", True): "allow",
    (Level.L3, "inferred", True): "allow",
    (Level.L3, "scheduled", True): "allow",
    (Level.L3, "explicit", False): "ask",
    (Level.L3, "inferred", False): "ask",
    (Level.L3, "scheduled", False): "ask",
    (Level.L2, "explicit", True): "allow",
    (Level.L2, "inferred", True): "ask",
    (Level.L2, "scheduled", True): "ask",
    (Level.L2, "explicit", False): "ask",
    (Level.L2, "inferred", False): "ask",
    (Level.L2, "scheduled", False): "ask",
    (Level.L1, "explicit", True): "ask_code",
    (Level.L1, "inferred", True): "ask_code",
    (Level.L1, "scheduled", True): "ask_code",
    (Level.L1, "explicit", False): "ask_code",
    (Level.L1, "inferred", False): "ask_code",
    (Level.L1, "scheduled", False): "ask_code",
}


@pytest.mark.parametrize(("key", "verdict"), TABLE.items())
def test_rule_matches_table(key: tuple[Level, Origin, bool], verdict: str) -> None:
    assert rule(*key) == verdict


def test_table_covers_every_origin() -> None:
    assert ORIGINS == ("explicit", "inferred", "scheduled")
    assert set(TABLE) == {(lv, o, a) for lv in Level for o in ORIGINS for a in (True, False)}


@pytest.mark.parametrize("autonomy", [True, False])
@pytest.mark.parametrize("level", list(Level))
def test_scheduled_is_never_explicit(level: Level, autonomy: bool) -> None:
    assert rule(level, "scheduled", autonomy) == rule(level, "inferred", autonomy)
    assert rule(Level.L2, "scheduled", autonomy) != "allow"


def _set_autonomy(db: sqlite3.Connection, value: str) -> None:
    db.execute("DELETE FROM kv WHERE key = 'autonomy'")
    if value == "missing":
        return
    if value == "not-json":
        db.execute("INSERT INTO kv (key, value, updated_at) VALUES ('autonomy', '{oops', 'x')")
        return
    kv_set(db, "autonomy", {"on": "on", "off": "off", "garbage": "ON ", "true": True}[value])


@pytest.mark.parametrize("value", ["on", "off", "garbage", "true", "missing", "not-json"])
def test_kill_switch_fails_closed(db: sqlite3.Connection, value: str) -> None:
    _set_autonomy(db, value)
    assert autonomy_on(db) is (value == "on")


# (spec, exempt from dry_run). Exempt specs only exist above L0.
MATRIX_SPECS = [
    (PEEK, False),
    (LABEL, False),
    (RESTART, False),
    (WIPE, False),
    (PAY, False),
    (ADD_NOTE, True),
    (DELETE_NOTE, True),
    (CLEAR_NOTES, True),
]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("autonomy", ["on", "off", "garbage"])
@pytest.mark.parametrize("origin", ORIGINS)
@pytest.mark.parametrize(("spec", "exempt"), MATRIX_SPECS, ids=lambda v: getattr(v, "name", v))
def test_decision_matrix(
    db: sqlite3.Connection,
    principals: Principals,
    registry: ToolRegistry,
    clock: Clock,
    task_id: str,
    spec: ToolSpec,
    exempt: bool,
    origin: Origin,
    autonomy: str,
    dry_run: bool,
) -> None:
    _set_autonomy(db, autonomy)
    gate = make_gate(db, principals, registry, clock, dry_run=dry_run)
    i = intent(spec, task_id, origin=origin, text="buy milk")
    decision = gate.evaluate(i)

    assert not isinstance(decision, Veto)
    assert is_store_internal(spec) is exempt
    level = spec.effective_level
    if dry_run and not exempt and level is not Level.L0:
        assert decision == DryRun(would=f'{spec.name.replace("_", " ")} text="buy milk"')
        return
    verdict = TABLE[(level, origin, autonomy == "on")]
    if verdict == "allow":
        assert isinstance(decision, Allow) and decision.approval_id is None
        assert gate.consume(decision.token, i.hash)
        return
    assert isinstance(decision, NeedsApproval)
    assert decision.require_code is (verdict == "ask_code")
    assert decision.approvers == ("justin",)
    assert decision.expires_at == NOW + timedelta(seconds=TTL_S)
    assert (decision.effect, decision.level) == (spec.effect, level)
    row = ap.get_approval(db, decision.approval_id)
    assert row is not None and row.code == decision.code and row.requested_by == "justin"


def test_veto_never_returned_and_every_evaluate_audited_once(
    db: sqlite3.Connection, gate: PolicyGate, task_id: str
) -> None:
    origins: list[str] = []
    for spec in SPECS:
        for origin in ("explicit", "inferred"):
            d = gate.evaluate(intent(spec, task_id, origin=origin))
            origins.append(origin)
            assert not isinstance(d, Veto)
    rows = audits(db, "gate.evaluate")
    assert [json.loads(r["detail"])["origin"] for r in rows] == origins


# --- dry_run exemption -----------------------------------------------------------------


def test_exempt_set_is_pinned() -> None:
    pinned = {"add_note", "delete_note", "clear_notes", "add_reminder", "cancel_reminder"}
    assert pinned == STORE_INTERNAL_TOOLS
    for name in ("restart_ollama", "restart_resonant", "update_resonant", "list_notes"):
        assert name not in STORE_INTERNAL_TOOLS


def test_exemption_is_keyed_on_registry_identity() -> None:
    assert is_store_internal(ADD_NOTE)
    assert not is_store_internal(EXT_ADD_NOTE)  # an extension can't borrow the name
    with_secret = ToolSpec(name="add_note", effect=Effect.WRITE, level=Level.L3, secrets=("k",))
    assert not is_store_internal(with_secret)
    not_builtin = ToolSpec(name="add_note", effect=Effect.WRITE, level=Level.L3, source="x")
    assert not is_store_internal(not_builtin)


def test_extension_tool_with_exempt_name_is_dry_run(
    db: sqlite3.Connection, principals: Principals, registry: ToolRegistry, clock: Clock
) -> None:
    kv_set(db, "autonomy", "on")
    gate = make_gate(db, principals, registry, clock, dry_run=True)
    d = gate.evaluate(intent(EXT_ADD_NOTE, "t1", principal="cofounder"))
    assert isinstance(d, DryRun) and d.would.startswith("dori: add note")


def test_registered_store_internal_builtins_declare_no_secrets(db: sqlite3.Connection) -> None:
    settings = load_settings()
    state = DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)), utcnow())
    registry = build_components(settings, db, state).registry
    for spec in registry.specs():
        if spec.extension is None and spec.name in STORE_INTERNAL_TOOLS:
            assert not spec.secrets and is_store_internal(spec)


# --- deny paths ------------------------------------------------------------------------


def test_registration_and_permission(gate: PolicyGate, task_id: str) -> None:
    forged = ToolSpec(name="label", effect=Effect.READ)  # claims to be a read
    assert isinstance(gate.evaluate(intent(forged, task_id)), Deny)
    assert isinstance(gate.evaluate(intent(LABEL, task_id, principal="cofounder")), Deny)
    assert isinstance(gate.evaluate(intent(PEEK, task_id, principal="stranger")), Deny)


@pytest.mark.parametrize(
    "args",
    [
        {"text": "pay \u202eevil"},
        {"text": "a\u200bb"},
        {"text": "bell\x07"},
        {"bad\u2066key": "v"},
        {"items": ["ok", {"deep": "x\u2028y"}]},
    ],
)
def test_unsafe_args_denied_without_echo(
    db: sqlite3.Connection, gate: PolicyGate, task_id: str, args: dict[str, Any]
) -> None:
    for spec in (PEEK, LABEL, WIPE):
        d = gate.evaluate(Intent.for_tool(spec, task_id=task_id, principal="justin", args=args))
        assert isinstance(d, Deny)
        assert "evil" not in d.reason and "key" not in d.reason and "deep" not in d.reason
    assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_unsafe_args_denied_before_dry_run(
    db: sqlite3.Connection, principals: Principals, registry: ToolRegistry, clock: Clock
) -> None:
    gate = make_gate(db, principals, registry, clock, dry_run=True)
    d = gate.evaluate(intent(RESTART, "t1", text="\u202e"))
    assert isinstance(d, Deny)


def test_approval_needs_a_task(db: sqlite3.Connection, gate: PolicyGate) -> None:
    d = gate.evaluate(intent(RESTART, "route:evt1"))
    assert d == Deny(NO_TASK)
    assert isinstance(gate.evaluate(intent(PEEK, "route:evt1")), Allow)


# --- approvals -------------------------------------------------------------------------


def test_reevaluate_reuses_the_pending_approval(
    db: sqlite3.Connection, gate: PolicyGate, task_id: str, clock: Clock
) -> None:
    first = gate.evaluate(intent(RESTART, task_id))
    clock.now += timedelta(minutes=5)
    again = gate.evaluate(intent(RESTART, task_id))
    assert isinstance(first, NeedsApproval) and first == again
    assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    other = gate.evaluate(intent(RESTART, task_id, x=2))  # different args: its own approval
    assert isinstance(other, NeedsApproval) and other.approval_id != first.approval_id
    assert other.code != first.code


def test_l1_explicit_still_needs_the_code(gate: PolicyGate, task_id: str) -> None:
    for spec in (WIPE, PAY, CLEAR_NOTES):
        d = gate.evaluate(intent(spec, task_id, origin="explicit"))
        assert isinstance(d, NeedsApproval) and d.require_code


def test_summary_action_hook(
    db: sqlite3.Connection,
    principals: Principals,
    registry: ToolRegistry,
    clock: Clock,
    task_id: str,
) -> None:
    def clear_action(i: Intent) -> str:
        return "delete all 4 notes"

    kv_set(db, "autonomy", "on")
    gate = make_gate(db, principals, registry, clock, actions={(None, "clear_notes"): clear_action})
    d = gate.evaluate(Intent.for_tool(CLEAR_NOTES, task_id=task_id, principal="justin", args={}))
    assert isinstance(d, NeedsApproval) and d.summary == "delete all 4 notes"


async def test_approval_is_single_use(
    db: sqlite3.Connection, gate: PolicyGate, task_id: str
) -> None:
    calls: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(args)
        return {"ok": True}

    ex = Executor(db, gate, {(None, "restart_resonant"): handler})
    i = intent(RESTART, task_id)
    asked = gate.evaluate(i)
    assert isinstance(asked, NeedsApproval)
    approve(gate, asked.approval_id)

    first = gate.evaluate(i)
    second = gate.evaluate(i)  # approved, unused: a second token for the same approval
    assert isinstance(first, Allow) and first.approval_id == asked.approval_id
    assert isinstance(second, Allow) and second.approval_id == asked.approval_id

    result = await ex.execute(i, first, step_no=1)
    assert result.ok and len(calls) == 1
    row = ap.get_approval(db, asked.approval_id)
    assert row is not None and row.used_at is not None

    with pytest.raises(NotAllowedError):  # a different key, same spent approval
        await ex.execute(i, second, step_no=2)
    assert len(calls) == 1
    assert audits(db, "executor.refused")[-1]["detail"].count("approval already used") == 1
    assert gate.evaluate(i) == Deny(APPROVAL_USED)


async def test_executor_refuses_an_approval_for_another_intent(
    db: sqlite3.Connection, gate: PolicyGate, task_id: str
) -> None:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        return {}

    ex = Executor(db, gate, {(None, "label"): handler})
    asked = gate.evaluate(intent(RESTART, task_id))
    assert isinstance(asked, NeedsApproval)
    approve(gate, asked.approval_id)
    other = intent(LABEL, task_id)
    allow = gate.evaluate(other)
    assert isinstance(allow, Allow)
    smuggled = Allow(allow.token, approval_id=asked.approval_id)
    with pytest.raises(NotAllowedError):
        await ex.execute(other, smuggled, step_no=1)
    row = ap.get_approval(db, asked.approval_id)
    assert row is not None and row.used_at is None


def test_rejection_holds(db: sqlite3.Connection, gate: PolicyGate, task_id: str) -> None:
    kv_set(db, "autonomy", "off")
    i = intent(LABEL, task_id)
    asked = gate.evaluate(i)
    assert isinstance(asked, NeedsApproval)
    approve(gate, asked.approval_id, approved=False)
    assert gate.evaluate(i) == Deny(NOT_APPROVED)
    # Even with a newer row for the intent, the rejection still holds.
    newer = ap.NewApproval(
        task_id, i.hash, "label", Effect.WRITE, Level.L3, "label", ("justin",), "C3", NOW
    )
    ap.insert_approval(db, newer, NOW + timedelta(seconds=1))
    assert gate.evaluate(i) == Deny(NOT_APPROVED)
    kv_set(db, "autonomy", "on")  # L3 would now run, but the owner said no
    assert gate.evaluate(i) == Deny(NOT_APPROVED)


def test_expiry_is_lazy(
    db: sqlite3.Connection, gate: PolicyGate, task_id: str, clock: Clock
) -> None:
    i = intent(RESTART, task_id)
    asked = gate.evaluate(i)
    assert isinstance(asked, NeedsApproval)
    assert [p.approval_id for p in gate.pending_for("justin")] == [asked.approval_id]
    clock.now = asked.expires_at
    assert gate.pending_for("justin") == ()
    assert len(audits(db, "approval.expired")) == 1
    assert gate.evaluate(i) == Deny(APPROVAL_EXPIRED)
    late = gate.resolve(ApprovalResolution(asked.approval_id, True, "justin", "cli", "cli"))
    assert not late.ok and late.reason == "expired"


def test_resolve_checks_approver_and_wakes_the_task(
    db: sqlite3.Connection, gate: PolicyGate, task_id: str
) -> None:
    asked = gate.evaluate(intent(RESTART, task_id))
    assert isinstance(asked, NeedsApproval)
    repo.set_status(db, task_id, "waiting", wait_reason="approval", wake_at=asked.expires_at)

    nope = gate.resolve(ApprovalResolution(asked.approval_id, True, "cofounder", "x", "cli"))
    assert not nope.ok and nope.reason == "not_found"  # same answer as a missing id
    missing = gate.resolve(ApprovalResolution("nope", True, "justin", "cli", "cli"))
    assert not missing.ok and missing.reason == "not_found"

    approve(gate, asked.approval_id)
    task = repo.get_task(db, task_id)
    assert task is not None and task.status == "queued"
    events = repo.pending_events(db, task_id)
    assert [(e.source, e.type) for e in events] == [("gate", "approval.reply")]
    again = gate.resolve(ApprovalResolution(asked.approval_id, True, "justin", "cli", "cli"))
    assert not again.ok and again.reason == "already_resolved"
    rows = audits(db, "approval.resolve")
    assert [r["decision"] for r in rows] == ["refused", "refused", "approved", "refused"]
    assert json.loads(rows[0]["detail"])["reason"] == "not an approver"


def test_batches_are_deferred(gate: PolicyGate) -> None:
    contract: Gate = gate  # PolicyGate satisfies the Gate protocol
    assert contract is gate
    with pytest.raises(NotImplementedError):
        gate.open_batch("justin", [])


def test_tokens_are_single_use_and_hash_bound(gate: PolicyGate, task_id: str) -> None:
    i = intent(PEEK, task_id)
    d: Decision = gate.evaluate(i)
    assert isinstance(d, Allow)
    assert not gate.consume(d.token, intent(PEEK, task_id, x=2).hash)
    d = gate.evaluate(i)
    assert isinstance(d, Allow)
    assert gate.consume(d.token, i.hash) and not gate.consume(d.token, i.hash)


# --- Executor.recorded -----------------------------------------------------------------


async def test_recorded(db: sqlite3.Connection, gate: PolicyGate, task_id: str) -> None:
    outcomes: list[Exception | None] = [ToolFailed("nope"), None]

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        err = outcomes.pop(0)
        if err is not None:
            raise err
        return {"done": 1}

    ex = Executor(db, gate, {(None, "label"): handler})
    i = intent(LABEL, task_id)
    key = idempotency_key(task_id, 1, 0, i.hash)
    assert ex.recorded(key) is None

    allow = gate.evaluate(i)
    assert isinstance(allow, Allow)
    await ex.execute(i, allow, step_no=1)
    failed = ex.recorded(key)
    assert failed is not None and not failed.ok and not failed.ambiguous
    assert failed.error == "ToolFailed: nope"

    allow = gate.evaluate(i)
    assert isinstance(allow, Allow)
    await ex.execute(i, allow, step_no=1)
    done = ex.recorded(key)
    assert done is not None and done.ok and done.replayed and done.data == {"done": 1}

    db.execute("UPDATE executions SET status = 'started' WHERE idempotency_key = ?", (key,))
    started = ex.recorded(key)
    assert started is not None and started.ambiguous and not started.ok


# --- extension approvers (D8) ------------------------------------------------------------

EXT_ROTATE = ToolSpec(
    name="rotate", effect=Effect.WRITE, level=Level.L2, source="ext:dori", extension="dori"
)


def team() -> Principals:
    return Principals.model_validate(
        {
            "principals": {
                "justin": {"role": "owner"},
                "cofounder": {"grants": {"dori": ["request", "approve"]}},
                "viewer": {"grants": {"dori": ["request"]}},  # no approve
                "elsewhere": {"grants": {"trade": ["request", "approve"]}},  # other extension
            }
        }
    )


def team_gate(
    db: sqlite3.Connection, clock: Clock, named: list[str], calls: list[str] | None = None
) -> PolicyGate:
    reg = ToolRegistry()
    for spec in (EXT_ROTATE, RESTART):
        reg.register(spec)
    kv_set(db, "autonomy", "on")

    def approvers_for(ext: str) -> list[str]:
        if calls is not None:
            calls.append(ext)
        return named

    return make_gate(db, team(), reg, clock, approvers_for=approvers_for)


def test_approvers_are_manifest_names_with_the_grant(
    db: sqlite3.Connection, clock: Clock, task_id: str
) -> None:
    named = ["viewer", "ghost", "cofounder", "elsewhere", "cofounder", "justin"]
    gate = team_gate(db, clock, named)
    assert gate.approvers("dori") == ("justin", "cofounder")

    asked = gate.evaluate(intent(EXT_ROTATE, task_id, origin="scheduled"))
    assert isinstance(asked, NeedsApproval) and asked.approvers == ("justin", "cofounder")
    row = ap.get_approval(db, asked.approval_id)
    assert row is not None and row.approvers == ("justin", "cofounder")


def test_an_extension_cant_grant_itself_approvers(
    db: sqlite3.Connection, clock: Clock, task_id: str
) -> None:
    # Naming principals in the manifest grants nothing: only principals.yaml does.
    gate = team_gate(db, clock, ["viewer", "elsewhere", "ghost"])
    asked = gate.evaluate(intent(EXT_ROTATE, task_id))
    assert isinstance(asked, NeedsApproval) and asked.approvers == ("justin",)
    nope = gate.resolve(ApprovalResolution(asked.approval_id, True, "viewer", "x", "cli"))
    assert not nope.ok and nope.reason == "not_found"


def test_default_extension_approvers_are_the_owner(
    db: sqlite3.Connection, principals: Principals, registry: ToolRegistry, clock: Clock
) -> None:
    assert make_gate(db, principals, registry, clock).approvers("dori") == ("justin",)


def test_builtins_never_consult_a_manifest(
    db: sqlite3.Connection, clock: Clock, task_id: str
) -> None:
    calls: list[str] = []
    gate = team_gate(db, clock, ["cofounder"], calls)
    asked = gate.evaluate(intent(RESTART, task_id))
    assert isinstance(asked, NeedsApproval) and asked.approvers == ("justin",)
    assert calls == []


def test_member_approver_resolves_an_extension_approval(
    db: sqlite3.Connection, clock: Clock, task_id: str
) -> None:
    gate = team_gate(db, clock, ["cofounder"])
    asked = gate.evaluate(intent(EXT_ROTATE, task_id, origin="scheduled"))
    assert isinstance(asked, NeedsApproval)
    repo.set_status(db, task_id, "waiting", wait_reason="approval", wake_at=asked.expires_at)
    assert [p.approval_id for p in gate.pending_for("cofounder")] == [asked.approval_id]

    done = gate.resolve(ApprovalResolution(asked.approval_id, True, "cofounder", "cli", "cli"))
    assert done.ok, done.reason
    allowed = gate.evaluate(intent(EXT_ROTATE, task_id, origin="scheduled"))
    assert isinstance(allowed, Allow) and allowed.approval_id == asked.approval_id


@pytest.mark.parametrize("level", [Level.L0, Level.L3])
def test_scheduled_l0_and_l3_run(
    db: sqlite3.Connection, clock: Clock, task_id: str, level: Level
) -> None:
    spec = ToolSpec(
        name="job_cleanup",
        effect=Effect.READ if level is Level.L0 else Effect.WRITE,
        level=level,
        source="ext:dori",
        extension="dori",
    )
    reg = ToolRegistry()
    reg.register(spec)
    kv_set(db, "autonomy", "on")
    gate = make_gate(db, team(), reg, clock)
    assert isinstance(gate.evaluate(intent(spec, task_id, origin="scheduled")), Allow)
    kv_set(db, "autonomy", "off")
    off = gate.evaluate(intent(spec, task_id, origin="scheduled"))
    assert isinstance(off, Allow) is (level is Level.L0)


def test_chat_paths_never_set_scheduled() -> None:
    """Only the scheduler and the job runner may set ``scheduled``; the router and the local
    runner (model and slash-command paths) never do."""
    import resonant.router
    import resonant.runners.local

    router_dir = Path(resonant.router.__file__).parent
    sources = [Path(resonant.runners.local.__file__), *sorted(router_dir.glob("*.py"))]
    for path in sources:
        assert '"scheduled"' not in path.read_text(), path.name


def test_a_bare_string_from_the_seam_is_refused(
    db: sqlite3.Connection, principals: Principals, registry: ToolRegistry, clock: Clock
) -> None:
    def one_name(_ext: str) -> str:
        return "cofounder"  # a str is a Sequence[str]: the type alone can't catch this

    gate = make_gate(db, principals, registry, clock, approvers_for=one_name)
    with pytest.raises(TypeError, match="not a str"):
        gate.approvers("dori")

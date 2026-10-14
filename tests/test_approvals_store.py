from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import pytest

from resonant.gate.types import ResolvedApproval, ResolveResult
from resonant.loop.types import NewTask, TaskStatus
from resonant.store import approvals as ap
from resonant.store import tasks as repo
from resonant.store.db import (
    StoreUnavailableError,
    connect,
    iso,
    latest_version,
    migrate,
    open_db,
    open_existing,
    schema_version,
    transaction,
)
from resonant_sdk import Effect, Level

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
TTL = timedelta(minutes=30)


def _task(db: sqlite3.Connection, status: TaskStatus = "waiting") -> str:
    task_id = repo.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    if status == "waiting":
        repo.set_status(db, task_id, "waiting", wait_reason="approval", wake_at=NOW + TTL)
    elif status != "queued":
        repo.set_status(db, task_id, status)
    return task_id


def _approval(
    db: sqlite3.Connection,
    task_id: str,
    *,
    code: str = "A7",
    intent_hash: str = "h1",
    approvers: tuple[str, ...] = ("justin",),
    expires_at: datetime = NOW + TTL,
    require_code: bool = False,
) -> str:
    spec = ap.NewApproval(
        task_id=task_id,
        intent_hash=intent_hash,
        tool="restart_resonant",
        effect=Effect.WRITE,
        level=Level.L2,
        summary="restart Resonant",
        approvers=approvers,
        code=code,
        expires_at=expires_at,
        require_code=require_code,
        requested_by="justin",
    )
    return ap.insert_approval(db, spec, NOW)


def _status(db: sqlite3.Connection, approval_id: str) -> str:
    a = ap.get_approval(db, approval_id)
    assert a is not None
    return a.status


def _audits(db: sqlite3.Connection, action: str) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM audit_log WHERE action = ?", (action,)).fetchall()


# --- migration 003 ---------------------------------------------------------------------


def _migrations_through(version: int) -> list[tuple[int, str]]:
    """Migrations 001..``version`` as shipped, to build an older store."""
    found = [
        (int(f.name[:3]), f.read_text())
        for f in resources.files("resonant.store.migrations").iterdir()
        if f.name.endswith(".sql")
    ]
    return sorted(m for m in found if m[0] <= version)


def test_migration_003_over_populated_002(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = connect(path)
    assert migrate(conn, _migrations_through(2)) == 2
    task_id = _task(conn)
    ts = iso(NOW)
    conn.execute(
        """INSERT INTO approvals (id, task_id, intent_hash, tool, effect, level, summary,
               approvers, status, expires_at, created_at, require_code, code)
           VALUES ('a1', ?, 'h', 'x', 'write', 'L1', 's', '["justin"]', 'pending', ?, ?,
                   1, 'D7')""",
        (task_id, iso(NOW + TTL), ts),
    )
    conn.close()

    # job-guard and status refuse a store whose schema is behind.
    with pytest.raises(StoreUnavailableError, match="schema v2"):
        open_existing(path)

    conn = open_db(path)
    assert schema_version(conn) == latest_version() >= 3
    a = ap.get_approval(conn, "a1")
    assert a is not None
    assert (a.status, a.code, a.require_code) == ("pending", "D7", True)
    assert a.used_at is None and a.resolved_event_id is None
    assert a.approvers == ("justin",) and a.channel_refs == ()
    conn.close()
    open_existing(path).close()


# --- create and look up ----------------------------------------------------------------


def test_insert_and_lookups(db: sqlite3.Connection) -> None:
    task_id = _task(db)
    aid = _approval(db, task_id, approvers=("justin", "cofounder"))
    a = ap.get_approval(db, aid)
    assert a is not None
    assert (a.task_id, a.tool, a.effect, a.level, a.status) == (
        task_id,
        "restart_resonant",
        Effect.WRITE,
        Level.L2,
        "pending",
    )
    assert a.expires_at == NOW + TTL and a.created_at == NOW

    assert [p.id for p in ap.pending_for(db, "justin", NOW)] == [aid]
    assert [p.id for p in ap.pending_for(db, "cofounder", NOW)] == [aid]
    assert ap.pending_for(db, "stranger", NOW) == []
    assert ap.pending_for(db, "justin", NOW + TTL) == []  # expired, even before expire_due

    pending = ap.pending_for_intent(db, task_id, "h1", NOW)
    assert pending is not None and pending.id == aid
    assert ap.pending_for_intent(db, task_id, "h2", NOW) is None
    by_code = ap.pending_by_code(db, "A7", NOW)
    assert by_code is not None and by_code.id == aid
    assert ap.pending_codes(db) == {"A7"}


def test_one_pending_per_intent_and_unique_codes(db: sqlite3.Connection) -> None:
    task_id = _task(db)
    _approval(db, task_id)
    with pytest.raises(sqlite3.IntegrityError):
        _approval(db, task_id, code="B2")  # same (task, intent)
    with pytest.raises(sqlite3.IntegrityError):
        _approval(db, _task(db), code="A7")  # code taken


def test_channel_refs_keep_first_send(db: sqlite3.Connection) -> None:
    aid = _approval(db, _task(db))
    assert ap.add_channel_ref(db, aid, ref="g1", at=NOW, principal="justin")
    assert not ap.add_channel_ref(db, aid, ref="g1", at=NOW + TTL, principal="justin")
    assert ap.add_channel_ref(db, aid, ref="g2", at=NOW + timedelta(seconds=5), principal="co")
    a = ap.get_approval(db, aid)
    assert a is not None
    assert [r.ref for r in a.channel_refs] == ["g1", "g2"]
    assert a.sent_at("justin") == NOW
    assert a.sent_at("nobody") is None


# --- resolve ---------------------------------------------------------------------------


def test_resolve_approves_once(db: sqlite3.Connection) -> None:
    task_id = _task(db)
    aid = _approval(db, task_id)
    assert ap.resolve(db, aid, approved=True, decided_by="justin", now=NOW, event_id="e1") is None
    a = ap.get_approval(db, aid)
    assert a is not None
    assert (a.status, a.decided_by, a.decided_at, a.resolved_event_id) == (
        "approved",
        "justin",
        NOW,
        "e1",
    )
    again = ap.resolve(db, aid, approved=False, decided_by="justin", now=NOW, event_id="e2")
    assert again == "already_resolved"
    assert _status(db, aid) == "approved"


def test_resolve_reject(db: sqlite3.Connection) -> None:
    aid = _approval(db, _task(db))
    assert ap.resolve(db, aid, approved=False, decided_by="justin", now=NOW) is None
    assert _status(db, aid) == "rejected"


def test_resolve_refuses_expired(db: sqlite3.Connection) -> None:
    task_id = _task(db)
    aid = _approval(db, task_id)
    late = NOW + TTL
    assert ap.resolve(db, aid, approved=True, decided_by="justin", now=late) == "expired"
    assert _status(db, aid) == "pending"  # lazily expired by expire_due, not by resolve
    assert ap.expire_due(db, late) == 1
    assert ap.resolve(db, aid, approved=True, decided_by="justin", now=NOW) == "expired"


def test_resolve_refuses_task_not_waiting(db: sqlite3.Connection) -> None:
    task_id = _task(db, "queued")
    aid = _approval(db, task_id)
    refusal = ap.resolve(db, aid, approved=True, decided_by="justin", now=NOW)
    assert refusal == "task_not_waiting"
    repo.set_status(db, task_id, "running")
    assert ap.resolve(db, aid, approved=True, decided_by="justin", now=NOW) is None


def test_resolve_unknown(db: sqlite3.Connection) -> None:
    assert ap.resolve(db, "nope", approved=True, decided_by="justin", now=NOW) == "not_found"


def test_two_connections_race_one_wins(tmp_path: Path) -> None:
    path = tmp_path / "race.db"
    a, b = open_db(path), open_db(path)
    aid = _approval(a, _task(a))
    with transaction(a):
        first = ap.resolve(a, aid, approved=True, decided_by="justin", now=NOW, event_id="e1")
    with transaction(b):
        second = ap.resolve(b, aid, approved=False, decided_by="cli", now=NOW, event_id="e2")
    assert (first, second) == (None, "already_resolved")
    assert _status(b, aid) == "approved"
    assert [x.id for x in ap.by_resolved_event(b, "e1")] == [aid]
    assert ap.by_resolved_event(b, "e2") == []


def test_by_resolved_event_rebuilds_result(db: sqlite3.Connection) -> None:
    task_id = _task(db)
    aid = _approval(db, task_id)
    ap.resolve(db, aid, approved=True, decided_by="justin", now=NOW, event_id="e1")
    result = ap.resolve_result(ap.by_resolved_event(db, "e1"))
    assert result == ResolveResult(ok=True, resolved=(ResolvedApproval(aid, task_id, "h1", True),))


# --- single use ------------------------------------------------------------------------


def test_mark_used_once(db: sqlite3.Connection) -> None:
    task_id = _task(db)
    aid = _approval(db, task_id)
    assert not ap.mark_used(db, aid, NOW)  # still pending
    assert ap.approved_unused(db, task_id, "h1") is None
    ap.resolve(db, aid, approved=True, decided_by="justin", now=NOW)
    unused = ap.approved_unused(db, task_id, "h1")
    assert unused is not None and unused.id == aid
    # Valid for the life of the task: no expiry check once approved.
    assert ap.expire_due(db, NOW + 2 * TTL) == 0
    assert ap.mark_used(db, aid, NOW)
    assert not ap.mark_used(db, aid, NOW)
    assert ap.approved_unused(db, task_id, "h1") is None
    a = ap.get_approval(db, aid)
    assert a is not None and a.used_at == NOW


# --- expiry ----------------------------------------------------------------------------


def test_expire_due(db: sqlite3.Connection) -> None:
    task_id = _task(db)
    soon = _approval(db, task_id, expires_at=NOW + timedelta(minutes=1))
    later = _approval(db, task_id, code="B2", intent_hash="h2")
    assert ap.expire_due(db, NOW) == 0
    assert ap.expire_due(db, NOW + timedelta(minutes=1)) == 1
    assert (_status(db, soon), _status(db, later)) == ("expired", "pending")
    rows = _audits(db, "approval.expired")
    assert [json.loads(r["detail"])["approval_id"] for r in rows] == [soon]
    assert rows[0]["task_id"] == task_id
    assert ap.pending_codes(db) == {"B2"}  # an expired code is free again


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_orphans_expire_on_save_step(db: sqlite3.Connection, status: TaskStatus) -> None:
    task_id = _task(db, "running")
    aid = _approval(db, task_id)
    other = _approval(db, _task(db), code="B2")
    with transaction(db):
        assert repo.save_step(db, task_id, status=status, checkpoint=None)
    assert (_status(db, aid), _status(db, other)) == ("expired", "pending")
    orphaned = _audits(db, "approval.orphaned")
    assert [json.loads(r["detail"])["approval_id"] for r in orphaned] == [aid]
    # A later yes binds nothing.
    assert ap.resolve(db, aid, approved=True, decided_by="justin", now=NOW) == "expired"


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_orphans_expire_on_set_status(db: sqlite3.Connection, status: TaskStatus) -> None:
    task_id = _task(db)
    aid = _approval(db, task_id)
    assert repo.set_status(db, task_id, status, expect=("waiting",))
    assert _status(db, aid) == "expired"
    assert len(_audits(db, "approval.orphaned")) == 1


def test_non_terminal_and_refused_changes_keep_approvals(db: sqlite3.Connection) -> None:
    task_id = _task(db)
    aid = _approval(db, task_id)
    assert not repo.set_status(db, task_id, "cancelled", expect=("queued",))
    repo.set_status(db, task_id, "queued", expect=("waiting",))
    with transaction(db):
        assert not repo.save_step(db, task_id, status="done", checkpoint=None)  # not running
    assert _status(db, aid) == "pending"
    assert _audits(db, "approval.orphaned") == []


def test_orphans_keep_decided_approvals(db: sqlite3.Connection) -> None:
    task_id = _task(db)
    aid = _approval(db, task_id)
    ap.resolve(db, aid, approved=True, decided_by="justin", now=NOW)
    repo.set_status(db, task_id, "done")
    assert _status(db, aid) == "approved"


def test_orphans_expire_when_crash_recovery_fails_a_task(db: sqlite3.Connection) -> None:
    doomed = _task(db, "running")
    db.execute("UPDATE tasks SET attempts = 4 WHERE id = ?", (doomed,))
    survivor = _task(db, "running")
    a_doomed = _approval(db, doomed)
    a_survivor = _approval(db, survivor, code="B2")
    with transaction(db):
        assert repo.requeue_interrupted(db, max_attempts=5) == (1, 1)
    assert (_status(db, a_doomed), _status(db, a_survivor)) == ("expired", "pending")


# --- wake_resolved ---------------------------------------------------------------------


def _resolved(db: sqlite3.Connection, aid: str) -> ResolveResult:
    assert ap.resolve(db, aid, approved=True, decided_by="justin", now=NOW, event_id="e1") is None
    return ap.resolve_result(ap.by_resolved_event(db, "e1"))


def _gate_events(db: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM events WHERE task_id = ? AND source = 'gate'", (task_id,)
    ).fetchall()


def test_wake_resolved_requeues_waiting_task_once(db: sqlite3.Connection) -> None:
    task_id = _task(db)
    aid = _approval(db, task_id)
    with transaction(db):
        result = _resolved(db, aid)
        assert ap.wake_resolved(db, result, NOW, principal="justin") == 1
    task = repo.get_task(db, task_id)
    assert task is not None
    assert (task.status, task.wait_reason, task.wake_at) == ("queued", None, None)
    (event,) = repo.pending_events(db, task_id)
    assert (event.type, event.principal, event.occurred_at) == ("approval.reply", "justin", NOW)
    assert event.payload == {"approval_id": aid, "approved": True}
    assert event.trace_id == task.trace_id

    # Waking again (a retried resolver) adds no event and changes nothing.
    repo.set_status(db, task_id, "waiting", wait_reason="approval")
    assert ap.wake_resolved(db, result, NOW) == 0
    assert len(_gate_events(db, task_id)) == 1
    task = repo.get_task(db, task_id)
    assert task is not None and task.status == "waiting"


def test_wake_resolved_leaves_running_task(db: sqlite3.Connection) -> None:
    task_id = _task(db, "running")
    aid = _approval(db, task_id)
    result = _resolved(db, aid)
    assert ap.wake_resolved(db, result, NOW) == 0
    task = repo.get_task(db, task_id)
    assert task is not None and task.status == "running"
    # The event is still there, so the step's Wait requeues instead of sleeping.
    assert len(repo.pending_events(db, task_id)) == 1


def test_wake_resolved_ignores_unknown_task(db: sqlite3.Connection) -> None:
    result = ResolveResult(ok=True, resolved=(ResolvedApproval("a", "missing", "h", True),))
    assert ap.wake_resolved(db, result, NOW) == 0
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

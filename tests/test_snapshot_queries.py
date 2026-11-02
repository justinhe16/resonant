"""The store queries behind the dashboard snapshot (PER-276): tasks, run codes, decided
approvals, and the audit rows of runs and tasks."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from resonant.loop.types import NewTask
from resonant.store import approvals as ap
from resonant.store import audit as audit_store
from resonant.store import runs
from resonant.store import tasks as task_store
from resonant.store.audit import audit
from resonant.store.db import utcnow
from resonant_sdk import Effect, Level


def _task(db: sqlite3.Connection) -> str:
    return task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))


def test_recent_tasks_are_active_first_then_newest(db: sqlite3.Connection) -> None:
    done1, done2, queued, running = (_task(db) for _ in range(4))
    task_store.set_status(db, done1, "done")
    task_store.set_status(db, done2, "failed", error="x")
    task_store.set_status(db, running, "running")
    for n, tid in enumerate([done1, done2, queued, running]):  # distinct update times
        db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (f"2026-11-02T10:00:0{n}.000Z", tid)
        )
    ids = [t.id for t in task_store.recent(db, 10)]
    # Active (running, queued) before ended; each group newest update first.
    assert ids == [running, queued, done2, done1]
    assert [t.id for t in task_store.recent(db, 2)] == [running, queued]
    assert task_store.recent(db, 0) == []


def test_run_codes_by_task(db: sqlite3.Connection) -> None:
    t1, t2 = _task(db), _task(db)
    r1 = runs.create(db, repo="", ask="a", principal="j", session_id="s1", max_seconds=60)
    r2 = runs.create(db, repo="", ask="b", principal="j", session_id="s2", max_seconds=60)
    r3 = runs.create(db, repo="", ask="c", principal="j", session_id="s3", max_seconds=60)
    runs.update(db, r1.code, task_id=t1)
    runs.update(db, r2.code, task_id=t2)
    runs.update(db, r3.code, task_id=t2)  # a second run of the task: the newest wins
    assert runs.codes_by_task(db, [t1, t2, "nope"]) == {t1: r1.code, t2: r3.code}
    assert runs.codes_by_task(db, []) == {}


def _approval(db: sqlite3.Connection, task_id: str | None, n: int, at: datetime) -> str:
    return ap.insert_approval(
        db,
        ap.NewApproval(
            task_id=task_id, intent_hash=f"h{n}", tool="t", effect=Effect.WRITE, level=Level.L2,
            summary=f"s{n}", approvers=("justin",), code=f"C{n}",
            expires_at=at + timedelta(minutes=30),
        ),
        at,
    )  # fmt: skip


def test_recent_decided_and_ids_by_task(db: sqlite3.Connection) -> None:
    now = utcnow()
    task = _task(db)
    task_store.set_status(db, task, "waiting", wait_reason="approval")
    pending = _approval(db, task, 1, now)
    yes = _approval(db, task, 2, now - timedelta(minutes=3))
    no = _approval(db, task, 3, now - timedelta(minutes=2))
    old = _approval(db, task, 4, now - timedelta(days=10))
    taskless = _approval(db, None, 5, now - timedelta(minutes=1))
    assert (
        ap.resolve(db, yes, approved=True, decided_by="j", now=now - timedelta(minutes=2)) is None
    )
    assert (
        ap.resolve(db, no, approved=False, decided_by="j", now=now - timedelta(minutes=1)) is None
    )
    ap.expire_one(db, taskless, now, reason="t")
    since = now - timedelta(days=7)
    assert [a.id for a in ap.recent_decided(db, since, 10)] == [taskless, no, yes]
    assert [a.id for a in ap.recent_decided(db, since, 2)] == [taskless, no]
    assert ap.recent_decided(db, now + timedelta(hours=1), 10) == []
    assert ap.ids_by_task(db, [task, "other"], since) == {task: [yes, no, pending]}
    assert old not in ap.ids_by_task(db, [task], since)[task]
    assert ap.ids_by_task(db, [], since) == {}


def test_run_events_are_bounded_by_the_oldest_start_and_the_last_n(db: sqlite3.Connection) -> None:
    t1, t2 = _task(db), _task(db)
    audit(db, "run.start", run="R0")  # before every start of the listed runs, and not listed
    audit(db, "run.start", task_id=t1, run="R1")
    audit(db, "run.start", task_id=t2, run="R2")
    for i in range(20):
        audit(db, f"run.e{i}", run="R1")
    audit(db, "run.ready", run="R2")
    audit(db, "tool.started", run="R2")  # not a run.* row
    audit(db, "run.ready", run="R3")  # not a listed run
    events = audit_store.run_events(db, [("R1", t1), ("R2", t2)], per_run=5)
    assert set(events) == {"R1", "R2"}
    assert [a for _, a in events["R1"]] == [f"run.e{i}" for i in range(15, 20)]  # the last 5
    assert [a for _, a in events["R2"]] == ["run.start", "run.ready"]
    assert all(at <= events["R1"][-1][0] for at, _ in events["R1"])
    assert audit_store.run_events(db, [], per_run=5) == {}
    # A run with no task has no floor: the scan starts at the beginning.
    both = audit_store.run_events(db, [("R0", None), ("R1", t1)], per_run=3)
    assert [a for _, a in both["R0"]] == ["run.start"] and len(both["R1"]) == 3


def test_run_events_scan_is_capped(db: sqlite3.Connection, monkeypatch: object) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr(audit_store, "RUN_EVENT_SCAN", 10)
    for i in range(30):
        audit(db, f"run.e{i}", run="R1")
    [(_, last)] = audit_store.run_events(db, [("R1", None)], per_run=50)["R1"][-1:]
    assert last == "run.e29"
    assert len(audit_store.run_events(db, [("R1", None)], per_run=50)["R1"]) == 10  # the newest 10


def test_task_events(db: sqlite3.Connection) -> None:
    a, b = _task(db), _task(db)
    for i in range(15):
        audit(db, f"x.e{i}", task_id=a)
    audit(db, "y.only", task_id=b)
    audit(db, "no.task")
    events = audit_store.task_events(db, [a, b, "none"], per_task=4)
    assert [x for _, x in events[a]] == [f"x.e{i}" for i in range(11, 15)]
    assert [x for _, x in events[b]] == ["y.only"] and "none" not in events
    assert audit_store.task_events(db, [], per_task=4) == {}

"""PER-259: migration 020 on a realistic store, and the daily prune (``core/prune``): only rows
past retention go, and never one that anything still points at. Temp databases only."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from resonant import store_upkeep
from resonant.api import DaemonState
from resonant.components import Components, build_components
from resonant.config import RetentionConfig, Settings, load_settings
from resonant.executor import CallContext
from resonant.extensions.process import FakeExtProcess
from resonant.extensions.scheduler import CORE_JOBS, CoreDeps, Scheduler
from resonant.loop import Loop, SlotPool
from resonant.ops.fakes import FakeOpsRunner
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store.db import (
    _migrations,  # pyright: ignore[reportPrivateUsage]
    connect,
    iso,
    kv_set,
    migrate,
    open_db,
    schema_version,
)

# 03:30 PDT on Wednesday 2026-10-21 is 10:30 UTC; the prune runs at 04:00 PDT.
NOW = datetime(2026, 10, 21, 11, 0, tzinfo=UTC)
CFG = RetentionConfig()  # 90 / 90 / 365 days
OLD, RECENT = 200, 10  # days before NOW: far past every retention, well inside every one


def ago(days: float) -> str:
    return iso(NOW - timedelta(days=days))


# --- seeding (raw SQL: the point is the rows, not the code that normally writes them) ---------


def task(
    conn: sqlite3.Connection,
    id: str,
    *,
    status: str = "done",
    days: float = OLD,
    reason: str | None = None,
    checkpoint: Any = None,
    origin: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tasks (id, kind, runner, priority, status, wait_reason, checkpoint,"
        " origin_event_id, trace_id, created_at, updated_at)"
        " VALUES (?, 'k', 'r', 5, ?, ?, ?, ?, 'tr', ?, ?)",
        (id, status, reason, json.dumps(checkpoint or {}), origin, ago(days), ago(days)),
    )


def event(
    conn: sqlite3.Connection,
    id: str,
    *,
    task_id: str | None = None,
    days: float = OLD,
    consumed: bool = True,
    source: str = "imessage",
    type: str = "message",
    principal: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (id, task_id, source, type, principal, payload, occurred_at,"
        " received_at, trace_id, consumed_at) VALUES (?, ?, ?, ?, ?, '{}', ?, ?, 'tr', ?)",
        (
            id,
            task_id,
            source,
            type,
            principal,
            ago(days),
            ago(days),
            ago(days) if consumed else None,
        ),
    )


def audit_row(
    conn: sqlite3.Connection, action: str, *, days: float, task_id: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO audit_log (at, action, task_id) VALUES (?, ?, ?)", (ago(days), action, task_id)
    )


def approval(
    conn: sqlite3.Connection, id: str, *, task_id: str | None, resolved_event: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO approvals (id, task_id, intent_hash, tool, effect, level, summary,"
        " status, resolved_event_id, expires_at, created_at)"
        " VALUES (?, ?, 'h', 't', 'write', 'L2', 's', 'approved', ?, ?, ?)",
        (id, task_id, resolved_event, ago(OLD), ago(OLD)),
    )


def execution(conn: sqlite3.Connection, key: str, task_id: str | None) -> None:
    conn.execute(
        "INSERT INTO executions (idempotency_key, task_id, intent_hash, status, started_at)"
        " VALUES (?, ?, 'h', 'succeeded', ?)",
        (key, task_id, ago(OLD)),
    )


def run(conn: sqlite3.Connection, code: str, task_id: str) -> None:
    conn.execute(
        "INSERT INTO runs (code, task_id, principal, repo, ask, session_id, max_seconds,"
        " created_at, updated_at) VALUES (?, ?, 'p', 'repo', 'ask', 's', 60, ?, ?)",
        (code, task_id, ago(OLD), ago(OLD)),
    )
    conn.execute(
        "INSERT INTO run_messages (run_code, text, created_at) VALUES (?, 'hi', ?)",
        (code, ago(OLD)),
    )


def system_text(conn: sqlite3.Connection, ref: str, task_id: str) -> None:
    conn.execute(
        "INSERT INTO system_texts (principal, kind, text, ref, task_id, sent_at)"
        " VALUES ('p', 'run', 't', ?, ?, ?)",
        (ref, task_id, ago(OLD)),
    )


def ext_job(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        "INSERT INTO ext_jobs (ext, job_id, open_task_id) VALUES ('e', 'j', ?)", (task_id,)
    )


def watch(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        "INSERT INTO brain_watches (principal, what, check_kind, op, interval_s, created_at,"
        " expires_at, next_at, open_task_id) VALUES ('p', 'w', 'ask', 'yes', 60, ?, ?, ?, ?)",
        (ago(OLD), ago(OLD), ago(OLD), task_id),
    )


def summary(conn: sqlite3.Connection, through: str) -> None:
    conn.execute(
        "INSERT INTO brain_summaries (principal, summary, through_event_id, source, updated_at)"
        " VALUES ('p', 's', ?, 'model', ?)",
        (through, ago(OLD)),
    )


def ids(conn: sqlite3.Connection, table: str, col: str = "id") -> set[str]:
    return {r[0] for r in conn.execute(f"SELECT {col} FROM {table}")}


def prune(conn: sqlite3.Connection, **kw: Any) -> store_upkeep.PruneResult:
    return store_upkeep.prune(conn, NOW, CFG, **kw)


def assert_intact(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not conn.in_transaction
    assert (
        conn.execute("SELECT 1 FROM kv WHERE key = ?", (store_upkeep.KV_AUDIT_BEFORE,)).fetchone()
        is None
    )


# --- retention -------------------------------------------------------------------------------


def test_only_rows_past_retention_go(db: sqlite3.Connection) -> None:
    for i, days in enumerate((OLD, 91, 89, RECENT, 0)):
        event(db, f"e{i}", days=days)
        task(db, f"t{i}", days=days)
        execution(db, f"x{i}", f"t{i}")
        audit_row(db, f"a{i}", days=days * 4)  # 800, 364 (kept), 356, 40, 0 days
    r = prune(db)
    assert r == store_upkeep.PruneResult(events=2, tasks=2, executions=2, audit=1)
    assert ids(db, "events") == {"e2", "e3", "e4"}
    assert ids(db, "tasks") == {"t2", "t3", "t4"}
    assert ids(db, "executions", "idempotency_key") == {"x2", "x3", "x4"}
    assert ids(db, "audit_log", "action") == {"a1", "a2", "a3", "a4"}
    assert_intact(db)
    assert prune(db) == store_upkeep.PruneResult()  # nothing left to do


def test_settings_bound_the_retention() -> None:
    with pytest.raises(ValueError):
        RetentionConfig(events_days=6)  # the rollup reads 7 days of events
    with pytest.raises(ValueError):
        RetentionConfig(tasks_days=6)
    with pytest.raises(ValueError):
        RetentionConfig(audit_days=29)
    assert load_settings().retention == RetentionConfig(enabled=True)


# --- events ----------------------------------------------------------------------------------


def test_events_that_something_needs_are_kept(db: sqlite3.Connection) -> None:
    task(
        db,
        "open",
        status="queued",
        days=RECENT,
        checkpoint={"deep": [{"x": "e-cp"}]},
        origin="e-origin",
    )
    event(db, "e-unconsumed", consumed=False)
    event(db, "e-open-task", task_id="open")
    event(db, "e-origin")
    event(db, "e-cp")  # named in an open task's checkpoint, nested
    event(db, "e-resolved")
    approval(db, "ap", task_id=None, resolved_event="e-resolved")
    event(db, "e-cursor")
    summary(db, "e-cursor")
    event(db, "e-old-closed", task_id=None)
    task(db, "finished")
    event(db, "e-finished-task", task_id="finished")
    r = prune(db)
    assert ids(db, "events") == {
        "e-unconsumed", "e-open-task", "e-origin", "e-cp", "e-resolved", "e-cursor",
    }  # fmt: skip
    assert r.events == 2  # e-old-closed, e-finished-task
    assert_intact(db)


# --- tasks -----------------------------------------------------------------------------------


def test_tasks_that_something_needs_are_kept(db: sqlite3.Connection) -> None:
    task(db, "open-q", status="queued", days=RECENT, checkpoint={"requested_by": "pointed-at"})
    task(db, "open-old-wait", status="waiting", reason="approval")  # old but open
    task(db, "pointed-at")
    task(db, "has-approval")
    approval(db, "ap", task_id="has-approval")
    task(db, "has-event")
    event(db, "ev", task_id="has-event", consumed=False)  # an event the event prune keeps
    task(db, "has-ext-job")
    ext_job(db, "has-ext-job")
    task(db, "has-watch")
    watch(db, "has-watch")
    task(db, "has-run")
    run(db, "R1", "has-run")
    task(db, "has-text")
    system_text(db, "ref1", "has-text")
    task(db, "goes-failed", status="failed")
    task(db, "goes-cancelled", status="cancelled")
    task(db, "goes-done")
    r = prune(db)
    assert ids(db, "tasks") == {
        "open-q", "open-old-wait", "pointed-at", "has-approval", "has-event", "has-ext-job",
        "has-watch", "has-run", "has-text",
    }  # fmt: skip
    assert r.tasks == 3
    assert db.execute("SELECT COUNT(*) FROM run_messages").fetchone()[0] == 1  # the run's stay
    assert_intact(db)


def test_a_task_goes_once_its_events_do(db: sqlite3.Connection) -> None:
    task(db, "t")
    event(db, "e1", task_id="t")
    event(db, "e2", task_id="t")
    r = prune(db)
    assert (r.events, r.tasks) == (2, 1) and ids(db, "tasks") == set()
    assert_intact(db)


def test_executions_go_with_their_task_and_only_then(db: sqlite3.Connection) -> None:
    task(db, "gone")
    task(db, "kept", status="running", days=RECENT)
    execution(db, "x-gone", "gone")
    execution(db, "x-kept", "kept")
    execution(db, "x-null", None)
    r = prune(db)
    assert (r.tasks, r.executions) == (1, 1)
    assert ids(db, "executions", "idempotency_key") == {"x-kept", "x-null"}
    assert_intact(db)


# --- audit -----------------------------------------------------------------------------------


def test_audit_rows_of_an_open_task_and_newer_rows_stay(db: sqlite3.Connection) -> None:
    task(db, "open", status="running", days=RECENT)
    task(db, "closed")
    audit_row(db, "open-old", days=OLD * 3, task_id="open")
    audit_row(db, "closed-old", days=OLD * 3, task_id="closed")
    audit_row(db, "plain-old", days=OLD * 3)
    audit_row(db, "fresh", days=RECENT)
    r = prune(db)
    assert r.audit == 2
    assert ids(db, "audit_log", "action") == {"open-old", "fresh"}
    assert_intact(db)


def test_a_plain_delete_still_aborts_after_a_prune(db: sqlite3.Connection) -> None:
    audit_row(db, "old", days=OLD * 3)
    audit_row(db, "fresh", days=RECENT)
    prune(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE audit_log SET action = 'x'")


def test_a_failing_audit_chunk_leaves_no_window_open(db: sqlite3.Connection) -> None:
    audit_row(db, "old", days=OLD * 3)
    db.execute(
        "CREATE TRIGGER boom BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="boom"):
        prune(db)
    assert_intact(db)  # rolled back: the key was never committed


# --- chunks, dry run -------------------------------------------------------------------------


def test_chunking_deletes_more_rows_than_one_chunk(db: sqlite3.Connection) -> None:
    for i in range(23):
        task(db, f"t{i}")
        event(db, f"e{i}", task_id=f"t{i}")
        execution(db, f"x{i}", f"t{i}")
        audit_row(db, f"a{i}", days=OLD * 3)
    task(db, "keep", status="queued", days=RECENT)
    event(db, "keep-e", task_id="keep", days=RECENT)
    r = prune(db, chunk=5)
    assert r == store_upkeep.PruneResult(events=23, tasks=23, executions=23, audit=23)
    assert ids(db, "tasks") == {"keep"} and ids(db, "events") == {"keep-e"}
    assert_intact(db)


def test_a_chunk_of_exactly_the_rows_still_finishes(db: sqlite3.Connection) -> None:
    for i in range(10):
        event(db, f"e{i}")
    assert prune(db, chunk=5).events == 10 and ids(db, "events") == set()


def test_dry_run_reports_the_counts_and_deletes_nothing(db: sqlite3.Connection) -> None:
    for i in range(4):
        task(db, f"t{i}")
        event(db, f"e{i}", task_id=f"t{i}")
        execution(db, f"x{i}", f"t{i}")
        audit_row(db, f"a{i}", days=OLD * 3)
    task(db, "keep")
    approval(db, "ap", task_id="keep")
    before = {t: ids(db, t, c) for t, c in (("tasks", "id"), ("events", "id"), ("audit_log", "id"))}
    dry = prune(db, dry_run=True)
    assert dry == store_upkeep.PruneResult(events=4, tasks=4, executions=4, audit=4)
    assert before == {
        t: ids(db, t, c) for t, c in (("tasks", "id"), ("events", "id"), ("audit_log", "id"))
    }
    assert prune(db) == dry  # the real run does exactly what the dry run said


def test_the_references_in_a_checkpoint_are_found_at_any_depth() -> None:
    cp = {"a": "x", "b": ["y", {"c": {"d": ["z", 3, None]}}], "n": 1}
    assert store_upkeep._strings(cp) == {"a", "x", "b", "y", "c", "d", "z", "n"}  # pyright: ignore[reportPrivateUsage]


def test_an_unreadable_checkpoint_does_not_stop_the_prune(db: sqlite3.Connection) -> None:
    task(db, "open", status="queued", days=RECENT)
    db.execute("UPDATE tasks SET checkpoint = 'not json' WHERE id = 'open'")
    event(db, "e")
    assert prune(db).events == 1


# --- the job ---------------------------------------------------------------------------------


def test_the_job_is_registered_and_runs_half_an_hour_after_the_backup() -> None:
    assert any(f.__name__ == "store_upkeep_job" for f in CORE_JOBS)
    assert store_upkeep.schedule("03:30") == "0 4 * * *"
    assert store_upkeep.schedule("23:45") == "15 0 * * *"  # wraps past midnight
    assert store_upkeep.schedule("23:30") == "0 0 * * *"
    assert store_upkeep.schedule("00:00") == "30 0 * * *"


@dataclass
class Harness:
    settings: Settings
    db: sqlite3.Connection
    components: Components

    def deps(self) -> CoreDeps:
        return CoreDeps(self.settings, self.db, self.components, None, None)


@contextlib.asynccontextmanager
async def harness(home: Path, *, dry_run: bool = False, **settings: Any) -> AsyncGenerator[Harness]:
    s = load_settings(home=home, timezone="America/Los_Angeles", dry_run=dry_run, **settings)
    s.principals_path.write_text(
        'principals:\n  justin:\n    role: owner\n    identities: ["imessage:+15551234567"]\n'
    )
    conn = open_db(s.db_path)
    loop = Loop(conn, {}, s.loop, SlotPool(2, 1))
    state = DaemonState(s, conn, loop, started_at=NOW)
    c = build_components(
        s,
        conn,
        state,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(FakeKeychain()),
        ext_runner=FakeExtProcess(),
    )
    try:
        yield Harness(s, conn, c)
    finally:
        conn.close()


CTX = CallContext(secrets={}, idempotency_key="k", task_id="t")


def good_backup(conn: sqlite3.Connection, hours_ago: float) -> None:
    kv_set(conn, "backup.last_ok", {"at": iso(NOW - timedelta(hours=hours_ago)), "file": "f"})


async def test_the_job_deletes_on_its_own_connection_and_audits(resonant_home: Path) -> None:
    async with harness(resonant_home) as h:
        task(h.db, "old")
        event(h.db, "e-old", task_id="old")
        audit_row(h.db, "ancient", days=OLD * 3)
        good_backup(h.db, 5)
        job = store_upkeep.store_upkeep_job(h.deps(), clock=lambda: NOW)
        assert job is not None and job.schedule == "0 4 * * *" and not job.runs_while_paused
        out = await job.run({}, CTX)
        assert out == {"events": 1, "tasks": 1, "executions": 0, "audit": 1}
        assert ids(h.db, "tasks") == set() and ids(h.db, "events") == set()
        [row] = h.db.execute("SELECT detail FROM audit_log WHERE action = 'store.pruned'")
        assert json.loads(row[0]) == out
        assert_intact(h.db)


async def test_the_job_skips_without_a_good_backup_in_the_last_24h(resonant_home: Path) -> None:
    async with harness(resonant_home) as h:
        task(h.db, "old")
        event(h.db, "e-old")
        job = store_upkeep.store_upkeep_job(h.deps(), clock=lambda: NOW)
        assert job is not None
        skipped = {"skipped": "no backup in the last 24h"}
        assert await job.run({}, CTX) == skipped  # none at all
        good_backup(h.db, 25)
        assert await job.run({}, CTX) == skipped  # too old
        assert ids(h.db, "tasks") == {"old"} and ids(h.db, "events") == {"e-old"}
        good_backup(h.db, 23)
        assert (await job.run({}, CTX))["events"] == 1


async def test_without_backups_enabled_there_is_nothing_to_wait_for(resonant_home: Path) -> None:
    async with harness(resonant_home, backup={"enabled": False}) as h:
        event(h.db, "e-old")
        job = store_upkeep.store_upkeep_job(h.deps(), clock=lambda: NOW)
        assert job is not None and (await job.run({}, CTX))["events"] == 1


async def test_the_job_in_dry_run_counts_and_deletes_nothing(resonant_home: Path) -> None:
    async with harness(resonant_home, dry_run=True) as h:
        event(h.db, "e-old")
        good_backup(h.db, 1)
        job = store_upkeep.store_upkeep_job(h.deps(), clock=lambda: NOW)
        assert job is not None
        out = await job.run({}, CTX)
        assert out == {"events": 1, "tasks": 0, "executions": 0, "audit": 0, "dry_run": True}
        assert ids(h.db, "events") == {"e-old"}
        assert (
            h.db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'store.pruned'").fetchone()[
                0
            ]
            == 0
        )


async def test_the_job_is_off_when_retention_is_disabled(resonant_home: Path) -> None:
    async with harness(resonant_home, retention={"enabled": False}) as h:
        assert store_upkeep.store_upkeep_job(h.deps()) is None


async def test_the_scheduler_runs_it_as_core_prune(resonant_home: Path) -> None:
    async with harness(resonant_home) as h:
        event(h.db, "e-old")
        good_backup(h.db, 1)
        kv_set(h.db, "paused", False)
        due = datetime(2026, 10, 21, 11, 0, tzinfo=UTC)  # 04:00 PDT
        s = Scheduler.from_components(
            h.settings,
            h.db,
            h.components,
            started_at=due - timedelta(minutes=1),
            core_jobs=(store_upkeep.store_upkeep_job,),
            clock=lambda: due,
        )
        (job,) = [j for j in s.jobs if j.core]
        assert job.label == "core/prune"
        await s.tick(due)
        await s.drain()
        await s.aclose()
        assert ids(h.db, "events") == set()


# --- migration 020 on a realistic store --------------------------------------------------------

TASK_COLS = (
    "id, kind, runner, extension, principal, lane, priority, status, wait_reason, wake_at,"
    " attempts, step_no, checkpoint, claude_session_id, worktree_path, scope, origin_event_id,"
    " channel_ref, trace_id, result, error, created_at, updated_at"
)
COUNTED = (
    "tasks", "events", "approvals", "executions", "audit_log", "ext_jobs", "runs", "run_messages",
    "system_texts", "brain_summaries", "kv", "spans", "decision_log", "memory",
)  # fmt: skip


def seed_v19(conn: sqlite3.Connection) -> None:
    for i, (status, reason) in enumerate(
        [
            ("queued", None), ("running", None), ("done", None), ("failed", None),
            ("cancelled", None), ("waiting", "approval"), ("waiting", "human_reply"),
            ("waiting", "usage_reset"), ("waiting", "ci"), ("waiting", "veto"),
        ]
    ):  # fmt: skip
        task(conn, f"t{i}", status=status, reason=reason, days=i * 30, origin=f"e{i}")
        if status == "waiting":
            conn.execute(
                "UPDATE tasks SET result = '{\"ok\": 1}', error = 'e', claude_session_id = 's',"
                " scope = 'x', worktree_path = '/w', channel_ref = 'c', extension = 'ext',"
                " principal = 'p', attempts = 2, step_no = 3, wake_at = ? WHERE id = ?",
                (ago(-1), f"t{i}"),
            )
        event(conn, f"e{i}", task_id=f"t{i}" if i % 2 else None, days=i * 20, consumed=i % 3 != 0)
        execution(conn, f"x{i}", f"t{i}")
    for i in range(10, 40):  # github events for the rollup scan
        event(conn, f"g{i}", source="ext:github", type="ext.github.pr_opened", days=i / 4)
    approval(conn, "ap1", task_id="t5", resolved_event="e5")
    approval(conn, "ap2", task_id="t2")
    ext_job(conn, "t1")
    run(conn, "R1", "t2")
    system_text(conn, "ref", "t3")
    summary(conn, "e4")
    for i in range(20):
        audit_row(conn, f"act{i}", days=i * 30, task_id=f"t{i % 10}" if i % 2 else None)
    conn.execute(
        "INSERT INTO spans (id, trace_id, name, started_at) VALUES ('s1', 'tr', 'n', ?)", (ago(1),)
    )
    conn.execute(
        "INSERT INTO decision_log (id, at, topic, decision) VALUES ('d1', ?, 't', 'd')", (ago(1),)
    )
    conn.execute(
        "INSERT INTO memory (scope, key, value, created_at, updated_at)"
        " VALUES ('s', 'k', 'v', ?, ?)",
        (ago(1), ago(1)),
    )


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNTED}


def test_migration_020_applies_to_a_copy_of_a_realistic_v19_store(tmp_path: Path) -> None:
    src = tmp_path / "v19.db"
    conn = connect(src)
    migrate(conn, [m for m in _migrations() if m[0] <= 19])
    assert schema_version(conn) == 19
    seed_v19(conn)
    before_counts = counts(conn)
    before_tasks = [tuple(r) for r in conn.execute(f"SELECT {TASK_COLS} FROM tasks ORDER BY id")]
    assert len(before_tasks) == 10

    copy = tmp_path / "copy.db"
    dst = sqlite3.connect(copy)
    conn.backup(dst)  # the SQLite backup API, like the daily backup
    dst.close()
    conn.close()

    c = connect(copy)
    try:
        assert schema_version(c) == 19
        assert migrate(c) == 22 == schema_version(c)
        assert counts(c) == before_counts
        assert [
            tuple(r) for r in c.execute(f"SELECT {TASK_COLS} FROM tasks ORDER BY id")
        ] == before_tasks
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("PRAGMA integrity_check").fetchall()[0][0] == "ok"
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # foreign keys still point at the new tasks table
        with pytest.raises(sqlite3.IntegrityError):
            event(c, "bogus", task_id="no-such-task")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "DELETE FROM tasks WHERE id = 't2'"
            )  # approvals, executions, runs point at it
        # the new reason is allowed, an invented one is not, and the pairing CHECK holds
        task(c, "res", status="waiting", reason="resource", days=0)
        with pytest.raises(sqlite3.IntegrityError):
            task(c, "bad", status="waiting", reason="nonsense", days=0)
        with pytest.raises(sqlite3.IntegrityError):
            task(c, "bad2", status="queued", reason="resource", days=0)
        with pytest.raises(sqlite3.IntegrityError):
            task(c, "bad3", status="waiting", reason=None, days=0)
        indexes = {
            r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert {"tasks_runnable", "tasks_wake", "events_source_time"} <= indexes
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'tasks_new'").fetchone() is None
        # the prune works on the migrated store and leaves it consistent
        store_upkeep.prune(c, NOW, RetentionConfig(events_days=30, tasks_days=30, audit_days=30))
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        c.close()


# --- review round 1: history tail, per-chunk references, deadline, index ---------------------


def test_the_brain_history_after_its_summary_cursor_is_kept(db: sqlite3.Connection) -> None:
    """``thread_after`` reads every event after the cursor: those exchanges aren't folded into
    the summary yet, so pruning them would lose them. Another principal's are fair game."""
    event(db, "before", principal="p", days=OLD + 2)
    event(db, "cursor", principal="p", days=OLD + 1)
    event(db, "after", principal="p", days=OLD)
    event(db, "other", principal="q", days=OLD)
    summary(db, "cursor")
    prune(db)
    assert ids(db, "events") == {"cursor", "after"}
    assert_intact(db)


def test_each_chunk_sees_references_added_since_the_last(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task opened between chunks that points at an old task keeps it: the references are
    read again inside each chunk's transaction, not once up front."""
    for i in range(4):
        event(db, f"e{i}")
    task(db, "old")
    real = store_upkeep.open_references
    calls = 0

    def spy(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
        nonlocal calls
        calls += 1
        if calls == 2:  # after the first events chunk: a writer opens a task pointing at "old"
            conn.execute("SAVEPOINT w")
            task(conn, "new", status="queued", days=0, checkpoint={"asking_task": "old"})
            conn.execute("RELEASE w")
        return real(conn)

    monkeypatch.setattr(store_upkeep, "open_references", spy)
    r = prune(db, chunk=2)
    assert r.events == 4 and r.tasks == 0
    assert ids(db, "tasks") == {"old", "new"}
    assert_intact(db)


def test_a_passed_deadline_stops_between_chunks(db: sqlite3.Connection) -> None:
    for i in range(6):
        event(db, f"e{i}")
    ticks = iter([0.0, 10.0])  # one chunk, then past the deadline
    r = prune(db, chunk=2, deadline=5.0, monotonic=lambda: next(ticks))
    assert r == store_upkeep.PruneResult(events=2)
    assert len(ids(db, "events")) == 4
    assert_intact(db)
    assert prune(db).events == 4  # the rest goes next run


def test_the_task_prune_finds_a_tasks_events_by_index(db: sqlite3.Connection) -> None:
    """Without ``events_task`` each candidate task (and the FK check on its delete) scans
    every event: minutes on a real backlog, under the write lock."""
    plan = " ".join(
        str(r["detail"])
        for r in db.execute("EXPLAIN QUERY PLAN SELECT 1 FROM events WHERE task_id = ?", ("t",))
    )
    assert "events_task" in plan

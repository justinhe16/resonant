from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from resonant.store.db import kv_get, kv_set, migrate, now_iso, open_db, schema_version, transaction

TABLES = {
    "tasks",
    "events",
    "approvals",
    "executions",
    "audit_log",
    "spans",
    "decision_log",
    "memory",
    "kv",
}


def test_migrate_creates_schema(db: sqlite3.Connection) -> None:
    names = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names >= TABLES
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    v1 = schema_version(open_db(path))
    conn = open_db(path)
    assert migrate(conn) == v1 == schema_version(conn) >= 1


def test_kv_roundtrip(db: sqlite3.Connection) -> None:
    assert kv_get(db, "autonomy", "on") == "on"
    kv_set(db, "autonomy", "off")
    kv_set(db, "autonomy", "on")
    assert kv_get(db, "autonomy") == "on"
    kv_set(db, "reset", {"at": "2026-01-01T00:00:00Z"})
    assert kv_get(db, "reset") == {"at": "2026-01-01T00:00:00Z"}


def test_transaction_rolls_back(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError), transaction(db):
        kv_set(db, "k", 1)
        raise RuntimeError("boom")
    assert kv_get(db, "k") is None


def _insert_task(db: sqlite3.Connection, status: str, wait_reason: str | None) -> None:
    ts = now_iso()
    db.execute(
        "INSERT INTO tasks (id, kind, runner, priority, status, wait_reason, trace_id,"
        " created_at, updated_at) VALUES (?, 'k', 'echo', 1, ?, ?, 't', ?, ?)",
        (f"t-{status}-{wait_reason}", status, wait_reason, ts, ts),
    )


def test_waiting_requires_reason(db: sqlite3.Connection) -> None:
    _insert_task(db, "waiting", "veto")
    _insert_task(db, "queued", None)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(db, "waiting", None)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(db, "running", "approval")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_task(db, "waiting", "bogus")


def test_event_dedupe_key_unique(db: sqlite3.Connection) -> None:
    ts = now_iso()
    row = "INSERT INTO events (id, source, type, dedupe_key, occurred_at, received_at, trace_id)"
    db.execute(row + " VALUES ('e1', 'imessage', 'message', 'im:1', ?, ?, 't')", (ts, ts))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(row + " VALUES ('e2', 'imessage', 'message', 'im:1', ?, ?, 't')", (ts, ts))


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "m.db")
    v = schema_version(conn)
    broken = [(v + 1, "CREATE TABLE half_done (x INTEGER);\nTHIS IS NOT SQL;")]
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, [*[(i, "") for i in range(1, v + 1)], *broken])
    assert not conn.in_transaction
    assert schema_version(conn) == v
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "half_done" not in names


def test_concurrent_migrators_apply_once(tmp_path: Path) -> None:
    path = tmp_path / "race.db"
    a, b = open_db(path), open_db(path)
    v = schema_version(a)
    extra = [*[(i, "") for i in range(1, v + 1)], (v + 1, "CREATE TABLE once (x INTEGER);")]
    # b decided the migration was pending, but a applies it first.
    assert migrate(a, extra) == v + 1
    assert migrate(b, extra) == v + 1
    assert not b.in_transaction


def test_audit_log_is_append_only(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO audit_log (at, action) VALUES (?, 'x')", (now_iso(),))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE audit_log SET action = 'y'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit_log")


def test_transaction_not_reentrant(db: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError, match="reentrant"), transaction(db), transaction(db):
        pass
    assert not db.in_transaction

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from resonant.store.db import (
    FOREIGN_KEYS_OFF,
    kv_get,
    kv_set,
    migrate,
    now_iso,
    open_db,
    schema_version,
    transaction,
)

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


# --- migrations that rebuild a parent table (PER-259) -----------------------------------------

PARENT_CHILD = """\
CREATE TABLE parent (id TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id TEXT REFERENCES parent (id));
INSERT INTO parent VALUES ('p1', 'one');
INSERT INTO child VALUES (1, 'p1');
"""


def _fk_on(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])


def test_foreign_keys_off_migration_rebuilds_a_table_children_point_at(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "fk.db")
    v = schema_version(conn)
    base = [(i, "") for i in range(1, v + 1)]
    rebuild = f"""{FOREIGN_KEYS_OFF}
CREATE TABLE parent_new (id TEXT PRIMARY KEY, label TEXT NOT NULL, extra TEXT);
INSERT INTO parent_new (id, label) SELECT id, label FROM parent;
DROP TABLE parent;
ALTER TABLE parent_new RENAME TO parent;
"""
    assert migrate(conn, [*base, (v + 1, PARENT_CHILD)]) == v + 1
    # the same rebuild without the marker fails: DROP TABLE deletes rows the child points at
    with pytest.raises(sqlite3.IntegrityError):
        migrate(conn, [*base, (v + 1, PARENT_CHILD), (v + 2, rebuild.split("\n", 1)[1])])
    assert schema_version(conn) == v + 1 and _fk_on(conn) and not conn.in_transaction
    assert migrate(conn, [*base, (v + 1, PARENT_CHILD), (v + 2, rebuild)]) == v + 2
    assert _fk_on(conn) and not conn.in_transaction
    assert [tuple(r) for r in conn.execute("SELECT id, label, extra FROM parent")] == [
        ("p1", "one", None)
    ]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):  # the child's foreign key still works
        conn.execute("INSERT INTO child VALUES (2, 'nope')")


def test_foreign_keys_off_migration_with_a_violation_rolls_back(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "fk.db")
    v = schema_version(conn)
    base = [(i, "") for i in range(1, v + 1)]
    migrate(conn, [*base, (v + 1, PARENT_CHILD)])
    bad = f"{FOREIGN_KEYS_OFF}\nINSERT INTO child VALUES (2, 'missing');\nCREATE TABLE half (x);\n"
    with pytest.raises(sqlite3.IntegrityError, match="foreign key"):
        migrate(conn, [*base, (v + 1, PARENT_CHILD), (v + 2, bad)])
    assert schema_version(conn) == v + 1
    assert _fk_on(conn) and not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 1
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "half" not in names


def test_foreign_keys_off_migration_restores_a_connection_that_had_them_off(
    tmp_path: Path,
) -> None:
    conn = open_db(tmp_path / "fk.db")
    v = schema_version(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    migrate(conn, [*[(i, "") for i in range(1, v + 1)], (v + 1, f"{FOREIGN_KEYS_OFF}\nSELECT 1;")])
    assert not _fk_on(conn)


def test_audit_delete_window_only_opens_for_rows_before_the_cutoff(db: sqlite3.Connection) -> None:
    ins = "INSERT INTO audit_log (at, action) VALUES (?, 'x')"
    db.execute(ins, ("2026-01-01T00:00:00.000+00:00",))
    db.execute(ins, ("2026-06-01T00:00:00.000+00:00",))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit_log")  # no window: nothing can go
    with transaction(db):
        kv_set(db, "store.prune_audit_before", "2026-03-01T00:00:00.000+00:00")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM audit_log")  # includes a row newer than the cutoff
        db.execute("DELETE FROM audit_log WHERE at < '2026-03-01'")
        db.execute("DELETE FROM kv WHERE key = 'store.prune_audit_before'")
    assert [r[0] for r in db.execute("SELECT at FROM audit_log")] == [
        "2026-06-01T00:00:00.000+00:00"
    ]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit_log")

from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import pytest
from typer.testing import CliRunner

from resonant.cli import app
from resonant.loop.types import PRIORITY_BACKGROUND, NewTask
from resonant.store import extensions as ext_store
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
from resonant.store.extensions import ExtensionRecord
from resonant.store.tasks import insert_task

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)


def _migrations_through(version: int) -> list[tuple[int, str]]:
    found = [
        (int(f.name[:3]), f.read_text())
        for f in resources.files("resonant.store.migrations").iterdir()
        if f.name.endswith(".sql")
    ]
    return sorted(m for m in found if m[0] <= version)


def _record(name: str = "example", **changes: object) -> ExtensionRecord:
    rec = ExtensionRecord(
        name=name,
        source=f"repo:{name}",
        commit="a1b2c3d4e5f6",
        tree_digest="9f3e01aa" * 8,
        manifest_sha="77" * 32,
        manifest={"version": 1, "name": name, "tools": []},
        installed_at=NOW,
        installed_by="justin",
        argv0_paths={"get_time": "/x/extensions/example/cli.py"},
    )
    return replace(rec, **changes)


def _task(conn: sqlite3.Connection) -> str:
    return insert_task(
        conn,
        NewTask(kind="job", runner="job", priority=PRIORITY_BACKGROUND, extension="example"),
    )


# --- migration ------------------------------------------------------------------------


def test_migration_004_over_populated_003(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = connect(path)
    assert migrate(conn, _migrations_through(3)) == 3
    task_id = "t_old"  # a v3 row (no ``title`` yet: migration 025)
    conn.execute(
        "INSERT INTO tasks (id, kind, runner, extension, priority, checkpoint, trace_id,"
        " created_at, updated_at) VALUES (?, 'job', 'job', 'example', ?, '{}', 'tr', ?, ?)",
        (task_id, PRIORITY_BACKGROUND, iso(NOW), iso(NOW)),
    )
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES ('reminders.seq', '7', ?)", (iso(NOW),)
    )
    conn.close()

    # job-guard and status refuse the v3 store until the daemon migrates it.
    with pytest.raises(StoreUnavailableError, match="schema v3 < v28"):
        open_existing(path)

    conn = open_db(path)
    assert schema_version(conn) == latest_version() == 28
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"extensions", "ext_jobs"} <= tables
    # Existing rows survive.
    assert conn.execute("SELECT id FROM tasks").fetchone()["id"] == task_id
    assert conn.execute("SELECT value FROM kv WHERE key = 'paused'").fetchone()[0] == "false"
    assert ext_store.list_extensions(conn) == [] and ext_store.list_jobs(conn) == []
    conn.close()
    open_existing(path).close()
    open_existing(path, readonly=True).close()


def test_job_guard_fails_closed_on_v3_store(
    resonant_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(resonant_home / "resonant.db")
    migrate(conn, _migrations_through(3))
    conn.close()
    executed: list[list[str]] = []

    def fake_execvp(file: str, args: list[str]) -> None:
        executed.append(list(args))

    monkeypatch.setattr(os, "execvp", fake_execvp)
    runner = CliRunner()
    res = runner.invoke(app, ["job-guard", "--job", "x/y", "--", "echo", "RAN"])
    assert res.exit_code == 75 and executed == []
    assert "schema v3 < v28" in res.output

    open_db(resonant_home / "resonant.db").close()  # the daemon migrates on start
    ok = runner.invoke(app, ["job-guard", "--job", "x/y", "--", "echo", "RAN"])
    assert ok.exit_code == 0 and executed == [["echo", "RAN"]]


# --- extensions -----------------------------------------------------------------------


def test_extension_crud(db: sqlite3.Connection) -> None:
    rec = _record()
    ext_store.save_extension(db, rec)
    assert ext_store.get_extension(db, "example") == rec
    assert ext_store.get_extension(db, "missing") is None

    # An update replaces every field.
    later = NOW + timedelta(days=1)
    updated = _record(commit=None, source="path:/src/example", installed_at=later)
    ext_store.save_extension(db, updated)
    got = ext_store.get_extension(db, "example")
    assert got == updated and got is not None and got.commit is None

    ext_store.save_extension(db, _record("alpha"))
    assert [e.name for e in ext_store.list_extensions(db)] == ["alpha", "example"]


def test_set_mcp_pin_writes_the_schema_and_still_compare_and_sets(db: sqlite3.Connection) -> None:
    pins = {
        "a": {"server": "s", "pin": "p1", "input_schema": {"type": "object"}, "v": 2},
        "b": {"server": "s", "pin": "q1", "input_schema": {"type": "object"}, "v": 2},
    }
    rec = _record(mcp_pins=pins)
    ext_store.save_extension(db, rec)
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    at = rec.installed_at
    assert not ext_store.set_mcp_pin(
        db, "example", "a", installed_at=at, old_pin="stale", pin="p2", version=2
    )
    later = NOW + timedelta(days=1)
    assert not ext_store.set_mcp_pin(
        db, "example", "a", installed_at=later, old_pin="p1", pin="p2", version=2
    )
    assert ext_store.set_mcp_pin(
        db, "example", "a", installed_at=at, old_pin="p1", pin="p2", version=2, input_schema=schema
    )
    got = ext_store.get_extension(db, "example")
    assert got is not None
    assert got.mcp_pins["a"] == {"server": "s", "pin": "p2", "input_schema": schema, "v": 2}
    assert got.mcp_pins["b"] == pins["b"]
    # Without a schema the stored one is kept (the v1 migration).
    assert ext_store.set_mcp_pin(
        db, "example", "b", installed_at=at, old_pin="q1", pin="q2", version=2
    )
    again = ext_store.get_extension(db, "example")
    assert again is not None and again.mcp_pins["b"]["input_schema"] == {"type": "object"}


def test_core_name_is_reserved(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="reserved"):
        ext_store.save_extension(db, _record("core"))


def test_status(db: sqlite3.Connection) -> None:
    ext_store.save_extension(db, _record())
    ext_store.save_extension(db, _record("alpha"))
    assert ext_store.set_status(db, "example", "disabled_tampered") is True
    # Already set: False, so "alert once" callers can key off it.
    assert ext_store.set_status(db, "example", "disabled_tampered") is False
    assert ext_store.set_status(db, "missing", "disabled") is False
    assert [e.name for e in ext_store.list_extensions(db, status="enabled")] == ["alpha"]
    tampered = ext_store.list_extensions(db, status="disabled_tampered")
    assert [e.name for e in tampered] == ["example"]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE extensions SET status = 'bogus' WHERE name = 'alpha'")


def test_delete_removes_jobs(db: sqlite3.Connection) -> None:
    ext_store.save_extension(db, _record())
    ext_store.mark_due(db, "example", "morning-ping", NOW)
    ext_store.mark_due(db, "core", "briefing", NOW)
    assert ext_store.delete_extension(db, "example") is True
    assert ext_store.get_extension(db, "example") is None
    assert [(j.ext, j.job_id) for j in ext_store.list_jobs(db)] == [("core", "briefing")]
    assert ext_store.delete_extension(db, "example") is False


# --- ext_jobs -------------------------------------------------------------------------


def test_mark_due_upserts_and_never_moves_back(db: sqlite3.Connection) -> None:
    s = ext_store.mark_due(db, "example", "morning-ping", NOW)
    assert (s.last_due, s.last_run_at, s.fail_streak, s.open_task_id) == (NOW, None, 0, None)

    later = NOW + timedelta(hours=1)
    assert ext_store.mark_due(db, "example", "morning-ping", later).last_due == later
    # A stale writer can't rewind it.
    assert ext_store.mark_due(db, "example", "morning-ping", NOW).last_due == later
    assert ext_store.get_job(db, "example", "morning-ping") == ext_store.JobState(
        "example", "morning-ping", later, None, None, None, 0
    )
    assert ext_store.get_job(db, "example", "other") is None


def test_gated_run_claims_due_with_its_task(db: sqlite3.Connection) -> None:
    with transaction(db):
        task_id = _task(db)
        ext_store.mark_due(db, "example", "cleanup", NOW, open_task_id=task_id)
    claimed = ext_store.get_job(db, "example", "cleanup")
    assert claimed is not None and claimed.open_task_id == task_id
    # A later mark without a task keeps it open.
    s = ext_store.mark_due(db, "example", "cleanup", NOW + timedelta(hours=1))
    assert s.open_task_id == task_id
    # A stale due time doesn't move the open task pointer.
    stale = ext_store.mark_due(db, "example", "cleanup", NOW, open_task_id=_task(db))
    assert stale.open_task_id == task_id
    # Only real tasks can be referenced.
    with pytest.raises(sqlite3.IntegrityError):
        ext_store.mark_due(db, "example", "other", NOW, open_task_id="no-such-task")


def test_record_run_streak(db: sqlite3.Connection) -> None:
    task_id = _task(db)
    ext_store.mark_due(db, "example", "cleanup", NOW, open_task_id=task_id)
    s = ext_store.record_run(db, "example", "cleanup", at=NOW, outcome="failed", ok=False)
    assert (s.fail_streak, s.last_outcome, s.open_task_id, s.last_due) == (
        1,
        "failed",
        None,
        NOW,
    )
    s = ext_store.record_run(db, "example", "cleanup", at=NOW, outcome="failed", ok=False)
    assert s.fail_streak == 2
    later = NOW + timedelta(minutes=5)
    s = ext_store.record_run(db, "example", "cleanup", at=later, outcome="ok", ok=True)
    assert (s.fail_streak, s.last_outcome, s.last_run_at) == (0, "ok", later)

    # A first-ever run creates the row.
    s = ext_store.record_run(db, "core", "briefing", at=NOW, outcome="failed", ok=False)
    assert (s.last_due, s.fail_streak) == (None, 1)


def test_list_and_prune_jobs(db: sqlite3.Connection) -> None:
    for job in ("b", "a", "c"):
        ext_store.mark_due(db, "example", job, NOW)
    ext_store.mark_due(db, "core", "briefing", NOW)
    assert [j.job_id for j in ext_store.list_jobs(db, "example")] == ["a", "b", "c"]
    assert [(j.ext, j.job_id) for j in ext_store.list_jobs(db)] == [
        ("core", "briefing"),
        ("example", "a"),
        ("example", "b"),
        ("example", "c"),
    ]

    assert ext_store.prune_jobs(db, "example", {"a"}) == 2
    assert [j.job_id for j in ext_store.list_jobs(db, "example")] == ["a"]
    assert ext_store.prune_jobs(db, "example", ()) == 1
    assert ext_store.list_jobs(db, "example") == []
    assert len(ext_store.list_jobs(db)) == 1


def test_migration_005_adds_pulse_items_over_populated_004(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = connect(path)
    assert migrate(conn, _migrations_through(4)) == 4
    conn.close()
    with pytest.raises(StoreUnavailableError, match="schema v4 < v28"):
        open_existing(path)
    conn = open_db(path)
    assert schema_version(conn) == 28
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pulse_items)")}
    assert {"source", "key", "urgency", "fingerprint", "cleared", "told_fingerprint"} <= cols
    conn.close()


# --- ext_panels (migration 026, PER-280) ------------------------------------------------


def test_migration_026_creates_ext_panels_over_a_populated_025(tmp_path: Path) -> None:
    conn = connect(tmp_path / "old.db")
    assert migrate(conn, _migrations_through(25)) == 25
    ext_store.save_extension(conn, _record())
    conn.close()
    conn = open_db(tmp_path / "old.db")
    assert schema_version(conn) == 28
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(ext_panels)")]
    assert cols == [
        "ext",
        "panel_id",
        "data_json",
        "updated_at",
        "bytes",
        "last_run_at",
        "last_ok",
        "last_error",
        "fail_streak",
        "expect_by",
    ]
    assert ext_store.get_extension(conn, "example") is not None  # rows survive
    assert ext_store.list_panels(conn) == []
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ext_panels (ext, panel_id, fail_streak) VALUES ('a', 'b', -1)")
    conn.close()


def test_panel_save_fail_list_and_streak(db: sqlite3.Connection) -> None:
    assert ext_store.get_panel(db, "example", "paper") is None
    later = NOW + timedelta(minutes=15)
    with transaction(db):
        st = ext_store.record_panel_failure(db, "example", "paper", ran_at=NOW, error="boom")
    assert (st.data_json, st.last_ok, st.fail_streak, st.last_error) == (None, False, 1, "boom")
    with transaction(db):
        st = ext_store.save_panel_ok(
            db, "example", "paper", data='{"views":[]}', updated_at=NOW, ran_at=NOW, bytes=14,
            expect_by=later,
        )  # fmt: skip
    assert st.data_json == '{"views":[]}' and st.last_ok is True and st.fail_streak == 0
    assert st.last_error is None and st.expect_by == later and st.bytes == 14
    assert st.updated_at == NOW and st.last_run_at == NOW
    with transaction(db):
        ext_store.record_panel_failure(db, "example", "paper", ran_at=later, error="one")
        st = ext_store.record_panel_failure(db, "example", "paper", ran_at=later, error="two")
    # a failure keeps the last good copy and counts up
    assert st.fail_streak == 2 and st.last_error == "two" and st.last_ok is False
    assert st.data_json == '{"views":[]}' and st.updated_at == NOW and st.expect_by == later
    with transaction(db):
        st = ext_store.save_panel_ok(
            db, "example", "paper", data="{}", updated_at=later, ran_at=later, bytes=2,
            expect_by=later,
        )  # fmt: skip
    assert st.fail_streak == 0 and st.last_error is None
    with transaction(db):
        ext_store.record_panel_failure(db, "other", "z", ran_at=NOW, error="x")
        ext_store.record_panel_failure(db, "example", "alpha", ran_at=NOW, error="x")
    assert [(p.ext, p.panel_id) for p in ext_store.list_panels(db)] == [
        ("example", "alpha"),
        ("example", "paper"),
        ("other", "z"),
    ]
    assert [p.panel_id for p in ext_store.list_panels(db, "example")] == ["alpha", "paper"]


def test_prune_panels_and_delete_extension_cascade(db: sqlite3.Connection) -> None:
    ext_store.save_extension(db, _record())
    with transaction(db):
        for pid in ("a", "b", "c"):
            ext_store.record_panel_failure(db, "example", pid, ran_at=NOW, error="x")
        ext_store.record_panel_failure(db, "keep", "a", ran_at=NOW, error="x")
        assert ext_store.prune_panels(db, "example", ["b"]) == 2
    assert [p.panel_id for p in ext_store.list_panels(db, "example")] == ["b"]
    with transaction(db):
        assert ext_store.prune_panels(db, "example", []) == 1
    assert ext_store.list_panels(db, "example") == []
    with transaction(db):
        ext_store.record_panel_failure(db, "example", "a", ran_at=NOW, error="x")
        assert ext_store.delete_extension(db, "example") is True
    assert [p.ext for p in ext_store.list_panels(db)] == ["keep"]

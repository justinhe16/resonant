"""BK-1: the daily store backup, its retention, the 36h health alert, the scheduler's
run-while-paused rule and ``resonant backup now|list|restore``. Temp homes only."""

from __future__ import annotations

import contextlib
import functools
import os
import sqlite3
import stat
import threading
import time
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from resonant import backup, cli, killswitch, launchd
from resonant.api import DaemonState
from resonant.cli import app
from resonant.components import Components, build_components
from resonant.config import Settings, load_settings
from resonant.extensions.process import FakeExtProcess
from resonant.extensions.scheduler import CORE_JOBS, CoreDeps, Scheduler
from resonant.gateway.channel import MessageRef
from resonant.loop import Loop, SlotPool
from resonant.monitor import HealthMonitor, health_snapshot
from resonant.ops.fakes import FakeOpsRunner
from resonant.router.fast_path import render_status
from resonant.secrets import FakeKeychain, SecretStore
from resonant.status import collect_status
from resonant.store import extensions as ext_store
from resonant.store.db import atomic, iso, kv_get, kv_set, open_db

TZ = "America/Los_Angeles"
# 03:30 PDT on Monday 2026-10-19 is 10:30 UTC.
T0 = datetime(2026, 10, 19, 10, 30, tzinfo=UTC)
PRINCIPALS = """\
principals:
  justin:
    role: owner
    identities: ["imessage:+15551234567"]
"""


def settings_for(home: Path, **backup_cfg: Any) -> Settings:
    return load_settings(home=home, timezone=TZ, backup=backup_cfg)


@pytest.fixture
def home(resonant_home: Path) -> Path:
    return resonant_home


@pytest.fixture
def store(home: Path) -> Iterator[tuple[Settings, sqlite3.Connection]]:
    settings = settings_for(home)
    conn = open_db(settings.db_path)
    with atomic(conn):
        kv_set(conn, "seed", "x" * 1000)
    yield settings, conn
    conn.close()


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def touch_copies(directory: Path, days: list[date]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for d in days:
        (directory / backup.backup_name(d)).write_bytes(b"old")


# --- making a copy -----------------------------------------------------------------------


def test_backup_is_consistent_while_the_daemon_writes(
    store: tuple[Settings, sqlite3.Connection],
) -> None:
    settings, conn = store
    with atomic(conn):
        conn.execute("CREATE TABLE t (n INTEGER PRIMARY KEY, pad TEXT)")
    stop = threading.Event()
    written: list[int] = []

    def writer() -> None:
        w = sqlite3.connect(settings.db_path, isolation_level=None)
        w.execute("PRAGMA busy_timeout=5000")
        n = 0
        while not stop.is_set() or n < 200:
            n += 1
            w.execute("INSERT INTO t (n, pad) VALUES (?, ?)", (n, "p" * 500))
            written.append(n)
            time.sleep(0.001)  # a busy daemon, not a lock hog: record()'s kv write gets in
        w.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        results = [backup.run_backup(conn, settings, now=T0) for _ in range(5)]
    finally:
        stop.set()
        t.join()
    path = results[-1].path
    assert path == settings.backups_dir / "daily" / "resonant-2026-10-19.db"
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700
    assert [p.name for p in path.parent.iterdir() if p.name != ".lock"] == [path.name]
    copy = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        rows = [r[0] for r in copy.execute("SELECT n FROM t ORDER BY n")]
        assert rows == list(range(1, len(rows) + 1))  # a prefix of the writes: no tear
        assert 0 < len(rows) < len(written)  # writes went on during and after the backup
        assert copy.execute("SELECT value FROM kv WHERE key = 'seed'").fetchone() is not None
    finally:
        copy.close()
    last = kv_get(conn, backup.KV_LAST_OK)
    assert last["file"] == path.name and last["at"] == iso(T0)
    (row,) = conn.execute("SELECT * FROM audit_log WHERE action = 'backup.done'").fetchall()[-1:]
    assert '"file": "resonant-2026-10-19.db"' in row["detail"]


def test_a_failed_integrity_check_keeps_the_old_copies(
    store: tuple[Settings, sqlite3.Connection],
) -> None:
    settings, conn = store
    daily = settings.backups_dir / "daily"
    old = [date(2026, 10, 1) + timedelta(days=i) for i in range(12)]
    touch_copies(daily, old)
    before = sorted(p.name for p in daily.iterdir())
    with pytest.raises(backup.BackupError, match="integrity check failed"):
        backup.run_backup(conn, settings, now=T0, check=lambda _c: "*** page 3 is never used")
    assert sorted(p.name for p in daily.iterdir() if p.name != ".lock") == before  # no prune
    assert not list(daily.glob(".*.tmp*"))
    assert kv_get(conn, backup.KV_LAST_OK) is None
    err = kv_get(conn, backup.KV_LAST_ERROR)
    assert err["at"] == iso(T0) and "page 3" in err["error"]
    assert (
        conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'backup.failed'").fetchone()[0]
        == 1
    )


def test_a_missing_store_fails_without_creating_one(home: Path) -> None:
    settings = settings_for(home)
    with pytest.raises(backup.BackupError, match="no store"):
        backup.create_backup(settings, now=T0)
    assert not settings.db_path.exists()


def test_a_stale_temp_file_from_a_crash_is_replaced(
    store: tuple[Settings, sqlite3.Connection],
) -> None:
    settings, conn = store
    daily = settings.backups_dir / "daily"
    daily.mkdir(parents=True)
    (daily / ".resonant-2026-10-19.db.tmp").write_bytes(b"torn")
    backup.run_backup(conn, settings, now=T0)
    assert not (daily / ".resonant-2026-10-19.db.tmp").exists()


def test_a_second_backup_at_once_is_skipped_not_failed(
    store: tuple[Settings, sqlite3.Connection],
) -> None:
    settings, conn = store
    daily = settings.backups_dir / "daily"
    locked = backup._locked(daily)  # pyright: ignore[reportPrivateUsage]
    with locked, pytest.raises(backup.BackupBusy, match="another backup"):
        backup.run_backup(conn, settings, now=T0)
    assert kv_get(conn, backup.KV_LAST_ERROR) is None  # not a failure: no alert noise
    assert (
        conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'backup.failed'").fetchone()[0]
        == 0
    )


def test_a_prune_failure_keeps_the_good_backup(
    store: tuple[Settings, sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, conn = store

    def broken(directory: Path, **_kw: int) -> list[Path]:
        raise PermissionError("nope")

    monkeypatch.setattr(backup, "prune", broken)
    result = backup.run_backup(conn, settings, now=T0)
    assert result.prune_error == "PermissionError: nope"
    assert kv_get(conn, backup.KV_LAST_OK)["file"] == "resonant-2026-10-19.db"


def test_an_unexpected_error_is_recorded(
    store: tuple[Settings, sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, conn = store

    def boom(_now: datetime, _tz: str) -> date:
        raise KeyError("tz")

    monkeypatch.setattr(backup, "local_day", boom)
    with pytest.raises(backup.BackupError, match="KeyError"):
        backup.run_backup(conn, settings, now=T0)
    assert "KeyError" in kv_get(conn, backup.KV_LAST_ERROR)["error"]


# --- retention ---------------------------------------------------------------------------


def test_keep_days_is_seven_daily_plus_four_sundays() -> None:
    days = [date(2026, 10, 19) - timedelta(days=i) for i in range(60)]
    keep = backup.keep_days(days, keep_daily=7, keep_weekly=4)
    daily = {date(2026, 10, 19) - timedelta(days=i) for i in range(7)}  # 13..19 (incl. Sun 18)
    sundays = {date(2026, 10, 18), date(2026, 10, 11), date(2026, 10, 4), date(2026, 9, 27)}
    assert keep == daily | sundays
    assert len(keep) == 10  # Sunday the 18th is in both
    assert backup.keep_days([], keep_daily=7, keep_weekly=4) == set()
    assert backup.keep_days(days, keep_daily=1, keep_weekly=0) == {date(2026, 10, 19)}
    gaps = [date(2026, 10, 19), date(2026, 10, 1), date(2026, 9, 1)]
    assert backup.keep_days(gaps, keep_daily=7, keep_weekly=4) == set(gaps)


def test_prune_never_touches_anything_but_daily_copies(home: Path) -> None:
    daily = home / "backups" / "daily"
    days = [date(2026, 10, 19) - timedelta(days=i) for i in range(30)]
    touch_copies(daily, days)
    keepers = [
        "resonant.db.pre-migration-018",
        "resonant-2026-01-01.db.bak",
        "resonant-2026-13-45.db",  # not a date
        "notes.txt",
    ]
    for name in keepers:
        (daily / name).write_bytes(b"keep")
    (home / "elsewhere.db").write_bytes(b"x")
    (daily / "resonant-2020-01-05.db").symlink_to(home / "elsewhere.db")  # a Sunday link
    removed = backup.prune(daily, keep_daily=7, keep_weekly=4)
    left = {p.name for p in daily.iterdir()}
    assert set(keepers) <= left and "resonant-2020-01-05.db" in left
    kept = {backup.backup_name(d) for d in backup.keep_days(days, keep_daily=7, keep_weekly=4)}
    assert {p.name for p in removed} == {backup.backup_name(d) for d in days} - kept
    assert left == kept | set(keepers) | {"resonant-2020-01-05.db"}


def test_a_good_backup_prunes_with_the_configured_retention(home: Path) -> None:
    settings = settings_for(home, keep_daily=2, keep_weekly=1)
    conn = open_db(settings.db_path)
    try:
        daily = settings.backups_dir / "daily"
        touch_copies(daily, [date(2026, 10, 18), date(2026, 10, 17), date(2026, 10, 11)])
        result = backup.run_backup(conn, settings, now=T0)
        assert sorted(result.pruned) == ["resonant-2026-10-11.db", "resonant-2026-10-17.db"]
        assert [p.name for _, p in backup.copies(daily)] == [
            "resonant-2026-10-19.db",
            "resonant-2026-10-18.db",
        ]
    finally:
        conn.close()


def test_extra_dir_gets_a_verified_copy_and_a_missing_one_is_reported(home: Path) -> None:
    extra = home.parent / "icloud"
    settings = settings_for(home, extra_dir=str(extra))
    conn = open_db(settings.db_path)
    try:
        result = backup.run_backup(conn, settings, now=T0)
        assert result.extra is None and result.extra_error is not None
        assert "does not exist" in result.extra_error
        assert not extra.exists()  # never created: an unmounted disk stays visible
        assert kv_get(conn, backup.KV_LAST_OK) is not None  # the main copy still counts
        assert kv_get(conn, backup.KV_EXTRA_ERROR)["error"] == result.extra_error
        extra.mkdir()
        result = backup.run_backup(conn, settings, now=T0 + timedelta(days=1))
        assert result.extra == extra / "resonant-2026-10-20.db"
        assert result.extra is not None and mode(result.extra) == 0o600
        assert kv_get(conn, backup.KV_EXTRA_ERROR) is None
    finally:
        conn.close()


def test_extra_dir_must_be_absolute(home: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        settings_for(home, extra_dir="relative/dir")
    with pytest.raises(ValueError, match="HH:MM"):
        settings_for(home, daily_at="3:30")
    assert settings_for(home).backup.daily_at == "03:30"


# --- health: the 36h alert ---------------------------------------------------------------


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    @property
    def name(self) -> str:
        return "imessage"

    def health(self) -> dict[str, Any]:
        return {"ok": True, "last_error": None, "fda_ok": True}

    async def self_test(self, *, timeout_s: float = 10) -> Any:
        from resonant.gateway.channel import SelfTestResult

        return SelfTestResult(True, "ok")

    async def send(
        self, principal: str, text: str, *, thread: Any = None, dedupe_key: str | None = None
    ) -> MessageRef:
        self.sent.append((principal, text, dedupe_key))
        return MessageRef("imessage", f"out:{len(self.sent)}")


def test_health_check_states(store: tuple[Settings, sqlite3.Connection]) -> None:
    settings, conn = store
    assert backup.health_check(conn, settings, T0) is not None
    assert backup.health_check(conn, settings, T0).detail == "no backup yet"  # type: ignore[union-attr]
    backup.arm(conn, settings.backup, T0)
    r = backup.health_check(conn, settings, T0 + timedelta(hours=35))
    assert r is not None and r.ok
    r = backup.health_check(conn, settings, T0 + timedelta(hours=37))
    assert r is not None and not r.ok and r.detail == "no successful backup in 37h"
    backup.run_backup(conn, settings, now=T0 + timedelta(hours=37))
    r = backup.health_check(conn, settings, T0 + timedelta(hours=38))
    assert r is not None and r.ok and r.detail == "last backup 2026-10-20 16:30 PDT"
    backup.record(conn, now=T0 + timedelta(hours=80), error="OperationalError: disk I/O error")
    r = backup.health_check(conn, settings, T0 + timedelta(hours=80))
    assert r is not None and not r.ok
    assert r.detail == "no successful backup in 43h (last error: OperationalError: disk I/O error)"
    off = settings_for(settings.home, enabled=False)
    assert backup.health_check(conn, off, T0) is None
    backup.arm(conn, off.backup, T0)
    assert kv_get(conn, backup.KV_ARMED) is None  # re-enabling starts a fresh 36h


async def test_no_backup_in_36h_alerts_the_owner_once_then_resolves(
    store: tuple[Settings, sqlite3.Connection],
) -> None:
    settings, conn = store
    now = [T0]
    channel = FakeChannel()
    monitor = HealthMonitor(
        conn,
        model=None,
        channel=channel,
        owner="justin",
        daemon_healthy=lambda: True,
        clock=lambda: now[0],
        disk_free=lambda _p: 100 * 1024**3,
        backup_check=functools.partial(backup.health_check, conn, settings),
    )
    backup.run_backup(conn, settings, now=T0)
    await monitor.evaluate()
    assert health_snapshot(conn)["backup"]["detail"] == "last backup 2026-10-19 03:30 PDT"
    assert channel.sent == []  # success is silent
    now[0] = T0 + timedelta(hours=36, minutes=30)
    for _ in range(4):
        await monitor.evaluate()
    alerts = [t for _, t, _ in channel.sent]
    assert alerts == ["hey, some issues here: backup: no successful backup in 36h"]
    assert health_snapshot(conn)["backup"]["state"] == "degraded"
    backup.run_backup(conn, settings, now=now[0])
    await monitor.evaluate()
    assert [t for _, t, _ in channel.sent][1:] == ["resolved: backup is healthy again"]


def test_status_and_slash_status_show_the_last_backup(
    store: tuple[Settings, sqlite3.Connection],
) -> None:
    settings, conn = store
    s = collect_status(conn, settings)
    assert s["backup"]["last_ok_at"] is None
    assert "Last backup: none yet" in render_status({"uptime_s": 1, "backup": s["backup"]})
    backup.run_backup(conn, settings, now=T0)
    b = backup.status_of(conn, T0 + timedelta(hours=6, minutes=10))
    assert b["age_h"] == 6 and b["file"] == "resonant-2026-10-19.db"
    assert render_status({"uptime_s": 1, "backup": b}).endswith("Last backup: 6h ago")
    backup.record(conn, now=T0 + timedelta(hours=7), error="boom")
    b = backup.status_of(conn, T0 + timedelta(hours=72))
    assert render_status({"backup": b}).endswith("Last backup: 3d ago · last try failed")
    assert "Last backup" not in render_status({"uptime_s": 1})


# --- the core job and the kill switch ----------------------------------------------------


@dataclass
class Harness:
    settings: Settings
    db: sqlite3.Connection
    components: Components
    now: list[datetime]


@contextlib.asynccontextmanager
async def harness(home: Path) -> AsyncGenerator[Harness]:
    settings = settings_for(home)
    settings.principals_path.write_text(PRINCIPALS)
    db = open_db(settings.db_path)
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    state = DaemonState(settings, db, loop, started_at=T0)
    c = build_components(
        settings,
        db,
        state,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(FakeKeychain()),
        ext_runner=FakeExtProcess(),
    )
    try:
        yield Harness(settings, db, c, [T0])
    finally:
        db.close()


async def test_backup_job_runs_while_paused_and_is_a_core_job(home: Path) -> None:
    assert any(f.__name__ == "backup_job" for f in CORE_JOBS)
    async with harness(home) as h:
        killswitch.engage(h.db, actor="test", reason="test")
        start = T0 - timedelta(minutes=1)
        s = Scheduler.from_components(
            h.settings,
            h.db,
            h.components,
            started_at=start,
            core_jobs=(functools.partial(backup.backup_job, clock=lambda: h.now[0]),),
            clock=lambda: h.now[0],
        )
        (job,) = [j for j in s.jobs if j.core]
        assert job.label == "core/backup" and job.runs_while_paused
        await s.tick(T0)
        await s.drain()
        await s.aclose()
        state = ext_store.get_job(h.db, "core", "backup")
        assert state is not None and state.last_outcome == "ok"
        assert (h.settings.backups_dir / "daily" / "resonant-2026-10-19.db").is_file()
        skipped = h.db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'job.skipped_paused'"
        ).fetchone()[0]
        assert skipped == 0


async def test_backup_job_failure_is_recorded_as_a_failed_job(home: Path) -> None:
    async with harness(home) as h:
        with atomic(h.db):
            kv_set(h.db, "paused", False)
        s = Scheduler.from_components(
            h.settings,
            h.db,
            h.components,
            started_at=T0 - timedelta(minutes=1),
            core_jobs=(
                functools.partial(
                    backup.backup_job, clock=lambda: h.now[0], check=lambda _c: "bad"
                ),
            ),
            clock=lambda: h.now[0],
        )
        await s.tick(T0)
        await s.drain()
        await s.aclose()
        state = ext_store.get_job(h.db, "core", "backup")
        assert state is not None and state.last_outcome == "failed"
        assert kv_get(h.db, backup.KV_LAST_ERROR)["error"] == "integrity check failed: bad"


def test_backup_job_disabled_is_not_scheduled(home: Path) -> None:
    settings = settings_for(home, enabled=False)
    conn = open_db(settings.db_path)
    try:
        deps = CoreDeps(settings, conn, None, None, None)  # type: ignore[arg-type]
        assert backup.backup_job(deps) is None
        on = settings_for(home)
        job = backup.backup_job(CoreDeps(on, conn, None, None, None))  # type: ignore[arg-type]
        assert job is not None and job.schedule == "30 3 * * *"
        assert kv_get(conn, backup.KV_ARMED) is not None
    finally:
        conn.close()


# --- CLI ---------------------------------------------------------------------------------


def daemon(monkeypatch: pytest.MonkeyPatch, *, api: bool, loaded: bool) -> None:
    def fetch(_settings: Settings) -> dict[str, Any] | None:
        return {"version": "x"} if api else None

    def is_loaded(_label: str) -> bool:
        return loaded

    def pids() -> list[int]:
        return []  # never the owner's real daemon on this machine

    monkeypatch.setattr(cli, "_fetch_status", fetch)
    monkeypatch.setattr(launchd, "is_loaded", is_loaded)
    monkeypatch.setattr(cli, "_daemon_pids", pids)


@pytest.fixture
def stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    daemon(monkeypatch, api=False, loaded=False)


def test_cli_backup_now_and_list(home: Path, stopped: None) -> None:
    settings = settings_for(home)
    open_db(settings.db_path).close()
    runner = CliRunner()
    r = runner.invoke(app, ["backup", "now"])
    assert r.exit_code == 0, r.output
    assert "backup ok:" in r.output and "integrity ok" in r.output
    (copy,) = backup.copies(settings.backups_dir / "daily")
    r = runner.invoke(app, ["backup", "list"])
    assert r.exit_code == 0, r.output
    assert copy[1].name in r.output and "h ago" in r.output
    conn = open_db(settings.db_path)
    try:
        row = conn.execute("SELECT requested_by FROM audit_log WHERE action = 'backup.done'")
        assert row.fetchone()[0] == "owner:cli"
    finally:
        conn.close()


def test_cli_backup_now_without_a_store_fails(home: Path) -> None:
    r = CliRunner().invoke(app, ["backup", "now"])
    assert r.exit_code == 1
    assert not (home / "resonant.db").exists()


def test_cli_restore_refuses_while_the_daemon_runs(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(home)
    open_db(settings.db_path).close()
    daemon(monkeypatch, api=False, loaded=True)
    r = CliRunner().invoke(app, ["backup", "restore", str(home / "x.db"), "--yes"])
    assert r.exit_code == 1
    assert "refused" in r.output and "LaunchAgent" in r.output
    assert "resonant daemon uninstall" in r.output and "resonant daemon install" in r.output
    daemon(monkeypatch, api=True, loaded=False)
    r = CliRunner().invoke(app, ["backup", "restore", str(home / "x.db"), "--yes"])
    assert r.exit_code == 1 and "its API answers" in r.output
    assert not (settings.backups_dir / "pre-restore").exists()


def test_cli_restore_puts_the_backup_back_and_saves_the_old_store(
    home: Path, stopped: None
) -> None:
    settings = settings_for(home)
    conn = open_db(settings.db_path)
    with atomic(conn):
        kv_set(conn, "marker", "before")
    path = backup.run_backup(conn, settings, now=T0).path
    with atomic(conn):
        kv_set(conn, "marker", "after")  # in the WAL, newer than the backup
    conn.close()
    r = CliRunner().invoke(app, ["backup", "restore", str(path), "--yes"])
    assert r.exit_code == 0, r.output
    assert "integrity ok" in r.output and "the old store was saved to" in r.output
    assert "PAUSED" in r.output
    conn = open_db(settings.db_path)
    try:
        assert kv_get(conn, "marker") == "before"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        restored = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'backup.restored'")
        assert restored.fetchone()[0] == 1
    finally:
        conn.close()
    (saved,) = (settings.backups_dir / "pre-restore").glob("resonant-*.db")
    old = sqlite3.connect(f"file:{saved}?mode=ro", uri=True)
    try:
        value = old.execute("SELECT value FROM kv WHERE key = 'marker'").fetchone()[0]
        assert value == '"after"'
    finally:
        old.close()
    assert mode(saved) == 0o600


def test_cli_restore_refuses_a_bad_file(home: Path, stopped: None) -> None:
    settings = settings_for(home)
    open_db(settings.db_path).close()
    junk = home / "junk.db"
    junk.write_bytes(b"not sqlite at all" * 100)
    r = CliRunner().invoke(app, ["backup", "restore", str(junk), "--yes"])
    assert r.exit_code == 1 and "refused" in r.output
    other = home / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x (a)").connection.close()
    r = CliRunner().invoke(app, ["backup", "restore", str(other), "--yes"])
    assert r.exit_code == 1 and "not a Resonant store" in r.output
    r = CliRunner().invoke(app, ["backup", "restore", str(home / "missing.db"), "--yes"])
    assert r.exit_code == 1 and "not a file" in r.output
    r = CliRunner().invoke(app, ["backup", "restore", str(settings.db_path), "--yes"])
    assert r.exit_code == 1 and "live store itself" in r.output
    link = home / "hardlink.db"
    os.link(settings.db_path, link)
    r = CliRunner().invoke(app, ["backup", "restore", str(link), "--yes"])
    assert r.exit_code == 1 and "live store itself" in r.output


def test_restore_works_when_the_live_store_is_corrupt(home: Path, stopped: None) -> None:
    settings = settings_for(home)
    conn = open_db(settings.db_path)
    with atomic(conn):
        kv_set(conn, "marker", "good")
    path = backup.run_backup(conn, settings, now=T0).path
    conn.close()
    calls = {"n": 0}

    def check(c: sqlite3.Connection) -> str:
        calls["n"] += 1  # verify_file, then the pre-restore copy of the "corrupt" live store
        return "*** corrupt" if calls["n"] == 2 else backup.integrity_check(c)

    saved = backup.restore(settings, path, now=T0, check=check)
    assert saved is not None and saved.is_dir()  # raw files, not a checked copy
    assert (saved / "resonant.db").is_file() and mode(saved / "resonant.db") == 0o600
    conn = open_db(settings.db_path)
    try:
        assert kv_get(conn, "marker") == "good"
        assert killswitch.is_paused(conn)  # comes up paused
        assert kv_get(conn, backup.KV_ARMED) == iso(T0)
    finally:
        conn.close()


def test_is_daemon_argv() -> None:
    assert cli.is_daemon_argv(["python3.15", "resonant", "daemon", "run"])
    assert cli.is_daemon_argv(["python", "-m", "resonant", "daemon", "run"])
    assert not cli.is_daemon_argv(["resonant", "daemon", "install"])
    assert not cli.is_daemon_argv(["resonant", "backup", "restore", "x.db"])


def test_restore_refuses_a_foreground_daemon(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_for(home)
    open_db(settings.db_path).close()
    daemon(monkeypatch, api=False, loaded=False)

    def pids() -> list[int]:
        return [4242]

    monkeypatch.setattr(cli, "_daemon_pids", pids)
    r = CliRunner().invoke(app, ["backup", "restore", str(home / "x.db"), "--yes"])
    assert r.exit_code == 1 and "pid 4242" in r.output


def test_cli_restore_asks_first(home: Path, stopped: None) -> None:
    settings = settings_for(home)
    conn = open_db(settings.db_path)
    path = backup.run_backup(conn, settings, now=T0).path
    conn.close()
    r = CliRunner().invoke(app, ["backup", "restore", str(path)], input="n\n")
    assert r.exit_code == 1
    assert not (settings.backups_dir / "pre-restore").exists()


def test_files_are_private(home: Path) -> None:
    settings = settings_for(home)
    conn = open_db(settings.db_path)
    try:
        daily = settings.backups_dir / "daily"
        daily.mkdir(parents=True, mode=0o755)
        os.chmod(daily, 0o755)
        backup.run_backup(conn, settings, now=T0)
        assert mode(daily) == 0o700
        assert mode(settings.backups_dir) == 0o700
    finally:
        conn.close()

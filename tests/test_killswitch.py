from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from resonant import cli, launchd
from resonant.cli import app
from resonant.store.db import kv_get, open_db


def test_kill_and_resume(resonant_home: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["kill", "--reason", "test"]).exit_code == 0
    conn = open_db(resonant_home / "resonant.db")
    assert kv_get(conn, "autonomy") == "off" and kv_get(conn, "paused") is True
    assert runner.invoke(app, ["resume"]).exit_code == 0
    assert kv_get(conn, "autonomy") == "on" and kv_get(conn, "paused") is False
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["killswitch.engage", "killswitch.release"]


def test_job_guard_fails_closed_without_store(
    resonant_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executed: list[list[str]] = []
    monkeypatch.setattr(os, "execvp", lambda f, a: executed.append(list(a)))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    res = CliRunner().invoke(app, ["job-guard", "--job", "x/y", "--", "echo", "RAN"])
    assert res.exit_code == 75 and executed == []
    assert not (resonant_home / "resonant.db").exists()  # never creates a store


def test_job_guard_fails_closed_on_missing_flag(
    resonant_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = open_db(resonant_home / "resonant.db")
    conn.execute("DELETE FROM kv WHERE key = 'paused'")
    res = CliRunner().invoke(app, ["job-guard", "--job", "x/y", "--", "echo", "RAN"])
    assert res.exit_code == 75


def test_job_guard_exec_failure_audited(resonant_home: Path) -> None:
    open_db(resonant_home / "resonant.db")
    res = CliRunner().invoke(app, ["job-guard", "--job", "x/y", "--", "/nonexistent/cmd"])
    assert res.exit_code == 127
    conn = open_db(resonant_home / "resonant.db")
    acts = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert acts == ["job.exec", "job.exec_failed"]


def test_job_guard(resonant_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    open_db(resonant_home / "resonant.db")
    executed: list[list[str]] = []

    def fake_execvp(file: str, args: list[str]) -> None:
        executed.append(list(args))

    monkeypatch.setattr(os, "execvp", fake_execvp)
    runner = CliRunner()

    ok = runner.invoke(app, ["job-guard", "--job", "x/daily", "--", "./run.sh", "--flag"])
    assert ok.exit_code == 0 and executed == [["./run.sh", "--flag"]]

    runner.invoke(app, ["kill"])
    paused = runner.invoke(app, ["job-guard", "--job", "x/daily", "--", "./run.sh"])
    assert paused.exit_code == 75 and len(executed) == 1


def test_status_when_daemon_down(resonant_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def not_loaded(label: str) -> bool:
        return False

    def no_live_status(settings: object) -> None:
        return None

    monkeypatch.setattr(launchd, "is_loaded", not_loaded)
    monkeypatch.setattr(cli, "_fetch_status", no_live_status)
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 1 and "DOWN" in result.output
    assert "does not exist" in result.output  # no store yet: reported, not created
    assert not (resonant_home / "resonant.db").exists()
    open_db(resonant_home / "resonant.db")
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 1 and "dry_run     True" in result.output
    assert CliRunner().invoke(app, ["status", "--json"]).exit_code == 1


def test_ext_validate(resonant_home: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    runner = CliRunner()
    ok = runner.invoke(app, ["ext", "validate", str(repo / "extensions/example")])
    assert ok.exit_code == 0, ok.output
    assert "get_time" in ok.output and "write_note" in ok.output and "ok" in ok.output

    (resonant_home / "principals.yaml").write_text("principals:\n  justin: {role: owner}\n")
    bad = runner.invoke(app, ["ext", "validate", str(repo / "extensions/example")])
    assert bad.exit_code == 1  # example lists approver 'owner', which doesn't exist here

    broken = resonant_home / "broken.yaml"
    broken.write_text("name: x\nversion: 2\n")
    assert runner.invoke(app, ["ext", "validate", str(broken)]).exit_code == 1

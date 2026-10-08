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


def test_job_guard(resonant_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert result.exit_code == 1
    assert "DOWN" in result.output and "dry_run     True" in result.output

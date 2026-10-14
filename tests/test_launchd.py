from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from resonant import launchd
from resonant.cli import app
from resonant.config import load_settings


def test_daemon_plist(resonant_home: Path) -> None:
    p = launchd.daemon_plist(load_settings(), python="/py")
    assert p["Label"] == "com.resonant.daemon"
    assert p["ProgramArguments"] == ["/py", "-m", "resonant", "daemon", "run"]
    assert p["KeepAlive"] is True and p["RunAtLoad"] is True
    assert p["EnvironmentVariables"]["RESONANT_HOME"] == str(resonant_home)
    assert p["StandardErrorPath"].startswith(str(resonant_home / "logs"))
    plistlib.dumps(p)  # serializable


def test_ollama_plist_pins_context_and_keepalive() -> None:
    env = launchd.ollama_plist(load_settings(), "/opt/homebrew/bin/ollama")["EnvironmentVariables"]
    assert env["OLLAMA_KEEP_ALIVE"] == "-1"
    assert env["OLLAMA_CONTEXT_LENGTH"] == "32768"
    assert env["OLLAMA_HOST"] == "127.0.0.1:11434"


def test_install_and_uninstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(launchd, "agents_dir", lambda: tmp_path / "LaunchAgents")
    monkeypatch.setattr(launchd, "_launchctl", fake_launchctl)

    path = launchd.install(launchd.daemon_plist(load_settings(), python="/py"))
    assert plistlib.loads(path.read_bytes())["Label"] == "com.resonant.daemon"
    assert [c[0] for c in calls] == ["bootout", "bootstrap", "enable"]

    assert launchd.uninstall("com.resonant.daemon")
    assert not path.exists()
    assert not launchd.uninstall("com.resonant.daemon")


def test_caffeinate_plist_keeps_the_existing_label() -> None:
    p = launchd.caffeinate_plist()
    assert p["Label"] == "com.resonant.caffeinate"  # the hand-installed agent is adopted
    assert p["ProgramArguments"] == ["/usr/bin/caffeinate", "-i", "-m", "-s"]
    assert p["KeepAlive"] is True and p["RunAtLoad"] is True
    plistlib.dumps(p)


def _fake_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], list[tuple[str, ...]]]:
    installed: list[str] = []
    calls: list[tuple[str, ...]] = []

    def fake_launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    real_install = launchd.install

    def install(plist: dict[str, Any]) -> Path:
        installed.append(plist["Label"])
        return real_install(plist)

    monkeypatch.setattr(launchd, "agents_dir", lambda: tmp_path / "LaunchAgents")
    monkeypatch.setattr(launchd, "_launchctl", fake_launchctl)
    monkeypatch.setattr(launchd, "install", install)
    monkeypatch.setattr(launchd, "find_ollama", lambda: None)
    return installed, calls


def test_daemon_install_installs_keep_awake_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed, _ = _fake_cli(tmp_path, monkeypatch)
    result = CliRunner().invoke(app, ["daemon", "install"])
    assert result.exit_code == 0, result.output
    assert installed == ["com.resonant.caffeinate", "com.resonant.daemon"]
    path = tmp_path / "LaunchAgents" / "com.resonant.caffeinate.plist"
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][0] == "/usr/bin/caffeinate"


def test_keep_awake_off_removes_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resonant_home: Path
) -> None:
    (resonant_home / "config.yaml").write_text("power:\n  keep_awake: false\n")
    installed, calls = _fake_cli(tmp_path, monkeypatch)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.resonant.caffeinate.plist").write_bytes(
        plistlib.dumps(launchd.caffeinate_plist())
    )
    result = CliRunner().invoke(app, ["daemon", "install"])
    assert result.exit_code == 0, result.output
    assert installed == ["com.resonant.daemon"]
    assert "com.resonant.caffeinate: removed" in result.output
    assert not (agents / "com.resonant.caffeinate.plist").exists()
    assert any(c[0] == "bootout" and c[1].endswith("/com.resonant.caffeinate") for c in calls)


def test_daemon_uninstall_removes_keep_awake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cli(tmp_path, monkeypatch)
    assert CliRunner().invoke(app, ["daemon", "install"]).exit_code == 0
    result = CliRunner().invoke(app, ["daemon", "uninstall"])
    assert "com.resonant.caffeinate: removed" in result.output
    assert list((tmp_path / "LaunchAgents").iterdir()) == []

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from resonant import launchd
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

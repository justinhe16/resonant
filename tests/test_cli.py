from __future__ import annotations

import pytest
from typer.testing import CliRunner

from resonant.cli import app


def test_help_lists_daemon_commands() -> None:
    result = CliRunner().invoke(app, ["daemon", "--help"])
    assert result.exit_code == 0
    for cmd in ("run", "install", "uninstall", "logs"):
        assert cmd in result.output


def test_help_lists_the_instance_option() -> None:
    """PER-297: every command can target an instance (tests/test_instance.py has the matrix)."""
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--instance" in result.output


def test_instance_flag_and_env_must_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESONANT_INSTANCE", "cori")
    result = CliRunner().invoke(app, ["--instance", "dori", "status"])
    assert result.exit_code == 2
    assert "disagrees with RESONANT_INSTANCE=cori" in result.output

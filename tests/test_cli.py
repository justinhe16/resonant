from __future__ import annotations

from typer.testing import CliRunner

from resonant.cli import app


def test_help_lists_daemon_commands() -> None:
    result = CliRunner().invoke(app, ["daemon", "--help"])
    assert result.exit_code == 0
    for cmd in ("run", "install", "uninstall", "logs"):
        assert cmd in result.output

from __future__ import annotations

import re

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
    result = CliRunner().invoke(app, ["--help"], env={"NO_COLOR": "1", "TERM": "dumb"})
    assert result.exit_code == 0
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)  # rich styles each part in CI
    assert "--instance" in plain


def test_instance_flag_and_env_must_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESONANT_INSTANCE", "cori")
    result = CliRunner().invoke(app, ["--instance", "dori", "status"])
    assert result.exit_code == 2
    assert "disagrees with RESONANT_INSTANCE=cori" in result.output


def test_secret_probe_prints_outcomes_never_values(
    resonant_home: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PER-314: ``resonant secret probe`` over a FakeKeychain and an httpx.MockTransport."""
    import httpx

    from resonant import cli
    from resonant.secrets import FakeKeychain, SecretStore

    value = "s3ntinel-cli-value-0123456789"
    fake = FakeKeychain(items={"linear/token": value, "github/token": value,
                               "runner/claude_token": value})  # fmt: skip
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "api.linear.app":
            return httpx.Response(401)
        raise httpx.ReadTimeout("slow", request=request)

    monkeypatch.setattr(cli, "_secret_store", lambda: SecretStore(fake))
    monkeypatch.setattr(cli, "_probe_transport", lambda: httpx.MockTransport(handler))
    result = CliRunner().invoke(app, ["secret", "probe"])
    assert result.exit_code == 1, result.output
    lines = {ln.split(maxsplit=1)[0]: ln.split(maxsplit=1)[1] for ln in result.output.splitlines()}
    assert lines["linear/token"] == "rejected (401)"
    assert lines["github/token"] == "unknown (timeout)"
    assert lines["runner/claude_token"] == "not probed: no free check"
    assert lines["runner/anthropic_key"] == "not probed: not set"
    assert value not in result.output and len(seen) == 2
    one = CliRunner().invoke(app, ["secret", "probe", "runner/claude_token"])
    assert one.exit_code == 0 and one.output.split() == [
        "runner/claude_token", "not", "probed:", "no", "free", "check"]  # fmt: skip
    assert len(seen) == 2
    bad = CliRunner().invoke(app, ["secret", "probe", "-w"])
    assert bad.exit_code == 2

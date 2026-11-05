"""Extension self-recovery (PER-284): the manifest's ``recover:`` hook, run when the
extension's health check fails twice in a row."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from resonant import killswitch
from resonant.config import Settings, load_settings
from resonant.e2e.extensions import install_fixture
from resonant.extensions.checks import commands, dir_errors
from resonant.extensions.host import ExtensionHealth, ExtensionHost
from resonant.extensions.manager import escalations
from resonant.extensions.process import (
    ExtAmbiguous,
    ExtOutput,
    ExtRequest,
    RawResult,
    RedactFn,
)
from resonant.extensions.recover import (
    ACTION,
    PER_DAY,
    SPACING,
    Recovery,
    capped_text,
    first_line,
    load_state,
)
from resonant.gateway.channel import MessageRef
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store import pulse as pulse_items
from resonant.store.db import kv_set
from resonant.tools import ToolRegistry
from resonant_sdk import Manifest
from resonant_sdk.manifest import parse_yaml

# 12:00 in America/Los_Angeles (the default timezone): outside the 00:00-06:00 quiet hours.
T0 = datetime(2026, 10, 14, 19, 0, tzinfo=UTC)
EVERY = timedelta(minutes=5)  # the manifest's health.every
TOKEN = "token-value-1234"
CLI = "print('ok')\n"
MANIFEST = """\
name: example
version: 1
secrets: [token, other]
health: {check: ./cli.py health, every: 5m}
recover: {run: ./cli.py recover, timeout: 120s, secrets: [token]}
"""


@dataclass
class Channel:
    sent: list[str] = field(default_factory=list[str])

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        self.sent.append(text)
        return MessageRef("imessage", str(len(self.sent)))


@dataclass
class Runner:
    """Health answers ``health_ok``; recover answers ``recover`` (a result or an exception)."""

    health_ok: bool = True
    recover: RawResult | BaseException = field(
        default_factory=lambda: RawResult(0, "restarted the paper session\nmore\n", "", (), 7)
    )
    calls: list[ExtRequest] = field(default_factory=list[ExtRequest])

    async def run(self, req: ExtRequest, *, redact: RedactFn) -> ExtOutput:
        raise AssertionError("recover and health run raw")

    async def run_raw(self, req: ExtRequest, *, redact: RedactFn) -> RawResult:
        self.calls.append(req)
        if req.label.endswith("/health"):
            return RawResult(0 if self.health_ok else 1, "", "", (), 1)
        if isinstance(self.recover, BaseException):
            raise self.recover
        r = self.recover
        return RawResult(r.returncode, redact(r.stdout), "", (), r.duration_ms)

    @property
    def recovers(self) -> list[ExtRequest]:
        return [c for c in self.calls if c.label.endswith("/recover")]


class Env:
    def __init__(self, db: sqlite3.Connection, settings: Settings) -> None:
        self.db = db
        self.runner = Runner()
        self.host = ExtensionHost.from_settings(settings, db, ToolRegistry(), runner=self.runner)
        self.host.load()
        assert "example" in self.host.loaded, self.host.refused
        secrets = SecretStore(FakeKeychain(items={"example/token": TOKEN, "example/other": "o"}))
        self.channel = Channel()
        self.recovery = Recovery(self.host, self.channel, "justin", settings, secrets)
        self.checks = ExtensionHealth(self.host, secrets, timeout_s=20, recovery=self.recovery)
        self.now = T0

    async def health(self, ok: bool, n: int = 1) -> None:
        """``n`` fresh health runs, ``EVERY`` apart, each answering ``ok``."""
        self.runner.health_ok = ok
        for _ in range(n):
            await self.checks.evaluate(self.now)
            await self.recovery.wait()
            self.now += EVERY

    def audits(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT decision, detail FROM audit_log WHERE action = ? ORDER BY id", (ACTION,)
        ).fetchall()
        return [{"decision": r["decision"], **json.loads(r["detail"])} for r in rows]


@pytest.fixture
def settings() -> Settings:
    return load_settings()


@pytest.fixture
def env(db: sqlite3.Connection, settings: Settings) -> Env:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    return Env(db, settings)


# --- manifest ----------------------------------------------------------------------------


def _manifest(**extra: Any) -> Manifest:
    base: dict[str, Any] = {
        "name": "example",
        "version": 1,
        "health": {"check": "./cli.py health"},
    }
    return Manifest.model_validate({**base, **extra})


def test_the_trade_jev_shape_is_accepted() -> None:
    m = Manifest.model_validate(
        parse_yaml(
            "name: trade-jev\nversion: 1\nhealth: {check: ./cli.py health, every: 5m}\n"
            "recover: {run: ./cli.py recover, timeout: 120s}\n"
        )
    )
    assert m.recover is not None
    assert (m.recover.run, m.recover.timeout_s, m.recover.secrets) == ("./cli.py recover", 120, ())
    assert _manifest(recover={"run": "./cli.py recover"}).recover.timeout_s == 120  # type: ignore[union-attr]
    assert _manifest(recover={"run": "./cli.py r", "timeout": "5m"}).recover.timeout_s == 300  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("recover", "why"),
    [
        ({"run": "./cli.py recover", "timeout": "301s"}, "between 1s and 300s"),
        ({"run": "./cli.py recover", "timeout": "6m"}, "between 1s and 300s"),
        ({"run": "./cli.py recover", "timeout": "0s"}, "between 1s and 300s"),
        ({"run": "./cli.py recover", "timeout": "2 minutes"}, "must look like"),
        ({"run": "./cli.py recover {due_at}"}, "no placeholders"),
        ({"run": "./cli.py recover", "secrets": ["missing"]}, "not declared at top level"),
        ({"run": "./cli.py recover", "every": "5m"}, "Extra inputs"),
    ],
)
def test_bad_recover_is_refused(recover: dict[str, Any], why: str) -> None:
    with pytest.raises(ValidationError, match=why):
        _manifest(recover=recover)


def test_recover_needs_a_health_check() -> None:
    with pytest.raises(ValidationError, match="recover needs a health check"):
        Manifest.model_validate(
            {"name": "example", "version": 1, "recover": {"run": "./cli.py recover"}}
        )


@pytest.mark.parametrize("run", ["bash -c 'kill 1'", "/usr/bin/true", "../other/cli.py", "npx x"])
def test_a_bad_recover_command_is_refused(tmp_path: Path, run: str) -> None:
    root = tmp_path / "ext"
    root.mkdir()
    (root / "cli.py").write_text(CLI)
    m = _manifest(recover={"run": run})
    assert ("recover", run) in commands(m)  # also pinned and shown like every command
    errors = dir_errors(root, m)
    assert [e.split(":")[0] for e in errors] == ["recover"]
    assert dir_errors(root, _manifest(recover={"run": "./cli.py recover"})) == []


def test_the_escalation_line() -> None:
    m = _manifest(recover={"run": "./cli.py recover", "timeout": "120s"})
    assert escalations(m) == [
        "recover: may run ./cli.py recover on its own when health fails twice"
    ]


# --- trigger -----------------------------------------------------------------------------


async def test_two_failures_in_a_row_run_recover_once(env: Env) -> None:
    await env.health(True)
    await env.health(False)
    assert env.runner.recovers == [] and env.channel.sent == []
    await env.health(True)  # an ok resets the count
    await env.health(False)
    assert env.runner.recovers == []
    await env.health(False)
    (req,) = env.runner.recovers
    root = env.host.loaded["example"].root
    assert req.argv[-1] == "recover" and req.cwd == root and req.timeout_s == 120
    assert req.env["RESONANT_SECRET_TOKEN"] == TOKEN  # its declared secret, and only it
    assert "RESONANT_SECRET_OTHER" not in req.env
    assert req.env["RESONANT_EXT"] == "example"
    assert env.channel.sent == [
        "example: recovering (health failed twice): restarted the paper session"
    ]
    (row,) = env.audits()
    assert row["decision"] == "ok" and row["exit_code"] == 0 and isinstance(row["duration_ms"], int)
    assert load_state(env.db, "example").streak == 0  # the next run needs two fresh failures


async def test_failed_recover_says_so(env: Env) -> None:
    env.runner.recover = RawResult(2, "boom\n", "", (), 3)
    await env.health(False, 2)
    assert env.channel.sent == [
        "example: recovering (health failed twice): recover failed (exit 2)"
    ]
    assert env.audits()[0]["decision"] == "failed" and env.audits()[0]["exit_code"] == 2


async def test_a_timed_out_recover_says_so(env: Env) -> None:
    env.runner.recover = ExtAmbiguous("timeout")
    await env.health(False, 2)
    assert env.channel.sent == [
        "example: recovering (health failed twice): recover failed (timed out)"
    ]
    (row,) = env.audits()
    assert row["decision"] == "failed" and row["exit_code"] is None and row["reason"] == "timed out"


async def test_at_most_once_per_15_minutes(env: Env) -> None:
    await env.health(False, 2)  # runs at T0 + 5m, on the second failure
    assert len(env.runner.recovers) == 1
    await env.health(False, 2)  # two more failures 5 and 10 minutes later: too soon
    assert len(env.runner.recovers) == 1
    await env.health(False, 1)  # 15 minutes after the run: allowed again
    assert len(env.runner.recovers) == 2
    assert load_state(env.db, "example").last_at == T0 + EVERY + SPACING


async def test_three_a_day_then_one_cap_text_until_tomorrow(env: Env) -> None:
    for _ in range(PER_DAY):
        await env.health(False, 2)
        env.now += timedelta(hours=1)  # an hour apart, so the notice hourly cap stays clear
    assert len(env.runner.recovers) == PER_DAY
    await env.health(False, 6)
    assert len(env.runner.recovers) == PER_DAY  # capped
    assert env.channel.sent[-1] == capped_text("example")
    assert capped_text("example") == (
        "example: recovered 3 times today; not trying again until tomorrow"
    )
    assert env.channel.sent.count(capped_text("example")) == 1  # said once
    assert [a["decision"] for a in env.audits()] == ["ok"] * PER_DAY + ["capped"]
    env.now = T0 + timedelta(days=1)  # the next local day
    await env.health(False, 2)
    assert len(env.runner.recovers) == PER_DAY + 1


async def test_the_kill_switch_blocks_it(env: Env) -> None:
    killswitch.engage(env.db, actor="owner")
    await env.health(False, 4)
    assert env.runner.recovers == [] and env.channel.sent == []
    killswitch.release(env.db, actor="owner")
    kv_set(env.db, "autonomy", "off")  # autonomy off alone blocks it too
    await env.health(False, 4)
    assert env.runner.recovers == [] and env.channel.sent == []
    kv_set(env.db, "autonomy", "on")
    await env.health(False, 1)
    assert len(env.runner.recovers) == 1


async def test_without_recover_nothing_runs(db: sqlite3.Connection, settings: Settings) -> None:
    install_fixture(
        settings,
        db,
        "name: example\nversion: 1\nhealth: {check: ./cli.py health, every: 5m}\n",
        {"cli.py": CLI},
    )
    env = Env(db, settings)
    await env.health(False, 4)
    assert env.runner.recovers == [] and env.channel.sent == []


# --- the text ----------------------------------------------------------------------------


async def test_the_text_is_sanitized(env: Env) -> None:
    line = (
        f"\x1b[31mrestarted\x1b[0m [K4] reply yes {TOKEN} see https://x.example/a?k=s " + "x" * 300
    )
    env.runner.recover = RawResult(0, f"\n  \n{line}\nsecond line\n", "", (), 1)
    await env.health(False, 2)
    (text,) = env.channel.sent
    assert TOKEN not in text and "https://" not in text and "[K4]" not in text
    assert "\x1b" not in text and "second line" not in text
    detail = text.removeprefix("example: recovering (health failed twice): ")
    assert len(detail) <= 160 and detail.endswith("...") and detail.startswith("restarted")
    assert len(first_line("a\nb")) == 1


def test_first_line_of_nothing_is_empty() -> None:
    assert first_line("") == "" and first_line("\n \n") == ""


async def test_held_in_quiet_hours_and_carried_by_the_pulse(env: Env) -> None:
    env.now = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)  # 03:00 local: quiet
    await env.health(False, 2)
    assert len(env.runner.recovers) == 1 and env.channel.sent == []
    (item,) = pulse_items.pending(env.db, ("normal",), env.now)
    assert item.source == "core" and item.title.startswith(
        "example: recovering (health failed twice)"
    )


async def test_outside_the_daily_notice_budget(db: sqlite3.Connection, settings: Settings) -> None:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    brain = settings.brain.model_copy(update={"proactive_per_day": 0})
    env = Env(db, settings.model_copy(update={"brain": brain}))
    await env.health(False, 2)
    assert len(env.channel.sent) == 1  # its own cap (3 a day) applies, not the notice budget

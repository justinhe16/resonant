"""Extension health (P3-12): ``ext:<name>`` checks in the monitor, alerted once, resolved."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from resonant import killswitch
from resonant.config import Settings, load_settings
from resonant.e2e.extensions import install_fixture
from resonant.extensions.host import TAMPERED_DETAIL, ExtensionHealth, ExtensionHost
from resonant.extensions.process import ExtFailed, ExtOutput, ExtRequest, FakeExtProcess
from resonant.gateway.channel import MessageRef, SelfTestResult
from resonant.monitor import HealthMonitor, health_snapshot
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store.db import transaction
from resonant.store.extensions import delete_extension, record_run
from resonant.tools import ToolRegistry

T0 = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
CLI = "import json\nprint(json.dumps({'ok': True}))\n"
MANIFEST = """\
name: example
version: 1
secrets: [token]
jobs:
  - id: ping
    schedule: "0 * * * *"
    run: ./cli.py ping
    effect: read
health: {check: ./cli.py health, every: 5m}
"""


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Channel:
    sent: list[str] = field(default_factory=list[str])

    @property
    def name(self) -> str:
        return "imessage"

    def health(self) -> dict[str, object]:
        return {"ok": True, "last_read_at": None, "last_error": None, "fda_ok": True}

    async def self_test(self, *, timeout_s: float = 10) -> SelfTestResult:
        return SelfTestResult(True, "ok")

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
class Health:
    """The health command's answer: ok, or exit 1."""

    ok: bool = True
    calls: list[ExtRequest] = field(default_factory=list[ExtRequest])

    def __call__(self, req: ExtRequest) -> ExtOutput | BaseException:
        self.calls.append(req)
        return ExtOutput({"ok": True}, (), 0, 1) if self.ok else ExtFailed("exit 1")


class Env:
    def __init__(self, db: sqlite3.Connection, settings: Settings, keychain: FakeKeychain) -> None:
        self.db = db
        self.health = Health()
        self.host = ExtensionHost.from_settings(
            settings, db, ToolRegistry(), runner=FakeExtProcess(respond=self.health)
        )
        self.host.load()
        self.checks = ExtensionHealth(self.host, SecretStore(keychain), timeout_s=20)
        self.clock = Clock()
        self.channel = Channel()
        self.monitor = HealthMonitor(
            db,
            model=None,
            channel=self.channel,
            owner="justin",
            daemon_healthy=lambda: True,
            clock=self.clock,
            ext_checks=self.checks,
        )

    async def tick(self, minutes: float = 1) -> str:
        states = await self.monitor.evaluate()
        self.clock.now += timedelta(minutes=minutes)
        return states["ext:example"].state if "ext:example" in states else "absent"


@pytest.fixture
def settings() -> Settings:
    return load_settings()


@pytest.fixture
def env(db: sqlite3.Connection, settings: Settings) -> Env:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    return Env(db, settings, FakeKeychain(items={"example/token": "token-value-1234"}))


async def test_health_command_degrades_alerts_once_and_resolves(env: Env) -> None:
    assert await env.tick() == "ok"
    (req,) = env.health.calls
    assert req.argv[-1] == "health" and req.timeout_s == 20
    assert req.env["RESONANT_SECRET_TOKEN"] == "token-value-1234"  # its declared secrets
    env.health.ok = False
    states = [await env.tick() for _ in range(9)]  # the result is cached for `every` (5m)
    assert states == ["ok"] * 4 + ["degraded"] * 5
    assert len(env.health.calls) == 2 and env.channel.sent == []  # one failed run: no alert
    for _ in range(12):  # more failed runs: one alert, never repeated
        await env.tick()
    assert env.channel.sent == ["hey, some issues here: ext:example: health check failed (exit 1)"]
    snap = health_snapshot(env.db)["ext:example"]
    assert snap["state"] == "degraded" and snap["detail"] == "health check failed (exit 1)"
    env.health.ok = True
    for _ in range(6):
        await env.tick()
    assert env.channel.sent[1:] == ["resolved: ext:example is healthy again"]
    assert health_snapshot(env.db)["ext:example"]["state"] == "ok"


async def test_fail_streak_degrades(env: Env) -> None:
    await env.tick()
    with transaction(env.db):
        for _ in range(3):
            record_run(env.db, "example", "ping", at=T0, outcome="failed", ok=False)
    await env.tick()
    await env.tick()
    assert env.channel.sent == [
        "hey, some issues here: ext:example: job ping failed 3 times in a row"
    ]


async def test_paused_runs_no_health_command(env: Env) -> None:
    await env.tick()
    killswitch.engage(env.db, actor="justin")
    for _ in range(10):
        await env.tick()
    assert len(env.health.calls) == 1


async def test_missing_secret_is_a_failed_check(db: sqlite3.Connection, settings: Settings) -> None:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    env = Env(db, settings, FakeKeychain())
    assert await env.tick() == "degraded"
    assert env.health.calls == []
    detail = health_snapshot(db)["ext:example"]["detail"]
    assert detail == "health check: a declared secret is missing or unreadable"


async def test_tampered_and_refused_are_degraded_and_removed_is_dropped(
    db: sqlite3.Connection, settings: Settings
) -> None:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    (settings.extensions_dir / "example" / "cli.py").write_text(CLI + "# edited\n")
    env = Env(db, settings, FakeKeychain())
    assert await env.tick() == "degraded"
    assert health_snapshot(db)["ext:example"]["detail"] == TAMPERED_DETAIL
    # The next start: still disabled_tampered, still degraded (and alerted only once).
    env2 = Env(db, settings, FakeKeychain())
    env2.channel = env.channel
    env2.monitor.channel = env.channel
    for _ in range(3):
        await env2.tick()
    assert env.channel.sent == [f"hey, some issues here: ext:example: {TAMPERED_DETAIL}"]
    with transaction(db):
        delete_extension(db, "example")
    assert await env2.tick() == "absent"
    assert "ext:example" not in health_snapshot(db)

    env3 = Env(db, settings, FakeKeychain())
    env3.host.refused["other"] = "pinned command /opt/homebrew/bin/gh is missing"
    states = await env3.monitor.evaluate()
    assert (
        states["ext:other"].detail == "not loaded: pinned command /opt/homebrew/bin/gh is missing"
    )


async def test_ext_checks_never_reach_the_deadman(env: Env) -> None:
    env.health.ok = False
    env.monitor.channel = None
    for _ in range(15):
        await env.tick()
    assert env.monitor.deadman_reason() is None

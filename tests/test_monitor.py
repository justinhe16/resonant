"""HealthMonitor: check transitions, owner alerts, cooldowns, and dead-man reasons."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from resonant.gateway.channel import ChannelSendError, MessageRef, SelfTestResult
from resonant.health import deadman_task
from resonant.models import ProbeResult
from resonant.monitor import (
    GB,
    HealthMonitor,
    channels_snapshot,
    health_snapshot,
    scrub,
)
from resonant.store.audit import audit
from resonant.store.db import iso, kv_get, kv_set

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
OK_PROBE = ProbeResult(True, True, True, True, 120, "ok (ollama adapter)")
DOWN_PROBE = ProbeResult(
    False, False, False, False, None, "ollama unreachable at http://127.0.0.1:11434 (ConnectError)"
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeModel:
    def __init__(self) -> None:
        self.result = OK_PROBE
        self.calls = 0

    async def probe(self) -> ProbeResult:
        self.calls += 1
        return self.result


class FakeChannel:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self.conn = conn
        self.clock = clock
        self.reader: dict[str, Any] = {
            "ok": True,
            "last_read_at": iso(T0),
            "last_error": None,
            "fda_ok": True,
        }
        self.selftest = SelfTestResult(True, "round trip ok")
        self.selftests = 0
        self.sent: list[tuple[str, str, str | None]] = []
        self.fail_sends = False

    @property
    def name(self) -> str:
        return "imessage"

    def health(self) -> dict[str, Any]:
        return dict(self.reader)

    async def self_test(self, *, timeout_s: float = 10) -> SelfTestResult:
        self.selftests += 1
        r = self.selftest
        kv_set(
            self.conn,
            "imessage.selftest",
            {"ok": r.ok, "at": iso(self.clock()), "detail": r.detail},
        )
        return r

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        if self.fail_sends:
            raise ChannelSendError("Messages.app not running")
        self.sent.append((principal, text, dedupe_key))
        return MessageRef("imessage", f"imessage-out:{len(self.sent)}")


class Env:
    def __init__(self, db: sqlite3.Connection, tmp_path: Path, *, channel: bool = True) -> None:
        self.clock = Clock()
        self.model = FakeModel()
        self.channel = FakeChannel(db, self.clock) if channel else None
        self.healthy = True
        self.free = 50 * GB
        self.db = db
        self.monitor = HealthMonitor(
            db,
            model=self.model,
            channel=self.channel,
            owner="justin" if channel else None,
            daemon_healthy=lambda: self.healthy,
            db_path=tmp_path / "test.db",
            model_meta={"model": "qwen", "api": "ollama"},
            clock=self.clock,
            disk_free=lambda _p: self.free,
        )

    async def tick(self, n: int = 1) -> None:
        for _ in range(n):
            await self.monitor.evaluate()
            self.clock.advance(60)

    @property
    def sent(self) -> list[tuple[str, str, str | None]]:
        assert self.channel is not None
        return self.channel.sent


@pytest.fixture
def env(db: sqlite3.Connection, tmp_path: Path) -> Env:
    return Env(db, tmp_path)


def _state(db: sqlite3.Connection, check: str) -> dict[str, Any]:
    return health_snapshot(db)[check]


async def test_all_ok_first_round(env: Env) -> None:
    await env.tick()
    snap = health_snapshot(env.db)
    assert {c: s["state"] for c, s in snap.items()} == {
        "model": "ok",
        "imessage": "ok",
        "loop": "ok",
        "store": "ok",
    }
    assert snap["model"]["since"] == iso(T0)
    assert env.sent == []
    assert env.monitor.deadman_reason() is None
    # The model probe is cached in the CLI's shape for the model_status builtin.
    cached = kv_get(env.db, "model.probe")
    assert cached["ok"] is True and cached["model"] == "qwen" and cached["api"] == "ollama"
    assert set(cached) == {
        "ok", "model_present", "ctx_ok", "thinking_off_ok", "latency_ms", "detail",
        "model", "api", "checked_at",
    }  # fmt: skip


async def test_health_unknown_without_monitor(db: sqlite3.Connection) -> None:
    snap = health_snapshot(db)
    assert set(snap) == {"model", "imessage", "loop", "store"}
    assert all(s["state"] == "unknown" and s["since"] is None for s in snap.values())


async def test_model_probe_every_five_minutes(env: Env) -> None:
    await env.tick(5)  # t=0..4m
    assert env.model.calls == 1
    await env.tick()  # t=5m
    assert env.model.calls == 2


async def test_model_degraded_alerts_once_then_recovers(env: Env) -> None:
    env.model.result = DOWN_PROBE
    await env.tick()
    s = _state(env.db, "model")
    assert s["state"] == "degraded" and s["detail"] == "model unreachable or not pulled"
    assert env.sent == []  # one degraded evaluation is not enough
    # The cached probe has no URL in it.
    assert "http" not in kv_get(env.db, "model.probe")["detail"]

    env.clock.advance(300 - 60)  # next probe due
    await env.tick()
    assert len(env.sent) == 1
    owner, text, key = env.sent[0]
    assert owner == "justin"
    assert text == "hey, some issues here: model: model unreachable or not pulled"
    assert key is not None and key.startswith("health:model:")

    for _ in range(10):  # still down for a while: no repeat alert
        env.clock.advance(300)
        await env.tick()
    assert len(env.sent) == 1

    env.model.result = OK_PROBE
    env.clock.advance(300)
    await env.tick()
    assert _state(env.db, "model")["state"] == "ok"
    assert len(env.sent) == 2
    assert env.sent[1][1] == "resolved: model is healthy again"
    assert env.sent[1][2] == f"{key}:resolved"
    # No message ever carries a URL.
    assert not any(re.search(r"://", t) for _, t, _ in env.sent)
    actions = [
        r["action"]
        for r in env.db.execute("SELECT action FROM audit_log WHERE action LIKE 'health.%'")
    ]
    assert actions == ["health.alert", "health.resolved"]


async def test_alert_cooldown_six_hours(env: Env) -> None:
    env.healthy = False
    await env.tick(2)
    assert len(env.sent) == 1  # alert
    env.healthy = True
    await env.tick()
    assert len(env.sent) == 2  # resolved
    env.healthy = False  # a new incident within 6h: no alert
    await env.tick(5)
    assert len(env.sent) == 2
    assert _state(env.db, "loop")["state"] == "degraded"
    env.clock.advance(6 * 3600)  # cooldown over, incident ongoing: alert now
    await env.tick()
    assert len(env.sent) == 3
    assert "loop is not ticking" in env.sent[2][1]


async def test_loop_dead_letters_and_failed_tasks(env: Env) -> None:
    audit(env.db, "event.dead_letter", detail="x")
    await env.tick()
    s = _state(env.db, "loop")
    assert s["state"] == "degraded"
    assert s["detail"] == "1 events dead-lettered in the last hour"


async def test_store_low_disk(env: Env) -> None:
    env.free = 1 * GB
    await env.tick(2)
    s = _state(env.db, "store")
    assert s["state"] == "degraded" and s["detail"] == "low disk: 1.0GB free"
    assert len(env.sent) == 1
    env.free = 3 * GB
    await env.tick()
    assert _state(env.db, "store")["state"] == "ok"


async def test_imessage_degraded_means_no_imessage_and_deadman_reason(env: Env) -> None:
    assert env.channel is not None
    env.channel.selftest = SelfTestResult(False, "Automation denied: allow it in System Settings")
    env.model.result = DOWN_PROBE
    await env.tick(5)
    assert _state(env.db, "imessage")["state"] == "degraded"
    assert env.sent == []  # no iMessage attempt for any check
    reason = env.monitor.deadman_reason()
    assert reason is not None
    assert reason.startswith("imessage: imessage selftest failed: Automation denied")
    assert "model: model unreachable" in reason


async def test_selftest_schedule(env: Env) -> None:
    assert env.channel is not None
    await env.tick()
    assert env.channel.selftests == 1  # none recorded yet: run now
    await env.tick(10)
    assert env.channel.selftests == 1
    env.clock.advance(6 * 3600)
    await env.tick()
    assert env.channel.selftests == 2
    # Two consecutive reader errors force a self-test.
    env.channel.reader.update(ok=False, last_error="database is locked")
    await env.tick()
    assert env.channel.selftests == 2
    assert _state(env.db, "imessage")["detail"] == "imessage reader error: database is locked"
    await env.tick()
    assert env.channel.selftests == 3


async def test_selftest_retried_while_failing(env: Env) -> None:
    assert env.channel is not None
    env.channel.selftest = SelfTestResult(False, "timed out")
    await env.tick()
    assert env.channel.selftests == 1
    env.channel.selftest = SelfTestResult(True, "ok")
    env.clock.advance(1800)
    await env.tick()
    assert env.channel.selftests == 2
    assert _state(env.db, "imessage")["state"] == "ok"


async def test_send_failure_is_retried_next_round(env: Env) -> None:
    assert env.channel is not None
    env.channel.fail_sends = True
    env.healthy = False
    await env.tick(2)
    assert env.sent == []
    env.channel.fail_sends = False
    await env.tick()
    assert len(env.sent) == 1


async def test_state_survives_restart(env: Env, db: sqlite3.Connection, tmp_path: Path) -> None:
    env.healthy = False
    await env.tick(2)
    assert len(env.sent) == 1
    # A new monitor (daemon restart) picks up the alerted incident: no duplicate alert.
    again = Env(db, tmp_path)
    again.clock.now = env.clock.now
    again.healthy = False
    await again.tick(3)
    assert again.sent == []
    again.healthy = True
    await again.tick()
    assert [t for _, t, _ in again.sent] == ["resolved: loop is healthy again"]


async def test_no_channel_reports_through_deadman(db: sqlite3.Connection, tmp_path: Path) -> None:
    e = Env(db, tmp_path, channel=False)
    await e.tick()
    assert _state(db, "imessage")["state"] == "unknown"
    assert e.monitor.deadman_reason() is None
    e.free = 0
    await e.tick()
    assert e.monitor.deadman_reason() == "store: low disk: 0.0GB free"


def test_scrub() -> None:
    assert scrub("see https://hc-ping.com/abc-123 now") == "see <url> now"
    assert len(scrub("x" * 500)) == 120


def test_channels_snapshot(db: sqlite3.Connection) -> None:
    assert channels_snapshot(db, imessage_enabled=False, channel=None) == [
        {
            "name": "imessage",
            "enabled": False,
            "ok": None,
            "last_read_at": None,
            "last_selftest": None,
        }
    ]
    kv_set(db, "imessage.selftest", {"ok": False, "at": iso(T0), "detail": "Automation denied"})
    ch = FakeChannel(db, Clock())
    [row] = channels_snapshot(db, imessage_enabled=True, channel=ch)
    assert row == {
        "name": "imessage",
        "enabled": True,
        "ok": True,
        "last_read_at": iso(T0),
        "last_selftest": {"ok": False, "at": iso(T0), "detail": "Automation denied"},
    }


# --- dead-man -----------------------------------------------------------------------------


async def test_deadman_fail_carries_reason_and_never_logs_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[tuple[str, str, bytes]] = []
    reason: list[str | None] = [None]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(500 if len(seen) > 3 else 200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    url = "https://hc.example/secret-uuid"
    task = asyncio.create_task(
        deadman_task(url, 0.01, lambda: True, client, reason=lambda: reason[0])
    )
    await asyncio.sleep(0.03)
    reason[0] = "imessage: imessage selftest failed: Automation denied"
    await asyncio.sleep(0.03)
    task.cancel()
    assert seen[0] == ("GET", "/secret-uuid", b"")
    assert ("POST", "/secret-uuid/fail", reason[0].encode()) in seen
    assert "secret-uuid" not in caplog.text


async def test_deadman_stale_loop_body() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task = asyncio.create_task(deadman_task("https://hc.example/u", 0.01, lambda: False, client))
    await asyncio.sleep(0.02)
    task.cancel()
    assert bodies[0] == b"event loop stale"


# --- no Slack in core ----------------------------------------------------------------------

_SLACK_IMPORT = re.compile(r"^\s*(?:import|from)\s+slack", re.MULTILINE | re.IGNORECASE)


def test_no_slack_imports_in_core() -> None:
    root = Path(__file__).resolve().parents[1] / "packages"
    files = [
        *(root / "resonant" / "src").rglob("*.py"),
        *(root / "resonant-sdk" / "src").rglob("*.py"),
    ]
    assert files
    offenders = [str(p) for p in files if _SLACK_IMPORT.search(p.read_text())]
    assert offenders == []
    deps = (root / "resonant" / "pyproject.toml").read_text().lower()
    assert "slack" not in deps


def test_cli_health_section(capsys: pytest.CaptureFixture[str]) -> None:
    from resonant.cli import _echo_health  # pyright: ignore[reportPrivateUsage]

    _echo_health(
        {
            "model": {"state": "degraded", "since": iso(T0), "detail": "model context too small"},
            "imessage": {"state": "ok", "since": iso(T0), "detail": "ok"},
            "loop": {"state": "unknown", "since": None, "detail": None},
        },
        live=True,
    )
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "health"
    assert out[1] == f"  model     DEGRADED since {iso(T0)}: model context too small"
    assert out[2] == f"  imessage  ok since {iso(T0)}"
    assert out[3] == "  loop      unknown"

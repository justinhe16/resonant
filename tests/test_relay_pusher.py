"""The status relay's pusher (resonant.dashboard.pusher, DB-3) against a fake Supabase."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from test_dashboard_snapshot import SECRET, _busy  # pyright: ignore[reportPrivateUsage]

from resonant.config import RelayConfig, load_settings
from resonant.dashboard import pusher as pusher_mod
from resonant.dashboard.pusher import (
    FAIL_AFTER,
    INGEST_PATH,
    SUPABASE_KEY_REF,
    SnapshotPusher,
    clear_relay_health,
    data_size,
)
from resonant.dashboard.snapshot import (
    RELAY_HEALTH_KV,
    Snapshot,
    build_snapshot,
    to_remote,
)
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store.db import kv_get, kv_set, utcnow

URL = "https://abcdefghijklmnop.supabase.co"
ANON = "fake-fake-fake-fake-fake"  # a fake anon key
TOKEN = "t" * 64  # a fake ingest token
LEAK = SECRET  # planted in every local-only text field by _busy
T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


@dataclass
class FakeSupabase:
    """``POST /rest/v1/rpc/relay_ingest`` with the migration's rules (token hash, remote tier,
    size cap). ``script`` holds what the next requests get: a status code or an exception."""

    token_sha256: str = hashlib.sha256(TOKEN.encode()).hexdigest()
    script: list[int | Exception] = field(default_factory=list[int | Exception])
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])
    stored: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return httpx.Response(step, json={"message": f"scripted {step} {TOKEN}"})
        if request.method != "POST" or request.url.path != INGEST_PATH:
            return httpx.Response(404)
        if request.headers.get("apikey") != ANON:
            return httpx.Response(401)
        body = json.loads(request.content)
        if hashlib.sha256(str(body.get("p_token", "")).encode()).hexdigest() != self.token_sha256:
            return httpx.Response(403)
        data = body["p_data"]
        if data_size(data) > 65536:
            return httpx.Response(413)
        if data.get("tier") != "remote":
            return httpx.Response(400)
        self.stored.append(data)
        return httpx.Response(204)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def _seed_local_text(conn: sqlite3.Connection) -> None:
    """Text in every local-only place (run asks, PR links, questions, approvals, pulse titles,
    trust rules, repos, health details), each holding ``LEAK``. None of it may leave."""
    _busy(conn, utcnow())


def make_pusher(
    db: sqlite3.Connection,
    fake: FakeSupabase,
    *,
    keychain: FakeKeychain | None = None,
    sleep: Callable[[float], Any] | None = None,
    **config: Any,
) -> SnapshotPusher:
    settings = load_settings()
    cfg = RelayConfig(enabled=True, supabase_url=URL, anon_key=ANON, **config)
    kc = keychain if keychain is not None else FakeKeychain(items={SUPABASE_KEY_REF: TOKEN})

    async def snapshot() -> Snapshot:
        return build_snapshot(db, settings, started_at=T0, now=utcnow(), version="abcdef12")

    return SnapshotPusher(
        db,
        cfg,
        snapshot=snapshot,
        secrets=SecretStore(kc),
        client=fake.client(),
        sleep=sleep or Sleeps(),
        clock=lambda: T0,
        jitter=lambda: 0.0,
    )


def health(db: sqlite3.Connection) -> dict[str, Any]:
    return cast(dict[str, Any], kv_get(db, RELAY_HEALTH_KV))


# --- what leaves -----------------------------------------------------------------------------


async def test_only_the_remote_tier_leaves(db: sqlite3.Connection) -> None:
    _seed_local_text(db)
    fake = FakeSupabase()
    p = make_pusher(db, fake)
    outcome = await p.push_once()
    assert outcome.ok, outcome
    [request] = fake.requests
    raw = request.content.decode()
    assert LEAK not in raw and "dori" not in raw  # no text, no repo names
    body = json.loads(raw)
    assert set(body) == {"p_token", "p_data"}
    data = body["p_data"]
    snap = Snapshot.model_validate(data)
    assert snap.tier == "remote"
    assert snap == to_remote(snap)  # nothing local left to strip
    for key in ("trust_rules", "repos"):
        assert key not in data
    for part in ("runs", "approvals", "questions", "pulse"):
        assert "items" not in data[part]
    assert "missing" not in data["runner"]
    assert all("reason" not in h for h in data["health"])
    assert data["approvals"]["open"] == 1 and data["paused"] is True  # counts and states do
    assert fake.stored == [data]


async def test_request_shape_and_credentials(db: sqlite3.Connection) -> None:
    fake = FakeSupabase()
    await make_pusher(db, fake).push_once()
    [r] = fake.requests
    assert str(r.url) == URL + INGEST_PATH
    assert r.url.scheme == "https"
    assert r.headers["apikey"] == ANON
    assert r.headers["authorization"] == f"Bearer {ANON}"
    # The ingest token is in the body only: never in the URL or a header.
    assert TOKEN not in str(r.url)
    assert all(TOKEN not in v for v in r.headers.values())
    assert json.loads(r.content)["p_token"] == TOKEN


async def test_the_secret_is_read_every_push(db: sqlite3.Connection) -> None:
    kc = FakeKeychain(items={SUPABASE_KEY_REF: TOKEN})
    p = make_pusher(db, FakeSupabase(), keychain=kc)
    await p.push_once()
    await p.push_once()
    assert len([c for c in kc.calls if c.argv[-1] == "-w"]) == 2


async def test_pushes_while_paused(db: sqlite3.Connection) -> None:
    kv_set(db, "paused", True)
    kv_set(db, "autonomy", "off")
    fake = FakeSupabase()
    assert (await make_pusher(db, fake).push_once()).ok
    assert fake.stored[0]["paused"] is True
    assert fake.stored[0]["autonomy"] == "off"


# --- retries and health ----------------------------------------------------------------------


async def test_retries_transient_errors_then_lands(db: sqlite3.Connection) -> None:
    fake = FakeSupabase(script=[503, httpx.ConnectError("refused")])
    sleeps = Sleeps()
    p = make_pusher(db, fake, sleep=sleeps)
    outcome = await p.push_once()
    assert outcome.ok
    assert len(fake.requests) == 3
    assert sleeps.calls == [2.0, 4.0]  # exponential backoff (no jitter in the test)
    assert health(db) == {
        "state": "ok",
        "since": T0.isoformat(timespec="milliseconds"),
        "detail": None,
    }


@pytest.mark.parametrize(
    "step", [500, 502, 429, httpx.ConnectTimeout("slow"), httpx.ReadError("reset")]
)
async def test_retryable_failures(db: sqlite3.Connection, step: int | Exception) -> None:
    fake = FakeSupabase(script=[step] * 3)
    p = make_pusher(db, fake)
    outcome = await p.push_once()
    assert not outcome.ok and not outcome.fatal
    assert len(fake.requests) == 3  # max_attempts, then wait for the next push
    assert health(db)["state"] == "degraded"
    assert "after 3 attempts" in health(db)["detail"]


async def test_degraded_then_failing_then_recovered(db: sqlite3.Connection) -> None:
    fake = FakeSupabase(script=[503] * (3 * FAIL_AFTER))
    p = make_pusher(db, fake, max_attempts=3)
    states: list[str] = []
    for _ in range(FAIL_AFTER):
        await p.push_once()
        states.append(health(db)["state"])
    assert states == ["degraded"] * (FAIL_AFTER - 1) + ["failing"]
    assert (await p.push_once()).ok
    assert health(db)["state"] == "ok" and p.failures == 0


@pytest.mark.parametrize("code", [401, 403])
async def test_rejected_credentials_fail_at_once_without_retry(
    db: sqlite3.Connection, code: int
) -> None:
    fake = FakeSupabase(script=[code])
    p = make_pusher(db, fake)
    outcome = await p.push_once()
    assert outcome.fatal
    assert len(fake.requests) == 1
    h = health(db)
    assert h["state"] == "failing"
    assert SUPABASE_KEY_REF in h["detail"] and f"HTTP {code}" in h["detail"]


@pytest.mark.parametrize("code", [400, 404, 413, 422])
async def test_refused_snapshot_is_not_retried(db: sqlite3.Connection, code: int) -> None:
    fake = FakeSupabase(script=[code])
    outcome = await make_pusher(db, fake).push_once()
    assert outcome.fatal and len(fake.requests) == 1
    assert health(db)["state"] == "failing"


async def test_wrong_token_is_rejected_by_the_server(db: sqlite3.Connection) -> None:
    fake = FakeSupabase(token_sha256=hashlib.sha256(b"another").hexdigest())
    outcome = await make_pusher(db, fake).push_once()
    assert outcome.fatal and "HTTP 403" in outcome.detail


async def test_missing_secret_sends_nothing(db: sqlite3.Connection) -> None:
    fake = FakeSupabase()
    outcome = await make_pusher(db, fake, keychain=FakeKeychain()).push_once()
    assert outcome.fatal
    assert fake.requests == []
    assert health(db)["state"] == "failing"
    assert f"missing secret {SUPABASE_KEY_REF}" in health(db)["detail"]


async def test_locked_keychain_is_degraded(db: sqlite3.Connection) -> None:
    fake = FakeSupabase()
    outcome = await make_pusher(db, fake, keychain=FakeKeychain(fail_with=36)).push_once()
    assert not outcome.ok and not outcome.fatal
    assert fake.requests == []
    assert health(db)["state"] == "degraded"
    assert "keychain" in health(db)["detail"]


async def test_snapshot_too_large_is_not_sent(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", 100)
    fake = FakeSupabase()
    outcome = await make_pusher(db, fake).push_once()
    assert outcome.fatal and "too large" in outcome.detail
    assert fake.requests == []


async def test_a_broken_snapshot_is_a_failed_push(db: sqlite3.Connection) -> None:
    async def broken() -> Snapshot:
        raise sqlite3.OperationalError(f"disk I/O error {LEAK}")

    fake = FakeSupabase()
    p = make_pusher(db, fake)
    p._snapshot = broken  # pyright: ignore[reportPrivateUsage]
    outcome = await p.push_once()
    assert not outcome.ok and outcome.detail == "snapshot failed (OperationalError)"
    assert fake.requests == []


async def test_since_is_kept_while_the_state_holds(db: sqlite3.Connection) -> None:
    fake = FakeSupabase()
    p = make_pusher(db, fake)
    await p.push_once()
    first = health(db)["since"]
    p._clock = lambda: T0 + timedelta(minutes=5)  # pyright: ignore[reportPrivateUsage]
    await p.push_once()
    assert health(db)["since"] == first


# --- redaction -------------------------------------------------------------------------------


async def test_errors_never_carry_secrets(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    leaky = [
        httpx.ConnectError(f"failed {URL}?token={TOKEN} key={ANON}"),
        httpx.ReadTimeout(f"timeout {TOKEN}"),
        RuntimeError(f"boom {TOKEN}"),  # an unexpected error from the transport
    ]
    fake = FakeSupabase(script=[*leaky, 401])
    p = make_pusher(db, fake, max_attempts=1)
    details: list[str] = []
    for _ in range(4):
        details.append((await p.push_once()).detail)
    assert details[2] == "request failed (RuntimeError)"
    for text in (json.dumps(health(db)), caplog.text):
        assert TOKEN not in text and ANON not in text
    # The project URL isn't secret (httpx's own request log shows it), but the row never needs it.
    assert URL not in json.dumps(health(db))


async def test_redactor_covers_a_detail_that_quotes_the_token(db: sqlite3.Connection) -> None:
    p = make_pusher(db, FakeSupabase(), max_attempts=1)

    async def quoting(body: bytes) -> pusher_mod.PushOutcome:
        return pusher_mod.PushOutcome(False, False, f"echo {TOKEN} and {ANON}")

    p._post = quoting  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    outcome = await p.push_once()
    assert TOKEN not in outcome.detail and ANON not in outcome.detail
    assert "[redacted]" in outcome.detail


# --- the run loop ----------------------------------------------------------------------------


async def test_run_pushes_every_period_and_survives_a_crash(db: sqlite3.Connection) -> None:
    fake = FakeSupabase()
    ticks = iter([0.0, 3.0, 100.0, 100.5, 200.0, 260.0])
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    p = make_pusher(db, fake, sleep=sleep, every_s=60)
    p._monotonic = lambda: next(ticks)  # pyright: ignore[reportPrivateUsage]
    calls = 0
    real = p.push_once

    async def flaky() -> pusher_mod.PushOutcome:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("unexpected")
        return await real()

    p.push_once = flaky  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await p.run()
    # 60 - 3 elapsed; then 60 - 0.5; then a push that overran the period goes again at once.
    assert sleeps == [57.0, 59.5, 0.0]
    assert calls == 3


async def test_run_owns_and_closes_its_client(db: sqlite3.Connection) -> None:
    settings = load_settings()
    cfg = RelayConfig(enabled=True, supabase_url=URL, anon_key=ANON)

    async def snapshot() -> Snapshot:
        return build_snapshot(db, settings, started_at=T0)

    async def stop(_: float) -> None:
        raise asyncio.CancelledError

    p = SnapshotPusher(db, cfg, snapshot=snapshot, secrets=SecretStore(FakeKeychain()), sleep=stop)
    assert p.client.follow_redirects is False
    with pytest.raises(asyncio.CancelledError):
        await p.run()  # the secret is missing, so nothing is sent
    assert p.client.is_closed


def test_clear_relay_health(db: sqlite3.Connection) -> None:
    kv_set(db, RELAY_HEALTH_KV, {"state": "failing", "since": None, "detail": "x"})
    clear_relay_health(db)
    assert kv_get(db, RELAY_HEALTH_KV) is None


# --- the health row in the snapshot ----------------------------------------------------------


def test_relay_row_in_the_snapshot(db: sqlite3.Connection, tmp_path: Path) -> None:
    settings = load_settings()
    snap = build_snapshot(db, settings, started_at=T0)
    assert "relay" not in {h.name for h in snap.health}
    since = T0.isoformat()
    kv_set(db, RELAY_HEALTH_KV, {"state": "failing", "since": since, "detail": "HTTP 401: x"})
    snap = build_snapshot(db, settings, started_at=T0)
    [row] = [h for h in snap.health if h.name == "relay"]
    assert row.state == "failing" and row.since == T0 and row.reason == "HTTP 401: x"
    [remote] = [h for h in to_remote(snap).health if h.name == "relay"]
    assert remote.state == "failing" and remote.reason is None
    kv_set(db, RELAY_HEALTH_KV, {"state": "bogus"})
    [row] = [h for h in build_snapshot(db, settings, started_at=T0).health if h.name == "relay"]
    assert row.state == "unknown"


# --- config ----------------------------------------------------------------------------------


def test_relay_config_defaults_off() -> None:
    cfg = load_settings().relay
    assert cfg.enabled is False and cfg.every_s == 60 and cfg.supabase_url is None


@pytest.mark.parametrize(
    "url",
    [
        "http://abc.supabase.co",
        "https://user:pw@abc.supabase.co",
        "https://abc.supabase.co/rest/v1",
        "https://abc.supabase.co?x=1",
        "https://abc.supabase.co#f",
        "https://127.0.0.1",
        "https://localhost",
        "ftp://abc.supabase.co",
        "abc.supabase.co",
    ],
)
def test_relay_url_must_be_bare_https(url: str) -> None:
    with pytest.raises(ValueError, match=r"relay\.supabase_url"):
        RelayConfig(supabase_url=url)


def test_relay_url_trailing_slash_is_dropped() -> None:
    assert RelayConfig(supabase_url=URL + "/").supabase_url == URL


def test_relay_enabled_needs_url_and_key() -> None:
    with pytest.raises(ValueError, match=r"relay\.enabled needs"):
        RelayConfig(enabled=True, supabase_url=URL)
    with pytest.raises(ValueError, match=r"relay\.enabled needs"):
        RelayConfig(enabled=True, anon_key=ANON)
    with pytest.raises(ValueError, match="anon_key"):
        RelayConfig(anon_key="has spaces in it, not a key")
    with pytest.raises(ValueError):
        RelayConfig(every_s=5)


# --- the daemon ------------------------------------------------------------------------------


def _port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_daemon_without_relay_sends_nothing_and_clears_the_row(resonant_home: Path) -> None:
    from resonant.daemon import Daemon
    from resonant.store.db import open_db

    settings = load_settings(api={"port": _port()})
    conn = open_db(settings.db_path)
    kv_set(conn, RELAY_HEALTH_KV, {"state": "failing", "since": None, "detail": "old"})
    conn.close()
    daemon = Daemon(settings, secrets=SecretStore(FakeKeychain()))
    assert daemon.pusher is None
    assert kv_get(daemon.conn, RELAY_HEALTH_KV) is None
    daemon.conn.close()


async def test_daemon_pushes_the_remote_tier(resonant_home: Path) -> None:
    from resonant.api import local_snapshot
    from resonant.daemon import Daemon

    settings = load_settings(
        api={"port": _port()},
        relay={"enabled": True, "supabase_url": URL, "anon_key": ANON, "every_s": 15},
    )
    kc = FakeKeychain(items={SUPABASE_KEY_REF: TOKEN})
    daemon = Daemon(settings, secrets=SecretStore(kc))
    assert daemon.pusher is not None
    fake = FakeSupabase()
    daemon.pusher = SnapshotPusher(
        daemon.conn,
        settings.relay,
        snapshot=lambda: local_snapshot(daemon.state),
        secrets=SecretStore(kc),
        client=fake.client(),
    )
    run = asyncio.create_task(daemon.run())
    for _ in range(200):
        if fake.stored:
            break
        await asyncio.sleep(0.02)
    daemon.stop()
    assert await asyncio.wait_for(run, 5) == 0
    [data] = fake.stored
    assert Snapshot.model_validate(data).tier == "remote"
    assert data["dry_run"] is True

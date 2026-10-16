from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from starlette.types import Message

from resonant.api import (
    RUNNER_CHECK_FAILED,
    DaemonState,
    LoopbackOnly,
    create_app,
    host_name,
)
from resonant.components import Components
from resonant.config import is_loopback, load_settings
from resonant.dashboard.snapshot import Snapshot
from resonant.loop import Loop, NewTask, SlotPool
from resonant.runners.claude import ClaudeRunner
from resonant.store.db import kv_set, utcnow


async def test_healthz_and_status(db: sqlite3.Connection) -> None:
    settings = load_settings()
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    loop.create_task(NewTask(kind="k", runner="echo", priority=1))
    kv_set(db, "paused", True)
    app = create_app(DaemonState(settings, db, loop, started_at=utcnow()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/healthz")).json() == {"ok": True}
        status = (await client.get("/api/status")).json()
    assert status["tasks"] == {"queued": 1}
    assert status["dry_run"] is True
    assert status["paused"] is True
    assert status["autonomy"] == "on"
    assert status["uptime_s"] >= 0


async def test_channels_and_health_without_monitor(db: sqlite3.Connection) -> None:
    settings = load_settings()
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    app = create_app(DaemonState(settings, db, loop, started_at=utcnow()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        channels = (await client.get("/api/channels")).json()
        status = (await client.get("/api/status")).json()
    assert channels == [
        {
            "name": "imessage",
            "enabled": False,
            "ok": None,
            "last_read_at": None,
            "last_selftest": None,
        }
    ]
    assert set(status["health"]) == {"model", "imessage", "loop", "store"}
    assert status["health"]["model"] == {
        "state": "unknown",
        "since": None,
        "detail": None,
        "checked_at": None,
    }


# --- the dashboard -----------------------------------------------------------------------


def _app(db: sqlite3.Connection, dist: Path) -> FastAPI:
    settings = load_settings()
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    return create_app(DaemonState(settings, db, loop, started_at=utcnow()), dashboard_dist=dist)


def _client(app: FastAPI, base_url: str = "http://127.0.0.1:7777", **kw: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, **kw), base_url=base_url)


async def test_dashboard_is_the_local_snapshot(db: sqlite3.Connection, tmp_path: Path) -> None:
    kv_set(db, "paused", True)
    async with _client(_app(db, tmp_path)) as client:
        r = await client.get("/api/dashboard")
    assert r.status_code == 200
    snap = Snapshot.model_validate(r.json())
    assert snap.tier == "local" and snap.paused is True
    assert snap.slots.max == 2 and snap.started_at is not None
    assert snap.runner.set_up is False  # no Claude runner wired
    assert r.json()["trust_rules"] == [] and r.json()["runs"]["items"] == []


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:7777", "http://localhost:7777", "http://[::1]:7777", "http://127.0.0.2"],
)
async def test_loopback_hosts_are_served(
    db: sqlite3.Connection, tmp_path: Path, base_url: str
) -> None:
    async with _client(_app(db, tmp_path), base_url) as client:
        assert (await client.get("/api/dashboard")).status_code == 200
        assert (await client.get("/healthz")).status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "127.0.0.1.evil.example",
        "evil.example:7777",
        "10.0.0.5:7777",
        "",
        "[::1]evil.example",
        "[::1].evil.example:80",
        "localhost:abc",
        "localhost.evil.example",
        "::1",  # an IPv6 Host must be bracketed
        "user@127.0.0.1",
    ],
)
async def test_a_non_loopback_host_header_is_refused(
    db: sqlite3.Connection, tmp_path: Path, host: str
) -> None:
    """A page that rebinds its DNS name to 127.0.0.1 still sends its own Host."""
    async with _client(_app(db, tmp_path)) as client:
        for path in ("/api/dashboard", "/api/status", "/", "/config.json"):
            r = await client.get(path, headers={"host": host})
            assert r.status_code == 403, path
            assert r.json() == {"detail": "loopback only"}


async def test_a_non_loopback_client_is_refused(db: sqlite3.Connection, tmp_path: Path) -> None:
    async with _client(_app(db, tmp_path), client=("192.168.1.20", 50000)) as client:
        assert (await client.get("/api/dashboard")).status_code == 403
        assert (await client.get("/healthz")).status_code == 403


@pytest.mark.parametrize("client", [("::1", 5000), ("127.0.0.1", 5000), None])
async def test_loopback_clients_pass(client: tuple[str, int] | None) -> None:
    status = await _guard({"client": client, "headers": [(b"host", b"127.0.0.1:7777")]})
    assert status == 200


async def test_no_host_or_two_hosts_is_refused() -> None:
    assert await _guard({"client": ("127.0.0.1", 1), "headers": []}) == 403
    two = [(b"host", b"127.0.0.1"), (b"host", b"evil.example")]
    assert await _guard({"client": ("127.0.0.1", 1), "headers": two}) == 403


async def test_a_refused_websocket_is_closed() -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(msg: Message) -> None:
        sent.append(msg)

    scope = {"type": "websocket", "client": ("127.0.0.1", 1), "headers": [(b"host", b"evil")]}
    await LoopbackOnly(_ok_app)(scope, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def _ok_app(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _guard(scope: dict[str, Any]) -> int:
    """Run ``LoopbackOnly`` on a hand-built http scope (httpx always sets one Host)."""
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(msg: Message) -> None:
        sent.append(msg)

    full = {"type": "http", "method": "GET", "path": "/", "query_string": b"", **scope}
    await LoopbackOnly(_ok_app)(full, receive, send)
    return int(sent[0]["status"])


def test_host_name() -> None:
    assert host_name("127.0.0.1:7777") == "127.0.0.1"
    assert host_name("[::1]:7777") == "::1"
    assert host_name("[::1]") == "::1"
    assert host_name("localhost") == "localhost"
    for bad in ("::1", "[::1]evil", "[::1].evil:80", "a:b:c", "localhost:abc", "", "a b"):
        assert host_name(bad) is None, bad
    assert is_loopback("::1") and is_loopback("LOCALHOST") and is_loopback("127.0.0.2")
    assert not is_loopback("localhost.evil.example") and not is_loopback("")


async def test_dashboard_is_not_cached(db: sqlite3.Connection, tmp_path: Path) -> None:
    async with _client(_app(db, tmp_path)) as client:
        r = await client.get("/api/dashboard")
    assert r.headers["cache-control"] == "no-store"


async def test_a_failing_runner_check_doesnt_break_the_dashboard(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    class Boom(ClaudeRunner):
        def __init__(self) -> None:
            self.calls = 0

        async def setup_gaps(self) -> tuple[list[str], list[str]]:
            self.calls += 1
            raise OSError("security timed out")

    settings = load_settings()
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    state = DaemonState(settings, db, loop, started_at=utcnow())
    boom = Boom()
    state.components = cast(Components, SimpleNamespace(runners={"claude": boom}))
    app = create_app(state, dashboard_dist=tmp_path)
    async with _client(app) as client:
        first = await client.get("/api/dashboard")
        second = await client.get("/api/dashboard")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["runner"] == {"set_up": False, "missing": [RUNNER_CHECK_FAILED]}
    assert boom.calls == 1  # cached like a successful check


async def test_placeholder_when_the_build_is_missing(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    async with _client(_app(db, tmp_path / "nope")) as client:
        r = await client.get("/")
        config = (await client.get("/config.json")).json()
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "dashboard/dist" in r.text and "/api/dashboard" in r.text
    assert config == {"source": "local"}


async def test_serves_the_build(db: sqlite3.Connection, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>UI</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / "config.json").write_text('{"source": "supabase"}')
    (tmp_path / "secret.txt").write_text("outside")
    async with _client(_app(db, dist)) as client:
        index = await client.get("/")
        js = await client.get("/assets/app.js")
        config = (await client.get("/config.json")).json()
        api = await client.get("/api/dashboard")
        outside = await client.get("/%2E%2E/secret.txt")
        missing = await client.get("/nope.js")
        (dist / "leak.txt").symlink_to(tmp_path / "secret.txt")
        leak = await client.get("/leak.txt")
    assert leak.status_code == 404  # a symlink out of the build is never followed
    assert index.status_code == 200 and "<title>UI</title>" in index.text
    assert js.status_code == 200 and js.text == "console.log(1)"
    assert config == {"source": "local"}  # the daemon is always the local source
    assert api.status_code == 200 and api.json()["tier"] == "local"
    assert outside.status_code == 404 and missing.status_code == 404

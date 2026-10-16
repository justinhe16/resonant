from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from resonant.api import DaemonState, create_app, host_name, is_loopback_host
from resonant.config import load_settings
from resonant.dashboard.snapshot import Snapshot
from resonant.loop import Loop, NewTask, SlotPool
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
    "host", ["evil.example", "127.0.0.1.evil.example", "evil.example:7777", "10.0.0.5:7777", ""]
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


def test_host_name_strips_the_port() -> None:
    assert host_name("127.0.0.1:7777") == "127.0.0.1"
    assert host_name("[::1]:7777") == "[::1]"
    assert host_name("::1") == "::1"
    assert host_name("localhost") == "localhost"
    assert is_loopback_host("[::1]") and is_loopback_host("::1") and is_loopback_host("LOCALHOST")
    assert not is_loopback_host("localhost.evil.example") and not is_loopback_host("")


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
    assert index.status_code == 200 and "<title>UI</title>" in index.text
    assert js.status_code == 200 and js.text == "console.log(1)"
    assert config == {"source": "local"}  # the daemon is always the local source
    assert api.status_code == 200 and api.json()["tier"] == "local"
    assert outside.status_code == 404 and missing.status_code == 404

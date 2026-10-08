from __future__ import annotations

import sqlite3

import httpx

from resonant.api import DaemonState, create_app
from resonant.config import load_settings
from resonant.loop import Loop, NewTask, SlotPool
from resonant.store.db import kv_set, utcnow


async def test_healthz_and_status(db: sqlite3.Connection) -> None:
    settings = load_settings()
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    loop.create_task(NewTask(kind="k", runner="echo", priority=1))
    kv_set(db, "paused", True)
    app = create_app(DaemonState(settings, db, loop, started_at=utcnow()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
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
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
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

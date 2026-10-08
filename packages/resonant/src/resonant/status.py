"""Daemon status snapshot, shared by the API (/api/status) and the CLI."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from resonant import __version__
from resonant.config import Settings
from resonant.monitor import health_snapshot
from resonant.store import tasks as repo
from resonant.store.db import iso, kv_get, utcnow


def collect_status(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    started_at: datetime | None = None,
    last_tick: datetime | None = None,
    inflight: int | None = None,
) -> dict[str, Any]:
    now = utcnow()
    return {
        "version": __version__,
        "now": iso(now),
        "uptime_s": None if started_at is None else round((now - started_at).total_seconds()),
        "last_tick": None if last_tick is None else iso(last_tick),
        "dry_run": settings.dry_run,
        "autonomy": kv_get(conn, "autonomy", "on"),
        "paused": kv_get(conn, "paused", False),
        "tasks": repo.count_by_status(conn),
        "waiting": repo.count_waiting_by_reason(conn),
        "inflight": inflight,
        "health": health_snapshot(conn),
    }

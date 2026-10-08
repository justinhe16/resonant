"""Local HTTP API. Bound to loopback only; exposed to the tailnet with `tailscale serve`."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI

from resonant import __version__
from resonant.config import Settings
from resonant.loop import Loop
from resonant.status import collect_status


@dataclass
class DaemonState:
    settings: Settings
    conn: sqlite3.Connection
    loop: Loop
    started_at: datetime


def create_app(state: DaemonState) -> FastAPI:
    app = FastAPI(title="resonant", version=__version__, docs_url=None, redoc_url=None)

    # Handlers are async so they run on the event loop thread that owns the sqlite connection.
    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return {"ok": True}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return collect_status(
            state.conn,
            state.settings,
            started_at=state.started_at,
            last_tick=state.loop.last_tick,
            inflight=state.loop.inflight,
        )

    return app

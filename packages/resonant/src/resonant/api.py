"""Local HTTP API. Bound to loopback only; exposed to the tailnet with `tailscale serve`."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from resonant import __version__
from resonant.config import Settings
from resonant.loop import Loop
from resonant.status import collect_status

if TYPE_CHECKING:
    from resonant.components import Components


@dataclass
class DaemonState:
    settings: Settings
    conn: sqlite3.Connection
    loop: Loop
    started_at: datetime
    components: Components | None = None  # set by the daemon after construction


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

    @app.get("/api/tools")
    async def tools() -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        if state.components is None:
            return []
        return [
            {
                "name": s.name,
                "extension": s.extension,
                "effect": s.effect.value,
                "level": s.effective_level.value,
                "description": s.description,
                "args_schema": s.args_schema,
            }
            for s in state.components.registry.specs()
        ]

    return app

"""Local HTTP API and the dashboard. Bound to loopback only; remote status is a push-only relay.

Config validation keeps ``api.host`` on loopback. On top of that every request must come from
a loopback client and name a loopback ``Host`` (``localhost`` or a loopback IP, any port), so
a web page that rebinds its DNS name to 127.0.0.1 can't read the dashboard through the owner's
browser. Anything else gets 403.

``/`` serves the committed dashboard build (``dashboard/dist``, DB-2) when it is there, and a
placeholder page when it isn't. ``/config.json`` is always ``{"source": "local"}``: the build
served here reads ``/api/dashboard``, whatever ``config.json`` the build ships.
"""

from __future__ import annotations

import ipaddress
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from resonant import __version__
from resonant.config import Settings
from resonant.dashboard.snapshot import build_snapshot
from resonant.loop import Loop
from resonant.monitor import channels_snapshot
from resonant.status import collect_status

if TYPE_CHECKING:
    from resonant.components import Components
    from resonant.monitor import HealthMonitor

# packages/resonant/src/resonant/api.py -> <repo>/dashboard/dist
DASHBOARD_DIST = Path(__file__).resolve().parents[4] / "dashboard" / "dist"
RUNNER_CHECK_TTL_S = 60.0  # the runner's set-up check asks the Keychain (by name, no values)

PLACEHOLDER_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Resonant dashboard</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem}
code{background:#8882;padding:0 .25rem;border-radius:4px}</style></head>
<body><h1>Resonant dashboard</h1>
<p>The dashboard UI isn't built in this checkout: <code>dashboard/dist</code> is missing.</p>
<p>The daemon is up. Its data is at <a href="/api/dashboard"><code>/api/dashboard</code></a>
and <a href="/api/status"><code>/api/status</code></a>.</p>
<p>To build the UI: <code>cd dashboard &amp;&amp; npm ci &amp;&amp; npm run build</code>,
then restart the daemon.</p></body></html>
"""


@dataclass
class DaemonState:
    settings: Settings
    conn: sqlite3.Connection
    loop: Loop
    started_at: datetime
    components: Components | None = None  # set by the daemon after construction
    monitor: HealthMonitor | None = None  # set when the health monitor runs (iMessage on)
    # (monotonic time, ClaudeRunner.setup_gaps() list) of the last runner check
    runner_check: tuple[float, list[str]] | None = field(default=None, repr=False)


def is_loopback_host(host: str) -> bool:
    """``host`` (an IP, ``[v6]`` or a name, without a port) is loopback."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.removeprefix("[").removesuffix("]")).is_loopback
    except ValueError:
        return False


def host_name(header: str) -> str:
    """A Host header without its port: ``127.0.0.1:7777`` -> ``127.0.0.1``,
    ``[::1]:7777`` -> ``[::1]``; a bare IPv6 address is kept whole."""
    if header.startswith("["):
        return header.split("]", 1)[0] + "]"
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


class LoopbackOnly:
    """403 for a client that isn't on loopback or a Host header that isn't a loopback name."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not _loopback_request(scope):
            if scope["type"] == "http":
                await JSONResponse({"detail": "loopback only"}, status_code=403)(
                    scope, receive, send
                )
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


def _loopback_request(scope: Scope) -> bool:
    client = scope.get("client")
    if client is not None and not is_loopback_host(str(client[0])):
        return False
    hosts = [v for k, v in scope.get("headers", ()) if k == b"host"]
    return len(hosts) == 1 and is_loopback_host(host_name(hosts[0].decode("latin-1")))


async def _runner_missing(state: DaemonState) -> list[str] | None:
    """The Claude runner's set-up gaps (cached for ``RUNNER_CHECK_TTL_S``), or None when no
    Claude runner is wired in this daemon (iMessage off)."""
    from resonant.runners.claude import ClaudeRunner

    runner = None if state.components is None else state.components.runners.get("claude")
    if not isinstance(runner, ClaudeRunner):
        return None
    now = time.monotonic()
    if state.runner_check is None or now - state.runner_check[0] > RUNNER_CHECK_TTL_S:
        missing, _how = await runner.setup_gaps()
        state.runner_check = (now, missing)
    return state.runner_check[1]


def create_app(state: DaemonState, *, dashboard_dist: Path | None = None) -> FastAPI:
    app = FastAPI(title="resonant", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(LoopbackOnly)
    dist = DASHBOARD_DIST if dashboard_dist is None else dashboard_dist

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

    @app.get("/api/dashboard")
    async def dashboard() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """The local-tier snapshot (docs/dashboard-plan.md §3, docs/snapshot.schema.json)."""
        snap = build_snapshot(
            state.conn,
            state.settings,
            started_at=state.started_at,
            last_tick=state.loop.last_tick,
            slots=state.loop.slots,
            runner_missing=await _runner_missing(state),
        )
        return snap.model_dump(mode="json")

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

    @app.get("/api/channels")
    async def channels() -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        return channels_snapshot(
            state.conn,
            imessage_enabled=state.settings.channels.imessage.enabled,
            channel=None if state.monitor is None else state.monitor.channel,
        )

    @app.get("/config.json")
    async def dashboard_config() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"source": "local"}

    # Last, so the routes above win over files of the same name in the build.
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def placeholder() -> str:  # pyright: ignore[reportUnusedFunction]
            return PLACEHOLDER_HTML

    return app

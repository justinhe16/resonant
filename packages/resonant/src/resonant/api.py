"""Local HTTP API and the dashboard. Bound to loopback only; remote status is a push-only relay.

Config validation keeps ``api.host`` on loopback. On top of that every request must come from
a loopback client and name a loopback ``Host`` (``localhost`` or a loopback IP, any port), so
a web page that rebinds its DNS name to 127.0.0.1 can't read the dashboard through the owner's
browser. Anything else gets 403.

``/`` serves the committed dashboard build (``dashboard/dist``, DB-2) when it is there, its
``config.json`` (``{"source": "local"}``) included, and a placeholder page when it isn't.
Every response carries a strict CSP (scripts from this origin only, no framing) and
``nosniff``; the build has no inline script.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from resonant import __version__
from resonant.config import Settings, is_loopback
from resonant.dashboard.snapshot import Snapshot, build_snapshot
from resonant.loop import Loop
from resonant.monitor import channels_snapshot
from resonant.status import collect_status

if TYPE_CHECKING:
    from resonant.components import Components
    from resonant.monitor import HealthMonitor

# packages/resonant/src/resonant/api.py -> <repo>/dashboard/dist
DASHBOARD_DIST = Path(__file__).resolve().parents[4] / "dashboard" / "dist"
RUNNER_CHECK_TTL_S = 60.0  # the runner's set-up check asks the Keychain (by name, no values)
RUNNER_CHECK_FAILED = "the runner's set-up check failed (see the daemon log)"

log = logging.getLogger(__name__)

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
    runner_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


_HOST_V6 = re.compile(r"\[([0-9A-Fa-f:.]+)\](?::\d{1,5})?")
_HOST_NAME = re.compile(r"([^:\[\]/@\s]+)(?::\d{1,5})?")


def host_name(header: str) -> str | None:
    """A Host header's host, without its port or brackets (``127.0.0.1:7777`` ->
    ``127.0.0.1``, ``[::1]:7777`` -> ``::1``), or None when it isn't a well-formed
    ``host[:port]``."""
    m = _HOST_V6.fullmatch(header) or _HOST_NAME.fullmatch(header)
    return None if m is None else m.group(1)


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


CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)


class SecurityHeaders:
    """Adds ``SECURITY_HEADERS`` to every http response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", ()), *SECURITY_HEADERS]
            await send(message)

        await self.app(scope, receive, with_headers)


def _loopback_request(scope: Scope) -> bool:
    # No client address (an in-process caller, never a TCP peer) passes; the bind is loopback.
    client = scope.get("client")
    if client is not None and not is_loopback(str(client[0])):
        return False
    hosts = [v for k, v in scope.get("headers", ()) if k == b"host"]
    if len(hosts) != 1:
        return False
    host = host_name(hosts[0].decode("latin-1"))
    return host is not None and is_loopback(host)


async def runner_missing(state: DaemonState) -> list[str] | None:
    """The Claude runner's set-up gaps (cached for ``RUNNER_CHECK_TTL_S``), or None when no
    Claude runner is wired in this daemon (iMessage off). A check that raises shows as one
    gap; the dashboard never fails because of it."""
    from resonant.runners.claude import ClaudeRunner

    runner = None if state.components is None else state.components.runners.get("claude")
    if not isinstance(runner, ClaudeRunner):
        return None
    async with state.runner_lock:  # concurrent polls share one check
        now = time.monotonic()
        if state.runner_check is None or now - state.runner_check[0] > RUNNER_CHECK_TTL_S:
            try:
                missing, _how = await runner.setup_gaps()
            except Exception as e:
                log.warning("runner set-up check failed: %s", type(e).__name__)
                missing = [RUNNER_CHECK_FAILED]
            state.runner_check = (now, missing)
        return state.runner_check[1]


async def local_snapshot(state: DaemonState) -> Snapshot:
    """The local-tier snapshot of this daemon, as ``/api/dashboard`` serves it. The status
    relay (``dashboard.pusher``) pushes ``to_remote`` of the same."""
    return build_snapshot(
        state.conn,
        state.settings,
        started_at=state.started_at,
        last_tick=state.loop.last_tick,
        slots=state.loop.slots,
        runner_missing=await runner_missing(state),
    )


def create_app(state: DaemonState, *, dashboard_dist: Path | None = None) -> FastAPI:
    app = FastAPI(title="resonant", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(LoopbackOnly)
    app.add_middleware(SecurityHeaders)  # outermost: the 403s carry them too
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
    async def dashboard(response: Response) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """The local-tier snapshot (docs/dashboard-plan.md §3, docs/snapshot.schema.json)."""
        response.headers["Cache-Control"] = "no-store"  # it carries asks and approval texts
        return (await local_snapshot(state)).model_dump(mode="json")

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

    # Last, so the routes above win over files of the same name in the build. The build's
    # config.json is served from it ({"source": "local"}; the Fly relay ships its own).
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")
    else:
        # A source checkout without a build (or an install outside the repo).
        log.info("dashboard build not found at %s; serving the placeholder", dist)

        @app.get("/", response_class=HTMLResponse)
        async def placeholder() -> str:  # pyright: ignore[reportUnusedFunction]
            return PLACEHOLDER_HTML

    return app

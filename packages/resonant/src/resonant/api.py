"""Local HTTP API and the dashboard. Bound to loopback only; remote status is a push-only relay.

Config validation keeps ``api.host`` on loopback. On top of that every request must come from
a loopback client and name a loopback ``Host`` (``localhost`` or a loopback IP, any port), so
a web page that rebinds its DNS name to 127.0.0.1 can't read the dashboard through the owner's
browser. Anything else gets 403.

``/`` serves the committed dashboard build (``dashboard/dist``, DB-2) when it is there, its
``config.json`` (``{"source": "local"}``) included, and a placeholder page when it isn't.
Every response carries a strict CSP (scripts from this origin only, no framing) and
``nosniff``; the build has no inline script.

PER-306: ``GET /api/instances`` lists this daemon and the other instances of its
``instances:`` config, and ``GET /api/instances/<name>/dashboard`` proxies one's
``/api/dashboard`` over loopback (its Remote Control links and approval codes stripped), so
the page can switch instances and stay same-origin.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from resonant import __version__
from resonant.config import Settings, is_loopback
from resonant.dashboard.diagram import cached_scan
from resonant.dashboard.snapshot import Snapshot, build_snapshot, panel_day, strip_local_json
from resonant.extensions.panel_history import parse_day
from resonant.gateway.channel import ChannelError
from resonant.loop import Loop
from resonant.monitor import channels_snapshot
from resonant.notify_owner import MAX_BODY, bearer, parse_body, token_ref_for
from resonant.notify_owner import PATH as NOTIFY_OWNER_PATH
from resonant.redact import Redactor
from resonant.secret_expiry import Probe as SecretFacts
from resonant.secret_expiry import SecretsHealth
from resonant.secrets import SecretError, SecretMissing, SecretStore
from resonant.status import collect_status
from resonant.store import extensions as ext_store
from resonant.store.db import iso, utcnow

if TYPE_CHECKING:
    from resonant.components import Components
    from resonant.extensions.pulse import Pulse
    from resonant.monitor import HealthMonitor

# packages/resonant/src/resonant/api.py -> <repo>/dashboard/dist
DASHBOARD_DIST = Path(__file__).resolve().parents[4] / "dashboard" / "dist"
RUNNER_CHECK_TTL_S = 60.0  # the runner's set-up check asks the Keychain (by name, no values)
RUNNER_CHECK_FAILED = "the runner's set-up check failed (see the daemon log)"
INSTANCE_TIMEOUT_S = 5.0  # PER-306: another instance's /api/dashboard, over loopback
INSTANCE_MAX_BYTES = 8 * 1024 * 1024

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
    pulse: Pulse | None = None  # set when the channel stack runs (PER-301's notify-owner)
    # (monotonic time, ClaudeRunner.setup_gaps() list) of the last runner check
    runner_check: tuple[float, list[str]] | None = field(default=None, repr=False)
    runner_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # PER-306: since when each linked instance hasn't answered (cleared by an answer)
    instance_down: dict[str, datetime] = field(default_factory=dict[str, datetime], repr=False)
    # PER-306: tests swap in a fake transport for the instance proxy
    instance_transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)


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


def _core_jobs(state: DaemonState) -> list[tuple[str, str, str]]:
    """``(id, cron, tz)`` of the scheduler's core jobs (none without a ``Scheduler``)."""
    from resonant.extensions.scheduler import Scheduler  # a cycle at import time

    scheduler = state.loop.scheduler
    if not isinstance(scheduler, Scheduler):
        return []
    return [(j.job_id, j.schedule.expr, str(j.schedule.tz)) for j in scheduler.jobs if j.core]


async def local_snapshot(state: DaemonState, *, redactor: Redactor | None = None) -> Snapshot:
    """The local-tier snapshot of this daemon, as ``/api/dashboard`` serves it. The status
    relay (``dashboard.pusher``) pushes ``to_remote`` of the same, built with a ``redactor``
    that holds the relay's own secrets."""
    return build_snapshot(
        state.conn,
        state.settings,
        started_at=state.started_at,
        last_tick=state.loop.last_tick,
        slots=state.loop.slots,
        runner_missing=await runner_missing(state),
        core_jobs=_core_jobs(state),
        redactor=redactor,
        launch_agents=await asyncio.to_thread(cached_scan),  # PER-277's diagram, 60 s cache
        owner=_owner(state),
        slack=_slack_health(state),
        secret_facts=await _secret_facts(state),
    )


async def _secret_facts(state: DaemonState) -> SecretFacts | None:
    """PER-315: the monitor's cached Keychain presence and ``mdat`` for the expiry refs (names
    and dates; re-read at most hourly), or None without a monitor (the stored expiries only)."""
    check: object = getattr(state.monitor, "secrets_check", None)  # None without a monitor
    if not isinstance(check, SecretsHealth):
        return None
    try:
        return await check.facts(utcnow())
    except Exception as e:  # the dashboard never fails because of the Keychain
        log.warning("secret expiry facts failed: %s", type(e).__name__)
        return None


def _slack_health(state: DaemonState) -> dict[str, Any] | None:
    """PER-306: the live Slack adapter's ``health()``, or None without one."""
    live = None if state.monitor is None else state.monitor.channels.get("slack")
    if live is None:
        return None
    try:
        return dict(live.health())
    except Exception as e:  # the dashboard never fails because of an adapter
        log.warning("slack health failed: %s", type(e).__name__)
        return None


def instances_list(state: DaemonState) -> dict[str, Any]:
    """PER-306 ``GET /api/instances``: this instance first, then the linked ones
    (``settings.instances``). One entry (no ``instances:`` config) means no switcher."""
    me = state.settings.instance
    return {
        "instances": [
            {"name": me.name, "display_name": me.display_name, "self": True},
            *(
                {"name": i.name, "display_name": i.name.capitalize(), "self": False}
                for i in state.settings.instances
            ),
        ]
    }


def _down(state: DaemonState, name: str, detail: str) -> JSONResponse:
    since = state.instance_down.setdefault(name, utcnow())
    return JSONResponse(
        {"detail": detail, "since": iso(since)},
        status_code=502,
        headers={"Cache-Control": "no-store"},
    )


async def instance_dashboard(state: DaemonState, name: str) -> JSONResponse:
    """PER-306 ``GET /api/instances/<name>/dashboard``: a linked instance's local-tier
    snapshot, fetched from its loopback ``/api/dashboard`` (``INSTANCE_TIMEOUT_S``), with
    its Remote Control links and approval codes stripped (``strip_local_json``), so the
    page stays same-origin (CSP ``connect-src 'self'``). 404 for a name that isn't linked;
    502 ``{detail, since}`` while it doesn't answer, or answers with anything but its own
    snapshot. Read-only: the one GET, no cookies, no redirects followed."""
    link = next((i for i in state.settings.instances if i.name == name), None)
    if link is None:
        return JSONResponse({"detail": "no such instance"}, status_code=404)
    try:
        async with (
            httpx.AsyncClient(
                transport=state.instance_transport,
                timeout=INSTANCE_TIMEOUT_S,
                follow_redirects=False,
                trust_env=False,  # never a proxy from the environment
            ) as client,
            client.stream(
                "GET", f"{link.url}/api/dashboard", headers={"accept": "application/json"}
            ) as r,
        ):
            if r.status_code != 200:
                return _down(state, name, f"{name} answered HTTP {r.status_code}")
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > INSTANCE_MAX_BYTES:
                    return _down(state, name, f"{name} sent more than {INSTANCE_MAX_BYTES} bytes")
    except httpx.HTTPError as e:
        log.info("instance %s isn't answering: %s", name, type(e).__name__)
        return _down(state, name, f"{name} isn't answering")
    try:
        data: object = json.loads(bytes(body))
    except ValueError:
        return _down(state, name, f"{name} didn't send JSON")
    if not isinstance(data, dict):
        return _down(state, name, f"{name} didn't send a snapshot")
    snap = cast(dict[str, Any], data)
    who: object = snap.get("instance")
    said = cast(dict[str, Any], who).get("name") if isinstance(who, dict) else None
    if said is not None and said != name:
        return _down(state, name, f"{link.url} answers as {str(said)[:32]}, not {name}")
    state.instance_down.pop(name, None)
    return JSONResponse(strip_local_json(snap), headers={"Cache-Control": "no-store"})


def _owner(state: DaemonState) -> str | None:
    """The owner principal's name, or None when principals.yaml isn't loaded (PER-304: the
    snapshot marks every other principal's words ``outside``)."""
    c = state.components
    return c.principals.owner if c is not None and c.principals_loaded else None


def _secrets(state: DaemonState) -> SecretStore:
    c = state.components
    found = None if c is None else c.executor.secrets
    return found if found is not None else SecretStore(service=state.settings.keychain_service)


async def notify_owner(state: DaemonState, request: Request) -> JSONResponse:
    """PER-301 ``POST /api/notify-owner`` (``resonant.notify_owner``): another instance's
    urgent alert, texted to the owner as personal Resonant's own. 401 without a bearer token
    or with the wrong one, 422 for any other body than the contract's (nothing stored), 404
    when this daemon has no channel or no ``core/notify_token_<instance>``."""
    token = bearer(request.headers.get("authorization"))
    if token is None:
        return JSONResponse({"detail": "missing bearer token"}, status_code=401)
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_BODY:
        return JSONResponse({"detail": f"body over {MAX_BODY} bytes"}, status_code=422)
    raw = await request.body()
    body = parse_body(raw)
    if isinstance(body, str):
        return JSONResponse({"detail": body}, status_code=422)
    pulse = state.pulse
    ref = token_ref_for(body.instance)
    if pulse is None or ref is None or body.instance == state.settings.instance.name:
        return JSONResponse({"detail": "not set up"}, status_code=404)
    try:
        expected = await _secrets(state).get(ref)
    except SecretMissing:
        return JSONResponse({"detail": "not set up"}, status_code=404)
    except SecretError as e:
        log.warning("notify-owner token unreadable: %s", e)
        return JSONResponse({"detail": "keychain unavailable"}, status_code=503)
    if not hmac.compare_digest(token.encode(), expected.encode()):
        log.warning("notify-owner: wrong token for instance %s", body.instance)
        return JSONResponse({"detail": "notify token mismatch"}, status_code=401)
    try:
        outcome, reason = await pulse.ingest_instance(
            body.instance, body.key, body.title, clear=body.clear
        )
    except ValueError as e:  # the store's own checks (parse_body should have caught it)
        return JSONResponse({"detail": str(e)[:200]}, status_code=422)
    except ChannelError as e:  # stored untold: the instance's retry sends it
        log.warning("notify-owner text failed: %s", type(e).__name__)
        return JSONResponse({"detail": "channel unavailable"}, status_code=503)
    log.info("notify-owner from %s: %s", body.instance, outcome)
    return JSONResponse({"outcome": outcome, "reason": reason})


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

    @app.get("/api/instances")
    async def instances() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return instances_list(state)

    @app.get("/api/instances/{name}/dashboard")
    async def instance_dashboard_route(name: str) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return await instance_dashboard(state, name)

    @app.get("/api/ext/{name}/panels/{panel_id}/days/{day}")
    async def panel_history_day(  # pyright: ignore[reportUnusedFunction]
        name: str, panel_id: str, day: str, response: Response
    ) -> dict[str, Any]:
        """PER-286: one archived day of a history panel (read-only). 404 for an unknown
        extension, panel or day, and for a day recorded as none or failed."""
        response.headers["Cache-Control"] = "no-store"
        record = ext_store.get_extension(state.conn, name) if len(name) <= 64 else None
        found = (
            panel_day(state.conn, record, panel_id, day)
            if record is not None and len(panel_id) <= 64 and parse_day(day) is not None
            else None
        )
        if found is None:
            raise HTTPException(status_code=404, detail="no such day")
        return found

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

    @app.post(NOTIFY_OWNER_PATH)
    async def notify_owner_route(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return await notify_owner(state, request)

    @app.get("/api/channels")
    async def channels() -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        live = {} if state.monitor is None else state.monitor.channels
        return channels_snapshot(
            state.conn,
            imessage_enabled=state.settings.channels.imessage.enabled,
            channel=live.get("imessage"),
            others={n: c for n, c in live.items() if n != "imessage"},
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

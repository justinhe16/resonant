from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from starlette.types import Message

from resonant.api import (
    CSP,
    DASHBOARD_DIST,
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
    assert set(status["health"]) == {"model", "imessage", "loop", "store", "backup", "secrets"}
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
    state.components = cast(
        Components, SimpleNamespace(runners={"claude": boom}, principals_loaded=False)
    )
    app = create_app(state, dashboard_dist=tmp_path)
    async with _client(app) as client:
        first = await client.get("/api/dashboard")
        second = await client.get("/api/dashboard")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["runner"] == {"set_up": False, "missing": [RUNNER_CHECK_FAILED], "gaps": 1}
    assert boom.calls == 1  # cached like a successful check


async def test_placeholder_when_the_build_is_missing(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    async with _client(_app(db, tmp_path / "nope")) as client:
        r = await client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "dashboard/dist" in r.text and "/api/dashboard" in r.text
    assert r.headers["content-security-policy"] == CSP


async def test_serves_the_build(db: sqlite3.Connection, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>UI</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / "config.json").write_text('{"source": "local"}')
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
    assert config == {"source": "local"}  # the build's own config.json
    assert api.status_code == 200 and api.json()["tier"] == "local"
    assert outside.status_code == 404 and missing.status_code == 404
    for r in (index, js, api, missing):
        assert r.headers["content-security-policy"] == CSP
        assert r.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self';" in CSP and "frame-ancestors 'none'" in CSP


async def test_serves_the_committed_build(db: sqlite3.Connection) -> None:
    """The real dashboard/dist (DB-2): index.html, its relative assets, the local config."""
    async with _client(_app(db, DASHBOARD_DIST)) as client:
        index = await client.get("/")
        config = (await client.get("/config.json")).json()
        assets = re.findall(r'(?:src|href)="\./([^"]+)"', index.text)
        assert assets  # the theme script, the bundle, the stylesheet
        for path in assets:
            assert (await client.get(f"/{path}")).status_code == 200, path
    assert index.status_code == 200 and '<div id="root">' in index.text
    assert "<script>" not in index.text  # no inline script: the CSP allows none
    assert config == {"source": "local"}


# --- PER-301: POST /api/notify-owner (the full contract: test_notify_owner.py) ------------


async def test_notify_owner_needs_a_token_and_a_channel(db: sqlite3.Connection) -> None:
    from resonant.secrets import FakeKeychain, SecretStore

    settings = load_settings()
    state = DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)), utcnow())
    keychain = FakeKeychain(items={"core/notify_token_cori": "cori-token-0123456789"})
    state.components = cast(
        Components,
        SimpleNamespace(executor=SimpleNamespace(secrets=SecretStore(keychain)), runners={}),
    )
    body = {"instance": "cori", "key": "k", "title": "t", "urgency": "urgent", "clear": False}
    transport = httpx.ASGITransport(app=create_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        missing = await client.post("/api/notify-owner", json=body)
        no_stack = await client.post(
            "/api/notify-owner", json=body, headers={"Authorization": "Bearer x"}
        )
        remote = await client.post("/api/notify-owner", json=body, headers={"Host": "evil.example"})
    assert missing.status_code == 401
    assert no_stack.status_code == 404  # no channel stack: nothing can text the owner
    assert remote.status_code == 403  # loopback only, like every route
    assert db.execute("SELECT COUNT(*) FROM pulse_items").fetchone()[0] == 0


# --- PER-306: the instance list and the loopback proxy -------------------------------------


def _linked_state(db: sqlite3.Connection, upstream: httpx.AsyncBaseTransport | None) -> DaemonState:
    from resonant.config import InstanceLink

    settings = load_settings().model_copy(
        update={"instances": [InstanceLink(name="cori", url="http://127.0.0.1:7778")]}
    )
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    return DaemonState(settings, db, loop, started_at=utcnow(), instance_transport=upstream)


def _cori_snapshot(name: str = "cori") -> dict[str, Any]:
    rc = "https://claude.ai/code/session_" + "Cori123"
    return {
        "schema": 2,
        "tier": "local",
        "instance": {"name": name, "display_name": name.capitalize()},
        "runs": {"items": [{"code": "R4", "remote_url": rc, "ask": f"see {rc}"}]},
        "approvals": {
            "items": [{"id": "a1", "code": "4821", "summary": "s"}],
            "recent": [{"id": "a0", "code": "1111", "summary": "t"}],
        },
        "claude_brain": {"state": "up", "remote_url": rc, "tools": []},
        "jobs": [{"id": "J1", "name": "flaky CI"}],
        "channels": {"slack": {"connected": True, "last_event_at": None, "followed_threads": 2}},
        "newer_field": {"remote_url": rc},
    }


async def test_instances_without_config_is_this_one_alone(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    async with _client(_app(db, tmp_path)) as client:
        r = await client.get("/api/instances")
        proxied = await client.get("/api/instances/cori/dashboard")
    assert r.json() == {
        "instances": [{"name": "resonant", "display_name": "Resonant", "self": True}]
    }
    assert proxied.status_code == 404


async def test_the_proxy_strips_links_and_codes(db: sqlite3.Connection, tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_cori_snapshot())

    state = _linked_state(db, httpx.MockTransport(answer))
    async with _client(create_app(state, dashboard_dist=tmp_path)) as client:
        listed = (await client.get("/api/instances")).json()["instances"]
        r = await client.get("/api/instances/cori/dashboard")
    assert [(i["name"], i["self"]) for i in listed] == [("resonant", True), ("cori", False)]
    assert listed[1]["display_name"] == "Cori"
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert str(seen[0].url) == "http://127.0.0.1:7778/api/dashboard"
    assert seen[0].method == "GET" and "cookie" not in seen[0].headers
    body = r.json()
    text = r.text
    assert "claude.ai/code" not in text and "4821" not in text and "1111" not in text
    assert '"remote_url"' not in text
    assert body["runs"]["items"][0]["code"] == "R4"  # a run's code is not an approval's
    assert body["runs"]["items"][0]["ask"] == "see [remote control link]"
    assert body["jobs"] == [{"id": "J1", "name": "flaky CI"}]
    assert body["channels"]["slack"]["followed_threads"] == 2
    assert body["newer_field"] == {}  # a newer schema's field passes, without its link


async def test_an_unreachable_instance_says_since_when(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def answer(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=_cori_snapshot())

    state = _linked_state(db, httpx.MockTransport(answer))
    async with _client(create_app(state, dashboard_dist=tmp_path)) as client:
        first = await client.get("/api/instances/cori/dashboard")
        second = await client.get("/api/instances/cori/dashboard")
        own = await client.get("/api/dashboard")  # personal's view is unaffected
        third = await client.get("/api/instances/cori/dashboard")
    assert first.status_code == 502 and second.status_code == 502
    assert first.json()["detail"] == "cori isn't answering"
    assert first.json()["since"] == second.json()["since"]  # since the first failure
    assert own.status_code == 200 and own.json()["instance"]["name"] == "resonant"
    assert third.status_code == 200 and "cori" not in state.instance_down


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (httpx.Response(500, json={}), "cori answered HTTP 500"),
        (
            httpx.Response(302, headers={"location": "http://evil.example/"}),
            "cori answered HTTP 302",
        ),
        (httpx.Response(200, content=b"<html>"), "cori didn't send JSON"),
        (httpx.Response(200, json=[1, 2]), "cori didn't send a snapshot"),
        (httpx.Response(200, json=_cori_snapshot("resonant")), "answers as resonant, not cori"),
    ],
)
async def test_the_proxy_refuses_anything_but_the_instances_snapshot(
    db: sqlite3.Connection, tmp_path: Path, response: httpx.Response, detail: str
) -> None:
    state = _linked_state(db, httpx.MockTransport(lambda _r: response))
    async with _client(create_app(state, dashboard_dist=tmp_path)) as client:
        r = await client.get("/api/instances/cori/dashboard")
    assert r.status_code == 502 and detail in r.json()["detail"]


def test_instance_links_are_loopback_only() -> None:
    from pydantic import ValidationError

    from resonant.config import InstanceLink, Settings

    assert InstanceLink(name="cori", url="http://127.0.0.1:7778/").url == "http://127.0.0.1:7778"
    for url in ("http://example.com:7778", "https://127.0.0.1:7778", "http://127.0.0.1:7778/x"):
        with pytest.raises(ValidationError):
            InstanceLink(name="cori", url=url)
    with pytest.raises(ValidationError):
        InstanceLink(name="Cori!", url="http://127.0.0.1:7778")
    base = load_settings().model_dump()
    link = {"name": "cori", "url": "http://127.0.0.1:7778"}
    with pytest.raises(ValidationError, match="at most once"):
        Settings.model_validate({**base, "instances": [link, link]})
    with pytest.raises(ValidationError, match="this instance"):
        Settings.model_validate({**base, "instances": [{**link, "name": "resonant"}]})


async def test_the_dashboard_lists_present_credential_expiries(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """PER-315: the monitor's cached Keychain facts decide which refs are present; a row
    whose item is gone is left out, and no value is read."""
    from datetime import timedelta

    from resonant.secret_expiry import SecretsHealth
    from resonant.secrets import FakeKeychain, SecretStore
    from resonant.store import secret_expiry as expiry_rows

    settings = load_settings()
    fake = FakeKeychain(service=settings.keychain_service, items={"github/token": "not-real"})
    now = utcnow()
    for ref in ("github/token", "linear/token"):
        expiry_rows.upsert(db, ref, expires_at=now + timedelta(days=40), source="owner",
                           set_at=now)  # fmt: skip
    check = SecretsHealth(db, SecretStore(fake, service=settings.keychain_service), settings)
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    state = DaemonState(settings, db, loop, started_at=now)
    state.monitor = cast(Any, SimpleNamespace(channels={}, secrets_check=check))
    async with _client(create_app(state, dashboard_dist=tmp_path)) as client:
        body = (await client.get("/api/dashboard")).json()
    assert [(e["ref"], e["source"], e["probe"]) for e in body["secrets"]] == [
        ("github/token", "owner", None)
    ]
    assert "not-real" not in json.dumps(body)
    assert all("-w" not in c.argv for c in fake.calls)


async def test_the_dashboard_carries_the_slack_adapters_state(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    class FakeSlack:
        def health(self) -> dict[str, Any]:
            return {"ok": True, "connected": True, "last_event_at": None, "followed_threads": 4}

    settings = load_settings()
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    state = DaemonState(settings, db, loop, started_at=utcnow())
    state.monitor = cast(Any, SimpleNamespace(channels={"slack": FakeSlack()}))
    async with _client(create_app(state, dashboard_dist=tmp_path)) as client:
        body = (await client.get("/api/dashboard")).json()
    assert body["channels"] == {
        "slack": {"connected": True, "last_event_at": None, "followed_threads": 4}
    }

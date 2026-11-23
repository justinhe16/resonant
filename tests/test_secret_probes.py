"""PER-314: credential probes, one read-only call a day per probe-able secret.

Every ``security`` call goes to ``FakeKeychain`` under a real ``SecretStore`` and every HTTP
request to an ``httpx.MockTransport``: no real Keychain, no real network.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from resonant import secret_expiry as sx
from resonant import secret_probes as sp
from resonant.config import NotifyOwnerConfig, Settings, load_settings
from resonant.dashboard.snapshot import build_snapshot
from resonant.executor import CallContext
from resonant.extensions.scheduler import CoreDeps
from resonant.gateway.channel import MessageRef
from resonant.notify_owner import OwnerBridge
from resonant.ops.relay_deploy import FLY_DEPLOY_TOKEN_REF
from resonant.secrets import (
    ANTHROPIC_KEY_REF,
    CLAUDE_TOKEN_REF,
    GITHUB_APP_KEY_REF,
    FakeKeychain,
    SecretStore,
)
from resonant.store import secret_expiry as rows
from resonant.store.db import kv_get

T0 = datetime(2026, 11, 23, 18, 0, tzinfo=UTC)  # 10:00 in Los Angeles
OWNER = "justin"
CTX = CallContext(secrets={}, idempotency_key="t", task_id="t")
SENTINEL = "s3ntinel-VALUE-0123456789abcdef"


@dataclass
class Hub:
    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    posts: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        self.sent.append((principal, text, dedupe_key))
        return MessageRef("imessage", f"out:{len(self.sent)}")

    async def post_room(self, key: str, text: str, *, dedupe_key: str | None = None) -> MessageRef:
        self.posts.append((key, text, dedupe_key))
        return MessageRef("slack", f"C0:{len(self.posts)}")


@dataclass
class Api:
    """A MockTransport handler: one status (or exception) per host, every request kept."""

    status: int = 200
    headers: dict[str, str] = field(default_factory=dict[str, str])
    body: Any = None
    raise_: type[Exception] | None = None
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])
    by_path: dict[str, tuple[int, dict[str, str], Any]] = field(
        default_factory=dict[str, tuple[int, dict[str, str], Any]]
    )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_ is not None:
            raise self.raise_("boom", request=request)  # type: ignore[call-arg]
        if (hit := self.by_path.get(request.url.path)) is not None:
            status, headers, body = hit
            return httpx.Response(status, headers=headers, json=body)
        return httpx.Response(self.status, headers=self.headers, json=self.body or {})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def deps(
    s: Settings, db: sqlite3.Connection, hub: Hub, bridge: OwnerBridge | None = None
) -> CoreDeps:
    return CoreDeps(s, db, None, hub, OWNER, bridge)  # type: ignore[arg-type]


def delivery(hub: Hub) -> sx.Delivery:
    return sx.Delivery(hub, OWNER)  # type: ignore[arg-type]


def personal(home: Path, **kw: Any) -> Settings:
    return load_settings(home=home, **kw)


def cori(tmp_path: Path) -> Settings:
    return load_settings(
        home=tmp_path / "cori-home",
        instance={"name": "cori"},
        notify_owner={},
        pulse={"heartbeat": {"room": "pulse"}},
        runner={
            "session_env": {
                "FLY_API_TOKEN": "dori/fly_staging_token",
                "VERCEL_TOKEN": "dori/vercel_token",
            }
        },
        secrets={"probes": {"dori/fly_staging_token": {"kind": "fly", "app": "dori-staging"}}},
    )


def keychain(settings: Settings, items: dict[str, str]) -> tuple[SecretStore, FakeKeychain]:
    fake = FakeKeychain(service=settings.keychain_service, items=dict(items))
    return SecretStore(fake, service=settings.keychain_service), fake


def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


TARGETS: dict[str, sp.Target] = {
    "github": sp.Target("github/token", "github"),
    "github_app": sp.Target(GITHUB_APP_KEY_REF, "github_app", app_id=123),
    "linear": sp.Target("linear/token", "linear"),
    "fly": sp.Target("dori/fly_staging_token", "fly", app="dori-staging"),
    "vercel": sp.Target("dori/vercel_token", "vercel"),
    "supabase": sp.Target("dori/supabase_staging_token", "supabase"),
    "anthropic": sp.Target(ANTHROPIC_KEY_REF, "anthropic"),
}
URLS = {
    "github": "https://api.github.com/user",
    "github_app": "https://api.github.com/app",
    "linear": "https://api.linear.app/graphql",
    "fly": "https://api.machines.dev/v1/apps/dori-staging",
    "vercel": "https://api.vercel.com/v2/user",
    "supabase": "https://api.supabase.com/v1/projects",
    "anthropic": "https://api.anthropic.com/v1/models",
}


async def one(kind: str, api: Api, value: str = SENTINEL) -> sp.Outcome:
    async with sp.client(api.transport) as c:
        return await sp.check(TARGETS[kind], value, c)


# --- each kind --------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(TARGETS))
async def test_each_kind_ok_rejected_rate_limit_timeout(kind: str) -> None:
    value = pem() if kind == "github_app" else SENTINEL
    api = Api(200)
    assert (await one(kind, api, value)).state == "ok"
    req = api.requests[0]
    assert str(req.url) == URLS[kind]
    assert req.headers["user-agent"] == "resonant-probe"
    assert req.method == ("POST" if kind == "linear" else "GET")
    assert (await one(kind, Api(401), value)) == sp.Outcome("rejected", 401)
    assert (await one(kind, Api(403), value)) == sp.Outcome("rejected", 403)
    limited = Api(403, headers={"x-ratelimit-remaining": "0"})
    assert (await one(kind, limited, value)) == sp.Outcome("unknown", 403, "rate limit")
    assert (await one(kind, Api(raise_=httpx.ReadTimeout), value)) == sp.Outcome(
        "unknown", None, "timeout"
    )
    assert (await one(kind, Api(503), value)) == sp.Outcome("unknown", 503, "HTTP 503")
    assert (await one(kind, Api(raise_=httpx.ConnectError), value)).state == "unknown"


async def test_auth_headers_per_kind() -> None:
    api = Api(200)
    await one("anthropic", api)
    assert api.requests[0].headers["x-api-key"] == SENTINEL
    assert "authorization" not in api.requests[0].headers
    await one("fly", api, "FlyV1 fm2_abc")
    assert api.requests[1].headers["authorization"] == "FlyV1 fm2_abc"
    await one("linear", api, "lin_api_abc")
    assert api.requests[2].headers["authorization"] == "lin_api_abc"
    assert json.loads(api.requests[2].content) == {"query": "{ viewer { id } }"}
    await one("github", api)
    assert api.requests[3].headers["authorization"] == f"Bearer {SENTINEL}"
    # the app key never leaves: only a JWT signed with it does
    key = pem()
    await one("github_app", api, key)
    jwt = api.requests[4].headers["authorization"].removeprefix("Bearer ")
    assert jwt.count(".") == 2 and key not in str(api.requests[4].headers)
    assert (await one("github_app", api, "not a key")) == sp.Outcome(
        "unknown", None, "unreadable key"
    )


async def test_linear_auth_error_and_no_redirects() -> None:
    body = {"errors": [{"message": "x", "extensions": {"code": "AUTHENTICATION_ERROR"}}]}
    assert (await one("linear", Api(400, body=body))).state == "rejected"
    assert (await one("linear", Api(400, body={"errors": []}))).state == "unknown"
    moved = Api(302, headers={"location": "https://evil.example/"})
    assert (await one("github", moved)) == sp.Outcome("unknown", 302, "HTTP 302")
    assert len(moved.requests) == 1
    assert (await one("github", Api(429))).reason == "rate limit"


async def test_github_and_vercel_expiry() -> None:
    hdr = {"github-authentication-token-expiration": "2027-01-01 00:00:00 UTC"}
    out = await one("github", Api(200, headers=hdr))
    assert out.expires_at == datetime(2027, 1, 1, tzinfo=UTC)
    assert (await one("github", Api(200))).expires_at is None  # classic: no header, nothing
    assert sp.parse_github_expiry("2027-01-01 00:00:00 -0800") == datetime(
        2027, 1, 1, 8, tzinfo=UTC
    )
    assert sp.parse_github_expiry("soon") is None
    ms = int(datetime(2027, 3, 1, tzinfo=UTC).timestamp() * 1000)
    api = Api(200, by_path={"/v5/user/tokens/current": (200, {}, {"token": {"expiresAt": ms}})})
    assert (await one("vercel", api)).expires_at == datetime(2027, 3, 1, tzinfo=UTC)
    api = Api(200, by_path={"/v5/user/tokens/current": (200, {}, {"token": {}})})
    assert (await one("vercel", api)).expires_at is None


# --- which refs -------------------------------------------------------------------------------


def test_plan_follows_the_table_env_and_config(resonant_home: Path, tmp_path: Path) -> None:
    by = {t.ref: t for t in sp.plan(personal(resonant_home))}
    assert by[CLAUDE_TOKEN_REF].why == "no free check" and not by[CLAUDE_TOKEN_REF].probed
    assert by["relay/supabase_key"].why == "checked by the relay pusher"
    assert by["runner/github_token"].kind == "github" and by["runner/github_token"].probed
    assert by["linear/token"].kind == "linear"
    assert by[GITHUB_APP_KEY_REF].why == "no runner.github_app"
    assert by[FLY_DEPLOY_TOKEN_REF].why == "no app"
    s = personal(
        resonant_home,
        relay={"fly_app": "resonant-relay-jhe"},
        runner={"github_app": {"app_id": 7, "installation_id": 8}},
    )
    by = {t.ref: t for t in sp.plan(s)}
    assert by[FLY_DEPLOY_TOKEN_REF] == sp.Target(
        FLY_DEPLOY_TOKEN_REF, "fly", app="resonant-relay-jhe"
    )
    assert by[GITHUB_APP_KEY_REF].app_id == 7 and by[GITHUB_APP_KEY_REF].probed

    c = load_settings(
        home=tmp_path / "c",
        instance={"name": "cori"},
        notify_owner={},
        channels={"slack": {"enabled": True, "team_id": "T0TEAM", "bot_user_id": "U0BOT"}},
        runner={
            "session_env": {
                "FLY_API_TOKEN": "dori/fly_staging_token",
                "VERCEL_TOKEN": "dori/vercel_token",
                "SUPABASE_ACCESS_TOKEN": "dori/supabase_tok",
                "DATABASE_URL": "dori/prod_db_url",
            }
        },
        secrets={
            "probes": {"dori/extra": {"kind": "supabase"}, "dori/vercel_token": {"kind": "off"}}
        },
    )
    by = {t.ref: t for t in sp.plan(c)}
    assert by["dori/fly_staging_token"].why == "no app"  # FLY_API_TOKEN, but no app given
    assert by["dori/vercel_token"].why == "off in config"
    assert by["dori/supabase_tok"].kind == "supabase" and by["dori/supabase_tok"].probed
    assert by["dori/prod_db_url"].why == "no probe for this ref"
    assert by["dori/extra"].kind == "supabase"
    assert by["slack/bot_token"].why == "checked by auth.test"
    assert by["core/notify_token"].why == "checked by the bridge"
    with pytest.raises(ValueError, match=r"secrets\.probes"):
        load_settings(home=tmp_path / "x", secrets={"probes": {"-w": {"kind": "fly"}}})
    with pytest.raises(ValueError, match="Fly app name"):
        load_settings(
            home=tmp_path / "x", secrets={"probes": {"a/b": {"kind": "fly", "app": "Bad App"}}}
        )


async def test_no_probeable_ref_present_makes_no_request(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    s = personal(resonant_home)
    store, fake = keychain(s, {CLAUDE_TOKEN_REF: SENTINEL, "relay/supabase_key": SENTINEL})
    api = Api(200)
    job = sx.secrets_job(deps(s, db, Hub()), clock=lambda: T0, store=store, transport=api.transport)
    assert job is not None
    assert await job.run({}, CTX) == {"quiet": True}
    assert api.requests == []
    assert db.execute("SELECT count(*) FROM kv WHERE key LIKE 'secrets.probe%'").fetchone()[0] == 0
    read = {c.argv[c.argv.index("-a") + 1] for c in fake.calls if "-w" in c.argv}
    assert CLAUDE_TOKEN_REF not in read and "relay/supabase_key" not in read  # never read


async def test_a_locked_keychain_skips_every_probe(
    db: sqlite3.Connection, resonant_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    s = personal(resonant_home)
    store, fake = keychain(s, {"linear/token": SENTINEL, "github/token": SENTINEL})
    fake.fail_with = 36
    api = Api(200)
    with caplog.at_level(logging.INFO):
        out = await sp.watch_probes(
            db,
            store,
            s,
            delivery(Hub()),
            T0,
            transport=api.transport,
        )
    assert out == [] and api.requests == []
    assert len(fake.calls) == 1
    assert sum("keychain unavailable" in r.message for r in caplog.records) == 1


# --- outcomes, expiry rows, alerts -------------------------------------------------------------


async def test_github_expiry_writes_a_probe_row_and_a_sooner_owner_row_wins(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    s = personal(resonant_home)
    store, _ = keychain(s, {"github/token": SENTINEL})
    api = Api(200, headers={"github-authentication-token-expiration": "2027-01-01 00:00:00 UTC"})
    d = delivery(Hub())
    out = await sp.watch_probes(db, store, s, d, T0, transport=api.transport)
    assert out == [{"ref": "github/token", "state": "ok", "status": 200}]
    row = rows.get(db, "github/token")
    assert row is not None and row.source == "probe"
    assert row.expires_at == datetime(2027, 1, 1, tzinfo=UTC)
    assert kv_get(db, "secrets.probe:github/token") == {
        "state": "ok",
        "status": 200,
        "at": "2026-11-23T18:00:00.000+00:00",
    }
    # unchanged next day: not rewritten (set_at stays)
    await sp.watch_probes(db, store, s, d, T0 + timedelta(days=1), transport=api.transport)
    assert rows.get(db, "github/token") == row
    # a sooner owner row is kept
    sooner = datetime(2026, 12, 15, tzinfo=UTC)
    rows.upsert(db, "github/token", expires_at=sooner, source="owner", set_at=T0)
    await sp.watch_probes(db, store, s, d, T0, transport=api.transport)
    kept = rows.get(db, "github/token")
    assert kept is not None and (kept.source, kept.expires_at) == ("owner", sooner)
    # a later owner row gives way to the API's date
    rows.upsert(
        db, "github/token", expires_at=datetime(2028, 1, 1, tzinfo=UTC), source="owner", set_at=T0
    )
    await sp.watch_probes(db, store, s, d, T0, transport=api.transport)
    got = rows.get(db, "github/token")
    assert got is not None and got.source == "probe"


async def test_a_rejection_alerts_once_then_recovery_sends_one_line(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    s = personal(resonant_home)
    store, _ = keychain(s, {"linear/token": SENTINEL})
    api = Api(401)
    hub = Hub()
    job = sx.secrets_job(deps(s, db, hub), clock=lambda: now, store=store, transport=api.transport)
    assert job is not None
    now = T0
    out = await job.run({}, CTX)
    assert out == {
        "probes": [{"ref": "linear/token", "state": "rejected", "status": 401, "alert": "sent"}]
    }
    assert hub.sent == [
        (
            OWNER,
            "🔑 linear/token was rejected (HTTP 401). Renew: resonant secret set linear/token",
            "probe:linear/token:rejected:2026-11-23",
        )
    ]
    health = sx.SecretsHealth(db, store, s)
    r = await health(now)
    assert (r.ok, r.failing, r.detail) == (False, True, "linear/token rejected")
    # the same incident the next day: no new alert
    now = T0 + timedelta(days=1)
    out = await job.run({}, CTX)
    assert out["probes"] == [{"ref": "linear/token", "state": "rejected", "status": 401}]
    assert len(hub.sent) == 1
    # an unknown in between changes nothing
    api.status = 503
    await job.run({}, CTX)
    assert len(hub.sent) == 1
    # fixed: one recovery line, then quiet
    api.status = 200
    now = T0 + timedelta(days=2)
    out = await job.run({}, CTX)
    assert out["probes"][0]["alert"] == "recovered"
    assert hub.sent[-1] == (
        OWNER,
        "✓ linear/token works again",
        "probe:linear/token:recovered:2026-11-23",
    )
    await job.run({}, CTX)
    assert len(hub.sent) == 2
    r = await health(now + timedelta(hours=2))
    assert not r.failing
    audits = db.execute(
        "SELECT decision FROM audit_log WHERE action = 'secrets.probe_alert' ORDER BY id"
    ).fetchall()
    assert [a["decision"] for a in audits] == ["sent", "recovered"]
    # a new incident starts a new key
    api.status = 401
    now = T0 + timedelta(days=5)
    await job.run({}, CTX)
    assert hub.sent[-1][2] == "probe:linear/token:rejected:2026-11-28"


async def test_expired_and_rejected_together_in_health(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    found = [sx.Entry("github/token", True, T0 - timedelta(days=1), "owner", T0)]
    r = sx.health_result(found, T0, "UTC", rejected=["linear/token"])
    assert (r.failing, r.detail) == (True, "expired: github/token; linear/token rejected")


async def test_cori_rejection_goes_through_the_bridge_and_the_room(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    s = cori(tmp_path)
    store, _ = keychain(s, {"dori/fly_staging_token": SENTINEL, "dori/vercel_token": SENTINEL})
    api = Api(401)
    hub = Hub()
    bridge = OwnerBridge(db, NotifyOwnerConfig(), instance="cori", secrets=store)
    job = sx.secrets_job(
        deps(s, db, hub, bridge),
        clock=lambda: T0,
        store=store,
        transport=api.transport,
    )
    assert job is not None
    out = await job.run({}, CTX)
    assert [p["ref"] for p in out["probes"]] == ["dori/fly_staging_token", "dori/vercel_token"]
    assert {str(r.url) for r in api.requests} == {
        "https://api.machines.dev/v1/apps/dori-staging",
        "https://api.vercel.com/v2/user",
    }
    titles = [str(v["title"]) for v in bridge.pending().values()]
    assert len(titles) == 2
    assert all("--instance cori secret set" in t and "rejected (HTTP 401)" in t for t in titles)
    assert [k for k, _, _ in hub.posts] == ["pulse", "pulse"]
    assert all("resonant --instance cori secret set" in t for _, t, _ in hub.posts + hub.sent)
    # recovery clears on the bridge
    api.status = 200
    await job.run({}, CTX)
    box = bridge.pending()
    assert all(v["clear"] is True for v in box.values())
    assert sorted(t for _, t, _ in hub.posts[2:]) == [
        "✓ dori/fly_staging_token works again",
        "✓ dori/vercel_token works again",
    ]


async def test_sentinel_never_leaves_the_one_header(
    db: sqlite3.Connection, resonant_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    s = personal(resonant_home)
    store, _ = keychain(
        s, {"linear/token": SENTINEL, "github/token": SENTINEL, ANTHROPIC_KEY_REF: SENTINEL}
    )

    def leaky(request: httpx.Request) -> httpx.Response:
        # an API that echoes the credential back in its body must not leak it either
        return httpx.Response(401, json={"echo": SENTINEL, "message": f"bad {SENTINEL}"})

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return leaky(request)

    hub = Hub()
    job = sx.secrets_job(
        deps(s, db, hub),
        clock=lambda: T0,
        store=store,
        transport=httpx.MockTransport(handler),
    )
    assert job is not None
    with caplog.at_level(logging.DEBUG):
        out = await job.run({}, CTX)
    assert len(seen) == 3
    for req in seen:
        assert SENTINEL not in str(req.url) and SENTINEL.encode() not in req.content
        holders = [k for k, v in req.headers.items() if SENTINEL in v]
        assert len(holders) == 1 and holders[0] in ("authorization", "x-api-key")
    dumps: list[str] = [json.dumps(out), caplog.text, json.dumps(hub.sent)]
    dumps += [json.dumps(dict(r)) for r in db.execute("SELECT * FROM audit_log")]
    dumps += [json.dumps(dict(r)) for r in db.execute("SELECT * FROM kv")]
    dumps += [json.dumps(dict(r), default=str) for r in db.execute("SELECT * FROM secret_expiry")]
    snap = build_snapshot(db, s, started_at=T0, runner_missing=[], now=T0)
    dumps.append(snap.model_dump_json())
    assert all(SENTINEL not in d for d in dumps)
    assert len(hub.sent) == 3  # each rejection told once, without the value


async def test_probe_all_reports_why_not(resonant_home: Path) -> None:
    s = personal(resonant_home)
    store, _ = keychain(s, {CLAUDE_TOKEN_REF: SENTINEL})
    api = Api(200)
    results, locked = await sp.probe_all(store, sp.plan(s), transport=api.transport)
    by = {r.ref: sp.describe(r) for r in results}
    assert not locked and api.requests == []
    assert by[CLAUDE_TOKEN_REF] == "not probed: no free check"
    assert by["linear/token"] == "not probed: not set"


def _outcome(state: str, status: int | None, reason: str | None = None) -> sp.Result:
    return sp.Result(sp.Target("a/b", "github"), sp.Outcome(state, status, reason))  # type: ignore[arg-type]


def test_describe_and_list_note() -> None:
    describe: Callable[[sp.Result], str] = sp.describe
    assert describe(_outcome("ok", 200)) == "ok"
    assert describe(_outcome("rejected", 401)) == "rejected (401)"
    assert describe(_outcome("unknown", None, "timeout")) == "unknown (timeout)"
    t = sp.Target("a/b", "github")
    assert sp.list_note(t, None) is None
    assert sp.list_note(t, {"state": "rejected", "status": 401}) == "probe rejected (401)"
    assert sp.list_note(t, {"state": "ok", "status": 200}) == "probe ok"
    assert sp.list_note(sp.Target(CLAUDE_TOKEN_REF, None, why="no free check"), None) == (
        "not probed: no free check"
    )
    assert sp.list_note(sp.Target("x/y", None, why="no probe for this ref"), None) is None


async def test_health_stays_failing_through_unknown_and_clears_when_the_ref_goes(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    s = personal(resonant_home)
    store, fake = keychain(s, {"linear/token": SENTINEL})
    api = Api(401)
    d = delivery(Hub())
    await sp.watch_probes(db, store, s, d, T0, transport=api.transport)
    assert sp.rejected_refs(db) == ["linear/token"]
    api.status = 503  # a timeout or 5xx during the incident doesn't clear it
    await sp.watch_probes(db, store, s, d, T0 + timedelta(days=1), transport=api.transport)
    assert sp.rejected_refs(db) == ["linear/token"]
    fake.fail_with = 36  # a locked Keychain forgets nothing
    await sp.watch_probes(db, store, s, d, T0 + timedelta(days=2), transport=api.transport)
    assert sp.rejected_refs(db) == ["linear/token"]
    fake.fail_with = None
    del fake.items["linear/token"]  # removed: no stale failing
    await sp.watch_probes(db, store, s, d, T0 + timedelta(days=3), transport=api.transport)
    assert sp.rejected_refs(db) == []
    assert kv_get(db, "secrets.probe:linear/token") is None
    assert kv_get(db, "secrets.probe_incident:linear/token") is None


async def test_an_unexpected_error_is_unknown_and_hides_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.linear.app":
            raise RuntimeError("odd")
        return httpx.Response(401)

    async with sp.client(httpx.MockTransport(handler)) as c:
        odd = await sp.check(TARGETS["linear"], SENTINEL, c)
        gh = await sp.check(TARGETS["github"], SENTINEL, c)
    assert odd == sp.Outcome("unknown", None, "error: RuntimeError")
    assert gh.state == "rejected"

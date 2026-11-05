"""The status relay's pusher (resonant.dashboard.pusher, DB-3) against a fake Supabase."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from test_dashboard_snapshot import (
    FAKE_TOKEN,
    SECRET,
    SECRET_SAMPLES,
    _busy,  # pyright: ignore[reportPrivateUsage]
    remote_dump,
    seed_secret,
    seed_typical,
    seed_worst,
)

from resonant.config import RelayConfig, load_settings
from resonant.dashboard import pusher as pusher_mod
from resonant.dashboard.pusher import (
    FAIL_AFTER,
    INGEST_PATH,
    SUPABASE_KEY_REF,
    SnapshotPusher,
    clear_relay_health,
    data_size,
)
from resonant.dashboard.snapshot import (
    RELAY_HEALTH_KV,
    Snapshot,
    build_snapshot,
    to_remote,
)
from resonant.redact import Redactor, redact_secrets
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store.claude_brain import SESSION_KV
from resonant.store.db import kv_get, kv_set, utcnow

URL = "https://abcdefghijklmnop.supabase.co"
ANON = "fake-fake-fake-fake-fake"  # a fake anon key
TOKEN = "t" * 64  # a fake ingest token
LEAK = SECRET  # planted in every local-only text field by _busy
T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


@dataclass
class FakeSupabase:
    """``POST /rest/v1/rpc/relay_ingest`` with the migration's rules (token hash, remote tier of
    schema 2, size cap of 256 KiB, no link). ``script`` holds what the next requests get: a
    status code or an exception."""

    token_sha256: str = hashlib.sha256(TOKEN.encode()).hexdigest()
    script: list[int | Exception] = field(default_factory=list[int | Exception])
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])
    stored: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return httpx.Response(step, json={"message": f"scripted {step} {TOKEN}"})
        if request.method != "POST" or request.url.path != INGEST_PATH:
            return httpx.Response(404)
        if request.headers.get("apikey") != ANON:
            return httpx.Response(401)
        body = json.loads(request.content)
        if hashlib.sha256(str(body.get("p_token", "")).encode()).hexdigest() != self.token_sha256:
            return httpx.Response(403)
        data = body["p_data"]
        if data_size(data) > 262144:
            return httpx.Response(413)
        if data.get("tier") != "remote" or data.get("schema") != 2:
            return httpx.Response(400)
        if '"remote_url"' in json.dumps(data):
            return httpx.Response(400)
        self.stored.append(data)
        return httpx.Response(204)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def _seed_local_text(conn: sqlite3.Connection) -> None:
    """Text in every place (run asks, PR links, questions, approvals, pulse titles, trust rules,
    repos, health details), each holding ``LEAK``."""
    _busy(conn, utcnow())


def make_pusher(
    db: sqlite3.Connection,
    fake: FakeSupabase,
    *,
    keychain: FakeKeychain | None = None,
    sleep: Callable[[float], Any] | None = None,
    **config: Any,
) -> SnapshotPusher:
    settings = load_settings()
    fields: dict[str, Any] = {"enabled": True, "supabase_url": URL, "anon_key": ANON, **config}
    cfg = RelayConfig(**fields)
    kc = keychain if keychain is not None else FakeKeychain(items={SUPABASE_KEY_REF: TOKEN})

    async def snapshot(redactor: Redactor) -> Snapshot:
        return build_snapshot(
            db, settings, started_at=T0, now=utcnow(), version="abcdef12", redactor=redactor
        )

    return SnapshotPusher(
        db,
        cfg,
        snapshot=snapshot,
        secrets=SecretStore(kc),
        client=fake.client(),
        sleep=sleep or Sleeps(),
        clock=lambda: T0,
        jitter=lambda: 0.0,
    )


def health(db: sqlite3.Connection) -> dict[str, Any]:
    return cast(dict[str, Any], kv_get(db, RELAY_HEALTH_KV))


# --- what leaves -----------------------------------------------------------------------------


async def test_the_remote_tier_leaves_with_its_text(db: sqlite3.Connection) -> None:
    """PER-276: the same data as the local tier, texts included, minus the Remote Control links
    and approval codes (and nothing else)."""
    _seed_local_text(db)
    kv_set(db, SESSION_KV, {"session_id": "s", "turns": 2, "pid": 1,
                            "remote_url": "https://claude.ai/code/session_abc"})  # fmt: skip
    fake = FakeSupabase()
    p = make_pusher(db, fake)
    outcome = await p.push_once()
    assert outcome.ok, outcome
    [request] = fake.requests
    raw = request.content.decode()
    assert "claude.ai" not in raw and "remote_url" not in raw
    body = json.loads(raw)
    assert set(body) == {"p_token", "p_data"}
    data = body["p_data"]
    snap = Snapshot.model_validate(data)
    assert snap.tier == "remote" and snap.schema_ == 2 and data["trimmed"] == "none"
    assert snap == to_remote(snap)  # nothing local left to strip
    assert data["runs"]["items"][0]["ask"] == f"build {LEAK}"  # text goes
    assert data["approvals"]["items"][0]["summary"] == f"restart {LEAK}"
    assert data["questions"]["items"][0]["text"] == f"which db {LEAK}?"
    assert data["pulse"]["items"][0]["title"] == f"CI red {LEAK}"
    assert data["trust_rules"] and data["repos"] and data["claude_brain"]["turns"] == 2
    assert data["approvals"]["open"] == 1 and data["paused"] is True
    assert '"code"' not in json.dumps(data["approvals"])  # the approval's code stays on the Mini
    assert fake.stored == [data]


async def test_request_shape_and_credentials(db: sqlite3.Connection) -> None:
    fake = FakeSupabase()
    await make_pusher(db, fake).push_once()
    [r] = fake.requests
    assert str(r.url) == URL + INGEST_PATH
    assert r.url.scheme == "https"
    assert r.headers["apikey"] == ANON
    # Not a JWT (like an sb_publishable_ key): apikey alone, never as a Bearer token.
    assert "authorization" not in r.headers
    # The ingest token is in the body only: never in the URL or a header.
    assert TOKEN not in str(r.url)
    assert all(TOKEN not in v for v in r.headers.values())
    assert json.loads(r.content)["p_token"] == TOKEN


async def test_a_legacy_jwt_anon_key_is_also_the_bearer(db: sqlite3.Connection) -> None:
    jwt = "fake-header.fake-payload.fake-signature"  # three dot-separated parts, like a JWT
    fake = FakeSupabase()
    await make_pusher(db, fake, anon_key=jwt).push_once()
    [r] = fake.requests
    assert r.headers["apikey"] == jwt
    assert r.headers["authorization"] == f"Bearer {jwt}"


async def test_the_secret_is_read_every_push(db: sqlite3.Connection) -> None:
    kc = FakeKeychain(items={SUPABASE_KEY_REF: TOKEN})
    p = make_pusher(db, FakeSupabase(), keychain=kc)
    await p.push_once()
    await p.push_once()
    assert len([c for c in kc.calls if c.argv[-1] == "-w"]) == 2


async def test_pushes_while_paused(db: sqlite3.Connection) -> None:
    kv_set(db, "paused", True)
    kv_set(db, "autonomy", "off")
    fake = FakeSupabase()
    assert (await make_pusher(db, fake).push_once()).ok
    assert fake.stored[0]["paused"] is True
    assert fake.stored[0]["autonomy"] == "off"


# --- retries and health ----------------------------------------------------------------------


async def test_retries_transient_errors_then_lands(db: sqlite3.Connection) -> None:
    fake = FakeSupabase(script=[503, httpx.ConnectError("refused")])
    sleeps = Sleeps()
    p = make_pusher(db, fake, sleep=sleeps)
    outcome = await p.push_once()
    assert outcome.ok
    assert len(fake.requests) == 3
    assert sleeps.calls == [2.0, 4.0]  # exponential backoff (no jitter in the test)
    assert health(db) == {
        "state": "ok",
        "since": T0.isoformat(timespec="milliseconds"),
        "detail": None,
    }


@pytest.mark.parametrize(
    "step", [500, 502, 429, httpx.ConnectTimeout("slow"), httpx.ReadError("reset")]
)
async def test_retryable_failures(db: sqlite3.Connection, step: int | Exception) -> None:
    fake = FakeSupabase(script=[step] * 3)
    p = make_pusher(db, fake)
    outcome = await p.push_once()
    assert not outcome.ok and not outcome.fatal
    assert len(fake.requests) == 3  # max_attempts, then wait for the next push
    assert health(db)["state"] == "degraded"
    assert "after 3 attempts" in health(db)["detail"]


async def test_degraded_then_failing_then_recovered(db: sqlite3.Connection) -> None:
    fake = FakeSupabase(script=[503] * (3 * FAIL_AFTER))
    p = make_pusher(db, fake, max_attempts=3)
    states: list[str] = []
    for _ in range(FAIL_AFTER):
        await p.push_once()
        states.append(health(db)["state"])
    assert states == ["degraded"] * (FAIL_AFTER - 1) + ["failing"]
    assert (await p.push_once()).ok
    assert health(db)["state"] == "ok" and p.failures == 0


async def test_a_wedged_client_is_replaced_after_fail_after_failures(
    db: sqlite3.Connection,
) -> None:
    """PER-284: after a Wi-Fi drop the old pool kept failing with ConnectError until a restart.
    From the FAIL_AFTER-th failure in a row on, the pusher drops its own client; a fresh one
    lands the next push."""
    fake = FakeSupabase()

    def wedged(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("stale pool", request=request)

    clients: list[httpx.AsyncClient] = []

    def factory() -> httpx.AsyncClient:
        first = not clients
        clients.append(
            httpx.AsyncClient(transport=httpx.MockTransport(wedged)) if first else fake.client()
        )
        return clients[-1]

    p = make_pusher(db, fake)
    p._client, p._owns_client, p._client_factory = None, True, factory  # pyright: ignore[reportPrivateUsage]
    for n in range(1, FAIL_AFTER):
        assert not (await p.push_once()).ok
        assert len(clients) == 1 and not clients[0].is_closed, n  # kept until FAIL_AFTER
    out = await p.push_once()
    assert not out.ok and out.detail.startswith("network error (ConnectError)")
    assert health(db)["state"] == "failing"
    assert clients[0].is_closed  # dropped on the FAIL_AFTER-th failure
    assert (await p.push_once()).ok  # a fresh client: the push lands
    assert len(clients) == 2 and len(fake.stored) == 1
    assert health(db)["state"] == "ok" and p.failures == 0


async def test_an_injected_client_is_never_dropped(db: sqlite3.Connection) -> None:
    fake = FakeSupabase(script=[503] * (FAIL_AFTER + 1))
    p = make_pusher(db, fake, max_attempts=1)
    client = p.client
    for _ in range(FAIL_AFTER + 1):
        await p.push_once()
    assert p.client is client and not client.is_closed  # the caller's to close
    assert (await p.push_once()).ok


async def test_the_factory_builds_an_owned_client(db: sqlite3.Connection) -> None:
    settings = load_settings()
    cfg = RelayConfig(enabled=True, supabase_url=URL, anon_key=ANON)
    fake = FakeSupabase(script=[503] * FAIL_AFTER)
    built: list[httpx.AsyncClient] = []

    def factory() -> httpx.AsyncClient:
        built.append(fake.client())
        return built[-1]

    async def snapshot(redactor: Redactor) -> Snapshot:
        return build_snapshot(db, settings, started_at=T0, redactor=redactor)

    p = SnapshotPusher(
        db,
        cfg.model_copy(update={"max_attempts": 1}),
        snapshot=snapshot,
        secrets=SecretStore(FakeKeychain(items={SUPABASE_KEY_REF: TOKEN})),
        client_factory=factory,
        sleep=Sleeps(),
    )
    for _ in range(FAIL_AFTER):
        await p.push_once()
    assert len(built) == 1 and built[0].is_closed
    assert (await p.push_once()).ok and len(built) == 2


@pytest.mark.parametrize("code", [401, 403])
async def test_rejected_credentials_fail_at_once_without_retry(
    db: sqlite3.Connection, code: int
) -> None:
    fake = FakeSupabase(script=[code])
    p = make_pusher(db, fake)
    outcome = await p.push_once()
    assert outcome.fatal
    assert len(fake.requests) == 1
    h = health(db)
    assert h["state"] == "failing"
    assert SUPABASE_KEY_REF in h["detail"] and f"HTTP {code}" in h["detail"]


@pytest.mark.parametrize("code", [400, 404, 413, 422])
async def test_refused_snapshot_is_not_retried(db: sqlite3.Connection, code: int) -> None:
    fake = FakeSupabase(script=[code])
    outcome = await make_pusher(db, fake).push_once()
    assert outcome.fatal and len(fake.requests) == 1
    assert health(db)["state"] == "failing"


async def test_wrong_token_is_rejected_by_the_server(db: sqlite3.Connection) -> None:
    fake = FakeSupabase(token_sha256=hashlib.sha256(b"another").hexdigest())
    outcome = await make_pusher(db, fake).push_once()
    assert outcome.fatal and "HTTP 403" in outcome.detail


async def test_missing_secret_sends_nothing(db: sqlite3.Connection) -> None:
    fake = FakeSupabase()
    outcome = await make_pusher(db, fake, keychain=FakeKeychain()).push_once()
    assert outcome.fatal
    assert fake.requests == []
    assert health(db)["state"] == "failing"
    assert f"missing secret {SUPABASE_KEY_REF}" in health(db)["detail"]


async def test_locked_keychain_is_degraded(db: sqlite3.Connection) -> None:
    fake = FakeSupabase()
    outcome = await make_pusher(db, fake, keychain=FakeKeychain(fail_with=36)).push_once()
    assert not outcome.ok and not outcome.fatal
    assert fake.requests == []
    assert health(db)["state"] == "degraded"
    assert "keychain" in health(db)["detail"]


async def test_snapshot_too_large_is_not_sent(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", 100)
    fake = FakeSupabase()
    outcome = await make_pusher(db, fake).push_once()
    assert outcome.fatal and "too large" in outcome.detail
    assert fake.requests == []


async def test_too_large_is_shrunk_and_says_so(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _busy(db, utcnow())
    full = to_remote(
        build_snapshot(db, load_settings(), started_at=T0, now=utcnow(), version="abcdef12")
    ).model_dump(mode="json")
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", data_size(full) - 1)  # just too big
    fake = FakeSupabase()
    outcome = await make_pusher(db, fake).push_once()
    assert outcome.ok, outcome
    data = json.loads(fake.requests[0].content)["p_data"]
    assert data["trimmed"] != "none" and data_size(data) <= pusher_mod.MAX_BYTES
    Snapshot.model_validate(data)
    assert data["pulse"]["open"] == full["pulse"]["open"] == 1  # the counts stay


async def test_a_broken_snapshot_is_a_failed_push(db: sqlite3.Connection) -> None:
    async def broken(_: Redactor) -> Snapshot:
        raise sqlite3.OperationalError(f"disk I/O error {LEAK}")

    fake = FakeSupabase()
    p = make_pusher(db, fake)
    p._snapshot = broken  # pyright: ignore[reportPrivateUsage]
    outcome = await p.push_once()
    assert not outcome.ok and outcome.detail == "snapshot failed (OperationalError)"
    assert fake.requests == []


async def test_since_is_kept_while_the_state_holds(db: sqlite3.Connection) -> None:
    fake = FakeSupabase()
    p = make_pusher(db, fake)
    await p.push_once()
    first = health(db)["since"]
    p._clock = lambda: T0 + timedelta(minutes=5)  # pyright: ignore[reportPrivateUsage]
    await p.push_once()
    assert health(db)["since"] == first


# --- redaction -------------------------------------------------------------------------------


async def test_errors_never_carry_secrets(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    leaky = [
        httpx.ConnectError(f"failed {URL}?token={TOKEN} key={ANON}"),
        httpx.ReadTimeout(f"timeout {TOKEN}"),
        RuntimeError(f"boom {TOKEN}"),  # an unexpected error from the transport
    ]
    fake = FakeSupabase(script=[*leaky, 401])
    p = make_pusher(db, fake, max_attempts=1)
    details: list[str] = []
    for _ in range(4):
        details.append((await p.push_once()).detail)
    assert details[2] == "request failed (RuntimeError)"
    for text in (json.dumps(health(db)), caplog.text):
        assert TOKEN not in text and ANON not in text
    # The project URL isn't secret (httpx's own request log shows it), but the row never needs it.
    assert URL not in json.dumps(health(db))


async def test_redactor_covers_a_detail_that_quotes_the_token(db: sqlite3.Connection) -> None:
    p = make_pusher(db, FakeSupabase(), max_attempts=1)

    async def quoting(body: bytes) -> pusher_mod.PushOutcome:
        return pusher_mod.PushOutcome(False, False, f"echo {TOKEN} and {ANON}")

    p._post = quoting  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    outcome = await p.push_once()
    assert TOKEN not in outcome.detail and ANON not in outcome.detail
    assert "[redacted]" in outcome.detail


# --- the secrets ------------------------------------------------------------------------------


async def test_the_token_is_read_before_the_snapshot_is_built(db: sqlite3.Connection) -> None:
    built: list[Redactor] = []

    async def snapshot(redactor: Redactor) -> Snapshot:
        built.append(redactor)
        return build_snapshot(db, load_settings(), started_at=T0)

    fake = FakeSupabase()
    p = make_pusher(db, fake, keychain=FakeKeychain())
    p._snapshot = snapshot  # pyright: ignore[reportPrivateUsage]
    outcome = await p.push_once()
    assert outcome.fatal and "missing secret" in outcome.detail
    assert built == [] and fake.requests == []  # failed before building anything
    # With the secret, the builder gets a redactor that knows the token and the anon key.
    p = make_pusher(db, fake)
    p._snapshot = snapshot  # pyright: ignore[reportPrivateUsage]
    assert (await p.push_once()).ok
    [redactor] = built
    assert redactor.text(f"a {TOKEN} b {ANON}") == "a [redacted] b [redacted]"


async def test_the_dump_is_scanned_again_for_the_secrets(db: sqlite3.Connection) -> None:
    """What the builder couldn't clean (an identifier, which isn't free text) is replaced
    by the pusher's own pass over every string: the known token, and credential-shaped text."""
    ghp = "ghp_" + "a1b2" * 8  # lowercase and digits only: a valid identifier
    assert redact_secrets(ghp) != ghp

    async def snapshot(redactor: Redactor) -> Snapshot:
        s = build_snapshot(db, load_settings(), started_at=T0, redactor=redactor)
        rows = [s.health[0].model_copy(update={"name": n}) for n in (TOKEN, ghp)]
        return s.model_copy(update={"health": [*rows, *s.health[1:]]})

    fake = FakeSupabase()
    p = make_pusher(db, fake)
    p._snapshot = snapshot  # pyright: ignore[reportPrivateUsage]
    outcome = await p.push_once()
    assert outcome.ok, outcome
    data = json.loads(fake.requests[0].content)["p_data"]
    text = json.dumps(data)
    assert TOKEN not in text and ghp not in text
    assert [h["name"] for h in data["health"][:2]] == ["[redacted]", "[redacted]"]


@pytest.mark.parametrize("family", sorted(SECRET_SAMPLES))
async def test_no_secret_reaches_the_relay(db: sqlite3.Connection, family: str) -> None:
    """A credential of each family, and the relay's own token, in every free-text source of
    the snapshot (run, approval, question, pulse, tool call, health, task, extension, ...):
    none reaches the transport, and what does is clean of credential-shaped text."""
    secret, raw = SECRET_SAMPLES[family]
    seed_secret(db, utcnow(), secret, FAKE_TOKEN)
    fake = FakeSupabase(token_sha256=hashlib.sha256(FAKE_TOKEN.encode()).hexdigest())
    p = make_pusher(db, fake, keychain=FakeKeychain(items={SUPABASE_KEY_REF: FAKE_TOKEN}))
    outcome = await p.push_once()
    assert outcome.ok, outcome
    [request] = fake.requests
    data = json.loads(request.content)["p_data"]
    text = json.dumps(data, ensure_ascii=False)
    assert raw not in text and FAKE_TOKEN not in text and secret not in text
    assert "leak" in text  # the texts went, cleaned
    assert redact_secrets(text) == text
    assert fake.stored == [data]


# --- shrinking ---------------------------------------------------------------------------------


def _stages(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The dump after each step of ``SHRINK_STEPS`` applied in turn, on copies."""
    out = copy.deepcopy(data)
    stages: list[dict[str, Any]] = []
    for name, step in pusher_mod.SHRINK_STEPS:
        out["trimmed"] = name
        step(out)
        stages.append(copy.deepcopy(out))
    return stages


def test_shrink_steps_are_ordered_deterministic_and_valid(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_worst(db, utcnow())
    data = remote_dump(db, utcnow())
    before = copy.deepcopy(data)
    # With a cap nothing fits (0), every step runs, one after the other.
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", 0)
    stages = _stages(data)
    assert [s["trimmed"] for s in stages] == ["history", "panels", "lists", "detail", "items"]
    sizes = [data_size(data), *(data_size(s) for s in stages)]
    assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == 6  # each one cuts more
    for stage in stages:
        Snapshot.model_validate(stage)  # every step's output is a valid snapshot
    history, panels, lists, detail, items = stages
    r = history["runs"]["items"][0]
    assert len(r["history"]) <= 4 and len(r["did"]) <= 3 and len(r["effects"]) <= 3
    assert all(len(t["history"]) <= 3 for t in history["tasks"]["items"])
    assert all(len(a["changes"] or []) <= 3 for a in history["approvals"]["items"])
    assert len(history["runs"]["items"]) == len(data["runs"]["items"])  # lists intact
    held = [p for e in history["extensions"] for p in e["panels"] if p["views"]]
    assert held and not any(p["views"] for e in panels["extensions"] for p in e["panels"])
    dropped = [p for e in panels["extensions"] for p in e["panels"] if p["truncated"]]
    assert dropped and all("views" not in p["outside"] for p in dropped)
    assert len(panels["runs"]["items"]) == len(data["runs"]["items"])  # the lists are untouched
    assert all(
        len(v) == 5
        for v in (lists["runs"]["items"], lists["tasks"]["items"], lists["approvals"]["recent"],
                  lists["pulse"]["items"], lists["questions"]["items"],
                  lists["claude_brain"]["tools"])
    )  # fmt: skip
    assert lists["approvals"]["items"] == history["approvals"]["items"]  # pending stay
    run = detail["runs"]["items"][0]
    assert run["summary"] is None and run["did"] == [] and run["detail"] is False
    assert "summary" not in run["outside"]
    assert all(e["tools"] == [] for e in detail["extensions"])
    assert all(t["error"] is None for t in detail["tasks"]["items"])
    assert all(a["changes"] is None for a in detail["approvals"]["items"])
    for key, sub in (
        ("runs", "items"), ("tasks", "items"), ("approvals", "items"), ("approvals", "recent"),
        ("pulse", "items"), ("questions", "items"),
    ):  # fmt: skip
        assert items[key][sub] == []
    assert items["claude_brain"]["tools"] == [] and items["trust_rules"] == []
    assert items["repos"] == [] and items["extensions"] and items["health"]
    assert items["runs"]["by_state"] == data["runs"]["by_state"]  # the counts stay
    assert items["approvals"]["open"] == data["approvals"]["open"]
    # Deterministic, and the input is untouched.
    assert _stages(data) == stages
    assert data == before


def test_shrink_stops_at_the_first_step_that_fits(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_worst(db, utcnow())
    data = remote_dump(db, utcnow())
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", 0)
    stages = _stages(data)
    for stage in stages:
        # A cap this stage just fits: the stages before it were over it.
        monkeypatch.setattr(pusher_mod, "MAX_BYTES", data_size(stage))
        out = pusher_mod.shrink(data)
        assert out["trimmed"] == stage["trimmed"]
        assert data_size(out) <= pusher_mod.MAX_BYTES
        Snapshot.model_validate(out)
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", data_size(data))
    assert pusher_mod.shrink(data) is data  # it fits: untouched
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", 0)
    assert pusher_mod.shrink(data)["trimmed"] == "items"  # nothing fits: the last step


def test_the_halving_stops_as_soon_as_it_fits(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_worst(db, utcnow())
    data = remote_dump(db, utcnow())
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", 0)
    history, panels, lists = _stages(data)[:3]
    floor = data_size(lists)
    half = copy.deepcopy(panels)
    half["trimmed"] = "lists"
    for parent, key in pusher_mod._lists(half):  # pyright: ignore[reportPrivateUsage]
        parent[key] = parent[key][: len(parent[key]) // 2]
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", data_size(half))
    out = pusher_mod.shrink(data)
    assert out["trimmed"] == "lists" and floor < data_size(out) <= data_size(half)
    assert len(out["runs"]["items"]) == len(history["runs"]["items"]) // 2
    assert out["extensions"] == half["extensions"]  # the panels step already ran


# --- the run loop ----------------------------------------------------------------------------


async def test_run_pushes_every_period_and_survives_a_crash(db: sqlite3.Connection) -> None:
    fake = FakeSupabase()
    ticks = iter([0.0, 3.0, 100.0, 100.5, 200.0, 260.0])
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    p = make_pusher(db, fake, sleep=sleep, every_s=60)
    p._monotonic = lambda: next(ticks)  # pyright: ignore[reportPrivateUsage]
    calls = 0
    real = p.push_once

    async def flaky() -> pusher_mod.PushOutcome:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("unexpected")
        return await real()

    p.push_once = flaky  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await p.run()
    # 60 - 3 elapsed; then 60 - 0.5; a push that overran the period still waits MIN_GAP_S.
    assert sleeps == [57.0, 59.5, 1.0]
    assert calls == 3


async def test_run_owns_and_closes_its_client(db: sqlite3.Connection) -> None:
    settings = load_settings()
    cfg = RelayConfig(enabled=True, supabase_url=URL, anon_key=ANON)

    async def snapshot(_: Redactor) -> Snapshot:
        return build_snapshot(db, settings, started_at=T0)

    async def stop(_: float) -> None:
        raise asyncio.CancelledError

    p = SnapshotPusher(db, cfg, snapshot=snapshot, secrets=SecretStore(FakeKeychain()), sleep=stop)
    assert p.client.follow_redirects is False
    with pytest.raises(asyncio.CancelledError):
        await p.run()  # the secret is missing, so nothing is sent
    assert p.client.is_closed


def test_clear_relay_health(db: sqlite3.Connection) -> None:
    kv_set(db, RELAY_HEALTH_KV, {"state": "failing", "since": None, "detail": "x"})
    clear_relay_health(db)
    assert kv_get(db, RELAY_HEALTH_KV) is None


# --- the health row in the snapshot ----------------------------------------------------------


def test_relay_row_in_the_snapshot(db: sqlite3.Connection, tmp_path: Path) -> None:
    settings = load_settings()
    snap = build_snapshot(db, settings, started_at=T0)
    assert "relay" not in {h.name for h in snap.health}
    since = T0.isoformat()
    kv_set(db, RELAY_HEALTH_KV, {"state": "failing", "since": since, "detail": "HTTP 401: x"})
    snap = build_snapshot(db, settings, started_at=T0)
    [row] = [h for h in snap.health if h.name == "relay"]
    assert row.state == "failing" and row.since == T0 and row.reason == "HTTP 401: x"
    [remote] = [h for h in to_remote(snap).health if h.name == "relay"]
    assert remote.state == "failing" and remote.reason == "HTTP 401: x"  # PER-276: same text
    kv_set(db, RELAY_HEALTH_KV, {"state": "bogus"})
    [row] = [h for h in build_snapshot(db, settings, started_at=T0).health if h.name == "relay"]
    assert row.state == "unknown"


# --- config ----------------------------------------------------------------------------------


def test_relay_config_defaults_off() -> None:
    cfg = load_settings().relay
    assert cfg.enabled is False and cfg.every_s == 60 and cfg.supabase_url is None


@pytest.mark.parametrize(
    "url",
    [
        "http://abc.supabase.co",
        "https://user:pw@abc.supabase.co",
        "https://abc.supabase.co/rest/v1",
        "https://abc.supabase.co?x=1",
        "https://abc.supabase.co#f",
        "https://127.0.0.1",
        "https://localhost",
        "ftp://abc.supabase.co",
        "abc.supabase.co",
    ],
)
def test_relay_url_must_be_bare_https(url: str) -> None:
    with pytest.raises(ValueError, match=r"relay\.supabase_url"):
        RelayConfig(supabase_url=url)


def test_relay_url_trailing_slash_is_dropped() -> None:
    assert RelayConfig(supabase_url=URL + "/").supabase_url == URL


def test_relay_enabled_needs_url_and_key() -> None:
    with pytest.raises(ValueError, match=r"relay\.enabled needs"):
        RelayConfig(enabled=True, supabase_url=URL)
    with pytest.raises(ValueError, match=r"relay\.enabled needs"):
        RelayConfig(enabled=True, anon_key=ANON)
    with pytest.raises(ValueError, match="anon_key"):
        RelayConfig(anon_key="has spaces in it, not a key")
    with pytest.raises(ValueError):
        RelayConfig(every_s=5)


# --- the daemon ------------------------------------------------------------------------------


def _port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_daemon_without_relay_sends_nothing_and_clears_the_row(resonant_home: Path) -> None:
    from resonant.daemon import Daemon
    from resonant.store.db import open_db

    settings = load_settings(api={"port": _port()})
    conn = open_db(settings.db_path)
    kv_set(conn, RELAY_HEALTH_KV, {"state": "failing", "since": None, "detail": "old"})
    conn.close()
    daemon = Daemon(settings, secrets=SecretStore(FakeKeychain()))
    assert daemon.pusher is None
    assert kv_get(daemon.conn, RELAY_HEALTH_KV) is None
    daemon.conn.close()


async def test_daemon_pushes_the_remote_tier(resonant_home: Path) -> None:
    from resonant.api import local_snapshot
    from resonant.daemon import Daemon

    settings = load_settings(
        api={"port": _port()},
        relay={"enabled": True, "supabase_url": URL, "anon_key": ANON, "every_s": 15},
    )
    kc = FakeKeychain(items={SUPABASE_KEY_REF: TOKEN})
    daemon = Daemon(settings, secrets=SecretStore(kc))
    assert daemon.pusher is not None
    fake = FakeSupabase()
    daemon.pusher = SnapshotPusher(
        daemon.conn,
        settings.relay,
        snapshot=lambda redactor: local_snapshot(daemon.state, redactor=redactor),
        secrets=SecretStore(kc),
        client=fake.client(),
    )
    run = asyncio.create_task(daemon.run())
    for _ in range(200):
        if fake.stored:
            break
        await asyncio.sleep(0.02)
    daemon.stop()
    assert await asyncio.wait_for(run, 5) == 0
    [data] = fake.stored
    assert Snapshot.model_validate(data).tier == "remote"
    assert data["dry_run"] is True


def test_dropping_panels_is_enough_when_they_are_what_is_over(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PER-280: ``trimmed`` says ``panels`` when only panels had to go: the biggest first, the
    lists not halved, every dropped panel still a row with its state."""
    from panel_helpers import panel_manifest, raw_panel, store_panel

    from resonant.store import extensions as ext_store

    now = utcnow()
    seed_typical(db, now)
    ids = ("a", "b", "c", "d")
    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name="ledger", source="repo:ledger", commit="a" * 40, tree_digest="d",
            manifest_sha="m", installed_at=now, installed_by="justin",
            manifest=panel_manifest("ledger", ids),
        ),
    )  # fmt: skip
    for k, pid in enumerate(ids):
        store_panel(db, "ledger", pid, raw_panel(rows=50 - 10 * k, items=50, details=100), now)
    data = remote_dump(db, now)
    assert data["trimmed"] == "none"
    size = data_size(data)
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", size - 30_000)  # about one panel too many
    out = pusher_mod.shrink(data)
    assert out["trimmed"] == "panels" and data_size(out) <= pusher_mod.MAX_BYTES
    held = {p["id"]: bool(p["views"]) for e in out["extensions"] for p in e["panels"]}
    assert held["a"] is False and sum(held.values()) >= 1  # the biggest went; not all of them
    dropped = [p for e in out["extensions"] for p in e["panels"] if not p["views"]]
    assert all(p["truncated"] and p["ok"] for p in dropped)
    for section in ("runs", "tasks", "pulse", "questions"):  # the lists are not halved
        assert len(out[section]["items"]) == len(data[section]["items"])
    Snapshot.model_validate(out)


def _no_strip(_out: dict[str, Any]) -> None:
    return None


def test_twenty_extensions_of_maxed_out_panels_still_shrink_to_fit(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PER-280: panel metadata (title, description, schedules, error) is cut too, so the
    worst case (the 20-extension cap, 15 with 8 maxed-out panels, on a typical store) fits."""
    from panel_helpers import panel_manifest, raw_panel, store_panel

    from resonant.store import extensions as ext_store

    now = utcnow()
    seed_typical(db, now)
    ids = tuple(f"p{k}" for k in range(8))
    big = raw_panel(rows=50, items=50, details=100)
    for e in range(20):
        name = f"w{e:02d}".ljust(40, "x")
        manifest = panel_manifest(name, ids)
        for panel in manifest["panels"]:
            panel.update(
                title="T" * 60, description="d" * 160, tz="America/Argentina/ComodRivadavia",
                refresh={"idle": "*/15 * * * *", "active": "*/1 9-16 * * 1-5"},
            )  # fmt: skip
        ext_store.save_extension(
            db,
            ext_store.ExtensionRecord(
                name=name, source="repo:x", commit="a" * 40, tree_digest="d", manifest_sha="m",
                installed_at=now, installed_by="justin", manifest=manifest,
            ),
        )  # fmt: skip
        for pid in ids:
            store_panel(db, name, pid, big, now)
            ext_store.record_panel_failure(db, name, pid, ran_at=now, error="e" * 200)
    data = remote_dump(db, now)
    panels = [p for e in data["extensions"] for p in e["panels"]]
    # 5 typical extensions + 15 of these fill the 20-extension cap
    assert len(panels) == 8 * 15 and data_size(data) > pusher_mod.MAX_BYTES
    # without the metadata step this is over the cap even with every view and list gone
    out = pusher_mod.shrink(data)
    assert data_size(out) <= pusher_mod.MAX_BYTES, data_size(out)
    Snapshot.model_validate(out)
    # Push it to the last step: with every view, list and diagram child gone the panels' own
    # metadata is what is left, and the last step cuts that too.
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", 0)
    with monkeypatch.context() as m:
        m.setattr(pusher_mod, "_strip_panels", _no_strip)
        without = data_size(_stages(data)[-1])
    with_strip = _stages(data)[-1]
    assert data_size(with_strip) < without - 50_000, (data_size(with_strip), without)
    monkeypatch.setattr(pusher_mod, "MAX_BYTES", without - 1)
    out = pusher_mod.shrink(data)
    assert out["trimmed"] == "items" and data_size(out) <= without - 1
    held = [p for e in out["extensions"] for p in e["panels"]]
    assert held and all(
        not p["views"] and p["description"] is None and p["error"] is None and p["outside"] == []
        for p in held
    )
    Snapshot.model_validate(out)


# --- panel days (PER-286) --------------------------------------------------------------------


@dataclass
class DaysFake:
    """The snapshot RPC accepts everything; ``relay_panel_day`` answers from ``script``
    (default 204) and records each body."""

    script: list[int] = field(default_factory=list[int])
    days: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == INGEST_PATH:
            return httpx.Response(204)
        assert request.url.path == pusher_mod.DAY_PATH
        assert request.headers.get("apikey") == ANON
        self.days.append(json.loads(request.content))
        return httpx.Response(self.script.pop(0) if self.script else 204)


def _days_pusher(db: sqlite3.Connection, fake: DaysFake, clock: list[datetime]) -> SnapshotPusher:
    settings = load_settings()

    async def snapshot(redactor: Redactor) -> Snapshot:
        return build_snapshot(db, settings, started_at=T0, now=T0, redactor=redactor)

    return SnapshotPusher(
        db,
        RelayConfig(enabled=True, supabase_url=URL, anon_key=ANON, max_attempts=1),
        snapshot=snapshot,
        secrets=SecretStore(FakeKeychain(items={SUPABASE_KEY_REF: TOKEN})),
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)),
        sleep=Sleeps(),
        clock=lambda: clock[0],
        jitter=lambda: 0.0,
    )


def _history_ext(db: sqlite3.Connection) -> None:
    from panel_helpers import normalized, panel_manifest, raw_panel

    from resonant.store import extensions as ext_store

    manifest = panel_manifest("ledger", ("paper",))
    manifest["panels"][0]["history"] = {"every": "daily", "keep": "90d"}
    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name="ledger", source="repo:ledger", commit="a" * 40, tree_digest="d",
            manifest_sha="m", manifest=manifest, installed_at=T0, installed_by="justin",
        ),
    )  # fmt: skip
    data = json.dumps(normalized(raw_panel(points=20, rows=3, items=3, details=3)).data)
    for day in ("2026-10-13", "2026-10-14", "2026-10-15", "2026-10-12"):
        ext_store.save_day_ok(db, "ledger", "paper", day, data=data, bytes=9, at=T0)
    db.commit()


async def test_archived_days_upload_once_after_a_push_oldest_first(db: sqlite3.Connection) -> None:
    from resonant.store import extensions as ext_store

    _history_ext(db)
    fake, clock = DaysFake(), [T0]
    pusher = _days_pusher(db, fake, clock)
    assert (await pusher.push_once()).ok
    assert [d["p_day"] for d in fake.days] == ["2026-10-12", "2026-10-13", "2026-10-14"]
    body = fake.days[0]
    assert body["p_ext"] == "ledger" and body["p_panel"] == "paper" and body["p_keep_days"] == 90
    assert body["p_token"] == TOKEN
    assert set(body["p_data"]) == {"views", "health", "truncated"}
    assert "local_url" not in json.dumps(body["p_data"])  # never leaves the Mac
    await pusher.push_once()
    assert [d["p_day"] for d in fake.days][3:] == ["2026-10-15"]
    await pusher.push_once()
    assert len(fake.days) == 4  # each once
    assert not any(d.upload_due for d in ext_store.list_panel_days(db, "ledger", "paper"))


async def test_a_failed_day_upload_backs_off_and_retries(db: sqlite3.Connection) -> None:
    from resonant.store import extensions as ext_store

    _history_ext(db)
    fake, clock = DaysFake(script=[503]), [T0]
    pusher = _days_pusher(db, fake, clock)
    assert (await pusher.push_once()).ok  # a day failing never fails the snapshot push
    assert [d["p_day"] for d in fake.days] == ["2026-10-12"]  # stops at the first failure
    row = ext_store.get_panel_day(db, "ledger", "paper", "2026-10-12")
    assert row is not None and row.upload_due and row.upload_tries == 1
    assert row.upload_next_at == T0 + timedelta(seconds=60)
    assert kv_get(db, RELAY_HEALTH_KV)["state"] == "ok"  # type: ignore[index]
    await pusher.push_once()
    assert [d["p_day"] for d in fake.days][1:] == ["2026-10-13", "2026-10-14", "2026-10-15"]
    clock[0] = T0 + timedelta(seconds=61)
    await pusher.push_once()
    assert fake.days[-1]["p_day"] == "2026-10-12"
    row = ext_store.get_panel_day(db, "ledger", "paper", "2026-10-12")
    assert row is not None and not row.upload_due and row.upload_tries == 0


async def test_a_day_rebuilt_to_none_is_deleted_remotely(db: sqlite3.Connection) -> None:
    from resonant.store import extensions as ext_store

    _history_ext(db)
    fake, clock = DaysFake(), [T0]
    pusher = _days_pusher(db, fake, clock)
    for _ in range(2):
        await pusher.push_once()
    ext_store.save_day_none(db, "ledger", "paper", "2026-10-13", at=T0)
    db.commit()
    await pusher.push_once()
    assert fake.days[-1]["p_day"] == "2026-10-13" and fake.days[-1]["p_data"] is None


async def test_days_of_a_removed_panel_are_dropped_not_sent(db: sqlite3.Connection) -> None:
    from resonant.store import extensions as ext_store

    _history_ext(db)
    record = ext_store.get_extension(db, "ledger")
    assert record is not None
    manifest = dict(record.manifest)
    manifest["panels"] = [{**manifest["panels"][0], "history": None}]
    ext_store.save_extension(db, replace_manifest(record, manifest))
    db.commit()
    fake, clock = DaysFake(), [T0]
    await _days_pusher(db, fake, clock).push_once()
    assert fake.days == []
    assert [d.day for d in ext_store.list_panel_days(db, "ledger", "paper") if d.upload_due] == [
        "2026-10-15"
    ]


def replace_manifest(record: Any, manifest: dict[str, Any]) -> Any:
    from dataclasses import replace

    return replace(record, manifest=manifest)

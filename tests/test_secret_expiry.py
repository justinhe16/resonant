"""PER-313: secret expiry metadata and the daily ``core/secrets`` watch.

Every ``security`` call goes to ``FakeKeychain`` under a real ``SecretStore``: nothing here
runs ``/usr/bin/security``, reads a real Keychain or reads a value (no ``-w``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from resonant import secret_expiry as sx
from resonant.config import NotifyOwnerConfig, Settings, load_settings
from resonant.executor import CallContext
from resonant.extensions.scheduler import CORE_JOBS, CoreDeps, secrets_job
from resonant.gateway.channel import ChannelSendError, MessageRef
from resonant.gateway.hub import RoomPosting
from resonant.notify_owner import OwnerBridge
from resonant.secrets import CLAUDE_TOKEN_REF, FakeKeychain, SecretStore
from resonant.store import secret_expiry as rows
from resonant.store.db import kv_get

T0 = datetime(2026, 11, 23, 18, 0, tzinfo=UTC)  # 10:00 in Los Angeles
OWNER = "alice"
CTX = CallContext(secrets={}, idempotency_key="t", task_id="t")


@dataclass
class Hub:
    """The hub as the job sees it: owner sends and room posts, deduped by key."""

    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    posts: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    keys: set[str] = field(default_factory=set[str])
    down: bool = False

    async def send(
        self, principal: str, text: str, *, thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:  # fmt: skip
        if self.down:
            raise ChannelSendError("down")
        if dedupe_key is None or dedupe_key not in self.keys:
            self.sent.append((principal, text, dedupe_key))
            if dedupe_key is not None:
                self.keys.add(dedupe_key)
        return MessageRef("imessage", f"out:{len(self.sent)}")

    async def post_room(self, key: str, text: str, *, dedupe_key: str | None = None) -> MessageRef:
        self.posts.append((key, text, dedupe_key))
        return MessageRef("slack", f"C0:{len(self.posts)}")


def personal(home: Path) -> Settings:
    return load_settings(home=home)


def cori(tmp_path: Path, *, room: str | None = "pulse") -> Settings:
    return load_settings(home=tmp_path / "cori-home", instance={"name": "cori"}, notify_owner={},
                         pulse={"heartbeat": {"room": room}})  # fmt: skip


def keychain(settings: Settings, *refs: str) -> tuple[SecretStore, FakeKeychain]:
    fake = FakeKeychain(service=settings.keychain_service)
    for ref in refs:
        fake.items[ref] = "not-a-real-value"
    return SecretStore(fake, service=settings.keychain_service), fake


def expire(db: sqlite3.Connection, ref: str, at: datetime, *, set_at: datetime = T0) -> None:
    rows.upsert(db, ref, expires_at=at, source="owner", set_at=set_at)


async def watch(db: sqlite3.Connection, store: SecretStore, s: Settings, d: sx.Delivery,
                now: datetime) -> dict[str, Any]:  # fmt: skip
    return await sx.watch(db, store, s, d, now)


def no_value_reads(fake: FakeKeychain) -> bool:
    return all("-w" not in c.argv and "-g" not in c.argv for c in fake.calls)


# --- parsing --------------------------------------------------------------------------------


def test_expiry_flags_parse_and_refuse() -> None:
    assert sx.parse_expires_in("365d", now=T0) == T0 + timedelta(days=365)
    for bad in ("0d", "1.5d", "d", "365", "-3d", "99999d"):
        with pytest.raises(ValueError, match="--expires-in"):
            sx.parse_expires_in(bad, now=T0)
    at = sx.parse_expires("2027-11-23", tz="America/Los_Angeles", now=T0)
    assert at == datetime(2027, 11, 23, 8, 0, tzinfo=UTC)  # midnight LA, stored UTC
    assert sx.parse_expires("2026-11-23", tz="America/Los_Angeles", now=T0)  # today is fine
    with pytest.raises(ValueError, match="in the past"):
        sx.parse_expires("2026-11-22", tz="America/Los_Angeles", now=T0)
    with pytest.raises(ValueError, match="want a date"):
        sx.parse_expires("next week", tz="America/Los_Angeles", now=T0)


def test_thresholds_pick_the_tightest() -> None:
    def t(days: int) -> int | None:
        at = T0 + timedelta(days=days, hours=1)
        return sx.threshold(sx.days_left(at, T0, "UTC"), at, T0)

    assert [t(d) for d in (45, 31, 30, 8, 7, 2, 1)] == [None, None, 30, 30, 7, 7, 1]
    assert sx.threshold(0, T0 - timedelta(seconds=1), T0) == 0
    assert sx.threshold(-3, T0 - timedelta(days=3), T0) == 0


def test_store_rows_round_trip_and_check(db: sqlite3.Connection) -> None:
    expire(db, "github/token", T0 + timedelta(days=90))
    row = rows.get(db, "github/token")
    assert row is not None and row.source == "owner" and row.expires_at == T0 + timedelta(days=90)
    rows.upsert(db, "github/token", expires_at=T0, source="assumed", set_at=T0, note="n")
    assert [(r.ref, r.source, r.note) for r in rows.all(db)] == [("github/token", "assumed", "n")]
    assert rows.clear(db, "github/token") and not rows.clear(db, "github/token")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO secret_expiry VALUES ('a/b', 'x', 'guess', 'x', NULL)")
    with pytest.raises(ValueError, match="bad secret ref"):
        rows.upsert(db, "-w", expires_at=T0, source="owner", set_at=T0)


# --- the daily job --------------------------------------------------------------------------


async def test_alerts_at_30_7_1_and_0_days_once_each(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    s = personal(resonant_home)
    store, fake = keychain(s, "github/token")
    expire(db, "github/token", T0 + timedelta(days=40), set_at=T0 - timedelta(days=50))
    hub = Hub()
    d = sx.Delivery(hub, OWNER)  # type: ignore[arg-type]
    for day in range(45):
        now = T0 + timedelta(days=day)
        await watch(db, store, s, d, now)
        await watch(db, store, s, d, now + timedelta(hours=2))  # a restart the same day
    assert [k for _, _, k in hub.sent] == [
        "expiry:github/token:2027-01-02:30",
        "expiry:github/token:2027-01-02:7",
        "expiry:github/token:2027-01-02:1",
        "expiry:github/token:2027-01-02:0",
    ]
    texts = [t for _, t, _ in hub.sent]
    assert texts[1] == ("🔑 github/token expires in 7 days (2027-01-02, owner). Renew: resonant "
                        "secret set github/token --expires-in 90d")  # fmt: skip
    assert texts[2].startswith("🔑 github/token expires in 1 day (")
    assert texts[3].startswith("🔑 github/token expired today (")
    assert all(len(t) <= sx.TEXT_MAX for t in texts)
    assert kv_get(db, "secrets.alerted:github/token")["sent"] == [0, 1, 7, 30]
    audits = db.execute(
        "SELECT detail FROM audit_log WHERE action = 'secrets.expiry_alert'"
    ).fetchall()
    assert len(audits) == 4 and all("Renew" not in r["detail"] for r in audits)
    assert no_value_reads(fake)

    # renewed: a new expires_at re-arms every threshold
    later = T0 + timedelta(days=45)
    expire(db, "github/token", later + timedelta(days=20), set_at=later)
    out = await watch(db, store, s, d, later)
    assert out == {"alerts": [{"ref": "github/token", "threshold": 30, "decision": "sent"}]}
    assert len(hub.sent) == 5


async def test_a_missed_30_day_run_still_sends_only_the_7_day_one(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    s = personal(resonant_home)
    store, _ = keychain(s, "github/token")
    expire(db, "github/token", T0 + timedelta(days=6))
    hub = Hub()
    await watch(db, store, s, sx.Delivery(hub, OWNER), T0)  # type: ignore[arg-type]
    assert [k.rsplit(":", 1)[1] for _, _, k in hub.sent if k] == ["7"]


async def test_nothing_known_is_quiet(db: sqlite3.Connection, resonant_home: Path) -> None:
    s = personal(resonant_home)
    store, fake = keychain(s)
    hub = Hub()
    assert await watch(db, store, s, sx.Delivery(hub, OWNER), T0) == {"quiet": True}  # type: ignore[arg-type]
    assert hub.sent == [] and fake.items == {}
    health = sx.SecretsHealth(db, store, s)
    r = await health(T0)
    assert (r.ok, r.detail, r.failing) == (True, "no expiries known", False)
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


async def test_a_row_for_a_missing_item_never_alerts(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    s = personal(resonant_home)
    store, _ = keychain(s)  # the item is gone
    expire(db, "github/token", T0 + timedelta(days=2))
    hub = Hub()
    assert await watch(db, store, s, sx.Delivery(hub, OWNER), T0) == {"quiet": True}  # type: ignore[arg-type]
    (e,) = [e for e in (await sx.survey(db, store, []))[0] if e.ref == "github/token"]
    assert sx.describe(e, T0, s.timezone) == "(expiry kept)"


async def test_a_locked_keychain_alerts_on_rows_only(
    db: sqlite3.Connection, resonant_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    s = personal(resonant_home)
    store, fake = keychain(s, CLAUDE_TOKEN_REF, "github/token")
    fake.modified[CLAUDE_TOKEN_REF] = T0 - timedelta(days=360)  # assumed: needs mdat
    expire(db, "github/token", T0 + timedelta(days=3))
    fake.fail_with = 36
    hub = Hub()
    out = await watch(db, store, s, sx.Delivery(hub, OWNER), T0)  # type: ignore[arg-type]
    assert out == {"alerts": [{"ref": "github/token", "threshold": 7, "decision": "sent"}]}
    assert len(fake.calls) == 1  # asked once, then not again
    assert sum("keychain unavailable" in r.message for r in caplog.records) == 1


async def test_claude_token_is_assumed_from_mdat(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    s = personal(resonant_home)
    store, fake = keychain(s, CLAUDE_TOKEN_REF)
    fake.modified[CLAUDE_TOKEN_REF] = T0 - timedelta(days=360)
    hub = Hub()
    await watch(db, store, s, sx.Delivery(hub, OWNER), T0)  # type: ignore[arg-type]
    ((_, text, key),) = hub.sent
    assert key == "expiry:runner/claude_token:2026-11-28:7"
    assert text == (
        "🔑 runner/claude_token expires in 5 days (2026-11-28, assumed). Renew: claude "
        "setup-token, then resonant secret set runner/claude_token --expires-in 365d"
    )
    assert no_value_reads(fake)
    # no parseable mdat: unknown, never guessed
    fake.modified.clear()
    found, _ = await sx.survey(db, store, sx.known_refs(s))
    (claude,) = [e for e in found if e.ref == CLAUDE_TOKEN_REF]
    assert claude.expires_at is None and sx.describe(claude, T0, s.timezone) == "unknown"


async def test_personal_texts_the_owner_and_a_failed_send_retries(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    s = personal(resonant_home)
    store, _ = keychain(s, "github/token")
    expire(db, "github/token", T0 + timedelta(days=20))
    hub = Hub(down=True)
    d = sx.Delivery(hub, OWNER)  # type: ignore[arg-type]
    out = await watch(db, store, s, d, T0)
    assert out["alerts"][0]["decision"] == "failed"
    hub.down = False
    await watch(db, store, s, d, T0 + timedelta(hours=1))
    assert [(p, k) for p, _, k in hub.sent] == [(OWNER, "expiry:github/token:2026-12-13:30")]
    assert hub.posts == []


async def test_cori_forwards_posts_to_the_room_and_dms(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    s = cori(tmp_path)
    store, fake = keychain(s, CLAUDE_TOKEN_REF, "dori/fly_token")
    fake.modified[CLAUDE_TOKEN_REF] = T0 - timedelta(days=364)
    expire(db, "dori/fly_token", T0 + timedelta(days=25), set_at=T0 - timedelta(days=5))
    hub = Hub()
    bridge = OwnerBridge(db, NotifyOwnerConfig(), instance="cori", secrets=store)
    job = sx.secrets_job(CoreDeps(s, db, None, hub, OWNER, bridge), clock=lambda: T0, store=store)  # type: ignore[arg-type]
    assert job is not None and job.schedule == "0 10 * * *" and job.quiet_idle
    assert job.runs_while_paused is False and job.catchup == timedelta(hours=20)
    out = await job.run({}, CTX)
    assert [a["ref"] for a in out["alerts"]] == ["dori/fly_token", CLAUDE_TOKEN_REF]
    box = bridge.pending()
    assert sorted(box) == ["secrets/dori/fly_token", "secrets/runner/claude_token"]
    titles = [str(v["title"]) for v in box.values()]
    assert all(len(t) <= 200 and "--instance cori" in t for t in titles)
    assert [k for k, _, _ in hub.posts] == ["pulse", "pulse"]
    assert len(hub.sent) == 2
    every = titles + [t for _, t, _ in hub.posts] + [t for _, t, _ in hub.sent]
    assert all("resonant --instance cori secret set" in t for t in every)
    assert "claude setup-token, then resonant --instance cori secret set runner/claude_token " \
        "--expires-in 365d" in titles[1]  # fmt: skip
    # a rerun the same day forwards nothing new
    assert await job.run({}, CTX) == {"quiet": True}
    assert len(hub.posts) == 2

    # no room configured: forward and DM only
    s2 = cori(tmp_path, room=None)
    d = sx.Delivery(hub, OWNER, bridge, hub, s2.pulse.heartbeat.room)  # type: ignore[arg-type]
    rows.clear(db, "dori/fly_token")
    expire(db, "dori/fly_token", T0 + timedelta(days=2))
    await watch(db, store, s2, d, T0)
    assert len(hub.posts) == 2 and len(hub.sent) == 3


def test_the_job_is_registered_and_needs_somewhere_to_send(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    assert secrets_job in CORE_JOBS
    s = personal(resonant_home)
    store, _ = keychain(s)
    assert sx.secrets_job(CoreDeps(s, db, None, None, None), store=store) is None  # type: ignore[arg-type]
    assert sx.secrets_job(CoreDeps(s, db, None, Hub(), None), store=store) is None  # type: ignore[arg-type]
    assert sx.secrets_job(CoreDeps(s, db, None, Hub(), OWNER), store=store) is not None  # type: ignore[arg-type]
    assert isinstance(Hub(), RoomPosting)


def test_known_refs_follow_the_config(resonant_home: Path, tmp_path: Path) -> None:
    base = sx.known_refs(personal(resonant_home))
    assert base == sorted(sx.CORE_REFS)
    from resonant.dashboard.pusher import SUPABASE_KEY_REF

    assert SUPABASE_KEY_REF in base
    s = load_settings(home=tmp_path / "cori-home", instance={"name": "cori"}, notify_owner={},
                      channels={"slack": {"enabled": True, "team_id": "T0TEAM",
                                          "bot_user_id": "U0BOT"}},
                      runner={"session_env": {"FLY_API_TOKEN": "dori/fly_token"}})  # fmt: skip
    extra = set(sx.known_refs(s)) - set(base)
    assert extra == {"core/notify_token", "slack/bot_token", "slack/app_token", "dori/fly_token"}
    p = load_settings(
        home=resonant_home, instances=[{"name": "cori", "url": "http://127.0.0.1:7778"}]
    )
    assert set(sx.known_refs(p)) - set(base) == {"core/notify_token_cori"}


# --- the health row -------------------------------------------------------------------------


async def test_health_ok_degraded_failing(db: sqlite3.Connection, resonant_home: Path) -> None:
    s = personal(resonant_home)
    store, fake = keychain(s, "github/token", "linear/token")
    health = sx.SecretsHealth(db, store, s)
    expire(db, "github/token", T0 + timedelta(days=300))
    r = await health(T0)
    assert (r.ok, r.detail) == (True, "soonest github/token in 300d")
    calls = len(fake.calls)
    expire(db, "github/token", T0 + timedelta(days=5))
    r = await health(T0 + timedelta(minutes=1))
    assert (r.ok, r.failing, r.detail) == (False, False, "github/token expires in 5d")
    assert len(fake.calls) == calls  # rows every time, the Keychain at most hourly
    expire(db, "linear/token", T0 - timedelta(days=1))
    r = await health(T0 + timedelta(minutes=2))
    assert (r.ok, r.failing, r.detail) == (False, True, "expired: linear/token")
    del fake.items["linear/token"]
    r = await health(T0 + timedelta(hours=2))  # re-probed: the expired one is gone
    assert (r.ok, r.failing) == (False, False)
    assert no_value_reads(fake)


# --- probes (PER-314) -------------------------------------------------------------------------


async def test_a_probe_row_and_a_rejected_probe_in_the_health_row(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    from resonant.store.db import kv_set

    s = personal(resonant_home)
    store, _ = keychain(s, "github/token", "linear/token")
    rows.upsert(db, "github/token", expires_at=T0 + timedelta(days=40), source="probe", set_at=T0)
    found, _ = await sx.survey(db, store, sx.known_refs(s))
    (gh,) = [e for e in found if e.ref == "github/token"]
    assert sx.describe(gh, T0, s.timezone) == "2027-01-02 (40d, probe)"
    health = sx.SecretsHealth(db, store, s)
    assert (await health(T0)).detail == "soonest github/token in 40d"
    kv_set(db, "secrets.probe:linear/token", {"state": "rejected", "status": 401, "at": "x"})
    r = await health(T0 + timedelta(minutes=1))
    assert (r.ok, r.failing, r.detail) == (False, True, "linear/token rejected")
    kv_set(db, "secrets.probe:linear/token", {"state": "ok", "status": 200, "at": "x"})
    assert (await health(T0 + timedelta(minutes=2))).ok


async def test_health_facts_are_shared_with_the_dashboard(
    db: sqlite3.Connection, resonant_home: Path
) -> None:
    """PER-315: the snapshot reads the same cached Keychain facts as the health row, so a
    dashboard poll never adds a Keychain call within the hour."""
    s = personal(resonant_home)
    store, fake = keychain(s, "github/token")
    health = sx.SecretsHealth(db, store, s)
    expire(db, "github/token", T0 + timedelta(days=30))
    await health(T0)
    calls = len(fake.calls)
    facts = await health.facts(T0 + timedelta(minutes=5))
    assert facts.present["github/token"] is True
    assert len(fake.calls) == calls
    later = await health.facts(T0 + timedelta(hours=1))  # stale: probed again
    assert later.present["github/token"] is True and len(fake.calls) > calls
    assert no_value_reads(fake)

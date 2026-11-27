"""PER-301: the notify-owner bridge. Cori's urgent alerts reach the owner's iMessage through
personal Resonant's ``POST /api/notify-owner``.

Personal side: the endpoint's contract (401/404/422, nothing stored on a refusal), the text
(``[Cori] 🔴 …`` once, ``✓`` on clear), pause and ``/ext mute cori``, and the text in the
brain's transcript. Instance side: ``OwnerBridge`` (outbox, retries with backoff, one DM per
problem, ``notify token mismatch``), the monitor and pulse hooks, config and the CLI. And two
real daemons in one process, with fake channels (personal: a fake iMessage adapter; Cori: a
fake Slack one), Cori's bridge talking to personal's real app over ASGI.

No real Keychain (``FakeKeychain``), no live daemon or port (ASGI transports and free ports
only), no real Messages or Slack.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from resonant import cli
from resonant.api import DaemonState, create_app
from resonant.brain.history import since_last
from resonant.config import NotifyConfig, NotifyOwnerConfig, Settings, load_settings
from resonant.daemon import Daemon
from resonant.e2e import FakeChannel, ScriptedModel
from resonant.e2e.extensions import install_fixture
from resonant.extensions.notify import mute
from resonant.extensions.pulse import Pulse, render_item
from resonant.gateway.channel import ChannelRefused, ChannelSendError, MessageRef
from resonant.loop import Loop, SlotPool
from resonant.monitor import BRIDGED_KV, CheckState, HealthMonitor
from resonant.notify_owner import (
    DOWN_KV,
    MAX_BODY,
    PATH,
    TEST_KEY,
    TEST_TITLE,
    OwnerBridge,
    owner_key,
    parse_body,
    token_ref_for,
)
from resonant.ops.fakes import FakeOpsRunner
from resonant.principals import load_principals
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store import pulse as items
from resonant.store import system_texts
from resonant.store.db import kv_get, kv_set, open_db, utcnow
from resonant.store.tasks import insert_event
from resonant_sdk import Event

OWNER = "alice"
TOKEN = "cori-notify-token-0123456789"
T0 = datetime(2031, 6, 10, 16, 0, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Channel:
    """A channel that records sends and honours dedupe keys."""

    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    keys: set[str] = field(default_factory=set[str])
    refuse: bool = False
    fail: int = 0

    async def send(self, principal: str, text: str, *, dedupe_key: str | None = None) -> MessageRef:
        if self.refuse:
            raise ChannelRefused("no identity")
        if self.fail:
            self.fail -= 1
            raise ChannelSendError("down")
        if dedupe_key is not None and dedupe_key in self.keys:
            return MessageRef("imessage", "dup")
        self.sent.append((principal, text, dedupe_key))
        if dedupe_key is not None:
            self.keys.add(dedupe_key)
        return MessageRef("imessage", str(len(self.sent)))

    @property
    def texts(self) -> list[str]:
        return [t for _, t, _ in self.sent]


# --- personal side: the endpoint ---------------------------------------------------------


@dataclass
class Personal:
    db: sqlite3.Connection
    channel: Channel
    keychain: FakeKeychain
    pulse: Pulse
    app: Any

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)
        return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1")


def body(**over: Any) -> dict[str, Any]:
    return {"instance": "cori", "key": "ext/dori/inc-1", "title": "Dori failing",
            "urgency": "urgent", "clear": False, **over}  # fmt: skip


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def personal(db: sqlite3.Connection) -> Personal:
    settings = load_settings()
    channel = Channel()
    keychain = FakeKeychain()
    keychain.items["core/notify_token_cori"] = TOKEN
    pulse = Pulse(
        db,
        channel=channel,
        recipients=lambda _s: (OWNER,),
        manifest_of=lambda _e: None,
        config=NotifyConfig(),
    )
    state = DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)),
                        started_at=utcnow())  # fmt: skip
    state.pulse = pulse
    state.components = _components_with(SecretStore(keychain))
    kv_set(db, "paused", False)
    return Personal(db, channel, keychain, pulse, create_app(state))


def _components_with(secrets: SecretStore) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(executor=SimpleNamespace(secrets=secrets), runners={})


def stored(db: sqlite3.Connection) -> int:
    return int(db.execute("SELECT COUNT(*) FROM pulse_items").fetchone()[0])


async def test_a_valid_call_texts_once_and_clear_says_check(personal: Personal) -> None:
    async with personal.client() as c:
        r1 = await c.post(PATH, json=body(), headers=auth())
        r2 = await c.post(PATH, json=body(), headers=auth())
        r3 = await c.post(PATH, json=body(clear=True), headers=auth())
    assert r1.status_code == 200 and r1.json() == {"outcome": "sent", "reason": None}
    assert r2.json() == {"outcome": "told", "reason": None}  # the repeat sends nothing
    assert r3.json()["outcome"] == "sent"
    assert personal.channel.texts == ["[Cori] \U0001f534 Dori failing", "[Cori] ✓ Dori failing"]
    item = items.get(personal.db, "instance:cori", "ext/dori/inc-1")
    assert item is not None and item.cleared and not item.changed


async def test_a_new_title_for_the_same_key_texts_again(personal: Personal) -> None:
    async with personal.client() as c:
        await c.post(PATH, json=body(), headers=auth())
        await c.post(PATH, json=body(title="Dori still failing: 3 errors"), headers=auth())
    assert personal.channel.texts[1] == "[Cori] \U0001f534 Dori still failing: 3 errors"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        auth("wrong-token-0000000000"),
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer "},
    ],
)
async def test_a_wrong_or_missing_token_is_401_and_stores_nothing(
    personal: Personal, headers: dict[str, str]
) -> None:
    async with personal.client() as c:
        r = await c.post(PATH, json=body(), headers=headers)
    assert r.status_code == 401
    assert stored(personal.db) == 0 and personal.channel.texts == []


async def test_a_mismatch_says_so(personal: Personal) -> None:
    async with personal.client() as c:
        r = await c.post(PATH, json=body(), headers=auth("rotated-on-one-side-only"))
    assert r.json() == {"detail": "notify token mismatch"}


@pytest.mark.parametrize(
    "payload",
    [
        body(extra="field"),
        body(urgency="normal"),
        body(clear="yes"),
        body(title=""),
        body(title="   "),
        body(title="\u200b\u202e\x07"),  # cleans to nothing
        body(title="x" * 201),
        body(key="k" * 121),
        body(key="Bad Key"),
        body(instance="Cori!"),
        {k: v for k, v in body().items() if k != "title"},
        [body()],
    ],
)
async def test_bad_bodies_are_422_and_store_nothing(personal: Personal, payload: Any) -> None:
    async with personal.client() as c:
        r = await c.post(PATH, json=payload, headers=auth())
    assert r.status_code == 422
    assert stored(personal.db) == 0 and personal.channel.texts == []


async def test_an_oversize_or_non_json_body_is_422(personal: Personal) -> None:
    big = json.dumps(body(title="x" * 150, pad=" " * MAX_BODY)).encode()
    async with personal.client() as c:
        r1 = await c.post(PATH, content=big, headers=auth())
        r2 = await c.post(PATH, content=b"not json", headers=auth())
    assert (r1.status_code, r2.status_code) == (422, 422)
    assert "over" in r1.json()["detail"]
    assert stored(personal.db) == 0


async def test_404_without_a_token_for_the_instance(personal: Personal) -> None:
    personal.keychain.items.clear()
    async with personal.client() as c:
        r1 = await c.post(PATH, json=body(), headers=auth())
        r2 = await c.post(PATH, json=body(instance="resonant"), headers=auth())  # itself
    assert (r1.status_code, r2.status_code) == (404, 404)
    assert stored(personal.db) == 0


async def test_404_without_a_channel_stack(db: sqlite3.Connection) -> None:
    settings = load_settings()
    state = DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)),
                        started_at=utcnow())  # fmt: skip
    keychain = FakeKeychain(items={"core/notify_token_cori": TOKEN})
    state.components = _components_with(SecretStore(keychain))
    transport = httpx.ASGITransport(app=create_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        r = await c.post(PATH, json=body(), headers=auth())
    assert r.status_code == 404
    assert keychain.calls == []  # nothing asked of the Keychain


async def test_a_channel_failure_is_503_and_the_retry_sends(personal: Personal) -> None:
    personal.channel.fail = 1
    async with personal.client() as c:
        r1 = await c.post(PATH, json=body(), headers=auth())
        r2 = await c.post(PATH, json=body(), headers=auth())
    assert (r1.status_code, r2.json()["outcome"]) == (503, "sent")
    assert personal.channel.texts == ["[Cori] \U0001f534 Dori failing"]


async def test_pause_holds_the_item_for_the_pulse(personal: Personal) -> None:
    kv_set(personal.db, "paused", True)
    async with personal.client() as c:
        r = await c.post(PATH, json=body(), headers=auth())
    assert r.json() == {"outcome": "held", "reason": "paused"}
    assert personal.channel.texts == []
    item = items.get(personal.db, "instance:cori", "ext/dori/inc-1")
    assert item is not None and item.changed  # pending: the hourly pulse carries it
    assert render_item(item) == "[Cori] Dori failing"


async def test_mute_holds_the_item(personal: Personal) -> None:
    mute(personal.db, "instance:cori", T0 + timedelta(days=3650), by=OWNER)
    async with personal.client() as c:
        r = await c.post(PATH, json=body(), headers=auth())
    assert r.json() == {"outcome": "held", "reason": "muted"}
    assert personal.channel.texts == []


async def test_an_alert_storm_is_capped(personal: Personal) -> None:
    personal.pulse.config = NotifyConfig(max_per_hour=2)
    async with personal.client() as c:
        outs = [(await c.post(PATH, json=body(key=f"k{i}"), headers=auth())).json()["outcome"]
                for i in range(4)]  # fmt: skip
    assert outs == ["sent", "sent", "held", "held"]
    assert len(personal.channel.texts) == 2


async def test_a_clear_for_something_never_said_is_silent(personal: Personal) -> None:
    async with personal.client() as c:
        r = await c.post(PATH, json=body(clear=True), headers=auth())
    assert r.json()["outcome"] == "told"
    assert personal.channel.texts == [] and stored(personal.db) == 0


async def test_a_clear_of_a_held_alert_closes_it_quietly(personal: Personal) -> None:
    kv_set(personal.db, "paused", True)
    async with personal.client() as c:
        await c.post(PATH, json=body(), headers=auth())
        kv_set(personal.db, "paused", False)
        r = await c.post(PATH, json=body(clear=True), headers=auth())
    assert r.json()["outcome"] == "told"
    assert personal.channel.texts == []  # no ✓ for something never texted
    item = items.get(personal.db, "instance:cori", "ext/dori/inc-1")
    assert item is not None and item.cleared and not item.changed


async def test_the_brain_sees_the_text_in_its_transcript(personal: Personal) -> None:
    async with personal.client() as c:
        await c.post(PATH, json=body(), headers=auth())
    [text] = system_texts.between(personal.db, OWNER)
    assert (text.kind, text.text, text.outside) == ("pulse", "(Cori) \U0001f534 Dori failing", True)
    since = since_last(personal.db, OWNER, after_event_id=None, before_event_id=None,
                       since=utcnow() - timedelta(hours=1), tz=UTC)  # fmt: skip
    assert since is not None and since.outside
    assert "Resonant texted: (Cori) \U0001f534 Dori failing" in since.text


async def test_extension_urgent_texts_are_not_recorded(db: sqlite3.Connection) -> None:
    """Personal Resonant without Cori is unchanged: an extension's urgent send is not put in
    ``system_texts`` (only ``instance:`` alerts are)."""
    from resonant_sdk import Manifest

    channel = Channel()
    m = Manifest.model_validate({"name": "dori", "version": 1, "report": {"max_urgency": "urgent"}})
    pulse = Pulse(db, channel=channel, recipients=lambda _e: (OWNER,),
                  manifest_of=lambda e: m if e == "dori" else None)  # fmt: skip
    kv_set(db, "paused", False)
    data = {"key": "inc", "urgency": "urgent"}
    event = Event(source="ext:dori", type="ext.dori.item", dedupe_key="ext:dori:1",
                  payload={"text": "Dori failing", "data": data})  # fmt: skip
    await pulse.route(event)
    assert channel.texts == ["[dori] \U0001f534 Dori failing"]
    assert system_texts.between(db, OWNER) == []


# --- the contract helpers ----------------------------------------------------------------


def test_contract_helpers() -> None:
    assert token_ref_for("cori") == "core/notify_token_cori"
    assert token_ref_for("my-bot") == "core/notify_token_my_bot"
    assert token_ref_for("a" * 32) is None  # too long for a ref
    assert owner_key("ext/Dori/Inc 1") == "ext/dori/inc-1"
    long = owner_key("ext/" + "x" * 200)
    assert len(long) == 120 and items.valid_key(long)
    assert owner_key("ext/" + "x" * 200 + "a") != owner_key("ext/" + "x" * 200 + "b")
    parsed = parse_body(json.dumps(body()).encode())
    assert not isinstance(parsed, str) and parsed.instance == "cori"


# --- instance side: the bridge -----------------------------------------------------------


class FakePersonal:
    """A transport answering like personal Resonant would, or failing."""

    def __init__(self) -> None:
        self.status = 200
        self.down = False
        self.bodies: list[dict[str, Any]] = []
        self.tokens: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused")
        self.tokens.append(request.headers["authorization"])
        self.bodies.append(json.loads(request.content))
        if self.status == 200:
            return httpx.Response(200, json={"outcome": "sent", "reason": None})
        return httpx.Response(self.status, json={"detail": "nope"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def cori_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "cori.db")
    yield conn
    conn.close()


def bridge(
    db: sqlite3.Connection, fake: FakePersonal, clock: Clock, slack: Channel, *, token: bool = True
) -> OwnerBridge:
    keychain = FakeKeychain(service="resonant-cori")
    if token:
        keychain.items["core/notify_token"] = TOKEN
    return OwnerBridge(db, NotifyOwnerConfig(), instance="cori",
                       secrets=SecretStore(keychain, service="resonant-cori"), channel=slack,
                       owner=OWNER, clock=clock, transport=fake.transport())  # fmt: skip


async def test_the_bridge_posts_the_contract(cori_db: sqlite3.Connection) -> None:
    fake, clock, slack = FakePersonal(), Clock(), Channel()
    b = bridge(cori_db, fake, clock, slack)
    b.forward("ext/dori/inc-1", "dori: Dori failing")
    assert await b.flush() == 1
    want = {"instance": "cori", "key": "ext/dori/inc-1", "title": "dori: Dori failing",
            "urgency": "urgent", "clear": False}  # fmt: skip
    assert fake.bodies == [want]
    assert fake.tokens == [f"Bearer {TOKEN}"]
    assert b.pending() == {} and slack.texts == []


async def test_the_latest_alert_per_key_wins(cori_db: sqlite3.Connection) -> None:
    fake, clock, slack = FakePersonal(), Clock(), Channel()
    b = bridge(cori_db, fake, clock, slack)
    b.forward("health/store", "store: disk low")
    b.forward("health/store", "store is healthy again", clear=True)
    await b.flush()
    assert [(x["title"], x["clear"]) for x in fake.bodies] == [("store is healthy again", True)]


async def test_unreachable_one_dm_then_retries_with_backoff(cori_db: sqlite3.Connection) -> None:
    fake, clock, slack = FakePersonal(), Clock(), Channel()
    fake.down = True
    b = bridge(cori_db, fake, clock, slack)
    b.forward("ext/dori/inc-1", "dori: Dori failing")
    b.forward("ext/dori/inc-2", "dori: Dori slow")
    assert await b.flush() == 0
    assert len(slack.texts) == 1 and "can't reach Resonant" in slack.texts[0]
    assert set(b.pending()) == {"ext/dori/inc-1", "ext/dori/inc-2"}
    assert await b.flush() == 0  # not due yet: backed off, nothing tried
    assert b.next_due_s() == pytest.approx(5.0)
    clock.now += timedelta(seconds=5)
    await b.flush()  # tried again, still down: no second DM
    assert len(slack.texts) == 1
    assert b.pending()["ext/dori/inc-1"]["attempts"] == 2
    assert b.next_due_s() == pytest.approx(10.0)
    fake.down = False
    clock.now += timedelta(seconds=10)
    assert await b.flush() == 2
    assert b.pending() == {} and kv_get(cori_db, DOWN_KV) is None
    fake.down = True  # a new outage says so again
    b.forward("ext/dori/inc-3", "dori: again")
    await b.flush()
    assert len(slack.texts) == 2


async def test_a_dm_that_fails_is_tried_again(cori_db: sqlite3.Connection) -> None:
    fake, clock, slack = FakePersonal(), Clock(), Channel(fail=1)
    fake.down = True
    b = bridge(cori_db, fake, clock, slack)
    b.forward("k", "t")
    await b.flush()
    assert slack.texts == []
    clock.now += timedelta(seconds=5)
    await b.flush()
    assert len(slack.texts) == 1


async def test_a_token_mismatch_is_said_once(cori_db: sqlite3.Connection) -> None:
    fake, clock, slack = FakePersonal(), Clock(), Channel()
    fake.status = 401
    b = bridge(cori_db, fake, clock, slack)
    b.forward("k1", "t1")
    await b.flush()
    clock.now += timedelta(minutes=10)
    b.forward("k2", "t2")
    await b.flush()
    assert len(slack.texts) == 1 and slack.texts[0].startswith("notify token mismatch")
    assert set(b.pending()) == {"k1", "k2"}  # kept: a fixed token delivers them


async def test_not_set_up_and_no_token_are_said(cori_db: sqlite3.Connection) -> None:
    fake, clock, slack = FakePersonal(), Clock(), Channel()
    fake.status = 404
    b = bridge(cori_db, fake, clock, slack)
    b.forward("k1", "t1")
    await b.flush()
    assert "core/notify_token_cori" in slack.texts[0]
    b2 = bridge(cori_db, fake, clock, slack, token=False)
    clock.now += timedelta(minutes=10)
    await b2.flush()
    assert "core/notify_token isn't set here" in slack.texts[1]
    assert fake.bodies == [fake.bodies[0]]  # no POST without a token


async def test_a_422_is_dropped(cori_db: sqlite3.Connection) -> None:
    fake, clock, slack = FakePersonal(), Clock(), Channel()
    fake.status = 422
    b = bridge(cori_db, fake, clock, slack)
    b.forward("k1", "t1")
    assert await b.flush() == 0
    assert b.pending() == {} and slack.texts == []


async def test_a_title_with_nothing_printable_is_sent_as_urgent_alert(
    cori_db: sqlite3.Connection,
) -> None:
    fake, clock, slack = FakePersonal(), Clock(), Channel()
    b = bridge(cori_db, fake, clock, slack)
    b.forward("k1", "\u200b\u202e")
    await b.flush()
    assert fake.bodies[0]["title"] == "urgent alert"


async def test_old_alerts_expire(cori_db: sqlite3.Connection) -> None:
    fake, clock, slack = FakePersonal(), Clock(), Channel()
    fake.down = True
    b = bridge(cori_db, fake, clock, slack)
    b.forward("k1", "t1")
    await b.flush()
    clock.now += timedelta(hours=25)
    await b.flush()
    assert b.pending() == {}


# --- instance side: hooks ----------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    def forward(self, key: str, title: str, *, clear: bool = False) -> None:
        self.calls.append((key, title, clear))


async def test_the_pulse_forwards_urgent_news_on_cori(cori_db: sqlite3.Connection) -> None:
    from resonant_sdk import Manifest

    rec = Recorder()
    m = Manifest.model_validate({"name": "dori", "version": 1, "report": {"max_urgency": "urgent"}})
    pulse = Pulse(cori_db, channel=Channel(), recipients=lambda _e: (OWNER,),
                  manifest_of=lambda e: m if e == "dori" else None, bridge=rec)  # type: ignore[arg-type]  # fmt: skip
    kv_set(cori_db, "paused", False)

    def ev(n: int, urgency: str, title: str = "Dori failing", clear: bool = False) -> Event:
        data = {"key": "inc", "urgency": urgency, "clear": clear}
        return Event(source="ext:dori", type="ext.dori.item", dedupe_key=f"ext:dori:{n}",
                     payload={"text": title, "data": data})  # fmt: skip

    await pulse.route(ev(1, "normal", "fyi"))  # not urgent: not forwarded
    await pulse.route(ev(2, "urgent"))
    await pulse.route(ev(3, "urgent"))  # told already: not again
    await pulse.route(ev(4, "normal", clear=True))  # the clear of an urgent item is urgent
    assert rec.calls == [("ext/dori/inc", "dori: Dori failing", False),
                         ("ext/dori/inc", "dori: Dori failing", True)]  # fmt: skip
    kv_set(cori_db, "paused", True)
    await pulse.route(ev(5, "urgent", "Dori down again"))
    assert len(rec.calls) == 2  # Cori paused: held, not forwarded


async def test_the_monitor_forwards_incidents_and_recoveries(cori_db: sqlite3.Connection) -> None:
    rec = Recorder()
    mon = HealthMonitor(cori_db, model=None, channel=None, owner=None,
                        daemon_healthy=lambda: True, bridge=rec)  # type: ignore[arg-type]  # fmt: skip
    mon.states = {}
    bad = CheckState(state="degraded", detail="disk low", consecutive=1)
    await mon._notify({"store": bad}, T0)  # pyright: ignore[reportPrivateUsage]
    assert rec.calls == []  # not yet alert_after
    bad.consecutive = 2
    await mon._notify({"store": bad}, T0)  # pyright: ignore[reportPrivateUsage]
    await mon._notify({"store": bad}, T0)  # pyright: ignore[reportPrivateUsage]
    assert rec.calls == [("health/store", "store: disk low", False)]
    assert kv_get(cori_db, BRIDGED_KV) == ["store"]
    await mon._notify({"store": CheckState(state="ok")}, T0)  # pyright: ignore[reportPrivateUsage]
    assert rec.calls[-1] == ("health/store", "store is healthy again", True)
    assert kv_get(cori_db, BRIDGED_KV) == []


# --- config and CLI ----------------------------------------------------------------------


def test_notify_owner_config(tmp_path: Path) -> None:
    cori = tmp_path / "cori-home"
    s = load_settings(home=cori, instance={"name": "cori"}, notify_owner={})
    assert s.notify_owner is not None
    assert (s.notify_owner.url, s.notify_owner.token_ref) == ("http://127.0.0.1:7777",
                                                              "core/notify_token")  # fmt: skip
    assert load_settings(home=cori, instance={"name": "cori"}).notify_owner is None
    with pytest.raises(ValidationError, match="notify_owner is for another instance"):
        load_settings(notify_owner={})
    for url in ("http://example.com:7777", "ftp://127.0.0.1", "http://127.0.0.1:7777/api"):
        with pytest.raises(ValidationError):
            load_settings(home=cori, instance={"name": "cori"}, notify_owner={"url": url})
    with pytest.raises(ValidationError):
        load_settings(home=cori, instance={"name": "cori"}, notify_owner={"token_ref": "bad"})


def test_cli_notify_owner_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "cori-home"
    home.mkdir()
    (home / "config.yaml").write_text("instance:\n  name: cori\nnotify_owner: {}\n")
    monkeypatch.setenv("RESONANT_HOME", str(home))
    keychain = FakeKeychain(items={"core/notify_token": TOKEN}, service="resonant-cori")
    monkeypatch.setattr(
        cli, "_secret_store", lambda: SecretStore(keychain, service="resonant-cori")
    )
    fake = FakePersonal()
    real = httpx.AsyncClient

    def client(**kw: Any) -> httpx.AsyncClient:
        assert kw["base_url"] == "http://127.0.0.1:7777"  # the configured URL; never reached
        kw["transport"] = fake.transport()
        return real(**kw)

    monkeypatch.setattr("resonant.notify_owner.httpx.AsyncClient", client)
    r = CliRunner().invoke(cli.app, ["notify-owner", "test"])
    assert r.exit_code == 0, r.output
    assert "personal Resonant: texted" in r.output
    assert fake.bodies[0]["key"] == TEST_KEY and fake.bodies[0]["title"] == TEST_TITLE
    fake.status = 401
    r = CliRunner().invoke(cli.app, ["notify-owner", "test", "--clear"])
    assert r.exit_code == 1 and "notify token mismatch" in r.output
    assert fake.bodies[-1]["clear"] is True


def test_cli_notify_owner_test_needs_the_config() -> None:
    r = CliRunner().invoke(cli.app, ["notify-owner", "test"])
    assert r.exit_code == 2 and "notify_owner isn't set" in r.output


# --- two daemons in one process ----------------------------------------------------------


def _principals(home: Path, identity: str) -> None:
    (home / "principals.yaml").write_text(
        f'principals:\n  {OWNER}:\n    role: owner\n    identities: ["{identity}"]\n'
    )


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def two_daemons(tmp_path: Path) -> tuple[Daemon, FakeChannel, Daemon, FakeChannel, Settings]:
    p_home, c_home = tmp_path / "personal", tmp_path / "cori"
    p_home.mkdir()
    c_home.mkdir()
    _principals(p_home, "imessage:+15551234567")
    _principals(c_home, "slack:U1")
    p_settings = load_settings(home=p_home, dry_run=False, api={"port": _free_port()})
    c_settings = load_settings(
        home=c_home, dry_run=False, instance={"name": "cori"}, api={"port": _free_port()},
        notify_owner={"url": f"http://127.0.0.1:{p_settings.api.port}"},
    )  # fmt: skip
    conn = open_db(c_settings.db_path)
    install_fixture(c_settings, conn, "name: dori\nversion: 1\nreport: {max_urgency: urgent}\n",
                    {"cli.py": "# stub\n"})  # fmt: skip
    conn.close()
    imessage = FakeChannel("imessage", load_principals(p_home / "principals.yaml"), threaded=False)
    slack = FakeChannel("slack", load_principals(c_home / "principals.yaml"))
    p_keys = FakeKeychain(items={"core/notify_token_cori": TOKEN})
    c_keys = FakeKeychain(items={"core/notify_token": TOKEN}, service="resonant-cori")
    personal = Daemon(p_settings, model=ScriptedModel(), ops_runner=FakeOpsRunner(),
                      secrets=SecretStore(p_keys), channels={"imessage": imessage})  # fmt: skip
    cori = Daemon(c_settings, model=ScriptedModel(), ops_runner=FakeOpsRunner(),
                  secrets=SecretStore(c_keys, service="resonant-cori"),
                  channels={"slack": slack})  # fmt: skip
    for d in (personal, cori):
        kv_set(d.conn, "paused", False)
    return personal, imessage, cori, slack, c_settings


async def test_two_daemons_coris_urgent_report_texts_the_owner(tmp_path: Path) -> None:
    personal, imessage, cori, slack, _ = two_daemons(tmp_path)
    try:
        assert cori.stack is not None and cori.stack.bridge is not None
        assert personal.stack is not None and personal.stack.bridge is None
        bridge = cori.stack.bridge
        bridge.transport = httpx.ASGITransport(app=create_app(personal.state))
        await slack.start(cori.loop.submit)
        await imessage.start(personal.loop.submit)

        def report(n: int, title: str, clear: bool = False) -> Event:
            data = {"key": "inc-1", "urgency": "urgent", "clear": clear}
            event = Event(source="ext:dori", type="ext.dori.item", dedupe_key=f"ext:dori:{n}",
                          payload={"text": title, "data": data})  # fmt: skip
            assert insert_event(cori.conn, event)
            return event

        assert cori.loop.router is not None
        await cori.loop.router(report(1, "Dori failing"))
        assert slack.texts == ["[dori] \U0001f534 Dori failing"]  # Cori's own DM, unchanged
        assert await bridge.flush() == 1
        assert imessage.texts == ["[Cori] \U0001f534 dori: Dori failing"]
        await cori.loop.router(report(2, "Dori failing"))  # the same news again: nothing
        await bridge.flush()
        assert len(imessage.texts) == 1
        await cori.loop.router(report(3, "Dori is fine", clear=True))
        await bridge.flush()
        assert imessage.texts[-1] == "[Cori] ✓ dori: Dori is fine"
        # personal's brain has it in the transcript, as outside text
        texts = system_texts.between(personal.conn, OWNER)
        # (system_texts stores texts cleaned: brackets read as parentheses)
        assert [t.text.replace("(Cori)", "[Cori]") for t in texts] == imessage.texts
        assert all(t.outside for t in texts)
        # personal unreachable: one Slack DM, and the alert is kept for the retry
        bridge.transport = httpx.MockTransport(lambda _r: (_ for _ in ()).throw(
            httpx.ConnectError("refused")))  # fmt: skip
        await cori.loop.router(report(4, "Dori failing again"))
        await bridge.flush()
        assert slack.texts[-1].startswith("I can't reach Resonant")
        assert list(bridge.pending()) == ["ext/dori/inc-1"]
    finally:
        personal.conn.close()
        cori.conn.close()

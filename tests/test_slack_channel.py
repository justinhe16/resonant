"""PER-295: ``SlackChannel`` over the fake Slack transport (no network, no real Keychain)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from resonant.config import SlackConfig, load_settings
from resonant.gate import Intent, NeedsApproval, PolicyGate
from resonant.gate.types import ApprovalReply, ApprovalResolution
from resonant.gateway.channel import (
    ChannelRateLimited,
    ChannelRefused,
    ChannelSendError,
    MessageRef,
    shared_thread,
)
from resonant.gateway.hub import ChannelHub
from resonant.gateway.slack import SlackChannel
from resonant.gateway.slack.blocks import format_text
from resonant.gateway.slack.client import SlackCallError, SlackRateLimited
from resonant.gateway.slack.fixtures import (
    FakeSlackSocket,
    FakeSlackTransport,
    FakeSlackWeb,
    click,
    mention,
    message,
)
from resonant.loop import NewTask
from resonant.principals import Principals
from resonant.runners.approve import prompt_approvers, remind_due
from resonant.secrets import FakeKeychain, SecretMissing, SecretStore
from resonant.store import approvals as ap
from resonant.store import slack as slack_store
from resonant.store import tasks as repo
from resonant.store.db import utcnow
from resonant.tools import ToolRegistry
from resonant.wiring import build_channels
from resonant_sdk import Effect, Event, Level, ToolSpec

TEAM, BOT = "T0TEAM", "U0BOT"
JUSTIN, BRIAN, MIU = "U1JUSTIN", "U2BRIAN", "U3MIU"
SERVICE = "resonant-cori"
BOT_TOKEN = "xoxb-SENTINEL-bot-0000000000"
APP_TOKEN = "xapp-SENTINEL-app-0000000000"
WIPE = ToolSpec(name="wipe", effect=Effect.WRITE, level=Level.L1)
ADD = ToolSpec(name="add_item", effect=Effect.WRITE, level=Level.L2)
PAY = ToolSpec(name="pay_bill", effect=Effect.PAY, level=Level.L1)


def principals(default: bool = False) -> Principals:
    data: dict[str, object] = {
        "principals": {
            "justin": {"role": "owner", "identities": [f"slack:{JUSTIN}", "imessage:+15550000001"]},
            "brian": {"role": "approver", "identities": [f"slack:{BRIAN}"]},
            "miu": {"identities": [f"slack:{MIU}"]},
            "mom": {"identities": ["imessage:+15550000002"]},
        }
    }
    if default:
        data["default"] = {"channel": "slack", "grants": {}}
    return Principals.model_validate(data)


def config(**kw: object) -> SlackConfig:
    base: dict[str, object] = {
        "enabled": True,
        "team_id": TEAM,
        "bot_user_id": BOT,
        "rooms": {"digest": "C0DIGEST", "pulse": "C0PULSE"},
    }
    base.update(kw)
    return SlackConfig.model_validate(base)


@dataclass
class Clock:
    now: datetime = field(default_factory=utcnow)

    def __call__(self) -> datetime:
        return self.now


@dataclass
class World:
    slack: SlackChannel
    web: FakeSlackWeb
    socket: FakeSlackSocket
    transport: FakeSlackTransport
    events: list[Event]
    clock: Clock
    slept: list[float]


def keychain() -> SecretStore:
    kc = FakeKeychain(
        items={"slack/bot_token": BOT_TOKEN, "slack/app_token": APP_TOKEN}, service=SERVICE
    )
    return SecretStore(kc, service=SERVICE)


def make(
    db: sqlite3.Connection,
    *,
    p: Principals | None = None,
    gate: PolicyGate | None = None,
    **cfg: object,
) -> World:
    web, socket = FakeSlackWeb(team_id=TEAM, bot_user_id=BOT), FakeSlackSocket()
    transport = FakeSlackTransport(web, socket)
    clock = Clock()
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    slack = SlackChannel(
        config(**cfg),
        p or principals(),
        db,
        secrets=keychain(),
        gate=gate,
        transport=transport,
        clock=clock,
        sleep=sleep,
        sweep_s=3600,
    )
    return World(slack, web, socket, transport, [], clock, slept)


def submitter(w: World):  # a loop.submit stand-in: dedupes like the events table
    seen: set[str] = set()

    def submit(event: Event) -> bool:
        if event.dedupe_key in seen:
            return False
        seen.add(event.dedupe_key or event.id)
        w.events.append(event)
        return True

    return submit


@pytest.fixture
async def w(db: sqlite3.Connection) -> AsyncIterator[World]:
    world = make(db)
    await world.slack.start(submitter(world))
    yield world
    await world.slack.stop()


# --- config -----------------------------------------------------------------------------------


def test_config_defaults_off_and_needs_team_and_bot() -> None:
    s = load_settings()
    assert s.channels.slack.enabled is False and s.channels.enabled() == ()
    with pytest.raises(ValidationError, match="team_id and bot_user_id"):
        SlackConfig(enabled=True)
    with pytest.raises(ValidationError, match="channel id"):
        config(rooms={"digest": "not-a-room"})
    with pytest.raises(ValidationError):
        config(team_id="nope")
    with pytest.raises(ValidationError):
        config(bot_token_ref="Not A Ref")
    assert config(link_base="https://dash.example.com/").link_base == "https://dash.example.com"
    assert load_settings(channels={"slack": config().model_dump()}).channels.enabled() == ("slack",)


def test_build_channels_adds_slack_only_when_enabled(db: sqlite3.Connection) -> None:
    off = load_settings()
    assert build_channels(off, principals(), db) == {}
    on = load_settings(channels={"slack": config().model_dump()})
    built = build_channels(on, principals(), db, secrets=keychain())
    assert list(built) == ["slack"] and isinstance(built["slack"], SlackChannel)


# --- start ------------------------------------------------------------------------------------


async def test_start_reads_both_tokens_from_the_instance_keychain(w: World) -> None:
    assert w.transport.tokens == [BOT_TOKEN, APP_TOKEN]
    assert w.socket.connects == 1 and w.web.method_calls("auth.test") == [{}]
    h = w.slack.health()
    assert h["ok"] is True and h["connected"] is True and h["followed_threads"] == 0
    assert h["detail"] == "connected, 0 followed threads"


async def test_start_refuses_a_token_for_another_workspace(db: sqlite3.Connection) -> None:
    world = make(db)
    world.web.auth_team = "T0OTHER"
    with pytest.raises(RuntimeError, match="another workspace"):
        await world.slack.start(submitter(world))
    assert world.socket.connects == 0


async def test_start_fails_on_a_missing_token(db: sqlite3.Connection) -> None:
    world = make(db)
    kc = FakeKeychain(items={"slack/bot_token": BOT_TOKEN}, service=SERVICE)
    world.slack.secrets = SecretStore(kc, service=SERVICE)
    with pytest.raises(SecretMissing):
        await world.slack.start(submitter(world))


async def test_tokens_never_reach_logs_audit_or_kv(
    db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    world = make(db)
    await world.slack.start(submitter(world))
    await world.socket.deliver(mention(BRIAN, f"<@{BOT}> hi", team=TEAM))
    world.web.fail("chat.postMessage", SlackCallError("chat.postMessage", "invalid_auth"))
    with pytest.raises(ChannelSendError) as e:
        await world.slack.send("brian", "hello")
    await world.slack.self_test()
    await world.slack.stop()
    dumped = "\n".join(
        [caplog.text, str(e.value)]
        + [str(tuple(r)) for r in db.execute("SELECT * FROM audit_log")]
        + [str(tuple(r)) for r in db.execute("SELECT * FROM kv")]
    )
    assert "SENTINEL" not in dumped
    assert "hello" not in caplog.text  # bodies aren't logged either


# --- inbound ----------------------------------------------------------------------------------


async def test_a_mention_routes_with_the_contract_payload_and_follows_the_thread(w: World) -> None:
    env = mention(BRIAN, f"<@{BOT}> what's on staging?", ts="1700000000.000100")
    await w.socket.deliver(env)
    assert w.socket.acks == [env["envelope_id"]]
    (e,) = w.events
    assert (e.source, e.type, e.principal) == ("slack", "message", "brian")
    assert e.dedupe_key == "slack:C0GENERAL:1700000000.000100"
    assert e.payload == {
        "text": "what's on staging?",
        "identity": f"slack:{BRIAN}",
        "channel": "C0GENERAL",
        "ts": "1700000000.000100",
        "thread_ts": "1700000000.000100",
        "thread": "C0GENERAL:1700000000.000100",
        "ref": "C0GENERAL:1700000000.000100",
        "outside": True,
        "at": e.payload["at"],
        "truncated": False,
    }
    assert w.web.reactions == [
        {"channel": "C0GENERAL", "timestamp": "1700000000.000100", "name": "eyes"}
    ]
    assert slack_store.known(w.slack.conn, "C0GENERAL", "1700000000.000100")
    assert w.slack.health()["followed_threads"] == 1


async def test_untagged_replies_route_only_in_followed_threads(w: World) -> None:
    await w.socket.deliver(mention(BRIAN, f"<@{BOT}> deploy status?", ts="1700000000.000100"))
    reply = message(MIU, "and staging?", ts="1700000001.000100", thread_ts="1700000000.000100")
    await w.socket.deliver(reply)
    assert [e.principal for e in w.events] == ["brian", "miu"]
    assert w.events[1].payload["thread"] == "C0GENERAL:1700000000.000100"
    # untagged elsewhere: a top-level message, and a reply in a thread Cori never joined
    await w.socket.deliver(message(BRIAN, "lunch?", ts="1700000002.000100"))
    other = message(BRIAN, "hm", ts="1700000003.000100", thread_ts="1600000000.000100")
    await w.socket.deliver(other)
    assert len(w.events) == 2
    # idle past follow_idle_days: no longer followed
    w.clock.now += timedelta(days=15)
    late = message(MIU, "still?", ts="1700000004.000100", thread_ts="1700000000.000100")
    await w.socket.deliver(late)
    assert len(w.events) == 2


async def test_dms_route_without_a_tag(w: World) -> None:
    await w.socket.deliver(message(JUSTIN, "status?", channel="D0JUSTIN", channel_type="im"))
    (e,) = w.events
    assert e.principal == "justin" and e.payload["thread"] == "D0JUSTIN:1700000000.000100"


@pytest.mark.parametrize(
    "env",
    [
        mention(BOT, f"<@{BOT}> echo"),  # the bot's own message
        message(BRIAN, f"<@{BOT}> x", bot_id="B0123"),  # any bot
        message(BRIAN, f"<@{BOT}> edited", subtype="message_changed"),
        message(BRIAN, "joined", subtype="channel_join"),
        mention(BRIAN, f"<@{BOT}> hi", team="T0OTHER"),  # another workspace
        mention(BRIAN, f"<@{BOT}> hi", user_team="T0OTHER"),  # a shared-channel stranger
        {"envelope_id": "e1", "type": "events_api", "payload": {"event": {"type": "message"}}},
        {"envelope_id": "e2", "type": "events_api", "payload": "truncated"},
    ],
)
async def test_echoes_edits_other_teams_and_truncated_payloads_never_route(
    w: World, env: dict[str, object]
) -> None:
    await w.socket.deliver(env)
    assert w.events == [] and w.web.reactions == []
    assert w.socket.acks == [env["envelope_id"]]  # still acked at once


async def test_long_messages_are_dropped_and_unknown_senders_audited(
    db: sqlite3.Connection,
) -> None:
    world = make(db, max_body_chars=10)
    await world.slack.start(submitter(world))
    await world.socket.deliver(mention(BRIAN, f"<@{BOT}> " + "x" * 11))
    await world.socket.deliver(mention("U9STRANGER", f"<@{BOT}> hi", ts="1700000009.000100"))
    assert world.events == []
    actions = [r[0] for r in db.execute("SELECT action FROM audit_log ORDER BY id")]
    assert "slack.too_long" in actions and "slack.unknown_sender" in actions
    await world.slack.stop()


async def test_an_unlisted_same_team_user_is_vouched_for_the_workspace_default(
    db: sqlite3.Connection,
) -> None:
    world = make(db, p=principals(default=True))
    await world.slack.start(submitter(world))
    await world.socket.deliver(mention("U9STRANGER", f"<@{BOT}> hi"))
    assert [e.principal for e in world.events] == ["slack:U9STRANGER"]
    await world.slack.stop()


async def test_mentions_render_as_names_cached(w: World) -> None:
    w.web.names[MIU] = "Miu"
    await w.socket.deliver(mention(BRIAN, f"<@{BOT}> ask <@{MIU}> &amp; me", ts="1.000001"))
    await w.socket.deliver(mention(BRIAN, f"<@{BOT}> <@{MIU}> again", ts="2.000001"))
    assert [e.payload["text"] for e in w.events] == ["ask @Miu & me", "@Miu again"]
    assert len(w.web.method_calls("users.info")) == 1


async def test_redelivered_envelopes_are_deduped_and_reconnects_keep_routing(w: World) -> None:
    env = mention(BRIAN, f"<@{BOT}> once", ts="1700000000.000100")
    await w.socket.deliver(env)
    await w.socket.deliver({**env, "envelope_id": "redelivered", "retry_attempt": 1})
    # the message twin of the app_mention (same ts) is the same event too
    await w.socket.deliver(message(BRIAN, f"<@{BOT}> once", ts="1700000000.000100"))
    assert len(w.events) == 1 and len(w.web.reactions) == 1
    w.socket.drop()
    await w.slack._sweep_once()  # pyright: ignore[reportPrivateUsage]
    h = w.slack.health()
    assert h["ok"] is False and h["last_error"] == "socket disconnected"
    assert (await w.slack.self_test()).ok is False
    w.socket.reconnect()
    await w.socket.deliver(mention(BRIAN, f"<@{BOT}> back", ts="1700000005.000100"))
    assert len(w.events) == 2 and w.slack.health()["ok"] is True
    assert (await w.slack.self_test()).ok is True


async def test_inbound_rate_limit(db: sqlite3.Connection) -> None:
    world = make(db, inbound_per_minute=2)
    await world.slack.start(submitter(world))
    for i in range(3):
        await world.socket.deliver(mention(BRIAN, f"<@{BOT}> {i}", ts=f"170000000{i}.000100"))
    assert len(world.events) == 2
    await world.slack.stop()


# --- outbound ---------------------------------------------------------------------------------


async def test_send_in_a_followed_thread_or_else_a_dm(w: World) -> None:
    await w.socket.deliver(mention(BRIAN, f"<@{BOT}> hi", ts="1700000000.000100"))
    thread = MessageRef("slack", "C0GENERAL:1700000000.000100", "C0GENERAL:1700000000.000100")
    ref = await w.slack.send("brian", "On it", thread=thread)
    post = w.web.posts[-1]
    assert (post.channel, post.thread_ts, post.text) == ("C0GENERAL", "1700000000.000100", "On it")
    assert ref == MessageRef("slack", f"C0GENERAL:{post.ts}", "C0GENERAL:1700000000.000100")
    # a thread Cori doesn't follow is never posted to: the principal's DM instead
    stray = MessageRef("slack", "C0RANDOM:1.000001", "C0RANDOM:1.000001")
    await w.slack.send("brian", "hello", thread=stray)
    await w.slack.send("brian", "again")
    assert [(p.channel, p.thread_ts) for p in w.web.posts[-2:]] == [("D2BRIAN", None)] * 2
    assert w.web.method_calls("conversations.open") == [{"users": BRIAN}]  # cached


async def test_send_refuses_unknown_principals_and_those_without_slack(w: World) -> None:
    with pytest.raises(ChannelRefused):
        await w.slack.send("nobody", "x")
    with pytest.raises(ChannelRefused):
        await w.slack.send("mom", "x")
    assert w.web.posts == []


async def test_send_chunks_dedupes_and_links(db: sqlite3.Connection) -> None:
    world = make(db, link_base="https://dash.example.com")
    await world.slack.start(submitter(world))
    long = ("word " * 1300).strip()
    await world.slack.send("brian", long, dedupe_key="k1")
    n = len(world.web.posts)
    assert n == 3 and all(len(p.text) <= 3000 for p in world.web.posts)
    assert world.web.posts[0].text.endswith("(1/3)")
    await world.slack.send("brian", long, dedupe_key="k1")
    assert len(world.web.posts) == n  # deduped
    await world.slack.send(
        "brian", "R12 opened https://github.com/justinhe16/resonant/pull/164 <ok> & done"
    )
    assert world.web.posts[-1].text == (
        "<https://dash.example.com#/runs/R12|R12> opened "
        "<https://github.com/justinhe16/resonant/pull/164|PR #164> &lt;ok&gt; &amp; done"
    )
    await world.slack.stop()


def test_format_text_without_a_link_base_keeps_run_codes_plain() -> None:
    assert format_text("R7 at https://github.com/a/b/pull/3") == (
        "R7 at <https://github.com/a/b/pull/3|PR #3>"
    )


async def test_send_respects_retry_after(w: World) -> None:
    w.web.fail("chat.postMessage", SlackRateLimited("chat.postMessage", 2))
    await w.slack.send("brian", "hi")
    assert w.slept == [2] and len(w.web.posts) == 1
    w.web.fail("chat.postMessage", SlackRateLimited("chat.postMessage", 120))
    with pytest.raises(ChannelRateLimited):
        await w.slack.send("brian", "hi again")
    w.web.fail("chat.postMessage", SlackCallError("chat.postMessage", "channel_not_found"))
    with pytest.raises(ChannelSendError, match="channel_not_found") as e:
        await w.slack.send("brian", "secret body")
    assert "secret body" not in str(e.value)


# --- rooms ------------------------------------------------------------------------------------


async def test_post_room_posts_only_to_configured_rooms(w: World, db: sqlite3.Connection) -> None:
    await w.slack.post_room("digest", "Tuesday's digest")
    assert (w.web.posts[-1].channel, w.web.posts[-1].text) == ("C0DIGEST", "Tuesday's digest")
    with pytest.raises(ChannelRefused):
        await w.slack.post_room("general", "nope")
    assert len(w.web.posts) == 1
    hub = ChannelHub({"slack": w.slack}, principals(), db)
    await hub.post_room("pulse", "alive")
    assert w.web.posts[-1].channel == "C0PULSE"
    with pytest.raises(ChannelRefused):
        await hub.post_room("unknown", "nope")


async def test_a_failed_room_post_is_noted_in_health_until_the_room_works(w: World) -> None:
    # PER-303: the bot removed from #voyager-pulse -> the slack check's detail says so (the
    # check stays ok: everything else still works); the next good post clears it
    w.web.fail("chat.postMessage", SlackCallError("chat.postMessage", "not_in_channel"))
    with pytest.raises(ChannelSendError):
        await w.slack.post_room("pulse", "alive", dedupe_key="heartbeat:2026-10-13:09")
    h = w.slack.health()
    assert "room pulse: chat.postMessage failed: not_in_channel" in h["detail"]
    assert h["ok"] is True and h["last_error"] is None
    with pytest.raises(ChannelRefused):
        await w.slack.post_room("gone", "alive")
    assert "room gone: unknown room 'gone'" in w.slack.health()["detail"]
    await w.slack.post_room("pulse", "alive", dedupe_key="heartbeat:2026-10-13:10")
    assert "room pulse" not in w.slack.health()["detail"]


HEADER = {"type": "header", "text": {"type": "plain_text", "text": "Daily"}}


def dropped(db: sqlite3.Connection) -> list[str]:
    q = "SELECT json_extract(detail, '$.reason') FROM audit_log WHERE action = ?"
    return [r[0] for r in db.execute(q, ("slack.room_blocks_dropped",))]


async def test_a_room_post_carries_blocks_with_the_text_as_fallback(
    w: World, db: sqlite3.Connection
) -> None:
    # PER-332: one message, the blocks plus the text (notifications); the hub passes them on
    hub = ChannelHub({"slack": w.slack}, principals(), db)
    await hub.post_room("digest", "Daily: 3 orders", blocks=[HEADER], dedupe_key="d:1")
    post = w.web.posts[-1]
    assert (post.channel, post.text, post.blocks) == ("C0DIGEST", "Daily: 3 orders", [HEADER])
    await hub.post_room("digest", "Daily: 3 orders", blocks=[HEADER], dedupe_key="d:1")
    assert len(w.web.posts) == 1 and dropped(db) == []
    await w.slack.post_room("digest", "x" * 3200, blocks=[HEADER])
    assert len(w.web.posts[-1].text) == 3000 and w.web.posts[-1].blocks == [HEADER]
    rich: list[dict[str, Any]] = [
        HEADER,
        {"type": "section", "fields": [{"type": "mrkdwn", "text": "*Web*\n<https://x.test|ok>"}]},
        {"type": "divider"},
        {"type": "context", "elements": [{"type": "plain_text", "text": "today", "emoji": True}]},
        {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
            {"type": "text", "text": "PRs "}, {"type": "link", "url": "https://x.test/pr/1"}]}]},
    ]  # fmt: skip
    await w.slack.post_room("digest", "Daily", blocks=rich)
    assert w.web.posts[-1].blocks == rich and dropped(db) == []


@pytest.mark.parametrize(
    "bad",
    [
        [],
        [HEADER] * 51,
        ["not a block"],
        [{"text": "no type"}],
        [{"type": "section", "text": {"type": "mrkdwn", "text": "hi <!channel>"}}],
        [{"type": "section", "text": {"type": "mrkdwn", "text": "hi <@U123>"}}],
        [{"type": "rich_text", "elements": [
            {"type": "rich_text_section", "elements": [{"type": "broadcast", "range": "channel"}]}
        ]}],
        [{"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text",
                                                                      "text": "Go"}}]}],
        [{"type": "section", "text": {"type": "mrkdwn", "text": "x"},
          "accessory": {"type": "image", "image_url": "https://example.com/p.png"}}],
        [{"type": "input"}],
        [{"type": ["x"]}],
        [{"type": "section", "text": {"type": {"a": 1}, "text": "x"}}],
    ],
)  # fmt: skip
async def test_invalid_room_blocks_post_the_text_alone(
    w: World, db: sqlite3.Connection, bad: list[Any]
) -> None:
    await w.slack.post_room("digest", "Daily: 3 orders", blocks=bad)
    assert [(p.text, p.blocks) for p in w.web.posts] == [("Daily: 3 orders", None)]
    assert dropped(db) == ["invalid"]


async def test_blocks_slack_rejects_fall_back_to_the_text(w: World, db: sqlite3.Connection) -> None:
    w.web.fail("chat.postMessage", SlackCallError("chat.postMessage", "invalid_blocks"))
    ref = await w.slack.post_room("digest", "Daily: 3 orders", blocks=[HEADER])
    assert [(p.text, p.blocks) for p in w.web.posts] == [("Daily: 3 orders", None)]
    assert ref.ref == f"C0DIGEST:{w.web.posts[0].ts}" and dropped(db) == ["invalid_blocks"]
    assert "room digest" not in w.slack.health()["detail"]
    # any other failure is the room's, not the blocks': it raises, no text retry
    w.web.fail("chat.postMessage", SlackCallError("chat.postMessage", "not_in_channel"))
    with pytest.raises(ChannelSendError, match="not_in_channel"):
        await w.slack.post_room("digest", "Daily", blocks=[HEADER])
    assert len(w.web.posts) == 1


# --- approval buttons -------------------------------------------------------------------------


def team_gate(db: sqlite3.Connection) -> PolicyGate:
    registry = ToolRegistry()
    for spec in (WIPE, ADD, PAY):
        registry.register(spec)
    return PolicyGate(db, principals(), registry, dry_run=False, approval_ttl_s=1800)


async def ask(
    world: World, gate: PolicyGate, spec: ToolSpec, *, thread: str | None = None
) -> NeedsApproval:
    """Brian's task asks for ``spec``; the prompts go out over the Slack channel."""
    db = world.slack.conn
    ref = None
    if thread is not None:
        ref = {
            "channel": "slack",
            "principal": "brian",
            "identity": f"slack:{BRIAN}",
            "thread": thread,
            "ref": thread,
        }
    task_id = repo.insert_task(
        db, NewTask(kind="chat", runner="local", priority=1, principal="brian", channel_ref=ref)
    )
    d = gate.evaluate(Intent.for_tool(spec, task_id=task_id, principal="brian", args={"x": 1}))
    assert isinstance(d, NeedsApproval)
    repo.set_status(db, task_id, "waiting", wait_reason="approval")
    world.clock.now = utcnow()
    result = await prompt_approvers(db, world.slack, d, task_id=task_id, now=utcnow)
    assert set(result.reached) == {"justin", "brian"}
    return d


@pytest.fixture
async def gw(db: sqlite3.Connection) -> AsyncIterator[tuple[World, PolicyGate]]:
    gate = team_gate(db)
    world = make(db, gate=gate)
    await world.slack.start(submitter(world))
    yield world, gate
    await world.slack.stop()


async def test_prompts_carry_buttons_in_the_askers_thread(gw: tuple[World, PolicyGate]) -> None:
    # PER-320: an approver asked in a team channel's thread: one prompt, there; no DMs
    w, gate = gw
    await w.socket.deliver(mention(BRIAN, f"<@{BOT}> wipe it", ts="1700000000.000100"))
    d = await ask(w, gate, WIPE, thread="C0GENERAL:1700000000.000100")
    (brian,) = w.web.posts
    assert (brian.channel, brian.thread_ts) == ("C0GENERAL", "1700000000.000100")
    assert brian.buttons and "<@" not in brian.text  # an approver asked: nobody is tagged
    assert f"[{d.code}]" in brian.text and "This can't be undone" in brian.text  # L1 wording
    assert len(slack_store.prompts(w.slack.conn)) == 1
    row = ap.get_approval(w.slack.conn, d.approval_id)
    assert row is not None
    assert {(r.principal, r.ref, r.thread) for r in row.channel_refs} == {
        (p, f"C0GENERAL:{brian.ts}", "C0GENERAL:1700000000.000100") for p in ("justin", "brian")
    }
    # a repeat prompt is deduped
    assert row.task_id is not None
    await prompt_approvers(w.slack.conn, w.slack, d, task_id=row.task_id, now=utcnow)
    assert len(w.web.posts) == 1


async def test_another_approver_answers_the_thread_prompt_a_member_cannot(
    gw: tuple[World, PolicyGate],
) -> None:
    w, gate = gw
    await w.socket.deliver(mention(BRIAN, f"<@{BOT}> add it", ts="1700000000.000100"))
    d = await ask(w, gate, ADD, thread="C0GENERAL:1700000000.000100")
    (posted,) = w.web.posts
    w.clock.now = utcnow() + timedelta(seconds=1)
    await w.socket.deliver(
        click(MIU, d.approval_id, d.code, ts=posted.ts, thread_ts="1700000000.000100")
    )
    assert w.web.ephemerals[-1]["text"] == "Only Justin or Brian can approve this."
    await w.socket.deliver(
        click(JUSTIN, d.approval_id, d.code, ts=posted.ts, thread_ts="1700000000.000100",
              action_ts="1800000600.000001")
    )  # fmt: skip
    row = ap.get_approval(w.slack.conn, d.approval_id)
    assert row is not None and (row.status, row.decided_by) == ("approved", "justin")
    assert [u["ts"] for u in w.web.updates] == [posted.ts]
    assert "Approved by Justin" in w.web.updates[0]["text"]


async def test_a_typed_yes_from_another_approver_binds_the_thread_prompt(
    gw: tuple[World, PolicyGate],
) -> None:
    # the one copy counts as sent to every approver (the sent-before rule)
    w, gate = gw
    await w.socket.deliver(mention(BRIAN, f"<@{BOT}> add it", ts="1700000000.000100"))
    d = await ask(w, gate, ADD, thread="C0GENERAL:1700000000.000100")
    result = gate.resolve_reply(
        ApprovalReply(
            actor="justin", actor_identity=f"slack:{JUSTIN}", channel="slack",
            raw_text=f"yes {d.code}", decision="approve", path="parser",
            event_id="slack:C0GENERAL:1700000009.000100",
            occurred_at=utcnow() + timedelta(seconds=1), code=d.code,
        )
    )  # fmt: skip
    assert result.ok, result.reason


async def test_a_members_thread_request_tags_every_approver_and_dms_nobody(
    gw: tuple[World, PolicyGate],
) -> None:
    w, gate = gw
    await w.socket.deliver(mention(MIU, f"<@{BOT}> deploy it", ts="1700000000.000100"))
    task_id = repo.insert_task(
        w.slack.conn, NewTask(kind="chat", runner="local", priority=1, principal="brian")
    )
    d = gate.evaluate(Intent.for_tool(ADD, task_id=task_id, principal="brian", args={"x": 1}))
    assert isinstance(d, NeedsApproval)
    repo.set_status(w.slack.conn, task_id, "waiting", wait_reason="approval")
    thread = MessageRef("slack", "C0GENERAL:1700000000.000100", "C0GENERAL:1700000000.000100")
    result = await prompt_approvers(
        w.slack.conn, w.slack, d, task_id=task_id, now=utcnow, in_thread=("miu", thread)
    )
    assert set(result.reached) == {"justin", "brian"} and result.unreachable == ()
    (posted,) = w.web.posts
    assert (posted.channel, posted.thread_ts) == ("C0GENERAL", "1700000000.000100")
    assert posted.text.startswith(f"<@{JUSTIN}> <@{BRIAN}>\n[{d.code}]")
    assert posted.buttons
    row = ap.get_approval(w.slack.conn, d.approval_id)
    assert row is not None and {r.principal for r in row.channel_refs} == {"justin", "brian"}
    # the edited outcome drops the tags
    w.clock.now = utcnow() + timedelta(seconds=1)
    await w.socket.deliver(
        click(BRIAN, d.approval_id, d.code, ts=posted.ts, thread_ts="1700000000.000100")
    )
    assert "<@" not in w.web.updates[-1]["text"]


async def test_the_owners_own_dm_thread_keeps_a_copy_per_approver(
    gw: tuple[World, PolicyGate],
) -> None:
    # a 1:1 conversation isn't shared: each approver gets the prompt in their own, as before
    w, gate = gw
    await w.socket.deliver(
        message(BRIAN, "add it", channel="D2BRIAN", channel_type="im", ts="1700000000.000100")
    )
    d = await ask(w, gate, ADD, thread="D2BRIAN:1700000000.000100")
    by_channel = {p.channel: p for p in w.web.posts if p.buttons}
    assert set(by_channel) == {"D2BRIAN", "D1JUSTIN"}
    assert by_channel["D2BRIAN"].thread_ts == "1700000000.000100"
    assert by_channel["D1JUSTIN"].thread_ts is None
    row = ap.get_approval(w.slack.conn, d.approval_id)
    assert row is not None and all(r.thread is None for r in row.channel_refs)


async def test_a_thread_no_longer_followed_falls_back_to_each_approvers_dm(
    gw: tuple[World, PolicyGate],
) -> None:
    w, gate = gw  # no message in C0GENERAL:1700000000.000100: not a thread Cori posts in
    await ask(w, gate, ADD, thread="C0GENERAL:1700000000.000100")
    assert {p.channel for p in w.web.posts if p.buttons} == {"D2BRIAN", "D1JUSTIN"}


async def test_a_members_copy_that_fell_back_to_a_dm_tags_nobody(
    gw: tuple[World, PolicyGate],
) -> None:
    w, gate = gw  # not a thread the bot posts in: the member's copy lands in their DM
    task_id = repo.insert_task(
        w.slack.conn, NewTask(kind="chat", runner="local", priority=1, principal="brian")
    )
    d = gate.evaluate(Intent.for_tool(ADD, task_id=task_id, principal="brian", args={"x": 1}))
    assert isinstance(d, NeedsApproval)
    repo.set_status(w.slack.conn, task_id, "waiting", wait_reason="approval")
    thread = MessageRef("slack", "C0GENERAL:1700000000.000100", "C0GENERAL:1700000000.000100")
    await prompt_approvers(
        w.slack.conn, w.slack, d, task_id=task_id, now=utcnow, in_thread=("miu", thread)
    )
    assert w.web.posts and not any("<@" in p.text for p in w.web.posts)
    assert {p.channel for p in w.web.posts if p.buttons} >= {"D2BRIAN", "D1JUSTIN"}


async def test_a_thread_prompts_reminder_goes_once_in_the_thread(
    gw: tuple[World, PolicyGate],
) -> None:
    w, gate = gw
    await w.socket.deliver(mention(BRIAN, f"<@{BOT}> add it", ts="1700000000.000100"))
    d = await ask(w, gate, ADD, thread="C0GENERAL:1700000000.000100")
    n = len(w.web.posts)
    assert await remind_due(w.slack.conn, w.slack, d.expires_at - timedelta(minutes=10)) == 1
    (reminder,) = w.web.posts[n:]
    assert (reminder.channel, reminder.thread_ts) == ("C0GENERAL", "1700000000.000100")
    assert reminder.text.startswith(f"[{d.code}] still waiting")
    assert await remind_due(w.slack.conn, w.slack, d.expires_at - timedelta(minutes=9)) == 0


async def test_an_approvers_click_resolves_and_edits_every_prompt(
    gw: tuple[World, PolicyGate],
) -> None:
    w, gate = gw
    d = await ask(w, gate, WIPE)  # L1: one click is enough
    w.clock.now = utcnow() + timedelta(seconds=1)
    posted = next(p for p in w.web.posts if p.channel == "D2BRIAN")
    await w.socket.deliver(click(BRIAN, d.approval_id, d.code, channel="D2BRIAN", ts=posted.ts))
    row = ap.get_approval(w.slack.conn, d.approval_id)
    assert row is not None and (row.status, row.decided_by) == ("approved", "brian")
    assert {u["channel"] for u in w.web.updates} == {"D2BRIAN", "D1JUSTIN"}
    for u in w.web.updates:
        assert "Approved by Brian" in u["text"]
        assert not any(b["type"] == "actions" for b in u["blocks"])  # buttons removed
    assert slack_store.prompts(w.slack.conn) == []
    audit = w.slack.conn.execute(
        "SELECT json_extract(detail, '$.path') FROM audit_log WHERE action = 'approval.resolve'"
    ).fetchone()
    assert audit[0] == "button"
    # a second approver clicking the old message: already decided, nothing changes
    jpost = next(p for p in w.web.posts if p.channel == "D1JUSTIN")
    await w.socket.deliver(
        click(
            JUSTIN,
            d.approval_id,
            d.code,
            approve=False,
            channel="D1JUSTIN",
            ts=jpost.ts,
            action_ts="1800000600.000001",
        )
    )
    assert w.web.ephemerals[-1]["text"] == "Already approved by Brian."
    row = ap.get_approval(w.slack.conn, d.approval_id)
    assert row is not None and row.status == "approved"


async def test_a_non_approvers_click_is_refused_and_changes_nothing(
    gw: tuple[World, PolicyGate],
) -> None:
    w, gate = gw
    await w.socket.deliver(mention(BRIAN, f"<@{BOT}> wipe", ts="1700000000.000100"))
    d = await ask(w, gate, ADD, thread="C0GENERAL:1700000000.000100")
    posted = next(p for p in w.web.posts if p.channel == "C0GENERAL")
    w.clock.now = utcnow() + timedelta(seconds=1)
    for uid in (MIU, "U9STRANGER"):
        await w.socket.deliver(
            click(
                uid,
                d.approval_id,
                d.code,
                channel="C0GENERAL",
                ts=posted.ts,
                thread_ts="1700000000.000100",
            )
        )
        assert w.web.ephemerals[-1] == {
            "channel": "C0GENERAL",
            "user": uid,
            "text": "Only Justin or Brian can approve this.",
            "thread_ts": "1700000000.000100",
        }
    row = ap.get_approval(w.slack.conn, d.approval_id)
    assert row is not None and row.status == "pending" and w.web.updates == []
    # Brian clicks the same message in the thread: his click counts (an approver)
    await w.socket.deliver(
        click(
            BRIAN,
            d.approval_id,
            d.code,
            approve=False,
            channel="C0GENERAL",
            ts=posted.ts,
            action_ts="1800000700.000001",
        )
    )
    row = ap.get_approval(w.slack.conn, d.approval_id)
    assert row is not None and (row.status, row.decided_by) == ("rejected", "brian")
    assert all("Denied by Brian" in u["text"] for u in w.web.updates)


async def test_a_redelivered_click_is_one_decision(gw: tuple[World, PolicyGate]) -> None:
    w, gate = gw
    d = await ask(w, gate, ADD)
    w.clock.now = utcnow() + timedelta(seconds=1)
    posted = next(p for p in w.web.posts if p.channel == "D2BRIAN")
    env = click(BRIAN, d.approval_id, d.code, channel="D2BRIAN", ts=posted.ts)
    await w.socket.deliver(env)
    await w.socket.deliver({**env, "envelope_id": "again"})
    n = w.slack.conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'approval.resolve' "
        "AND json_extract(detail, '$.ok') = 1"
    ).fetchone()[0]
    assert n == 1


async def test_pay_gets_no_buttons_and_expired_prompts_lose_theirs(
    gw: tuple[World, PolicyGate],
) -> None:
    w, gate = gw
    pay = await ask(w, gate, PAY)
    assert not any(p.buttons for p in w.web.posts)
    # a forged click on a pay approval binds nothing
    w.clock.now = utcnow() + timedelta(seconds=1)
    await w.socket.deliver(click(BRIAN, pay.approval_id, pay.code, channel="D2BRIAN"))
    row = ap.get_approval(w.slack.conn, pay.approval_id)
    assert row is not None and row.status == "pending"
    assert w.web.ephemerals[-1]["text"] == f"Reply yes {pay.code} to confirm."
    add = await ask(w, gate, ADD)
    assert len(slack_store.prompts(w.slack.conn)) == 2
    w.clock.now = utcnow() + timedelta(hours=1)
    assert await w.slack.sweep_prompts() == 2
    assert all("Expired, not done" in u["text"] for u in w.web.updates)
    posted = next(p for p in w.web.posts if p.buttons)
    await w.socket.deliver(
        click(
            BRIAN,
            add.approval_id,
            add.code,
            channel=posted.channel,
            ts=posted.ts,
            action_ts="1800000800.000001",
        )
    )
    assert w.web.ephemerals[-1]["text"] == "That approval expired. Not done."


async def test_a_typed_answer_also_closes_the_buttons(gw: tuple[World, PolicyGate]) -> None:
    w, gate = gw
    d = await ask(w, gate, ADD)
    ok = gate.resolve(
        ApprovalResolution(
            approval_id=d.approval_id,
            approved=True,
            actor="justin",
            actor_identity="cli",
            path="cli",
        )
    )
    assert ok.ok
    assert await w.slack.sweep_prompts() == 2
    assert all("Approved by Justin" in u["text"] for u in w.web.updates)


async def test_the_gate_takes_buttons_only_from_slack_and_only_with_a_code(
    gw: tuple[World, PolicyGate],
) -> None:
    w, gate = gw
    d = await ask(w, gate, ADD)
    at = utcnow() + timedelta(seconds=1)
    with pytest.raises(ValueError, match="code"):
        ApprovalReply(
            actor="brian",
            actor_identity=f"slack:{BRIAN}",
            channel="slack",
            raw_text="button",
            decision="approve",
            path="button",
            event_id="e0",
            occurred_at=at,
        )
    forged = ApprovalReply(
        actor="brian",
        actor_identity="imessage:+15550000009",
        channel="imessage",
        raw_text="button",
        decision="approve",
        path="button",
        event_id="e1",
        occurred_at=at,
        code=d.code,
    )
    assert gate.resolve_reply(forged).reason == "unsupported"
    early = ApprovalReply(
        actor="brian",
        actor_identity=f"slack:{BRIAN}",
        channel="slack",
        raw_text="button",
        decision="approve",
        path="button",
        event_id="e2",
        occurred_at=utcnow() - timedelta(hours=1),
        code=d.code,
    )
    assert gate.resolve_reply(early).reason == "not_sent_before"
    row = ap.get_approval(w.slack.conn, d.approval_id)
    assert row is not None and row.status == "pending"


async def test_an_event_without_team_id_is_never_vouched(w: World) -> None:
    env = mention(BRIAN, f"<@{BOT}> hi")
    del env["payload"]["team_id"]
    await w.socket.deliver(env)
    assert w.events == []


async def test_unknown_senders_are_audited_once_an_hour(db: sqlite3.Connection) -> None:
    world = make(db)
    await world.slack.start(submitter(world))
    for i in range(3):
        await world.socket.deliver(mention("U9STRANGER", f"<@{BOT}> {i}", ts=f"17000000{i}.1"))
    n = "SELECT COUNT(*) FROM audit_log WHERE action = 'slack.unknown_sender'"
    assert db.execute(n).fetchone()[0] == 1
    world.clock.now += timedelta(hours=2)
    await world.socket.deliver(mention("U9STRANGER", f"<@{BOT}> later", ts="1700000009.1"))
    assert db.execute(n).fetchone()[0] == 2
    await world.slack.stop()


async def test_a_click_must_match_the_posted_approval_and_code(
    gw: tuple[World, PolicyGate],
) -> None:
    w, gate = gw
    first = await ask(w, gate, ADD)
    second = await ask(w, gate, WIPE)
    w.clock.now = utcnow() + timedelta(seconds=1)
    posted = next(p for p in w.web.posts if p.channel == "D2BRIAN" and first.code in p.text)
    # the first prompt's message, carrying the second approval's code: binds nothing
    await w.socket.deliver(
        click(BRIAN, first.approval_id, second.code, channel="D2BRIAN", ts=posted.ts)
    )
    await w.socket.deliver(
        click(
            BRIAN,
            second.approval_id,
            second.code,
            channel="D2BRIAN",
            ts=posted.ts,
            action_ts="1800000900.000001",
        )
    )
    for d in (first, second):
        row = ap.get_approval(w.slack.conn, d.approval_id)
        assert row is not None and row.status == "pending"
    assert w.web.ephemerals[-1]["text"] == "Nothing is waiting for approval."


def test_brain_history_follows_the_conversations_channel(db: sqlite3.Connection) -> None:
    from resonant.brain.history import channel_of
    from resonant.runners.ask_tools import OWNER_SOURCES
    from resonant.store.tasks import insert_event

    slack_event = Event(
        source="slack",
        type="message",
        principal="brian",
        payload={"text": "hi"},
        dedupe_key="slack:C1:1.1",
    )
    other = Event(source="gate", type="human_input", payload={}, dedupe_key="g:1")
    insert_event(db, slack_event)
    insert_event(db, other)
    assert channel_of(db, slack_event.id) == "slack"
    assert channel_of(db, other.id) == "imessage" and channel_of(db, None) == "imessage"
    assert channel_of(db, "missing") == "imessage"
    assert {"imessage", "slack"} <= OWNER_SOURCES


def test_shared_thread_is_a_team_channels_thread_only() -> None:
    assert shared_thread(MessageRef("slack", "C0GENERAL:1.2", "C0GENERAL:1.1"))
    assert shared_thread(MessageRef("slack", "G0PRIVATE:1.2", "G0PRIVATE:1.1"))
    assert not shared_thread(MessageRef("slack", "D1JUSTIN:1.2", "D1JUSTIN:1.1"))  # a DM
    assert not shared_thread(MessageRef("slack", "C0GENERAL:1.2"))  # not in a thread
    assert not shared_thread(MessageRef("imessage", "guid"))
    assert not shared_thread(None)

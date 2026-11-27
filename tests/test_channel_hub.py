"""PER-292: the channel hub (``gateway/hub.py``), threads and identities
(``gateway/channel.py``), and the parts of the router, approvals, relay, monitor, config and
daemon that stopped assuming iMessage."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from resonant.config import load_settings
from resonant.daemon import Daemon, started_line
from resonant.e2e import FakeChannel
from resonant.gate import DryRunGate
from resonant.gateway.channel import (
    ApprovalPrompt,
    ChannelRefused,
    MessageRef,
    event_identity,
    thread_of,
)
from resonant.gateway.hub import ChannelHub
from resonant.loop.types import Task
from resonant.models import ProbeResult
from resonant.monitor import HealthMonitor, channels_snapshot, health_snapshot
from resonant.principals import Principal, Principals
from resonant.router import Router
from resonant.router.approvals import ApprovalInbox
from resonant.router.relay import Relay
from resonant.tools import ToolRegistry
from resonant.tools.builtin import BUILTIN_SPECS
from resonant_sdk import Event

OWNER = "alice"
MEMBER = "ana"
PHONE = "+15550000000"


@pytest.fixture
def principals() -> Principals:
    return Principals(
        principals={
            # principals.yaml order decides the default channel: Slack first for the owner here
            OWNER: Principal(role="owner", identities=("slack:U1", f"imessage:{PHONE}")),
            MEMBER: Principal(identities=("slack:U2",)),
        }
    )


def fakes(p: Principals, **kw: Any) -> tuple[FakeChannel, FakeChannel]:
    im = FakeChannel("imessage", p, threaded=False, **kw.get("imessage", {}))
    sl = FakeChannel("slack", p, **kw.get("slack", {}))
    return im, sl


def audits(db: sqlite3.Connection, action: str) -> list[dict[str, Any]]:
    rows = db.execute("SELECT detail FROM audit_log WHERE action = ?", (action,)).fetchall()
    return [json.loads(r[0] or "{}") for r in rows]


def prompt(thread: MessageRef | None = None) -> ApprovalPrompt:
    return ApprovalPrompt(
        approval_id="ap1",
        code="K4",
        summary="restart Resonant",
        effect="write",
        level="L2",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        thread=thread,
    )


def no_submit(_e: Event) -> bool:
    return True


# --- the hub: which adapter sends ----------------------------------------------------------


async def test_send_without_a_thread_uses_the_first_identity_on_a_running_adapter(
    db: sqlite3.Connection, principals: Principals
) -> None:
    im, sl = fakes(principals)
    hub = ChannelHub({"imessage": im, "slack": sl}, principals, db)
    await hub.send(OWNER, "hi", dedupe_key="k1")
    assert sl.texts == ["hi"] and im.texts == []  # slack is the owner's first identity
    assert hub.name == "imessage+slack" and hub.names == ("imessage", "slack")


async def test_send_with_a_thread_goes_to_that_channel_and_thread(
    db: sqlite3.Connection, principals: Principals
) -> None:
    im, sl = fakes(principals)
    hub = ChannelHub({"imessage": im, "slack": sl}, principals, db)
    await hub.send(OWNER, "in thread", thread=MessageRef("slack", "s:1", "T1"))
    assert sl.sent[0].thread == "T1"
    await hub.send(OWNER, "on imessage", thread=MessageRef("imessage", "G1", "x"))
    assert im.texts == ["on imessage"] and im.sent[0].thread is None  # unthreaded ignores it


async def test_a_thread_on_a_failed_adapter_falls_back_and_drops_the_thread(
    db: sqlite3.Connection, principals: Principals
) -> None:
    im, sl = fakes(principals, slack={"fail_start": True})
    hub = ChannelHub({"imessage": im, "slack": sl}, principals, db)
    await hub.start(no_submit)
    assert hub.running == ("imessage",) and "can't connect" in hub.failures["slack"]
    [failed] = audits(db, "channel.start_failed")
    assert failed["channel"] == "slack"
    await hub.send(OWNER, "fallback", thread=MessageRef("slack", "s:1", "T1"))
    assert im.texts == ["fallback"] and sl.texts == []


async def test_no_identity_on_a_running_adapter_is_refused_and_audited(
    db: sqlite3.Connection, principals: Principals
) -> None:
    im, _ = fakes(principals)
    hub = ChannelHub({"imessage": im}, principals, db)
    with pytest.raises(ChannelRefused):
        await hub.send(MEMBER, "hello")  # ana has only a Slack identity
    with pytest.raises(ChannelRefused):
        await hub.send("nobody", "hello")
    reasons = [a["reason"] for a in audits(db, "channel.send_refused")]
    assert reasons == ["principal has no identity on a running channel", "unknown principal"]
    assert im.texts == []
    assert "hello" not in json.dumps(audits(db, "channel.send_refused"))  # never the body


async def test_request_approval_follows_the_prompts_thread(
    db: sqlite3.Connection, principals: Principals
) -> None:
    im, sl = fakes(principals)
    hub = ChannelHub({"imessage": im, "slack": sl}, principals, db)
    await hub.request_approval(OWNER, prompt(MessageRef("slack", "s:1", "T1")))
    assert sl.sent[0].thread == "T1" and sl.sent[0].text.startswith("[K4]")
    # deduped by the approval key, like every adapter
    await hub.request_approval(OWNER, prompt(MessageRef("slack", "s:1", "T1")))
    assert len(sl.sent) == 1


async def test_request_approval_thread_from_another_channel_is_dropped(
    db: sqlite3.Connection,
) -> None:
    p = Principals(principals={OWNER: Principal(role="owner", identities=(f"imessage:{PHONE}",))})
    im, sl = fakes(p)
    hub = ChannelHub({"imessage": im, "slack": sl}, p, db)
    await hub.request_approval(OWNER, prompt(MessageRef("slack", "s:1", "T1")))
    assert im.prompts[0].thread is None and sl.sent == []


# --- the hub: lifecycle and health -----------------------------------------------------------


async def test_start_raises_only_when_no_adapter_starts(
    db: sqlite3.Connection, principals: Principals
) -> None:
    im = FakeChannel("imessage", principals, fail_start=True)
    hub = ChannelHub({"imessage": im}, principals, db)
    with pytest.raises(RuntimeError, match="can't connect"):
        await hub.start(no_submit)  # iMessage-only: the daemon still exits 1


async def test_stop_stops_started_adapters_and_sends_still_work_after(
    db: sqlite3.Connection, principals: Principals
) -> None:
    im, sl = fakes(principals)
    hub = ChannelHub({"imessage": im, "slack": sl}, principals, db)
    await hub.start(no_submit)
    await hub.stop()
    await hub.stop()  # twice is fine
    assert not sl.inject("U1", "after stop")  # no new events
    await hub.send(OWNER, "drain reply")  # the loop drains after the readers stop
    assert sl.texts == ["drain reply"]


async def test_health_aggregates_and_a_single_adapter_keeps_its_own_keys(
    db: sqlite3.Connection, principals: Principals
) -> None:
    im, sl = fakes(principals, slack={"fail_start": True})
    hub = ChannelHub({"imessage": im, "slack": sl}, principals, db)
    await hub.start(no_submit)
    h = hub.health()
    assert h["ok"] is False and h["last_read_at"] is not None
    assert h["last_error"].startswith("not running:") and set(h["adapters"]) == {
        "imessage",
        "slack",
    }

    class WithFda(FakeChannel):
        def health(self) -> dict[str, Any]:
            return {**super().health(), "fda_ok": True}

    solo = ChannelHub({"imessage": WithFda("imessage", principals)}, principals, db)
    assert solo.health()["fda_ok"] is True


async def test_self_test_runs_per_adapter(db: sqlite3.Connection, principals: Principals) -> None:
    im, sl = fakes(principals, slack={"fail_selftest": True})
    hub = ChannelHub({"imessage": im, "slack": sl}, principals, db)
    r = await hub.self_test(timeout_s=1)
    assert not r.ok and r.detail == "imessage: ok; slack: no round trip"
    assert im.selftests == sl.selftests == 1
    solo = ChannelHub({"imessage": im}, principals, db)
    assert (await solo.self_test()).detail == "ok"


def test_hub_needs_adapters_with_matching_names(
    db: sqlite3.Connection, principals: Principals
) -> None:
    with pytest.raises(ValueError, match="at least one"):
        ChannelHub({}, principals, db)
    with pytest.raises(ValueError, match="registered as"):
        ChannelHub({"imessage": FakeChannel("slack", principals)}, principals, db)


# --- threads and identities ------------------------------------------------------------------


def task_with(ref: dict[str, Any] | None) -> Task:
    now = datetime(2026, 11, 20, tzinfo=UTC)
    return Task(
        id="t1",
        kind="chat",
        runner="brain",
        status="running",
        priority=0,
        trace_id="tr1",
        created_at=now,
        updated_at=now,
        channel_ref=ref,
    )


def test_thread_of_old_rows_and_imessage_is_none() -> None:
    old = {
        "channel": "imessage",
        "principal": OWNER,
        "handle": PHONE,
        "chat_guid": "c",
        "guid": "G",
    }
    assert thread_of(task_with(old)) is None  # written before PER-292
    assert thread_of(task_with(None)) is None
    assert thread_of(None) is None
    new_imessage = {**old, "identity": f"imessage:{PHONE}", "thread": None, "ref": "G"}
    assert thread_of(new_imessage) is None


def test_thread_of_a_threaded_channel() -> None:
    ref = {"channel": "slack", "principal": OWNER, "identity": "slack:U1", "thread": "T1",
           "ref": "s:9"}  # fmt: skip
    assert thread_of(task_with(ref)) == MessageRef("slack", "s:9", "T1")
    assert thread_of(task_with(ref), to=OWNER) == MessageRef("slack", "s:9", "T1")
    assert thread_of(task_with(ref), to=MEMBER) is None  # never into another's thread


async def test_an_old_row_still_sends(db: sqlite3.Connection, principals: Principals) -> None:
    im, sl = fakes(principals)
    hub = ChannelHub({"imessage": im, "slack": sl}, principals, db)
    old = {
        "channel": "imessage",
        "principal": OWNER,
        "handle": PHONE,
        "chat_guid": "c",
        "guid": "G",
    }
    await hub.send(OWNER, "reply", thread=thread_of(task_with(old)))
    assert sl.texts == ["reply"]  # the owner's first running identity


def test_event_identity() -> None:
    assert event_identity(Event(source="slack", type="message", payload={"identity": "slack:U1"}))
    legacy = Event(source="imessage", type="message", payload={"handle": PHONE})
    assert event_identity(legacy) == f"imessage:{PHONE}"
    assert event_identity(Event(source="slack", type="message", payload={"handle": "x"})) is None
    # an identity from another channel than the event's source is not trusted
    spoof = Event(source="slack", type="message", payload={"identity": f"imessage:{PHONE}"})
    assert event_identity(spoof) is None


# --- router, approvals and relay on any channel ------------------------------------------------


def router(db: sqlite3.Connection, p: Principals, channels: tuple[str, ...]) -> Router:
    reg = ToolRegistry()
    for spec in BUILTIN_SPECS:
        reg.register(spec)
    return Router(
        db,
        principals=p,
        registry=reg,
        gate=DryRunGate(db, p, reg),
        handlers={},
        model=None,  # pyright: ignore[reportArgumentType]  # never called on these paths
        channels=channels,
    )


def slack_msg(text: str, uid: str = "U1", principal: str = OWNER) -> Event:
    return Event(
        source="slack",
        type="message",
        principal=principal,
        payload={"text": text, "identity": f"slack:{uid}", "ref": "s:5", "thread": "T5"},
    )


async def test_router_routes_messages_from_every_channel_in_the_hub(
    db: sqlite3.Connection, principals: Principals
) -> None:
    task = await router(db, principals, ("imessage", "slack")).route(slack_msg("/approvals"))
    assert task is not None and task.checkpoint["fast_reply"] == "Nothing is waiting for approval."
    assert task.channel_ref == {"channel": "slack", "principal": OWNER, "identity": "slack:U1",
                                "thread": "T5", "ref": "s:5"}  # fmt: skip
    # a channel the hub doesn't have is not routed (iMessage-only by default)
    assert await router(db, principals, ("imessage",)).route(slack_msg("/approvals")) is None


async def test_approval_reply_needs_the_events_identity_to_be_the_principal(
    db: sqlite3.Connection, principals: Principals
) -> None:
    reg = ToolRegistry()
    inbox = ApprovalInbox(db, principals=principals, gate=DryRunGate(db, principals, reg))
    # ana's identity on an event claiming to be alice's: refused, nothing revealed
    spoofed = slack_msg("yes K4", uid="U2", principal=OWNER)
    result = inbox.handle(spoofed, "yes K4", OWNER)
    assert result is not None and result.outcome == "unauthenticated"
    nameless = Event(source="slack", type="message", principal=OWNER, payload={"text": "yes"})
    result = inbox.handle(nameless, "yes", OWNER)
    assert result is not None and result.reason == "identity_mismatch"


def test_relay_authenticates_by_identity_on_any_channel(
    db: sqlite3.Connection, principals: Principals
) -> None:
    relay = Relay(db, principals=principals, model=None)  # pyright: ignore[reportArgumentType]
    assert relay._authentic(slack_msg("R4 ok"), OWNER)  # pyright: ignore[reportPrivateUsage]
    spoofed = slack_msg("R4 ok", uid="U2")
    assert not relay._authentic(spoofed, OWNER)  # pyright: ignore[reportPrivateUsage]


# --- monitor: one check per channel ------------------------------------------------------------


class Model:
    async def probe(self) -> ProbeResult:
        return ProbeResult(True, True, True, True, 5, "ok")


async def test_monitor_checks_each_channel_and_alerts_through_a_healthy_one(
    db: sqlite3.Connection, principals: Principals
) -> None:
    im, sl = fakes(principals, imessage={"fail_start": True})
    hub = ChannelHub({"imessage": im, "slack": sl}, principals, db)
    await hub.start(no_submit)
    now = [datetime(2026, 11, 20, 12, tzinfo=UTC)]
    monitor = HealthMonitor(
        db,
        model=Model(),
        channel=hub,
        channels={"imessage": im, "slack": sl},
        channel_failures=lambda: hub.failures,
        owner=OWNER,
        daemon_healthy=lambda: True,
        clock=lambda: now[0],
    )
    await monitor.evaluate()
    now[0] += timedelta(minutes=1)
    states = await monitor.evaluate()
    assert states["imessage"].state == "degraded"
    assert states["imessage"].detail is not None
    assert states["imessage"].detail.startswith("imessage not running:")
    assert states["slack"].state == "ok"
    # iMessage is down but Slack is up: the owner is texted there, no dead-man reason
    assert monitor.deadman_reason() is None
    assert any(t.startswith("hey, some issues here: imessage") for t in sl.texts)
    assert health_snapshot(db)["slack"]["state"] == "ok"


async def test_monitor_slack_selftest_failure_degrades_its_check(
    db: sqlite3.Connection, principals: Principals
) -> None:
    sl = FakeChannel("slack", principals, fail_selftest=True)
    hub = ChannelHub({"slack": sl}, principals, db)
    await hub.start(no_submit)
    monitor = HealthMonitor(
        db,
        model=None,
        channel=hub,
        channels={"slack": sl},
        owner=OWNER,
        daemon_healthy=lambda: True,
    )
    states = await monitor.evaluate()
    assert states["slack"].state == "degraded" and "imessage" not in monitor.channels
    assert states["imessage"].state == "unknown"  # iMessage off: no check
    states = await monitor.evaluate()
    assert monitor.deadman_reason() == "slack: slack selftest failed: no round trip"


def test_channels_snapshot_lists_other_adapters(
    db: sqlite3.Connection, principals: Principals
) -> None:
    sl = FakeChannel("slack", principals)
    rows = channels_snapshot(db, imessage_enabled=False, channel=None, others={"slack": sl})
    assert [r["name"] for r in rows] == ["imessage", "slack"]
    assert rows[0]["enabled"] is False and rows[1]["enabled"] is True


# --- config and daemon ------------------------------------------------------------------------


def test_channels_config_enabled() -> None:
    assert load_settings().channels.enabled() == ()
    s = load_settings(channels={"imessage": {"enabled": True}})
    assert s.channels.enabled() == ("imessage",)


def test_daemon_with_only_an_injected_channel_builds_the_stack(principals: Principals) -> None:
    settings = load_settings()
    fake = FakeChannel("slack", principals)
    daemon = Daemon(settings, channels={"slack": fake})
    try:
        assert daemon.stack is not None and daemon.imessage is daemon.stack
        assert daemon.stack.channel.names == ("slack",)
        assert daemon.loop.router is not None
        assert {"local", "brain", "claude", "reminder", "job"} <= set(daemon.loop.runners)
        assert daemon.state.monitor is not None
        assert set(daemon.state.monitor.channels) == {"slack"}
        assert daemon.stack.router.channels == frozenset({"slack"})
    finally:
        daemon.conn.close()
    line = started_line(settings, imessage=False, relay=False, extensions=0, jobs=0)
    assert "imessage=off" in line


def test_daemon_with_no_channel_is_phase0() -> None:
    daemon = Daemon(load_settings())
    try:
        assert daemon.stack is None and daemon.imessage is None
    finally:
        daemon.conn.close()

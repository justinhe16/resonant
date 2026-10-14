"""Pulse acceptance (docs/pulse-plan.md section 5, U1-U9): the daemon's parts, in process.

Same harness style as ``test_e2e_phase3``: chat.db is a FakeChatDB, osascript a
RecordingOsaRunner, and the real ``Daemon`` is built (not run). Time is a fake clock only:
the scheduler (``core/pulse``, ``core/briefing``), the ingest and the notifier read it, and
every date is explicit (2031, far from any real clock). A report is an ``ext.<name>.item``
event inserted exactly as ``persist_events`` stores one, stamped with the fake time, and
routed by the real loop (``wiring.route_event``) to the real ``Pulse``. Texts are the
RecordingOsaRunner's sends over the real iMessage channel (its dedupe records included).

- U1: ``test_u1_*``  U2: ``test_u2_*``  U3: ``test_u3_*``  U4: ``test_u4_*``
- U5: ``test_u5_*``  U6: ``test_u6_*``  U7: ``test_u7_*``  U8: ``test_u8_*``  U9: ``test_u9_*``
- crash between send and told, both paths: ``test_crash_*``
- the commands over iMessage with the daemon running: ``test_pulse_commands_over_imessage``
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import socket
import sqlite3
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from resonant import killswitch
from resonant.config import Settings, load_settings
from resonant.daemon import Daemon
from resonant.e2e import RecordingOsaRunner, ScriptedModel, wait_until
from resonant.e2e.extensions import install_fixture
from resonant.extensions import briefing, pulse_job
from resonant.extensions.notify import mute
from resonant.extensions.scheduler import LAST_TICK_KEY, Scheduler
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop import NewTask
from resonant.loop.types import PRIORITY_OWNER
from resonant.ops.fakes import FakeOpsRunner
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store import approvals as ap
from resonant.store import pulse as items
from resonant.store import tasks as repo
from resonant.store.db import iso, kv_set, open_db
from resonant.store.tasks import insert_event
from resonant_sdk import Effect, Event, Level

OWNER_PHONE = "+15551234567"
SELF = "resonant.agent@icloud.com"
TZ = "America/Los_Angeles"
LA = ZoneInfo(TZ)
PRINCIPALS = f"""\
principals:
  justin:
    role: owner
    identities: ["imessage:{OWNER_PHONE}"]
"""


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """``hour:minute`` local (Los Angeles) on 2031-06-<day>, as UTC. The 10th is a Tuesday."""
    return datetime(2031, 6, day, hour, minute, tzinfo=LA).astimezone(UTC)


def manifest(name: str, grant: str | None) -> str:
    report = "" if grant is None else f"report: {{max_urgency: {grant}}}\n"
    return f"name: {name}\nversion: 1\n{report}"


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_settings(home: Path, chat: FakeChatDB, **overrides: Any) -> Settings:
    (home / "principals.yaml").write_text(PRINCIPALS)
    settings = load_settings(
        dry_run=False,
        api={"port": free_port()},
        timezone=TZ,
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
        **overrides,
    )
    conn = open_db(settings.db_path)
    try:
        for name, grant in (("dori", "urgent"), ("trader", "normal"), ("rogue", None)):
            install_fixture(settings, conn, manifest(name, grant), {"cli.py": "# stub\n"})
    finally:
        conn.close()
    return settings


class World:
    """One daemon (restartable) on a fake clock, with the scheduler's core jobs on that clock."""

    def __init__(self, settings: Settings, osa: RecordingOsaRunner, clock: Clock) -> None:
        self.settings, self.osa, self.clock = settings, osa, clock
        self._seq = 0
        self.d = self._start()

    def _start(self) -> Daemon:
        d = Daemon(
            self.settings,
            model=ScriptedModel(),
            osa_runner=self.osa,
            ops_runner=FakeOpsRunner(),
            secrets=SecretStore(FakeKeychain()),
        )
        assert d.imessage is not None and d.imessage.notifier is not None
        assert d.imessage.pulse is not None and d.state.components is not None
        d.imessage.pulse.clock = self.clock
        d.imessage.notifier.clock = self.clock
        d.scheduler = Scheduler.from_components(
            self.settings,
            d.conn,
            d.state.components,
            started_at=self.clock.now,
            channel=d.imessage.channel,
            core_jobs=(
                functools.partial(briefing.briefing_job, clock=self.clock),
                functools.partial(pulse_job.pulse_job, clock=self.clock),
            ),
            clock=self.clock,
        )
        d.loop.scheduler = d.scheduler
        kv_set(d.conn, "paused", False)
        return d

    @property
    def conn(self) -> sqlite3.Connection:
        return self.d.conn

    def texts(self) -> list[str]:
        return [m.text for m in self.osa.sent]

    def restart(self, now: datetime) -> None:
        """The daemon dies and starts again (same store, same channel dedupe records)."""
        self.d.conn.close()
        self.clock.now = now
        self.d = self._start()

    def put(
        self,
        ext: str,
        key: str,
        title: str,
        *,
        urgency: str = "normal",
        state: dict[str, Any] | None = None,
        clear: bool = False,
        ttl: str = "24h",
        when: datetime | None = None,
    ) -> Event:
        """A ``report()`` stored the way ``scheduler.persist_events`` stores it."""
        when = when or self.clock.now
        self._seq += 1  # every report() has a unique dedupe key; the pulse decides what is news
        data = {"key": key, "urgency": urgency, "state": state, "clear": clear, "ttl": ttl}
        event = Event(
            source=f"ext:{ext}",
            type=f"ext.{ext}.item",
            payload={"text": title, "data": data},
            dedupe_key=f"ext:{ext}:item:{key}:{self._seq}",
            occurred_at=when,
        )
        assert insert_event(self.conn, event)
        return event

    async def report(self, ext: str, key: str, title: str, **kw: Any) -> Event:
        """Report at the current fake time and let the loop route it."""
        event = self.put(ext, key, title, **kw)
        await self.d.loop.run_once()
        return event

    async def tick(self, when: datetime) -> None:
        """Move the clock and run the scheduler (the pulse, the briefing), then the loop."""
        self.clock.now = when
        await self.d.scheduler.tick(when)
        await self.d.scheduler.drain()
        await self.d.loop.run_once()

    def unrouted(self) -> list[Event]:
        return repo.unrouted_events(self.conn)

    def audits(self, action: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM audit_log WHERE action = ?", (action,)).fetchall()

    def item(self, ext: str, key: str) -> items.Item:
        found = items.get(self.conn, ext, key)
        assert found is not None
        return found


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[FakeChatDB]:
    db = FakeChatDB(tmp_path / "chat.db")
    yield db
    db.close()


@contextlib.asynccontextmanager
async def world(
    home: Path, chat: FakeChatDB, start: datetime, **overrides: Any
) -> AsyncGenerator[World]:
    w = World(make_settings(home, chat, **overrides), RecordingOsaRunner(), Clock(start))
    try:
        yield w
    finally:
        w.d.conn.close()


# --- U1: urgent at 03:00 ---------------------------------------------------------------------


async def test_u1_urgent_at_3am_is_texted_once_even_across_a_restart(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    async with world(resonant_home, chat, at(10, 3, 0)) as w:
        await w.report("dori", "incident/412", "Dori failing: triaging", urgency="urgent")
        assert [(m.handle, m.text) for m in w.osa.sent] == [
            (OWNER_PHONE, "[dori] \U0001f534 Dori failing: triaging")
        ]
        assert not w.item("dori", "incident/412").changed  # told
        # The scheduler keeps ticking through the quiet hours: nothing more.
        for hour in (4, 5):
            await w.tick(at(10, hour))
        # The daemon restarts and the extension reports the same thing again.
        w.restart(at(10, 5, 10))
        await w.report("dori", "incident/412", "Dori failing: triaging", urgency="urgent")
        await w.tick(at(10, 6))  # the 06:00 pulse has nothing new either
        assert w.texts() == ["[dori] \U0001f534 Dori failing: triaging"]


# --- U2: normal at 03:00 ---------------------------------------------------------------------


async def test_u2_normal_at_3am_is_held_to_the_6am_pulse(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    async with world(resonant_home, chat, at(10, 3, 0)) as w:
        await w.report("trader", "pnl/today", "PnL -$1.8k today", state={"bucket": -2})
        for hour in (3, 4, 5):
            await w.tick(at(10, hour))
        assert w.texts() == []  # quiet hours: only urgent goes out
        await w.tick(at(10, 6))
        assert w.texts() == ["btw:\n[trader] PnL -$1.8k today"]
        await w.tick(at(10, 7))
        assert len(w.texts()) == 1


# --- U3: same state told once; A -> B -> A told three times ----------------------------------


async def test_u3_same_state_every_5_minutes_is_told_once(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    async with world(resonant_home, chat, at(10, 9, 0)) as w:
        for minute in range(5, 60, 5):
            w.clock.now = at(10, 9, minute)
            await w.report("trader", "pnl/today", "PnL -$1.8k today", state={"bucket": -2})
        await w.tick(at(10, 10))
        assert w.texts() == ["btw:\n[trader] PnL -$1.8k today"]
        for minute in range(5, 60, 5):
            w.clock.now = at(10, 10, minute)
            await w.report("trader", "pnl/today", "PnL -$1.8k today", state={"bucket": -2})
        await w.tick(at(10, 11))
        assert len(w.texts()) == 1


async def test_u3_a_b_a_is_told_three_times(resonant_home: Path, chat: FakeChatDB) -> None:
    async with world(resonant_home, chat, at(10, 9, 0)) as w:
        for step, state in enumerate(("A", "B", "A")):
            w.clock.now = at(10, 9 + step, 5)
            await w.report("dori", "svc/api", "API check", urgency="urgent", state={"s": state})
            await w.report("dori", "svc/api", "API check", urgency="urgent", state={"s": state})
        assert w.texts() == ["[dori] \U0001f534 API check"] * 3
    async with world(resonant_home, chat, at(10, 9, 0)) as w:  # and for a normal item, by pulse
        for step, state in enumerate(("A", "B", "A")):
            w.clock.now = at(10, 9 + step, 5)
            await w.report("trader", "pnl", "PnL moved", state={"s": state})
            await w.tick(at(10, 10 + step))
        assert w.texts() == ["btw:\n[trader] PnL moved"] * 3


# --- U4: nothing changed -> nothing sent ------------------------------------------------------


async def test_u4_a_quiet_hour_sends_nothing(resonant_home: Path, chat: FakeChatDB) -> None:
    async with world(resonant_home, chat, at(10, 9, 0)) as w:
        for hour in (9, 10, 11, 12):
            await w.tick(at(10, hour))
        assert w.texts() == []
        await w.report("trader", "pnl", "PnL +$1k", state={"b": 1})
        await w.tick(at(10, 13))
        for hour in (14, 15):
            await w.tick(at(10, hour))
        assert w.texts() == ["btw:\n[trader] PnL +$1k"]
        assert [json.loads(r["detail"])["job"] for r in w.audits("job.run")].count(
            "core/pulse"
        ) == 7  # it ran every hour; only one had news


# --- U5: 08:00 -------------------------------------------------------------------------------


async def test_u5_briefing_always_sends_with_updates_and_the_8am_pulse_does_not(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    async with world(resonant_home, chat, at(10, 6, 0)) as w:
        await w.tick(at(10, 7))
        await w.report("trader", "pnl", "PnL +$1k", state={"b": 1}, when=at(10, 7, 30))
        await w.report("dori", "digest", "3 PRs open", urgency="low", when=at(10, 7, 31))
        await w.tick(at(10, 8))
        # One message at 08:00: the briefing, with the Updates section; no "btw".
        (text,) = w.texts()
        assert "Updates:" in text
        assert "[trader] PnL +$1k" in text and "[dori] 3 PRs open" in text
        assert not text.startswith("btw:")
        assert not w.item("trader", "pnl").changed and not w.item("dori", "digest").changed
        await w.tick(at(10, 9))
        assert len(w.texts()) == 1  # told in the briefing: not again
        # Another day with nothing to say: the briefing still sends.
        await w.tick(at(11, 8))
        assert w.texts()[1:] == ["Nothing on today."]


# --- U6: paused ------------------------------------------------------------------------------


async def test_u6_paused_sends_nothing_and_resume_carries_changes_once(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    async with world(resonant_home, chat, at(10, 9, 0)) as w:
        killswitch.engage(w.conn, actor="justin")
        await w.report("dori", "inc", "Dori failing", urgency="urgent")
        await w.report("trader", "pnl", "PnL -$2k", state={"b": -2})
        await w.tick(at(10, 10))
        await w.tick(at(10, 11))
        assert w.texts() == []
        assert w.item("dori", "inc").changed and w.item("trader", "pnl").changed
        killswitch.release(w.conn, actor="justin")
        await w.tick(at(10, 12))
        assert w.texts() == ["btw:\n[dori] \U0001f534 Dori failing\n[trader] PnL -$2k"]
        await w.tick(at(10, 13))
        assert len(w.texts()) == 1


# --- U7: grant -------------------------------------------------------------------------------


async def test_u7_a_normal_grant_clamps_urgent_and_no_grant_is_refused(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    async with world(resonant_home, chat, at(10, 9, 0)) as w:
        await w.report("trader", "halt", "Trading halted", urgency="urgent")
        await w.report("rogue", "x", "buy now", urgency="urgent")
        assert w.texts() == [] and w.unrouted() == []
        assert w.item("trader", "halt").urgency == "normal"
        assert items.get(w.conn, "rogue", "x") is None
        (refused,) = w.audits("pulse.refused")
        assert "no report grant" in refused["detail"] and refused["extension"] == "rogue"
        await w.tick(at(10, 10))
        assert w.texts() == ["btw:\n[trader] Trading halted"]  # waited for the pulse, no marker


# --- U8: muted -------------------------------------------------------------------------------


async def test_u8_muted_items_are_skipped_then_told_once_after_the_mute_if_still_open(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    async with world(resonant_home, chat, at(10, 9, 0)) as w:
        mute(w.conn, "dori", at(10, 12, 0), by="justin")
        await w.report("dori", "inc", "Dori failing", urgency="urgent")
        await w.report("dori", "short", "Brief blip", ttl="1h")
        await w.report("trader", "pnl", "PnL +$1k", state={"b": 1})
        await w.tick(at(10, 10))
        await w.tick(at(10, 11))
        # Only the unmuted source spoke; dori's items stay untold.
        assert w.texts() == ["btw:\n[trader] PnL +$1k"]
        assert w.item("dori", "inc").changed
        # The short item expired inside the mute: it is never said. The long one, still open
        # and still unheard, rides the first pulse after the mute, exactly once.
        await w.tick(at(10, 12))
        assert w.texts()[1:] == ["btw:\n[dori] \U0001f534 Dori failing"]
        for hour in (13, 14):
            await w.tick(at(10, hour))
        assert len(w.texts()) == 2


# --- U9: approval re-mention -----------------------------------------------------------------


async def test_u9_a_waiting_approval_is_mentioned_once_before_it_expires(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    # Defaults: re-mentioned after 10 minutes, by the next hourly pulse; it expires at 30.
    async with world(resonant_home, chat, at(10, 9, 45)) as w:
        task_id = repo.insert_task(
            w.conn,
            NewTask(kind="chat", runner="local", priority=PRIORITY_OWNER, principal="justin"),
        )
        repo.set_status(w.conn, task_id, "waiting", wait_reason="approval")
        ap.insert_approval(
            w.conn,
            ap.NewApproval(
                task_id=task_id,
                intent_hash="h-a7",
                tool="restart_resonant",
                effect=Effect.WRITE,
                level=Level.L2,
                summary="restart Resonant",
                approvers=("justin",),
                code="A7",
                expires_at=at(10, 10, 15),
            ),
            at(10, 9, 45),
        )
        await w.tick(at(10, 10))
        assert w.texts() == ["btw:\n✋ A7 still waiting: restart Resonant"]
        for hour in (11, 12):
            await w.tick(at(10, hour))
        assert len(w.texts()) == 1  # once


# --- crash between send and told: exactly once ------------------------------------------------


async def test_crash_between_urgent_send_and_told_is_texted_once(
    resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(resonant_home, chat, at(10, 3, 0)) as w:
        real = items.mark_told

        def dies(*args: Any, **kw: Any) -> None:
            raise RuntimeError("daemon died")

        monkeypatch.setattr(items, "mark_told", dies)
        w.put("dori", "inc", "Dori failing", urgency="urgent")
        await w.d.loop.run_once()  # sent, then died before mark_told: the event is retried
        assert len(w.texts()) == 1 and len(w.unrouted()) == 1
        assert w.item("dori", "inc").changed
        monkeypatch.setattr(items, "mark_told", real)
        w.restart(at(10, 3, 1))
        await w.d.loop.run_once()  # routed again: the channel drops the repeated send
        assert w.texts() == ["[dori] \U0001f534 Dori failing"] and w.unrouted() == []
        assert not w.item("dori", "inc").changed


async def test_crash_between_pulse_send_and_told_is_texted_once(
    resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(resonant_home, chat, at(10, 5, 0)) as w:
        await w.tick(at(10, 5))  # the pulse has run before (catch-up needs a known last run)
        await w.report("trader", "pnl", "PnL -$1.8k today", state={"b": -2})
        real = items.mark_told
        calls = 0

        def dies(*args: Any, **kw: Any) -> None:
            nonlocal calls
            calls += 1
            raise asyncio.CancelledError  # the process is gone: the scheduler records nothing

        monkeypatch.setattr(items, "mark_told", dies)
        await w.tick(at(10, 6))  # the pulse is sent, then the daemon dies before marking
        assert calls == 1 and w.texts() == ["btw:\n[trader] PnL -$1.8k today"]
        assert w.item("trader", "pnl").changed
        monkeypatch.setattr(items, "mark_told", real)
        # The new item arrives while the daemon is down; it must not ride the old pulse.
        kv_set(w.conn, LAST_TICK_KEY, iso(at(10, 5, 59)))  # the tick that never finished
        w.restart(at(10, 6, 5))
        await w.report("trader", "late", "Late news", when=at(10, 6, 4))
        await w.tick(at(10, 6, 5))  # catch-up of the 06:00 pulse (within 50 minutes)
        assert w.texts() == ["btw:\n[trader] PnL -$1.8k today"]  # not sent twice
        assert not w.item("trader", "pnl").changed
        assert w.item("trader", "late").changed  # still pending
        await w.tick(at(10, 7))
        assert w.texts()[1:] == ["btw:\n[trader] Late news"]


# --- the commands, with the daemon running ----------------------------------------------------


async def test_pulse_commands_over_imessage(resonant_home: Path, chat: FakeChatDB) -> None:
    settings = make_settings(
        resonant_home, chat, pulse={"enabled": False}, briefing={"enabled": False}
    )
    osa = RecordingOsaRunner()
    daemon = Daemon(
        settings,
        model=ScriptedModel(),
        osa_runner=osa,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(FakeKeychain()),
    )
    kv_set(daemon.conn, "imessage.selftest", {"ok": True, "at": iso(at(10, 9)), "detail": "ok"})
    run = asyncio.create_task(daemon.run())
    try:
        assert daemon.imessage is not None
        channel = daemon.imessage.channel
        await wait_until(lambda: channel.health()["last_read_at"] is not None, 10)
        now = datetime.now(UTC)  # the only wall-clock use: the router's /pulse reads the real clock
        event = Event(
            source="ext:trader",
            type="ext.trader.item",
            payload={
                "text": "PnL -$1.8k today",
                "data": {"key": "pnl", "urgency": "normal", "state": None, "clear": False},
            },
            dedupe_key="ext:trader:item:pnl:1",
            occurred_at=now,
        )
        assert insert_event(daemon.conn, event)
        chat.add_message(handle=OWNER_PHONE, text="/pulse")
        await osa.wait_for(1, 20)
        assert "btw:\n[trader] PnL -$1.8k today" in osa.sent[0].text
        assert "Open: trader 1" in osa.sent[0].text
        chat.add_message(handle=OWNER_PHONE, text="/pulse now")
        await osa.wait_for(3, 20)
        assert [m.text for m in osa.sent[1:]] == ["btw:\n[trader] PnL -$1.8k today", "Pulse sent."]
        chat.add_message(handle=OWNER_PHONE, text="/pulse")
        await osa.wait_for(4, 20)
        assert "btw:" not in osa.sent[3].text and "Open: trader 1" in osa.sent[3].text
    finally:
        daemon.stop()
        assert await asyncio.wait_for(run, 10) == 0


async def test_failed_pulse_job_after_send_does_not_repeat_the_text_next_hour(
    resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job raises (not a crash) after the send: the scheduler consumes that due time, so
    no catch-up reruns it. The next hour must not say the same thing again."""
    async with world(resonant_home, chat, at(10, 9, 0)) as w:
        await w.report("trader", "pnl", "PnL -$1.8k today", state={"b": -2})
        real = items.mark_told

        def fails(*args: Any, **kw: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(items, "mark_told", fails)
        await w.tick(at(10, 10))
        assert w.texts() == ["btw:\n[trader] PnL -$1.8k today"]
        monkeypatch.setattr(items, "mark_told", real)
        await w.tick(at(10, 11))
        assert w.texts() == ["btw:\n[trader] PnL -$1.8k today"]
        assert not w.item("trader", "pnl").changed


async def _unsent_pulse(w: World, monkeypatch: pytest.MonkeyPatch, hour: int) -> None:
    """A pulse whose send went out but whose job raised before marking (due time consumed)."""
    real = items.mark_told

    def fails(*args: Any, **kw: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(items, "mark_told", fails)
    await w.tick(at(10, hour))
    monkeypatch.setattr(items, "mark_told", real)


async def test_unsent_record_older_than_two_hours_is_abandoned_and_recomposed(
    resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(resonant_home, chat, at(10, 9, 0)) as w:
        await w.report("trader", "pnl", "PnL -$1.8k today", state={"b": -2})
        await _unsent_pulse(w, monkeypatch, 10)
        # Hours later the item is still untold: the stale text is not resent, a fresh one is.
        await w.tick(at(10, 13))
        assert w.texts() == ["btw:\n[trader] PnL -$1.8k today"] * 2
        assert not w.item("trader", "pnl").changed


async def test_unsent_record_of_a_changed_item_is_abandoned(
    resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(resonant_home, chat, at(10, 9, 0)) as w:
        await w.report("trader", "pnl", "PnL -$1.8k today", state={"b": -2})
        await _unsent_pulse(w, monkeypatch, 10)
        await w.report("trader", "pnl", "PnL +$1k today", state={"b": 1})
        await w.tick(at(10, 11))
        assert w.texts() == ["btw:\n[trader] PnL -$1.8k today", "btw:\n[trader] PnL +$1k today"]


async def test_unsent_records_are_finished_oldest_first_and_a_corrupt_one_is_dropped(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    async with world(resonant_home, chat, at(10, 9, 0)) as w:
        sent: list[str] = []

        class Recording:
            async def send(
                self, principal: str, text: str, *, dedupe_key: str | None = None
            ) -> None:
                sent.append(text)

        now = at(10, 10)
        for name, when in (("z-newer", at(10, 9, 40)), ("a-older", at(10, 9, 10))):
            _, it = items.upsert(w.conn, "trader", name, name, "normal", None, False, 3600, when)
            kv_set(
                w.conn,
                pulse_job.INFLIGHT + name,
                {
                    "text": name,
                    "items": [["trader", name, it.fingerprint]],
                    "sent": False,
                    "at": iso(when),
                },
            )
        kv_set(
            w.conn,
            pulse_job.INFLIGHT + "bad",
            {"text": "x", "items": [], "sent": False, "at": "??"},
        )
        await pulse_job._finish_unsent(w.conn, Recording(), "justin", now, except_key="")  # type: ignore[arg-type]
        assert sent == ["a-older", "z-newer"]
        assert (
            w.conn.execute(
                "SELECT 1 FROM kv WHERE key = ?", (pulse_job.INFLIGHT + "bad",)
            ).fetchone()
            is None
        )
        pulse_job.prune_inflight(w.conn, now)  # a corrupt record never breaks pruning

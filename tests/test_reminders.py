from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from resonant.api import DaemonState
from resonant.components import deny_all_principals
from resonant.config import LoopConfig, load_settings
from resonant.executor import ToolFailed, ToolHandler
from resonant.gateway.channel import (
    ApprovalPrompt,
    ChannelRefused,
    ChannelSendError,
    MessageRef,
    SelfTestResult,
    Submit,
)
from resonant.loop import Fail, Loop, NewTask, SlotPool
from resonant.loop.types import PRIORITY_OWNER, Task
from resonant.runners.reminder import LATE_AFTER, ReminderRunner, reminder_text
from resonant.store import tasks as repo
from resonant.store.db import iso, kv_get, transaction, utcnow
from resonant.tools import ToolRegistry, builtin, reminders

TZ = "America/Los_Angeles"
OWNER = "owner"
# Tuesday 2026-10-13 10:00 PDT.
T0 = datetime(2026, 10, 13, 17, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@dataclass
class FakeChannel:
    """Records delivered texts and dedupes by key, like ``IMessageChannel``."""

    name: str = "imessage"
    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    keys: set[str] = field(default_factory=set[str])
    fail_with: list[BaseException] = field(default_factory=list[BaseException])
    attempts: int = 0
    delivered: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self, submit: Submit) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        self.attempts += 1
        if self.fail_with:
            raise self.fail_with.pop(0)
        if dedupe_key is None or dedupe_key not in self.keys:
            self.sent.append((principal, text, dedupe_key))
            if dedupe_key is not None:
                self.keys.add(dedupe_key)
        self.delivered.set()
        return MessageRef("imessage", f"out:{len(self.sent)}")

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        raise NotImplementedError

    async def self_test(self) -> SelfTestResult:
        return SelfTestResult(True, "fake")

    @property
    def texts(self) -> list[str]:
        return [t for _, t, _ in self.sent]


class _NoSave:
    def save(
        self,
        *,
        checkpoint: dict[str, Any] | None = None,
        claude_session_id: str | None = None,
        worktree_path: str | None = None,
    ) -> bool:
        return True


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def tools(db: sqlite3.Connection, clock: FakeClock) -> dict[str, ToolHandler]:
    settings = load_settings(timezone=TZ)
    state = DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)), utcnow())
    handlers = builtin.register_builtins(
        ToolRegistry(), db, state, principals=deny_all_principals(), clock=clock
    )
    return {name: h for (_, name), h in handlers.items()}


def make_loop(
    db: sqlite3.Connection,
    channel: FakeChannel,
    clock: Callable[[], datetime],
    tick_s: float = 30.0,
) -> Loop:
    runner = ReminderRunner(db, channel=channel, timezone=TZ, clock=clock)
    return Loop(
        db,
        {runner.name: runner},
        LoopConfig(tick_s=tick_s, stuck_after_s=60, max_attempts=3),
        SlotPool(5, 1),
        clock=clock,
    )


async def add(tools: dict[str, ToolHandler], at: datetime, text: str) -> dict[str, Any]:
    out = await tools["add_reminder"]({"at": at.isoformat(), "text": text})
    json.dumps(out, allow_nan=False)
    return out


def reminder_task(db: sqlite3.Connection, task_id: str) -> Task:
    task = repo.get_task(db, task_id)
    assert task is not None
    return task


# --- tools -----------------------------------------------------------------------------


async def test_add_creates_a_queued_reminder_task(
    db: sqlite3.Connection, tools: dict[str, ToolHandler]
) -> None:
    at = T0 + timedelta(minutes=20)
    out = await add(tools, at, "  stretch   now ")
    assert out == {
        "id": 1,
        "task_id": out["task_id"],
        "at": iso(at),
        "due": "10:20am",
        "text": "stretch now",
    }
    task = reminder_task(db, out["task_id"])
    assert (task.kind, task.runner, task.status) == ("reminder", "reminder", "queued")
    assert (task.principal, task.priority, task.wake_at) == (OWNER, PRIORITY_OWNER, at)
    assert task.checkpoint == {"n": 1, "text": "stretch now", "at": iso(at)}
    assert (await add(tools, at, "again"))["id"] == 2
    assert kv_get(db, reminders.SEQ_KEY) == 2


@pytest.mark.parametrize(
    "args",
    [
        {"text": "x"},
        {"at": "2026-10-13T11:00:00-07:00"},
        {"at": "2026-10-13T11:00:00-07:00", "text": "x", "extra": 1},
        {"at": "2026-10-13T11:00:00", "text": "x"},  # no offset
        {"at": "tomorrow", "text": "x"},
        {"at": 1760000000, "text": "x"},
        {"at": "2026-10-13T09:00:00-07:00", "text": "x"},  # passed
        {"at": "2027-10-14T11:00:00-07:00", "text": "x"},  # over a year
        {"at": "2026-10-13T11:00:00-07:00", "text": "   "},
        {"at": "2026-10-13T11:00:00-07:00", "text": "x" * 1001},
    ],
)
async def test_add_rejects_bad_args(
    db: sqlite3.Connection, tools: dict[str, ToolHandler], args: dict[str, Any]
) -> None:
    with pytest.raises(ToolFailed):
        await tools["add_reminder"](args)
    assert repo.count_by_status(db) == {}
    assert kv_get(db, reminders.SEQ_KEY) is None


async def test_add_accepts_1000_characters(tools: dict[str, ToolHandler]) -> None:
    assert reminders.TEXT_MAX == 1000  # GT-1
    out = await add(tools, T0 + timedelta(minutes=20), "y" * 1000)
    assert out["text"] == "y" * 1000


async def test_a_routed_reminder_survives_a_late_runner(
    db: sqlite3.Connection, tools: dict[str, ToolHandler], channel: FakeChannel, clock: FakeClock
) -> None:
    """``/remind now x`` routed 10 min before the command task ran (a busy loop, a restart):
    checked against ``routed_at``, it is set and fires at once, marked late."""
    routed = T0 - timedelta(minutes=10)
    args = {"at": routed.isoformat(), "text": "stretch"}
    with pytest.raises(ToolFailed, match="that time has passed"):
        await tools["add_reminder"](args)  # without routed_at: checked against the clock
    out = await tools["add_reminder"]({**args, "routed_at": routed.isoformat()})
    assert out["at"] == iso(routed)
    await make_loop(db, channel, clock).run_until_idle()
    assert channel.texts == ["⏰ stretch (late, due 9:50am)"]


async def test_a_near_term_routed_reminder_is_on_time(
    db: sqlite3.Connection, tools: dict[str, ToolHandler], channel: FakeChannel, clock: FakeClock
) -> None:
    routed = T0 - timedelta(seconds=90)  # "/remind in 2 min x", run 90 s late
    at = routed + timedelta(minutes=2)
    await tools["add_reminder"](
        {"at": at.isoformat(), "text": "x", "routed_at": routed.isoformat()}
    )
    loop = make_loop(db, channel, clock)
    await loop.run_until_idle()
    assert channel.sent == []
    clock.advance(seconds=30)
    await loop.run_until_idle()
    assert channel.texts == ["⏰ x"]


@pytest.mark.parametrize(
    "routed_at",
    [
        (T0 + timedelta(minutes=5)).isoformat(),  # in the future
        (T0 - timedelta(days=1, minutes=1)).isoformat(),  # more than a day old
        "2026-10-13T10:00:00",  # no offset
        "yesterday",
        1760000000,
    ],
)
async def test_routed_at_must_be_recent(
    db: sqlite3.Connection, tools: dict[str, ToolHandler], routed_at: Any
) -> None:
    at = (T0 + timedelta(minutes=20)).isoformat()
    with pytest.raises(ToolFailed, match="routed_at"):
        await tools["add_reminder"]({"at": at, "text": "x", "routed_at": routed_at})
    assert repo.count_by_status(db) == {}


def test_the_model_cannot_send_routed_at() -> None:
    """Model tool calls are validated against the schema, which has no ``routed_at``."""
    schema = reminders.ADD_REMINDER.args_schema
    assert "routed_at" not in schema["properties"]
    assert schema["additionalProperties"] is False


async def test_list_soonest_first_and_pending_only(
    db: sqlite3.Connection, tools: dict[str, ToolHandler], clock: FakeClock
) -> None:
    late = await add(tools, T0 + timedelta(days=1), "later")
    soon = await add(tools, T0 + timedelta(minutes=5), "soon")
    gone = await add(tools, T0 + timedelta(minutes=1), "gone")
    with transaction(db):
        repo.set_status(db, gone["task_id"], "done")
    # A reminder task of another principal never shows.
    with transaction(db):
        repo.insert_task(
            db,
            NewTask(
                kind="reminder",
                runner="reminder",
                priority=PRIORITY_OWNER,
                principal="someone",
                checkpoint={"n": 99, "text": "x", "at": iso(T0)},
            ),
        )
    out = await tools["list_reminders"]({})
    assert out == {
        "reminders": [
            {"id": soon["id"], "at": soon["at"], "due": "10:05am", "text": "soon"},
            {"id": late["id"], "at": late["at"], "due": "tomorrow 10:00am", "text": "later"},
        ],
        "count": 2,
    }
    with pytest.raises(ToolFailed):
        await tools["list_reminders"]({"x": 1})


async def test_cancel(db: sqlite3.Connection, tools: dict[str, ToolHandler]) -> None:
    r = await add(tools, T0 + timedelta(minutes=20), "stretch")
    out = await tools["cancel_reminder"]({"id": r["id"]})
    assert out["cancelled"] is True and out["id"] == r["id"] and out["text"] == "stretch"
    assert reminder_task(db, r["task_id"]).status == "cancelled"
    assert (await tools["list_reminders"]({}))["count"] == 0
    with pytest.raises(ToolFailed, match="already cancelled"):
        await tools["cancel_reminder"]({"id": r["id"]})
    with pytest.raises(ToolFailed, match="no reminder 7"):
        await tools["cancel_reminder"]({"id": 7})
    for bad in ({"id": 0}, {"id": "1"}, {"id": True}, {}, {"id": 1, "x": 2}):
        with pytest.raises(ToolFailed):
            await tools["cancel_reminder"](bad)


async def test_cancel_refuses_fired(db: sqlite3.Connection, tools: dict[str, ToolHandler]) -> None:
    r = await add(tools, T0 + timedelta(minutes=1), "x")
    with transaction(db):
        repo.set_status(db, r["task_id"], "done")
    with pytest.raises(ToolFailed, match="already went off"):
        await tools["cancel_reminder"]({"id": r["id"]})
    r = await add(tools, T0 + timedelta(minutes=1), "y")
    with transaction(db):
        repo.set_status(db, r["task_id"], "failed")
    with pytest.raises(ToolFailed, match="failed to send"):
        await tools["cancel_reminder"]({"id": r["id"]})


# --- runner ----------------------------------------------------------------------------


def test_reminder_text() -> None:
    at = T0
    assert reminder_text("stretch", at, now=at + LATE_AFTER, tz=TZ) == "⏰ stretch"
    assert (
        reminder_text("stretch", at, now=at + LATE_AFTER + timedelta(seconds=1), tz=TZ)
        == "⏰ stretch (late, due 10:00am)"
    )
    assert (
        reminder_text("stretch", at, now=at + timedelta(days=1), tz=TZ)
        == "⏰ stretch (late, due Tue 10:00am)"
    )


async def test_runner_send_is_deduped_per_reminder(
    db: sqlite3.Connection, tools: dict[str, ToolHandler], channel: FakeChannel, clock: FakeClock
) -> None:
    r = await add(tools, T0, "stretch")
    task = reminder_task(db, r["task_id"])
    runner = ReminderRunner(db, channel=channel, timezone=TZ, clock=clock)
    first = await runner.step(task, [], _NoSave())
    second = await runner.step(task, [], _NoSave())  # a replay after a crash
    assert first == second
    assert channel.sent == [(OWNER, "⏰ stretch", f"reminder:{task.id}")]
    assert channel.attempts == 2


async def test_runner_failures(db: sqlite3.Connection, channel: FakeChannel) -> None:
    runner = ReminderRunner(db, channel=channel, timezone=TZ, clock=FakeClock())
    base = Task(
        id="t1",
        kind="reminder",
        runner="reminder",
        priority=PRIORITY_OWNER,
        status="running",
        trace_id="tr",
        created_at=T0,
        updated_at=T0,
        principal=OWNER,
        checkpoint={"n": 1, "text": "x", "at": iso(T0)},
    )
    channel.fail_with = [ChannelSendError("down"), ChannelRefused("no identity")]
    retry = await runner.step(base, [], _NoSave())
    refused = await runner.step(base, [], _NoSave())
    assert isinstance(retry, Fail) and retry.retryable
    assert isinstance(refused, Fail) and not refused.retryable
    for cp in ({}, {"text": "x", "at": "bad"}, {"text": "x", "at": "2026-10-13T10:00:00"}):
        out = await runner.step(replace(base, checkpoint=cp), [], _NoSave())
        assert isinstance(out, Fail) and "malformed" in out.error and not out.retryable
    out = await runner.step(replace(base, principal=None), [], _NoSave())
    assert isinstance(out, Fail) and "principal" in out.error
    assert channel.sent == []


# --- loop ------------------------------------------------------------------------------


async def test_fires_once_on_time(
    db: sqlite3.Connection, tools: dict[str, ToolHandler], channel: FakeChannel, clock: FakeClock
) -> None:
    loop = make_loop(db, channel, clock)
    r = await add(tools, T0 + timedelta(minutes=20), "stretch")
    await loop.run_until_idle()
    assert channel.sent == []
    assert loop.wait_s() == 30.0
    clock.advance(minutes=19, seconds=50)
    assert loop.wait_s() == 10.0
    clock.advance(seconds=9.5)
    assert 0 < loop.wait_s() <= 1.0
    await loop.run_until_idle()
    assert channel.sent == []
    clock.advance(seconds=0.5)
    assert loop.wait_s() == 0.0  # fell due since the last tick: tick at once
    await loop.run_until_idle()
    assert channel.texts == ["⏰ stretch"]
    task = reminder_task(db, r["task_id"])
    assert task.status == "done" and task.result == {"late": False, "late_s": 0}
    clock.advance(hours=1)
    await loop.run_until_idle()
    assert channel.texts == ["⏰ stretch"]
    assert (await tools["list_reminders"]({}))["count"] == 0


async def test_survives_a_new_loop_on_the_same_db(
    db: sqlite3.Connection, tools: dict[str, ToolHandler], channel: FakeChannel, clock: FakeClock
) -> None:
    await add(tools, T0 + timedelta(minutes=5), "stretch")
    first = make_loop(db, channel, clock)
    first.resume()
    await first.run_until_idle()
    clock.advance(minutes=4)
    second = make_loop(db, channel, clock)  # "restart" before it is due
    second.resume()
    await second.run_until_idle()
    assert channel.sent == []
    clock.advance(minutes=1)
    await second.run_until_idle()
    assert channel.texts == ["⏰ stretch"]


async def test_missed_while_down_fires_once_marked_late(
    db: sqlite3.Connection, tools: dict[str, ToolHandler], channel: FakeChannel, clock: FakeClock
) -> None:
    await add(tools, T0 + timedelta(hours=5), "stretch")  # 3:00pm local
    clock.advance(hours=5, minutes=10)  # the daemon was down from before T to T+10 min
    loop = make_loop(db, channel, clock)
    loop.resume()
    await loop.run_until_idle()
    assert channel.texts == ["⏰ stretch (late, due 3:00pm)"]
    await make_loop(db, channel, clock).run_until_idle()
    assert channel.attempts == 1


async def test_send_failure_retries_without_double_send(
    db: sqlite3.Connection, tools: dict[str, ToolHandler], channel: FakeChannel, clock: FakeClock
) -> None:
    r = await add(tools, T0 + timedelta(minutes=1), "stretch")
    channel.fail_with = [ChannelSendError("down")]
    loop = make_loop(db, channel, clock)
    clock.advance(minutes=1)
    await loop.run_until_idle()
    task = reminder_task(db, r["task_id"])
    assert task.status == "queued" and task.wake_at is not None and task.attempts == 1
    assert (await tools["list_reminders"]({}))["count"] == 1  # still pending
    assert 0 < loop.wait_s() <= 5.0  # the retry backoff, not a whole tick
    clock.advance(seconds=5)
    await loop.run_until_idle()
    assert channel.texts == ["⏰ stretch"]


async def test_cancel_prevents_firing(
    db: sqlite3.Connection, tools: dict[str, ToolHandler], channel: FakeChannel, clock: FakeClock
) -> None:
    r = await add(tools, T0 + timedelta(minutes=20), "stretch")
    loop = make_loop(db, channel, clock)
    await tools["cancel_reminder"]({"id": r["id"]})
    clock.advance(minutes=30)
    await loop.run_until_idle()
    assert channel.sent == []
    assert loop.wait_s() == 30.0


async def test_run_sleeps_until_the_next_wake(db: sqlite3.Connection, channel: FakeChannel) -> None:
    """With a 30 s tick, a reminder due in 50 ms still fires right away (real clock)."""
    loop = make_loop(db, channel, utcnow, tick_s=30.0)
    at = utcnow() + timedelta(milliseconds=50)
    with transaction(db):
        repo.insert_task(
            db,
            NewTask(
                kind="reminder",
                runner="reminder",
                priority=PRIORITY_OWNER,
                principal=OWNER,
                checkpoint={"n": 1, "text": "stretch", "at": iso(at)},
                wake_at=at,
            ),
        )
    run = asyncio.create_task(loop.run())
    try:
        await asyncio.wait_for(channel.delivered.wait(), timeout=5)
    finally:
        loop.stop()
        await run
    assert channel.texts == ["⏰ stretch"]


# --- store -----------------------------------------------------------------------------


def test_next_wake_at(db: sqlite3.Connection) -> None:
    def task(wake_at: datetime | None) -> str:
        spec = NewTask(kind="k", runner="echo", priority=1, wake_at=wake_at)
        return repo.insert_task(db, spec)

    assert repo.next_wake_at(db, T0) is None
    task(None)
    past = task(T0 - timedelta(minutes=1))
    assert repo.next_wake_at(db, T0) is None  # already due: runnable, not a future wake
    later = task(T0 + timedelta(minutes=10))
    soon = task(T0 + timedelta(minutes=5))
    assert repo.next_wake_at(db, T0) == T0 + timedelta(minutes=5)
    repo.set_status(db, soon, "done")
    repo.set_status(db, past, "waiting", wait_reason="approval", wake_at=T0 + timedelta(minutes=2))
    assert repo.next_wake_at(db, T0) == T0 + timedelta(minutes=2)
    repo.set_status(db, past, "cancelled")
    assert repo.next_wake_at(db, T0) == T0 + timedelta(minutes=10)
    assert reminder_wake(db, later) == T0 + timedelta(minutes=10)


def reminder_wake(db: sqlite3.Connection, task_id: str) -> datetime | None:
    return reminder_task(db, task_id).wake_at

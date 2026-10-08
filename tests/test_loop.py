from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest
from resonant.config import LoopConfig
from resonant.loop import Continue, Done, Fail, Loop, NewTask, SlotPool, StepOutcome, Task, Wait
from resonant.loop.types import PRIORITY_BACKGROUND, PRIORITY_ONCALL, PRIORITY_OWNER
from resonant.store import tasks as repo
from resonant.store.db import utcnow
from resonant_sdk import Event


class FakeClock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@dataclass
class Scripted:
    """Returns queued outcomes in order (the last one repeats). Records every call."""

    outcomes: list[StepOutcome | Exception]
    name: str = "scripted"
    uses_claude_slot: bool = False
    calls: list[tuple[Task, list[Event]]] = field(default_factory=list[tuple[Task, list[Event]]])

    async def step(self, task: Task, events: list[Event]) -> StepOutcome:
        self.calls.append((task, events))
        out = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(out, Exception):
            raise out
        return out


@dataclass
class Blocking:
    """Blocks until released. Used to hold steps in flight."""

    name: str = "blocking"
    uses_claude_slot: bool = False
    release: asyncio.Event = field(default_factory=asyncio.Event)
    started: list[str] = field(default_factory=list[str])

    async def step(self, task: Task, events: list[Event]) -> StepOutcome:
        self.started.append(task.id)
        await self.release.wait()
        return Done(result={})


def make_loop(
    db: sqlite3.Connection,
    runners: dict[str, Any],
    clock: FakeClock | None = None,
    **kw: Any,
) -> Loop:
    slots = kw.pop("slots", SlotPool(5, 1))
    return Loop(
        db,
        runners,
        LoopConfig(tick_s=0.01, stuck_after_s=60, max_attempts=3),
        slots,
        clock=clock or FakeClock(),
        **kw,
    )


def spec(runner: str = "scripted", priority: int = PRIORITY_OWNER, **kw: Any) -> NewTask:
    return NewTask(kind="test", runner=runner, priority=priority, **kw)


def task(db: sqlite3.Connection, task_id: str) -> Task:
    t = repo.get_task(db, task_id)
    assert t is not None
    return t


async def test_task_runs_to_done(db: sqlite3.Connection) -> None:
    r = Scripted([Done(result={"ok": 1})])
    loop = make_loop(db, {"scripted": r})
    tid = loop.create_task(spec())
    await loop.run_until_idle()
    t = task(db, tid)
    assert (t.status, t.result, t.step_no) == ("done", {"ok": 1}, 1)


async def test_continue_persists_checkpoint_between_steps(db: sqlite3.Connection) -> None:
    class Counter:
        name = "counter"
        uses_claude_slot = False

        async def step(self, task: Task, events: list[Event]) -> StepOutcome:
            n = task.checkpoint.get("n", 0) + 1
            return Done(result={"n": n}) if n == 3 else Continue({"n": n})

    loop = make_loop(db, {"counter": Counter()})
    tid = loop.create_task(spec("counter"))
    await loop.run_until_idle()
    t = task(db, tid)
    assert (t.status, t.result, t.step_no) == ("done", {"n": 3}, 3)


async def test_event_wakes_waiting_task(db: sqlite3.Connection) -> None:
    r = Scripted([Wait("human_reply", {"asked": True}), Done(result={})])
    loop = make_loop(db, {"scripted": r})
    tid = loop.create_task(spec())
    await loop.run_until_idle()
    t = task(db, tid)
    assert (t.status, t.wait_reason, t.checkpoint) == ("waiting", "human_reply", {"asked": True})

    assert loop.submit(Event(source="imessage", type="message", task_id=tid, payload={"t": "y"}))
    await loop.run_until_idle()
    assert task(db, tid).status == "done"
    assert [e.payload for e in r.calls[1][1]] == [{"t": "y"}]
    assert repo.pending_events(db, tid) == []


async def test_wake_at_fires_on_tick(db: sqlite3.Connection) -> None:
    clock = FakeClock()
    r = Scripted([Wait("usage_reset", {}, wake_at=clock.now + timedelta(hours=1)), Done({})])
    loop = make_loop(db, {"scripted": r}, clock)
    tid = loop.create_task(spec())
    await loop.run_until_idle()
    clock.advance(minutes=59)
    await loop.run_until_idle()
    assert task(db, tid).status == "waiting"
    clock.advance(minutes=2)
    await loop.run_until_idle()
    assert task(db, tid).status == "done"


async def test_event_during_step_is_not_lost(db: sqlite3.Connection) -> None:
    holder: dict[str, Loop] = {}

    class SubmitsDuringStep:
        name = "s"
        uses_claude_slot = False
        calls = 0

        async def step(self, task: Task, events: list[Event]) -> StepOutcome:
            self.calls += 1
            if self.calls == 1:
                holder["loop"].submit(Event(source="x", type="late", task_id=task.id))
                return Wait("approval", {})
            return Done(result={"saw": [e.type for e in events]})

    loop = make_loop(db, {"s": SubmitsDuringStep()})
    holder["loop"] = loop
    tid = loop.create_task(spec("s"))
    await loop.run_until_idle()
    assert task(db, tid).result == {"saw": ["late"]}


async def test_retryable_failure_backs_off_and_keeps_events(db: sqlite3.Connection) -> None:
    clock = FakeClock()
    r = Scripted([Fail("flaky", retryable=True), Done({})])
    loop = make_loop(db, {"scripted": r}, clock)
    tid = loop.create_task(spec())
    loop.submit(Event(source="x", type="input", task_id=tid))
    await loop.run_until_idle()
    t = task(db, tid)
    assert (t.status, t.attempts, t.error) == ("queued", 1, "flaky")
    assert t.wake_at is not None and t.wake_at > clock.now
    assert len(repo.pending_events(db, tid)) == 1

    clock.advance(seconds=10)
    await loop.run_until_idle()
    assert task(db, tid).status == "done"
    assert [e.type for e in r.calls[1][1]] == ["input"]


async def test_exhausted_retries_fail(db: sqlite3.Connection) -> None:
    clock = FakeClock()
    r = Scripted([RuntimeError("boom")])
    loop = make_loop(db, {"scripted": r}, clock)
    tid = loop.create_task(spec())
    for _ in range(3):
        await loop.run_until_idle()
        clock.advance(minutes=10)
    t = task(db, tid)
    assert (t.status, t.attempts) == ("failed", 3)
    assert t.error is not None and "boom" in t.error


async def test_non_retryable_failure_is_terminal(db: sqlite3.Connection) -> None:
    loop = make_loop(db, {"scripted": Scripted([Fail("bad input")])})
    tid = loop.create_task(spec())
    await loop.run_until_idle()
    assert task(db, tid).status == "failed"


async def test_crash_mid_step_resumes_from_checkpoint(db: sqlite3.Connection) -> None:
    blocking = Blocking(name="work")
    loop1 = make_loop(db, {"work": blocking})
    tid = loop1.create_task(spec("work", checkpoint={"progress": 1}))
    loop1.submit(Event(source="x", type="input", task_id=tid))
    await loop1.run_once()
    await asyncio.sleep(0)
    assert task(db, tid).status == "running"
    # Simulate a crash: the step dies without persisting anything.
    for t in list(loop1._inflight.values()):  # pyright: ignore[reportPrivateUsage]
        t.cancel()
    await asyncio.sleep(0.01)
    assert task(db, tid).status == "running"

    r = Scripted([Done({})])
    loop2 = make_loop(db, {"work": r})
    assert loop2.resume() == 1
    await loop2.run_until_idle()
    resumed_task, events = r.calls[0]
    assert resumed_task.checkpoint == {"progress": 1}
    assert [e.type for e in events] == ["input"]
    assert task(db, tid).status == "done"


async def test_dedupe_key_drops_duplicates(db: sqlite3.Connection) -> None:
    loop = make_loop(db, {})
    assert loop.submit(Event(source="imessage", type="message", dedupe_key="im:42"))
    assert not loop.submit(Event(source="imessage", type="message", dedupe_key="im:42"))


async def test_priority_order(db: sqlite3.Connection) -> None:
    r = Scripted([Done({})])
    loop = make_loop(db, {"scripted": r}, max_parallel=1)
    ids = {
        p: loop.create_task(spec(priority=p))
        for p in (PRIORITY_BACKGROUND, PRIORITY_ONCALL, PRIORITY_OWNER)
    }
    await loop.run_until_idle()
    assert [c[0].id for c in r.calls] == [
        ids[PRIORITY_ONCALL],
        ids[PRIORITY_OWNER],
        ids[PRIORITY_BACKGROUND],
    ]


async def test_claude_slots_cap_and_reserve(db: sqlite3.Connection) -> None:
    claude = Blocking(name="claude", uses_claude_slot=True)
    loop = make_loop(db, {"claude": claude}, slots=SlotPool(capacity=2, reserved_oncall=1))
    normal = [loop.create_task(spec("claude", PRIORITY_OWNER)) for _ in range(3)]
    oncall = loop.create_task(spec("claude", PRIORITY_ONCALL, lane="oncall"))
    await loop.run_once()
    await asyncio.sleep(0)
    # One normal task takes the unreserved slot. The reserved slot goes to on-call.
    assert sorted(claude.started) == sorted([normal[0], oncall])
    assert loop.slots.in_use == 2
    claude.release.set()
    await loop.run_until_idle()
    assert all(task(db, t).status == "done" for t in [*normal, oncall])
    assert loop.slots.in_use == 0


async def test_stuck_step_is_cancelled_and_retried(db: sqlite3.Connection) -> None:
    clock = FakeClock()
    loop = make_loop(db, {"blocking": Blocking()}, clock)
    tid = loop.create_task(spec("blocking"))
    await loop.run_once()
    await asyncio.sleep(0)
    clock.advance(seconds=61)
    await loop.run_once()
    await asyncio.sleep(0.01)
    t = task(db, tid)
    assert (t.status, t.attempts, t.error) == ("queued", 1, "step exceeded stuck_after_s")


async def test_cancel_inflight(db: sqlite3.Connection) -> None:
    loop = make_loop(db, {"blocking": Blocking()})
    tid = loop.create_task(spec("blocking"))
    await loop.run_once()
    await asyncio.sleep(0)
    assert loop.cancel(tid)
    await asyncio.sleep(0.01)
    assert task(db, tid).status == "cancelled"
    assert not loop.cancel(tid)


async def test_router_creates_task_from_event(db: sqlite3.Connection) -> None:
    r = Scripted([Done({})])

    async def router(event: Event) -> NewTask | None:
        return spec(principal=event.principal) if event.type == "message" else None

    loop = make_loop(db, {"scripted": r}, router=router)
    ev = Event(source="imessage", type="message", principal="justin", payload={"text": "hi"})
    loop.submit(ev)
    loop.submit(Event(source="imessage", type="noise"))
    await loop.run_until_idle()
    created, events = r.calls[0]
    assert created.origin_event_id == ev.id
    assert created.trace_id == ev.trace_id
    assert created.principal == "justin"
    assert [e.id for e in events] == [ev.id]
    assert repo.unrouted_events(db) == []


async def test_unknown_runner_fails_task(db: sqlite3.Connection) -> None:
    loop = make_loop(db, {})
    tid = loop.create_task(spec("nope"))
    await loop.run_until_idle()
    t = task(db, tid)
    assert t.status == "failed" and t.error == "unknown runner 'nope'"


async def test_run_and_stop(db: sqlite3.Connection) -> None:
    r = Scripted([Done({})])
    loop = make_loop(db, {"scripted": r})
    runner_task = asyncio.create_task(loop.run())
    tid = loop.create_task(spec())
    for _ in range(100):
        if task(db, tid).status == "done":
            break
        await asyncio.sleep(0.01)
    loop.stop()
    await asyncio.wait_for(runner_task, 2)
    assert task(db, tid).status == "done"


@pytest.mark.parametrize("attempt,expected", [(1, 5), (2, 10), (3, 20), (10, 300)])
def test_retry_backoff(attempt: int, expected: float) -> None:
    from resonant.loop.loop import retry_backoff

    assert retry_backoff(attempt).total_seconds() == expected

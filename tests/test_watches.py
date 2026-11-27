"""BR-7 watches and proactive notices (docs/brain-plan.md D10): ``brain/watches.py``,
``tools/watches.py``, ``runners/watch.py`` (the checks and ``core/watches``) and
``brain/notices.py`` (through the pulse, capped per hour and per day). The tools run through
the real builtin handlers, the checks through the real gate and executor; the channel is a
recorder. Nothing touches ~/.resonant (``resonant_home``)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from resonant.api import DaemonState
from resonant.brain import notices
from resonant.brain import watches as store
from resonant.components import Components, build_components
from resonant.config import Settings, load_settings
from resonant.executor import CallContext, ContextHandler, ToolFailed
from resonant.extensions.notify import clean_text
from resonant.extensions.scheduler import CORE_JOBS, CoreDeps
from resonant.gate import Intent, NeedsApproval
from resonant.gateway.channel import MessageRef
from resonant.loop import Loop, SlotPool
from resonant.loop.types import Done, NewTask, StepContext
from resonant.runners.ask import FOLLOW_UP_KEY, AskRefused, Queued
from resonant.runners.watch import WatchRunner, check_label, queue_due, watches_job
from resonant.store import pulse as items
from resonant.store.db import kv_set
from resonant.store.tasks import get_task, insert_task
from resonant.tools import watches as watch_tools
from resonant.tools.base import BuiltinContext

PRINCIPALS = """\
principals:
  alice:
    role: owner
    identities: ["imessage:+15551234567"]
"""
NOW = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)  # 11:00 in New York: not quiet


@dataclass
class Sent:
    principal: str
    text: str
    dedupe_key: str | None
    thread: MessageRef | None = None


@dataclass
class Recorder:
    sent: list[Sent] = field(default_factory=list[Sent])
    keys: set[str] = field(default_factory=set[str])

    async def send(
        self,
        principal: str,
        text: str,
        *,
        dedupe_key: str | None = None,
        thread: MessageRef | None = None,
        **_: Any,
    ) -> Any:
        if dedupe_key is not None and dedupe_key in self.keys:
            return None
        if dedupe_key is not None:
            self.keys.add(dedupe_key)
        self.sent.append(Sent(principal, text, dedupe_key, thread))
        return None


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def make_settings(**brain: Any) -> Settings:
    return load_settings(dry_run=False, timezone="America/New_York", brain=brain)


@pytest.fixture
def settings(resonant_home: Any) -> Settings:
    s = make_settings()
    s.principals_path.write_text(PRINCIPALS)
    return s


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def components(settings: Settings, db: sqlite3.Connection, clock: Clock) -> Components:
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    state = DaemonState(settings, db, loop, started_at=NOW)
    state.components = build_components(settings, db, state)
    kv_set(db, "autonomy", "on")
    return with_clock(state, clock)


def with_clock(state: DaemonState, clock: Clock) -> Components:
    """The watch tools again over a context with the fake clock (expiry, list windows)."""
    c = state.components
    assert c is not None
    ctx = BuiltinContext(
        conn=state.conn, state=state, registry=c.registry, principals=c.principals, clock=clock
    )
    for name, handler in watch_tools.handlers(ctx).items():
        c.handlers[(None, name)] = handler
    return c


async def call(c: Components, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return await c.handlers[(None, name)](args or {})


def runner(
    c: Components,
    db: sqlite3.Connection,
    settings: Settings,
    ch: Recorder,
    clock: Clock,
    asks: Any = None,
) -> WatchRunner:
    return WatchRunner(
        db,
        gate=c.gate,
        executor=c.executor,
        registry=c.registry,
        principals=c.principals,
        channel=cast(Any, ch),
        settings=settings,
        asks=lambda: asks,
        clock=clock,
    )


async def run_due(db: sqlite3.Connection, w: WatchRunner, clock: Clock) -> list[Any]:
    """Queue what's due now and run each check task once."""
    queue_due(db, clock())
    rows = db.execute("SELECT id FROM tasks WHERE runner = 'watch' AND status = 'queued'")
    out: list[Any] = []
    for (task_id,) in rows.fetchall():
        task = get_task(db, task_id)
        assert task is not None
        out.append(await w.step(task, [], cast(StepContext, None)))
        db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    return out


# --- create_watch, list_watches, cancel_watch ------------------------------------------------


async def test_create_a_tool_watch(components: Components, db: sqlite3.Connection) -> None:
    out = await call(
        components,
        "create_watch",
        {
            "what": "milk on the list",
            "tool": "list_notes",
            "args": {},
            "when": "contains",
            "text": "milk",
        },
    )
    assert out["created"] is True and out["id"] == "W1" and out["every_minutes"] == 60
    w = store.get(db, 1)
    assert w is not None and (w.state, w.check_kind, w.tool, w.op) == (
        "active",
        "tool",
        "list_notes",
        "contains",
    )
    assert w.expires_at == NOW + timedelta(days=7) and w.next_at == NOW


async def test_create_an_ask_watch_defaults_to_hourly(components: Components) -> None:
    out = await call(
        components,
        "create_watch",
        {"what": "iPhone in stock", "ask": "Is it in stock at https://x.test?"},
    )
    assert out["check"] == {"ask": "Is it in stock at https://x.test?"}
    assert out["every_minutes"] == 60


@pytest.mark.parametrize(
    ("args", "why"),
    [
        ({"what": "x"}, "either tool"),
        ({"what": "x", "tool": "list_notes", "ask": "q?"}, "either tool"),
        ({"what": "x", "tool": "add_note", "args": {"text": "a"}, "when": "changed"}, "read-only"),
        ({"what": "x", "tool": "ask_claude", "args": {}, "when": "changed"}, "no tool named"),
        ({"what": "x", "tool": "nope", "when": "changed"}, "no tool named"),
        ({"what": "x", "tool": "list_notes", "args": {"x": 1}, "when": "changed"}, "don't fit"),
        ({"what": "x", "tool": "list_notes", "when": "contains"}, "needs match_text"),
        ({"what": "x", "tool": "list_notes", "when": "changed", "every_minutes": 10}, "every 15"),
        ({"what": "x", "ask": "q?", "every_minutes": 30}, "every 60"),
        ({"what": "x", "ask": "q?", "days": 31}, "days is 1 to 30"),
        ({"what": "x", "ask": "q?", "when": "contains"}, "takes no args"),
        ({"what": "x", "ask": "q?", "bogus": 1}, "unknown arguments"),
    ],
)
async def test_create_refuses(components: Components, args: dict[str, Any], why: str) -> None:
    with pytest.raises(ToolFailed, match=why):
        await call(components, "create_watch", args)


async def test_create_refuses_past_max_active(
    resonant_home: Any, db: sqlite3.Connection, clock: Clock
) -> None:
    s = make_settings(watches={"max_active": 1})
    s.principals_path.write_text(PRINCIPALS)
    loop = Loop(db, {}, s.loop, SlotPool(2, 1))
    state = DaemonState(s, db, loop, started_at=NOW)
    state.components = build_components(s, db, state)
    c = with_clock(state, clock)
    await call(c, "create_watch", {"what": "a", "ask": "q?"})
    with pytest.raises(ToolFailed, match="1 watches are already active"):
        await call(c, "create_watch", {"what": "b", "ask": "q?"})


async def test_create_watch_asks_the_owner_first(
    components: Components, db: sqlite3.Connection
) -> None:
    spec = components.registry.get(None, "create_watch")
    assert spec is not None
    for origin in ("inferred", "tainted", "scheduled"):
        task = insert_task(db, NewTask(kind="chat", runner="brain", priority=1, principal="alice"))
        intent = Intent.for_tool(
            spec, task_id=task, principal="alice", origin=origin, args={"what": "x", "ask": "q?"}
        )
        assert isinstance(components.gate.evaluate(intent), NeedsApproval), origin


async def test_list_and_cancel(components: Components, clock: Clock) -> None:
    await call(components, "create_watch", {"what": "a", "ask": "q?"})
    await call(components, "create_watch", {"what": "b", "ask": "r?"})
    out = await call(components, "cancel_watch", {"id": "w1"})
    assert out == {"id": "W1", "what": "a", "cancelled": True, "state": "cancelled"}
    again = await call(components, "cancel_watch", {"id": "W1"})
    assert again["cancelled"] is False and again["state"] == "cancelled"
    with pytest.raises(ToolFailed, match="no watch W9"):
        await call(components, "cancel_watch", {"id": "9"})
    listed = await call(components, "list_watches")
    assert [(w["id"], w["state"]) for w in listed["watches"]] == [
        ("W2", "active"),
        ("W1", "cancelled"),
    ]
    assert listed["active"] == 1
    clock.now += timedelta(days=8)  # ended a week ago: no longer listed
    assert [w["id"] for w in (await call(components, "list_watches"))["watches"]] == ["W2"]


# --- decisions -----------------------------------------------------------------------------


def _watch(op: store.Op, value: str | None = None, **kw: Any) -> store.Watch:
    base: dict[str, Any] = dict(
        id=1,
        principal="alice",
        what="w",
        check_kind="tool",
        extension=None,
        tool="t",
        args={},
        question=None,
        op=op,
        value=value,
        then="stop",
        interval_s=900,
        state="active",
        created_at=NOW,
        expires_at=NOW,
        next_at=NOW,
        checks=0,
        fails=0,
        fired=0,
        last_met=False,
        baseline=None,
        last_checked_at=None,
        last_error=None,
        last_ask_task=None,
        open_task_id=None,
    )
    base.update(kw)
    return store.Watch(**base)


def test_judge_contains_and_missing_fire_on_the_edge() -> None:
    hit = store.judge_tool(_watch("contains", "Shipped"), {"status": "shipped today"})
    assert (hit.met, hit.fire, hit.detail) == (True, True, 'saw "Shipped"')
    again = store.judge_tool(_watch("contains", "shipped", last_met=True), {"s": "shipped"})
    assert (again.met, again.fire) == (True, False)  # still true: no second text
    gone = store.judge_tool(_watch("missing", "pending"), {"s": "done"})
    assert (gone.met, gone.fire, gone.detail) == (True, True, '"pending" is gone')


def test_judge_changed_sets_a_baseline_first() -> None:
    first = store.judge_tool(_watch("changed"), {"v": 1})
    assert (first.met, first.fire) == (False, False) and first.baseline
    same = store.judge_tool(_watch("changed", baseline=first.baseline), {"v": 1})
    assert (same.met, same.fire, same.baseline) == (False, False, None)
    diff = store.judge_tool(_watch("changed", baseline=first.baseline), {"v": 2})
    assert (diff.met, diff.fire, diff.detail) == (True, True, "it changed") and diff.baseline


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        ("YES, in stock.", True),
        ("no. Sold out.", False),
        ("yes", True),
        ("Maybe?", None),
        ("**YES** it is", True),
    ],
)
def test_parse_yes(answer: str, said: bool | None) -> None:
    assert store.parse_yes(answer) is said


def test_judge_ask_detail_is_the_rest_of_the_answer() -> None:
    v = store.judge_ask(_watch("yes", check_kind="ask"), "YES. The Pro is in stock.")
    assert (v.met, v.fire, v.detail) == (True, True, "The Pro is in stock")


# --- core/watches and the checks ----------------------------------------------------------


def test_core_job_needs_a_channel_and_an_owner(
    settings: Settings, db: sqlite3.Connection, components: Components
) -> None:
    assert any(f.__name__ == "watches_job" for f in CORE_JOBS)
    deps = CoreDeps(settings, db, components, None, "alice")
    assert watches_job(deps) is None
    job = watches_job(CoreDeps(settings, db, components, cast(Any, Recorder()), "alice"))
    assert job is not None and (job.id, job.schedule) == ("watches", "*/5 * * * *")
    off = make_settings(watches={"enabled": False})
    assert watches_job(CoreDeps(off, db, components, cast(Any, Recorder()), "alice")) is None


async def test_queue_due_claims_once_and_expires(
    components: Components, db: sqlite3.Connection, clock: Clock
) -> None:
    await call(components, "create_watch", {"what": "a", "ask": "q?", "days": 1})
    assert queue_due(db, NOW) == {"queued": 1, "expired": 0}
    w = store.get(db, 1)
    assert w is not None and w.open_task_id is not None and w.next_at == NOW + timedelta(hours=1)
    assert queue_due(db, NOW + timedelta(hours=2)) == {"queued": 0, "expired": 0}  # still open
    db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (w.open_task_id,))
    assert queue_due(db, NOW + timedelta(hours=2))["queued"] == 1
    assert queue_due(db, NOW + timedelta(days=2)) == {"queued": 0, "expired": 0}  # open again
    db.execute("UPDATE tasks SET status = 'done' WHERE runner = 'watch'")
    assert queue_due(db, NOW + timedelta(days=2)) == {"queued": 0, "expired": 1}
    ended = store.get(db, 1)
    assert ended is not None and ended.state == "expired"


async def test_tool_watch_fires_once_through_the_pulse(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    ch = Recorder()
    w = runner(components, db, settings, ch, clock)
    await call(
        components,
        "create_watch",
        {
            "what": "milk on the list",
            "tool": "list_notes",
            "args": {},
            "when": "contains",
            "text": "milk",
            "every_minutes": 15,
        },
    )
    (out,) = await run_due(db, w, clock)
    assert isinstance(out, Done) and out.result["status"] == "checked" and ch.sent == []
    await call(components, "add_note", {"text": "buy milk"})
    clock.now += timedelta(minutes=15)
    (out,) = await run_due(db, w, clock)
    assert out.result["status"] == "fired"
    (sent,) = ch.sent
    assert sent.principal == "alice" and sent.text == '\U0001f440 W1 milk on the list: saw "milk"'
    watch = store.get(db, 1)
    assert watch is not None and (watch.state, watch.fired, watch.checks) == ("fired", 1, 2)
    item = items.get(db, "core", "notice/watch/1/1")
    assert item is not None and not item.changed  # told: the hourly pulse won't repeat it
    gate_rows = db.execute(
        "SELECT detail FROM audit_log WHERE action = 'gate.evaluate' AND tool = 'list_notes'"
    ).fetchall()
    assert len(gate_rows) == 2 and all('"scheduled"' in r[0] for r in gate_rows)
    clock.now += timedelta(minutes=15)
    assert await run_due(db, w, clock) == []  # ended


async def test_then_continue_fires_again_only_after_a_gap(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    ch = Recorder()
    w = runner(components, db, settings, ch, clock)
    await call(
        components,
        "create_watch",
        {
            "what": "milk",
            "tool": "list_notes",
            "args": {},
            "when": "contains",
            "text": "milk",
            "then": "continue",
            "every_minutes": 15,
        },
    )
    await call(components, "add_note", {"text": "milk"})
    for _ in range(3):
        await run_due(db, w, clock)
        clock.now += timedelta(minutes=15)
    assert len(ch.sent) == 1  # met three times in a row: one text
    await call(components, "clear_notes", {})
    await run_due(db, w, clock)
    clock.now += timedelta(minutes=15)
    await call(components, "add_note", {"text": "milk again"})
    await run_due(db, w, clock)
    assert len(ch.sent) == 2 and ch.sent[1].dedupe_key == "notice:notice/watch/1/2"


async def test_a_failing_check_ends_the_watch_with_one_notice(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    ch = Recorder()
    w = runner(components, db, settings, ch, clock)
    await call(
        components,
        "create_watch",
        {"what": "gone", "tool": "list_notes", "args": {}, "when": "changed", "every_minutes": 15},
    )
    db.execute("UPDATE brain_watches SET tool = 'no_such_tool'")
    outs: list[Any] = []
    for _ in range(3):
        outs += await run_due(db, w, clock)
        clock.now += timedelta(minutes=15)
    assert [o.result["status"] for o in outs] == ["check_failed", "check_failed", "failed"]
    (sent,) = ch.sent
    assert sent.text.startswith("⚠ Stopped watch W1 gone: its check failed 3 times")
    watch = store.get(db, 1)
    assert watch is not None and watch.state == "failed"


async def test_an_ask_watch_answers_through_the_hook(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    asked: list[dict[str, Any]] = []

    class Asks:
        def enqueue(self, question: str, **kw: Any) -> Queued:
            asked.append({"question": question, **kw})
            return Queued(f"ask{len(asked)}")

    ch = Recorder()
    w = runner(components, db, settings, ch, clock, asks=Asks())
    await call(components, "create_watch", {"what": "Pro in stock", "ask": "Is the Pro in stock?"})
    (out,) = await run_due(db, w, clock)
    assert out.result == {"watch": 1, "status": "asked", "ask_task": "ask1"}
    assert asked[0]["watch"] == 1 and asked[0]["kind"] == "research"
    assert asked[0]["question"] == "Is the Pro in stock?" and asked[0]["principal"] == "alice"
    await w.ask_answered(1, "ask1", "answered", "NO, sold out.")
    assert ch.sent == []
    clock.now += timedelta(hours=1)
    await run_due(db, w, clock)
    await w.ask_answered(1, "ask2", "answered", "YES. In stock at the Fifth Ave store.")
    await w.ask_answered(1, "ask2", "answered", "YES. In stock at the Fifth Ave store.")
    (sent,) = ch.sent
    assert sent.text == "\U0001f440 W1 Pro in stock: In stock at the Fifth Ave store"
    watch = store.get(db, 1)
    assert watch is not None and watch.state == "fired"


async def test_a_refused_ask_skips_without_failing(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    from resonant.runners.ask import AskRefused

    class Capped:
        def enqueue(self, question: str, **kw: Any) -> AskRefused:
            return AskRefused("I've asked Claude 30 times today, the daily cap.")

    w = runner(components, db, settings, Recorder(), clock, asks=Capped())
    await call(components, "create_watch", {"what": "x", "ask": "q?"})
    (out,) = await run_due(db, w, clock)
    assert out.result["status"] == "skipped"
    watch = store.get(db, 1)
    assert watch is not None and (watch.state, watch.fails, watch.checks) == ("active", 0, 1)


# --- notices -------------------------------------------------------------------------------


async def notice(
    db: sqlite3.Connection, settings: Settings, ch: Recorder, key: str, now: datetime = NOW
) -> notices.Notice:
    return await notices.send_notice(
        db, cast(Any, ch), "alice", settings, key=key, title=f"\U0001f440 {key} happened", now=now
    )


async def test_daily_cap_holds_the_rest_for_the_briefing(db: sqlite3.Connection) -> None:
    s = make_settings(proactive_per_day=2)
    ch = Recorder()
    outs = [await notice(db, s, ch, f"k{i}", NOW + timedelta(hours=i)) for i in range(3)]
    assert [o.outcome for o in outs] == ["sent", "sent", "capped_day"]
    assert [m.text for m in ch.sent] == ["\U0001f440 k0 happened", "\U0001f440 k1 happened"]
    held = items.get(db, "core", "notice/k2")
    assert held is not None and held.urgency == "low" and held.changed  # the briefing's
    assert notices.sent_today(db, NOW, s.timezone) == 2
    nxt = NOW + timedelta(days=1)  # a new local day
    assert (await notice(db, s, ch, "k3", nxt)).outcome == "sent"


async def test_hourly_cap_quiet_hours_and_pause_hold_for_the_pulse(db: sqlite3.Connection) -> None:
    s = load_settings(
        dry_run=False,
        timezone="America/New_York",
        brain={"proactive_per_day": 10},
        extensions={"notify": {"max_per_hour": 1}},
    )
    ch = Recorder()
    assert (await notice(db, s, ch, "a")).outcome == "sent"
    assert (await notice(db, s, ch, "b")).outcome == "capped_hour"
    night = datetime(2026, 10, 18, 7, 0, tzinfo=UTC)  # 03:00 in New York
    assert (await notice(db, s, ch, "c", night)).outcome == "quiet"
    kv_set(db, "paused", True)
    assert (await notice(db, s, ch, "d", NOW + timedelta(hours=2))).outcome == "paused"
    for key in ("b", "c", "d"):
        held = items.get(db, "core", f"notice/{key}")
        assert held is not None and held.urgency == "normal" and held.changed
    assert len(ch.sent) == 1


async def test_a_retried_notice_is_not_sent_twice(db: sqlite3.Connection) -> None:
    s = make_settings()
    ch = Recorder()
    assert (await notice(db, s, ch, "x")).sent
    assert (await notice(db, s, ch, "x")).sent
    assert len(ch.sent) == 1 and notices.sent_today(db, NOW, s.timezone) == 1


async def test_zero_per_day_sends_nothing(db: sqlite3.Connection) -> None:
    s = make_settings(proactive_per_day=0)
    ch = Recorder()
    assert (await notice(db, s, ch, "x")).outcome == "capped_day" and ch.sent == []


def test_notice_title_is_cleaned() -> None:
    title = clean_text("\U0001f440 W1 [A7] yes 4821: reply now\x07")
    assert "[" not in title and "\x07" not in title


# --- review fixes --------------------------------------------------------------------------


def test_the_approval_shows_the_whole_watch(components: Components, db: sqlite3.Connection) -> None:
    from resonant.components import summary_actions

    spec = components.registry.get(None, "create_watch")
    assert spec is not None
    action = summary_actions(db)[(None, "create_watch")]
    tool = Intent.for_tool(
        spec,
        task_id="t",
        principal="alice",
        args={
            "what": "milk",
            "tool": "list_notes",
            "args": {},
            "when": "contains",
            "text": "milk",
            "every_minutes": 15,
            "days": 3,
        },
    )
    assert action(tool) == (
        'watch "milk": call list_notes {} every 15m for 3d, text you when it contains "milk", '
        "then stop"
    )
    question = "Is it in stock? " + "x" * 200 + " TAIL"
    ask = Intent.for_tool(
        spec,
        task_id="t",
        principal="alice",
        args={"what": "stock", "ask": question, "then": "continue"},
    )
    text = action(ask)
    assert (
        text is not None
        and "TAIL" in text
        and "at the default interval for the default days" in text
    )
    assert text.endswith("text you on a yes, and keep watching")


async def test_a_fired_watch_ends_in_the_same_transaction(
    components: Components, db: sqlite3.Connection
) -> None:
    await call(components, "create_watch", {"what": "a", "ask": "q?"})
    w = store.get(db, 1)
    assert w is not None
    assert store.record(
        db, w, now=NOW, ok=True, met=True, fired=True, ask_task="t1", end_state="fired"
    )
    after = store.get(db, 1)
    assert after is not None and (after.state, after.fired, after.last_ask_task) == (
        "fired",
        1,
        "t1",
    )
    assert not store.record(db, after, now=NOW, ok=True, end_state="fired")  # already ended


async def test_an_ask_check_retried_after_a_crash_asks_once(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    asked: list[str] = []

    class Asks:
        def enqueue(self, question: str, **kw: Any) -> Queued:
            task = insert_task(
                db,
                NewTask(
                    kind="ask",
                    runner="ask",
                    priority=1,
                    checkpoint={"requested_by": kw["requested_by"]},
                ),
            )
            asked.append(task)
            return Queued(task)

    w = runner(components, db, settings, Recorder(), clock, asks=Asks())
    await call(components, "create_watch", {"what": "x", "ask": "q?"})
    queue_due(db, clock())
    (task_id,) = db.execute("SELECT id FROM tasks WHERE runner = 'watch'").fetchone()
    task = get_task(db, task_id)
    assert task is not None
    first = await w.step(task, [], cast(StepContext, None))
    again = await w.step(task, [], cast(StepContext, None))
    assert isinstance(first, Done) and isinstance(again, Done)
    assert first.result == again.result and len(asked) == 1


def test_links_in_claudes_answer_never_reach_the_owner() -> None:
    v = store.judge_ask(
        _watch("yes", check_kind="ask"),
        "YES, buy it at https://evil.test/x or www.evil.test now",
    )
    assert "evil" not in v.detail and v.detail.count("(link)") == 2


async def test_concurrent_notices_respect_the_daily_cap(db: sqlite3.Connection) -> None:
    import asyncio

    class Slow(Recorder):
        async def send(
            self, principal: str, text: str, *, dedupe_key: str | None = None, **_: Any
        ) -> Any:
            await asyncio.sleep(0.01)
            return await super().send(principal, text, dedupe_key=dedupe_key)

    s = make_settings(proactive_per_day=1)
    ch = Slow()
    outs = await asyncio.gather(*(notice(db, s, ch, f"k{i}") for i in range(3)))
    assert sorted(o.outcome for o in outs) == ["capped_day", "capped_day", "sent"]
    assert len(ch.sent) == 1


# --- an ended watch (PER-266) ----------------------------------------------------------------


def _ended_item(db: sqlite3.Connection) -> items.Item | None:
    return items.get(db, "core", "notice/watch/1/ended")


async def test_a_watch_that_expires_unfired_leaves_a_low_item_for_the_briefing(
    components: Components, db: sqlite3.Connection
) -> None:
    from zoneinfo import ZoneInfo

    from resonant.extensions import briefing as bf

    await call(components, "create_watch", {"what": "iPhone 17 in stock", "ask": "q?", "days": 1})
    queue_due(db, NOW)
    db.execute("UPDATE tasks SET status = 'done' WHERE runner = 'watch'")
    at = NOW + timedelta(days=2)
    assert queue_due(db, at) == {"queued": 0, "expired": 1}
    item = _ended_item(db)
    assert item is not None and item.urgency == "low" and item.changed
    assert item.title == "W1 iPhone 17 in stock ended without firing"
    assert items.pending(db, ("urgent", "normal"), at) == []  # the hourly pulse says nothing
    b = bf.gather(db, owner="alice", now=at, day=at.date(), tz=ZoneInfo("UTC"))
    assert [i.key for i in b.updates] == ["notice/watch/1/ended"]
    assert "Updates:\nW1 iPhone 17 in stock ended without firing" in bf.render(b)
    assert queue_due(db, at + timedelta(hours=1)) == {"queued": 0, "expired": 0}  # once
    # it expires with its TTL: gone from the briefing a day later, never said
    later = at + timedelta(seconds=notices.TTL_S + 60)
    assert items.open_items(db, later) == [] and items.pending(db, ("low",), later) == []
    assert (
        bf.gather(db, owner="alice", now=later, day=later.date(), tz=ZoneInfo("UTC")).updates == ()
    )


async def test_a_check_that_finds_its_watch_expired_leaves_the_item_too(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    ch = Recorder()
    w = runner(components, db, settings, ch, clock)
    spec: dict[str, Any] = {"what": "milk", "tool": "list_notes", "args": {}, "when": "contains"}
    await call(components, "create_watch", {**spec, "text": "milk", "days": 1})
    queue_due(db, NOW)
    (task_id,) = db.execute("SELECT id FROM tasks WHERE runner = 'watch'").fetchone()
    task = get_task(db, task_id)
    assert task is not None
    clock.now = NOW + timedelta(days=2)
    out = await w.step(task, [], cast(StepContext, None))
    assert isinstance(out, Done) and out.result["status"] == "expired" and ch.sent == []
    item = _ended_item(db)
    assert item is not None and item.title == "W1 milk ended without firing"


async def test_a_watch_that_fired_ends_without_the_item(
    components: Components, db: sqlite3.Connection
) -> None:
    await call(components, "create_watch", {"what": "a", "ask": "q?", "days": 1})
    db.execute("UPDATE brain_watches SET fired = 1")  # then="continue": it fired before
    assert queue_due(db, NOW + timedelta(days=2)) == {"queued": 0, "expired": 1}
    assert _ended_item(db) is None
    await call(components, "create_watch", {"what": "b", "ask": "q?", "days": 1})
    await call(components, "cancel_watch", {"id": "W2"})  # cancelled: the owner knows
    assert items.get(db, "core", "notice/watch/2/ended") is None


def test_the_briefing_reads_notices_by_their_prefix() -> None:
    from resonant.extensions import briefing as bf

    assert bf.NOTICE_PREFIX == notices.KEY_PREFIX


# --- origin and follow-up (PER-346) ---------------------------------------------------------

THREAD_REF = {"channel": "slack", "principal": "alice", "thread": "C1:1700.1", "ref": "C1:1700.2"}
PR_WATCH: dict[str, Any] = {
    "what": "PR 159 green",
    "tool": "list_notes",
    "args": {},
    "when": "contains",
    "match_text": "green",
    "every_minutes": 15,
}


def asking_task(db: sqlite3.Connection, channel_ref: dict[str, Any] | None) -> CallContext:
    """The brain turn that calls ``create_watch``: a task with that ``channel_ref``."""
    task = insert_task(
        db,
        NewTask(kind="ask", runner="ask", priority=1, principal="alice", channel_ref=channel_ref),
    )
    return CallContext(secrets={}, idempotency_key=f"k-{task}", task_id=task)


async def create_in(
    c: Components, db: sqlite3.Connection, args: dict[str, Any], ref: dict[str, Any] | None
) -> dict[str, Any]:
    handler = cast(ContextHandler, c.handlers[(None, "create_watch")])
    return await handler(args, asking_task(db, ref))


class FollowUps:
    """A fake ask runner that inserts the task like the real one (its checkpoint marker)."""

    def __init__(self, db: sqlite3.Connection, refuse: str | None = None) -> None:
        self.db = db
        self.refuse = refuse
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, question: str, **kw: Any) -> Queued | AskRefused:
        self.calls.append({"question": question, **kw})
        if self.refuse is not None:
            return AskRefused(self.refuse, "busy")
        cp = {"question": question, "tainted": kw["tainted"], FOLLOW_UP_KEY: kw["follow_up"]}
        task = insert_task(
            self.db,
            NewTask(kind="ask", runner="ask", priority=1, principal=kw["principal"],
                    checkpoint=cp, channel_ref=kw["channel_ref"]),
        )  # fmt: skip
        return Queued(task)


async def test_a_watch_keeps_its_origin_and_notices_go_there(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    ch = Recorder()
    w = runner(components, db, settings, ch, clock)
    out = await create_in(components, db, PR_WATCH, THREAD_REF)
    assert out["origin"] == "thread" and out["then"] == "stop" and out["follow_up"] is None
    assert out["check"]["match_text"] == "green"
    watch = store.get(db, 1)
    assert watch is not None and watch.channel_ref == THREAD_REF
    assert watch.conversation == "C1:1700.1"
    await call(components, "add_note", {"text": "checks green"})
    await run_due(db, w, clock)
    (sent,) = ch.sent
    assert sent.principal == "alice"
    assert sent.thread == MessageRef(channel="slack", ref="C1:1700.2", thread="C1:1700.1")


async def test_no_origin_or_someone_elses_thread_falls_back_to_the_dm(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    ch = Recorder()
    w = runner(components, db, settings, ch, clock)
    await create_in(components, db, PR_WATCH, None)  # an iMessage turn, or no thread
    await create_in(components, db, {**PR_WATCH, "what": "b"}, {**THREAD_REF, "principal": "x"})
    listed = await call(components, "list_watches")
    assert [x["origin"] for x in listed["watches"]] == ["direct", "direct"]
    await call(components, "add_note", {"text": "green"})
    await run_due(db, w, clock)
    assert len(ch.sent) == 2 and all(s.thread is None for s in ch.sent)


async def test_default_behaviour_is_unchanged_without_a_task(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    asks = FollowUps(db)
    ch = Recorder()
    w = runner(components, db, settings, ch, clock, asks=asks)
    await call(components, "create_watch", {**PR_WATCH, "match_text": None, "text": "green"})
    watch = store.get(db, 1)
    assert watch is not None and (watch.then, watch.channel_ref, watch.follow_up) == (
        "stop",
        None,
        None,
    )
    await call(components, "add_note", {"text": "green"})
    await run_due(db, w, clock)
    (sent,) = ch.sent
    assert sent.thread is None and asks.calls == []  # no follow-up without then=follow_up
    assert check_label(watch) == "watch W1"


async def test_follow_up_queues_one_tainted_ask_as_the_creator_in_the_origin(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    asks = FollowUps(db)
    ch = Recorder()
    w = runner(components, db, settings, ch, clock, asks=asks)
    args = {**PR_WATCH, "then": "follow_up", "follow_up": "merge PR 159 if green, else say why"}
    out = await create_in(components, db, args, THREAD_REF)
    assert out["then"] == "follow_up" and out["follow_up"] == args["follow_up"]
    watch = store.get(db, 1)
    assert watch is not None and check_label(watch) == "watch W1 (in a thread, then follow up)"
    (first,) = await run_due(db, w, clock)
    assert first.result["status"] == "checked" and asks.calls == []
    await call(components, "add_note", {"text": "checks green"})
    clock.now += timedelta(minutes=15)
    (out2,) = await run_due(db, w, clock)
    assert out2.result["status"] == "fired"
    (q,) = asks.calls
    assert q["question"] == (
        'Watch W1 (PR 159 green) fired: saw "green". '
        "You asked yourself to: merge PR 159 if green, else say why"
    )
    assert (q["principal"], q["channel_ref"], q["tools"], q["tainted"], q["taint_hops"]) == (
        "alice",
        THREAD_REF,
        True,
        True,
        0,
    )
    assert q["follow_up"] == "watch/1/1" and "requested_by" not in q
    (sent,) = ch.sent
    assert sent.thread is not None and "couldn't follow up" not in sent.text
    after = store.get(db, 1)
    assert after is not None and after.state == "fired"
    assert after.follow_up_task is not None
    clock.now += timedelta(minutes=15)
    assert await run_due(db, w, clock) == []  # the watch ended after its follow-up


async def test_a_retried_fire_never_queues_the_follow_up_twice(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    asks = FollowUps(db)
    w = runner(components, db, settings, Recorder(), clock, asks=asks)
    args = {**PR_WATCH, "then": "follow_up", "follow_up": "merge it"}
    await create_in(components, db, args, THREAD_REF)
    await call(components, "add_note", {"text": "green"})
    watch = store.get(db, 1)
    assert watch is not None
    v = store.judge_tool(watch, {"notes": ["green"]})
    # a crash after the follow-up was queued but before the hit was recorded: the retry
    # judges the same still-active watch again
    assert watch.state == "active"
    first = w._follow_up(watch, v.detail, "watch/1/1")  # pyright: ignore[reportPrivateUsage]
    again = w._follow_up(watch, v.detail, "watch/1/1")  # pyright: ignore[reportPrivateUsage]
    assert first is None and again is None and len(asks.calls) == 1
    queued = db.execute(
        "SELECT COUNT(*) FROM tasks WHERE json_extract(checkpoint, '$.watch_follow_up') = ?",
        ("watch/1/1",),
    ).fetchone()[0]
    assert queued == 1


async def test_a_refused_follow_up_is_said_in_the_notice(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    ch = Recorder()
    w = runner(components, db, settings, ch, clock, asks=FollowUps(db, refuse="Claude is busy."))
    await create_in(components, db, {**PR_WATCH, "follow_up": "merge it"}, THREAD_REF)
    await call(components, "add_note", {"text": "green"})
    await run_due(db, w, clock)
    (sent,) = ch.sent
    assert sent.text.endswith("(couldn't follow up: Claude is busy.)")
    watch = store.get(db, 1)
    assert watch is not None and watch.state == "fired" and watch.follow_up_task is None


@pytest.mark.parametrize(
    ("args", "why"),
    [
        ({"match_text": "Merge PR 159 once checks pass. Then tell Alice."}, "then: follow_up"),
        ({"match_text": "x" * 61}, "not a sentence"),
        ({"match_text": None, "text": "Merge it when it is green."}, "then: follow_up"),
        ({"text": "red"}, "give match_text only"),
        ({"then": "follow_up"}, "needs follow_up"),
        ({"then": "continue", "follow_up": "merge it"}, "goes with then=follow_up"),
    ],
)
async def test_create_refuses_sentences_and_bad_follow_ups(
    components: Components, args: dict[str, Any], why: str
) -> None:
    full = {k: v for k, v in {**PR_WATCH, **args}.items() if v is not None}
    with pytest.raises(ToolFailed, match=why):
        await call(components, "create_watch", full)


@pytest.mark.parametrize(
    ("text", "sentence"),
    [
        ("MERGED", False),
        ("v1.2 released", False),
        ('"conclusion":"success"', False),
        ("Add to bag", False),
        ("merge it.", True),
        ("Checks pass. Merge.", True),
        ("x" * 61, True),
    ],
)
def test_sentence_like(text: str, sentence: bool) -> None:
    assert store.sentence_like(text) is sentence


def test_the_approval_shows_the_follow_up(components: Components) -> None:
    spec = components.registry.get(None, "create_watch")
    assert spec is not None
    args = {**PR_WATCH, "then": "follow_up", "follow_up": "merge PR 159"}
    intent = Intent.for_tool(spec, task_id="t", principal="alice", origin="inferred", args=args)
    assert watch_tools.summary_action(intent) == (
        'watch "PR 159 green": call list_notes {} every 15m for the default days, '
        'text you when it contains "green", then follow up: "merge PR 159"'
    )


def test_migration_037_keeps_old_watches_and_their_ids(tmp_path: Any) -> None:
    from resonant.store.db import _migrations, migrate  # pyright: ignore[reportPrivateUsage]

    raw = sqlite3.connect(tmp_path / "v36.db", isolation_level=None)
    raw.row_factory = sqlite3.Row
    every = _migrations()
    assert migrate(raw, [m for m in every if m[0] <= 36]) == 36
    cols = "principal, what, check_kind, op, value, interval_s, created_at, expires_at, next_at"
    for what in ("a", "b"):
        raw.execute(
            f"INSERT INTO brain_watches ({cols}) VALUES ('alice', ?, 'tool', 'contains', 'x', "
            "900, '2026-11-01T00:00:00+00:00', '2026-11-08T00:00:00+00:00', "
            "'2026-11-01T00:00:00+00:00')",
            (what,),
        )
    raw.execute("DELETE FROM brain_watches WHERE what = 'b'")  # the high-water mark is 2
    assert migrate(raw, every) == 38
    old = store.get(raw, 1)
    assert old is not None and (old.what, old.then, old.channel_ref, old.follow_up) == (
        "a",
        "stop",
        None,
        None,
    )
    new = store.create(raw, principal="alice", what="c", check_kind="ask", op="yes",
                       interval_s=3600, now=NOW, expires_at=NOW, question="q?",
                       then="follow_up", follow_up="do it")  # fmt: skip
    assert new.id == 3 and new.then == "follow_up" and new.follow_up == "do it"


async def test_a_follow_up_never_uses_someone_elses_thread(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    asks = FollowUps(db)
    w = runner(components, db, settings, Recorder(), clock, asks=asks)
    other = {**THREAD_REF, "principal": "someone"}
    await create_in(components, db, {**PR_WATCH, "follow_up": "merge it"}, other)
    await call(components, "add_note", {"text": "green"})
    await run_due(db, w, clock)
    (q,) = asks.calls
    assert q["channel_ref"] is None and q["principal"] == "alice"

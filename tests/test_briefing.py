"""Core daily briefing (P3-13): content, sections, the Updates items, schedule, once a day.

The real store, components and Scheduler over a temp home, on a fake clock, with a fake
channel that dedupes by key like the iMessage adapter.
"""

from __future__ import annotations

import contextlib
import functools
import itertools
import sqlite3
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from voice_script import Script, scripted_voice

from resonant import killswitch
from resonant.api import DaemonState
from resonant.components import Components, build_components
from resonant.config import Settings, load_settings, resonant_home
from resonant.executor import CallContext
from resonant.extensions import briefing as bf
from resonant.extensions.process import FakeExtProcess
from resonant.extensions.scheduler import CORE_JOBS, LAST_TICK_KEY, CoreDeps, Scheduler
from resonant.gateway.channel import ApprovalPrompt, MessageRef, SelfTestResult, Submit
from resonant.loop import Loop, SlotPool
from resonant.loop.types import PRIORITY_OWNER, NewTask, TaskStatus, WaitReason
from resonant.ops.fakes import FakeOpsRunner
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store import approvals as ap
from resonant.store import extensions as ext_store
from resonant.store import pulse as pulse_store
from resonant.store import runs as runs_store
from resonant.store import tasks as repo
from resonant.store.db import atomic, iso, kv_set, open_db
from resonant.tools.reminders import add_reminder
from resonant_sdk import Effect, Level

OWNER = "justin"
PRINCIPALS = """\
principals:
  justin:
    role: owner
    identities: ["imessage:+15551234567"]
"""
TZ = "America/Los_Angeles"
LA = ZoneInfo(TZ)


def local(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """``hour:minute`` America/Los_Angeles on 2026-<month>-<day>, in UTC."""
    return datetime(2026, month, day, hour, minute, tzinfo=LA).astimezone(UTC)


T0 = local(10, 13, 8)  # 15:00 UTC (PDT)
CTX = CallContext(secrets={}, idempotency_key="t", task_id="t")


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class FakeChannel:
    """Records sends, deduping by key like the real adapter."""

    name: str = "imessage"
    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    keys: set[str] = field(default_factory=set[str])

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
        if dedupe_key is None or dedupe_key not in self.keys:
            self.sent.append((principal, text, dedupe_key))
            if dedupe_key is not None:
                self.keys.add(dedupe_key)
        return MessageRef("imessage", f"out:{len(self.sent)}")

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        raise AssertionError("not used")

    async def self_test(self) -> SelfTestResult:
        return SelfTestResult(True, "fake")

    def texts(self) -> list[str]:
        return [t for _, t, _ in self.sent]


@dataclass
class Harness:
    settings: Settings
    db: sqlite3.Connection
    clock: Clock
    components: Components
    channel: FakeChannel
    sched: Scheduler | None = None

    def scheduler(self, *, at: datetime | None = None) -> Scheduler:
        """A scheduler with only the briefing core job (a daemon start at ``at``)."""
        if at is not None:
            self.clock.now = at
        self.sched = Scheduler.from_components(
            self.settings,
            self.db,
            self.components,
            started_at=self.clock.now,
            channel=self.channel,
            core_jobs=(functools.partial(bf.briefing_job, clock=self.clock),),
            clock=self.clock,
        )
        self.sched.jobs = tuple(j for j in self.sched.jobs if j.core)
        return self.sched

    async def tick(self, at: datetime) -> None:
        assert self.sched is not None
        self.clock.now = at
        await self.sched.tick(at)
        await self.sched.drain()

    def deps(self, channel: FakeChannel | None = None) -> CoreDeps:
        return CoreDeps(self.settings, self.db, self.components, channel, OWNER)

    def audits(self, action: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,)
        ).fetchall()


_HOMES = itertools.count()


@contextlib.asynccontextmanager
async def harness(
    *, briefing: dict[str, Any] | None = None, at: datetime = T0
) -> AsyncGenerator[Harness]:
    home = resonant_home() / f"h{next(_HOMES)}"  # each harness its own store
    home.mkdir(parents=True)
    settings = load_settings(home=home, dry_run=False, timezone=TZ, briefing=briefing or {})
    settings.principals_path.write_text(PRINCIPALS)
    db = open_db(settings.db_path)
    clock = Clock(at)
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1), clock=clock)
    state = DaemonState(settings, db, loop, started_at=at)
    c = build_components(
        settings,
        db,
        state,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(FakeKeychain()),
        ext_runner=FakeExtProcess(),
    )
    with atomic(db):
        kv_set(db, "paused", False)
    h = Harness(settings, db, clock, c, FakeChannel())
    try:
        yield h
    finally:
        if h.sched is not None:
            await h.sched.aclose()
        db.close()


def remind(db: sqlite3.Connection, at: datetime, text: str, *, now: datetime) -> None:
    add_reminder(db, {"at": iso(at), "text": text}, owner=OWNER, now=now, tz=TZ)


def task(
    db: sqlite3.Connection,
    status: TaskStatus,
    *,
    kind: str = "chat",
    wait_reason: WaitReason | None = None,
) -> str:
    task_id = repo.insert_task(
        db,
        NewTask(
            kind=kind,
            runner="local",
            priority=PRIORITY_OWNER,
            principal=OWNER,
            checkpoint={"title": "chat: SECRET-TITLE", "body": "SECRET-BODY"},
        ),
    )
    if status != "queued":
        repo.set_status(db, task_id, status, wait_reason=wait_reason)
    return task_id


def approval(db: sqlite3.Connection, code: str, now: datetime) -> None:
    task_id = task(db, "waiting", wait_reason="approval")
    ap.insert_approval(
        db,
        ap.NewApproval(
            task_id=task_id,
            intent_hash=f"h-{code}",
            tool="restart_resonant",
            effect=Effect.WRITE,
            level=Level.L2,
            summary="restart Resonant",
            approvers=(OWNER,),
            code=code,
            expires_at=now + timedelta(hours=1),
        ),
        now,
    )


def degrade(db: sqlite3.Connection, check: str) -> None:
    with atomic(db):
        kv_set(db, f"health.{check}", {"state": "degraded", "detail": "SECRET-DETAIL"})


# --- render: pure --------------------------------------------------------------------------


def test_an_empty_briefing_is_one_short_line() -> None:
    assert bf.render(bf.Briefing()) == bf.NOTHING == "Morning. Nothing for today."
    assert bf.sections(bf.Briefing()) == []
    days = [date(2026, 10, 13) + timedelta(days=n) for n in range(len(bf.NOTHING_LINES))]
    said = [bf.render(bf.Briefing(day=d)) for d in days]
    assert sorted(said) == sorted(bf.NOTHING_LINES)  # a few variants, one per day in turn
    assert all("\n" not in s and len(s) <= 40 and s.startswith("Morning.") for s in said)
    assert bf.render(bf.Briefing(day=days[0])) == bf.nothing_line(days[0])  # same all day


def test_empty_workers_and_approvals_sections_are_left_out() -> None:
    assert bf.render(bf.Briefing(approvals=(), workers=(), day=date(2026, 10, 13))) == (
        bf.nothing_line(date(2026, 10, 13))
    )
    assert "Workers:" not in bf.render(bf.Briefing(running=1))
    assert bf.sections(bf.Briefing(running=1)) == ["work"]


def test_example_briefing_and_empty_sections_are_omitted() -> None:
    b = bf.Briefing(
        reminders=(("9:30am", "stretch"), ("3:00pm", "call mom")),
        running=1,
        waiting=("approval", "approval"),
        approvals=("A7",),
        degraded=("imessage",),
    )
    assert bf.render(b) == (
        "⏰ 2 reminders: 9:30am stretch, 3:00pm call mom\n"
        "▶ 1 running, 2 waiting (approval)\n"
        "✋ 1 approval pending (A7)\n"
        "⚠ imessage degraded"
    )
    only = bf.Briefing(approvals=("A7", "C3"), queued=2)
    assert bf.render(only) == "▶ 2 queued\n✋ 2 approvals pending (A7, C3)"
    assert bf.sections(only) == ["work", "approvals"]


def test_briefing_fits_one_chunk() -> None:
    b = bf.Briefing(
        reminders=tuple((f"{h}:00am", "x" * 200) for h in range(1, 12)),
        waiting=("approval",) * 20,
        approvals=tuple(f"A{i}" for i in range(20)),
        degraded=("model", "imessage", "loop", "store"),
    )
    text = bf.render(b)
    assert len(text) <= bf.MAX_CHARS
    assert "+8 more" in text  # 3 reminders shown
    assert "(A0, A1, A2, A3, A4, …)" in text


# --- gather: from the store ----------------------------------------------------------------


async def test_sections_come_from_the_store_in_local_time_without_bodies() -> None:
    async with harness() as h:
        now = local(10, 13, 7, 30)
        remind(h.db, local(10, 13, 15), "call mom", now=now)
        remind(h.db, local(10, 13, 9, 30), "stretch", now=now)
        remind(h.db, local(10, 14, 0, 30), "tomorrow, not today", now=now)  # 07:30 UTC on 14
        remind(h.db, local(10, 13, 23, 30), "late tonight", now=now)  # 06:30 UTC on the 14th
        task(h.db, "running")
        task(h.db, "waiting", wait_reason="human_reply")
        task(h.db, "queued")
        task(h.db, "done")
        approval(h.db, "A7", now)
        degrade(h.db, "imessage")
        out = await bf.send_briefing(h.db, h.channel, OWNER, tz=TZ, now=now)
        ((who, text, key),) = h.channel.sent
        assert (who, key) == (OWNER, "briefing:2026-10-13")
        assert text == (
            "⏰ 3 reminders: 9:30am stretch, 3:00pm call mom, 11:30pm late tonight\n"
            "▶ 1 running, 2 waiting (approval, human_reply), 1 queued\n"
            "✋ 1 approval pending (A7)\n"
            "⚠ imessage degraded"
        )
        assert out == {
            "sent": True,
            "date": "2026-10-13",
            "sections": ["reminders", "work", "approvals", "health"],
            "chars": len(text),
        }
        assert "SECRET" not in text and "SECRET" not in repr(out)


async def test_github_summary_is_an_updates_item_not_a_hard_coded_line() -> None:
    async with harness() as h:
        with atomic(h.db):
            pulse_store.upsert(
                h.db,
                "github",
                "github/summary",
                "3 open PRs, 1 failing CI, 2 awaiting your review",
                "low",
                {"open": 3, "failing": 1, "review_requested": 2},
                False,
                30 * 3600,
                T0,
            )
        out = await bf.send_briefing(h.db, h.channel, OWNER, tz=TZ, now=T0)
        assert h.channel.texts() == [
            "Updates:\n[github] 3 open PRs, 1 failing CI, 2 awaiting your review"
        ]
        assert out["sections"] == ["updates"]


# --- the job: schedule, catch-up, once a day ----------------------------------------------


def test_briefing_is_a_core_job() -> None:
    assert bf.briefing_job in CORE_JOBS


async def test_disabled_or_unreachable_is_not_scheduled() -> None:
    async with harness(briefing={"enabled": False}) as h:
        assert bf.briefing_job(h.deps(h.channel)) is None
        assert h.scheduler().jobs == ()
    async with harness() as h:
        assert bf.briefing_job(h.deps(None)) is None  # iMessage off
        deps = CoreDeps(h.settings, h.db, h.components, h.channel, None)
        assert bf.briefing_job(deps) is None  # no principals.yaml
        job = bf.briefing_job(h.deps(h.channel))
        assert job is not None
        assert (job.id, job.schedule, job.catchup) == ("briefing", "0 8 * * *", bf.CATCHUP)
    async with harness(briefing={"at": "06:45"}) as h:
        job = bf.briefing_job(h.deps(h.channel))
        assert job is not None and job.schedule == "45 6 * * *"


async def test_e13_fires_at_eight_local_within_a_tick() -> None:
    async with harness(at=local(10, 13, 7, 59)) as h:
        s = h.scheduler()
        assert s.next_due(h.clock.now) == T0
        await h.tick(local(10, 13, 7, 59))
        assert h.channel.sent == []
        await h.tick(T0 + timedelta(seconds=h.settings.loop.tick_s))
        empty = bf.nothing_line(date(2026, 10, 13))
        assert h.channel.sent == [(OWNER, empty, "briefing:2026-10-13")]
        (run,) = h.audits("job.run")
        assert run["extension"] == "core"
        assert h.audits("gate.evaluate") == []
        state = ext_store.get_job(h.db, "core", "briefing")
        assert state is not None and state.last_outcome == "ok"


async def test_e13_follows_local_time_across_dst() -> None:
    # 2026-11-01: PDT -> PST. 08:00 local is 15:00 UTC the day before, 16:00 UTC on the day.
    async with harness(at=local(10, 31, 7)) as h:
        s = h.scheduler()
        assert s.next_due(h.clock.now) == datetime(2026, 10, 31, 15, tzinfo=UTC)
        await h.tick(datetime(2026, 10, 31, 15, tzinfo=UTC))
        assert s.next_due(h.clock.now) == datetime(2026, 11, 1, 16, tzinfo=UTC)
        await h.tick(datetime(2026, 11, 1, 15, tzinfo=UTC))  # 07:00 PST: not yet
        assert len(h.channel.sent) == 1
        await h.tick(datetime(2026, 11, 1, 16, tzinfo=UTC))
        assert [k for _, _, k in h.channel.sent] == ["briefing:2026-10-31", "briefing:2026-11-01"]
    # 2027-03-14: PST -> PDT. 08:00 local is 16:00 UTC the day before, 15:00 UTC on the day.
    async with harness(at=datetime(2027, 3, 13, 15, tzinfo=UTC)) as h:
        s = h.scheduler()
        assert s.next_due(h.clock.now) == datetime(2027, 3, 13, 16, tzinfo=UTC)
        await h.tick(datetime(2027, 3, 13, 16, tzinfo=UTC))
        assert s.next_due(h.clock.now) == datetime(2027, 3, 14, 15, tzinfo=UTC)
        await h.tick(datetime(2027, 3, 14, 15, tzinfo=UTC))
        assert [k for _, _, k in h.channel.sent] == ["briefing:2027-03-13", "briefing:2027-03-14"]


async def test_e13_once_per_local_day_across_restart_and_catch_up() -> None:
    async with harness() as h:
        h.scheduler()
        await h.tick(T0)
        await h.tick(T0 + timedelta(minutes=1))
        h.scheduler(at=T0 + timedelta(minutes=5))  # a restart
        await h.tick(T0 + timedelta(minutes=5))
        # The ext_jobs row lost (a crash before it was recorded): the dedupe key holds.
        with atomic(h.db):
            h.db.execute("DELETE FROM ext_jobs")
        h.scheduler(at=T0 + timedelta(minutes=6))
        await h.tick(T0 + timedelta(minutes=6))
        assert len(h.channel.sent) == 1
        # The scheduled run repeated later that day (no due_at): not sent again.
        job = bf.briefing_job(h.deps(h.channel), clock=h.clock)
        assert job is not None
        await job.run({}, CTX)
        assert len(h.channel.sent) == 1


async def test_catch_up_within_three_hours_of_downtime_sends_once() -> None:
    async with harness() as h:
        with atomic(h.db):
            kv_set(h.db, LAST_TICK_KEY, iso(local(10, 13, 6)))
            ext_store.mark_due(h.db, "core", "briefing", local(10, 12, 8))
        h.scheduler(at=local(10, 13, 10, 30))
        await h.tick(local(10, 13, 10, 30))
        assert [k for _, _, k in h.channel.sent] == ["briefing:2026-10-13"]
        (run,) = h.audits("job.run")
        assert '"late": true' in run["detail"]
        h.scheduler(at=local(10, 13, 10, 31))
        await h.tick(local(10, 13, 10, 31))
        assert len(h.channel.sent) == 1
    async with harness() as h:
        with atomic(h.db):
            kv_set(h.db, LAST_TICK_KEY, iso(local(10, 13, 6)))
            ext_store.mark_due(h.db, "core", "briefing", local(10, 12, 8))
        h.scheduler(at=local(10, 13, 11, 30))  # past the 3h window
        await h.tick(local(10, 13, 11, 30))
        assert h.channel.sent == []
        assert len(h.audits("job.missed")) == 1


async def test_e5_once_a_day_across_midnight() -> None:
    async with harness() as h:
        job = bf.briefing_job(h.deps(h.channel), clock=h.clock)
        assert job is not None
        h.clock.now = local(10, 13, 23, 59)
        await job.run({}, CTX)
        h.clock.now = local(10, 14, 0, 1)  # a new local day, still the 14th 07:01 UTC
        await job.run({}, CTX)
        h.scheduler(at=local(10, 14, 7, 59))
        await h.tick(local(10, 14, 8))  # the 14th's was already sent
        await h.tick(local(10, 15, 8))
        assert [k for _, _, k in h.channel.sent] == [
            "briefing:2026-10-13",
            "briefing:2026-10-14",
            "briefing:2026-10-15",
        ]


async def test_skipped_while_paused() -> None:
    async with harness() as h:
        killswitch.engage(h.db, actor="test")
        h.scheduler()
        await h.tick(T0)
        assert h.channel.sent == []
        assert len(h.audits("job.skipped_paused")) == 1
        out = await bf.send_briefing(h.db, h.channel, OWNER, tz=TZ, now=T0)
        assert out == {"sent": False, "date": "2026-10-13", "skipped": "paused"}
        assert h.channel.sent == []


async def test_on_demand_never_uses_up_the_scheduled_one() -> None:
    async with harness() as h:
        out = await bf.run_briefing_now(h.deps(h.channel), now=local(10, 13, 7))
        assert out["sent"] is True
        h.scheduler(at=local(10, 13, 7, 59))
        await h.tick(T0)
        assert [k for _, _, k in h.channel.sent] == [
            "briefing:manual:2026-10-13:0700",
            "briefing:2026-10-13",
        ]


async def test_on_demand_sends_at_most_once_a_minute() -> None:
    async with harness() as h:
        deps = h.deps(h.channel)
        await bf.run_briefing_now(deps, now=local(10, 13, 9, 15))
        await bf.run_briefing_now(deps, now=local(10, 13, 9, 15) + timedelta(seconds=40))
        await bf.run_briefing_now(deps, now=local(10, 13, 9, 16))
        assert [k for _, _, k in h.channel.sent] == [
            "briefing:manual:2026-10-13:0915",
            "briefing:manual:2026-10-13:0916",
        ]


async def test_on_demand_sends_nothing_while_paused_or_unreachable() -> None:
    async with harness() as h:
        killswitch.engage(h.db, actor="test")
        out = await bf.run_briefing_now(h.deps(h.channel), now=T0)
        assert out == {"sent": False, "date": "2026-10-13", "skipped": "paused"}
        assert await bf.run_briefing_now(h.deps(None), now=T0) == {
            "sent": False,
            "skipped": "unreachable",
        }
        assert h.channel.sent == []


# --- Claude's voice (PER-273) --------------------------------------------------------------


async def run_job(h: Harness, due: datetime, *, now: datetime | None = None) -> dict[str, Any]:
    """The scheduled job (the path that hands ``components.voice`` to the briefing)."""
    job = bf.briefing_job(h.deps(h.channel), clock=h.clock)
    assert job is not None
    h.clock.now = now or due
    return await job.run({"due_at": due.isoformat()}, CTX)


MORNING = (
    "Morning. You've got stretch at 9:30am and call mom at 3pm. R1 couldn't finish adding "
    "retries to the notion client in dori: its tests still fail."
)


def seed_day(h: Harness) -> None:
    """Two reminders, a failed worker nobody heard of, an approval, a degraded check and an
    extension item: every kind of line the briefing can hold."""
    remind(h.db, local(10, 13, 15), "call mom", now=T0)
    remind(h.db, local(10, 13, 9, 30), "stretch", now=T0)
    run = runs_store.create(
        h.db, repo="dori", ask="add retries to the notion client", principal=OWNER,
        session_id="s", max_seconds=600,
    )  # fmt: skip
    runs_store.update(h.db, run.code, status="running")
    runs_store.finish(h.db, run.code, "failed", failure="tests failed", step="reply")
    pulse_store.upsert(
        h.db, "core", pulse_store.run_key(run.code), f"{run.code} failed: tests failed",
        "urgent", {"status": "failed"}, True, 24 * 3600, T0,
    )  # fmt: skip
    approval(h.db, "A7", T0)
    degrade(h.db, "imessage")
    pulse_store.upsert(h.db, "dori", "k", "Dori recovered", "normal", None, False, 24 * 3600, T0)


async def test_the_briefing_is_prose_the_kept_lines_then_the_closing_line() -> None:
    async with harness() as h:
        seed_day(h)
        script = Script(MORNING)
        h.components.voice = scripted_voice(
            h.db, h.settings.home, script, clock=h.clock, timezone=TZ
        )
        out = await run_job(h, T0)
        assert out["sent"] is True
        assert h.channel.texts() == [
            f"{MORNING}\n⚠ imessage degraded\n[dori] Dori recovered\n"
            "Needs you: approve A7; a look at R1."
        ]
        prompt = script.prompts[0]
        assert "A7" not in prompt and "SECRET" not in prompt  # approval codes never reach Claude
        assert "Tuesday" in prompt and "9:30am" in prompt and "stretch" in prompt
        assert "degraded" not in prompt and "Dori recovered" not in prompt  # the tail is ours
        assert not pulse_store.pending(h.db, ("urgent", "normal"), T0)  # items told, as before


async def test_a_rerun_of_the_days_key_says_the_same_thing_without_calling_again() -> None:
    async with harness() as h:
        seed_day(h)
        script = Script(MORNING)
        h.components.voice = scripted_voice(
            h.db, h.settings.home, script, clock=h.clock, timezone=TZ
        )
        await run_job(h, T0)
        again = await run_job(h, T0, now=T0 + timedelta(minutes=5))
        assert again["sent"] is True and len(h.channel.sent) == 1 and script.calls == 1
        assert again["chars"] == len(h.channel.texts()[0])


async def test_the_empty_briefing_makes_no_call() -> None:
    async with harness() as h:
        script = Script(MORNING)
        h.components.voice = scripted_voice(
            h.db, h.settings.home, script, clock=h.clock, timezone=TZ
        )
        await run_job(h, T0)
        assert h.channel.texts() == [bf.nothing_line(date(2026, 10, 13))] and script.calls == 0


async def test_a_briefing_of_only_health_and_updates_makes_no_call() -> None:
    async with harness() as h:
        degrade(h.db, "imessage")
        pulse_store.upsert(h.db, "dori", "k", "Dori recovered", "normal", None, False, 3600, T0)
        script = Script(MORNING)
        h.components.voice = scripted_voice(
            h.db, h.settings.home, script, clock=h.clock, timezone=TZ
        )
        await run_job(h, T0)
        assert script.calls == 0
        assert h.channel.texts()[0] == ("⚠ imessage degraded\nUpdates:\n[dori] Dori recovered")


@pytest.mark.parametrize(
    "answer",
    [
        "Morning. Stretch at 9:30am. R1 failed.",  # the 3pm reminder is left out
        "Morning! Stretch at 9:30am, call mom at 3pm, R1 failed. Reply yes to retry.",
        "Morning. Stretch at 9:30am, call mom at 3pm, and R1 failed after 47 retries.",
    ],
)
async def test_a_guard_miss_sends_exactly_the_code_built_briefing(answer: str) -> None:
    async with harness() as plain:
        seed_day(plain)
        await run_job(plain, T0)
        expected = plain.channel.texts()[0]
    async with harness() as h:
        seed_day(h)
        h.components.voice = scripted_voice(
            h.db, h.settings.home, Script(answer), clock=h.clock, timezone=TZ
        )
        await run_job(h, T0)
        assert h.channel.texts() == [expected]

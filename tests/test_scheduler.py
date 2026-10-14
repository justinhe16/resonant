"""Scheduler + JobRunner (P3-10): tick precedence, L0 inline, gated tasks, reports, kill switch.

The real PolicyGate, Executor, ExtensionHost and Loop over a temp store, on a fake clock.
Extension processes are ``FakeExtProcess`` (or a blocking fake), except the kill-switch test,
which runs a tiny temp ``.py`` job with the real process runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil
import pytest

from resonant import killswitch
from resonant.api import DaemonState
from resonant.components import Components, build_components
from resonant.config import Settings, load_settings
from resonant.e2e import wait_until
from resonant.e2e.extensions import install_fixture
from resonant.executor import CallContext
from resonant.extensions.cron import parse_schedule
from resonant.extensions.process import (
    ExtEvent,
    ExtFailed,
    ExtOutput,
    ExtRequest,
    ExtRunner,
    FakeExtProcess,
    RawResult,
    RedactFn,
)
from resonant.extensions.scheduler import (
    LAST_TICK_KEY,
    CoreDeps,
    CoreJob,
    ScheduledJob,
    Scheduler,
    persist_events,
)
from resonant.gate import ApprovalResolution, PolicyGate
from resonant.gateway.channel import (
    ApprovalPrompt,
    ChannelRefused,
    MessageRef,
    SelfTestResult,
    Submit,
    approval_dedupe_key,
)
from resonant.loop import Loop, SlotPool
from resonant.ops.fakes import FakeOpsRunner
from resonant.runners.job import JobRunner, done_text, failed_text
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store import extensions as ext_store
from resonant.store import tasks as repo
from resonant.store.db import iso, kv_set, open_db, transaction
from resonant_sdk import Effect, ToolSpec

OWNER = "justin"
PRINCIPALS = """\
principals:
  justin:
    role: owner
    identities: ["imessage:+15551234567"]
  maria:
    identities: ["imessage:+15557654321"]
    grants: {example: [request, approve]}
"""
# America/Los_Angeles is UTC-7 on this date: 08:00 local is 15:00 UTC.
T0 = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
MANIFEST = """\
name: example
version: 1
approvers: [maria]
jobs:
  - id: morning-ping
    schedule: "0 8 * * *"
    run: ./cli.py ping
    effect: read
  - id: cleanup
    schedule: "0 9,12 * * *"
    run: ./cli.py cleanup
    effect: write
    level: L3
  - id: rotate-logs
    schedule: "0 10 * * *"
    run: ./cli.py rotate
    effect: write
    level: L2
  - id: purge
    schedule: "0 11 * * *"
    run: ./cli.py purge
    effect: write
    level: L1
"""
CLI = "import json\nprint(json.dumps({}))\n"
PING = ExtOutput({"ok": True}, (ExtEvent("ping", "ping", "ping:1"),), 0, 5)


class Crash(BaseException):
    """The process dying mid-step: not an Exception, so nothing catches it."""


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def set(self, at: datetime) -> None:
        self.now = at


@dataclass
class FakeChannel:
    """Records sends and approval prompts, deduping by key like the real adapter."""

    name: str = "imessage"
    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    prompts: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    keys: set[str] = field(default_factory=set[str])
    unreachable: set[str] = field(default_factory=set[str])
    crash_send: int = 0

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
        if principal in self.unreachable:
            raise ChannelRefused(f"no identity for {principal}")
        if self.crash_send:
            self.crash_send -= 1
            raise Crash
        if dedupe_key is None or dedupe_key not in self.keys:
            self.sent.append((principal, text, dedupe_key))
            if dedupe_key is not None:
                self.keys.add(dedupe_key)
        return MessageRef("imessage", f"out:{len(self.sent)}")

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        if principal in self.unreachable:
            raise ChannelRefused(f"no identity for {principal}")
        key = approval_dedupe_key(prompt.approval_id, principal)
        if key not in self.keys:
            self.keys.add(key)
            self.prompts.append((principal, prompt.text()))
        return MessageRef("imessage", f"prompt:{key}")

    async def self_test(self) -> SelfTestResult:
        return SelfTestResult(True, "fake")

    def texts(self) -> list[str]:
        return [t for _, t, _ in self.sent]


@dataclass
class BlockingRunner:
    """An ``ExtRunner`` whose runs wait for ``release``; tracks how many run at once."""

    release: asyncio.Event = field(default_factory=asyncio.Event)
    calls: list[ExtRequest] = field(default_factory=list[ExtRequest])
    active: int = 0
    peak: int = 0

    async def run(self, req: ExtRequest, *, redact: RedactFn) -> ExtOutput:
        self.calls.append(req)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return ExtOutput({}, (), 0, 0)

    async def run_raw(self, req: ExtRequest, *, redact: RedactFn) -> RawResult:
        raise AssertionError("not used")


@dataclass
class Harness:
    settings: Settings
    db: sqlite3.Connection
    clock: Clock
    components: Components
    channel: FakeChannel
    jobs: JobRunner
    loop: Loop
    sched: Scheduler

    async def tick(self, at: datetime | None = None) -> None:
        """One loop tick at ``at``, then every inline run and gated task to completion."""
        if at is not None:
            self.clock.set(at)
        await self.loop.run_once()
        await self.sched.drain()
        await self.loop.run_until_idle()

    def restart(self, *, at: datetime) -> Scheduler:
        """A new scheduler over the same store (a daemon restart at ``at``)."""
        self.clock.set(at)
        new = Scheduler.from_components(
            self.settings, self.db, self.components, started_at=at, clock=self.clock
        )
        self.loop.scheduler = new
        self.sched = new
        return new

    def audits(self, action: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM audit_log WHERE action = ? ORDER BY id", (action,)
        ).fetchall()

    def tasks(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()

    def state(self, job_id: str) -> ext_store.JobState:
        s = ext_store.get_job(self.db, "example", job_id)
        assert s is not None
        return s


@contextlib.asynccontextmanager
async def harness(
    tmp_path: Path,
    runner: ExtRunner | None,
    *,
    manifest: str = MANIFEST,
    files: dict[str, str] | None = None,
    at: datetime = T0,
    extensions: dict[str, Any] | None = None,
    before: Callable[[sqlite3.Connection], None] | None = None,
) -> AsyncGenerator[Harness]:
    settings = load_settings(dry_run=False, extensions=extensions or {})
    settings.principals_path.write_text(PRINCIPALS)
    db = open_db(settings.db_path)
    install_fixture(settings, db, manifest, files or {"cli.py": CLI})
    if before is not None:
        before(db)
    clock = Clock(at)
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1), clock=clock)
    state = DaemonState(settings, db, loop, started_at=at)
    c = build_components(
        settings,
        db,
        state,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(FakeKeychain()),
        ext_runner=runner,
    )
    assert isinstance(c.gate, PolicyGate)
    c.gate.clock = clock
    channel = FakeChannel()
    jobs = JobRunner(
        db,
        gate=c.gate,
        executor=c.executor,
        registry=c.registry,
        channel=channel,
        recipients=c.gate.approvers,
        timezone=settings.timezone,
        now=clock,
    )
    loop.runners[jobs.name] = jobs
    sched = Scheduler.from_components(settings, db, c, started_at=at, clock=clock)
    loop.scheduler = sched
    sched.start()
    h = Harness(settings, db, clock, c, channel, jobs, loop, sched)
    try:
        yield h
    finally:
        await h.sched.aclose()
        await sched.aclose()
        db.close()


def at_local(hour: int, minute: int = 0, day: int = 13) -> datetime:
    """``hour:minute`` America/Los_Angeles on 2026-10-<day>, in UTC."""
    return datetime(2026, 10, day, hour + 7, minute, tzinfo=UTC)


# --- E4: L0 inline, on time, no task row -----------------------------------------------


async def test_e4_l0_job_runs_within_a_tick_with_no_task_row(tmp_path: Path) -> None:
    fake = FakeExtProcess(results=[PING])
    async with harness(tmp_path, fake, at=at_local(7, 59) + timedelta(seconds=50)) as h:
        assert h.loop.wait_s() == pytest.approx(10.0)  # sleeps only until the due time
        await h.tick()
        assert fake.calls == []
        due = at_local(8)
        await h.tick(due + timedelta(seconds=2))
        assert len(fake.calls) == 1
        req = fake.calls[0]
        assert req.env["RESONANT_JOB_DUE_AT"] == iso(due)
        assert req.label == "example/job_morning_ping"
        assert h.tasks() == []  # no task row for an L0 job
        st = h.state("morning-ping")
        assert st.last_due == due and st.last_outcome == "ok" and st.open_task_id is None
        (run,) = h.audits("job.run")
        assert run["extension"] == "example" and run["tool"] == "job_morning_ping"
        (gated,) = h.audits("gate.evaluate")
        assert gated["task_id"] == f"job:example/morning-ping:{iso(due)}"
        assert '"origin": "scheduled"' in gated["detail"]
        assert h.db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
        (event,) = h.db.execute("SELECT * FROM events").fetchall()
        assert event["source"] == "ext:example" and event["type"] == "ext.example.ping"
        assert event["dedupe_key"] == "ext:example:ping:1" and event["principal"] is None
        # A second tick, a later tick and a restart don't run it again.
        await h.tick(due + timedelta(seconds=40))
        h.restart(at=due + timedelta(seconds=45))
        await h.tick()
        assert len(fake.calls) == 1
        assert h.channel.sent == []  # L0 is silent; notify is P3-12's


async def test_l0_failure_counts_the_streak_and_reports_nothing(tmp_path: Path) -> None:
    fake = FakeExtProcess(results=[ExtFailed("exit 2", stderr_tail="boom")])
    async with harness(tmp_path, fake) as h:
        await h.tick(at_local(8))
        st = h.state("morning-ping")
        assert st.fail_streak == 1 and st.last_outcome == "failed"
        (row,) = h.audits("job.failed")
        assert "exit 2" in row["detail"] and "boom" in row["detail"]
        assert h.channel.sent == []


# --- E5: L3 runs and reports -------------------------------------------------------------


async def test_e5_l3_job_runs_and_reports_to_approvers(tmp_path: Path) -> None:
    fake = FakeExtProcess(results=[ExtOutput({"summary": "removed 3 files"}, (), 0, 1)])
    async with harness(tmp_path, fake) as h:
        due = at_local(9)
        await h.tick(due)
        (task,) = h.tasks()
        assert (task["kind"], task["runner"], task["status"]) == ("job", "job", "done")
        assert task["priority"] == 3 and task["principal"] == OWNER
        assert len(fake.calls) == 1
        assert h.channel.sent == [
            (OWNER, "example/cleanup: done: removed 3 files", f"jobreport:{task['id']}:justin"),
            ("maria", "example/cleanup: done: removed 3 files", f"jobreport:{task['id']}:maria"),
        ]
        st = h.state("cleanup")
        assert st.open_task_id is None and st.last_due == due and st.fail_streak == 0
        (ev,) = [r for r in h.audits("gate.evaluate") if r["task_id"] == task["id"]]
        assert ev["decision"] == "Allow" and '"origin": "scheduled"' in ev["detail"]


async def test_e5_definite_failure_reported_once_per_local_day(tmp_path: Path) -> None:
    fake = FakeExtProcess(results=[ExtFailed("exit 1")])
    async with harness(tmp_path, fake) as h:
        h.channel.unreachable.add("maria")
        await h.tick(at_local(9))
        await h.tick(at_local(12))
        assert len(fake.calls) == 2
        day = "2026-10-13"
        assert h.channel.sent == [
            (OWNER, "example/cleanup failed: exit 1", f"jobfail:example/cleanup:{day}:justin")
        ]
        assert h.state("cleanup").fail_streak == 2
        assert len(h.audits("job.failed")) == 2
        await h.tick(at_local(9, day=14))  # a new local day reports again
        assert len(h.channel.sent) == 2


# --- E6: L2 / L1 ask ---------------------------------------------------------------------


async def test_e6_l2_asks_then_runs_once_approved(tmp_path: Path) -> None:
    fake = FakeExtProcess()
    async with harness(tmp_path, fake) as h:
        await h.tick(at_local(10))
        (task,) = h.tasks()
        assert task["status"] == "waiting" and task["wait_reason"] == "approval"
        assert fake.calls == []
        assert [p for p, _ in h.channel.prompts] == [OWNER, "maria"]
        text = h.channel.prompts[0][1]
        assert text.startswith("[") and text.endswith("? Reply yes / no")
        assert "example" in text
        approval = h.db.execute("SELECT id FROM approvals").fetchone()["id"]
        h.components.gate.resolve(ApprovalResolution(approval, True, OWNER, "cli", "cli"))
        await h.tick()
        assert len(fake.calls) == 1
        assert h.channel.texts()[-2:] == ["example/rotate-logs: done"] * 2


async def test_e6_l1_needs_the_code_and_expires_as_not_done(tmp_path: Path) -> None:
    fake = FakeExtProcess()
    async with harness(tmp_path, fake) as h:
        await h.tick(at_local(11))
        (_, text), _ = h.channel.prompts
        assert "This can't be undone. Reply yes " in text
        await h.tick(at_local(11, 45))  # past the 30-minute TTL
        (task,) = h.tasks()
        assert task["status"] == "done" and fake.calls == []
        assert h.channel.texts()[0] == "example/purge: not done (approval expired)"
        assert len(h.audits("job.denied")) == 1


async def test_waiting_job_task_makes_the_next_due_an_overlap(tmp_path: Path) -> None:
    async with harness(tmp_path, FakeExtProcess()) as h:
        await h.tick(at_local(10))
        assert h.tasks()[0]["status"] == "waiting"
        await h.sched.tick(at_local(10, day=14))  # the scheduler alone: still waiting
        (row,) = h.audits("job.overlap")
        assert iso(at_local(10, day=14)) in row["detail"]
        assert len(h.tasks()) == 1


# --- E8: kill switch ---------------------------------------------------------------------


async def test_e8_paused_skips_without_catch_up_after_resume(tmp_path: Path) -> None:
    fake = FakeExtProcess(results=[PING])
    async with harness(tmp_path, fake) as h:
        killswitch.engage(h.db, actor=OWNER)
        await h.tick(at_local(8))
        await h.tick(at_local(9))
        assert fake.calls == [] and h.tasks() == []
        actions = [r["detail"] for r in h.audits("job.skipped_paused")]
        assert len(actions) == 2
        assert h.state("morning-ping").last_due == at_local(8)
        killswitch.release(h.db, actor=OWNER)
        await h.tick(at_local(9, 5))
        assert fake.calls == [] and h.tasks() == []  # no catch-up after /resume
        await h.tick(at_local(8, day=14))
        assert len(fake.calls) == 1


async def test_cli_kill_is_seen_on_the_next_tick(tmp_path: Path) -> None:
    async with harness(tmp_path, FakeExtProcess()) as h:
        tracker = h.components.extensions.tracker if h.components.extensions else None
        assert tracker is not None and not tracker.paused
        with transaction(h.db):
            kv_set(h.db, "paused", True)
        assert not tracker.paused
        await h.tick(at_local(7))
        assert tracker.paused
        with transaction(h.db):
            kv_set(h.db, "paused", False)
        await h.tick(at_local(7, 1))
        assert not tracker.paused


async def test_kill_then_resume_without_a_yield_leaves_runs_allowed(tmp_path: Path) -> None:
    async with harness(tmp_path, FakeExtProcess()) as h:
        await h.tick(at_local(7))
        tracker = h.components.extensions.tracker if h.components.extensions else None
        assert tracker is not None
        killswitch.engage(h.db, actor=OWNER)
        assert tracker.paused  # held at once
        killswitch.release(h.db, actor=OWNER)  # before the background kill ran
        await h.sched.drain()
        assert not tracker.paused


async def test_paused_gated_task_is_skipped_by_the_job_runner(tmp_path: Path) -> None:
    fake = FakeExtProcess()
    async with harness(tmp_path, fake) as h:
        h.clock.set(at_local(9))
        await h.sched.tick(h.clock())  # queued, not yet dispatched
        killswitch.engage(h.db, actor=OWNER)
        await h.loop.run_until_idle()
        (task,) = h.tasks()
        assert task["status"] == "done" and fake.calls == []
        assert len(h.audits("job.skipped_paused")) == 1
        assert h.channel.sent == []


SLEEPER = """\
import os, subprocess, time
data = os.environ["RESONANT_EXT_DATA"]
child = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
with open(os.path.join(data, "child.pid"), "w") as f:
    f.write(str(child.pid))
time.sleep(60)
"""
SLEEPER_MANIFEST = """\
name: example
version: 1
jobs:
  - id: sleeper
    schedule: "0 8 * * *"
    run: ./sleeper.py
    effect: read
"""


def gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


async def test_e8_kill_switch_kills_an_inflight_job_and_its_escaped_child(
    tmp_path: Path,
) -> None:
    async with harness(
        tmp_path, None, manifest=SLEEPER_MANIFEST, files={"sleeper.py": SLEEPER}
    ) as h:
        h.clock.set(at_local(8))
        await h.loop.run_once()
        pidfile = h.settings.ext_data_dir / "example" / "child.pid"
        await wait_until(lambda: pidfile.exists() and bool(pidfile.read_text()), 10)
        child = int(pidfile.read_text())
        assert not gone(child)
        killswitch.engage(h.db, actor=OWNER)  # the /kill hook kills at once
        async with asyncio.timeout(10):
            await h.sched.drain()
        await wait_until(lambda: gone(child), 5)
        (row,) = h.audits("job.killed")
        assert "killed" in row["detail"]
        assert h.audits("job.run") == [] and h.state("sleeper").last_due == at_local(8)
        assert h.state("sleeper").fail_streak == 0
        killswitch.release(h.db, actor=OWNER)
        await h.tick(at_local(8, 1))
        assert h.audits("job.run") == []  # no re-run after /resume


# --- E11: catch-up, missed, overlap ------------------------------------------------------

HOURLY = """\
name: example
version: 1
jobs:
  - id: hourly
    schedule: "0 * * * *"
    run: ./cli.py hourly
    effect: read
    catchup: 3h
  - id: strict
    schedule: "0 * * * *"
    run: ./cli.py strict
    effect: read
"""


def downtime_since(at: datetime) -> Callable[[sqlite3.Connection], None]:
    def before(db: sqlite3.Connection) -> None:
        with transaction(db):
            kv_set(db, LAST_TICK_KEY, iso(at))
            for job in ("hourly", "strict"):
                ext_store.mark_due(db, "example", job, at + timedelta(minutes=5))

    return before


async def test_e11_catch_up_after_downtime_runs_the_latest_once(tmp_path: Path) -> None:
    fake = FakeExtProcess()
    start = T0 + timedelta(minutes=30)  # 15:30 UTC, down since 12:00, last due 12:05
    async with harness(
        tmp_path, fake, manifest=HOURLY, at=start, before=downtime_since(T0 - timedelta(hours=3))
    ) as h:
        await h.tick()
        assert [c.label for c in fake.calls] == ["example/job_hourly"]
        assert fake.calls[0].env["RESONANT_JOB_DUE_AT"] == iso(T0)
        (run,) = h.audits("job.run")
        assert '"late": true' in run["detail"]
        missed = [r["detail"] for r in h.audits("job.missed")]
        assert len(missed) == 5  # hourly 13:00, 14:00; strict 13:00, 14:00, 15:00
        h.restart(at=start + timedelta(minutes=1))
        await h.tick()
        assert len(fake.calls) == 1  # once, also across a restart


async def test_catch_up_only_covers_downtime_not_a_stall(tmp_path: Path) -> None:
    fake = FakeExtProcess()
    async with harness(tmp_path, fake, manifest=HOURLY, at=T0 - timedelta(hours=2)) as h:
        await h.tick(T0 - timedelta(hours=2))
        assert len(fake.calls) == 2  # both on time at 13:00
        await h.tick(T0 + timedelta(minutes=30))  # running, but no tick for 2.5h
        assert len(fake.calls) == 2
        assert len(h.audits("job.missed")) == 4  # 14:00 and 15:00, each job


async def test_e11_overlap_is_skipped_and_audited(tmp_path: Path) -> None:
    manifest = MANIFEST.replace('schedule: "0 8 * * *"', 'schedule: "*/5 * * * *"')
    blocking = BlockingRunner()
    async with harness(tmp_path, blocking, manifest=manifest) as h:
        h.clock.set(at_local(8))
        await h.loop.run_once()
        await asyncio.sleep(0.01)
        assert h.sched.running == 1
        h.clock.set(at_local(8, 5))
        await h.loop.run_once()
        (row,) = h.audits("job.overlap")
        assert iso(at_local(8, 5)) in row["detail"]
        blocking.release.set()
        await h.sched.drain()
        assert len(blocking.calls) == 1
        assert h.state("morning-ping").last_due == at_local(8, 5)


async def test_concurrency_cap_bounds_inline_runs(tmp_path: Path) -> None:
    manifest = HOURLY + "".join(
        f"""  - id: extra{i}
    schedule: "0 * * * *"
    run: ./cli.py x
    effect: read
"""
        for i in range(3)
    )
    blocking = BlockingRunner()
    async with harness(
        tmp_path, blocking, manifest=manifest, extensions={"max_concurrent_jobs": 2}
    ) as h:
        h.clock.set(T0)
        await h.loop.run_once()
        await asyncio.sleep(0.05)
        assert blocking.active == 2 and h.sched.running == 5
        blocking.release.set()
        await h.sched.drain()
        assert len(blocking.calls) == 5 and blocking.peak == 2


# --- exactly once ------------------------------------------------------------------------


@dataclass
class Ctx:
    db: sqlite3.Connection
    task_id: str

    def save(
        self,
        *,
        checkpoint: dict[str, Any] | None = None,
        claude_session_id: str | None = None,
        worktree_path: str | None = None,
    ) -> bool:
        with transaction(self.db):
            return repo.save_progress(
                self.db,
                self.task_id,
                checkpoint=checkpoint,
                claude_session_id=claude_session_id,
                worktree_path=worktree_path,
            )


async def crash_one_step(h: Harness) -> str:
    """Queue the 09:00 cleanup task and run one JobRunner step that crashes."""
    h.clock.set(at_local(9))
    await h.sched.tick(h.clock())
    (row,) = h.tasks()
    with transaction(h.db):
        repo.set_status(h.db, row["id"], "running", expect=("queued",))
    task = repo.get_task(h.db, row["id"])
    assert task is not None
    with pytest.raises(Crash):
        await h.jobs.step(task, [], Ctx(h.db, task.id))
    with transaction(h.db):  # the restart: resume() requeues it
        repo.requeue_interrupted(h.db, h.settings.loop.max_attempts)
    return task.id


async def test_gated_job_runs_once_after_a_crash_before_it_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeExtProcess(results=[ExtOutput({}, (ExtEvent("done", "d", "k"),), 0, 1)])
    crashes = [Crash()]

    def flaky(*args: Any, **kwargs: Any) -> int:
        if crashes:
            raise crashes.pop()
        return persist_events(*args, **kwargs)

    monkeypatch.setattr("resonant.runners.job.persist_events", flaky)
    async with harness(tmp_path, fake) as h:
        task_id = await crash_one_step(h)
        await h.loop.run_until_idle()
        assert len(fake.calls) == 1  # replayed from executions, never re-run
        (ex,) = h.db.execute("SELECT status FROM executions").fetchall()
        assert ex["status"] == "succeeded"
        (run,) = h.audits("job.run")
        assert '"replayed": true' in run["detail"]
        assert repo.get_task(h.db, task_id) is not None
        assert [t for _, t, _ in h.channel.sent] == ["example/cleanup: done"] * 2
        assert h.state("cleanup").fail_streak == 0


async def test_gated_job_report_resent_not_rerun_after_a_crash_mid_report(
    tmp_path: Path,
) -> None:
    fake = FakeExtProcess()
    async with harness(tmp_path, fake) as h:
        h.channel.crash_send = 1
        await crash_one_step(h)
        await h.loop.run_until_idle()
        assert len(fake.calls) == 1
        assert len(h.audits("job.run")) == 1  # recorded once
        assert h.channel.texts() == ["example/cleanup: done"] * 2


async def test_gated_due_time_is_claimed_once_across_ticks_and_restarts(tmp_path: Path) -> None:
    fake = FakeExtProcess()
    async with harness(tmp_path, fake) as h:
        h.clock.set(at_local(10))
        await h.sched.tick(h.clock())
        await h.sched.tick(h.clock() + timedelta(seconds=30))
        h.restart(at=at_local(10, 1))
        await h.sched.tick(h.clock())
        assert len(h.tasks()) == 1
        assert h.state("rotate-logs").open_task_id == h.tasks()[0]["id"]
        assert len(h.audits("job.queued")) == 1


# --- core jobs, isolation, the loop ------------------------------------------------------


async def test_core_job_runs_inline_without_the_gate_and_a_bad_job_is_isolated(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def briefing(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        calls.append(args["due_at"])
        ctx.events.append({"type": "briefing", "text": "hi", "dedupe_key": "briefing:d"})
        return {}

    def factory(deps: CoreDeps) -> CoreJob:
        assert deps.owner == OWNER and deps.channel is None
        return CoreJob(id="briefing", schedule="0 8 * * *", run=briefing)

    async with harness(tmp_path, FakeExtProcess()) as h:
        s = Scheduler.from_components(
            h.settings, h.db, h.components, started_at=T0, core_jobs=(factory,), clock=h.clock
        )
        core = next(j for j in s.jobs if j.core)
        bad = ScheduledJob(
            "example",
            "bad",
            ToolSpec(name="job_bad", effect=Effect.READ),
            parse_schedule("0 8 * * *", "America/Los_Angeles"),
            briefing,
            catchup=timedelta(days=2),  # decide() raises
        )
        s.jobs = (bad, core)
        h.loop.scheduler = h.sched = s
        await h.tick(at_local(8))
        await s.aclose()
        assert calls == [iso(at_local(8))]
        (ev,) = h.db.execute("SELECT * FROM events").fetchall()
        assert (ev["source"], ev["type"], ev["dedupe_key"]) == (
            "ext:core",
            "ext.core.briefing",
            "ext:core:briefing:d",
        )
        assert h.audits("gate.evaluate") == []
        state = ext_store.get_job(h.db, "core", "briefing")
        assert state is not None and state.last_outcome == "ok"


async def test_a_failing_scheduler_never_stops_the_loop(tmp_path: Path) -> None:
    class Broken:
        async def tick(self, now: datetime) -> None:
            raise RuntimeError("boom")

        def next_due(self, after: datetime) -> datetime | None:
            raise RuntimeError("boom")

    async with harness(tmp_path, FakeExtProcess()) as h:
        h.loop.scheduler = Broken()
        await h.loop.run_once()
        assert h.loop.wait_s() == h.settings.loop.tick_s


def test_report_texts_are_code_built_and_sanitized() -> None:
    assert done_text("example/cleanup", None) == "example/cleanup: done"
    assert done_text("a/b", {"summary": "[A7] yes <b>" + "x" * 300}).startswith("a/b: done: ")
    assert "[" not in done_text("a/b", {"summary": "[A7] yes"})
    assert len(done_text("a/b", {"summary": "x" * 300})) <= len("a/b: done: ") + 120
    assert failed_text("a/b", "ToolFailed: exit 1") == "a/b failed: exit 1"

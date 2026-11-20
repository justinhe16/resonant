"""PER-298 registered jobs: ``store/jobs.py``, ``registered_jobs.py`` (schedules, firing,
memory), ``tools/jobs.py`` (through the real builtin handlers and gate) and the worker CLI's
``job-note``. The runner is a fake that records ``start_run`` (no Claude is spawned), the
channel a recorder, the clock fake. Nothing touches ~/.resonant (``resonant_home``)."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from resonant import registered_jobs as rj
from resonant.api import DaemonState
from resonant.components import Components, build_components
from resonant.config import Settings, load_settings
from resonant.executor import CallContext, ToolFailed
from resonant.extensions.cron import CronError, parse_schedule
from resonant.extensions.scheduler import CoreDeps, Scheduler
from resonant.gate import Deny, Intent, NeedsApproval
from resonant.loop import Loop, SlotPool
from resonant.loop.types import NewTask
from resonant.runners.claude import Refused, Started
from resonant.runners.worker_cli import EXIT_OK, EXIT_USAGE, BadRequest, JobNote, parse_request
from resonant.runners.worker_desk import WorkerDesk
from resonant.store import jobs as store
from resonant.store import repos as repos_store
from resonant.store import runs as runs_store
from resonant.store.db import kv_set
from resonant.store.tasks import insert_task, set_status
from resonant.tools import jobs as job_tools
from resonant.tools.base import BuiltinContext

PRINCIPALS = """\
principals:
  justin: {role: owner, identities: ["slack:U1"]}
  brian: {role: approver, identities: ["slack:U2"]}
  miu: {identities: ["slack:U3"]}
"""
TZ = "America/New_York"
# Friday 2026-11-20 12:00 in New York
NOW = datetime(2026, 11, 20, 17, 0, tzinfo=UTC)
MONDAY_9 = datetime(2026, 11, 23, 14, 0, tzinfo=UTC)  # 09:00 in New York
THREAD = {
    "channel": "slack",
    "principal": "brian",
    "identity": "slack:U2",
    "thread": "C0GENERAL:1700000000.000100",
    "ref": "C0GENERAL:1700000000.000100",
}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Recorder:
    sent: list[tuple[str, str, Any]] = field(default_factory=list[tuple[str, str, Any]])
    rooms: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    async def send(self, principal: str, text: str, *, thread: Any = None, **_: Any) -> Any:
        self.sent.append((principal, text, thread))

    async def post_room(self, key: str, text: str, **_: Any) -> Any:
        self.rooms.append((key, text))


@dataclass
class FakeRunner:
    """``start_run`` creates a real (queued) run row, so ``busy`` sees it as active."""

    conn: sqlite3.Connection
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    refuse: str | None = None

    async def start_run(self, repo: str | None, ask: str, principal: str, **kw: Any) -> Any:
        self.calls.append({"repo": repo, "ask": ask, "principal": principal, **kw})
        if self.refuse is not None:
            return Refused(self.refuse)
        run = runs_store.create(
            self.conn,
            repo=repo or "",
            ask=ask,
            principal=principal,
            session_id=str(uuid.uuid4()),
            max_seconds=600,
        )
        if kw.get("job") is not None:
            store.link_run(self.conn, run.code, kw["job"])
        return Started(run.code, repo or "", 10)


@pytest.fixture
def settings(resonant_home: Any) -> Settings:
    s = load_settings(
        dry_run=False,
        timezone=TZ,
        channels={"slack": {"rooms": {"digest": "C0DIGEST"}}},
    )
    s.principals_path.write_text(PRINCIPALS)
    return s


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def components(settings: Settings, db: sqlite3.Connection, clock: Clock) -> Components:
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    state = DaemonState(settings, db, loop, started_at=NOW)
    state.components = c = build_components(settings, db, state)
    kv_set(db, "autonomy", "on")
    ctx = BuiltinContext(
        conn=db, state=state, registry=c.registry, principals=c.principals, clock=clock
    )
    for name, handler in job_tools.handlers(ctx).items():
        c.handlers[(None, name)] = handler
    return c


def task_of(db: sqlite3.Connection, principal: str, ref: dict[str, Any] | None = THREAD) -> str:
    tid = insert_task(
        db, NewTask(kind="chat", runner="local", priority=1, principal=principal, channel_ref=ref)
    )
    set_status(db, tid, "running")
    return tid


async def call(
    c: Components, name: str, args: dict[str, Any] | None = None, task: str | None = None
) -> dict[str, Any]:
    ctx = None if task is None else CallContext(secrets={}, idempotency_key="k", task_id=task)
    return await c.handlers[(None, name)](args or {}, ctx)  # type: ignore[call-arg]


async def add(c: Components, db: sqlite3.Connection, **args: Any) -> dict[str, Any]:
    args.setdefault("schedule", "0 9 * * 1")
    args.setdefault("prompt", "check flaky CI and file tickets")
    return await call(c, "add_job", args, task_of(db, "brian"))


async def tick(db: sqlite3.Connection, c: Components, runner: Any, ch: Recorder, at: datetime):
    return await rj.tick(db, at, runner=runner, principals=c.principals, channel=ch)  # type: ignore[arg-type]


# --- schedules ------------------------------------------------------------------------------


def test_cron_in_words() -> None:
    assert rj.describe("0 9 * * 1") == "every Monday at 09:00"
    assert rj.describe("30 8 * * *") == "every day at 08:30"
    assert rj.describe("0 9 * * 1-5") == "on weekdays at 09:00"
    assert rj.describe("0 9,17 * * 1,4") == "every Monday and Thursday at 09:00 and 17:00"
    assert rj.describe("*/30 * * * *") == "every 30 minutes"
    assert rj.describe("15 */2 * * *") == "every 2 hours at :15"
    assert rj.describe("0 9 1 * *") == "on day 1 of each month at 09:00"
    assert rj.describe("0 9 * 1 *") == "on the schedule '0 9 * 1 *'"


@pytest.mark.parametrize("expr", ["*/10 * * * *", "0,5 9 * * 1", "0,55 * * * *", "* 9 * * *"])
def test_more_often_than_every_15_minutes_is_refused(expr: str) -> None:
    with pytest.raises(CronError):
        rj.parse(expr, TZ)


def test_every_15_minutes_and_slower_are_fine() -> None:
    for expr in ("*/15 * * * *", "0 * * * *", "0 9 * * 1", "0,30 9-17 * * 1-5"):
        assert rj.parse(expr, TZ).expr == expr


# --- tools ------------------------------------------------------------------------------------


async def test_brians_words_create_a_job_the_reply_says_when(
    components: Components, db: sqlite3.Connection
) -> None:
    out = await add(components, db, name="flaky CI")
    assert out["added"] is True and out["id"] == "J1"
    assert out["text"] == (
        "Added J1 (flaky CI): every Monday at 09:00 (0 9 * * 1, America/New_York). "
        "Next: Mon 23 Nov 09:00, Mon 30 Nov 09:00, Mon 7 Dec 09:00. Output: this thread."
    )
    job = store.get(db, "J1")
    assert job is not None
    assert (job.created_by, job.output, job.state) == ("brian", "thread", "active")
    assert job.channel_ref == THREAD and job.last_due == NOW


async def test_add_job_refusals(components: Components, db: sqlite3.Connection) -> None:
    with pytest.raises(ToolFailed, match="at most every 15 minutes"):
        await add(components, db, schedule="*/5 * * * *")
    with pytest.raises(ToolFailed, match="want 5 fields"):
        await add(components, db, schedule="every monday")
    with pytest.raises(ToolFailed, match="no registered repo 'nope'"):
        await add(components, db, repo="nope")
    with pytest.raises(ToolFailed, match=r"rooms: digest"):
        await add(components, db, output="room:general")
    assert store.live(db) == []


async def test_outputs(components: Components, db: sqlite3.Connection) -> None:
    assert (await add(components, db, output="#digest"))["output"] == "room:digest"
    assert (await add(components, db, output="dm"))["output"] == "dm"
    # no thread (a DM or iMessage): the default is a DM
    unthreaded = task_of(db, "brian", ref=None)
    out = await call(components, "add_job", {"schedule": "0 9 * * *", "prompt": "x"}, unthreaded)
    assert out["output"] == "dm"


def test_miu_is_refused_brian_is_asked(components: Components, db: sqlite3.Connection) -> None:
    args = {"schedule": "0 9 * * 1", "prompt": "check flaky CI"}
    for spec in job_tools.SPECS:
        intent = Intent.for_tool(
            spec,
            task_id=task_of(db, "miu"),
            principal="miu",
            args=args if spec is job_tools.ADD_JOB else {"id": "J1"},
        )
        assert isinstance(components.gate.evaluate(intent), Deny), spec.name
    asked = components.gate.evaluate(
        Intent.for_tool(
            job_tools.ADD_JOB, task_id=task_of(db, "brian"), principal="brian", args=args
        )
    )
    assert isinstance(asked, NeedsApproval)
    assert "every Monday at 09:00" in (asked.summary or "")


async def test_pause_resume_delete_and_list(
    components: Components, db: sqlite3.Connection, clock: Clock
) -> None:
    await add(components, db, name="flaky CI")
    listed = await call(components, "list_jobs")
    assert (
        listed["text"] == "Jobs:\nJ1 flaky CI: every Monday at 09:00 · active · no runs yet · "
        "next Mon 23 Nov 09:00"
    )
    assert (await call(components, "pause_job", {"id": "j1"}))["text"] == "Paused J1 (flaky CI)."
    assert (await call(components, "list_jobs"))["jobs"][0]["state"] == "paused"
    assert (await call(components, "list_jobs"))["jobs"][0]["next_due"] is None
    clock.now = MONDAY_9 + timedelta(days=1)
    resumed = await call(components, "resume_job", {"id": "1"})
    assert resumed["text"] == "Resumed J1 (flaky CI). Next: Mon 30 Nov 09:00."
    job = store.get(db, "J1")
    assert job is not None and job.last_due == clock.now  # nothing caught up
    assert (await call(components, "delete_job", {"id": "J1"}))["deleted"] is True
    assert (await call(components, "list_jobs"))["text"] == "No jobs."
    with pytest.raises(ToolFailed, match="no job J1"):
        await call(components, "pause_job", {"id": "J1"})


# --- firing ---------------------------------------------------------------------------------


async def test_a_due_job_starts_exactly_one_run_and_skips_while_busy(
    components: Components, db: sqlite3.Connection
) -> None:
    await add(components, db, name="flaky CI")
    runner, ch = FakeRunner(db), Recorder()
    assert await tick(db, components, runner, ch, MONDAY_9 - timedelta(minutes=1)) == {
        "quiet": True
    }
    at = MONDAY_9 + timedelta(seconds=20)
    assert (await tick(db, components, runner, ch, at))["started"] == 1
    assert (await tick(db, components, runner, ch, at + timedelta(seconds=30))) == {"quiet": True}
    (started,) = runner.calls
    assert started["principal"] == "brian" and started["report"] == "now"
    assert started["channel_ref"] == THREAD
    assert started["job"] == store.RunLink("J1", None)
    job = store.get(db, "J1")
    assert job is not None and job.last_run_code == "R1"
    # a week later R1 is still going: skipped, not queued for later
    week = at + timedelta(days=7)
    assert (await tick(db, components, runner, ch, week))["overlap"] == 1
    assert len(runner.calls) == 1
    assert (
        db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'regjob.overlap'").fetchone()[0]
        == 1
    )
    runs_store.finish(db, "R1", "done")
    assert (await tick(db, components, runner, ch, week + timedelta(days=7)))["started"] == 1
    listed = await call(components, "list_jobs")
    assert listed["jobs"][0]["last_run"]["code"] == "R2"


async def test_missed_while_down_is_not_caught_up(
    components: Components, db: sqlite3.Connection
) -> None:
    await add(components, db)
    runner, ch = FakeRunner(db), Recorder()
    late = MONDAY_9 + timedelta(hours=2)  # the daemon was down at 09:00
    assert (await tick(db, components, runner, ch, late))["missed"] == 1
    assert runner.calls == []


async def test_a_paused_job_is_skipped(components: Components, db: sqlite3.Connection) -> None:
    await add(components, db)
    await call(components, "pause_job", {"id": "J1"})
    runner = FakeRunner(db)
    assert await tick(db, components, runner, Recorder(), MONDAY_9) == {"quiet": True}
    assert runner.calls == []


async def test_room_and_dm_outputs_reach_the_run(
    components: Components, db: sqlite3.Connection
) -> None:
    await add(components, db, output="room:digest")
    await add(components, db, output="dm")
    runner = FakeRunner(db)
    await tick(db, components, runner, Recorder(), MONDAY_9)
    room, dm = runner.calls
    assert room["job"] == store.RunLink("J1", "digest") and room["channel_ref"] is None
    assert dm["job"] == store.RunLink("J2", None) and dm["channel_ref"] is None


async def test_the_repo_is_gone_the_job_pauses_and_says_so(
    components: Components, db: sqlite3.Connection
) -> None:
    repos_store.add(db, "own/dori", base="main", source="test", name="dori")
    await add(components, db, repo="dori", output="room:digest")
    repos_store.forget(db, "dori")
    runner, ch = FakeRunner(db), Recorder()
    await tick(db, components, runner, ch, MONDAY_9)
    assert runner.calls == []
    job = store.get(db, "J1")
    assert job is not None and job.state == "paused" and job.last_error == "its repo dori is gone"
    assert ch.rooms == [
        (
            "digest",
            "J1 (check flaky CI and file tickets) paused itself: its repo "
            "dori is gone. Resume it once the repo is back.",
        )
    ]


async def test_a_removed_creator_runs_as_the_owner(
    components: Components, db: sqlite3.Connection, settings: Settings
) -> None:
    await add(components, db)
    job = store.get(db, "J1")
    assert job is not None
    gone = components.principals.model_validate(
        {"principals": {"justin": {"role": "owner", "identities": ["slack:U1"]}}}
    )
    assert rj.principal_for(job, gone) == "justin"
    runner = FakeRunner(db)
    await rj.tick(db, MONDAY_9, runner=runner, principals=gone, channel=Recorder())  # type: ignore[arg-type]
    (started,) = runner.calls
    assert started["principal"] == "justin" and started["channel_ref"] is None


async def test_a_refused_start_is_recorded(components: Components, db: sqlite3.Connection) -> None:
    await add(components, db)
    runner = FakeRunner(db, refuse="Resonant is paused. /resume first.")
    assert (await tick(db, components, runner, Recorder(), MONDAY_9))["refused"] == 1
    job = store.get(db, "J1")
    assert job is not None and job.last_error == "Resonant is paused. /resume first."


async def test_run_job_now(components: Components, db: sqlite3.Connection) -> None:
    await add(components, db)
    # no runner here: a plain refusal
    out = await call(components, "run_job_now", {"id": "J1"})
    assert out == {
        "id": "J1",
        "started": False,
        "text": "J1 didn't start: the Claude runner isn't running.",
    }
    components.runners["claude"] = FakeRunner(db)  # type: ignore[assignment]
    out = await call(components, "run_job_now", {"id": "J1"})
    assert out["started"] is True and out["code"] == "R1"
    again = await call(components, "run_job_now", {"id": "J1"})
    assert again["text"] == "J1's last run R1 is still going."


# --- memory ---------------------------------------------------------------------------------


def test_job_note_requests() -> None:
    req = parse_request({"cmd": "job_note", "payload": '{"seen": ["a", "b"], "note": "hi"}'})
    assert req == JobNote("hi", ("a", "b"))
    for bad in ("nope", "[]", '{"x": 1}', "{}", '{"note": 3}', '{"seen": "a"}'):
        with pytest.raises(BadRequest):
            parse_request({"cmd": "job_note", "payload": bad})
    with pytest.raises(BadRequest, match="too long"):
        parse_request({"cmd": "job_note", "payload": '{"note": "' + "x" * 5000 + '"}'})


async def test_job_note_persists_into_the_next_prompt(
    components: Components, db: sqlite3.Connection, clock: Clock
) -> None:
    await add(components, db, name="flaky CI")
    runner = FakeRunner(db)
    await tick(db, components, runner, Recorder(), MONDAY_9)
    first = rj.run_context(db, "R1")
    assert first is not None and "no memory from earlier runs yet" in first
    assert "resonant job-note" in first
    desk = WorkerDesk(
        db,
        channel=cast(Any, Recorder()),
        trust=None,
        clock=clock,
        ask_timeout_s=60,
        wake=lambda _r, _k: None,
    )
    gone: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    reply = await desk.handle("R1", JobNote("2 flaky: test_a, test_b", ("PR#1", "PR#2")), gone)
    assert reply is not None and (reply.text, reply.exit) == ("Saved for J1's next run.", EXIT_OK)
    await desk.handle("R1", JobNote(None, ("PR#1", "PR#3")), gone)
    job = store.get(db, "J1")
    assert job is not None and job.memory == "2 flaky: test_a, test_b"
    assert job.seen == ("PR#2", "PR#1", "PR#3")
    runs_store.finish(db, "R1", "done")
    await tick(db, components, runner, Recorder(), MONDAY_9 + timedelta(days=7))
    nxt = rj.run_context(db, "R2")
    assert nxt is not None
    assert "Its memory from earlier runs:\n2 flaky: test_a, test_b" in nxt
    assert "Already seen (skip these): PR#2, PR#1, PR#3" in nxt
    # a run that isn't a job's has nothing to save to
    other = runs_store.create(
        db, repo="", ask="x", principal="justin", session_id="s", max_seconds=60
    )
    reply = await desk.handle(other.code, JobNote("x"), gone)
    assert reply is not None and reply.exit == EXIT_USAGE and rj.run_context(db, other.code) is None


def test_memory_and_seen_are_capped(db: sqlite3.Connection) -> None:
    job = store.create(
        db,
        name="n",
        schedule="0 9 * * 1",
        tz=TZ,
        prompt="p",
        repo=None,
        output="dm",
        created_by="brian",
        channel_ref=None,
        now=NOW,
    )
    with pytest.raises(store.NoteTooBig):
        store.save_note(db, job.id, note="é" * 3000, seen=(), now=NOW)
    keys = tuple(f"k{i}" for i in range(store.SEEN_MAX + 50))
    saved = store.save_note(db, job.id, note=None, seen=keys, now=NOW)
    assert saved is not None and len(saved.seen) == store.SEEN_MAX and saved.seen[-1] == keys[-1]


# --- personal Resonant with no jobs: nothing new --------------------------------------------


async def test_no_jobs_records_nothing(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    deps = CoreDeps(settings, db, components, Recorder(), "justin")  # type: ignore[arg-type]
    core = rj.registered_jobs_job(deps)
    assert core is not None and core.quiet_idle and core.schedule == "* * * * *"
    assert rj.registered_jobs_job(CoreDeps(settings, db, components, None, "justin")) is None
    sched = Scheduler.from_components(
        settings,
        db,
        components,
        started_at=NOW,
        core_jobs=(rj.registered_jobs_job,),
        clock=clock,
        channel=cast(Any, Recorder()),
    )
    before = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    for minute in range(1, 4):
        clock.now = NOW + timedelta(minutes=minute)
        await sched.tick(clock.now)
        await sched.drain()
    rows = db.execute("SELECT action, detail FROM audit_log").fetchall()[before:]
    assert [tuple(r) for r in rows] == []
    assert db.execute("SELECT COUNT(*) FROM registered_jobs").fetchone()[0] == 0
    await sched.aclose()


def test_parse_schedule_tz_is_the_jobs(db: sqlite3.Connection) -> None:
    sched = parse_schedule("0 9 * * 1", TZ)
    assert rj.fmt_time(rj.next_times(sched, NOW, 1)[0], TZ) == "Mon 23 Nov 09:00"


async def test_a_jobs_end_text_goes_to_its_room(db: sqlite3.Connection) -> None:
    """``ClaudeRunner._send_end``: a job run with a room posts there; any other run (or a
    room that refuses) goes to the principal as before."""
    from types import SimpleNamespace

    from resonant.gateway.channel import ChannelRefused
    from resonant.runners.claude import ClaudeRunner

    def run_for(link: store.RunLink | None) -> runs_store.Run:
        run = runs_store.create(db, repo="", ask="x", principal="brian", session_id="s",
                                max_seconds=60)  # fmt: skip
        if link is not None:
            store.link_run(db, run.code, link)
        return run

    def sender(ch: Recorder) -> Any:
        async def send_run(run: runs_store.Run, text: str, *, dedupe_key: str | None) -> None:
            await ch.send(run.principal, text, dedupe_key=dedupe_key)

        return send_run

    ch = Recorder()
    me = SimpleNamespace(conn=db, channel=ch, _send_run=sender(ch))
    await ClaudeRunner._send_end(me, run_for(store.RunLink("J1", "digest")), "R1 done")  # type: ignore[arg-type]
    await ClaudeRunner._send_end(me, run_for(store.RunLink("J2")), "R2 done")  # type: ignore[arg-type]
    await ClaudeRunner._send_end(me, run_for(None), "R3 done")  # type: ignore[arg-type]
    assert ch.rooms == [("digest", "R1 done")]
    assert [(p, t) for p, t, _ in ch.sent] == [("brian", "R2 done"), ("brian", "R3 done")]

    class Refusing(Recorder):
        async def post_room(self, key: str, text: str, **_: Any) -> Any:
            raise ChannelRefused("no running channel has rooms")

    no_room = Refusing()
    me = SimpleNamespace(conn=db, channel=no_room, _send_run=sender(no_room))
    await ClaudeRunner._send_end(me, run_for(store.RunLink("J1", "digest")), "R4 done")  # type: ignore[arg-type]
    assert [(p, t) for p, t, _ in no_room.sent] == [("brian", "R4 done")]


def test_the_approval_shows_the_whole_prompt(
    components: Components, db: sqlite3.Connection
) -> None:
    long = "check flaky CI. " * 80  # 1280 chars: what runs unattended is what is approved
    heading = job_tools.summary_action(
        Intent.for_tool(
            job_tools.ADD_JOB,
            task_id=task_of(db, "brian"),
            principal="brian",
            args={"schedule": "0 9 * * 1", "prompt": long},
        )
    )
    assert heading is not None and "(+" in heading and "more chars)" in heading
    assert heading.count("check flaky CI.") >= 60


async def test_a_linked_run_counts_as_busy_and_starts_dont_overlap(
    components: Components, db: sqlite3.Connection
) -> None:
    await add(components, db)
    job = store.get(db, "J1")
    assert job is not None and not rj.busy(db, job)
    run = runs_store.create(db, repo="", ask="x", principal="brian", session_id="s", max_seconds=60)
    store.link_run(db, run.code, store.RunLink("J1"))  # started, but never recorded
    assert rj.busy(db, job)
    runs_store.finish(db, run.code, "done")
    assert not rj.busy(db, job)
    rj._starting.add("J1")  # pyright: ignore[reportPrivateUsage]
    try:
        ok, text = await rj.start(
            db,
            job,
            runner=FakeRunner(db),
            principals=components.principals,
            channel=None,
            now=NOW,
            manual=True,
            by="justin",
        )
    finally:
        rj._starting.discard("J1")  # pyright: ignore[reportPrivateUsage]
    assert not ok and text == "J1 is starting a run already."


async def test_manual_runs_audit_who(components: Components, db: sqlite3.Connection) -> None:
    await add(components, db)
    components.runners["claude"] = FakeRunner(db)  # type: ignore[assignment]
    await call(components, "run_job_now", {"id": "J1"}, task_of(db, "justin"))
    (row,) = db.execute("SELECT detail FROM audit_log WHERE action = 'regjob.started'")
    assert '"by": "justin"' in row[0] and '"manual": true' in row[0]


async def test_a_paused_instance_with_no_jobs_records_nothing(
    components: Components, db: sqlite3.Connection, settings: Settings, clock: Clock
) -> None:
    from resonant import killswitch

    killswitch.engage(db, actor="test")
    sched = Scheduler.from_components(
        settings,
        db,
        components,
        started_at=NOW,
        core_jobs=(rj.registered_jobs_job,),
        clock=clock,
        channel=cast(Any, Recorder()),
    )
    before = db.execute("SELECT MAX(id) FROM audit_log").fetchone()[0]
    for minute in range(1, 4):
        clock.now = NOW + timedelta(minutes=minute)
        await sched.tick(clock.now)
        await sched.drain()
    rows = db.execute("SELECT action FROM audit_log WHERE id > ?", (before,)).fetchall()
    assert [r[0] for r in rows if r[0].startswith("job.")] == []
    await sched.aclose()

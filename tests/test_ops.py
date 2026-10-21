"""Ops tools (P2-14): restarts, /update, disk, logs, the detached helper and "back up".

Every subprocess goes through ``FakeOpsRunner``: nothing here runs git, uv or launchctl.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from resonant.config import load_settings
from resonant.executor import ToolFailed
from resonant.gate import DryRun, Intent, NeedsApproval, PolicyGate
from resonant.gateway.channel import ChannelRefused, ChannelSendError, MessageRef
from resonant.launchd import DAEMON_LABEL, OLLAMA_LABEL
from resonant.loop import NewTask
from resonant.loop.types import PRIORITY_OWNER
from resonant.ops import (
    LAUNCHCTL,
    ProcResult,
    SubprocessRunner,
    back_up,
    back_up_text,
    kickstart_argv,
    mark_restart,
    ops_env,
    restart_later,
    restart_later_argv,
    restart_pending,
)
from resonant.ops.fakes import FakeOpsRunner
from resonant.principals import Principal, Principals
from resonant.router.render import render_disk, render_logs
from resonant.runners.local import dry_run_reply, report_line
from resonant.store.db import kv_set, transaction
from resonant.store.tasks import insert_task
from resonant.tools import ToolRegistry, ops
from resonant.tools.ops import (
    GIT,
    OLLAMA_COOLDOWN,
    PULL_TIMEOUT_S,
    RESTART_OLLAMA,
    RESTART_RESONANT,
    SYNC_TIMEOUT_S,
    UPDATE_RESONANT,
    UV_CACHE,
    Ops,
    clean_line,
    log_line,
    recent_logs,
    scrub,
    strip_home,
)

OWNER = "justin"
UID = 501
T0 = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
UV = "/opt/homebrew/bin/uv"
HELPER = (sys.executable, "-m", "resonant.ops.restart_later", DAEMON_LABEL, "3")
KICK_DAEMON = (LAUNCHCTL, "kickstart", "-k", f"gui/{UID}/{DAEMON_LABEL}")
KICK_OLLAMA = (LAUNCHCTL, "kickstart", "-k", f"gui/{UID}/{OLLAMA_LABEL}")
BRANCH = (GIT, "rev-parse", "--abbrev-ref", "HEAD")
STATUS = (GIT, "status", "--porcelain")
HEAD = (GIT, "rev-parse", "--short=7", "HEAD")
PULL = (GIT, "pull", "--ff-only")


def sync() -> tuple[str, ...]:
    """/update's uv sync, with its own cache under the Resonant home (not ~/.cache/uv)."""
    return (UV, "sync", "--frozen", "--cache-dir", str(load_settings().home / UV_CACHE))


def ok(stdout: str = "") -> ProcResult:
    return ProcResult(0, stdout, "")


def make_ops(
    db: sqlite3.Connection,
    runner: FakeOpsRunner,
    repo: Path | None,
    *,
    uv: str | None = UV,
) -> Ops:
    settings = load_settings(ops={"repo_path": repo, "uv_path": uv})
    return Ops(db, settings, runner, owner=OWNER, clock=lambda: T0, uid=UID)


def clean_repo(runner: FakeOpsRunner, *, old: str = "a1b2c3d", new: str = "e4f5a6b") -> None:
    runner.ok(*BRANCH, stdout="main\n").ok(*STATUS, stdout="")
    runner.on(*HEAD, results=[ok(old + "\n"), ok(new + "\n")])


# --- argv builders and the helper --------------------------------------------------------


def test_argv_shapes_and_labels() -> None:
    assert kickstart_argv(OLLAMA_LABEL, UID) == list(KICK_OLLAMA)
    assert restart_later_argv(DAEMON_LABEL) == list(HELPER)
    for build in (kickstart_argv, restart_later_argv):
        with pytest.raises(ValueError):
            build("com.apple.Finder")


def test_restart_later_sleeps_then_kickstarts() -> None:
    events: list[Any] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        events.append(("run", argv, kw["timeout"], kw["check"]))
        return subprocess.CompletedProcess(argv, 0)

    assert restart_later.main([DAEMON_LABEL, "3"], run=run, sleep=events.append) == 0
    assert events[0] == 3.0
    assert events[1][0] == "run" and events[1][1] == kickstart_argv(DAEMON_LABEL)
    assert events[1][2] == 15.0 and events[1][3] is False


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["com.apple.Finder", "3"],
        [DAEMON_LABEL, "x"],
        [DAEMON_LABEL, "-1"],
        [DAEMON_LABEL, "61"],
    ],
)
def test_restart_later_refuses_bad_args(argv: list[str]) -> None:
    def never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("must not run")

    assert restart_later.main(argv, run=never, sleep=never) == 2


def test_restart_later_reports_a_failed_kickstart() -> None:
    def failing(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 113)

    def slow(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 15)

    assert restart_later.main([OLLAMA_LABEL, "0"], run=failing, sleep=lambda _s: None) == 1
    assert restart_later.main([OLLAMA_LABEL, "0"], run=slow, sleep=lambda _s: None) == 1


# --- restart_ollama ----------------------------------------------------------------------


async def test_restart_ollama_kickstarts_with_a_timeout(db: sqlite3.Connection) -> None:
    runner = FakeOpsRunner()
    out = await make_ops(db, runner, None).restart_ollama()
    assert out == {"label": OLLAMA_LABEL, "restarted": True}
    (call,) = runner.calls
    assert call.argv == KICK_OLLAMA and call.timeout_s == 15.0
    assert runner.spawned == [] and restart_pending(db) is None
    assert RESTART_OLLAMA.timeout_s > call.timeout_s  # the executor never times out first


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (ProcResult(113, "", "Could not find service"), "kickstart failed (exit 113)"),
        (TimeoutError(), "timed out after 15s"),
        (FileNotFoundError(), "couldn't start"),
    ],
)
async def test_restart_ollama_failures(
    db: sqlite3.Connection, result: ProcResult | BaseException, message: str
) -> None:
    runner = FakeOpsRunner().on(LAUNCHCTL, results=[result])
    with pytest.raises(ToolFailed, match=re.escape(message)) as e:
        await make_ops(db, runner, None).restart_ollama()
    assert "Could not find" not in str(e.value)  # stderr is never shown


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def ops_at(db: sqlite3.Connection, runner: FakeOpsRunner, clock: Clock) -> Ops:
    return Ops(db, load_settings(), runner, owner=OWNER, clock=clock, uid=UID)


async def test_a_second_restart_ollama_inside_the_cooldown_is_refused(
    db: sqlite3.Connection,
) -> None:
    runner, clock = FakeOpsRunner(), Clock()
    ops_ = ops_at(db, runner, clock)
    assert (await ops_.restart_ollama())["restarted"] is True
    clock.now = T0 + timedelta(minutes=2, seconds=10)
    with pytest.raises(ToolFailed) as e:
        await ops_.restart_ollama()
    assert str(e.value) == "I restarted it 2m ago; I can restart it again in 3m"
    clock.now = T0 + OLLAMA_COOLDOWN - timedelta(seconds=20)
    with pytest.raises(ToolFailed, match=r"again in 20s$"):
        await ops_.restart_ollama()
    assert len(runner.calls) == 1  # neither refusal ran launchctl
    clock.now = T0 + OLLAMA_COOLDOWN
    assert (await ops_.restart_ollama())["restarted"] is True
    assert len(runner.calls) == 2


async def test_the_cooldown_holds_across_a_daemon_restart(db: sqlite3.Connection) -> None:
    clock = Clock()
    await ops_at(db, FakeOpsRunner(), clock).restart_ollama()
    clock.now = T0 + timedelta(minutes=1)
    with pytest.raises(ToolFailed, match="again in 4m"):
        await ops_at(db, FakeOpsRunner(), clock).restart_ollama()  # a fresh Ops (new daemon)


@pytest.mark.parametrize(
    ("result", "cools"),
    [(TimeoutError(), True), (ProcResult(113, "", ""), False), (FileNotFoundError(), False)],
    ids=["timed-out", "exit-113", "couldnt-start"],
)
async def test_only_a_kickstart_that_may_have_run_starts_the_cooldown(
    db: sqlite3.Connection, result: ProcResult | BaseException, cools: bool
) -> None:
    clock = Clock()
    runner = FakeOpsRunner().on(LAUNCHCTL, results=[result, ok()])
    ops_ = ops_at(db, runner, clock)
    with pytest.raises(ToolFailed):
        await ops_.restart_ollama()
    clock.now = T0 + timedelta(seconds=30)
    if cools:
        with pytest.raises(ToolFailed, match="I restarted it 30s ago"):
            await ops_.restart_ollama()
    else:
        assert (await ops_.restart_ollama())["restarted"] is True


async def test_a_garbage_cooldown_stamp_doesnt_block_a_restart(db: sqlite3.Connection) -> None:
    for stamp in ("2026-10-13T15:00:00", "not a time", 12):  # naive, garbage, wrong type
        kv_set(db, ops.OLLAMA_RESTARTED_KEY, stamp)
        assert (await ops_at(db, FakeOpsRunner(), Clock()).restart_ollama())["restarted"]


async def test_a_clock_that_went_back_doesnt_block_a_restart(db: sqlite3.Connection) -> None:
    clock = Clock()
    ops_ = ops_at(db, FakeOpsRunner(), clock)
    await ops_.restart_ollama()
    clock.now = T0 - timedelta(hours=1)
    assert (await ops_.restart_ollama())["restarted"] is True


# --- restart_resonant --------------------------------------------------------------------


async def test_restart_resonant_marks_then_spawns_the_delayed_helper(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    runner = FakeOpsRunner().ok(*HEAD, stdout="abc1234\n")
    out = await make_ops(db, runner, tmp_path).restart_resonant()
    assert out == {"label": DAEMON_LABEL, "restarting": True, "delay_s": 3, "commit": "abc1234"}
    assert runner.argvs() == [HEAD]
    assert runner.calls[0].cwd == tmp_path
    # Detached, delayed, and never a direct kickstart from the daemon itself.
    assert runner.spawned == [HELPER]
    assert KICK_DAEMON not in runner.argvs()
    assert restart_pending(db) == {
        "by": OWNER,
        "at": "2026-10-13T15:00:00.000+00:00",
        "commit": "abc1234",
    }


async def test_restart_resonant_without_a_repo_or_commit(db: sqlite3.Connection) -> None:
    runner = FakeOpsRunner()
    out = await make_ops(db, runner, None).restart_resonant()
    assert out["commit"] is None and runner.calls == [] and runner.spawned == [HELPER]

    runner = FakeOpsRunner().fail(*HEAD)
    out = await make_ops(db, runner, Path("/nowhere")).restart_resonant()
    assert out["commit"] is None and runner.spawned == [HELPER]


async def test_restart_resonant_spawn_failure_clears_the_marker(db: sqlite3.Connection) -> None:
    runner = FakeOpsRunner(spawn_error=OSError("no python"))
    with pytest.raises(ToolFailed, match="restart helper"):
        await make_ops(db, runner, None).restart_resonant()
    assert restart_pending(db) is None


# --- update_resonant ---------------------------------------------------------------------


async def test_update_pulls_syncs_and_restarts(db: sqlite3.Connection, tmp_path: Path) -> None:
    runner = FakeOpsRunner()
    clean_repo(runner)
    out = await make_ops(db, runner, tmp_path).update_resonant()
    assert out == {
        "old": "a1b2c3d",
        "new": "e4f5a6b",
        "updated": True,
        "synced": True,
        "restarting": True,
        "delay_s": 3,
    }
    assert runner.argvs() == [BRANCH, STATUS, HEAD, PULL, HEAD, sync()]
    assert {c.cwd for c in runner.calls} == {tmp_path}
    timeouts = {c.argv: c.timeout_s for c in runner.calls}
    assert timeouts[PULL] == PULL_TIMEOUT_S == 60 and timeouts[sync()] == SYNC_TIMEOUT_S == 240
    assert sum(c.timeout_s for c in runner.calls) < UPDATE_RESONANT.timeout_s
    assert runner.spawned == [HELPER]
    marker = restart_pending(db)
    assert marker is not None and marker["commit"] == "e4f5a6b"


async def test_update_refuses_a_dirty_tree(db: sqlite3.Connection, tmp_path: Path) -> None:
    runner = FakeOpsRunner().ok(*BRANCH, stdout="main\n").ok(*STATUS, stdout=" M README.md\n")
    with pytest.raises(ToolFailed, match="working tree has changes; not updating"):
        await make_ops(db, runner, tmp_path).update_resonant()
    assert runner.argvs() == [BRANCH, STATUS]  # nothing runs after the checks
    assert runner.spawned == [] and restart_pending(db) is None


@pytest.mark.parametrize("branch", ["feature\n", "HEAD\n"])  # HEAD: detached
async def test_update_refuses_off_main(db: sqlite3.Connection, tmp_path: Path, branch: str) -> None:
    runner = FakeOpsRunner().ok(*BRANCH, stdout=branch)
    with pytest.raises(ToolFailed, match="not on main; not updating"):
        await make_ops(db, runner, tmp_path).update_resonant()
    assert runner.argvs() == [BRANCH]
    assert runner.spawned == []


async def test_update_already_current_does_not_restart(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    runner = FakeOpsRunner()
    clean_repo(runner, new="a1b2c3d")
    out = await make_ops(db, runner, tmp_path).update_resonant()
    assert out == {
        "old": "a1b2c3d",
        "new": "a1b2c3d",
        "updated": False,
        "synced": None,
        "restarting": False,
    }
    assert sync() not in runner.argvs() and runner.spawned == []
    assert restart_pending(db) is None


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (ProcResult(128, "", "fatal: Not possible to fast-forward"), r"exit 128\); not updated"),
        (TimeoutError(), "git pull timed out after 60s"),
    ],
)
async def test_update_pull_failure(
    db: sqlite3.Connection, tmp_path: Path, result: ProcResult | BaseException, message: str
) -> None:
    runner = FakeOpsRunner()
    clean_repo(runner)
    runner.on(*PULL, results=[result])
    with pytest.raises(ToolFailed, match=message) as e:
        await make_ops(db, runner, tmp_path).update_resonant()
    assert "fatal" not in str(e.value)
    assert sync() not in runner.argvs() and runner.spawned == []


@pytest.mark.parametrize(
    ("result", "error"),
    [(ProcResult(1, "", "lock mismatch"), "exit 1"), (TimeoutError(), "timed out after 240s")],
)
async def test_update_sync_failure_reports_and_does_not_restart(
    db: sqlite3.Connection, tmp_path: Path, result: ProcResult | BaseException, error: str
) -> None:
    runner = FakeOpsRunner()
    clean_repo(runner)
    runner.on(*sync(), results=[result])
    out = await make_ops(db, runner, tmp_path).update_resonant()
    assert out == {
        "old": "a1b2c3d",
        "new": "e4f5a6b",
        "updated": True,
        "synced": False,
        "sync_error": error,
        "restarting": False,
    }
    assert runner.spawned == [] and restart_pending(db) is None
    line = report_line(intent(UPDATE_RESONANT), out)
    assert line.startswith("Updated a1b2c3d → e4f5a6b, but uv sync failed")
    assert "not restarting" in line


async def test_update_needs_repo_and_uv(db: sqlite3.Connection, tmp_path: Path) -> None:
    runner = FakeOpsRunner()
    with pytest.raises(ToolFailed, match="repo_path"):
        await make_ops(db, runner, None).update_resonant()
    with pytest.raises(ToolFailed, match="uv_path"):
        await make_ops(db, runner, tmp_path, uv=None).update_resonant()
    assert runner.calls == []


async def test_update_check_failure_is_definite(db: sqlite3.Connection, tmp_path: Path) -> None:
    runner = FakeOpsRunner().on(*BRANCH, results=[TimeoutError()])
    with pytest.raises(ToolFailed, match="git rev-parse timed out after 10s"):
        await make_ops(db, runner, tmp_path).update_resonant()


# --- reports and summaries ---------------------------------------------------------------


def intent(spec: Any, args: dict[str, Any] | None = None) -> Intent:
    return Intent.for_tool(spec, task_id="t", principal=OWNER, args=args or {})


def test_report_lines() -> None:
    assert report_line(intent(RESTART_OLLAMA), {"restarted": True}) == "Restarted Ollama."
    assert report_line(intent(RESTART_RESONANT), {"restarting": True}) == "Restarting Resonant…"
    upd = intent(UPDATE_RESONANT)
    done = {"old": "a1b2c3d", "new": "e4f5a6b", "updated": True, "restarting": True}
    assert report_line(upd, done) == "Updated a1b2c3d → e4f5a6b, restarting…"
    same = {"old": "a1b2c3d", "new": "a1b2c3d", "updated": False, "restarting": False}
    assert report_line(upd, same) == "Already up to date (a1b2c3d)."
    # An unrecognized result falls back to the summary.
    assert report_line(intent(RESTART_RESONANT), {}) == "Done: restart Resonant"


def make_gate(db: sqlite3.Connection, *, dry_run: bool) -> PolicyGate:
    from resonant.components import summary_actions

    registry = ToolRegistry()
    for spec in ops.SPECS:
        registry.register(spec)
    principals = Principals(principals={OWNER: Principal(role="owner")})
    with transaction(db):
        kv_set(db, "autonomy", "on")
    return PolicyGate(
        db, principals, registry, dry_run=dry_run, approval_ttl_s=1800, actions=summary_actions(db)
    )


def test_ops_tools_are_not_dry_run_exempt(db: sqlite3.Connection) -> None:
    gate = make_gate(db, dry_run=True)
    wording = [
        (RESTART_OLLAMA, "Dry run: would restart Ollama"),
        (RESTART_RESONANT, "Dry run: would restart Resonant"),
        (UPDATE_RESONANT, "Dry run: would update Resonant"),
    ]
    for spec, reply in wording:
        for origin in ("explicit", "inferred"):
            i = Intent.for_tool(spec, task_id="t", principal=OWNER, args={}, origin=origin)
            decision = gate.evaluate(i)
            assert isinstance(decision, DryRun)
            assert dry_run_reply(decision) == reply
    # L0 reads still run.
    for spec in (ops.DISK, ops.RECENT_LOGS):
        assert not isinstance(gate.evaluate(intent(spec)), DryRun)


def test_inferred_restart_asks_with_the_ops_heading(db: sqlite3.Connection) -> None:
    gate = make_gate(db, dry_run=False)
    task_id = insert_task(db, NewTask(kind="chat", runner="local", priority=PRIORITY_OWNER))
    i = Intent.for_tool(RESTART_RESONANT, task_id=task_id, principal=OWNER, args={})
    decision = gate.evaluate(i)
    assert isinstance(decision, NeedsApproval) and not decision.require_code
    assert decision.summary == "restart Resonant"


# --- "back up" ---------------------------------------------------------------------------


@dataclass
class Channel:
    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    keys: set[str] = field(default_factory=set[str])
    fail: list[BaseException] = field(default_factory=list[BaseException])

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        if self.fail:
            raise self.fail.pop(0)
        if dedupe_key is None or dedupe_key not in self.keys:
            self.sent.append((principal, text, dedupe_key))
        if dedupe_key is not None:
            self.keys.add(dedupe_key)
        return MessageRef("imessage", "x")


async def test_back_up_only_after_a_requested_restart(db: sqlite3.Connection) -> None:
    channel = Channel()
    assert not await back_up(db, channel, OWNER, clock=lambda: T0)
    assert channel.sent == []

    mark_restart(db, by=OWNER, commit="abc1234", now=T0)
    later = T0 + timedelta(seconds=6)
    assert await back_up(db, channel, OWNER, clock=lambda: later)
    key = "restart:2026-10-13T15:00:00.000+00:00"
    assert channel.sent == [(OWNER, "Back up (abc1234), down 6s", key)]
    assert restart_pending(db) is None

    # Once: the next start has nothing to say.
    assert not await back_up(db, channel, OWNER, clock=lambda: later)
    assert len(channel.sent) == 1


async def test_back_up_retries_after_a_send_failure(db: sqlite3.Connection) -> None:
    channel = Channel(fail=[ChannelSendError("Messages not running")])
    mark_restart(db, by=OWNER, commit=None, now=T0)
    assert not await back_up(db, channel, OWNER, clock=lambda: T0)
    assert restart_pending(db) is not None  # kept for the next start
    assert await back_up(db, channel, OWNER, clock=lambda: T0)
    assert channel.sent[0][1] == "Back up, down 0s"

    refused = Channel(fail=[ChannelRefused("no handle")])
    mark_restart(db, by=OWNER, commit=None, now=T0)
    assert not await back_up(db, refused, OWNER, clock=lambda: T0)
    assert restart_pending(db) is None  # a refusal won't get better by retrying


async def test_stale_marker_is_dropped_without_a_message(db: sqlite3.Connection) -> None:
    channel = Channel()
    mark_restart(db, by=OWNER, commit="abc1234", now=T0)
    later = T0 + timedelta(minutes=11)  # the kickstart never happened
    assert not await back_up(db, channel, OWNER, clock=lambda: later)
    assert channel.sent == [] and restart_pending(db) is None


async def test_subprocess_runner_kills_on_timeout(tmp_path: Path) -> None:
    """The real runner (harmless argv: a sleeping python) times out and kills it."""
    runner = SubprocessRunner()
    done = await runner.run([sys.executable, "-c", "print('hi')"], timeout_s=10, cwd=tmp_path)
    assert done == ProcResult(0, "hi\n", "")
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.5)
    assert time.monotonic() - start < 5
    assert runner.env["GIT_TERMINAL_PROMPT"] == "0"


async def test_no_daemon_env_var_reaches_an_ops_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real runner, a harmless argv (python printing its env): only the fixed keys."""
    for name in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "RESONANT_HOME", "PYTHONPATH", "DYLD_X"):
        monkeypatch.setenv(name, "daemon-only-value")
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin:/bin")
    runner = SubprocessRunner()
    dump = "import json, os; print(json.dumps(dict(os.environ)))"
    child: dict[str, str] = json.loads(
        (await runner.run([sys.executable, "-c", dump], timeout_s=10, cwd=tmp_path)).stdout
    )
    # Added by the child itself, not passed down: macOS's __CF_USER_TEXT_ENCODING, and
    # LC_CTYPE from Python's C-locale coercion (PEP 538) when no locale is set.
    child.pop("__CF_USER_TEXT_ENCODING", None)
    if child.get("LC_CTYPE") in {"UTF-8", "C.UTF-8"}:
        del child["LC_CTYPE"]
    assert "daemon-only-value" not in child.values()
    assert set(child) <= {"HOME", "USER", "LOGNAME", "TMPDIR"} | set(ops_env({}))
    assert child["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert child["GIT_TERMINAL_PROMPT"] == "0"


def test_ops_env_is_fixed() -> None:
    daemon = {"HOME": "/Users/x", "TMPDIR": "/tmp/x", "AWS_SECRET": "s", "PATH": "/evil:/bin"}
    env = ops_env(daemon)
    assert env["HOME"] == "/Users/x" and env["TMPDIR"] == "/tmp/x"
    assert "AWS_SECRET" not in env and env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert ops_env(daemon, {"PATH": "/bin"})["PATH"] == "/bin"  # an explicit extra wins


def test_back_up_text() -> None:
    marker = {"by": OWNER, "at": "2026-10-13T15:00:00.000+00:00", "commit": "abc1234"}
    assert back_up_text(marker, T0 + timedelta(seconds=75)) == "Back up (abc1234), down 1m"
    assert back_up_text({"at": "garbage"}, T0) == "Back up"
    noted = {**marker, "note": "Installed example"}
    assert back_up_text(noted, T0 + timedelta(seconds=75)) == (
        "Back up (abc1234), down 1m\nInstalled example"
    )


# --- disk and logs -----------------------------------------------------------------------


async def test_disk_lists_root_and_home(db: sqlite3.Connection, tmp_path: Path) -> None:
    from resonant.api import DaemonState
    from resonant.config import Settings
    from resonant.loop import Loop, SlotPool
    from resonant.tools.base import BuiltinContext

    settings: Settings = load_settings()
    state = DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)), started_at=T0)
    principals = Principals(principals={OWNER: Principal(role="owner")})
    handlers = ops.handlers(
        BuiltinContext(db, state, ToolRegistry(), principals, ops_runner=FakeOpsRunner())
    )
    out = await handlers["disk"]({})
    paths = [d["path"] for d in out["disk"]]
    assert paths == ["/", str(settings.home)]
    assert all(isinstance(d["free_bytes"], int) for d in out["disk"])
    with pytest.raises(ToolFailed):
        await handlers["disk"]({"x": 1})
    text = render_disk(
        {"disk": [{"path": "/", "free_bytes": 120.3e9, "total_bytes": 494.4e9, "used_pct": 75.7}]}
    )
    assert text == "Disk: / 120.3 GB free of 494.4 GB (75.7% used)"
    assert render_disk({}) == "Disk: unknown."


def test_scrub() -> None:
    raw = (
        "send to +15551234567 and (555) 123-4567 or 555.123.4567 failed; "
        "mail justin@example.com via https://hc-ping.com/abc?x=1 and www.example.com/x"
    )
    assert scrub(raw) == (
        "send to <handle> and <handle> or <handle> failed; mail <handle> via <url> and <url>"
    )
    # Timestamps, ids and counts survive.
    kept = "2026-10-13T15:00:00.123Z task 01JABCDEF took 1234 ms (3 calls)"
    assert scrub(kept) == kept


def test_log_line_is_short_and_scrubbed() -> None:
    rec = {
        "event": "imessage send to justin failed: +15551234567",
        "level": "warning",
        "logger": "resonant.gateway.imessage.channel",
        "timestamp": "2026-10-13T15:04:05.123456Z",
        "url": "https://example.com/secret-token",
        "attempt": 2,
    }
    assert log_line(json.dumps(rec)) == (
        "15:04:05 warning resonant.gateway.imessage.channel: imessage send to justin failed: "
        "<handle> url=<url> attempt=2"
    )
    assert len(log_line(json.dumps({"event": "x" * 1000}))) == 300
    assert log_line("plain text from mail@x.io") == "plain text from <handle>"


def test_recent_logs_tail_cap_and_args(tmp_path: Path) -> None:
    assert recent_logs(tmp_path, {}) == {"lines": [], "count": 0, "n": 10}  # no log yet
    log = tmp_path / "resonant.jsonl"
    log.write_text(
        "".join(json.dumps({"event": f"e{i}", "level": "info"}) + "\n" for i in range(50))
    )

    out = recent_logs(tmp_path, {})
    assert out["count"] == 10 and out["lines"][-1] == "info e49" and out["lines"][0] == "info e40"
    assert recent_logs(tmp_path, {"n": 3})["lines"] == ["info e47", "info e48", "info e49"]
    capped = recent_logs(tmp_path, {"n": 500})
    assert capped["n"] == 20 and capped["count"] == 20
    for bad in ({"n": 0}, {"n": "5"}, {"n": True}, {"lines": 3}):
        with pytest.raises(ToolFailed):
            recent_logs(tmp_path, bad)


def test_recent_logs_reads_only_the_tail_of_a_big_file(tmp_path: Path) -> None:
    log = tmp_path / "resonant.jsonl"
    line = json.dumps({"event": "x" * 200, "level": "info"}) + "\n"
    log.write_text(line * 5000 + json.dumps({"event": "last", "level": "info"}) + "\n")
    out = recent_logs(tmp_path, {"n": 2})
    assert out["lines"][-1] == "info last" and out["lines"][0].startswith("info xxx")


def fake_token(prefix: str, n: int = 36) -> str:
    """Built at runtime, so no token-shaped literal sits in the source."""
    return prefix + ("Ab3" * n)[:n]


def test_recent_logs_has_no_home_paths_or_tokens(tmp_path: Path) -> None:
    home = str(Path.home())
    sk, ghp, short = fake_token("sk-" + "ant-"), fake_token("gh" + "p_"), fake_token("sk-", 8)
    blob = ("Q9" + "x" * 10 + "Zk3") * 3  # 45 chars of base64-ish key material
    records = [
        {"event": "worker started", "cwd": f"{home}/code/resonant", "level": "info"},
        {"event": f"read /Users/someone/.ssh/id_ed25519 and {home}/.resonant/x"},
        {"event": f"auth with {sk}", "level": "warning"},
        {"event": "github push", "token": ghp, "header": f"Bearer {ghp}"},
        {"event": f"short key {short}", "key": blob},
        {"event": f"GITHUB_TOKEN={ghp} {home}/Library"},
    ]
    log = tmp_path / "resonant.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in records) + f"plain {sk} at {home}\n")
    lines = recent_logs(tmp_path, {"n": 20})["lines"]
    assert len(lines) == len(records) + 1
    text = "\n".join(lines)
    assert "/Users/" not in text and home not in text
    assert not re.search(r"\bsk-|\bghp_", text), text
    assert blob not in text and short not in text
    assert lines[0] == "info worker started cwd=~/code/resonant"
    assert lines[1] == "read ~/.ssh/id_ed25519 and ~/.resonant/x"
    assert lines[2] == "warning auth with [redacted]"


def test_clean_line_keeps_ids_shas_and_times() -> None:
    kept = (
        "2026-10-13T15:00:00.123Z task 01JABCDEFGHJKMNPQRSTVWXYZ0 sha "
        "e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3 trace 3f2b8c1e-7d4a-4b9e-9c2f-1a2b3c4d5e6f"
    )
    assert clean_line(kept) == kept
    model = "model=mistral-nemo-instruct-2407-Q4_K_M_gguf_v2-final-build"
    assert clean_line(model) == model  # a long name with - and _ isn't key material
    deep = f"cwd={Path.home()}/resonant-per-260/packages/resonant/src/Resonant2/tools failed"
    assert clean_line(deep) == "cwd=~/resonant-per-260/packages/resonant/src/Resonant2/tools failed"
    assert clean_line("x" * 1000).endswith("…") and len(clean_line("x" * 1000)) == 300


def test_strip_home() -> None:
    assert strip_home("/Users/a/x and /Users/bob.smith/y", home="/Users/a") == "~/x and ~/y"
    assert strip_home("/opt/x /var/root", home="/var/root") == "/opt/x ~"
    assert strip_home("/opt/x", home="/") == "/opt/x"


def test_tail_log_is_cleaned_the_same_way(tmp_path: Path) -> None:
    from resonant.tools.files import tail_log

    home, ghp = str(Path.home()), fake_token("gh" + "p_")
    (tmp_path / "resonant.jsonl").write_text(json.dumps({"event": f"{home}/x {ghp}"}) + "\n")
    (tmp_path / "daemon.log").write_text(f"boot {home}/y {ghp}\n")
    assert tail_log(tmp_path, {})["lines"] == ["~/x [redacted]"]
    assert tail_log(tmp_path, {"name": "daemon.log"})["lines"] == ["boot ~/y [redacted]"]


def test_render_logs_keeps_the_newest_within_one_chunk() -> None:
    lines = [f"{i:03d} " + "y" * 295 for i in range(20)]
    text = render_logs({"lines": lines})
    assert len(text) <= 2000 and text.endswith(lines[-1]) and lines[0] not in text
    assert render_logs({"lines": []}) == "No log lines."

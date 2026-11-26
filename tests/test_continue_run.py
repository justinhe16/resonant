"""PER-344: continuing a run that a limit stopped (``ClaudeRunner.continue_run``, the
``continue_run`` tool, ``/continue``): the same run code, Claude session and worktree, with a
fresh budget, finishing on its branch with a PR; and every refusal."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from test_runner_claude import Harness, build, nearly_full, opened, plenty_free, with_scenario

from resonant import killswitch
from resonant.executor import CallContext
from resonant.gate import Allow, Deny, Intent
from resonant.loop import NewTask
from resonant.router import commands
from resonant.runners import claude as claude_mod
from resonant.runners.claude import CONTINUE_HEAD, Continued, Refused, limit_of
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.store.tasks import insert_task

THREAD = {"channel": "test", "thread": "T1", "ref": "in:1", "principal": "owner"}


def last_prompt(h: Harness, code: str = "R1") -> dict[str, Any]:
    return h.argv_log(code)[-1]


def continue_rows(h: Harness) -> list[sqlite3.Row]:
    return h.conn.execute("SELECT detail FROM audit_log WHERE action = 'run.continued'").fetchall()


# --- the limit a failure names ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "limit"),
    [
        ("timed out at 45m (work kept: /runs R1)", "time"),
        ("used over 4.9 GiB of disk (5000 MiB folder cap; work kept: /runs R1)", "disk"),
        ("the disk is nearly full (5 GiB free, under the 10 GiB floor; work kept: /runs R1)",
         "low_disk"),
        ("the runner cache grew over 20 GiB (the cache cap; work kept: /runs R1)", None),
        ("Claude hit the turn limit (work kept: /runs R1)", None),
        ("Claude failed (exit 1) (work kept: /runs R1)", None),
        ("git failed: timed out at the remote", None),
        (None, None),
    ],
)  # fmt: skip
def test_limit_of_reads_only_the_code_built_limits(failure: str | None, limit: str | None) -> None:
    assert limit_of(failure) == limit


# --- a timed-out run is continued and finishes with its PR -----------------------------------


async def test_a_timed_out_run_is_continued_and_opens_its_pr(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="hang_then_ok", max_session_s=1)
    code = await h.start()
    await h.drain()
    before = h.run(code)
    assert (
        before.status == "failed" and before.failure == f"timed out at 1s (work kept: /runs {code})"
    )
    old_task = before.task_id
    h.runner.max_session_s = 60  # the fresh budget is the configured one

    res = await h.runner.continue_run(code)
    assert isinstance(res, Continued), res
    assert res.text == (
        "Continuing R1 · proj: it was stopped by the time limit; picking up its kept work. "
        "Up to 1m more. /runs to check, /cancel R1 to stop."
    )
    queued = h.run(code)
    assert queued.status == "queued" and queued.step == "session" and queued.failure is None
    assert queued.ended_at is None and queued.task_id not in (None, old_task)
    assert queued.max_seconds == int(before.session_seconds) + 60
    assert queued.task_id is not None
    task = task_store.get_task(db, queued.task_id)
    assert task is not None and task.checkpoint["continued"] == 1
    assert len(continue_rows(h)) == 1

    await h.drain()
    after = h.run(code)
    assert after.status == "ready" and after.pr_number is not None
    # the same run: its code, Claude session, branch and worktree
    assert (after.session_id, after.branch, after.workspace) == (
        before.session_id, before.branch, before.workspace,
    )  # fmt: skip
    assert after.sessions == before.sessions + 1
    resumed = last_prompt(h, code)
    assert resumed["argv"][resumed["argv"].index("--resume") + 1] == before.session_id
    assert resumed["prompt"].startswith(CONTINUE_HEAD) and "the time limit" in resumed["prompt"]
    assert "Finish the task you were given" in resumed["prompt"]
    assert "delete build outputs" not in resumed["prompt"]  # the tidy line is the disk's
    assert after.resume_prompt is None  # the turn ended: nothing to resend
    # both outcomes are texted: the timeout, then the PR (a second end isn't deduped away)
    texts = h.chan.texts()
    assert any("timed out" in t for t in texts)
    assert any(opened(code)(t) for t in texts)


async def test_a_continued_run_that_times_out_again_says_so_again(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="hang", max_session_s=1)
    code = await h.start()
    await h.drain()
    assert isinstance(await h.runner.continue_run(code), Continued)
    await h.drain()
    run = h.run(code)
    assert run.status == "failed" and run.failure == f"timed out at 2s (work kept: /runs {code})"
    assert sum("timed out" in t for t in h.chan.texts()) == 2
    # and once more: the counter goes on
    assert isinstance(await h.runner.continue_run(code), Continued)
    task = task_store.get_task(db, cast(str, h.run(code).task_id))
    assert task is not None and task.checkpoint["continued"] == 2
    await h.drain()


# --- the disk ------------------------------------------------------------------------------


async def test_a_run_stopped_by_the_disk_cap_is_continued_with_a_larger_cap(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="disk", disk_limit_bytes=3_000_000, disk_interval_s=0.1)
    code = await h.start()
    await h.drain()
    assert limit_of(h.run(code).failure) == "disk"
    with_scenario(h, "happy")  # the 4 MB it wrote stays: the next cap is 6 MB
    res = await h.runner.continue_run(code)
    assert isinstance(res, Continued) and res.why == "the per-run disk cap"
    await h.drain()
    run = h.run(code)
    assert run.status == "ready" and run.pr_number is not None
    prompt = last_prompt(h, code)["prompt"]
    assert "the per-run disk cap" in prompt and "delete build outputs" in prompt


async def test_a_run_stopped_by_a_nearly_full_disk_waits_for_space(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="diskgrow", disk_interval_s=0.2, disk_free=nearly_full)
    code = await h.start()
    await h.drain()
    assert limit_of(h.run(code).failure) == "low_disk"
    res = await h.runner.continue_run(code)
    assert isinstance(res, Refused)
    assert res.reason == (
        "The disk is still nearly full (5 GiB free, under the 10 GiB floor). Free some space, "
        "then continue R1."
    )
    assert h.run(code).status == "failed"  # untouched
    h.runner._disk_free = plenty_free  # pyright: ignore[reportPrivateUsage]
    with_scenario(h, "happy")
    assert isinstance(await h.runner.continue_run(code), Continued)
    await h.drain()
    assert h.run(code).status == "ready"


# --- refusals ------------------------------------------------------------------------------


async def test_refuses_a_run_that_failed_for_another_reason(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="error")
    code = await h.start()
    await h.drain()
    assert h.run(code).status == "failed"
    res = await h.runner.continue_run(code)
    assert isinstance(res, Refused) and "failed for another reason" in res.reason
    assert h.run(code).status == "failed" and continue_rows(h) == []


async def test_refuses_when_the_work_is_gone(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, scenario="hang", max_session_s=1)
    code = await h.start()
    await h.drain()
    shutil.rmtree(h.worktree(code))  # what the daily cleanup does after keep_days
    res = await h.runner.continue_run(code)
    assert isinstance(res, Refused)
    assert res.reason == (
        "R1's work is gone (its worktree was cleaned up), so it can't be continued. "
        "/run to start it again."
    )
    assert h.run(code).status == "failed"


async def test_refuses_when_the_run_dir_is_gone(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, scenario="hang", max_session_s=1)
    code = await h.start()
    await h.drain()
    shutil.rmtree(h.code_dir(code))
    res = await h.runner.continue_run(code)
    assert isinstance(res, Refused) and "work is gone" in res.reason


async def test_refuses_runs_that_are_going_ended_well_or_unknown(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    assert await h.runner.continue_run("R9") == Refused("No run R9.")
    code = await h.start()
    res = await h.runner.continue_run(code)  # queued, not stepped yet
    assert res == Refused("R1 is still going (queued); nothing to continue.")
    await h.drain()
    assert h.run(code).status == "ready"
    res = await h.runner.continue_run(code)
    assert res == Refused("R1 ended ready, not on a limit: nothing to continue.")


async def test_refuses_a_cancelled_run(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, scenario="hang")
    code = await h.start()
    await h.runner.cancel_run(code)
    res = await h.runner.continue_run(code)
    assert res == Refused("R1 was cancelled and its work discarded. /run to start over.")


async def test_a_second_continue_is_refused_and_paused_refuses(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="hang", max_session_s=1)
    code = await h.start()
    await h.drain()
    killswitch.engage(db, actor="owner")
    res = await h.runner.continue_run(code)
    assert res == Refused("Resonant is paused. /resume first.")
    killswitch.release(db, actor="owner")
    assert isinstance(await h.runner.continue_run(code), Continued)
    res = await h.runner.continue_run(code)
    assert res == Refused("R1 is still going (queued); nothing to continue.")
    assert len(continue_rows(h)) == 1
    await h.drain()


async def test_refuses_while_the_failed_task_is_still_wrapping_up(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="hang", max_session_s=1)
    code = await h.start()
    await h.drain()
    task_id = cast(str, h.run(code).task_id)
    db.execute("UPDATE tasks SET status = 'queued' WHERE id = ?", (task_id,))  # a reply retry
    res = await h.runner.continue_run(code)
    assert res == Refused("R1 is still wrapping up; try again in a moment.")


async def test_reopen_only_takes_a_failed_run(tmp_path: Path, db: sqlite3.Connection) -> None:
    run = runs_store.create(db, repo="proj", ask="x", principal="owner", session_id="s",
                            max_seconds=60)  # fmt: skip
    assert not runs_store.reopen(db, run.code, max_seconds=120, resume_prompt="p")
    runs_store.finish(db, run.code, "failed", failure="timed out at 1m", step="reply")
    assert runs_store.reopen(db, run.code, max_seconds=120, resume_prompt="p")
    got = runs_store.get(db, run.code)
    assert got is not None and (got.status, got.step, got.max_seconds) == ("queued", "session", 120)
    assert got.failure is None and got.ended_at is None and got.resume_prompt == "p"
    assert not runs_store.reopen(db, run.code, max_seconds=120, resume_prompt="p")


# --- the start line in the run's thread ---------------------------------------------------


async def test_a_brain_continued_run_says_continuing_in_its_thread(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="hang_then_ok", max_session_s=1)
    res = await h.runner.start_run("proj", "fix the flaky test", "owner", report="now",
                                   channel_ref=THREAD)  # fmt: skip
    assert not isinstance(res, Refused)
    await h.drain()
    h.runner.max_session_s = 60
    assert isinstance(await h.runner.continue_run("R1", announce=True), Continued)
    await h.drain()
    assert "Continuing R1 (fix the flaky test): it was stopped by the time limit." in (
        h.chan.texts()
    )
    assert h.run("R1").status == "ready"


# --- the tool and the command ----------------------------------------------------------------


PRINCIPALS = """\
principals:
  owner:
    role: owner
    identities: ["imessage:+15550000000"]
  lead:
    role: approver
    identities: ["slack:U22222222"]
  cofounder:
    identities: ["slack:U11111111"]
    grants:
      example: [request]
"""


class FakeContinue:
    """The runner's ``continue_run``, as the tool calls it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def setup_missing(self) -> str | None:
        return None

    async def continue_run(self, code: str, *, announce: bool = False) -> Continued | Refused:
        self.calls.append((code, announce))
        if code == "R9":
            return Refused("No run R9.")
        return Continued(code, "proj", "the time limit", 45)


@pytest.fixture
def tool_parts(tmp_path: Path, db: sqlite3.Connection) -> tuple[Any, FakeContinue]:
    from resonant.api import DaemonState
    from resonant.components import build_components
    from resonant.config import RunnerConfig, load_settings
    from resonant.loop import Loop, SlotPool
    from resonant.store.db import utcnow

    base = load_settings()
    settings = base.model_copy(
        update={"runner": RunnerConfig(code_dir=tmp_path / "code"), "dry_run": False}
    )
    settings.principals_path.write_text(PRINCIPALS)
    state = DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)), utcnow())
    components = build_components(settings, db, state)
    state.components = components
    fake = FakeContinue()
    components.runners["claude"] = cast(Any, fake)
    return components, fake


def _task(db: sqlite3.Connection, principal: str) -> str:
    return insert_task(db, NewTask(kind="chat", runner="local", priority=1, principal=principal))


async def test_the_tool_continues_for_the_owner_and_an_approver(
    tool_parts: tuple[Any, FakeContinue], db: sqlite3.Connection
) -> None:
    components, fake = tool_parts
    handler = components.handlers[(None, "continue_run")]
    for who in ("owner", "lead"):
        ctx = CallContext(secrets={}, idempotency_key=f"k-{who}", task_id=_task(db, who),
                          origin="explicit")  # fmt: skip
        out = await handler({"code": "r1"}, ctx)
        assert out["continued"] is True and out["code"] == "R1" and "announced" not in out
        assert out["text"].startswith("Continuing R1 · proj")
    assert fake.calls == [("R1", False), ("R1", False)]


async def test_the_tool_refuses_a_member_and_passes_a_refusal_on(
    tool_parts: tuple[Any, FakeContinue], db: sqlite3.Connection
) -> None:
    components, fake = tool_parts
    handler = components.handlers[(None, "continue_run")]
    ctx = CallContext(secrets={}, idempotency_key="k-m", task_id=_task(db, "cofounder"))
    out = await handler({"code": "R1"}, ctx)
    assert out == {"continued": False, "code": "R1",
                   "text": "Only the owner or an approver can continue a run."}  # fmt: skip
    assert fake.calls == []
    out = await handler({"code": "R9"}, None)
    assert out == {"continued": False, "code": "R9", "text": "No run R9."}


async def test_the_brain_continuing_a_run_lets_the_run_announce_itself(
    tool_parts: tuple[Any, FakeContinue], db: sqlite3.Connection
) -> None:
    components, fake = tool_parts
    handler = components.handlers[(None, "continue_run")]
    ctx = CallContext(secrets={}, idempotency_key="k-b", task_id=_task(db, "owner"),
                      origin="inferred")  # fmt: skip
    out = await handler({"code": "R1"}, ctx)
    assert out["continued"] is True and out["announced"] is True
    assert fake.calls == [("R1", True)]


def test_the_gate_lets_the_owner_and_an_approver_and_denies_a_member(
    tool_parts: tuple[Any, FakeContinue], db: sqlite3.Connection
) -> None:
    components, _ = tool_parts
    spec = components.registry.get(None, "continue_run")
    assert spec is not None and spec.effective_level.name == "L3"

    def decide(who: str) -> object:
        task = _task(db, who)
        intent = Intent.for_tool(spec, task_id=task, principal=who, args={"code": "R1"},
                                 origin="inferred")  # fmt: skip
        return components.gate.evaluate(intent)

    assert isinstance(decide("owner"), Allow)
    assert isinstance(decide("lead"), Allow)
    assert isinstance(decide("cofounder"), Deny)


def test_slash_continue_parses_a_run_code() -> None:
    cmd = commands.get("/continue")
    assert cmd is not None and cmd.tool == "continue_run" and cmd.owner_only and cmd.staff
    assert commands.run_code_args("r12") == {"code": "R12"}


def test_the_continue_prompt_is_code_built() -> None:
    text = claude_mod.CONTINUE_PROMPT.format(why="the time limit", tidy="")
    assert text.startswith(CONTINUE_HEAD + ": it hit the time limit.")

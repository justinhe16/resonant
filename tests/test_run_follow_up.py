"""PER-354: a run carries a follow-up. When it ends, exactly one brain ask is queued in the run's
current thread, as its current requester, with the instruction and how it ended; a continued
run follows up where it was continued; no follow-up, no ask; the chain is bounded."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from test_continue_run import THREAD_A, THREAD_B, FakeContinue, record_threads, tool_parts
from test_runner_claude import Harness, build
from test_runner_fork import merge_harness

from resonant.executor import CallContext, ToolFailed
from resonant.loop import NewTask
from resonant.runners.ask import FOLLOW_UP_KEY, AskRefused, Queued
from resonant.runners.claude import Continued, Started
from resonant.store import runs as runs_store
from resonant.store.db import iso
from resonant.store.tasks import insert_task

__all__ = ["tool_parts"]  # the fixture, imported for the tool tests below

FIX = "fix the flaky test"
LGTM = "if the review says LGTM, merge the PR"


class FollowUps:
    """A fake ask runner that inserts the task like the real one (its checkpoint markers)."""

    def __init__(self, db: sqlite3.Connection, refuse: str | None = None) -> None:
        self.db = db
        self.refuse = refuse
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, question: str, **kw: Any) -> Queued | AskRefused:
        self.calls.append({"question": question, **kw})
        if self.refuse is not None:
            return AskRefused(self.refuse, "busy")
        cp = {"question": question, FOLLOW_UP_KEY: kw["follow_up"],
              runs_store.FOLLOW_UP_DEPTH_KEY: kw["follow_up_depth"]}  # fmt: skip
        task = insert_task(
            self.db,
            NewTask(kind="ask", runner="ask", priority=1, principal=kw["principal"],
                    checkpoint=cp, channel_ref=kw["channel_ref"]),
        )  # fmt: skip
        return Queued(task)


def rig(
    tmp_path: Path, db: sqlite3.Connection, refuse: str | None = None, **kw: Any
) -> tuple[Harness, FollowUps]:
    asks = FollowUps(db, refuse)
    h = build(tmp_path, db, asks=lambda: asks, **kw)
    return h, asks


async def started(h: Harness, *, follow_up: str | None = LGTM, depth: int = 0,
                  ref: dict[str, Any] | None = THREAD_A, requested_by: str | None = None,
                  ) -> str:  # fmt: skip
    res = await h.runner.start_run("proj", FIX, "owner", report="now", channel_ref=ref,
                                   requested_by=requested_by, follow_up=follow_up,
                                   follow_up_depth=depth)  # fmt: skip
    assert isinstance(res, Started), res
    return res.code


def follow_rows(db: sqlite3.Connection) -> list[str]:
    rows = db.execute(
        "SELECT decision FROM audit_log WHERE action = 'run.follow_up' ORDER BY id"
    ).fetchall()
    return [r[0] for r in rows]


# --- one ask, in the run's thread ------------------------------------------------------------


async def test_a_run_with_a_follow_up_queues_one_ask_in_its_thread(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h, asks = rig(tmp_path, db)
    code = await started(h)
    assert h.run(code).follow_up == LGTM and h.run(code).follow_up_depth == 0
    await h.drain()
    run = h.run(code)
    assert run.status == "ready" and run.pr_number is not None and run.ended_at is not None
    (q,) = asks.calls
    assert q["question"] == (
        f"R1 ({FIX}) ended: ready for review, PR #{run.pr_number} {run.pr_url} "
        f"({run.summary}). You asked yourself to do this when it ended: {LGTM}"
    )
    assert (q["principal"], q["channel_ref"], q["tools"], q["tainted"], q["taint_hops"]) == (
        "owner", THREAD_A, True, True, 0,
    )  # fmt: skip
    assert q["follow_up"] == f"run/R1/{iso(run.ended_at)}"
    assert q["follow_up_depth"] == 1 and "requested_by" not in q
    assert follow_rows(db) == ["queued"]
    # a retry or restart of the run's end never queues it twice
    await h.runner._follow_up(run)  # pyright: ignore[reportPrivateUsage]
    await h.runner._conclude(run)  # pyright: ignore[reportPrivateUsage]
    assert len(asks.calls) == 1 and follow_rows(db) == ["queued"]


async def test_no_follow_up_means_no_ask(tmp_path: Path, db: sqlite3.Connection) -> None:
    h, asks = rig(tmp_path, db)
    code = await started(h, follow_up=None)
    await h.drain()
    assert h.run(code).status == "ready" and h.run(code).follow_up is None
    assert asks.calls == [] and follow_rows(db) == []


@pytest.mark.parametrize(("scenario", "status"), [("nochanges", "no_changes"), ("error", "failed")])
async def test_a_run_that_ends_without_a_pr_follows_up_too(
    tmp_path: Path, db: sqlite3.Connection, scenario: str, status: str
) -> None:
    h, asks = rig(tmp_path, db, scenario=scenario)
    code = await started(h, follow_up="after it, tell the channel")
    await h.drain()
    assert h.run(code).status == status
    (q,) = asks.calls
    assert q["question"].startswith(f"R1 ({FIX}) ended: ")
    assert q["question"].endswith("You asked yourself to do this when it ended: "
                                  "after it, tell the channel")  # fmt: skip


async def test_a_cancelled_run_follows_up_nothing(tmp_path: Path, db: sqlite3.Connection) -> None:
    h, asks = rig(tmp_path, db, scenario="hang")
    code = await started(h)
    await h.runner.cancel_run(code)
    await h.drain()
    assert h.run(code).status == "cancelled" and asks.calls == []


async def test_a_members_run_follows_up_as_that_member(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h, asks = rig(tmp_path, db)
    theirs = {**THREAD_A, "principal": "alice"}
    await started(h, ref=theirs, requested_by="alice")
    await h.drain()
    (q,) = asks.calls
    assert (q["principal"], q["channel_ref"]) == ("alice", theirs)


async def test_a_run_with_no_thread_follows_up_in_the_requesters_own_conversation(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h, asks = rig(tmp_path, db)
    await started(h, ref=None)
    await h.drain()
    (q,) = asks.calls
    assert (q["principal"], q["channel_ref"]) == ("owner", None)


# --- a continued run follows up where it was continued ---------------------------------------


async def test_a_continued_run_keeps_its_follow_up_and_asks_in_the_new_thread(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h, asks = rig(tmp_path, db, scenario="hang_then_ok", max_session_s=1)
    theirs = {**THREAD_A, "principal": "alice"}
    code = await started(h, ref=theirs, requested_by="alice")
    await h.drain()
    assert h.run(code).status == "failed"
    (first,) = asks.calls  # the timeout is an end too: alice hears it in her thread
    assert (first["principal"], first["channel_ref"]) == ("alice", theirs)
    assert " ended: failed (timed out at 1s" in first["question"]
    h.runner.max_session_s = 60

    res = await h.runner.continue_run(code, channel_ref=THREAD_B, requested_by="owner",
                                      follow_up_depth=1)  # fmt: skip
    assert isinstance(res, Continued), res
    assert h.run(code).follow_up == LGTM and h.run(code).follow_up_depth == 1
    await h.drain()
    run = h.run(code)
    assert run.status == "ready"
    _, second = asks.calls
    assert (second["principal"], second["channel_ref"]) == ("owner", THREAD_B)
    assert second["follow_up"] == f"run/R1/{iso(cast(Any, run.ended_at))}"
    assert second["follow_up"] != first["follow_up"] and second["follow_up_depth"] == 2
    assert " ended: ready for review, PR #" in second["question"]


async def test_a_continue_never_lowers_the_depth(tmp_path: Path, db: sqlite3.Connection) -> None:
    h, _ = rig(tmp_path, db, scenario="hang", max_session_s=1)
    code = await started(h, depth=2)
    await h.drain()
    h.runner.max_session_s = 60
    assert isinstance(await h.runner.continue_run(code, follow_up_depth=1), Continued)
    assert h.run(code).follow_up_depth == 2
    await h.runner.cancel_run(code)
    await h.drain()


# --- the chain is bounded ---------------------------------------------------------------------


async def test_a_run_at_the_depth_cap_queues_no_ask_and_says_so(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h, asks = rig(tmp_path, db)
    sent = record_threads(h)
    await started(h, depth=runs_store.FOLLOW_UP_DEPTH_MAX)
    await h.drain()
    assert asks.calls == [] and follow_rows(db) == ["refused"]
    note = f"R1: couldn't follow up ({runs_store.FOLLOW_UP_DEPTH_MAX} follow-ups deep already): "
    assert ("owner", note + LGTM, "C0TEAM:100.1") in sent


async def test_a_refused_ask_is_said_once_in_the_thread(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h, asks = rig(tmp_path, db, refuse="Claude is busy")
    sent = record_threads(h)
    code = await started(h)
    await h.drain()
    assert len(asks.calls) == 1 and follow_rows(db) == ["refused"]
    note = f"R1: couldn't follow up (Claude is busy): {LGTM}"
    assert [t for _, t, _ in sent].count(note) == 1
    await h.runner._follow_up(h.run(code))  # pyright: ignore[reportPrivateUsage]
    assert [t for _, t, _ in sent].count(note) == 1  # deduped


async def test_no_ask_runner_is_said_too(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db)  # no asks: ask_claude isn't running
    sent = record_threads(h)
    await started(h)
    await h.drain()
    assert f"R1: couldn't follow up (ask_claude isn't running): {LGTM}" in [t for _, t, _ in sent]


# --- an auto-merge follows up once it is merged ------------------------------------------------


async def test_an_auto_merged_run_follows_up_once_after_the_merge(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    asks = FollowUps(db)
    h = merge_harness(tmp_path, db, asks=lambda: asks)
    res = await h.runner.start_run("proj", "fix it and merge it", "owner", merge=True,
                                   report="now", follow_up="then reinstall the agent")  # fmt: skip
    assert isinstance(res, Started), res
    code = res.code
    h.loop._stopping = False  # pyright: ignore[reportPrivateUsage]
    running = asyncio.ensure_future(h.loop.run())
    try:
        for _ in range(800):
            if asks.calls:
                break
            await asyncio.sleep(0.05)
    finally:
        h.loop.stop()
        await running
    assert h.run(code).status == "merged"
    (q,) = asks.calls
    assert q["question"].startswith(f"{code} (fix it and merge it) ended: merged, PR #41 ")


# --- the prompt -------------------------------------------------------------------------------


async def test_the_prompt_is_code_built_and_clipped(tmp_path: Path, db: sqlite3.Connection) -> None:
    h, _ = rig(tmp_path, db)
    await started(h, follow_up="post here")
    base = h.run("R1")
    long = replace(base, status="failed", failure="x " * 500, ask="a\nb " * 60)
    text = runs_store.follow_up_prompt(long)
    assert text.startswith("R1 (a b a b") and "…) ended: failed (x x" in text
    assert text.endswith("…). You asked yourself to do this when it ended: post here")
    done = replace(base, status="done", summary=None)
    assert runs_store.follow_up_prompt(done) == (
        f"R1 ({FIX}) ended: done. You asked yourself to do this when it ended: post here"
    )
    await h.runner.cancel_run("R1")


# --- the tools --------------------------------------------------------------------------------


class FakeStart(FakeContinue):
    """The runner's ``start_run`` and ``continue_run``, as the tools call them."""

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[dict[str, Any]] = []
        self.continues: list[dict[str, Any]] = []

    async def start_run(self, repo: str | None, ask: str, principal: str,
                        **kw: Any) -> Started:  # fmt: skip
        self.starts.append({"repo": repo, "ask": ask, **kw})
        return Started("R1", repo or "", 45)

    async def continue_run(self, code: str, **kw: Any) -> Continued:  # type: ignore[override]
        self.continues.append({"code": code, **kw})
        return Continued(code, "proj", "the time limit", 45)


def _ask_task(db: sqlite3.Connection, depth: int | None) -> str:
    cp: dict[str, Any] = {"question": "R0 ended"}
    if depth is not None:
        cp.update({FOLLOW_UP_KEY: "run/R0/x", runs_store.FOLLOW_UP_DEPTH_KEY: depth})
    return insert_task(db, NewTask(kind="ask", runner="ask", priority=1, principal="owner",
                                   checkpoint=cp, channel_ref=THREAD_A))  # fmt: skip


def _ctx(task_id: str) -> CallContext:
    return CallContext(secrets={}, idempotency_key=f"k-{task_id}", task_id=task_id)


async def test_start_run_takes_a_follow_up_and_passes_the_depth_on(
    tool_parts: tuple[Any, FakeContinue], db: sqlite3.Connection
) -> None:
    components, _ = tool_parts
    fake = FakeStart()
    components.runners["claude"] = cast(Any, fake)
    handler = components.handlers[(None, "start_run")]
    out = await handler(
        {"ask": "review PR 12", "follow_up": f"  {LGTM}\n"}, _ctx(_ask_task(db, None))
    )
    assert out["started"] is True
    out = await handler({"ask": "reinstall", "follow_up": "say it's done"}, _ctx(_ask_task(db, 2)))
    assert out["started"] is True
    plain = await handler({"ask": "no promise"}, _ctx(_ask_task(db, None)))
    assert plain["started"] is True
    first, second, third = fake.starts
    assert first["follow_up"] == LGTM and "follow_up_depth" not in first
    assert (second["follow_up"], second["follow_up_depth"]) == ("say it's done", 2)
    assert "follow_up" not in third and "follow_up_depth" not in third
    with pytest.raises(ToolFailed, match="follow_up"):
        await handler({"ask": "x", "follow_up": 3}, _ctx(_ask_task(db, None)))


async def test_start_run_refuses_a_follow_up_at_the_depth_cap(
    tool_parts: tuple[Any, FakeContinue], db: sqlite3.Connection
) -> None:
    components, _ = tool_parts
    fake = FakeStart()
    components.runners["claude"] = cast(Any, fake)
    handler = components.handlers[(None, "start_run")]
    deep = _ctx(_ask_task(db, runs_store.FOLLOW_UP_DEPTH_MAX))
    with pytest.raises(ToolFailed, match="already 3 deep"):
        await handler({"ask": "again", "follow_up": "and again"}, deep)
    assert fake.starts == []
    out = await handler({"ask": "again"}, deep)  # without a follow-up it still starts
    assert out["started"] is True
    assert fake.starts[0]["follow_up_depth"] == runs_store.FOLLOW_UP_DEPTH_MAX


async def test_continue_run_passes_a_follow_up_asks_depth(
    tool_parts: tuple[Any, FakeContinue], db: sqlite3.Connection
) -> None:
    components, _ = tool_parts
    fake = FakeStart()
    components.runners["claude"] = cast(Any, fake)
    handler = components.handlers[(None, "continue_run")]
    await handler({"code": "R1"}, _ctx(_ask_task(db, 2)))
    await handler({"code": "R1"}, _ctx(_ask_task(db, None)))
    first, second = fake.continues
    assert first["follow_up_depth"] == 2 and "follow_up_depth" not in second


def test_old_runs_have_no_follow_up(db: sqlite3.Connection) -> None:
    run = runs_store.create(db, repo="", ask="x", principal="owner", session_id="s",
                            max_seconds=60)  # fmt: skip
    assert (run.follow_up, run.follow_up_depth) == (None, 0)
    assert (
        runs_store.chain_depth(None) == 0 and runs_store.chain_depth({"follow_up_depth": True}) == 0
    )

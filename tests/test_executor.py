from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest

from resonant.executor import Executor, NotAllowedError, ToolFailed
from resonant.gate import Allow, DryRun, Intent
from resonant.loop import NewTask
from resonant.store.tasks import insert_task
from resonant_sdk import Effect, Level, ToolSpec

WRITE = ToolSpec(name="label", effect=Effect.WRITE, level=Level.L3, extension="ext", timeout_s=0.05)
READ = ToolSpec(name="peek", effect=Effect.READ, extension="ext", timeout_s=0.05)


class FakeGate:
    """Issues tokens like a real gate; consume() is single-use and hash-bound."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}

    def allow(self, intent: Intent) -> Allow:
        token = f"tok{len(self.tokens)}"
        self.tokens[token] = intent.hash
        return Allow(token=token)

    def consume(self, token: str, intent_hash: str) -> bool:
        return self.tokens.pop(token, None) == intent_hash


@pytest.fixture
def task_id(db: sqlite3.Connection) -> str:
    return insert_task(db, NewTask(kind="k", runner="echo", priority=1))


def make(task_id: str, spec: ToolSpec = WRITE) -> Intent:
    return Intent.for_tool(spec, task_id=task_id, principal="justin", args={"x": 1})


def actions(db: sqlite3.Connection) -> list[str]:
    return [r["action"] for r in db.execute("SELECT action FROM audit_log ORDER BY id")]


async def test_executes_once_and_replays(db: sqlite3.Connection, task_id: str) -> None:
    calls: list[dict[str, Any]] = []

    async def label(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(args)
        return {"labelled": True}

    gate = FakeGate()
    ex = Executor(db, gate, {("ext", "label"): label})
    i = make(task_id)
    first = await ex.execute(i, gate.allow(i), step_no=1)
    again = await ex.execute(i, gate.allow(i), step_no=1)
    assert first.ok and first.data == {"labelled": True} and not first.replayed
    assert again.replayed and len(calls) == 1
    other_call = await ex.execute(i, gate.allow(i), step_no=1, call_index=1)
    assert other_call.ok and not other_call.replayed and len(calls) == 2
    assert actions(db)[:3] == ["executor.start", "executor.result", "executor.replayed"]


async def test_forged_or_reused_allow_refused(db: sqlite3.Connection, task_id: str) -> None:
    gate = FakeGate()
    ex = Executor(db, gate, {})
    i = make(task_id)
    with pytest.raises(NotAllowedError):
        await ex.execute(i, Allow(token="forged"), step_no=1)
    with pytest.raises(NotAllowedError):
        await ex.execute(i, DryRun(would="Allow"), step_no=1)
    decision = gate.allow(Intent.for_tool(WRITE, task_id=task_id, principal="justin", args={}))
    with pytest.raises(NotAllowedError):
        await ex.execute(i, decision, step_no=1)  # token issued for different args
    assert actions(db) == ["executor.refused"] * 3


async def test_write_timeout_is_ambiguous_and_never_rerun(
    db: sqlite3.Connection, task_id: str
) -> None:
    calls = 0

    async def slow(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)
        return {}

    gate = FakeGate()
    ex = Executor(db, gate, {("ext", "label"): slow})
    i = make(task_id)
    res = await ex.execute(i, gate.allow(i), step_no=1)
    assert res.ambiguous and not res.ok
    again = await ex.execute(i, gate.allow(i), step_no=1)
    assert again.ambiguous and calls == 1
    assert res.idempotency_key is not None
    assert ex.resolve_ambiguous(res.idempotency_key, succeeded=True, actor="justin")
    replay = await ex.execute(i, gate.allow(i), step_no=1)
    assert replay.replayed and calls == 1
    assert "executor.ambiguous_resolved" in actions(db)


async def test_read_timeout_is_a_plain_failure(db: sqlite3.Connection, task_id: str) -> None:
    async def slow(args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {}

    gate = FakeGate()
    ex = Executor(db, gate, {("ext", "peek"): slow})
    i = make(task_id, READ)
    res = await ex.execute(i, gate.allow(i), step_no=1)
    assert not res.ok and not res.ambiguous


async def test_definite_failure_can_retry(db: sqlite3.Connection, task_id: str) -> None:
    attempts = 0

    async def flaky(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ToolFailed("rate limited before acting")
        return {}

    gate = FakeGate()
    ex = Executor(db, gate, {("ext", "label"): flaky})
    i = make(task_id)
    first = await ex.execute(i, gate.allow(i), step_no=1)
    assert not first.ok and not first.ambiguous
    assert (await ex.execute(i, gate.allow(i), step_no=1)).ok


async def test_unexpected_error_and_bad_result_are_ambiguous_for_writes(
    db: sqlite3.Connection, task_id: str
) -> None:
    async def boom(args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("connection reset")

    async def nan(args: dict[str, Any]) -> dict[str, Any]:
        return {"x": float("nan")}

    gate = FakeGate()
    i = make(task_id)
    assert (
        await Executor(db, gate, {("ext", "label"): boom}).execute(i, gate.allow(i), step_no=1)
    ).ambiguous
    assert (
        await Executor(db, gate, {("ext", "label"): nan}).execute(i, gate.allow(i), step_no=2)
    ).ambiguous


async def test_cancel_marks_ambiguous(db: sqlite3.Connection, task_id: str) -> None:
    started = asyncio.Event()

    async def hang(args: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(10)
        return {}

    spec = ToolSpec(name="label", effect=Effect.WRITE, level=Level.L3, extension="ext")
    gate = FakeGate()
    ex = Executor(db, gate, {("ext", "label"): hang})
    i = make(task_id, spec)
    run = asyncio.create_task(ex.execute(i, gate.allow(i), step_no=1))
    await started.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert actions(db)[-1] == "executor.ambiguous"
    assert (await ex.execute(i, gate.allow(i), step_no=1)).ambiguous


async def test_handlers_keyed_by_extension(db: sqlite3.Connection, task_id: str) -> None:
    async def a(args: dict[str, Any]) -> dict[str, Any]:
        return {"from": "a"}

    other = ToolSpec(name="label", effect=Effect.WRITE, level=Level.L3, extension="other")
    gate = FakeGate()
    ex = Executor(db, gate, {("ext", "label"): a})
    i = make(task_id, other)
    res = await ex.execute(i, gate.allow(i), step_no=1)
    assert not res.ok and "no handler for other/label" in (res.error or "")

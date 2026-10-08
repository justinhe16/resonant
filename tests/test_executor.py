from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from resonant.executor import Executor, NotAllowedError, idempotency_key
from resonant.gate import Allow, DryRun, Intent
from resonant.loop import NewTask
from resonant.store.tasks import insert_task
from resonant_sdk import Effect, Level


@pytest.fixture
def intent(db: sqlite3.Connection) -> Intent:
    task_id = insert_task(db, NewTask(kind="k", runner="echo", priority=1))
    return Intent(task_id, "justin", "ext", "label", {"x": 1}, Effect.WRITE, Level.L3)


async def test_executes_once_and_replays(db: sqlite3.Connection, intent: Intent) -> None:
    calls: list[dict[str, Any]] = []

    async def label(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(args)
        return {"labelled": True}

    ex = Executor(db, {"label": label})
    first = await ex.execute(intent, Allow(), step_no=1)
    again = await ex.execute(intent, Allow(), step_no=1)
    assert first.ok and first.data == {"labelled": True} and not first.replayed
    assert again.replayed and again.data == {"labelled": True}
    assert len(calls) == 1
    actions = [r["action"] for r in db.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["executor.start", "executor.result"]


async def test_unfinished_attempt_is_ambiguous(db: sqlite3.Connection, intent: Intent) -> None:
    db.execute(
        "INSERT INTO executions (idempotency_key, task_id, intent_hash, status, started_at)"
        " VALUES (?, ?, ?, 'started', 'x')",
        (idempotency_key(intent.task_id, 1, intent.hash), intent.task_id, intent.hash),
    )
    res = await Executor(db, {}).execute(intent, Allow(), step_no=1)
    assert res.ambiguous and not res.ok


async def test_failure_can_retry(db: sqlite3.Connection, intent: Intent) -> None:
    attempts = 0

    async def flaky(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("slow")
        return {}

    ex = Executor(db, {"label": flaky})
    assert not (await ex.execute(intent, Allow(), step_no=1)).ok
    assert (await ex.execute(intent, Allow(), step_no=1)).ok


async def test_requires_allow(db: sqlite3.Connection, intent: Intent) -> None:
    with pytest.raises(NotAllowedError):
        await Executor(db, {}).execute(intent, DryRun(would="Allow"), step_no=1)

"""Task reads: ``list_tasks`` and ``task_counts``. Ids, kinds, states and ages only."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, get_args

from resonant.executor import ToolFailed, ToolHandler
from resonant.loop.types import TaskStatus
from resonant.store import tasks as repo
from resonant.store.db import utcnow
from resonant.tools.base import NO_ARGS, BuiltinContext, no_args
from resonant_sdk import Effect, ToolSpec

LIST_TASKS_MAX = 50
LIST_TASKS_DEFAULT = 10

_STATUSES: tuple[str, ...] = get_args(TaskStatus)

LIST_TASKS = ToolSpec(
    name="list_tasks",
    description=(
        "List recent tasks (newest first) with id, kind, status, wait reason and age. "
        "Optionally filter by status."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(_STATUSES)},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": LIST_TASKS_MAX,
                "default": LIST_TASKS_DEFAULT,
            },
        },
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=5.0,
)
TASK_COUNTS = ToolSpec(
    name="task_counts",
    description="Count tasks by status, and waiting tasks by wait reason.",
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)

SPECS: tuple[ToolSpec, ...] = (LIST_TASKS, TASK_COUNTS)


def _age_s(created_at: str, now: datetime) -> int:
    return max(0, round((now - datetime.fromisoformat(created_at)).total_seconds()))


def list_tasks(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    unknown = set(args) - {"status", "limit"}
    if unknown:
        raise ToolFailed(f"unknown arguments: {sorted(unknown)}")
    status = args.get("status")
    if status is not None and status not in _STATUSES:
        raise ToolFailed(f"status must be one of {list(_STATUSES)}")
    limit = args.get("limit", LIST_TASKS_DEFAULT)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ToolFailed("limit must be an integer")
    limit = max(1, min(limit, LIST_TASKS_MAX))
    # Task ids are ULIDs, so ordering by the primary key is newest-first and indexed.
    if status is None:
        rows = conn.execute(
            "SELECT id, kind, status, wait_reason, created_at FROM tasks ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, kind, status, wait_reason, created_at FROM tasks
               WHERE status = ? ORDER BY id DESC LIMIT ?""",
            (status, limit),
        ).fetchall()
    now = utcnow()
    tasks = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "status": r["status"],
            "wait_reason": r["wait_reason"],
            "age_s": _age_s(r["created_at"], now),
        }
        for r in rows
    ]
    return {"tasks": tasks, "count": len(tasks), "limit": limit, "status": status}


def task_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    by_status = repo.count_by_status(conn)
    return {
        "by_status": by_status,
        "waiting_by_reason": repo.count_waiting_by_reason(conn),
        "total": sum(by_status.values()),
    }


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    conn = ctx.conn

    async def _list_tasks(args: dict[str, Any]) -> dict[str, Any]:
        return list_tasks(conn, args)

    async def _task_counts(args: dict[str, Any]) -> dict[str, Any]:
        no_args(TASK_COUNTS.name, args)
        return task_counts(conn)

    return {LIST_TASKS.name: _list_tasks, TASK_COUNTS.name: _task_counts}

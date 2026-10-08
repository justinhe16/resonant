"""Task and event repositories. Plain functions over a sqlite3 connection."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, cast

from resonant.loop.types import NewTask, Task, TaskStatus, WaitReason
from resonant.store.db import iso, now_iso
from resonant_sdk import Event, new_id


def _dt(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def _json(v: str | None) -> Any:
    return None if v is None else json.loads(v)


def _dump(v: Any) -> str | None:
    return None if v is None else json.dumps(v)


def row_to_task(r: sqlite3.Row) -> Task:
    created = _dt(r["created_at"])
    updated = _dt(r["updated_at"])
    assert created is not None and updated is not None
    return Task(
        id=r["id"],
        kind=r["kind"],
        runner=r["runner"],
        priority=r["priority"],
        status=cast(TaskStatus, r["status"]),
        trace_id=r["trace_id"],
        created_at=created,
        updated_at=updated,
        extension=r["extension"],
        principal=r["principal"],
        lane=r["lane"],
        wait_reason=cast(WaitReason | None, r["wait_reason"]),
        wake_at=_dt(r["wake_at"]),
        attempts=r["attempts"],
        step_no=r["step_no"],
        checkpoint=json.loads(r["checkpoint"]),
        claude_session_id=r["claude_session_id"],
        worktree_path=r["worktree_path"],
        scope=_json(r["scope"]),
        origin_event_id=r["origin_event_id"],
        channel_ref=_json(r["channel_ref"]),
        result=_json(r["result"]),
        error=r["error"],
    )


def insert_task(conn: sqlite3.Connection, spec: NewTask) -> str:
    task_id = new_id()
    ts = now_iso()
    conn.execute(
        """INSERT INTO tasks (id, kind, runner, extension, principal, lane, priority, checkpoint,
               scope, origin_event_id, channel_ref, trace_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id,
            spec.kind,
            spec.runner,
            spec.extension,
            spec.principal,
            spec.lane,
            spec.priority,
            json.dumps(spec.checkpoint),
            _dump(spec.scope),
            spec.origin_event_id,
            _dump(spec.channel_ref),
            spec.trace_id or new_id(),
            ts,
            ts,
        ),
    )
    return task_id


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    r = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return None if r is None else row_to_task(r)


def runnable_tasks(conn: sqlite3.Connection, now: datetime, limit: int) -> list[Task]:
    """Queued tasks whose backoff (wake_at) has passed, by priority then age."""
    rows = conn.execute(
        """SELECT * FROM tasks WHERE status = 'queued' AND (wake_at IS NULL OR wake_at <= ?)
           ORDER BY priority, created_at LIMIT ?""",
        (iso(now), limit),
    ).fetchall()
    return [row_to_task(r) for r in rows]


def wake_due(conn: sqlite3.Connection, now: datetime) -> int:
    """waiting -> queued for tasks whose wake_at has passed."""
    cur = conn.execute(
        """UPDATE tasks SET status = 'queued', wait_reason = NULL, wake_at = NULL, updated_at = ?
           WHERE status = 'waiting' AND wake_at IS NOT NULL AND wake_at <= ?""",
        (now_iso(), iso(now)),
    )
    return cur.rowcount


def set_status(
    conn: sqlite3.Connection,
    task_id: str,
    status: TaskStatus,
    *,
    wait_reason: WaitReason | None = None,
    wake_at: datetime | None = None,
    error: str | None = None,
    expect: tuple[TaskStatus, ...] | None = None,
) -> bool:
    """Change status. If ``expect`` is given, only applies when the current status matches."""
    sql = (
        "UPDATE tasks SET status = ?, wait_reason = ?, wake_at = ?, error = COALESCE(?, error),"
        " updated_at = ? WHERE id = ?"
    )
    params: list[Any] = [
        status,
        wait_reason,
        None if wake_at is None else iso(wake_at),
        error,
        now_iso(),
        task_id,
    ]
    if expect:
        sql += " AND status IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(expect))
    return conn.execute(sql, params).rowcount == 1


def save_step(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: TaskStatus,
    checkpoint: dict[str, Any] | None,
    wait_reason: WaitReason | None = None,
    wake_at: datetime | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    attempts_delta: int = 0,
    reset_attempts: bool = False,
) -> bool:
    """Persist one step's outcome. Call inside a transaction, together with consume_events.

    Returns False if the task is no longer 'running' (e.g. it was cancelled mid-step).
    """
    cur = conn.execute(
        """UPDATE tasks SET status = ?, wait_reason = ?, wake_at = ?,
               checkpoint = COALESCE(?, checkpoint), result = COALESCE(?, result),
               error = ?, step_no = step_no + 1,
               attempts = CASE WHEN ? THEN 0 ELSE attempts + ? END,
               updated_at = ?
           WHERE id = ? AND status = 'running'""",
        (
            status,
            wait_reason,
            None if wake_at is None else iso(wake_at),
            _dump(checkpoint),
            _dump(result),
            error,
            reset_attempts,
            attempts_delta,
            now_iso(),
            task_id,
        ),
    )
    return cur.rowcount == 1


def save_progress(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    checkpoint: dict[str, Any] | None,
    claude_session_id: str | None,
    worktree_path: str | None,
) -> bool:
    """Mid-step progress for a running task. Fields left as None are unchanged."""
    cur = conn.execute(
        """UPDATE tasks SET checkpoint = COALESCE(?, checkpoint),
               claude_session_id = COALESCE(?, claude_session_id),
               worktree_path = COALESCE(?, worktree_path), updated_at = ?
           WHERE id = ? AND status = 'running'""",
        (_dump(checkpoint), claude_session_id, worktree_path, now_iso(), task_id),
    )
    return cur.rowcount == 1


def requeue_interrupted(conn: sqlite3.Connection, max_attempts: int) -> tuple[int, int]:
    """On startup: tasks still 'running' were interrupted by a crash.

    Each crash counts as an attempt, so a step that kills the process can't crash-loop
    forever under launchd KeepAlive. Returns (requeued, failed).
    """
    ts = now_iso()
    failed = conn.execute(
        """UPDATE tasks SET status = 'failed', attempts = attempts + 1, updated_at = ?,
               error = 'interrupted by daemon crash too many times'
           WHERE status = 'running' AND attempts + 1 >= ?""",
        (ts, max_attempts),
    ).rowcount
    requeued = conn.execute(
        """UPDATE tasks SET status = 'queued', attempts = attempts + 1, updated_at = ?
           WHERE status = 'running'""",
        (ts,),
    ).rowcount
    return requeued, failed


def requeue_running(conn: sqlite3.Connection, task_ids: list[str]) -> int:
    """Graceful shutdown: steps cancelled on purpose go back to the queue, no attempt used."""
    cur = conn.execute(
        """UPDATE tasks SET status = 'queued', updated_at = ?
           WHERE status = 'running' AND id IN (SELECT value FROM json_each(?))""",
        (now_iso(), json.dumps(task_ids)),
    )
    return cur.rowcount


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def count_waiting_by_reason(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT wait_reason, COUNT(*) AS n FROM tasks WHERE status = 'waiting' GROUP BY wait_reason"
    ).fetchall()
    return {r["wait_reason"]: r["n"] for r in rows}


# --- events ----------------------------------------------------------------------------


def insert_event(conn: sqlite3.Connection, event: Event) -> bool:
    """Append an event. Returns False if it is a duplicate (same id or dedupe_key)."""
    cur = conn.execute(
        """INSERT INTO events (id, task_id, source, type, principal, payload, dedupe_key,
               occurred_at, received_at, trace_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT DO NOTHING""",
        (
            event.id,
            event.task_id,
            event.source,
            event.type,
            event.principal,
            json.dumps(event.payload),
            event.dedupe_key,
            iso(event.occurred_at),
            now_iso(),
            event.trace_id,
        ),
    )
    return cur.rowcount == 1


def _row_to_event(r: sqlite3.Row) -> Event:
    return Event(
        id=r["id"],
        task_id=r["task_id"],
        source=r["source"],
        type=r["type"],
        principal=r["principal"],
        payload=json.loads(r["payload"]),
        dedupe_key=r["dedupe_key"],
        occurred_at=datetime.fromisoformat(r["occurred_at"]),
        trace_id=r["trace_id"],
    )


def pending_events(conn: sqlite3.Connection, task_id: str) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM events WHERE task_id = ? AND consumed_at IS NULL ORDER BY rowid",
        (task_id,),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def unrouted_events(conn: sqlite3.Connection, limit: int = 100) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM events WHERE task_id IS NULL AND consumed_at IS NULL ORDER BY rowid LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def consume_events(conn: sqlite3.Connection, event_ids: list[str]) -> None:
    if event_ids:
        conn.execute(
            "UPDATE events SET consumed_at = ? WHERE id IN (SELECT value FROM json_each(?))",
            (now_iso(), json.dumps(event_ids)),
        )


def attach_event(conn: sqlite3.Connection, event_id: str, task_id: str) -> None:
    conn.execute("UPDATE events SET task_id = ? WHERE id = ?", (task_id, event_id))


def get_event(conn: sqlite3.Connection, event_id: str) -> Event | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return None if row is None else _row_to_event(row)


def principal_thread(
    conn: sqlite3.Connection,
    principal: str,
    *,
    source: str,
    event_type: str,
    before_event_id: str,
    limit: int,
) -> list[tuple[Event, dict[str, Any] | None]]:
    """``principal``'s last ``limit`` events of one source and type before ``before_event_id``.

    Oldest first. Each event comes with the checkpoint of the task it started (None if it
    started none), so a runner can show its earlier replies as conversation history.
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        """SELECT e.*, t.checkpoint AS origin_checkpoint FROM events e
           LEFT JOIN tasks t ON t.origin_event_id = e.id
           WHERE e.principal = ? AND e.source = ? AND e.type = ?
             AND e.rowid < (SELECT rowid FROM events WHERE id = ?)
           ORDER BY e.rowid DESC LIMIT ?""",
        (principal, source, event_type, before_event_id, limit),
    ).fetchall()
    out: list[tuple[Event, dict[str, Any] | None]] = []
    for r in reversed(rows):
        cp: Any = _json(r["origin_checkpoint"])
        out.append((_row_to_event(r), cast(dict[str, Any], cp) if isinstance(cp, dict) else None))
    return out

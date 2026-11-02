"""Append-only audit log. Every action is recorded; secrets must be redacted by callers."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from resonant.store.db import now_iso

RUN_EVENT_SCAN = 20_000  # rows of the audit log ``run_events`` looks at, newest first
EVENT_NAME = re.compile(r"^[a-z][a-z0-9._:-]{0,63}$")


def audit(
    conn: sqlite3.Connection,
    action: str,
    *,
    task_id: str | None = None,
    requested_by: str | None = None,
    approved_by: str | None = None,
    extension: str | None = None,
    tool: str | None = None,
    effect: str | None = None,
    level: str | None = None,
    intent_hash: str | None = None,
    idempotency_key: str | None = None,
    decision: str | None = None,
    trace_id: str | None = None,
    **detail: Any,
) -> None:
    conn.execute(
        """INSERT INTO audit_log (at, action, task_id, requested_by, approved_by, extension, tool,
               effect, level, intent_hash, idempotency_key, decision, detail, trace_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now_iso(),
            action,
            task_id,
            requested_by,
            approved_by,
            extension,
            tool,
            effect,
            level,
            intent_hash,
            idempotency_key,
            decision,
            json.dumps(detail, default=str),
            trace_id,
        ),
    )


def run_events(
    conn: sqlite3.Connection, runs: Sequence[tuple[str, str | None]], per_run: int = 12
) -> dict[str, list[tuple[datetime, str]]]:
    """``run code -> [(at, action)]``, oldest first, the last ``per_run`` of each run's
    ``run.*`` audit rows (``run.start``, ``run.ready``, ...). The log keeps 365 days and has
    no index on ``detail``, so the scan starts at a floor: the lowest of the oldest
    ``run.start`` of the runs' tasks (``audit_task`` index) and, for a run with no task, the
    first audit row at or after its ``runs.created_at`` (a run's rows are never older than
    it); a run unknown to ``runs`` and without a task has no floor (0). The rows read are
    capped at ``RUN_EVENT_SCAN`` matching rows, newest first, so a busy log can cut the
    oldest events of a run."""
    if not runs:
        return {}
    codes = [code for code, _ in runs]
    task_ids = [t for _, t in runs if t is not None]
    floors: list[int] = []
    if task_ids:
        row = conn.execute(
            "SELECT MIN(id) FROM audit_log WHERE action = 'run.start'"
            " AND task_id IN (SELECT value FROM json_each(?))",
            (json.dumps(task_ids),),
        ).fetchone()
        floors.append(int(row[0] or 0))
    taskless = [code for code, t in runs if t is None]
    if taskless:
        row = conn.execute(
            "SELECT MIN(created_at) FROM runs WHERE code IN (SELECT value FROM json_each(?))",
            (json.dumps(taskless),),
        ).fetchone()
        first = None
        if row is not None and row[0] is not None:
            first = conn.execute(
                "SELECT id FROM audit_log WHERE at >= ? ORDER BY id LIMIT 1", (row[0],)
            ).fetchone()
        floors.append(0 if row is None or row[0] is None else int(first[0]) if first else 1 << 62)
    floor = min(floors)
    rows = conn.execute(
        """SELECT at, action, json_extract(detail, '$.run') FROM audit_log
               WHERE id >= ? AND action LIKE 'run.%'
                 AND json_extract(detail, '$.run') IN (SELECT value FROM json_each(?))
               ORDER BY id DESC LIMIT ?""",
        (floor, json.dumps(codes), RUN_EVENT_SCAN),
    ).fetchall()
    out: dict[str, list[tuple[datetime, str]]] = {}
    for at, action, code in rows:  # newest first
        kept = out.setdefault(str(code), [])
        if len(kept) < per_run:
            kept.append((datetime.fromisoformat(at), action))
    return {code: events[::-1] for code, events in out.items()}


def task_events(
    conn: sqlite3.Connection, task_ids: Sequence[str], per_task: int = 10
) -> dict[str, list[tuple[datetime, str]]]:
    """``task id -> [(at, action)]``, oldest first, the last ``per_task`` audit rows of each
    task (the ``audit_task`` index)."""
    if not task_ids:
        return {}
    rows = conn.execute(
        """SELECT task_id, at, action FROM (
               SELECT task_id, at, action, id, ROW_NUMBER() OVER (
                   PARTITION BY task_id ORDER BY id DESC) AS n
               FROM audit_log WHERE task_id IN (SELECT value FROM json_each(?)))
           WHERE n <= ? ORDER BY task_id, id""",
        (json.dumps(list(task_ids)), per_task),
    ).fetchall()
    out: dict[str, list[tuple[datetime, str]]] = {}
    for task_id, at, action in rows:
        out.setdefault(task_id, []).append((datetime.fromisoformat(at), action))
    return out

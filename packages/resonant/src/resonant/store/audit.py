"""Append-only audit log. Every action is recorded; secrets must be redacted by callers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from resonant.store.db import now_iso


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

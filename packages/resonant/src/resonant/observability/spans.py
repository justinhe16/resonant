"""Spans: one row per traced operation (llm call, tool, gate, executor, loop step).

with span(conn, "gate.evaluate", trace_id=t.trace_id, task_id=t.id) as s:
    s.set(decision="Allow")
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from resonant.store.db import atomic, now_iso
from resonant_sdk import new_id


@dataclass
class Span:
    id: str
    trace_id: str
    attrs: dict[str, Any] = field(default_factory=dict[str, Any])

    def set(self, **attrs: Any) -> None:
        self.attrs.update(attrs)


@contextmanager
def span(
    conn: sqlite3.Connection,
    name: str,
    *,
    trace_id: str,
    task_id: str | None = None,
    parent_id: str | None = None,
    **attrs: Any,
) -> Generator[Span]:
    s = Span(id=new_id(), trace_id=trace_id, attrs=dict(attrs))
    with atomic(conn):
        conn.execute(
            """INSERT INTO spans (id, trace_id, parent_id, task_id, name, attrs, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (s.id, trace_id, parent_id, task_id, name, json.dumps(s.attrs, default=str), now_iso()),
        )
    status = "ok"
    try:
        yield s
    except BaseException as e:
        status = "error"
        s.set(error=f"{type(e).__name__}: {e}")
        raise
    finally:
        # Safe inside a caller's transaction too: atomic() nests via SAVEPOINT.
        with atomic(conn):
            conn.execute(
                "UPDATE spans SET status = ?, attrs = ?, ended_at = ? WHERE id = ?",
                (status, json.dumps(s.attrs, default=str), now_iso(), s.id),
            )

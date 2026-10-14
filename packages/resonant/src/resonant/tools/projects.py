"""``projects``: open work first, then recently finished tasks. Ids, states, kinds, titles, ages.

Open tasks (running, waiting, queued) come before finished ones (done, failed, cancelled),
so a burst of quick replies never hides work still in flight. Each group is ordered by
``updated_at`` descending. At most 15 items, or 50 with ``all``.

Titles are built by code, never from message bodies:

- ``checkpoint.title`` (the router writes the command name or ``chat: <intent>``);
- ``⏰ <text>`` for a reminder (its own text, clipped to 30 characters);
- otherwise the task kind.

Only those two checkpoint fields are read, in SQL; the rest of the checkpoint never leaves
the database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from resonant.executor import ToolFailed, ToolHandler
from resonant.store.db import utcnow
from resonant.tools.base import BuiltinContext
from resonant_sdk import Effect, ToolSpec

PROJECTS_DEFAULT = 15
PROJECTS_ALL = 50
TITLE_MAX = 40
REMINDER_TEXT_MAX = 30

PROJECTS = ToolSpec(
    name="projects",
    description=(
        "Open work (running, waiting, queued) and recently finished tasks, most recently "
        "updated first, with state, wait reason, kind, a short title and age. "
        "15 items, or 50 with all."
    ),
    args_schema={
        "type": "object",
        "properties": {"all": {"type": "boolean", "default": False}},
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=5.0,
)

SPECS: tuple[ToolSpec, ...] = (PROJECTS,)


def clip(text: str, limit: int) -> str:
    """Whitespace collapsed, at most ``limit`` characters (an ellipsis marks a cut)."""
    t = " ".join(text.split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def title_for(kind: str, title: Any, reminder_text: Any) -> str:
    if isinstance(title, str) and title.strip():
        return clip(title, TITLE_MAX)
    if kind == "reminder" and isinstance(reminder_text, str) and reminder_text.strip():
        return "⏰ " + clip(reminder_text, REMINDER_TEXT_MAX)
    return kind


def _age_s(updated_at: str, now: datetime) -> int:
    return max(0, round((now - datetime.fromisoformat(updated_at)).total_seconds()))


def projects(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    unknown = set(args) - {"all"}
    if unknown:
        raise ToolFailed(f"unknown arguments: {sorted(unknown)}")
    show_all = args.get("all", False)
    if not isinstance(show_all, bool):
        raise ToolFailed("all must be a boolean")
    limit = PROJECTS_ALL if show_all else PROJECTS_DEFAULT
    rows = conn.execute(
        """SELECT id, kind, status, wait_reason, updated_at,
                   CASE WHEN json_type(checkpoint, '$.title') = 'text'
                       THEN json_extract(checkpoint, '$.title') END AS title,
                   CASE WHEN kind = 'reminder' AND json_type(checkpoint, '$.text') = 'text'
                       THEN json_extract(checkpoint, '$.text') END AS reminder_text
            FROM tasks  -- open first; the IN list is loop.types.TERMINAL
            ORDER BY status IN ('cancelled', 'done', 'failed'), updated_at DESC, id DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()
    total: int = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    now = utcnow()
    items = [
        {
            "id": r["id"],
            "state": r["status"],
            "wait_reason": r["wait_reason"],
            "age_s": _age_s(r["updated_at"], now),
            "kind": r["kind"],
            "title": title_for(r["kind"], r["title"], r["reminder_text"]),
        }
        for r in rows
    ]
    return {"items": items, "count": len(items), "more": total - len(items), "all": show_all}


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    conn = ctx.conn

    async def _projects(args: dict[str, Any]) -> dict[str, Any]:
        return projects(conn, args)

    return {PROJECTS.name: _projects}

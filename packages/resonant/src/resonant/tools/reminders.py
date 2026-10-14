"""Reminders: ``add_reminder`` (L3), ``list_reminders`` (L0), ``cancel_reminder`` (L3).

A reminder is a durable queued task, not a scheduler entry: ``add_reminder`` inserts
``NewTask(kind="reminder", runner="reminder", wake_at=at)``. Queued tasks don't run before
``wake_at``, so the loop dispatches it when it is due and ``ReminderRunner`` sends it. It
survives restarts with the ``tasks`` row, and a missed one fires once on start, marked late.

Each reminder gets a short number from kv ``reminders.seq`` (``/unremind 3``), kept in the
task checkpoint as ``n`` with ``text`` and ``at`` (ISO, UTC). Handlers don't see the
requester; the gate lets only the owner call global builtins, so reminders belong to the
owner. ``at`` comes in as an ISO-8601 datetime with an offset: from the model, or from the
``/remind`` parser (``tools/when.py``).

``/remind`` also passes ``routed_at``, the time the router resolved ``<when>`` against, and
``at`` is checked against that instead of the handler's clock: the command task may run
late (a busy loop, a restart before it ran), and ``/remind now x`` or ``in 1 min`` must not
fail as "passed" because of it. A reminder that is due by then fires at once, marked late.
``routed_at`` is not in the args schema, so the model can't send it (its calls are
validated against the schema); it must not be in the future or more than a day old.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from resonant.executor import ToolFailed, ToolHandler
from resonant.loop.types import PRIORITY_OWNER, NewTask
from resonant.store import tasks as repo
from resonant.store.db import atomic, iso, kv_get, kv_set
from resonant.tools.base import NO_ARGS, BuiltinContext, no_args
from resonant.tools.when import WhenError, check_at, format_due
from resonant_sdk import Effect, Level, ToolSpec

KIND = "reminder"
RUNNER = "reminder"
TEXT_MAX = 200
LIST_MAX = 50
SEQ_KEY = "reminders.seq"
ROUTED_MAX_AGE = timedelta(days=1)
ROUTED_SKEW = timedelta(seconds=60)

ADD_REMINDER = ToolSpec(
    name="add_reminder",
    description=(
        "Remind the owner at a time. `at` is an ISO-8601 datetime with a UTC offset, in the "
        "future and at most a year out; `text` is what to say."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "at": {"type": "string", "format": "date-time"},
            "text": {"type": "string", "minLength": 1, "maxLength": TEXT_MAX},
        },
        "required": ["at", "text"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=5.0,
)
LIST_REMINDERS = ToolSpec(
    name="list_reminders",
    description="List pending reminders, soonest first, with id, due time and text.",
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)
CANCEL_REMINDER = ToolSpec(
    name="cancel_reminder",
    description="Cancel a pending reminder by its id (the number list_reminders shows).",
    args_schema={
        "type": "object",
        "properties": {"id": {"type": "integer", "minimum": 1}},
        "required": ["id"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=5.0,
)

SPECS: tuple[ToolSpec, ...] = (ADD_REMINDER, LIST_REMINDERS, CANCEL_REMINDER)


def _check_keys(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(args) - allowed
    if unknown:
        raise ToolFailed(f"unknown arguments: {sorted(unknown)}")


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolFailed("text must be a non-empty string")
    text = " ".join(value.split())
    if len(text) > TEXT_MAX:
        raise ToolFailed(f"text must be at most {TEXT_MAX} chars")
    return text


def _iso(name: str, value: Any) -> datetime:
    if not isinstance(value, str):
        raise ToolFailed(f"{name} must be an ISO-8601 datetime string")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ToolFailed(f"{name} must be an ISO-8601 datetime with a UTC offset") from None
    if dt.utcoffset() is None:
        raise ToolFailed(f"{name} needs a UTC offset")
    return dt


def _reference(value: Any, now: datetime) -> datetime:
    """What ``at`` is checked against: ``now``, or a ``/remind`` command's ``routed_at``."""
    if value is None:
        return now
    routed = _iso("routed_at", value)
    if routed > now + ROUTED_SKEW or now - routed > ROUTED_MAX_AGE:
        raise ToolFailed("routed_at is out of range")
    return min(routed, now)


def _at(value: Any, now: datetime) -> datetime:
    at = _iso("at", value)
    try:
        check_at(at, now=now)
    except WhenError as e:
        raise ToolFailed(str(e)) from None
    return at


def _id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolFailed("id must be a positive integer")
    return value


def _view(row: sqlite3.Row, now: datetime, tz: str) -> dict[str, Any]:
    at = datetime.fromisoformat(row["at"])
    return {
        "id": row["n"],
        "at": row["at"],
        "due": format_due(at, now=now, tz=tz),
        "text": row["text"],
    }


def add_reminder(
    conn: sqlite3.Connection, args: dict[str, Any], *, owner: str, now: datetime, tz: str
) -> dict[str, Any]:
    _check_keys(args, {"at", "text", "routed_at"})
    if "at" not in args or "text" not in args:
        raise ToolFailed("add_reminder needs at and text")
    at = _at(args["at"], _reference(args.get("routed_at"), now))
    text = _text(args["text"])
    with atomic(conn):
        seq = kv_get(conn, SEQ_KEY, 0)
        n = (seq if isinstance(seq, int) and not isinstance(seq, bool) else 0) + 1
        kv_set(conn, SEQ_KEY, n)
        task_id = repo.insert_task(
            conn,
            NewTask(
                kind=KIND,
                runner=RUNNER,
                priority=PRIORITY_OWNER,
                principal=owner,
                checkpoint={"n": n, "text": text, "at": iso(at)},
                wake_at=at,
            ),
        )
    return {
        "id": n,
        "task_id": task_id,
        "at": iso(at),
        "due": format_due(at, now=now, tz=tz),
        "text": text,
    }


_LIST_SQL = """SELECT id, status, json_extract(checkpoint, '$.n') AS n,
           json_extract(checkpoint, '$.text') AS text, json_extract(checkpoint, '$.at') AS at
    FROM tasks WHERE kind = ? AND runner = ? AND principal = ? AND status IN ('queued', 'running')
    ORDER BY at, n LIMIT ?"""
_BY_N_SQL = """SELECT id, status, json_extract(checkpoint, '$.n') AS n,
           json_extract(checkpoint, '$.text') AS text, json_extract(checkpoint, '$.at') AS at
    FROM tasks WHERE kind = ? AND runner = ? AND principal = ?
        AND json_extract(checkpoint, '$.n') = ?"""


def list_reminders(
    conn: sqlite3.Connection, *, owner: str, now: datetime, tz: str
) -> dict[str, Any]:
    rows = conn.execute(_LIST_SQL, (KIND, RUNNER, owner, LIST_MAX)).fetchall()
    reminders = [_view(r, now, tz) for r in rows]
    return {"reminders": reminders, "count": len(reminders)}


def cancel_reminder(
    conn: sqlite3.Connection, args: dict[str, Any], *, owner: str, now: datetime, tz: str
) -> dict[str, Any]:
    _check_keys(args, {"id"})
    n = _id(args.get("id"))
    with atomic(conn):
        row = conn.execute(_BY_N_SQL, (KIND, RUNNER, owner, n)).fetchone()
        if row is None:
            raise ToolFailed(f"no reminder {n}")
        if row["status"] != "queued":
            state = {
                "running": "is firing now",
                "done": "already went off",
                "failed": "failed to send",
            }
            raise ToolFailed(f"reminder {n} {state.get(row['status'], 'is already cancelled')}")
        if not repo.set_status(conn, row["id"], "cancelled", expect=("queued",)):
            raise ToolFailed(f"reminder {n} is no longer pending")
    return {"cancelled": True, **_view(row, now, tz)}


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    conn = ctx.conn

    def tz() -> str:
        return ctx.state.settings.timezone

    async def _add(args: dict[str, Any]) -> dict[str, Any]:
        return add_reminder(conn, args, owner=ctx.principals.owner, now=ctx.clock(), tz=tz())

    async def _list(args: dict[str, Any]) -> dict[str, Any]:
        no_args(LIST_REMINDERS.name, args)
        return list_reminders(conn, owner=ctx.principals.owner, now=ctx.clock(), tz=tz())

    async def _cancel(args: dict[str, Any]) -> dict[str, Any]:
        return cancel_reminder(conn, args, owner=ctx.principals.owner, now=ctx.clock(), tz=tz())

    return {
        ADD_REMINDER.name: _add,
        LIST_REMINDERS.name: _list,
        CANCEL_REMINDER.name: _cancel,
    }

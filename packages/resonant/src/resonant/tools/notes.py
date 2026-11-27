"""Notes: ``list_notes`` (L0), ``add_note`` (L3), ``delete_note`` (L3), ``clear_notes`` (L1),
``undo_note`` (L3).

Notes live in ``memory`` with ``scope="notes"``: the key is the note id as a decimal string
and the value is JSON ``{text, at, deleted_at}``. Ids come from kv ``notes.seq`` and are
never reused. Deletes are soft: they set ``deleted_at`` and keep the row (for a later
undo), and every read hides soft-deleted notes. No migration is needed.

Replay can't duplicate a note: the executor records each call under its idempotency key
and returns the recorded result instead of running the handler again.

``undo_note`` (``/undo``, PER-264) restores the last ``delete_note`` or ``clear_notes``: each
records what it deleted in kv ``notes.undo`` (the ids and their ``deleted_at``), and the undo
clears ``deleted_at`` on exactly those rows (one deleted again since keeps its later delete).
An undo leaves ``{"undone": true}`` there, so a second ``/undo`` refuses: an undo is never
undone. With nothing recorded it refuses too.

A note is at most ``TEXT_MAX`` (1000) characters after stripping.

``add_note`` is ``taint_exempt`` (PER-245): it runs in a tainted task too. Deleting or
clearing notes is not: those still ask.

``clear_notes`` is destructive, so its approval prompt says how many notes it would
delete: ``summary_action`` builds that phrase in code, for
``gate.summary.summarize(intent, action=...)``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from resonant.executor import ToolFailed, ToolHandler
from resonant.gate.types import Intent
from resonant.store.db import atomic, kv_get, kv_set, now_iso
from resonant.tools.base import NO_ARGS, BuiltinContext, no_args
from resonant_sdk import Effect, Level, ToolSpec

SCOPE = "notes"
SEQ_KEY = "notes.seq"
UNDO_KEY = "notes.undo"  # {"ids": [...], "at": deleted_at, "what": "unnote"|"clear"}
TEXT_MAX = 1000  # GT-1: was 200
LIST_MAX = 20

LIST_NOTES = ToolSpec(
    name="list_notes",
    description=f"The owner's saved notes, newest first (at most {LIST_MAX}).",
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)
ADD_NOTE = ToolSpec(
    name="add_note",
    description=f"Save a short note (at most {TEXT_MAX} characters).",
    args_schema={
        "type": "object",
        "properties": {"text": {"type": "string", "minLength": 1, "maxLength": TEXT_MAX}},
        "required": ["text"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=5.0,
    taint_exempt=True,  # PER-245: runs on its level even when tainted
    approval_summary='add a note: "{text}"',
)
DELETE_NOTE = ToolSpec(
    name="delete_note",
    description="Delete one note by its id.",
    args_schema={
        "type": "object",
        "properties": {"id": {"type": "integer", "minimum": 1}},
        "required": ["id"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=5.0,
    approval_summary="delete note {id}",
)
CLEAR_NOTES = ToolSpec(
    name="clear_notes",
    description="Delete all notes. Always asks for a confirmation code.",
    args_schema=NO_ARGS,
    effect=Effect.WRITE,
    level=Level.L1,
    timeout_s=5.0,
)

UNDO_NOTE = ToolSpec(
    name="undo_note",
    description="Undo the last note delete or clear (restores those notes). Not an undo.",
    args_schema=NO_ARGS,
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=5.0,
    approval_summary="undo the last note delete",
)

SPECS: tuple[ToolSpec, ...] = (LIST_NOTES, ADD_NOTE, DELETE_NOTE, CLEAR_NOTES, UNDO_NOTE)


def _check_keys(name: str, args: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(args) - allowed
    if unknown:
        raise ToolFailed(f"{name}: unknown arguments: {sorted(unknown)}")


def _note(key: str, value: str) -> dict[str, Any]:
    data = json.loads(value)
    return {"id": int(key), "text": data["text"], "at": data["at"]}


def live_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM memory "
        "WHERE scope = ? AND json_extract(value, '$.deleted_at') IS NULL",
        (SCOPE,),
    ).fetchone()[0]


def list_notes(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT key, value FROM memory "
        "WHERE scope = ? AND json_extract(value, '$.deleted_at') IS NULL "
        "ORDER BY CAST(key AS INTEGER) DESC LIMIT ?",
        (SCOPE, LIST_MAX),
    ).fetchall()
    notes = [_note(r["key"], r["value"]) for r in rows]
    return {"notes": notes, "count": len(notes), "total": live_count(conn)}


def add_note(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(ADD_NOTE.name, args, {"text"})
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolFailed("add_note: text must be a non-empty string")
    text = text.strip()
    if len(text) > TEXT_MAX:
        raise ToolFailed(f"add_note: text is {len(text)} characters (at most {TEXT_MAX})")
    at = now_iso()
    with atomic(conn):
        seq = kv_get(conn, SEQ_KEY, 0)
        # Never reuse an id, even if the counter was lost or reset.
        top = conn.execute(
            "SELECT MAX(CAST(key AS INTEGER)) FROM memory WHERE scope = ?", (SCOPE,)
        ).fetchone()[0]
        note_id = max(seq if isinstance(seq, int) else 0, top or 0) + 1
        kv_set(conn, SEQ_KEY, note_id)
        conn.execute(
            "INSERT INTO memory (scope, key, value, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (SCOPE, str(note_id), json.dumps({"text": text, "at": at, "deleted_at": None}), at, at),
        )
    return {"id": note_id, "text": text, "at": at}


def delete_note(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(DELETE_NOTE.name, args, {"id"})
    note_id = args.get("id")
    if not isinstance(note_id, int) or isinstance(note_id, bool) or note_id < 1:
        raise ToolFailed("delete_note: id must be a positive integer")
    at = now_iso()
    with atomic(conn):
        row = conn.execute(
            "SELECT key, value FROM memory "
            "WHERE scope = ? AND json_extract(value, '$.deleted_at') IS NULL AND key = ?",
            (SCOPE, str(note_id)),
        ).fetchone()
        if row is None:
            raise ToolFailed(f"delete_note: no note {note_id}")
        conn.execute(
            "UPDATE memory SET value = json_set(value, '$.deleted_at', ?), updated_at = ? "
            "WHERE scope = ? AND key = ?",
            (at, at, SCOPE, str(note_id)),
        )
        kv_set(conn, UNDO_KEY, {"ids": [note_id], "at": at, "what": "unnote"})
    return {**_note(row["key"], row["value"]), "deleted_at": at}


def clear_notes(conn: sqlite3.Connection) -> dict[str, Any]:
    at = now_iso()
    with atomic(conn):
        ids = [
            int(r["key"])
            for r in conn.execute(
                "SELECT key FROM memory "
                "WHERE scope = ? AND json_extract(value, '$.deleted_at') IS NULL",
                (SCOPE,),
            ).fetchall()
        ]
        cur = conn.execute(
            "UPDATE memory SET value = json_set(value, '$.deleted_at', ?), updated_at = ? "
            "WHERE scope = ? AND json_extract(value, '$.deleted_at') IS NULL",
            (at, at, SCOPE),
        )
        if ids:  # clearing nothing leaves the last real delete undoable
            kv_set(conn, UNDO_KEY, {"ids": ids, "at": at, "what": "clear"})
    return {"cleared": cur.rowcount, "deleted_at": at}


def undo_note(conn: sqlite3.Connection) -> dict[str, Any]:
    """Restore what the last ``delete_note``/``clear_notes`` deleted (module docstring)."""
    with atomic(conn):
        raw = kv_get(conn, UNDO_KEY)
        last = cast(dict[str, Any], raw) if isinstance(raw, dict) else None
        if last is not None and last.get("undone") is True:
            raise ToolFailed("undo_note: the last change was an undo; an undo can't be undone")
        if last is None or not isinstance(last.get("at"), str):
            raise ToolFailed("undo_note: no note delete to undo")
        at = cast(str, last["at"])
        ids = [i for i in cast(list[Any], last.get("ids") or []) if isinstance(i, int)]
        now = now_iso()
        restored: list[int] = []
        for note_id in ids:
            cur = conn.execute(
                "UPDATE memory SET value = json_set(value, '$.deleted_at', json('null')), "
                "updated_at = ? WHERE scope = ? AND key = ? "
                "AND json_extract(value, '$.deleted_at') = ?",
                (now, SCOPE, str(note_id), at),
            )
            if cur.rowcount == 1:
                restored.append(note_id)
        kv_set(conn, UNDO_KEY, {"undone": True})
    return {"restored": len(restored), "ids": restored, "what": last.get("what")}


def summary_action(conn: sqlite3.Connection, intent: Intent) -> str | None:
    """The code-built approval heading for ``intent``, or None to use the default.

    Only the builtin ``clear_notes`` (matched by registry identity, so an extension tool of
    the same name doesn't qualify) has one: ``delete all 4 notes``.
    """
    if intent.extension is not None or intent.tool != CLEAR_NOTES.name:
        return None
    n = live_count(conn)
    return f"delete all {n} note{'' if n == 1 else 's'}"


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    conn = ctx.conn

    async def _list_notes(args: dict[str, Any]) -> dict[str, Any]:
        no_args(LIST_NOTES.name, args)
        return list_notes(conn)

    async def _add_note(args: dict[str, Any]) -> dict[str, Any]:
        return add_note(conn, args)

    async def _delete_note(args: dict[str, Any]) -> dict[str, Any]:
        return delete_note(conn, args)

    async def _clear_notes(args: dict[str, Any]) -> dict[str, Any]:
        no_args(CLEAR_NOTES.name, args)
        return clear_notes(conn)

    async def _undo_note(args: dict[str, Any]) -> dict[str, Any]:
        no_args(UNDO_NOTE.name, args)
        return undo_note(conn)

    return {
        LIST_NOTES.name: _list_notes,
        ADD_NOTE.name: _add_note,
        DELETE_NOTE.name: _delete_note,
        CLEAR_NOTES.name: _clear_notes,
        UNDO_NOTE.name: _undo_note,
    }

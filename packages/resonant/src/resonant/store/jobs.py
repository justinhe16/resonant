"""Registered jobs (migration 030, PER-298): cron plus a prompt, one worker run per firing.

A job is one ``registered_jobs`` row. ``create`` gives it the next ``J<n>``; ``get`` takes
``J3`` or ``3``; ``live`` lists the active and paused ones (never ``deleted``). ``claim_due``
moves ``last_due`` with compare-and-set, so a due time is claimed (and starts a run) at most
once. ``started``/``refused`` record what a firing did. ``save_note`` is the write side of
``resonant job-note``: ``note`` replaces the job's memory (at most ``MEMORY_MAX`` bytes),
``seen`` keys are appended (deduped, the newest ``SEEN_MAX`` kept).

The run-to-job link (which job a run belongs to, and where its end text goes) is kept in kv
(``run_link``/``link_run``), written by ``ClaudeRunner.start_run`` before the run's task
exists, so the session's first prompt and its ``job-note`` calls always find it.

Every function commits on its own (``atomic``), so callers may also wrap them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from resonant.store.db import atomic, iso, kv_get, kv_set

Kind = Literal["schedule", "watch"]
State = Literal["active", "paused", "deleted"]
MEMORY_MAX = 4096  # bytes of the job's note (UTF-8)
SEEN_MAX = 200  # seen keys kept (the newest)
SEEN_KEY_MAX = 200  # characters per seen key
NAME_MAX = 60
PROMPT_MAX = 2000
ERROR_MAX = 200
_ID = re.compile(r"^\s*[Jj]?(\d{1,9})\s*$")
_LINK = "run.job:"  # kv key prefix: run code -> {"job": "J3", "room": key | None}


@dataclass(frozen=True)
class Job:
    id: str
    kind: Kind
    name: str
    schedule: str
    tz: str
    prompt: str
    repo: str | None
    output: str  # thread | dm | room:<key>
    created_by: str
    channel_ref: dict[str, Any] | None
    state: State
    last_due: datetime | None
    last_run_code: str | None
    last_run_at: datetime | None
    last_error: str | None
    memory: str | None
    seen: tuple[str, ...]
    created_at: datetime
    updated_at: datetime

    @property
    def room(self) -> str | None:
        """The room key of a ``room:<key>`` output, else None."""
        if not self.output.startswith("room:"):
            return None
        return self.output.partition(":")[2] or None


def _dt(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def _row(r: sqlite3.Row) -> Job:
    ref: object = json.loads(r["channel_ref"]) if r["channel_ref"] else None
    memory: object = json.loads(r["memory_json"]) if r["memory_json"] else None
    note = cast(dict[str, Any], memory).get("note") if isinstance(memory, dict) else None
    seen: object = json.loads(r["seen_json"]) if r["seen_json"] else []
    keys = tuple(str(k) for k in cast(list[Any], seen)) if isinstance(seen, list) else ()
    created = _dt(r["created_at"])
    updated = _dt(r["updated_at"])
    assert created is not None and updated is not None
    return Job(
        id=r["id"],
        kind=r["kind"],
        name=r["name"],
        schedule=r["schedule"],
        tz=r["tz"],
        prompt=r["prompt"],
        repo=r["repo"],
        output=r["output"],
        created_by=r["created_by"],
        channel_ref=cast(dict[str, Any], ref) if isinstance(ref, dict) else None,
        state=r["state"],
        last_due=_dt(r["last_due"]),
        last_run_code=r["last_run_code"],
        last_run_at=_dt(r["last_run_at"]),
        last_error=r["last_error"],
        memory=note if isinstance(note, str) else None,
        seen=keys,
        created_at=created,
        updated_at=updated,
    )


def parse_id(text: object) -> str | None:
    """``J3`` for ``"J3"``, ``"j3"`` or ``"3"``; None if it isn't a job id."""
    if not isinstance(text, str):
        return None
    m = _ID.match(text)
    return f"J{int(m.group(1))}" if m else None


def create(
    conn: sqlite3.Connection,
    *,
    name: str,
    schedule: str,
    tz: str,
    prompt: str,
    repo: str | None,
    output: str,
    created_by: str,
    channel_ref: dict[str, Any] | None,
    now: datetime,
    kind: Kind = "schedule",
) -> Job:
    """A new active job. ``last_due`` starts at ``now``: only due times after it fire."""
    with atomic(conn):
        cur = conn.execute(
            "INSERT INTO registered_jobs (id, kind, name, schedule, tz, prompt, repo, output,"
            " created_by, channel_ref, state, last_due, created_at, updated_at)"
            " VALUES ('', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                kind,
                name,
                schedule,
                tz,
                prompt,
                repo,
                output,
                created_by,
                None if channel_ref is None else json.dumps(channel_ref),
                iso(now),
                iso(now),
                iso(now),
            ),
        )
        n = cur.lastrowid
        conn.execute("UPDATE registered_jobs SET id = ? WHERE n = ?", (f"J{n}", n))
    job = get(conn, f"J{n}")
    assert job is not None
    return job


def get(conn: sqlite3.Connection, job_id: str) -> Job | None:
    """The job ``J3`` (or ``3``), deleted ones included; None if there is none."""
    jid = parse_id(job_id)
    if jid is None:
        return None
    row = conn.execute("SELECT * FROM registered_jobs WHERE id = ?", (jid,)).fetchone()
    return None if row is None else _row(row)


def live(conn: sqlite3.Connection, kind: Kind | None = None) -> list[Job]:
    """Active and paused jobs, oldest first."""
    sql = "SELECT * FROM registered_jobs WHERE state != 'deleted'"
    params: tuple[Any, ...] = ()
    if kind is not None:
        sql += " AND kind = ?"
        params = (kind,)
    return [_row(r) for r in conn.execute(sql + " ORDER BY n", params).fetchall()]


def active(conn: sqlite3.Connection, kind: Kind = "schedule") -> list[Job]:
    rows = conn.execute(
        "SELECT * FROM registered_jobs WHERE state = 'active' AND kind = ? ORDER BY n", (kind,)
    ).fetchall()
    return [_row(r) for r in rows]


def count_live(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM registered_jobs WHERE state != 'deleted'").fetchone()
    return int(row[0])


def set_state(
    conn: sqlite3.Connection,
    job_id: str,
    state: State,
    now: datetime,
    *,
    expect: tuple[State, ...],
    error: str | None = None,
) -> bool:
    """Move a job to ``state`` if it is in one of ``expect``. Resuming moves ``last_due`` to
    ``now``, so nothing due while paused is caught up."""
    placeholders = ",".join("?" for _ in expect)
    sets = "state = ?, updated_at = ?"
    params: list[Any] = [state, iso(now)]
    if state == "active":
        sets += ", last_due = ?"
        params.append(iso(now))
    if error is not None:
        sets += ", last_error = ?"
        params.append(error[:ERROR_MAX])
    with atomic(conn):
        cur = conn.execute(
            f"UPDATE registered_jobs SET {sets} WHERE id = ? AND state IN ({placeholders})",  # noqa: S608 - placeholders only
            (*params, job_id, *expect),
        )
    return cur.rowcount == 1


def claim_due(conn: sqlite3.Connection, job: Job, due: datetime, now: datetime) -> bool:
    """Move ``last_due`` from what ``job`` read to ``due`` (compare-and-set). False: another
    tick moved it first, or the job is no longer active."""
    with atomic(conn):
        cur = conn.execute(
            "UPDATE registered_jobs SET last_due = ?, updated_at = ?"
            " WHERE id = ? AND state = 'active' AND last_due IS ?",
            (iso(due), iso(now), job.id, None if job.last_due is None else iso(job.last_due)),
        )
    return cur.rowcount == 1


def started(conn: sqlite3.Connection, job_id: str, run_code: str, now: datetime) -> None:
    with atomic(conn):
        conn.execute(
            "UPDATE registered_jobs SET last_run_code = ?, last_run_at = ?, last_error = NULL,"
            " updated_at = ? WHERE id = ?",
            (run_code, iso(now), iso(now), job_id),
        )


def refused(conn: sqlite3.Connection, job_id: str, why: str, now: datetime) -> None:
    with atomic(conn):
        conn.execute(
            "UPDATE registered_jobs SET last_error = ?, updated_at = ? WHERE id = ?",
            (" ".join(why.split())[:ERROR_MAX], iso(now), job_id),
        )


class NoteTooBig(ValueError):
    """A ``job-note`` note over ``MEMORY_MAX`` bytes."""


def save_note(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    note: str | None,
    seen: tuple[str, ...],
    now: datetime,
) -> Job | None:
    """``resonant job-note``: ``note`` (when given) replaces the memory; ``seen`` keys are
    appended (deduped, newest ``SEEN_MAX`` kept, each clipped to ``SEEN_KEY_MAX``). None when
    the job is gone or deleted. Raises :class:`NoteTooBig`."""
    if note is not None and len(note.encode()) > MEMORY_MAX:
        raise NoteTooBig(f"the note is {len(note.encode())} bytes, at most {MEMORY_MAX}")
    with atomic(conn):
        job = get(conn, job_id)
        if job is None or job.state == "deleted":
            return None
        keys = list(job.seen)
        for raw in seen:
            key = " ".join(raw.split())[:SEEN_KEY_MAX]
            if not key:
                continue
            if key in keys:
                keys.remove(key)
            keys.append(key)
        keys = keys[-SEEN_MAX:]
        memory = job.memory if note is None else note
        conn.execute(
            "UPDATE registered_jobs SET memory_json = ?, seen_json = ?, updated_at = ?"
            " WHERE id = ?",
            (
                None if memory is None else json.dumps({"note": memory, "at": iso(now)}),
                json.dumps(keys) if keys else None,
                iso(now),
                job.id,
            ),
        )
    return get(conn, job_id)


# --- run <-> job (kv) ---------------------------------------------------------------------


@dataclass(frozen=True)
class RunLink:
    job: str
    room: str | None = None


def link_run(conn: sqlite3.Connection, run_code: str, link: RunLink) -> None:
    with atomic(conn):
        kv_set(conn, _LINK + run_code, {"job": link.job, "room": link.room})


def run_link(conn: sqlite3.Connection, run_code: str) -> RunLink | None:
    """Which job ``run_code`` was started for (and its room), or None for any other run."""
    value: object = kv_get(conn, _LINK + run_code)
    if not isinstance(value, dict):
        return None
    d = cast(dict[str, Any], value)
    job, room = d.get("job"), d.get("room")
    if not isinstance(job, str):
        return None
    return RunLink(job, room if isinstance(room, str) and room else None)


__all__ = [
    "MEMORY_MAX",
    "NAME_MAX",
    "PROMPT_MAX",
    "SEEN_MAX",
    "Job",
    "Kind",
    "NoteTooBig",
    "RunLink",
    "State",
    "active",
    "claim_due",
    "count_live",
    "create",
    "get",
    "link_run",
    "live",
    "parse_id",
    "refused",
    "run_link",
    "save_note",
    "set_state",
    "started",
]

"""Extensions, job-state and panel repositories (migrations 004, 012, 026, 027). Plain
functions.

- ``extensions``: one row per installed extension, the registry of record. It holds the
  approved manifest snapshot and the pins (``commit``, ``tree_digest``, ``manifest_sha``,
  ``argv0_paths``) that the host re-verifies at load. The manager writes it; the host only
  reads it, flips ``status`` and re-pins an MCP tool (``set_mcp_pin``: a v1 pin at version 2, or an
  accepted schema drift, PER-275).
- ``ext_panels``: the latest good copy and run state per ``(ext, panel_id)`` (PER-280).
- ``ext_panel_days``: a panel's archived copy per calendar day (PER-286, ``history``).
- ``ext_jobs``: scheduler state per ``(ext, job_id)``. Core jobs use ``ext="core"``.
  ``last_due`` only moves forward, so a stale writer can never make a due time run twice.

Callers own transactions and audit rows; every function here is a single statement, or
groups its statements with :func:`atomic`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast

from resonant.store.db import atomic, iso

ExtensionStatus = Literal["enabled", "disabled", "disabled_tampered"]

CORE_EXT = "core"


@dataclass(frozen=True)
class ExtensionRecord:
    name: str
    source: str  # "repo:<name>" | "path:<abs dir>"
    commit: str | None
    tree_digest: str
    manifest_sha: str
    manifest: dict[str, Any]  # the approved manifest snapshot
    installed_at: datetime
    installed_by: str
    argv0_paths: Mapping[str, str] = field(default_factory=dict[str, str])
    status: ExtensionStatus = "enabled"
    # What ext add approved per MCP tool: {tool: {server, pin, input_schema[, v]}} (migration
    # 012; ``"v": 2`` since GT-1, absent on a version-1 pin).
    mcp_pins: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class JobState:
    ext: str
    job_id: str
    last_due: datetime | None
    last_run_at: datetime | None
    last_outcome: str | None
    open_task_id: str | None
    fail_streak: int


@dataclass(frozen=True)
class PanelState:
    """One panel's latest good copy and run state (``ext_panels``, migration 026)."""

    ext: str
    panel_id: str
    data_json: str | None  # the normalized {views, health, local_url, truncated}
    updated_at: datetime | None
    bytes: int
    last_run_at: datetime | None
    last_ok: bool | None  # None: never run
    last_error: str | None
    fail_streak: int
    expect_by: datetime | None


DayStatus = Literal["pending", "ok", "none", "failed"]


@dataclass(frozen=True)
class PanelDay:
    """One archived day of a panel (``ext_panel_days``, migration 027)."""

    ext: str
    panel_id: str
    day: str  # YYYY-MM-DD in the panel's tz
    status: DayStatus
    data_json: str | None  # a good copy (kept while a rebuild is pending or failed)
    bytes: int
    created_at: datetime
    attempts: int
    last_error: str | None
    next_try_at: datetime | None
    upload_due: bool
    upload_tries: int
    upload_next_at: datetime | None


def _dt(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def _row_to_extension(r: sqlite3.Row) -> ExtensionRecord:
    return ExtensionRecord(
        name=r["name"],
        source=r["source"],
        commit=r["commit_sha"],
        tree_digest=r["tree_digest"],
        manifest_sha=r["manifest_sha"],
        manifest=json.loads(r["manifest_json"]),
        installed_at=datetime.fromisoformat(r["installed_at"]),
        installed_by=r["installed_by"],
        argv0_paths=json.loads(r["argv0_paths"]),
        status=cast(ExtensionStatus, r["status"]),
        mcp_pins=json.loads(r["mcp_pins"]),
    )


def _row_to_job(r: sqlite3.Row) -> JobState:
    return JobState(
        ext=r["ext"],
        job_id=r["job_id"],
        last_due=_dt(r["last_due"]),
        last_run_at=_dt(r["last_run_at"]),
        last_outcome=r["last_outcome"],
        open_task_id=r["open_task_id"],
        fail_streak=r["fail_streak"],
    )


# --- extensions -----------------------------------------------------------------------


def save_extension(conn: sqlite3.Connection, ext: ExtensionRecord) -> None:
    """Insert or fully replace the row for ``ext.name`` (install and every update)."""
    if ext.name == CORE_EXT:
        raise ValueError(f"{CORE_EXT!r} is reserved for core jobs")
    conn.execute(
        """INSERT INTO extensions (name, source, commit_sha, tree_digest, manifest_sha,
               manifest_json, argv0_paths, status, installed_at, installed_by, mcp_pins)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (name) DO UPDATE SET
               source = excluded.source, commit_sha = excluded.commit_sha,
               tree_digest = excluded.tree_digest, manifest_sha = excluded.manifest_sha,
               manifest_json = excluded.manifest_json, argv0_paths = excluded.argv0_paths,
               status = excluded.status, installed_at = excluded.installed_at,
               installed_by = excluded.installed_by, mcp_pins = excluded.mcp_pins""",
        (
            ext.name,
            ext.source,
            ext.commit,
            ext.tree_digest,
            ext.manifest_sha,
            json.dumps(ext.manifest),  # declared order kept: positional command args
            json.dumps(dict(ext.argv0_paths), sort_keys=True),
            ext.status,
            iso(ext.installed_at),
            ext.installed_by,
            json.dumps(dict(ext.mcp_pins), sort_keys=True),
        ),
    )


def get_extension(conn: sqlite3.Connection, name: str) -> ExtensionRecord | None:
    row = conn.execute("SELECT * FROM extensions WHERE name = ?", (name,)).fetchone()
    return None if row is None else _row_to_extension(row)


def list_extensions(
    conn: sqlite3.Connection, *, status: ExtensionStatus | None = None
) -> list[ExtensionRecord]:
    """Every row (or those with ``status``), sorted by name."""
    if status is None:
        rows = conn.execute("SELECT * FROM extensions ORDER BY name").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM extensions WHERE status = ? ORDER BY name", (status,)
        ).fetchall()
    return [_row_to_extension(r) for r in rows]


def set_status(conn: sqlite3.Connection, name: str, status: ExtensionStatus) -> bool:
    """Set ``status``. False when there is no such row or it already had that status."""
    cur = conn.execute(
        "UPDATE extensions SET status = ? WHERE name = ? AND status != ?", (status, name, status)
    )
    return cur.rowcount == 1


def set_mcp_pin(
    conn: sqlite3.Connection,
    name: str,
    tool: str,
    *,
    installed_at: datetime,
    old_pin: str,
    pin: str,
    version: int,
    input_schema: dict[str, Any] | None = None,
) -> bool:
    """Re-pin one MCP tool of the install made at ``installed_at``, only while its stored pin
    is still ``old_pin``. Only ``pin``, ``v`` and (when given) ``input_schema`` of that one
    entry change. False (nothing written) after a reinstall or update, or once done."""
    with atomic(conn):
        row = conn.execute(
            "SELECT mcp_pins FROM extensions WHERE name = ? AND installed_at = ?",
            (name, iso(installed_at)),
        ).fetchone()
        if row is None:
            return False
        pins = cast(dict[str, Any], json.loads(row["mcp_pins"]))
        entry: object = pins.get(tool)
        if not isinstance(entry, dict) or cast(dict[str, Any], entry).get("pin") != old_pin:
            return False
        new_entry = {**cast(dict[str, Any], entry), "pin": pin, "v": version}
        if input_schema is not None:
            new_entry["input_schema"] = input_schema
        pins[tool] = new_entry
        conn.execute(
            "UPDATE extensions SET mcp_pins = ? WHERE name = ?",
            (json.dumps(pins, sort_keys=True), name),
        )
    return True


def delete_extension(conn: sqlite3.Connection, name: str) -> bool:
    """Delete the row and its ``ext_jobs`` state in one transaction. False if absent."""
    with atomic(conn):
        conn.execute("DELETE FROM ext_jobs WHERE ext = ?", (name,))
        conn.execute("DELETE FROM ext_panels WHERE ext = ?", (name,))
        conn.execute("DELETE FROM ext_panel_days WHERE ext = ?", (name,))
        cur = conn.execute("DELETE FROM extensions WHERE name = ?", (name,))
    return cur.rowcount == 1


# --- ext_jobs -------------------------------------------------------------------------


def get_job(conn: sqlite3.Connection, ext: str, job_id: str) -> JobState | None:
    row = conn.execute(
        "SELECT * FROM ext_jobs WHERE ext = ? AND job_id = ?", (ext, job_id)
    ).fetchone()
    return None if row is None else _row_to_job(row)


def list_jobs(conn: sqlite3.Connection, ext: str | None = None) -> list[JobState]:
    """Every job's state (or one extension's), sorted by ``(ext, job_id)``."""
    if ext is None:
        rows = conn.execute("SELECT * FROM ext_jobs ORDER BY ext, job_id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ext_jobs WHERE ext = ? ORDER BY job_id", (ext,)
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def mark_due(
    conn: sqlite3.Connection,
    ext: str,
    job_id: str,
    due: datetime,
    *,
    open_task_id: str | None = None,
) -> JobState:
    """Record ``due`` as handled (creating the row if needed); ``last_due`` never moves back.

    ``open_task_id`` (a gated run's task) is set only when ``due`` advances ``last_due``;
    otherwise it is left as is. The caller checks ``last_due < due`` first and calls this in
    the same transaction as ``insert_task``, so a due time is claimed exactly once.
    """
    conn.execute(
        """INSERT INTO ext_jobs (ext, job_id, last_due, open_task_id) VALUES (?, ?, ?, ?)
           ON CONFLICT (ext, job_id) DO UPDATE SET
               last_due = CASE WHEN ext_jobs.last_due IS NULL
                                 OR excluded.last_due > ext_jobs.last_due
                               THEN excluded.last_due ELSE ext_jobs.last_due END,
               open_task_id = CASE WHEN excluded.open_task_id IS NOT NULL
                                    AND (ext_jobs.last_due IS NULL
                                         OR excluded.last_due > ext_jobs.last_due)
                                   THEN excluded.open_task_id ELSE ext_jobs.open_task_id END""",
        (ext, job_id, iso(due), open_task_id),
    )
    state = get_job(conn, ext, job_id)
    assert state is not None
    return state


def record_run(
    conn: sqlite3.Connection,
    ext: str,
    job_id: str,
    *,
    at: datetime,
    outcome: str,
    ok: bool,
    clear_open: bool = True,
) -> JobState:
    """Record a finished run: ``fail_streak`` resets on ``ok`` and grows otherwise.

    Clears ``open_task_id`` unless ``clear_open`` is False (a run on demand, which never
    owned it). Returns the new state (``fail_streak`` feeds health).
    """
    conn.execute(
        """INSERT INTO ext_jobs (ext, job_id, last_run_at, last_outcome, fail_streak)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (ext, job_id) DO UPDATE SET
               last_run_at = excluded.last_run_at, last_outcome = excluded.last_outcome,
               open_task_id = CASE WHEN ? THEN NULL ELSE ext_jobs.open_task_id END,
               fail_streak = CASE WHEN ? THEN 0 ELSE ext_jobs.fail_streak + 1 END""",
        (ext, job_id, iso(at), outcome, 0 if ok else 1, clear_open, ok),
    )
    state = get_job(conn, ext, job_id)
    assert state is not None
    return state


def prune_jobs(conn: sqlite3.Connection, ext: str, keep: Collection[str]) -> int:
    """Delete ``ext``'s job rows not in ``keep`` (jobs an update removed). Returns the count."""
    keep_ids = sorted(keep)
    marks = ", ".join("?" for _ in keep_ids)
    sql = "DELETE FROM ext_jobs WHERE ext = ?"
    if keep_ids:
        sql += f" AND job_id NOT IN ({marks})"
    return conn.execute(sql, (ext, *keep_ids)).rowcount


# --- ext_panels (PER-280) ---------------------------------------------------------------


def _row_to_panel(r: sqlite3.Row) -> PanelState:
    return PanelState(
        ext=r["ext"],
        panel_id=r["panel_id"],
        data_json=r["data_json"],
        updated_at=_dt(r["updated_at"]),
        bytes=r["bytes"],
        last_run_at=_dt(r["last_run_at"]),
        last_ok=None if r["last_ok"] is None else bool(r["last_ok"]),
        last_error=r["last_error"],
        fail_streak=r["fail_streak"],
        expect_by=_dt(r["expect_by"]),
    )


def get_panel(conn: sqlite3.Connection, ext: str, panel_id: str) -> PanelState | None:
    row = conn.execute(
        "SELECT * FROM ext_panels WHERE ext = ? AND panel_id = ?", (ext, panel_id)
    ).fetchone()
    return None if row is None else _row_to_panel(row)


def list_panels(conn: sqlite3.Connection, ext: str | None = None) -> list[PanelState]:
    """Every panel's state (or one extension's), sorted by ``(ext, panel_id)``."""
    if ext is None:
        rows = conn.execute("SELECT * FROM ext_panels ORDER BY ext, panel_id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ext_panels WHERE ext = ? ORDER BY panel_id", (ext,)
        ).fetchall()
    return [_row_to_panel(r) for r in rows]


def save_panel_ok(
    conn: sqlite3.Connection,
    ext: str,
    panel_id: str,
    *,
    data: str,
    updated_at: datetime,
    ran_at: datetime,
    bytes: int,
    expect_by: datetime,
) -> PanelState:
    """Store a good copy: the streak resets and the error clears."""
    conn.execute(
        """INSERT INTO ext_panels (ext, panel_id, data_json, updated_at, bytes, last_run_at,
               last_ok, last_error, fail_streak, expect_by)
           VALUES (?, ?, ?, ?, ?, ?, 1, NULL, 0, ?)
           ON CONFLICT (ext, panel_id) DO UPDATE SET
               data_json = excluded.data_json, updated_at = excluded.updated_at,
               bytes = excluded.bytes, last_run_at = excluded.last_run_at, last_ok = 1,
               last_error = NULL, fail_streak = 0, expect_by = excluded.expect_by""",
        (ext, panel_id, data, iso(updated_at), bytes, iso(ran_at), iso(expect_by)),
    )
    state = get_panel(conn, ext, panel_id)
    assert state is not None
    return state


def record_panel_failure(
    conn: sqlite3.Connection, ext: str, panel_id: str, *, ran_at: datetime, error: str
) -> PanelState:
    """Record a failed run: the streak grows and the last good copy is kept."""
    conn.execute(
        """INSERT INTO ext_panels (ext, panel_id, last_run_at, last_ok, last_error, fail_streak)
           VALUES (?, ?, ?, 0, ?, 1)
           ON CONFLICT (ext, panel_id) DO UPDATE SET
               last_run_at = excluded.last_run_at, last_ok = 0,
               last_error = excluded.last_error, fail_streak = ext_panels.fail_streak + 1""",
        (ext, panel_id, iso(ran_at), error),
    )
    state = get_panel(conn, ext, panel_id)
    assert state is not None
    return state


def prune_panels(conn: sqlite3.Connection, ext: str, keep: Collection[str]) -> int:
    """Delete ``ext``'s panel rows not in ``keep`` (panels an update removed)."""
    keep_ids = sorted(keep)
    marks = ", ".join("?" for _ in keep_ids)
    sql = "DELETE FROM ext_panels WHERE ext = ?"
    if keep_ids:
        sql += f" AND panel_id NOT IN ({marks})"
    return conn.execute(sql, (ext, *keep_ids)).rowcount


def prune_panel_days(conn: sqlite3.Connection, ext: str, keep: Collection[str]) -> int:
    """Delete ``ext``'s archived days of panels not in ``keep`` (removed, or no history)."""
    keep_ids = sorted(keep)
    marks = ", ".join("?" for _ in keep_ids)
    sql = "DELETE FROM ext_panel_days WHERE ext = ?"
    if keep_ids:
        sql += f" AND panel_id NOT IN ({marks})"
    return conn.execute(sql, (ext, *keep_ids)).rowcount


# --- ext_panel_days (PER-286) ---------------------------------------------------------------


def _row_to_day(r: sqlite3.Row) -> PanelDay:
    created = _dt(r["created_at"])
    assert created is not None
    return PanelDay(
        ext=r["ext"],
        panel_id=r["panel_id"],
        day=r["day"],
        status=r["status"],
        data_json=r["data_json"],
        bytes=r["bytes"],
        created_at=created,
        attempts=r["attempts"],
        last_error=r["last_error"],
        next_try_at=_dt(r["next_try_at"]),
        upload_due=bool(r["upload_due"]),
        upload_tries=r["upload_tries"],
        upload_next_at=_dt(r["upload_next_at"]),
    )


def get_panel_day(conn: sqlite3.Connection, ext: str, panel_id: str, day: str) -> PanelDay | None:
    row = conn.execute(
        "SELECT * FROM ext_panel_days WHERE ext = ? AND panel_id = ? AND day = ?",
        (ext, panel_id, day),
    ).fetchone()
    return None if row is None else _row_to_day(row)


def list_panel_days(
    conn: sqlite3.Connection, ext: str, panel_id: str, *, since: str | None = None
) -> list[PanelDay]:
    """A panel's archived days, newest first (``since``: that day and later)."""
    rows = conn.execute(
        "SELECT * FROM ext_panel_days WHERE ext = ? AND panel_id = ? AND day >= ?"
        " ORDER BY day DESC",
        (ext, panel_id, since or ""),
    ).fetchall()
    return [_row_to_day(r) for r in rows]


def save_day_ok(
    conn: sqlite3.Connection,
    ext: str,
    panel_id: str,
    day: str,
    *,
    data: str,
    bytes: int,
    at: datetime,
) -> None:
    """A good copy for ``day``: the relay gets it next."""
    conn.execute(
        """INSERT INTO ext_panel_days (ext, panel_id, day, status, data_json, bytes, created_at,
               attempts, last_error, next_try_at, upload_due, upload_tries, upload_next_at)
           VALUES (?, ?, ?, 'ok', ?, ?, ?, 1, NULL, NULL, 1, 0, NULL)
           ON CONFLICT (ext, panel_id, day) DO UPDATE SET
               status = 'ok', data_json = excluded.data_json, bytes = excluded.bytes,
               attempts = ext_panel_days.attempts + 1, last_error = NULL, next_try_at = NULL,
               upload_due = 1, upload_tries = 0, upload_next_at = NULL""",
        (ext, panel_id, day, data, bytes, iso(at)),
    )


def save_day_none(
    conn: sqlite3.Connection, ext: str, panel_id: str, day: str, *, at: datetime
) -> None:
    """No data that day: never retried, never listed. A copy it replaces (a rebuild) is
    dropped from the relay too."""
    conn.execute(
        """INSERT INTO ext_panel_days (ext, panel_id, day, status, data_json, bytes, created_at,
               attempts, last_error, next_try_at, upload_due, upload_tries, upload_next_at)
           VALUES (?, ?, ?, 'none', NULL, 0, ?, 1, NULL, NULL, 0, 0, NULL)
           ON CONFLICT (ext, panel_id, day) DO UPDATE SET
               status = 'none', upload_due = (ext_panel_days.data_json IS NOT NULL),
               data_json = NULL, bytes = 0, attempts = ext_panel_days.attempts + 1,
               last_error = NULL, next_try_at = NULL, upload_tries = 0, upload_next_at = NULL""",
        (ext, panel_id, day, iso(at)),
    )


def record_day_failure(
    conn: sqlite3.Connection,
    ext: str,
    panel_id: str,
    day: str,
    *,
    at: datetime,
    error: str | None,
    max_attempts: int,
    next_try_at: datetime | None,
    count: bool = True,
) -> PanelDay:
    """A failed attempt: ``pending`` until ``next_try_at``, ``failed`` after ``max_attempts``.
    A good copy from before (a rebuild) is kept. ``count=False`` (a dry run, a kill) only
    defers the day."""
    add = 1 if count else 0
    conn.execute(
        """INSERT INTO ext_panel_days (ext, panel_id, day, status, created_at, attempts,
               last_error, next_try_at)
           VALUES (?, ?, ?, CASE WHEN ? >= ? THEN 'failed' ELSE 'pending' END, ?, ?, ?, ?)
           ON CONFLICT (ext, panel_id, day) DO UPDATE SET
               attempts = ext_panel_days.attempts + ?,
               status = CASE WHEN ext_panel_days.attempts + ? >= ? THEN 'failed'
                             ELSE 'pending' END,
               last_error = COALESCE(excluded.last_error, ext_panel_days.last_error),
               next_try_at = excluded.next_try_at""",
        (ext, panel_id, day, add, max_attempts, iso(at), add, error,
         None if next_try_at is None else iso(next_try_at), add, add, max_attempts),
    )  # fmt: skip
    row = get_panel_day(conn, ext, panel_id, day)
    assert row is not None
    return row


def queue_rebuild(
    conn: sqlite3.Connection, ext: str, panel_id: str, days: Collection[str], *, at: datetime
) -> int:
    """Mark ``days`` to be archived again (``resonant ext panel-history --rebuild``): each is
    ``pending`` with no attempts; a good copy shows until the new one replaces it."""
    n = 0
    for day in sorted(days):
        conn.execute(
            """INSERT INTO ext_panel_days (ext, panel_id, day, status, created_at)
               VALUES (?, ?, ?, 'pending', ?)
               ON CONFLICT (ext, panel_id, day) DO UPDATE SET
                   status = 'pending', attempts = 0, last_error = NULL, next_try_at = NULL""",
            (ext, panel_id, day, iso(at)),
        )
        n += 1
    return n


def prune_days_before(conn: sqlite3.Connection, ext: str, panel_id: str, day: str) -> int:
    """Delete a panel's archived days before ``day`` (past its ``keep``)."""
    return conn.execute(
        "DELETE FROM ext_panel_days WHERE ext = ? AND panel_id = ? AND day < ?",
        (ext, panel_id, day),
    ).rowcount


def days_to_upload(conn: sqlite3.Connection, now: datetime, limit: int) -> list[PanelDay]:
    """Days the relay still has to receive (oldest first), not held by a backoff."""
    rows = conn.execute(
        """SELECT * FROM ext_panel_days WHERE upload_due = 1
               AND (upload_next_at IS NULL OR upload_next_at <= ?)
           ORDER BY day, ext, panel_id LIMIT ?""",
        (iso(now), limit),
    ).fetchall()
    return [_row_to_day(r) for r in rows]


def mark_uploaded(
    conn: sqlite3.Connection, ext: str, panel_id: str, day: str, sent: str | None
) -> bool:
    """The relay has ``sent`` (the ``data_json`` that was uploaded). A row whose copy changed
    meanwhile (a rebuild landed) stays due. True when cleared."""
    return (
        conn.execute(
            """UPDATE ext_panel_days SET upload_due = 0, upload_tries = 0, upload_next_at = NULL
               WHERE ext = ? AND panel_id = ? AND day = ? AND data_json IS ?""",
            (ext, panel_id, day, sent),
        ).rowcount
        == 1
    )


def mark_upload_failed(
    conn: sqlite3.Connection, ext: str, panel_id: str, day: str, *, next_at: datetime
) -> None:
    conn.execute(
        """UPDATE ext_panel_days SET upload_tries = upload_tries + 1, upload_next_at = ?
           WHERE ext = ? AND panel_id = ? AND day = ?""",
        (iso(next_at), ext, panel_id, day),
    )

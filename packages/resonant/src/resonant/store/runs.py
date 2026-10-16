"""Claude runner runs (migration 006). Plain functions over a connection.

A run is one ask worked on by one Claude session, in a worktree of the repo's checkout or, with
no repo (``repo = ''``), in a scratch dir (migration 009). The row holds
everything a restart needs: the step reached, the session id, the live process's group, leader
and start time (so a reused pid is never signalled), the time already used, and the outcome.
Callers own transactions; every function here is one statement or joins the caller's.

Codes (``R1``, ``R2`` ...) come from an AUTOINCREMENT key and are never reused.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from resonant.store.db import atomic, iso, now_iso

Status = Literal[
    "queued", "running", "waiting", "ready", "merged", "failed", "cancelled", "no_changes", "done"
]
Step = Literal["prepare", "session", "harvest", "publish", "reply"]

ACTIVE: tuple[Status, ...] = ("queued", "running", "waiting")
ENDED: tuple[Status, ...] = ("ready", "merged", "failed", "cancelled", "no_changes", "done")

_COLUMNS = frozenset(
    {
        "task_id", "status", "step", "branch", "base_branch", "base_sha", "head_sha",
        "sessions", "attempts", "pgid", "leader_pid", "leader_started_at", "session_seconds",
        "wait_seconds", "waiting_since", "cost_usd", "tokens_in", "tokens_out", "exit_code",
        "pr_number", "pr_url", "files_changed", "flagged_paths", "summary", "failure",
        "started_at", "ended_at", "workspace", "checkout",
    }
)  # fmt: skip


@dataclass(frozen=True)
class Run:
    code: str
    task_id: str | None
    principal: str
    repo: str
    ask: str
    status: Status
    step: Step
    branch: str | None
    base_branch: str | None
    base_sha: str | None
    head_sha: str | None
    session_id: str
    sessions: int
    attempts: int
    pgid: int | None
    leader_pid: int | None
    leader_started_at: float | None
    session_seconds: float
    max_seconds: int
    max_budget_usd: float | None
    wait_seconds: float
    waiting_since: datetime | None
    cost_usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    exit_code: int | None
    pr_number: int | None
    pr_url: str | None
    files_changed: int | None
    flagged_paths: tuple[str, ...]
    summary: str | None
    failure: str | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    updated_at: datetime
    workspace: str | None = None  # the worktree or scratch dir (migration 009)
    checkout: str | None = None  # the checkout the worktree belongs to

    @property
    def scratch(self) -> bool:
        """A run with no repo: a scratch dir, no GitHub token, no PR."""
        return not self.repo

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def flag_line(self) -> str:
        """``⚠ changes Makefile, package.json`` or an empty string (same form as the
        checks module's)."""
        return f"⚠ changes {', '.join(self.flagged_paths)}" if self.flagged_paths else ""


def _dt(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def _row(r: sqlite3.Row) -> Run:
    created, updated = _dt(r["created_at"]), _dt(r["updated_at"])
    assert created is not None and updated is not None
    flagged = cast(list[str], json.loads(r["flagged_paths"]))
    return Run(
        code=r["code"],
        task_id=r["task_id"],
        principal=r["principal"],
        repo=r["repo"],
        ask=r["ask"],
        status=r["status"],
        step=r["step"],
        branch=r["branch"],
        base_branch=r["base_branch"],
        base_sha=r["base_sha"],
        head_sha=r["head_sha"],
        session_id=r["session_id"],
        sessions=r["sessions"],
        attempts=r["attempts"],
        pgid=r["pgid"],
        leader_pid=r["leader_pid"],
        leader_started_at=r["leader_started_at"],
        session_seconds=r["session_seconds"],
        max_seconds=r["max_seconds"],
        max_budget_usd=r["max_budget_usd"],
        wait_seconds=r["wait_seconds"],
        waiting_since=_dt(r["waiting_since"]),
        cost_usd=r["cost_usd"],
        tokens_in=r["tokens_in"],
        tokens_out=r["tokens_out"],
        exit_code=r["exit_code"],
        pr_number=r["pr_number"],
        pr_url=r["pr_url"],
        files_changed=r["files_changed"],
        flagged_paths=tuple(flagged),
        summary=r["summary"],
        failure=r["failure"],
        created_at=created,
        started_at=_dt(r["started_at"]),
        ended_at=_dt(r["ended_at"]),
        updated_at=updated,
        # no columns before migration 009 (a migration reading runs part way)
        workspace=r["workspace"] if "workspace" in r.keys() else None,  # noqa: SIM118
        checkout=r["checkout"] if "checkout" in r.keys() else None,  # noqa: SIM118
    )


def create(
    conn: sqlite3.Connection,
    *,
    repo: str,
    ask: str,
    principal: str,
    session_id: str,
    max_seconds: int,
    max_budget_usd: float | None = None,
) -> Run:
    """Insert a queued run and give it the next code. Never reuses a code."""
    ts = now_iso()
    with atomic(conn):
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'runs'").fetchone()
        seq = (0 if row is None else int(row["seq"])) + 1
        conn.execute(
            "INSERT INTO runs (seq, code, principal, repo, ask, session_id, max_seconds,"
            " max_budget_usd, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (seq, f"R{seq}", principal, repo, ask, session_id, max_seconds, max_budget_usd, ts, ts),
        )
    run = get(conn, f"R{seq}")
    assert run is not None
    return run


def get(conn: sqlite3.Connection, code: str) -> Run | None:
    r = conn.execute("SELECT * FROM runs WHERE code = ?", (code.upper(),)).fetchone()
    return None if r is None else _row(r)


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[Run]:
    """Active runs first, then the most recent."""
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY (status IN ('queued', 'running', 'waiting')) DESC,"
        " seq DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row(r) for r in rows]


def active(conn: sqlite3.Connection, repo: str | None = None) -> list[Run]:
    sql = "SELECT * FROM runs WHERE status IN ('queued', 'running', 'waiting')"
    params: list[Any] = []
    if repo is not None:
        sql += " AND repo = ?"
        params.append(repo)
    return [_row(r) for r in conn.execute(sql + " ORDER BY seq", params)]


def with_process(conn: sqlite3.Connection) -> list[Run]:
    """Runs that have a recorded session process (the startup reaper's input)."""
    return [
        _row(r)
        for r in conn.execute("SELECT * FROM runs WHERE pgid IS NOT NULL OR leader_pid IS NOT NULL")
    ]


def update(conn: sqlite3.Connection, code: str, **fields: Any) -> bool:
    """Set columns (a whitelist) on a run. ``flagged_paths`` takes a sequence of strings and
    datetimes are stored as ISO text."""
    if not fields:
        return False
    bad = set(fields) - _COLUMNS
    if bad:
        raise ValueError(f"not run columns: {sorted(bad)}")
    values: list[Any] = []
    for key, value in fields.items():
        if key == "flagged_paths":
            value = json.dumps(list(value))
        elif isinstance(value, datetime):
            value = iso(value)
        values.append(value)
    sets = ", ".join(f"{k} = ?" for k in fields)
    sql = f"UPDATE runs SET {sets}, updated_at = ? WHERE code = ?"  # noqa: S608 - whitelisted
    return conn.execute(sql, [*values, now_iso(), code]).rowcount == 1


def finish(
    conn: sqlite3.Connection,
    code: str,
    status: Status,
    *,
    failure: str | None = None,
    **fields: Any,
) -> bool:
    """End an active run: sets the status, ``ended_at``, clears the process fields. Returns
    False when the run was no longer active (a cancel that came first wins)."""
    if status in ACTIVE:
        raise ValueError("finish() takes an ended status")
    with atomic(conn):
        run = get(conn, code)
        if run is None or not run.active:
            return False
        return update(
            conn,
            code,
            status=status,
            failure=failure,
            ended_at=now_iso(),
            pgid=None,
            leader_pid=None,
            leader_started_at=None,
            **fields,
        )


def cancel(conn: sqlite3.Connection, code: str, reason: str) -> bool:
    """queued, running or waiting -> cancelled. False if it wasn't active."""
    return finish(conn, code, "cancelled", failure=reason, step="reply")


def set_process(conn: sqlite3.Connection, code: str, pgid: int, pid: int, started: float) -> None:
    """Record the live session's process."""
    conn.execute(
        "UPDATE runs SET pgid = ?, leader_pid = ?, leader_started_at = ?,"
        " updated_at = ? WHERE code = ?",
        (pgid, pid, started, now_iso(), code),
    )


def mark_session(conn: sqlite3.Connection, code: str) -> None:
    """Claude's first message of a session arrived: it exists, so it can be resumed."""
    conn.execute(
        "UPDATE runs SET sessions = sessions + 1, updated_at = ? WHERE code = ?",
        (now_iso(), code),
    )


def clear_process(conn: sqlite3.Connection, code: str, *, crashed: bool = False) -> None:
    """The session's processes are gone. ``crashed`` counts a daemon crash as an attempt."""
    with atomic(conn):
        update(conn, code, pgid=None, leader_pid=None, leader_started_at=None)
        if crashed:
            conn.execute("UPDATE runs SET attempts = attempts + 1 WHERE code = ?", (code,))


def ended_at_map(conn: sqlite3.Connection) -> dict[str, float]:
    """``code -> epoch seconds`` for runs that ended (the cleanup job's input). Active runs
    are left out and so are never touched."""
    rows = conn.execute(
        "SELECT code, ended_at FROM runs WHERE ended_at IS NOT NULL"
        " AND status NOT IN ('queued', 'running', 'waiting')"
    ).fetchall()
    out: dict[str, float] = {}
    for r in rows:
        ended = _dt(r["ended_at"])
        if ended is not None:
            out[r["code"]] = ended.timestamp()
    return out


def orphans(conn: sqlite3.Connection) -> list[tuple[Run, str, str | None]]:
    """Active runs whose task already ended: ``(run, task status, task error)``. A task fails
    by itself after too many crashes, and then nothing would end its run."""
    rows = conn.execute(
        "SELECT r.*, t.status AS t_status, t.error AS t_error FROM runs r"
        " JOIN tasks t ON t.id = r.task_id"
        " WHERE r.status IN ('queued', 'running', 'waiting')"
        " AND t.status IN ('failed', 'cancelled', 'done')"
    ).fetchall()
    return [(_row(r), r["t_status"], r["t_error"]) for r in rows]


def count_by_status(conn: sqlite3.Connection) -> Mapping[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM runs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}

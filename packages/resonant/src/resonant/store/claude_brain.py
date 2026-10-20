"""The Claude brain's stored state (BR-11): kv ``brain.claude_session``, read by
``runners/claude_session.py`` and shown by /status and the dashboard. Kept apart so those can
read it without importing the runners.

``{session_id, started_at, turns, written, pid?, created?, marker?, remote_url?, hops?}``:
``written`` once a turn came back (so ``--resume`` can work); ``pid``/``created``/``marker``
while a process runs; ``remote_url`` while Remote Control is on; ``hops`` the last turn's
taint hops (absent: clean).

PER-258: kv ``brain.shell_grants`` holds one-shot grants for the brain's shell changes the
owner approved after their turn (``add_grant``/``take_grant``), and the ``brain.tool`` audit
rows (one per Claude brain tool call) feed the dashboard (``recent_tools``, ``tool_counts``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, cast

from resonant.store.db import atomic, iso, kv_get, kv_set

SESSION_KV = "brain.claude_session"


def session_state(conn: sqlite3.Connection) -> dict[str, Any]:
    raw = kv_get(conn, SESSION_KV)
    return dict(cast(dict[str, Any], raw)) if isinstance(raw, dict) else {}


def status_of(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """``{state: up|down, turns, remote_url?}``, or None when there is no session yet."""
    st = session_state(conn)
    if not st.get("session_id"):
        return None
    turns = st.get("turns")
    out: dict[str, Any] = {
        "state": "up" if isinstance(st.get("pid"), int) else "down",
        "turns": turns if isinstance(turns, int) and not isinstance(turns, bool) else 0,
    }
    url = st.get("remote_url")
    if isinstance(url, str) and url:
        out["remote_url"] = url
    return out


# --- shell grants (PER-258) ----------------------------------------------------------------

GRANTS_KV = "brain.shell_grants"
GRANT_TTL = timedelta(minutes=30)
TOOL_ACTION = "brain.tool"  # one audit row per Claude brain tool call
COUNT_WINDOW = 1000


def grant_key(args: Mapping[str, Any]) -> str:
    """What a grant matches: the tool, the exact command (or path and edit digest) and the
    directory. Never the display fields."""
    keep = {k: args.get(k) for k in ("tool", "command", "cwd", "digest")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()


def add_grant(
    conn: sqlite3.Connection, args: Mapping[str, Any], *, task_id: str, now: datetime
) -> None:
    """A one-shot grant: the owner said yes to this change after its turn ended, so the next
    identical request runs once without asking (``take_grant``), within ``GRANT_TTL``. Call
    inside a transaction."""
    grants = _grants(conn, now)
    grants[grant_key(args)] = {"task": task_id, "expires": iso(now + GRANT_TTL)}
    kv_set(conn, GRANTS_KV, grants)


def take_grant(conn: sqlite3.Connection, args: Mapping[str, Any], *, now: datetime) -> str | None:
    """Spend the grant for this change, if there is one: the task that was approved."""
    with atomic(conn):
        grants = _grants(conn, now)
        found = grants.pop(grant_key(args), None)
        if found is not None:
            kv_set(conn, GRANTS_KV, grants)
    task = found.get("task") if found is not None else None
    return task if isinstance(task, str) else None


def _grants(conn: sqlite3.Connection, now: datetime) -> dict[str, dict[str, Any]]:
    raw = kv_get(conn, GRANTS_KV)
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in cast(dict[str, Any], raw).items():
        if not isinstance(v, dict):
            continue
        g = cast(dict[str, Any], v)
        try:
            expires = datetime.fromisoformat(str(g.get("expires")))
        except ValueError:
            continue
        if expires > now:
            out[k] = g
    return out


def recent_tools(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """The Claude brain's last tool calls (``brain.tool`` audit rows), newest first."""
    rows = conn.execute(
        """SELECT at, tool, decision, detail FROM audit_log WHERE action = ?
           ORDER BY id DESC LIMIT ?""",
        (TOOL_ACTION, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for at, tool, decision, detail in rows:
        try:
            d: Any = json.loads(detail or "{}")
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append({**cast(dict[str, Any], d), "at": at, "tool": tool, "decision": decision})
    return out


def tool_counts(conn: sqlite3.Connection, since: datetime) -> dict[str, int]:
    """Counts since ``since``: ``calls`` (every tool call), ``writes`` (changes that ran),
    over the last ``COUNT_WINDOW`` calls (the audit log has no index on its action)."""
    row = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(json_extract(detail, '$.write') = 1
                                       AND json_extract(detail, '$.status') = 'ok'), 0)
           FROM (SELECT at, detail FROM audit_log WHERE action = ? ORDER BY id DESC LIMIT ?)
           WHERE at >= ?""",
        (TOOL_ACTION, COUNT_WINDOW, iso(since)),
    ).fetchone()
    return {"calls": int(row[0]), "writes": int(row[1])}

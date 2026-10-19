"""The Claude brain's stored state (BR-11): kv ``brain.claude_session``, read by
``runners/claude_session.py`` and shown by /status and the dashboard. Kept apart so those can
read it without importing the runners.

``{session_id, started_at, turns, written, pid?, created?, marker?, remote_url?, hops?}``:
``written`` once a turn came back (so ``--resume`` can work); ``pid``/``created``/``marker``
while a process runs; ``remote_url`` while Remote Control is on; ``hops`` the last turn's
taint hops (absent: clean).
"""

from __future__ import annotations

import sqlite3
from typing import Any, cast

from resonant.store.db import kv_get

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

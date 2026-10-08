"""Global kill switch (owner-only).

Engaging it does two things:
- ``autonomy = "off"``: the gate requires approval for everything above L0, and pending
  veto timers are cancelled (Phase 2).
- ``paused = true``: critical-job wrappers (``resonant job-guard``) and on-call autonomy
  check this before acting, so they hold even while the daemon is down.
"""

from __future__ import annotations

import sqlite3

from resonant.store.audit import audit
from resonant.store.db import kv_get, kv_set, transaction


def engage(conn: sqlite3.Connection, *, actor: str, reason: str | None = None) -> None:
    with transaction(conn):
        kv_set(conn, "autonomy", "off")
        kv_set(conn, "paused", True)
        audit(conn, "killswitch.engage", requested_by=actor, reason=reason)


def release(conn: sqlite3.Connection, *, actor: str) -> None:
    with transaction(conn):
        kv_set(conn, "autonomy", "on")
        kv_set(conn, "paused", False)
        audit(conn, "killswitch.release", requested_by=actor)


def is_paused(conn: sqlite3.Connection) -> bool:
    return bool(kv_get(conn, "paused", False))

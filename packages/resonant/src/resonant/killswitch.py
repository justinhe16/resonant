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
from resonant.store.db import atomic, kv_get, kv_set


def _state(conn: sqlite3.Connection) -> dict[str, object]:
    return {"autonomy": kv_get(conn, "autonomy"), "paused": kv_get(conn, "paused")}


def engage(conn: sqlite3.Connection, *, actor: str, reason: str | None = None) -> None:
    with atomic(conn):
        before = _state(conn)
        kv_set(conn, "autonomy", "off")
        kv_set(conn, "paused", True)
        audit(conn, "killswitch.engage", requested_by=actor, reason=reason, before=before)


def release(conn: sqlite3.Connection, *, actor: str) -> None:
    """Restore full autonomy and unpause. Owner-only; audited with the prior state."""
    with atomic(conn):
        before = _state(conn)
        kv_set(conn, "autonomy", "on")
        kv_set(conn, "paused", False)
        audit(conn, "killswitch.release", requested_by=actor, before=before)


class PausedStateUnknownError(RuntimeError):
    pass


def is_paused(conn: sqlite3.Connection) -> bool:
    """Read the paused flag. A missing or non-boolean row is unknown, so callers fail closed."""
    value = kv_get(conn, "paused")
    if not isinstance(value, bool):
        raise PausedStateUnknownError(f"paused flag is {value!r}")
    return value

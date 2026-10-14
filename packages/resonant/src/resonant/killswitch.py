"""Global kill switch (owner-only).

Engaging it does two things:
- ``autonomy = "off"``: the gate requires approval for everything above L0. The gate
  reads it on every call, and anything but ``"on"`` counts as off.
- ``paused = true``: critical-job wrappers (``resonant job-guard``) and on-call autonomy
  check this before acting, so they hold even while the daemon is down.

Pause hooks (Phase 3). ``add_pause_hook(fn)`` registers ``fn(conn, paused)``, called after
``engage`` (``paused=True``) or ``release`` (``False``) commits. The daemon's scheduler uses
it to kill every tracked extension process at once on ``/kill``. A hook that raises is
logged and never undoes the switch. A CLI ``resonant kill`` runs in another process, so no
hook fires there: the scheduler notices the flag on its next tick.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from resonant.store.audit import audit
from resonant.store.db import atomic, kv_get, kv_set

log = logging.getLogger(__name__)

PauseHook = Callable[[sqlite3.Connection, bool], None]
_hooks: list[PauseHook] = []


def add_pause_hook(hook: PauseHook) -> Callable[[], None]:
    """Call ``hook(conn, paused)`` after every engage and release. Returns a remover."""
    _hooks.append(hook)

    def remove() -> None:
        if hook in _hooks:
            _hooks.remove(hook)

    return remove


def _notify(conn: sqlite3.Connection, paused: bool) -> None:
    for hook in list(_hooks):
        try:
            hook(conn, paused)
        except Exception:
            log.exception("kill switch hook failed")


def _state(conn: sqlite3.Connection) -> dict[str, object]:
    return {"autonomy": kv_get(conn, "autonomy"), "paused": kv_get(conn, "paused")}


def engage(conn: sqlite3.Connection, *, actor: str, reason: str | None = None) -> None:
    with atomic(conn):
        before = _state(conn)
        kv_set(conn, "autonomy", "off")
        kv_set(conn, "paused", True)
        audit(conn, "killswitch.engage", requested_by=actor, reason=reason, before=before)
    _notify(conn, True)


def release(conn: sqlite3.Connection, *, actor: str) -> None:
    """Restore full autonomy and unpause. Owner-only; audited with the prior state."""
    with atomic(conn):
        before = _state(conn)
        kv_set(conn, "autonomy", "on")
        kv_set(conn, "paused", False)
        audit(conn, "killswitch.release", requested_by=actor, before=before)
    _notify(conn, False)


class PausedStateUnknownError(RuntimeError):
    pass


def is_paused(conn: sqlite3.Connection) -> bool:
    """Read the paused flag. A missing or non-boolean row is unknown, so callers fail closed."""
    value = kv_get(conn, "paused")
    if not isinstance(value, bool):
        raise PausedStateUnknownError(f"paused flag is {value!r}")
    return value

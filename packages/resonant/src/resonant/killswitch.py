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
hook fires there: the scheduler notices the flag on its next tick and fires the hooks itself
(``fire_hooks``), so a pause written by another process (``resonant kill``, personal
Resonant's fan-out) ends runs and the brain session like an in-process ``/kill``.

Pause source (PER-290). ``paused_by`` (kv) is who engaged it: the owner's handle,
``owner:cli``, or ``sibling:resonant`` when personal Resonant's ``/kill`` paused this instance
(``resonant pause --by sibling:resonant``). An engage while already paused keeps the first
source, so personal's ``/resume`` (``resonant resume --if-by sibling:resonant``) never undoes a
pause someone else made. ``release`` clears it.
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
PAUSED_BY_KEY = "paused_by"
SIBLING_SOURCE = "sibling:resonant"  # personal Resonant's /kill, fanned out (PER-290)


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
    """Autonomy off, paused. ``paused_by`` becomes ``actor`` unless already paused (the
    first source stays)."""
    with atomic(conn):
        before = _state(conn)
        if kv_get(conn, "paused") is not True or not isinstance(paused_by(conn), str):
            kv_set(conn, PAUSED_BY_KEY, actor)
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
        conn.execute("DELETE FROM kv WHERE key = ?", (PAUSED_BY_KEY,))
        audit(conn, "killswitch.release", requested_by=actor, before=before)
    _notify(conn, False)


def paused_by(conn: sqlite3.Connection) -> str | None:
    """Who engaged the pause in force (PER-290), or None (not recorded)."""
    value = kv_get(conn, PAUSED_BY_KEY)
    return value if isinstance(value, str) else None


def fire_hooks(conn: sqlite3.Connection, paused: bool) -> None:
    """Run the pause hooks for a change another process made (the scheduler's tick saw the
    flag move without an in-process ``engage``/``release``)."""
    _notify(conn, paused)


class PausedStateUnknownError(RuntimeError):
    pass


def is_paused(conn: sqlite3.Connection) -> bool:
    """Read the paused flag. A missing or non-boolean row is unknown, so callers fail closed."""
    value = kv_get(conn, "paused")
    if not isinstance(value, bool):
        raise PausedStateUnknownError(f"paused flag is {value!r}")
    return value

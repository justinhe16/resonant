"""Two daemons, one checkout (PER-290): what one instance does to its siblings.

Every instance runs from the same checkout and venv, each with its own ``RESONANT_HOME``.
The siblings are found from their LaunchAgent plists (``instance.siblings``). Each one is
reached only through what is already its own:

- **Kill switch.** Personal Resonant's ``/kill`` (and ``resonant kill``) also pauses every
  sibling by running the sibling's CLI, ``RESONANT_HOME=<home> resonant pause --by
  sibling:resonant``, which writes the sibling's own ``paused`` kv; its daemon sees it on its
  next tick (``Scheduler.tick`` fires the pause hooks). Personal ``/resume`` runs ``resonant
  resume --if-by sibling:resonant``, which resumes the sibling only when that pause is the
  one in force: a pause an approver made on the sibling stays. A sibling whose CLI fails is
  named in the reply ("Cori: couldn't pause (exit 1)"); personal is paused either way.
- **Deploys** (``tools.ops.Ops.update_resonant``): one pull and one sync for all; on a
  migration-bearing deploy every store is backed up first (``backup_store``, with the running
  code, before any daemon restarts on the new code); then each loaded sibling gets the restart
  marker in its own store and a ``launchctl kickstart -k`` (``restart_sibling``), so it says
  "Back up" on its own channel; the deploying instance restarts last. A stopped (unloaded)
  sibling is left stopped and reported.

Every subprocess goes through the ``OpsRunner`` (argv, no shell, a timeout, the minimal
``ops_env``); a sibling's env is passed as ``/usr/bin/env NAME=value`` arguments. stderr is
never shown.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from resonant import instance
from resonant.instance import Sibling
from resonant.killswitch import SIBLING_SOURCE
from resonant.ops import (
    KICKSTART_TIMEOUT_S,
    LAUNCHCTL,
    OpsRunner,
    clear_restart,
    kickstart_argv,
    mark_restart,
)

ENV = "/usr/bin/env"
CLI_TIMEOUT_S = 20.0
PRINT_TIMEOUT_S = 10.0
BACKUP_TIMEOUT_S = 90.0
# ``resonant resume --if-by``: the pause in force isn't from that source, so it stays.
EXIT_KEPT = 3
# ``resonant resume --if-by``: it wasn't paused at all.
EXIT_NOT_PAUSED = 4

KillSwitchFanOut = Callable[[bool], Coroutine[Any, Any, list[str]]]


def cli_argv(sibling: Sibling, *args: str, python: str | None = None) -> list[str]:
    """``/usr/bin/env RESONANT_HOME=<home> [RESONANT_INSTANCE=<name>] <python> -m resonant
    <args>``: the sibling's own CLI, from this checkout's venv."""
    env = [f"{instance.HOME_ENV}={sibling.home}"]
    if not instance.is_default(sibling.name):
        env.append(f"{instance.INSTANCE_ENV}={sibling.name}")
    return [ENV, *env, python or sys.executable, "-m", "resonant", *args]


def print_argv(label: str, uid: int | None = None) -> list[str]:
    """``launchctl print gui/<uid>/<label>``: exit 0 when the agent is loaded."""
    if not instance.is_daemon_label(label):
        raise ValueError(f"not an instance daemon label: {label!r}")
    return [LAUNCHCTL, "print", f"gui/{os.getuid() if uid is None else uid}/{label}"]


async def _cli(runner: OpsRunner, sibling: Sibling, *args: str) -> tuple[int | None, str]:
    """Run the sibling's CLI: ``(returncode, why)``; returncode None when it didn't finish."""
    try:
        res = await runner.run(cli_argv(sibling, *args), timeout_s=CLI_TIMEOUT_S)
    except TimeoutError:
        return None, f"timed out after {CLI_TIMEOUT_S:g}s"
    except OSError as e:
        return None, f"couldn't start ({type(e).__name__})"
    return res.returncode, f"exit {res.returncode}"


async def pause_siblings(runner: OpsRunner, siblings: Sequence[Sibling]) -> list[str]:
    """``resonant pause --by sibling:resonant`` in every sibling: one line each."""
    lines: list[str] = []
    for s in siblings:
        rc, why = await _cli(runner, s, "pause", "--by", SIBLING_SOURCE)
        lines.append(f"{s.display_name}: paused" if rc == 0 else
                     f"{s.display_name}: couldn't pause ({why})")  # fmt: skip
    return lines


async def resume_siblings(runner: OpsRunner, siblings: Sequence[Sibling]) -> list[str]:
    """``resonant resume --if-by sibling:resonant`` in every sibling: one line each."""
    lines: list[str] = []
    for s in siblings:
        rc, why = await _cli(runner, s, "resume", "--if-by", SIBLING_SOURCE)
        if rc == 0:
            lines.append(f"{s.display_name}: resumed")
        elif rc == EXIT_NOT_PAUSED:
            lines.append(f"{s.display_name}: wasn't paused")
        elif rc == EXIT_KEPT:
            lines.append(
                f"{s.display_name}: still paused (not paused by {instance.DEFAULT_DISPLAY_NAME})"
            )
        else:
            lines.append(f"{s.display_name}: couldn't resume ({why})")
    return lines


def kill_switch_fan_out(
    me: str,
    runner: OpsRunner,
    *,
    find: Callable[[str], list[Sibling]] = instance.siblings,
) -> KillSwitchFanOut | None:
    """What personal Resonant's ``/kill`` and ``/resume`` run after their own switch:
    ``fan(True)`` pauses every sibling, ``fan(False)`` resumes the ones it paused. None for
    any other instance (its kill switch is its own)."""
    if not instance.is_default(me):
        return None

    async def fan(paused: bool) -> list[str]:
        found = find(me)
        if not found:
            return []
        return await (pause_siblings if paused else resume_siblings)(runner, found)

    return fan


# --- deploys ------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreBackup:
    """One instance's store to back up before a migration-bearing deploy."""

    name: str  # display name
    home: Path
    instance: str


def backup_store(target: StoreBackup, now: datetime, actor: str) -> str | None:
    """Back ``target``'s store up with the running code (blocking: run it in a thread).
    None on success, else a short reason. The copy is named and pruned as that instance's
    own (``<name>-YYYY-MM-DD.db``) and recorded in its own store."""
    from resonant import backup
    from resonant.config import InstanceConfig, Settings
    from resonant.store.db import StoreUnavailableError, open_existing

    try:
        settings = Settings(home=target.home, instance=InstanceConfig(name=target.instance))
    except ValueError as e:  # a config the running code can't load
        return f"its config didn't load ({type(e).__name__})"
    try:
        result = backup.create_backup(settings, now=now)
    except backup.BackupBusy:
        return None  # a backup is being made right now: that copy is as good
    except backup.BackupError as e:
        return str(e)[:80]
    try:
        conn = open_existing(settings.db_path)
    except StoreUnavailableError:
        return None  # the copy is made; only its kv record is missing
    try:
        backup.record(conn, now=now, result=result, actor=actor)
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return None


async def backup_stores(
    targets: Sequence[StoreBackup], now: datetime, actor: str
) -> list[dict[str, Any]]:
    """Back every store up, one after another: ``[{name, ok, error?}]``."""
    out: list[dict[str, Any]] = []
    for t in targets:
        try:
            error = await asyncio.wait_for(
                asyncio.to_thread(backup_store, t, now, actor), BACKUP_TIMEOUT_S
            )
        except TimeoutError:
            error = f"timed out after {BACKUP_TIMEOUT_S:g}s"
        out.append({"name": t.name, "ok": error is None} | ({"error": error} if error else {}))
    return out


async def restart_sibling(
    runner: OpsRunner,
    sibling: Sibling,
    *,
    commit: str | None,
    now: datetime,
    uid: int | None = None,
) -> dict[str, Any]:
    """Restart one sibling on the new code: ``{name, state, detail?}`` with ``state`` one of
    ``restarted``, ``stopped`` (not loaded: left stopped) or ``failed``.

    The restart marker goes into the sibling's own store first, so the sibling says "Back
    up" to its own owner on its own channel when it starts (``ops.back_up``)."""
    from resonant.store.db import StoreUnavailableError, open_existing

    out: dict[str, Any] = {"name": sibling.display_name}
    try:
        loaded = await runner.run(print_argv(sibling.label, uid), timeout_s=PRINT_TIMEOUT_S)
    except (TimeoutError, OSError) as e:
        return out | {"state": "failed", "detail": f"launchctl print: {type(e).__name__}"}
    if loaded.returncode != 0:
        return out | {"state": "stopped"}
    db_path = sibling.home / "resonant.db"
    conn: sqlite3.Connection | None = None
    try:
        conn = open_existing(db_path)
        mark_restart(conn, by="", commit=commit, now=now)  # "" -> its own owner
    except (StoreUnavailableError, sqlite3.Error):
        if conn is not None:
            conn.close()
        conn = None  # restart it anyway; it just won't say "Back up"
    why: str | None = None
    try:
        res = await runner.run(kickstart_argv(sibling.label, uid), timeout_s=KICKSTART_TIMEOUT_S)
        if res.returncode != 0:
            why = f"exit {res.returncode}"
    except TimeoutError:
        why = f"timed out after {KICKSTART_TIMEOUT_S:g}s"
    except OSError as e:
        why = f"couldn't start ({type(e).__name__})"
    if conn is not None:
        try:
            if why is not None:
                clear_restart(conn, planned=True)
        finally:
            conn.close()
    if why is not None:
        return out | {"state": "failed", "detail": why}
    return out | {"state": "restarted"}


def _dicts(raw: object) -> list[dict[str, Any]]:
    items = cast(list[object], raw) if isinstance(raw, list) else []
    return [cast(dict[str, Any], d) for d in items if isinstance(d, dict)]


def sibling_lines(result: dict[str, Any]) -> list[str]:
    """The deploy reply's lines about siblings and backups (code-built)."""
    lines = [
        f"{b.get('name')}: backup failed ({b.get('error')})"
        for b in _dicts(result.get("backups"))
        if b.get("ok") is False
    ]
    for s in _dicts(result.get("siblings")):
        name, state = s.get("name"), s.get("state")
        if state == "restarted":
            lines.append(f"{name}: restarting")
        elif state == "stopped":
            lines.append(f"{name}: stopped, not started")
        else:
            lines.append(f"{name} didn't come back ({s.get('detail')})")
    return lines


__all__ = [
    "BACKUP_TIMEOUT_S",
    "CLI_TIMEOUT_S",
    "EXIT_KEPT",
    "EXIT_NOT_PAUSED",
    "PRINT_TIMEOUT_S",
    "KillSwitchFanOut",
    "StoreBackup",
    "backup_store",
    "backup_stores",
    "cli_argv",
    "kill_switch_fan_out",
    "pause_siblings",
    "print_argv",
    "restart_sibling",
    "resume_siblings",
    "sibling_lines",
]

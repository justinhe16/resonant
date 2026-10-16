"""Mac mini ops plumbing: the subprocess runner, the detached restart, and "back up".

Every ops subprocess is an argv list (never a shell), with a timeout, through an injectable
:class:`OpsRunner`, so tests use :class:`resonant.ops.fakes.FakeOpsRunner` and never run a
real command. stderr is captured but never logged or shown: it can echo paths and remotes.

**Restarting Resonant.** The daemon can't restart itself in-line: ``launchctl kickstart -k``
kills the process that would send the reply. So a restart (``mark_restart`` + a detached
helper) goes like this:

1. kv ``ops.restart_pending = {by, at, commit}`` is written.
2. ``restart_later_argv`` (``python -m resonant.ops.restart_later <label> 3``) is spawned
   in a new session, so it leaves launchd's process group and survives the kill. It
   sleeps 3 s, then runs ``launchctl kickstart -k gui/<uid>/<label>``.
3. The tool returns at once; the runner records and sends its reply well inside the delay.

On the next start ``back_up`` sends ``Back up (<commit>), down Ns`` to the owner (deduped
by ``restart:<at>``) and clears the marker, so it is sent once, and only after a restart
the owner asked for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from resonant.launchd import DAEMON_LABEL, OLLAMA_LABEL
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow

log = logging.getLogger(__name__)

LAUNCHCTL = "/bin/launchctl"
LABELS = frozenset({DAEMON_LABEL, OLLAMA_LABEL})  # the only services ops may restart
RESTART_DELAY_S = 3
KICKSTART_TIMEOUT_S = 15.0
RESTART_PENDING_KEY = "ops.restart_pending"

# git and uv never prompt: there is no terminal, and a prompt would only hang to the timeout.
_ENV = {
    # System dirs only: git and uv run by absolute path, and nothing they spawn is looked up
    # in a dir a Claude worker can write (docs/employee-plan.md D2)
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
}
# A marker older than this is stale (the kickstart never happened): no "back up" for it.
RESTART_MARKER_MAX_AGE = timedelta(minutes=10)


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


class Sender(Protocol):
    """The part of a ``Channel`` that "back up" needs."""

    async def send(self, principal: str, text: str, *, dedupe_key: str | None = None) -> object: ...


class OpsRunner(Protocol):
    async def run(
        self, argv: Sequence[str], *, timeout_s: float, cwd: Path | None = None
    ) -> ProcResult:
        """Run argv to completion. Raises ``TimeoutError`` past ``timeout_s`` (the process
        is killed) and ``OSError`` if it can't start."""
        ...

    def spawn_detached(self, argv: Sequence[str]) -> None:
        """Start argv in a new session and don't wait for it. Raises ``OSError``."""
        ...


class SubprocessRunner:
    """The real runner: ``asyncio.create_subprocess_exec`` and ``subprocess.Popen``."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self.env = {**os.environ, **_ENV, **(env or {})}

    async def run(
        self, argv: Sequence[str], *, timeout_s: float, cwd: Path | None = None
    ) -> ProcResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=self.env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, so a kill reaches its children
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except (TimeoutError, asyncio.CancelledError):
            # git spawns ssh / git-remote-https and uv spawns build backends: kill them all.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            raise
        return ProcResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
        )

    def spawn_detached(self, argv: Sequence[str]) -> None:
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv),
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # leave launchd's process group: survive the kill
            close_fds=True,
        )


def kickstart_argv(label: str, uid: int | None = None) -> list[str]:
    """``launchctl kickstart -k gui/<uid>/<label>`` for one of the two ops labels."""
    if label not in LABELS:
        raise ValueError(f"not an ops label: {label!r}")
    return [LAUNCHCTL, "kickstart", "-k", f"gui/{os.getuid() if uid is None else uid}/{label}"]


def restart_later_argv(label: str, delay_s: int = RESTART_DELAY_S) -> list[str]:
    """The detached helper that restarts ``label`` after ``delay_s`` seconds."""
    if label not in LABELS:
        raise ValueError(f"not an ops label: {label!r}")
    return [sys.executable, "-m", "resonant.ops.restart_later", label, str(delay_s)]


def mark_restart(
    conn: sqlite3.Connection,
    *,
    by: str,
    commit: str | None,
    now: datetime,
    note: str | None = None,
) -> dict[str, Any]:
    """Write the restart marker that makes the next start say "back up".

    ``note`` (code-built, e.g. ``Installed example``) is sent on its own line after it.
    """
    marker: dict[str, Any] = {"by": by, "at": iso(now), "commit": commit}
    if note:
        marker["note"] = note
    with atomic(conn):
        kv_set(conn, RESTART_PENDING_KEY, marker)
    return marker


def clear_restart(conn: sqlite3.Connection) -> None:
    with atomic(conn):
        conn.execute("DELETE FROM kv WHERE key = ?", (RESTART_PENDING_KEY,))


def restart_pending(conn: sqlite3.Connection) -> dict[str, Any] | None:
    value: object = kv_get(conn, RESTART_PENDING_KEY)
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def back_up_text(marker: Mapping[str, Any], now: datetime) -> str:
    """``Back up (abc1234), down 6s``. A missing commit or time is left out."""
    from resonant.router.render import duration

    commit = marker.get("commit")
    text = f"Back up ({commit})" if isinstance(commit, str) and commit else "Back up"
    at = marker.get("at")
    if isinstance(at, str):
        try:
            down = (now - datetime.fromisoformat(at)).total_seconds()
        except (TypeError, ValueError):
            down = None
        if down is not None:
            text += f", down {duration(down)}"
    note = marker.get("note")
    if isinstance(note, str) and note:
        text += f"\n{note}"
    return text


async def back_up(
    conn: sqlite3.Connection,
    channel: Sender,
    owner: str | None,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> bool:
    """On start: if a restart was requested, tell the owner it is back, once.

    Returns True if a message was sent (or had already been sent). A send failure keeps the
    marker, so the next start tries again; the dedupe key prevents a double send. A marker
    older than ``RESTART_MARKER_MAX_AGE`` is stale (the restart never happened) and is
    dropped without a message.
    """
    from resonant.gateway.channel import ChannelError, ChannelRefused

    marker = restart_pending(conn)
    if marker is None:
        return False
    by, at = marker.get("by"), marker.get("at")
    to = by if isinstance(by, str) and by else owner
    try:
        stale = not isinstance(at, str) or clock() - datetime.fromisoformat(at) > (
            RESTART_MARKER_MAX_AGE
        )
    except (TypeError, ValueError):
        stale = True
    if to is None or stale:
        clear_restart(conn)
        return False
    try:
        await channel.send(
            to, back_up_text(marker, clock()), dedupe_key=f"restart:{marker.get('at')}"
        )
    except ChannelRefused as e:
        log.warning("back-up message refused: %s", e)
        clear_restart(conn)
        return False
    except ChannelError as e:
        log.warning("back-up message failed (will retry on next start): %s", e)
        return False
    clear_restart(conn)
    return True


__all__ = [
    "KICKSTART_TIMEOUT_S",
    "LABELS",
    "LAUNCHCTL",
    "RESTART_DELAY_S",
    "RESTART_MARKER_MAX_AGE",
    "RESTART_PENDING_KEY",
    "OpsRunner",
    "ProcResult",
    "SubprocessRunner",
    "back_up",
    "back_up_text",
    "clear_restart",
    "kickstart_argv",
    "mark_restart",
    "restart_later_argv",
    "restart_pending",
]

"""Mac mini ops: ``restart_ollama`` (L3), ``restart_resonant`` (L2), ``update_resonant`` (L2),
``disk`` (L0) and ``recent_logs`` (L0).

Every subprocess is an argv list through the injectable ``OpsRunner`` (``resonant.ops``):
no shell, a timeout each, stderr never logged or returned. The restarts act only on the two
fixed launchd labels, and ``update_resonant`` only in ``settings.ops.repo_path``.

- ``restart_ollama``: ``launchctl kickstart -k gui/<uid>/com.resonant.ollama`` (15 s).
- ``restart_resonant``: writes the restart marker, spawns the detached helper that kickstarts
  the daemon after 3 s, and returns at once, so the runner's reply goes out first
  (``resonant.ops``). A replayed task returns the recorded result: no second restart.
- ``update_resonant``: refuses unless the repo is on ``main`` with a clean tree; then
  ``git pull --ff-only``. HEAD unchanged: ``Already up to date``, no restart. Otherwise
  ``uv sync --frozen``; a failed sync is reported and nothing restarts (the tree is at the
  new commit, the running process is unaffected). A good sync restarts as above.
- ``disk``: free space on ``/`` and ``RESONANT_HOME``.
- ``recent_logs``: the last ``n`` (≤ 20) lines of ``logs/resonant.jsonl``, one short line
  each, with URLs → ``<url>`` and emails and phone numbers → ``<handle>``, ≤ 300 chars.

None of these is exempt from dry_run: they act on the system, so under dry_run the gate
answers ``Dry run: would restart Ollama`` and the handler never runs.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from resonant.config import Settings
from resonant.executor import ToolFailed, ToolHandler
from resonant.gate.types import Intent
from resonant.launchd import DAEMON_LABEL, OLLAMA_LABEL
from resonant.ops import (
    KICKSTART_TIMEOUT_S,
    RESTART_DELAY_S,
    OpsRunner,
    ProcResult,
    SubprocessRunner,
    clear_restart,
    kickstart_argv,
    mark_restart,
    restart_later_argv,
)
from resonant.tools.base import NO_ARGS, BuiltinContext, no_args
from resonant.tools.system import disk_usage
from resonant_sdk import Effect, Level, ToolSpec

# SIP-protected; never looked up by name (a worker may write /opt/homebrew/bin)
GIT = "/usr/bin/git"
# `uv sync` on /update uses a cache under the Resonant home: Claude workers can't write it
# (unlike ~/.cache/uv), so nothing they plant there is copied into the daemon's venv.
UV_CACHE = "uv-cache"
GIT_TIMEOUT_S = 10.0  # local reads: branch, status, HEAD
PULL_TIMEOUT_S = 60.0
SYNC_TIMEOUT_S = 240.0
# Every step of an update, plus slack, so the executor's timeout never fires first.
UPDATE_TIMEOUT_S = 4 * GIT_TIMEOUT_S + PULL_TIMEOUT_S + SYNC_TIMEOUT_S + 20.0  # 360 s

LOG_FILE = "resonant.jsonl"
LOGS_DEFAULT = 10
LOGS_MAX = 20
LOG_LINE_MAX = 300
_TAIL_BYTES = 256 * 1024

RESTART_OLLAMA = ToolSpec(
    name="restart_ollama",
    description="Restart the local model server (Ollama).",
    args_schema=NO_ARGS,
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=KICKSTART_TIMEOUT_S + 5.0,
)
RESTART_RESONANT = ToolSpec(
    name="restart_resonant",
    description="Restart the Resonant daemon. It says it is back up when it returns.",
    args_schema=NO_ARGS,
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=GIT_TIMEOUT_S + 5.0,
)
UPDATE_RESONANT = ToolSpec(
    name="update_resonant",
    description=(
        "Update Resonant from GitHub (fast-forward main, sync dependencies) and restart. "
        "Refuses if the checkout has local changes or isn't on main."
    ),
    args_schema=NO_ARGS,
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=UPDATE_TIMEOUT_S,
)
DISK = ToolSpec(
    name="disk",
    description="Free disk space on the Mac mini.",
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)
RECENT_LOGS = ToolSpec(
    name="recent_logs",
    description=f"The last lines of Resonant's log (at most {LOGS_MAX}), URLs and handles removed.",
    args_schema={
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 1, "maximum": LOGS_MAX}},
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=5.0,
)

SPECS: tuple[ToolSpec, ...] = (RESTART_OLLAMA, RESTART_RESONANT, UPDATE_RESONANT, DISK, RECENT_LOGS)

# Code-built approval and dry-run headings (``restart Ollama?``), for ``gate.summary``.
SUMMARY_ACTIONS: dict[str, str] = {
    RESTART_OLLAMA.name: "restart Ollama",
    RESTART_RESONANT.name: "restart Resonant",
    UPDATE_RESONANT.name: "update Resonant",
}


def summary_action(intent: Intent) -> str | None:
    """The heading for a builtin ops tool (matched by registry identity), else None."""
    if intent.extension is not None:
        return None
    return SUMMARY_ACTIONS.get(intent.tool)


# --- restarts and update ---------------------------------------------------------------


class Ops:
    """The side-effecting ops, over an ``OpsRunner``. One per daemon."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        runner: OpsRunner,
        *,
        owner: str,
        clock: Callable[[], datetime],
        uid: int | None = None,
    ) -> None:
        self.conn = conn
        self.repo = settings.ops.repo_path
        self.uv = settings.ops.uv_path
        self.uv_cache = settings.home / UV_CACHE
        self.runner = runner
        self.owner = owner
        self.clock = clock
        self.uid = os.getuid() if uid is None else uid

    async def _run(self, what: str, argv: list[str], timeout_s: float) -> ProcResult:
        try:
            return await self.runner.run(argv, timeout_s=timeout_s, cwd=self.repo)
        except TimeoutError:
            raise ToolFailed(f"{what} timed out after {timeout_s:g}s") from None
        except OSError as e:
            raise ToolFailed(f"{what} couldn't start ({type(e).__name__})") from None

    async def _git(self, *args: str, timeout_s: float = GIT_TIMEOUT_S) -> str:
        res = await self._run(f"git {args[0]}", [GIT, *args], timeout_s)
        if res.returncode != 0:
            raise ToolFailed(f"git {args[0]} failed (exit {res.returncode})")
        return res.stdout.strip()

    async def _head(self) -> str:
        return await self._git("rev-parse", "--short=7", "HEAD")

    async def restart_ollama(self) -> dict[str, Any]:
        argv = kickstart_argv(OLLAMA_LABEL, self.uid)
        try:
            res = await self.runner.run(argv, timeout_s=KICKSTART_TIMEOUT_S)
        except TimeoutError:
            raise ToolFailed(f"launchctl timed out after {KICKSTART_TIMEOUT_S:g}s") from None
        except OSError as e:
            raise ToolFailed(f"launchctl couldn't start ({type(e).__name__})") from None
        if res.returncode != 0:
            raise ToolFailed(f"launchctl kickstart failed (exit {res.returncode})")
        return {"label": OLLAMA_LABEL, "restarted": True}

    def _restart_later(self, commit: str | None) -> None:
        """Mark the restart, then spawn the detached helper (the reply goes out first)."""
        mark_restart(self.conn, by=self.owner, commit=commit, now=self.clock())
        try:
            self.runner.spawn_detached(restart_later_argv(DAEMON_LABEL, RESTART_DELAY_S))
        except OSError as e:
            clear_restart(self.conn, planned=True)
            raise ToolFailed(f"couldn't start the restart helper ({type(e).__name__})") from None

    async def restart_resonant(self) -> dict[str, Any]:
        commit: str | None = None
        if self.repo is not None:
            try:
                commit = await self._head()
            except ToolFailed:
                commit = None  # the restart doesn't need it; "back up" just omits it
        self._restart_later(commit)
        return {
            "label": DAEMON_LABEL,
            "restarting": True,
            "delay_s": RESTART_DELAY_S,
            "commit": commit,
        }

    async def update_resonant(self) -> dict[str, Any]:
        if self.repo is None:
            raise ToolFailed("ops.repo_path isn't set; not updating")
        if self.uv is None:
            raise ToolFailed("ops.uv_path isn't set; not updating")
        if await self._git("rev-parse", "--abbrev-ref", "HEAD") != "main":
            raise ToolFailed("not on main; not updating")
        if await self._git("status", "--porcelain"):
            raise ToolFailed("working tree has changes; not updating")
        old = await self._head()
        pull = await self._run("git pull", [GIT, "pull", "--ff-only"], PULL_TIMEOUT_S)
        if pull.returncode != 0:
            raise ToolFailed(f"git pull --ff-only failed (exit {pull.returncode}); not updated")
        new = await self._head()
        out: dict[str, Any] = {"old": old, "new": new, "updated": new != old}
        if new == old:
            return {**out, "synced": None, "restarting": False}
        try:
            sync = await self.runner.run(
                [str(self.uv), "sync", "--frozen", "--cache-dir", str(self.uv_cache)],
                timeout_s=SYNC_TIMEOUT_S,
                cwd=self.repo,
            )
            sync_error = None if sync.returncode == 0 else f"exit {sync.returncode}"
        except TimeoutError:
            sync_error = f"timed out after {SYNC_TIMEOUT_S:g}s"
        except OSError as e:
            sync_error = f"couldn't start ({type(e).__name__})"
        if sync_error is not None:
            # The tree is at the new commit; the running process is untouched. No restart.
            return {**out, "synced": False, "sync_error": sync_error, "restarting": False}
        self._restart_later(new)
        return {**out, "synced": True, "restarting": True, "delay_s": RESTART_DELAY_S}


# --- reads -------------------------------------------------------------------------------


def disk(home: str) -> dict[str, Any]:
    disks = [disk_usage("/")]
    if home != "/":
        disks.append(disk_usage(home))
    return {"disk": disks}


_URL = re.compile(r"\b(?:https?|ftp|file|wss?)://\S+|\bwww\.\S+", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# E.164 (``+15551234567``) and North American formats (``(555) 123-4567``, ``555.123.4567``).
_PHONE = re.compile(r"\+\d[\d\s().-]{6,}\d|\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")
_SPACE = re.compile(r"\s+")


def scrub(text: str) -> str:
    """URLs → ``<url>``; emails and phone numbers → ``<handle>``; whitespace collapsed."""
    text = _URL.sub("<url>", text)
    text = _EMAIL.sub("<handle>", text)
    text = _PHONE.sub("<handle>", text)
    return _SPACE.sub(" ", text).strip()


def _clip(text: str) -> str:
    return text if len(text) <= LOG_LINE_MAX else text[: LOG_LINE_MAX - 1] + "…"


def log_line(raw: str) -> str:
    """One log record as ``15:04:05 warning resonant.daemon: event k=v``, scrubbed, clipped."""
    try:
        rec: object = json.loads(raw)
    except ValueError:
        rec = None
    if not isinstance(rec, dict):
        return _clip(scrub(raw))
    d = cast(dict[str, Any], rec)
    ts = str(d.pop("timestamp", ""))
    clock = ts[11:19] if len(ts) >= 19 else ts
    level = str(d.pop("level", ""))
    logger = str(d.pop("logger", ""))
    event = str(d.pop("event", ""))
    head = " ".join(p for p in (clock, level, f"{logger}:" if logger else "") if p)
    rest = " ".join(
        f"{k}={v if isinstance(v, str) else json.dumps(v, default=str)}" for k, v in d.items()
    )
    return _clip(scrub(" ".join(p for p in (head, event, rest) if p)))


def tail_lines(path: Path, n: int) -> list[str]:
    """The last ``n`` non-empty lines of ``path`` (reads at most the last 256 KB)."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            data = f.read()
    except FileNotFoundError:
        return []
    lines = data.decode(errors="replace").splitlines()
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]  # the first line is probably cut
    return [ln for ln in lines if ln.strip()][-n:]


def recent_logs(logs_dir: Path, args: dict[str, Any]) -> dict[str, Any]:
    unknown = set(args) - {"n"}
    if unknown:
        raise ToolFailed(f"recent_logs: unknown arguments: {sorted(unknown)}")
    n = args.get("n", LOGS_DEFAULT)
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ToolFailed("recent_logs: n must be a positive integer")
    n = min(n, LOGS_MAX)
    try:
        lines = [log_line(raw) for raw in tail_lines(logs_dir / LOG_FILE, n)]
    except OSError as e:
        raise ToolFailed(f"recent_logs: can't read the log ({type(e).__name__})") from None
    return {"lines": lines, "count": len(lines), "n": n}


# --- registration ------------------------------------------------------------------------


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    settings = ctx.state.settings
    ops = Ops(
        ctx.conn,
        settings,
        ctx.ops_runner if ctx.ops_runner is not None else SubprocessRunner(),
        owner=ctx.principals.owner,
        clock=ctx.clock,
    )
    home, logs_dir = str(settings.home), settings.logs_dir

    async def _restart_ollama(args: dict[str, Any]) -> dict[str, Any]:
        no_args(RESTART_OLLAMA.name, args)
        return await ops.restart_ollama()

    async def _restart_resonant(args: dict[str, Any]) -> dict[str, Any]:
        no_args(RESTART_RESONANT.name, args)
        return await ops.restart_resonant()

    async def _update_resonant(args: dict[str, Any]) -> dict[str, Any]:
        no_args(UPDATE_RESONANT.name, args)
        return await ops.update_resonant()

    async def _disk(args: dict[str, Any]) -> dict[str, Any]:
        no_args(DISK.name, args)
        return disk(home)

    async def _recent_logs(args: dict[str, Any]) -> dict[str, Any]:
        return recent_logs(logs_dir, args)

    return {
        RESTART_OLLAMA.name: _restart_ollama,
        RESTART_RESONANT.name: _restart_resonant,
        UPDATE_RESONANT.name: _update_resonant,
        DISK.name: _disk,
        RECENT_LOGS.name: _recent_logs,
    }

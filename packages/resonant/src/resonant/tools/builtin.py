"""Built-in read tools: questions about Resonant itself and the machine it runs on.

Every builtin is ``effect=read`` with ``extension=None`` (so global scope, owner-only by
default). Handlers are async ``(args) -> dict``, return JSON-serializable dicts, and are
fast: database reads are indexed and capped, and ``system_status`` spends at most ~500ms
on the macOS memory-pressure probe. "No data" is an empty result, never an exception.
Bad arguments raise ``ToolFailed`` (a definite failure the caller can report back).

No tool returns message bodies, task checkpoints/results, or file contents.
Output shapes are documented in ``docs/interfaces.md`` ("Built-in tools").
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sqlite3
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast, get_args

import psutil

from resonant.executor import HandlerKey, ToolFailed, ToolHandler
from resonant.loop.types import TaskStatus
from resonant.status import collect_status
from resonant.store import tasks as repo
from resonant.store.db import kv_get, utcnow
from resonant.tools.registry import ToolRegistry
from resonant_sdk import Effect, ToolSpec

if TYPE_CHECKING:
    from resonant.api import DaemonState

LIST_TASKS_MAX = 50
LIST_TASKS_DEFAULT = 10
MEMORY_PRESSURE_TIMEOUT_S = 0.5
MODEL_PROBE_KEY = "model.probe"

_STATUSES: tuple[str, ...] = get_args(TaskStatus)
_NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
# kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical.
_PRESSURE_LEVELS = {"1": "normal", "2": "warn", "4": "critical"}
_FREE_PCT = re.compile(r"free percentage:\s*(\d+)%")

LIST_TASKS = ToolSpec(
    name="list_tasks",
    description=(
        "List recent tasks (newest first) with id, kind, status, wait reason and age. "
        "Optionally filter by status."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(_STATUSES)},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": LIST_TASKS_MAX,
                "default": LIST_TASKS_DEFAULT,
            },
        },
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=5.0,
)
TASK_COUNTS = ToolSpec(
    name="task_counts",
    description="Count tasks by status, and waiting tasks by wait reason.",
    args_schema=_NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)
SYSTEM_STATUS = ToolSpec(
    name="system_status",
    description=(
        "Machine health: CPU, load average, memory, swap, memory pressure, free disk and uptime."
    ),
    args_schema=_NO_ARGS,
    effect=Effect.READ,
    timeout_s=2.0,
)
DAEMON_STATUS = ToolSpec(
    name="daemon_status",
    description=(
        "Resonant daemon status: uptime, last loop tick, in-flight tasks, dry_run, "
        "autonomy and paused."
    ),
    args_schema=_NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)
MODEL_STATUS = ToolSpec(
    name="model_status",
    description="Last cached health probe of the local model (no live model call).",
    args_schema=_NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)

BUILTIN_SPECS: tuple[ToolSpec, ...] = (
    LIST_TASKS,
    TASK_COUNTS,
    SYSTEM_STATUS,
    DAEMON_STATUS,
    MODEL_STATUS,
)


def _no_args(name: str, args: dict[str, Any]) -> None:
    if args:
        raise ToolFailed(f"{name} takes no arguments (got {sorted(args)})")


def _age_s(created_at: str, now: datetime) -> int:
    return max(0, round((now - datetime.fromisoformat(created_at)).total_seconds()))


def list_tasks(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    unknown = set(args) - {"status", "limit"}
    if unknown:
        raise ToolFailed(f"unknown arguments: {sorted(unknown)}")
    status = args.get("status")
    if status is not None and status not in _STATUSES:
        raise ToolFailed(f"status must be one of {list(_STATUSES)}")
    limit = args.get("limit", LIST_TASKS_DEFAULT)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ToolFailed("limit must be an integer")
    limit = max(1, min(limit, LIST_TASKS_MAX))
    # Task ids are ULIDs, so ordering by the primary key is newest-first and indexed.
    if status is None:
        rows = conn.execute(
            "SELECT id, kind, status, wait_reason, created_at FROM tasks ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, kind, status, wait_reason, created_at FROM tasks
               WHERE status = ? ORDER BY id DESC LIMIT ?""",
            (status, limit),
        ).fetchall()
    now = utcnow()
    tasks = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "status": r["status"],
            "wait_reason": r["wait_reason"],
            "age_s": _age_s(r["created_at"], now),
        }
        for r in rows
    ]
    return {"tasks": tasks, "count": len(tasks), "limit": limit, "status": status}


def task_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    by_status = repo.count_by_status(conn)
    return {
        "by_status": by_status,
        "waiting_by_reason": repo.count_waiting_by_reason(conn),
        "total": sum(by_status.values()),
    }


async def _run(argv: list[str], timeout_s: float) -> str | None:
    """stdout of a short command, or None if it is missing, fails, or is too slow."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return None
    return out.decode(errors="replace") if proc.returncode == 0 else None


async def memory_pressure(timeout_s: float = MEMORY_PRESSURE_TIMEOUT_S) -> dict[str, Any]:
    """macOS memory pressure, best effort. ``level`` is "unknown" off macOS or on timeout."""
    level_out, free_out = await asyncio.gather(
        _run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"], timeout_s),
        _run(["/usr/bin/memory_pressure", "-Q"], timeout_s),
    )
    level = _PRESSURE_LEVELS.get((level_out or "").strip(), "unknown")
    match = _FREE_PCT.search(free_out or "")
    return {"level": level, "free_pct": int(match.group(1)) if match else None}


def _disk(path: str) -> dict[str, Any]:
    try:
        u = psutil.disk_usage(path)
    except OSError:
        return {"path": path, "free_bytes": None, "total_bytes": None, "used_pct": None}
    return {"path": path, "free_bytes": u.free, "total_bytes": u.total, "used_pct": u.percent}


async def system_status(home: str) -> dict[str, Any]:
    pressure = await memory_pressure()
    try:
        load: list[float] | None = [round(x, 2) for x in psutil.getloadavg()]
    except OSError:
        load = None
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disks = [_disk("/")]
    if home != "/":
        disks.append(_disk(home))
    return {
        # Since the previous call (primed at registration), so it never blocks.
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count(),
        "load_avg": load,
        "memory": {"used_bytes": vm.used, "total_bytes": vm.total, "percent": vm.percent},
        "swap": {"used_bytes": swap.used, "total_bytes": swap.total, "percent": swap.percent},
        "memory_pressure": pressure,
        "disk": disks,
        "uptime_s": max(0, round(time.time() - psutil.boot_time())),
    }


def daemon_status(state: DaemonState) -> dict[str, Any]:
    s = collect_status(
        state.conn,
        state.settings,
        started_at=state.started_at,
        last_tick=state.loop.last_tick,
        inflight=state.loop.inflight,
    )
    keys = ("version", "uptime_s", "last_tick", "inflight", "dry_run", "autonomy", "paused")
    return {k: s[k] for k in keys}


def model_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """The cached probe written by the model client / health check. Never calls the model."""
    probe: object = kv_get(conn, MODEL_PROBE_KEY)
    if not isinstance(probe, dict):
        return {"known": False}
    # kv values are JSON, so the keys are already strings.
    return {**cast(dict[str, Any], probe), "known": True}


def register_builtins(
    registry: ToolRegistry, conn: sqlite3.Connection, state: DaemonState
) -> dict[HandlerKey, ToolHandler]:
    """Register the builtin specs and return their handlers, keyed ``(None, name)``."""
    psutil.cpu_percent(interval=None)  # prime: the first non-blocking reading is meaningless
    home = str(state.settings.home)

    async def _list_tasks(args: dict[str, Any]) -> dict[str, Any]:
        return list_tasks(conn, args)

    async def _task_counts(args: dict[str, Any]) -> dict[str, Any]:
        _no_args(TASK_COUNTS.name, args)
        return task_counts(conn)

    async def _system_status(args: dict[str, Any]) -> dict[str, Any]:
        _no_args(SYSTEM_STATUS.name, args)
        return await system_status(home)

    async def _daemon_status(args: dict[str, Any]) -> dict[str, Any]:
        _no_args(DAEMON_STATUS.name, args)
        return daemon_status(state)

    async def _model_status(args: dict[str, Any]) -> dict[str, Any]:
        _no_args(MODEL_STATUS.name, args)
        return model_status(conn)

    handlers: dict[HandlerKey, ToolHandler] = {
        (None, LIST_TASKS.name): _list_tasks,
        (None, TASK_COUNTS.name): _task_counts,
        (None, SYSTEM_STATUS.name): _system_status,
        (None, DAEMON_STATUS.name): _daemon_status,
        (None, MODEL_STATUS.name): _model_status,
    }
    for spec in BUILTIN_SPECS:
        registry.register(spec)
    return handlers

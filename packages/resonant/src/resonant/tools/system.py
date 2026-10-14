"""Machine and daemon reads: ``system_status``, ``daemon_status`` and ``model_status``.

``system_status`` spends at most ~500ms on the macOS memory-pressure probe.
``model_status`` reads the cached probe only; it never calls the model.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Any, cast

import psutil

from resonant.executor import ToolHandler
from resonant.status import collect_status
from resonant.store.db import kv_get
from resonant.tools.base import NO_ARGS, BuiltinContext, no_args
from resonant_sdk import Effect, ToolSpec

if TYPE_CHECKING:
    from resonant.api import DaemonState

MEMORY_PRESSURE_TIMEOUT_S = 0.5
MODEL_PROBE_KEY = "model.probe"

# kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical.
_PRESSURE_LEVELS = {"1": "normal", "2": "warn", "4": "critical"}
_FREE_PCT = re.compile(r"free percentage:\s*(\d+)%")

SYSTEM_STATUS = ToolSpec(
    name="system_status",
    description=(
        "Machine health: CPU, load average, memory, swap, memory pressure, free disk and uptime."
    ),
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=2.0,
)
DAEMON_STATUS = ToolSpec(
    name="daemon_status",
    description=(
        "Resonant daemon status: uptime, last loop tick, in-flight tasks, dry_run, "
        "autonomy and paused."
    ),
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)
MODEL_STATUS = ToolSpec(
    name="model_status",
    description="Last cached health probe of the local model (no live model call).",
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)

SPECS: tuple[ToolSpec, ...] = (SYSTEM_STATUS, DAEMON_STATUS, MODEL_STATUS)


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


def disk_usage(path: str) -> dict[str, Any]:
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
    disks = [disk_usage("/")]
    if home != "/":
        disks.append(disk_usage(home))
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


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    psutil.cpu_percent(interval=None)  # prime: the first non-blocking reading is meaningless
    conn, state = ctx.conn, ctx.state
    home = str(state.settings.home)

    async def _system_status(args: dict[str, Any]) -> dict[str, Any]:
        no_args(SYSTEM_STATUS.name, args)
        return await system_status(home)

    async def _daemon_status(args: dict[str, Any]) -> dict[str, Any]:
        no_args(DAEMON_STATUS.name, args)
        return daemon_status(state)

    async def _model_status(args: dict[str, Any]) -> dict[str, Any]:
        no_args(MODEL_STATUS.name, args)
        return model_status(conn)

    return {
        SYSTEM_STATUS.name: _system_status,
        DAEMON_STATUS.name: _daemon_status,
        MODEL_STATUS.name: _model_status,
    }

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from resonant.api import DaemonState, create_app
from resonant.components import Components, build_components
from resonant.config import Settings, load_settings
from resonant.daemon import Daemon
from resonant.executor import NotAllowedError, ToolFailed
from resonant.gate import Allow, Deny, Intent
from resonant.loop import Loop, NewTask, SlotPool
from resonant.store.db import kv_set, utcnow
from resonant.store.tasks import insert_task, set_status
from resonant.tools import ToolRegistry, builtin
from resonant_sdk import Effect, Level

REPO = Path(__file__).resolve().parents[1]
NAMES = {"list_tasks", "task_counts", "system_status", "daemon_status", "model_status"}


@pytest.fixture
def settings() -> Settings:
    return load_settings()


@pytest.fixture
def state(db: sqlite3.Connection, settings: Settings) -> DaemonState:
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    return DaemonState(settings, db, loop, started_at=utcnow())


@pytest.fixture
def components(settings: Settings, db: sqlite3.Connection, state: DaemonState) -> Components:
    state.components = build_components(settings, db, state)
    return state.components


@pytest.fixture
def with_principals(settings: Settings) -> Iterator[None]:
    shutil.copy(REPO / "config/principals.example.yaml", settings.principals_path)
    yield


async def call(components: Components, name: str, args: dict[str, Any] | None = None) -> Any:
    data = await components.handlers[(None, name)](args or {})
    json.dumps(data, allow_nan=False)  # every result must be JSON-serializable
    return data


def seed(db: sqlite3.Connection) -> dict[str, str]:
    ids = {name: insert_task(db, NewTask(kind=name, runner="echo", priority=1)) for name in "abcd"}
    set_status(db, ids["b"], "running")
    set_status(db, ids["c"], "waiting", wait_reason="approval")
    set_status(db, ids["d"], "done")
    return ids


def test_registers_five_global_reads(components: Components) -> None:
    specs = components.registry.specs()
    assert {s.name for s in specs} == NAMES
    assert all(s.extension is None and s.source == "builtin" for s in specs)
    assert all(s.effect is Effect.READ and s.effective_level is Level.L0 for s in specs)
    assert set(components.handlers) == {(None, n) for n in NAMES}


def test_double_registration_rejected(db: sqlite3.Connection, state: DaemonState) -> None:
    registry = ToolRegistry()
    builtin.register_builtins(registry, db, state)
    with pytest.raises(ValueError, match="already registered"):
        builtin.register_builtins(registry, db, state)


async def test_list_tasks(db: sqlite3.Connection, components: Components) -> None:
    ids = seed(db)
    out = await call(components, "list_tasks")
    assert out["count"] == 4 and out["limit"] == builtin.LIST_TASKS_DEFAULT
    assert {t["id"] for t in out["tasks"]} == set(ids.values())
    row = next(t for t in out["tasks"] if t["id"] == ids["c"])
    assert row == {
        "id": ids["c"],
        "kind": "c",
        "status": "waiting",
        "wait_reason": "approval",
        "age_s": row["age_s"],
    }
    assert row["age_s"] >= 0
    waiting = await call(components, "list_tasks", {"status": "waiting"})
    assert [t["id"] for t in waiting["tasks"]] == [ids["c"]] and waiting["status"] == "waiting"
    assert (await call(components, "list_tasks", {"status": "failed"}))["tasks"] == []


async def test_list_tasks_newest_first(db: sqlite3.Connection, components: Components) -> None:
    first = insert_task(db, NewTask(kind="k", runner="echo", priority=1))
    await asyncio.sleep(0.003)  # ULIDs order by millisecond
    second = insert_task(db, NewTask(kind="k", runner="echo", priority=1))
    out = await call(components, "list_tasks")
    assert [t["id"] for t in out["tasks"]] == [second, first]


async def test_list_tasks_limit_capped(db: sqlite3.Connection, components: Components) -> None:
    for _ in range(60):
        insert_task(db, NewTask(kind="k", runner="echo", priority=1))
    assert (await call(components, "list_tasks", {"limit": 500}))["count"] == 50
    assert (await call(components, "list_tasks", {"limit": 0}))["count"] == 1
    assert (await call(components, "list_tasks", {"limit": 3}))["limit"] == 3


@pytest.mark.parametrize(
    "args",
    [{"status": "bogus"}, {"limit": "5"}, {"limit": True}, {"limit": 1.5}, {"extra": 1}],
)
async def test_list_tasks_bad_args(components: Components, args: dict[str, Any]) -> None:
    with pytest.raises(ToolFailed):
        await call(components, "list_tasks", args)


async def test_no_arg_tools_reject_args(components: Components) -> None:
    for name in NAMES - {"list_tasks"}:
        with pytest.raises(ToolFailed):
            await call(components, name, {"x": 1})


async def test_list_tasks_never_returns_content(
    db: sqlite3.Connection, components: Components
) -> None:
    insert_task(
        db,
        NewTask(kind="k", runner="echo", priority=1, checkpoint={"body": "secret message"}),
    )
    out = await call(components, "list_tasks")
    assert "secret message" not in json.dumps(out)


async def test_task_counts(db: sqlite3.Connection, components: Components) -> None:
    assert await call(components, "task_counts") == {
        "by_status": {},
        "waiting_by_reason": {},
        "total": 0,
    }
    seed(db)
    assert await call(components, "task_counts") == {
        "by_status": {"queued": 1, "running": 1, "waiting": 1, "done": 1},
        "waiting_by_reason": {"approval": 1},
        "total": 4,
    }


async def test_daemon_status(db: sqlite3.Connection, components: Components) -> None:
    kv_set(db, "paused", True)
    out = await call(components, "daemon_status")
    assert set(out) == {
        "version",
        "uptime_s",
        "last_tick",
        "inflight",
        "dry_run",
        "autonomy",
        "paused",
    }
    assert out["dry_run"] is True and out["paused"] is True and out["autonomy"] == "on"
    assert out["inflight"] == 0 and out["last_tick"] is None and out["uptime_s"] >= 0


async def test_model_status(db: sqlite3.Connection, components: Components) -> None:
    assert await call(components, "model_status") == {"known": False}
    kv_set(db, "model.probe", "not a dict")
    assert await call(components, "model_status") == {"known": False}
    probe = {"ok": True, "model": "qwen3:30b-a3b", "checked_at": "2026-10-08T00:00:00+00:00"}
    kv_set(db, "model.probe", probe)
    assert await call(components, "model_status") == {**probe, "known": True}


def fake_psutil(monkeypatch: pytest.MonkeyPatch, *, loadavg_fails: bool = False) -> None:
    def getloadavg() -> tuple[float, float, float]:
        if loadavg_fails:
            raise OSError("no loadavg")
        return (1.234, 0.5, 0.25)

    def disk_usage(path: str) -> SimpleNamespace:
        if path == "/missing":
            raise FileNotFoundError(path)
        return SimpleNamespace(free=10, total=100, percent=90.0)

    fake = SimpleNamespace(
        cpu_percent=lambda interval=None: 12.5,  # pyright: ignore[reportUnknownLambdaType]
        cpu_count=lambda: 8,
        getloadavg=getloadavg,
        virtual_memory=lambda: SimpleNamespace(used=4, total=16, percent=25.0),
        swap_memory=lambda: SimpleNamespace(used=0, total=2, percent=0.0),
        disk_usage=disk_usage,
        boot_time=lambda: time.time() - 3600,
    )
    monkeypatch.setattr(builtin, "psutil", fake)


async def test_system_status_shape(
    monkeypatch: pytest.MonkeyPatch, components: Components, settings: Settings
) -> None:
    fake_psutil(monkeypatch)

    async def run(argv: list[str], timeout_s: float) -> str | None:
        if "sysctl" in argv[0]:
            return "1\n"
        return "System-wide memory free percentage: 77%\n"

    monkeypatch.setattr(builtin, "_run", run)
    out = await call(components, "system_status")
    assert out["cpu_percent"] == 12.5 and out["cpu_count"] == 8
    assert out["load_avg"] == [1.23, 0.5, 0.25]
    assert out["memory"] == {"used_bytes": 4, "total_bytes": 16, "percent": 25.0}
    assert out["swap"] == {"used_bytes": 0, "total_bytes": 2, "percent": 0.0}
    assert out["memory_pressure"] == {"level": "normal", "free_pct": 77}
    assert [d["path"] for d in out["disk"]] == ["/", str(settings.home)]
    assert out["disk"][0] == {"path": "/", "free_bytes": 10, "total_bytes": 100, "used_pct": 90.0}
    assert 3590 <= out["uptime_s"] <= 3610


async def test_system_status_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux CI: no memory_pressure, no sysctl key, a missing path."""
    fake_psutil(monkeypatch, loadavg_fails=True)

    async def run(argv: list[str], timeout_s: float) -> str | None:
        return None

    monkeypatch.setattr(builtin, "_run", run)
    out = await builtin.system_status("/missing")
    assert out["memory_pressure"] == {"level": "unknown", "free_pct": None}
    assert out["load_avg"] is None
    assert out["disk"][1] == {
        "path": "/missing",
        "free_bytes": None,
        "total_bytes": None,
        "used_pct": None,
    }


async def test_run_missing_or_slow_command() -> None:
    assert await builtin._run(["/nonexistent/memory_pressure", "-Q"], 0.5) is None  # pyright: ignore[reportPrivateUsage]
    start = time.monotonic()
    assert await builtin._run(["sleep", "5"], 0.05) is None  # pyright: ignore[reportPrivateUsage]
    assert time.monotonic() - start < 1
    assert await builtin._run(["false"], 0.5) is None  # pyright: ignore[reportPrivateUsage]
    assert await builtin._run(["echo", "hi"], 0.5) == "hi\n"  # pyright: ignore[reportPrivateUsage]


async def test_system_status_real(components: Components) -> None:
    """Unpatched: works here (macOS or Linux) and stays within its budget."""
    start = time.monotonic()
    out = await call(components, "system_status")
    assert time.monotonic() - start < 1.0
    assert out["memory_pressure"]["level"] in {"normal", "warn", "critical", "unknown"}
    assert out["memory"]["total_bytes"] > 0


def test_missing_principals_denies_channels(
    settings: Settings,
    db: sqlite3.Connection,
    state: DaemonState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="resonant.components"):
        comps = build_components(settings, db, state)
    assert "denying all channel principals" in caplog.text
    assert not comps.principals_loaded
    assert comps.principals.resolve("imessage:+15550000000") is None
    task_id = insert_task(db, NewTask(kind="k", runner="local", priority=1))
    intent = Intent.for_tool(builtin.TASK_COUNTS, task_id=task_id, principal="someone", args={})
    assert isinstance(comps.gate.evaluate(intent), Deny)


def test_invalid_principals_is_an_error(
    settings: Settings, db: sqlite3.Connection, state: DaemonState
) -> None:
    settings.principals_path.write_text("principals: {a: {role: member}}\n")
    with pytest.raises(ValueError, match="exactly one owner"):
        build_components(settings, db, state)


@pytest.mark.usefixtures("with_principals")
async def test_gate_executor_path(db: sqlite3.Connection, components: Components) -> None:
    assert components.principals_loaded
    owner = components.principals.resolve("imessage:+15550000000")
    assert owner == "owner"
    seed(db)
    task_id = insert_task(db, NewTask(kind="k", runner="local", priority=1))
    intent = Intent.for_tool(builtin.TASK_COUNTS, task_id=task_id, principal=owner, args={})
    decision = components.gate.evaluate(intent)
    assert isinstance(decision, Allow)
    result = await components.executor.execute(intent, decision, step_no=1)
    assert result.ok and result.data is not None and result.data["total"] == 5
    with pytest.raises(NotAllowedError):  # the token was consumed
        await components.executor.execute(intent, decision, step_no=1, call_index=1)

    listed = Intent.for_tool(
        builtin.LIST_TASKS, task_id=task_id, principal=owner, args={"status": "waiting"}
    )
    out = await components.executor.execute(listed, components.gate.evaluate(listed), step_no=2)
    assert out.ok and out.data is not None and out.data["count"] == 1

    bad = Intent.for_tool(builtin.LIST_TASKS, task_id=task_id, principal=owner, args={"x": 1})
    failed = await components.executor.execute(bad, components.gate.evaluate(bad), step_no=3)
    assert not failed.ok and not failed.ambiguous and "unknown arguments" in (failed.error or "")


@pytest.mark.usefixtures("with_principals")
def test_member_denied_global_builtins(db: sqlite3.Connection, components: Components) -> None:
    member = components.principals.resolve("slack:U11111111")
    assert member == "cofounder"
    task_id = insert_task(db, NewTask(kind="k", runner="local", priority=1))
    for spec in builtin.BUILTIN_SPECS:
        intent = Intent.for_tool(spec, task_id=task_id, principal=member, args={})
        assert isinstance(components.gate.evaluate(intent), Deny)


async def test_api_tools(state: DaemonState, components: Components) -> None:
    transport = httpx.ASGITransport(app=create_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        tools = (await client.get("/api/tools")).json()
    assert {t["name"] for t in tools} == NAMES
    lt = next(t for t in tools if t["name"] == "list_tasks")
    assert lt == {
        "name": "list_tasks",
        "extension": None,
        "effect": "read",
        "level": "L0",
        "description": builtin.LIST_TASKS.description,
        "args_schema": builtin.LIST_TASKS.args_schema,
    }


async def test_api_tools_without_components(state: DaemonState) -> None:
    transport = httpx.ASGITransport(app=create_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.get("/api/tools")).json() == []


async def test_daemon_wires_components(settings: Settings) -> None:
    daemon = Daemon(settings)
    try:
        comps = daemon.state.components
        assert comps is not None and not comps.principals_loaded
        assert {s.name for s in comps.registry.specs()} == NAMES
        assert comps.gate.registry is comps.registry
    finally:
        daemon.conn.close()

"""Phase 2 acceptance (docs/phase2-plan.md §1): the whole daemon, in process, with fakes.

chat.db is a FakeChatDB, osascript is a RecordingOsaRunner and the model is a ScriptedModel,
as in ``test_e2e.py``. Each Phase 2 ticket adds its own scenarios (E1-E15) here.

Until the local runner's ``direct_call`` lands (P2-11), tool calls are driven by
``ScriptRunner``: a test runner that sends each call in its checkpoint through the daemon's
own gate and executor inside a loop step, the way a runner will.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from resonant.components import Components, build_components
from resonant.config import load_settings
from resonant.daemon import Daemon
from resonant.e2e import RecordingOsaRunner, ScriptedModel, wait_until
from resonant.gate import Allow, DryRun, Intent, NeedsApproval, PolicyGate
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop import NewTask
from resonant.loop.types import PRIORITY_OWNER, Done, StepContext, StepOutcome, Task, Wait
from resonant.runners.reminder import ReminderRunner
from resonant.store.db import iso, kv_set, utcnow
from resonant_sdk import Effect, Event, Level, ToolSpec

OWNER_PHONE = "+15551234567"
SELF = "resonant.agent@icloud.com"
PRINCIPALS = f"""\
principals:
  justin:
    role: owner
    identities: ["imessage:{OWNER_PHONE}"]
"""

# A non-exempt side effect standing in for the ops tools until P2-14 registers them.
FAKE_OPS = ToolSpec(
    name="fake_restart",
    description="Restart something (test stand-in for an ops tool).",
    args_schema={"type": "object", "properties": {}, "additionalProperties": False},
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=5.0,
)


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class ScriptRunner:
    """``runner="script"``: runs ``checkpoint["calls"]`` (``{tool, args, origin?}``) through
    the gate and executor in one step. Records each decision and result in ``results``.
    Like the local runner will (D3), it waits for an approval it was asked for, so the
    approval stays pending; otherwise it is done."""

    components: Components
    name: str = "script"
    uses_claude_slot: bool = False
    max_step_s: float | None = 30.0
    decisions: list[Any] = field(default_factory=list[Any])
    results: dict[str, list[dict[str, Any]]] = field(default_factory=dict[str, Any])

    async def step(self, task: Task, events: list[Event], ctx: StepContext) -> StepOutcome:
        out: list[dict[str, Any]] = []
        asks: list[NeedsApproval] = []
        for i, call in enumerate(task.checkpoint["calls"]):
            spec = self.components.registry.get(None, call["tool"])
            assert spec is not None
            assert task.principal is not None
            intent = Intent.for_tool(
                spec,
                task_id=task.id,
                principal=task.principal,
                args=call.get("args", {}),
                origin=call.get("origin", "inferred"),
            )
            decision = self.components.gate.evaluate(intent)
            self.decisions.append(decision)
            if isinstance(decision, NeedsApproval):
                asks.append(decision)
            row: dict[str, Any] = {"tool": spec.name, "decision": type(decision).__name__}
            if isinstance(decision, Allow):
                res = await self.components.executor.execute(
                    intent, decision, step_no=task.step_no, call_index=i
                )
                row |= {"ok": res.ok, "data": res.data}
            out.append(row)
        self.results[task.id] = out
        if asks:
            return Wait("approval", task.checkpoint, wake_at=asks[0].expires_at)
        return Done({"calls": out})


@dataclass
class Running:
    daemon: Daemon
    osa: RecordingOsaRunner
    chat: FakeChatDB
    port: int
    script: ScriptRunner
    fake_ops_calls: list[dict[str, Any]]

    @property
    def conn(self) -> sqlite3.Connection:
        return self.daemon.conn

    @property
    def components(self) -> Components:
        assert self.daemon.state.components is not None
        return self.daemon.state.components

    def rows(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, args).fetchall())

    def audits(self, action: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.rows("SELECT * FROM audit_log WHERE action = ? ORDER BY id", action):
            d = dict(r)
            d["detail"] = json.loads(d["detail"] or "{}")
            out.append(d)
        return out

    async def run_script(self, *calls: dict[str, Any]) -> list[dict[str, Any]]:
        """Run ``calls`` in one ``runner="script"`` step; return each call's outcome."""
        task_id = self.daemon.loop.create_task(
            NewTask(
                kind="script",
                runner="script",
                priority=PRIORITY_OWNER,
                principal="justin",
                checkpoint={"calls": list(calls)},
            )
        )

        def finished() -> bool:
            (row,) = self.rows("SELECT status FROM tasks WHERE id = ?", task_id)
            return row["status"] in ("done", "failed", "waiting")

        await wait_until(finished, 10)
        return self.script.results[task_id]

    async def status(self) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{self.port}/api/status")
        return r.json()


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[FakeChatDB]:
    db = FakeChatDB(tmp_path / "chat.db")
    yield db
    db.close()


@pytest.fixture
async def running(
    resonant_home: Path, chat: FakeChatDB, caplog: pytest.LogCaptureFixture
) -> AsyncIterator[Running]:
    """The daemon with iMessage on and the default ``dry_run: true``."""
    (resonant_home / "principals.yaml").write_text(PRINCIPALS)
    port = free_port()
    settings = load_settings(
        api={"port": port},
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
    )
    osa = RecordingOsaRunner()
    daemon = Daemon(settings, model=ScriptedModel(), osa_runner=osa)
    kv_set(daemon.conn, "imessage.selftest", {"ok": True, "at": iso(utcnow()), "detail": "ok"})
    components = daemon.state.components
    assert components is not None
    fake_ops_calls: list[dict[str, Any]] = []

    async def fake_ops(args: dict[str, Any]) -> dict[str, Any]:
        fake_ops_calls.append(args)
        return {"restarted": True}

    components.registry.register(FAKE_OPS)
    components.handlers[(None, FAKE_OPS.name)] = fake_ops
    script = ScriptRunner(components)
    daemon.loop.runners[script.name] = script

    caplog.set_level(logging.INFO, logger="resonant.daemon")
    run = asyncio.create_task(daemon.run())
    assert daemon.imessage is not None
    channel = daemon.imessage.channel
    await wait_until(lambda: channel.health()["last_read_at"] is not None, 10)
    yield Running(daemon, osa, chat, port, script, fake_ops_calls)
    daemon.stop()
    assert await asyncio.wait_for(run, 10) == 0


# --- wiring (P2-9) ---------------------------------------------------------------------


async def test_policy_gate_and_reminder_runner_are_wired(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    d, c = running.daemon, running.components
    assert d.imessage is not None
    gate = c.gate
    assert isinstance(gate, PolicyGate)
    assert gate.dry_run is True
    assert gate.ttl == timedelta(seconds=d.settings.gate.approval_ttl_s)
    assert c.executor.verifier is gate
    assert d.imessage.runner.gate is gate
    assert isinstance(d.imessage.reminders, ReminderRunner)
    assert d.loop.runners["reminder"] is d.imessage.reminders is c.runners["reminder"]
    assert d.imessage.reminders.channel is d.imessage.channel
    assert d.imessage.reminders.timezone == d.settings.timezone
    assert any("gate=policy dry_run=True" in r.getMessage() for r in caplog.get_records("setup"))
    assert (await running.status())["approvals_pending"] == 0


def test_imessage_disabled_has_policy_gate_and_no_reminder_runner() -> None:
    daemon = Daemon(load_settings())
    try:
        assert daemon.imessage is None
        assert set(daemon.loop.runners) == {"echo"}
        assert daemon.state.components is not None
        assert isinstance(daemon.state.components.gate, PolicyGate)
        assert daemon.state.components.runners == {}
    finally:
        daemon.conn.close()


def test_gate_follows_settings(db: sqlite3.Connection) -> None:
    from resonant.api import DaemonState
    from resonant.loop import Loop, SlotPool

    settings = load_settings(dry_run=False, gate={"approval_ttl_s": 600})
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    state = DaemonState(settings, db, loop, started_at=utcnow())
    gate = build_components(settings, db, state).gate
    assert isinstance(gate, PolicyGate)
    assert gate.dry_run is False
    assert gate.ttl == timedelta(seconds=600)


async def test_fast_path_read_still_allowed(running: Running) -> None:
    r = running
    r.chat.add_message(handle=OWNER_PHONE, text="what's running?")
    sent = await r.osa.wait_for(1, 10)
    assert (sent.handle, sent.text) == (OWNER_PHONE, "Nothing running.")
    (audit,) = r.audits("gate.evaluate")
    assert audit["tool"] == "list_tasks" and audit["decision"] == "Allow"
    assert audit["detail"]["origin"] == "inferred"
    assert audit["detail"]["dry_run"] is True


# --- E15: dry_run ------------------------------------------------------------------------


async def test_e15_dry_run_runs_store_tools_and_holds_back_the_rest(running: Running) -> None:
    r = running
    due = utcnow() + timedelta(seconds=1)
    calls = await r.run_script(
        {"tool": "add_note", "args": {"text": "buy milk"}},
        {"tool": "add_reminder", "args": {"at": due.isoformat(), "text": "stretch"}},
        {"tool": FAKE_OPS.name},
        {"tool": "clear_notes"},
    )
    assert [c["decision"] for c in calls] == ["Allow", "Allow", "DryRun", "NeedsApproval"]

    # The store-internal tools ran.
    assert calls[0]["ok"] and calls[0]["data"]["id"] == 1
    assert calls[1]["ok"] and calls[1]["data"]["text"] == "stretch"

    # The non-exempt write was held back and never reached its handler.
    dry = r.script.decisions[2]
    assert isinstance(dry, DryRun) and dry.would == "fake restart"
    assert r.fake_ops_calls == []

    # L1 still asks with a code (the task waits on it); the summary is the notes hook's.
    asked = r.script.decisions[3]
    assert isinstance(asked, NeedsApproval) and asked.require_code
    assert asked.summary.startswith("delete all 1 note")
    assert (await r.status())["approvals_pending"] == 1

    # The reminder, created inside a step, fires on time through the iMessage channel
    # (well within one 30 s tick: the loop sleeps only until the next wake_at).
    sent = await r.osa.wait_for(1, 10)
    assert (sent.handle, sent.text) == (OWNER_PHONE, "⏰ stretch")
    assert utcnow() >= due
    (task,) = r.rows("SELECT status FROM tasks WHERE runner = 'reminder'")
    assert task["status"] == "done"

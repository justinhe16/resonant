"""Phase 2 acceptance (docs/phase2-plan.md §1): the whole daemon, in process, with fakes.

chat.db is a FakeChatDB, osascript is a RecordingOsaRunner and the model is a ScriptedModel,
as in ``test_e2e.py``. Each Phase 2 ticket adds its own scenarios (E1-E15) here.

Until the local runner's ``direct_call`` lands (P2-11), tool calls are driven by
``ScriptRunner``: a test runner that sends each call in its checkpoint through the daemon's
own gate and executor inside a loop step, the way a runner will.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import socket
import sqlite3
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from resonant.api import DaemonState
from resonant.components import Components, build_components
from resonant.config import load_settings
from resonant.daemon import Daemon
from resonant.e2e import RecordingOsaRunner, ScriptedModel, wait_until
from resonant.e2e.fakes import DEFAULT_FINAL_REPLY
from resonant.gate import Allow, DryRun, Intent, NeedsApproval, PolicyGate
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop import Loop, NewTask, SlotPool
from resonant.loop.types import PRIORITY_OWNER, Done, StepContext, StepOutcome, Task, Wait
from resonant.ops import mark_restart, restart_pending
from resonant.ops.fakes import FakeOpsRunner
from resonant.router import commands
from resonant.runners.reminder import ReminderRunner
from resonant.store.db import iso, kv_set, utcnow
from resonant.tools.when import format_due
from resonant_sdk import Event

OWNER_PHONE = "+15551234567"
SELF = "resonant.agent@icloud.com"
PRINCIPALS = f"""\
principals:
  justin:
    role: owner
    identities: ["imessage:{OWNER_PHONE}"]
"""


def free_port() -> int:
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
    ops: FakeOpsRunner

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
    caplog.set_level(logging.INFO, logger="resonant.daemon")
    async with start_daemon(resonant_home, chat) as r:
        yield r


def build_daemon(
    home: Path, chat: FakeChatDB, ops: FakeOpsRunner, *, dry_run: bool = True
) -> Running:
    """A daemon with iMessage on, fakes for osascript, the model and ops subprocesses."""
    (home / "principals.yaml").write_text(PRINCIPALS)
    port = free_port()
    settings = load_settings(
        dry_run=dry_run,
        api={"port": port},
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
    )
    osa = RecordingOsaRunner()
    daemon = Daemon(settings, model=ScriptedModel(), osa_runner=osa, ops_runner=ops)
    kv_set(daemon.conn, "imessage.selftest", {"ok": True, "at": iso(utcnow()), "detail": "ok"})
    components = daemon.state.components
    assert components is not None
    script = ScriptRunner(components)
    daemon.loop.runners[script.name] = script
    return Running(daemon, osa, chat, port, script, ops)


@contextlib.asynccontextmanager
async def start_daemon(
    home: Path,
    chat: FakeChatDB,
    before_run: Callable[[Running], None] | None = None,
    *,
    dry_run: bool = True,
) -> AsyncGenerator[Running]:
    r = build_daemon(home, chat, FakeOpsRunner(), dry_run=dry_run)
    if before_run is not None:
        before_run(r)
    run = asyncio.create_task(r.daemon.run())
    assert r.daemon.imessage is not None
    channel = r.daemon.imessage.channel
    await wait_until(lambda: channel.health()["last_read_at"] is not None, 10)
    try:
        yield r
    finally:
        r.daemon.stop()
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
        {"tool": "restart_ollama"},
        {"tool": "clear_notes"},
    )
    assert [c["decision"] for c in calls] == ["Allow", "Allow", "DryRun", "NeedsApproval"]

    # The store-internal tools ran.
    assert calls[0]["ok"] and calls[0]["data"]["id"] == 1
    assert calls[1]["ok"] and calls[1]["data"]["text"] == "stretch"

    # The non-exempt write was held back and never reached its handler.
    dry = r.script.decisions[2]
    assert isinstance(dry, DryRun) and dry.would == "restart Ollama"
    assert r.ops.calls == [] and r.ops.spawned == []

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


async def test_e15_dry_run_ops_reply(running: Running) -> None:
    """``/restart ollama`` under dry_run: the local runner's templated reply, no subprocess."""
    r = running
    r.daemon.loop.create_task(
        NewTask(
            kind="command",
            runner="local",
            priority=PRIORITY_OWNER,
            principal="justin",
            checkpoint={
                "direct_call": {"tool": "restart_ollama", "args": {}},
                "title": "/restart ollama",
            },
        )
    )
    sent = await r.osa.wait_for(1, 10)
    assert (sent.handle, sent.text) == (OWNER_PHONE, "Dry run: would restart Ollama")
    assert r.ops.calls == [] and r.ops.spawned == []


# --- "back up" (P2-14) -------------------------------------------------------------------


async def test_normal_start_sends_no_back_up(running: Running) -> None:
    await asyncio.sleep(0.2)
    assert running.osa.sent == []
    assert restart_pending(running.conn) is None


async def test_back_up_once_after_a_requested_restart(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    def marked(r: Running) -> None:
        mark_restart(r.conn, by="justin", commit="abc1234", now=utcnow() - timedelta(seconds=6))

    async with start_daemon(resonant_home, chat, before_run=marked) as r:
        sent = await r.osa.wait_for(1, 10)
        assert sent.handle == OWNER_PHONE
        assert sent.text.startswith("Back up (abc1234), down ")
        assert restart_pending(r.conn) is None

    # The next start (no restart requested) says nothing.
    async with start_daemon(resonant_home, chat) as r:
        await asyncio.sleep(0.2)
        assert r.osa.sent == []


# --- chat intents (P2-16): plain chat reaches the tools through the model -----------------


@pytest.fixture
async def live(resonant_home: Path, chat: FakeChatDB) -> AsyncIterator[Running]:
    """The daemon after the go-live flip: ``dry_run: false``."""
    async with start_daemon(resonant_home, chat, dry_run=False) as r:
        yield r


def reply(r: Running, text: str) -> None:
    """An approval reply, timestamped clearly after the prompt (the sent-before rule)."""
    r.chat.add_message(handle=OWNER_PHONE, text=text, at=utcnow() + timedelta(seconds=1))


def chat_task(r: Running, intent: str) -> sqlite3.Row:
    (task,) = r.rows("SELECT * FROM tasks WHERE kind = 'chat'")
    assert json.loads(task["checkpoint"])["intent"] == intent
    return task


async def test_natural_language_reminder_is_set_with_its_due_time(live: Running) -> None:
    r = live
    r.chat.add_message(handle=OWNER_PHONE, text="remind me in 20 minutes to stretch")
    sent = await r.osa.wait_for(1, 10)

    task = chat_task(r, "reminders")
    (call,) = r.audits("gate.evaluate")
    assert (call["tool"], call["decision"]) == ("add_reminder", "Allow")
    assert call["detail"]["origin"] == "inferred"  # the model chose it: L3 runs and reports

    (reminder,) = r.rows("SELECT * FROM tasks WHERE runner = 'reminder'")
    cp = json.loads(reminder["checkpoint"])
    assert cp["text"] == "stretch"
    due = datetime.fromisoformat(cp["at"])
    # The model computed ``at`` from the "Current time" line.
    assert timedelta(minutes=19) <= due - utcnow() <= timedelta(minutes=20)
    assert reminder["status"] == "queued"

    # The reply carries the code-built report with the due time, in the owner's zone.
    when = format_due(due, now=utcnow(), tz=r.daemon.settings.timezone)
    assert sent.handle == OWNER_PHONE
    assert f'⏰ Reminder 1 set for {when}: "stretch"' in sent.text
    assert json.loads(task["checkpoint"])["call"]["tool"] == "add_reminder"
    (ex,) = r.rows("SELECT status FROM executions")
    assert ex["status"] == "succeeded"


async def test_chat_restart_resonant_asks_and_runs_only_after_yes(live: Running) -> None:
    r = live
    r.chat.add_message(handle=OWNER_PHONE, text="restart resonant")
    prompt = await r.osa.wait_for(1, 10)

    await wait_until(lambda: chat_task(r, "ops")["status"] == "waiting", 10)
    assert chat_task(r, "ops")["wait_reason"] == "approval"
    match = re.fullmatch(r"\[([A-Z]\d)\] restart Resonant\? Reply yes / no", prompt.text)
    assert match is not None, prompt.text
    (audit,) = r.audits("gate.evaluate")
    assert (audit["tool"], audit["decision"]) == ("restart_resonant", "NeedsApproval")
    assert audit["detail"]["origin"] == "inferred"
    await asyncio.sleep(0.2)
    assert r.ops.spawned == []  # nothing ran yet
    assert restart_pending(r.conn) is None

    reply(r, "yes")
    done = await r.osa.wait_for(2, 10)
    assert done.text == "Restarting Resonant…"
    assert len(r.ops.spawned) == 1  # the detached helper, exactly once
    assert restart_pending(r.conn) is not None
    await wait_until(lambda: chat_task(r, "ops")["status"] == "done", 10)
    assert r.rows("SELECT status FROM approvals")[0]["status"] == "approved"


async def test_what_can_you_do_lists_real_commands(live: Running) -> None:
    r = live
    r.chat.add_message(handle=OWNER_PHONE, text="what can you do?")
    sent = await r.osa.wait_for(1, 10)
    assert sent.text == DEFAULT_FINAL_REPLY  # the scripted model's narration

    chat_task(r, "system")
    (audit,) = r.audits("gate.evaluate")
    assert (audit["tool"], audit["decision"]) == ("list_commands", "Allow")
    (ex,) = r.rows("SELECT status, result FROM executions")
    assert ex["status"] == "succeeded"
    listed = [c["name"] for c in json.loads(ex["result"])["commands"]]
    assert listed == [c.name for c in commands.COMMANDS]  # the registry, owner's view
    # The model got that registry output (as untrusted data) before it answered.
    assert r.daemon.imessage is not None
    model = r.daemon.imessage.model
    assert isinstance(model, ScriptedModel)
    assert model.calls["tools"] == 2 and model.calls["answer"] == 1


async def test_chat_clear_notes_needs_the_code(live: Running) -> None:
    r = live
    await r.run_script({"tool": "add_note", "args": {"text": "buy milk"}})
    r.chat.add_message(handle=OWNER_PHONE, text="clear my notes")
    prompt = await r.osa.wait_for(1, 10)
    match = re.fullmatch(
        r"\[([A-Z]\d)\] delete all 1 note\? This can't be undone\. Reply yes \1 / no",
        prompt.text,
    )
    assert match is not None, prompt.text
    await wait_until(lambda: chat_task(r, "notes")["status"] == "waiting", 10)
    audit = r.audits("gate.evaluate")[-1]
    assert (audit["tool"], audit["detail"]["origin"]) == ("clear_notes", "inferred")

    reply(r, "yes")  # a plain yes never approves L1
    refused = await r.osa.wait_for(2, 10)
    assert refused.text == f"Reply yes {match.group(1)} to confirm."
    assert chat_task(r, "notes")["status"] == "waiting"

    reply(r, f"yes {match.group(1)}")
    done = await r.osa.wait_for(3, 10)
    assert done.text == "Cleared 1 note."

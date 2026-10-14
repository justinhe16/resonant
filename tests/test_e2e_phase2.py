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
from resonant.gate import Allow, DryRun, Intent, NeedsApproval, PolicyGate
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop import Loop, NewTask, SlotPool
from resonant.loop.types import PRIORITY_OWNER, Done, StepContext, StepOutcome, Task, Wait
from resonant.ops import mark_restart, restart_pending
from resonant.ops.fakes import FakeOpsRunner
from resonant.runners.reminder import ReminderRunner
from resonant.store.db import iso, kv_set, utcnow
from resonant.tools.ops import GIT
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

    async def say(self, text: str, replies: int = 1) -> list[str]:
        """Send ``text`` from the owner's phone; return the next ``replies`` sends."""
        n = len(self.osa.sent)
        self.chat.add_message(handle=OWNER_PHONE, text=text)
        await self.osa.wait_for(n + replies, 10)
        return [m.text for m in self.osa.sent[n : n + replies]]

    async def prompt_recorded(self) -> None:
        """Wait until the pending approval's prompt send is recorded: a reply must come after
        it (the sent-before rule), and a person never answers that fast."""

        def recorded() -> bool:
            rows = self.rows("SELECT channel_refs FROM approvals WHERE status = 'pending'")
            return bool(rows) and all(json.loads(r["channel_refs"]) for r in rows)

        await wait_until(recorded, 10)
        await asyncio.sleep(0.01)

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
    home: Path, chat: FakeChatDB, ops_runner: FakeOpsRunner, **overrides: Any
) -> Running:
    """A daemon with iMessage on, fakes for osascript, the model and ops subprocesses."""
    (home / "principals.yaml").write_text(PRINCIPALS)
    port = free_port()
    settings = load_settings(
        api={"port": port},
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
        **overrides,
    )
    osa = RecordingOsaRunner()
    daemon = Daemon(settings, model=ScriptedModel(), osa_runner=osa, ops_runner=ops_runner)
    kv_set(daemon.conn, "imessage.selftest", {"ok": True, "at": iso(utcnow()), "detail": "ok"})
    components = daemon.state.components
    assert components is not None
    script = ScriptRunner(components)
    daemon.loop.runners[script.name] = script
    return Running(daemon, osa, chat, port, script, ops_runner)


@contextlib.asynccontextmanager
async def start_daemon(
    home: Path,
    chat: FakeChatDB,
    before_run: Callable[[Running], None] | None = None,
    *,
    ops_runner: FakeOpsRunner | None = None,
    **overrides: Any,
) -> AsyncGenerator[Running]:
    r = build_daemon(home, chat, ops_runner or FakeOpsRunner(), **overrides)
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


# --- P2-15: slash commands ----------------------------------------------------------------


@pytest.fixture
async def live(resonant_home: Path, chat: FakeChatDB) -> AsyncIterator[Running]:
    """The daemon with ``dry_run: false`` and scripted git/uv for ``/update``."""
    ops = (
        FakeOpsRunner()
        .ok(GIT, "rev-parse", "--abbrev-ref", "HEAD", stdout="main\n")
        .ok(GIT, "status", "--porcelain")
        .ok(GIT, "rev-parse", "--short=7", "HEAD", stdout="abc1234\n")
    )
    async with start_daemon(
        resonant_home, chat, ops_runner=ops, dry_run=False, ops={"uv_path": "/opt/uv/bin/uv"}
    ) as r:
        yield r


def _code(prompt: str) -> str:
    m = re.match(r"\[([A-Z][0-9])\] ", prompt)
    assert m, prompt
    return m.group(1)


async def test_e1_note_runs_with_no_prompt(running: Running) -> None:
    r = running
    assert await r.say("/note buy milk") == ['Saved note 1: "buy milk"']
    (row,) = r.rows("SELECT status FROM executions")
    assert row["status"] == "succeeded"
    (audit,) = r.audits("gate.evaluate")
    assert audit["tool"] == "add_note" and audit["decision"] == "Allow"
    assert audit["detail"]["origin"] == "explicit"
    assert (await r.status())["approvals_pending"] == 0

    assert await r.say("/notes") == ["Notes (1):\n1. buy milk"]
    assert await r.say("/unnote 1") == ["Deleted note 1."]
    assert await r.say("/notes") == ["No notes."]
    assert await r.say("/unnote 1") == ["Failed: delete note id=1 (delete_note: no note 1)"]
    assert await r.say("/unnote one") == ["that's not a note number. Usage: /unnote <n>"]


async def test_e4_notes_clear_needs_the_code(running: Running) -> None:
    r = running
    await r.say("/note a")
    await r.say("/note b")
    (prompt,) = await r.say("/notes clear")
    code = _code(prompt)
    assert prompt == f"[{code}] delete all 2 notes? This can't be undone. Reply yes {code} / no"
    await r.prompt_recorded()

    assert await r.say("yes") == [f"Reply yes {code} to confirm."]
    assert await r.say("/notes") == ["Notes (2):\n2. b\n1. a"]  # still there

    assert await r.say(f"yes {code}") == ["Cleared 2 notes."]
    assert await r.say("/notes") == ["No notes."]
    (audit,) = [a for a in r.audits("gate.evaluate") if a["tool"] == "clear_notes"][:1]
    assert audit["detail"]["origin"] == "explicit"


async def test_e4_notes_clear_no_denies(running: Running) -> None:
    r = running
    await r.say("/note a")
    (prompt,) = await r.say("/notes clear")
    assert prompt.endswith(f"Reply yes {_code(prompt)} / no")
    await r.prompt_recorded()
    assert await r.say("no") == ["Not done."]
    assert await r.say("/notes") == ["Notes (1):\n1. a"]


async def test_e2_restart_resonant_runs_with_no_prompt(live: Running) -> None:
    r = live
    assert await r.say("/restart resonant") == ["Restarting Resonant…"]
    assert len(r.ops.spawned) == 1 and "resonant.ops.restart_later" in r.ops.spawned[0]
    marker = restart_pending(r.conn)
    assert marker is not None and marker["commit"] == "abc1234"
    audit = [a for a in r.audits("gate.evaluate") if a["tool"] == "restart_resonant"]
    assert [(a["decision"], a["detail"]["origin"]) for a in audit] == [("Allow", "explicit")]
    assert (await r.status())["approvals_pending"] == 0

    assert await r.say("/restart ollama") == ["Restarted Ollama."]
    assert any("kickstart" in argv for argv in r.ops.argvs())


async def test_e6_remind_set_listed_and_cancelled(running: Running) -> None:
    r = running
    before = utcnow()
    (reply,) = await r.say("/remind in 20 min stretch")
    (task,) = r.rows("SELECT id, status, wake_at, checkpoint FROM tasks WHERE kind = 'reminder'")
    cp = json.loads(task["checkpoint"])
    at = datetime.fromisoformat(cp["at"])
    assert timedelta(minutes=19) < at - before <= timedelta(minutes=20, seconds=5)
    due = format_due(at, now=utcnow(), tz=r.daemon.settings.timezone)
    assert reply == f'⏰ set for {due}: "stretch" (reminder 1)'
    assert (task["status"], cp["text"]) == ("queued", "stretch")

    assert await r.say("/reminders") == [f"Reminders (1):\n1. {due}: stretch"]
    assert await r.say("/unremind 1") == [f"Cancelled reminder 1 (was due {due})."]
    (task,) = r.rows("SELECT status FROM tasks WHERE kind = 'reminder'")
    assert task["status"] == "cancelled"
    assert await r.say("/reminders") == ["No reminders."]


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("/remind 3 call mom", "say 3pm or 15:00"),
        ("/remind yesterday 9am x", "that time has passed"),
        ("/remind in 5 min", "say what to remind you about"),
    ],
)
async def test_e8_bad_times_get_a_usage_reply(running: Running, text: str, hint: str) -> None:
    r = running
    assert await r.say(text) == [f"{hint}. Usage: /remind <when> <text>"]
    assert r.rows("SELECT id FROM tasks WHERE kind IN ('reminder', 'command')") == []


async def test_e9_update_refuses_a_dirty_tree(resonant_home: Path, chat: FakeChatDB) -> None:
    ops = (
        FakeOpsRunner()
        .ok(GIT, "rev-parse", "--abbrev-ref", "HEAD", stdout="main\n")
        .ok(GIT, "status", "--porcelain", stdout=" M README.md\n")
    )
    async with start_daemon(
        resonant_home, chat, ops_runner=ops, dry_run=False, ops={"uv_path": "/opt/uv/bin/uv"}
    ) as r:
        assert await r.say("/update") == [
            "Failed: update resonant (working tree has changes; not updating)"
        ]
        assert not any(argv[:2] == (GIT, "pull") for argv in r.ops.argvs())
        assert r.ops.spawned == []


async def test_e9_update_already_current(live: Running) -> None:
    r = live
    assert await r.say("/update") == ["Already up to date (abc1234)."]
    assert r.ops.spawned == []


async def test_e10_unknown_command_suggests_and_never_executes(running: Running) -> None:
    r = running
    assert await r.say("/remnd 3pm x") == ["Unknown command /remnd. Did you mean /remind?"]
    assert r.rows("SELECT id FROM tasks WHERE kind IN ('reminder', 'command')") == []
    assert r.audits("gate.evaluate") == []

    (text,) = await r.say("/help")
    for usage in (
        "/note <text>",
        "/notes [clear]",
        "/unnote <n>",
        "/remind <when> <text>",
        "/reminders",
        "/unremind <n>",
        "/restart ollama|resonant",
        "/update",
        "/disk",
        "/logs [n]",
        "/projects [all]",
        "/approvals [code]",
    ):
        assert f"\n{usage}: " in text, usage
    (text,) = await r.say("/help rem")
    assert text.splitlines()[1:] == [
        "/remind <when> <text>: set a reminder: 3pm, in 20 min, tomorrow 9am, fri 5pm",
        "/reminders: pending reminders, soonest first",
        "/unremind <n>: cancel reminder n",
    ]


async def test_e11_projects_shows_commands_by_title(running: Running) -> None:
    r = running
    await r.say("/note buy milk")
    (text,) = await r.say("/projects")
    assert text.startswith("Projects (")
    assert "- done · command · /note · " in text
    assert "buy milk" not in text  # titles never carry message text


async def test_e15_dry_run_through_the_router(running: Running) -> None:
    """dry_run on: ``/restart ollama`` is held back while notes and reminders still run."""
    r = running
    assert await r.say("/restart ollama") == ["Dry run: would restart Ollama"]
    assert await r.say("/update") == ["Dry run: would update Resonant"]
    assert r.ops.calls == [] and r.ops.spawned == []
    assert await r.say("/note x") == ['Saved note 1: "x"']
    (reply,) = await r.say("/remind in 1 h y")
    assert reply.startswith("⏰ set for ") and reply.endswith('"y" (reminder 1)')

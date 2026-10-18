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
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
from resonant.gateway.imessage.sender import OsaResult
from resonant.loop import Loop, NewTask, SlotPool
from resonant.loop.types import PRIORITY_OWNER, Done, StepContext, StepOutcome, Task, Wait
from resonant.ops import mark_restart, restart_pending
from resonant.ops.fakes import FakeOpsRunner
from resonant.router import commands
from resonant.router.approvals import NOTHING_WAITING
from resonant.router.fast_path import KILL_REPLY, RESUME_REPLY
from resonant.runners.reminder import ReminderRunner
from resonant.store.db import iso, kv_get, kv_set, utcnow
from resonant.store.tasks import get_event
from resonant.tools.ops import GIT
from resonant.tools.when import clock_time, format_due
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
    home: Path,
    chat: FakeChatDB,
    ops_runner: FakeOpsRunner,
    *,
    dry_run: bool = True,
    osa: RecordingOsaRunner | None = None,
    **overrides: Any,
) -> Running:
    """A daemon with iMessage on, fakes for osascript, the model and ops subprocesses."""
    (home / "principals.yaml").write_text(PRINCIPALS)
    port = free_port()
    overrides.setdefault("brain", {"mode": "intents"})  # scripted labels (loop is the default)
    settings = load_settings(
        dry_run=dry_run,
        api={"port": port},
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
        **overrides,
    )
    osa = osa or RecordingOsaRunner()
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
    dry_run: bool = True,
    ops_runner: FakeOpsRunner | None = None,
    osa: RecordingOsaRunner | None = None,
    **overrides: Any,
) -> AsyncGenerator[Running]:
    r = build_daemon(
        home, chat, ops_runner or FakeOpsRunner(), dry_run=dry_run, osa=osa, **overrides
    )
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

    # logged once the runner has started (it reads the daemon's own checkout first, GD-1),
    # which can land after the fixture's setup
    def started() -> bool:
        records = [*caplog.get_records("setup"), *caplog.records]
        return any("gate=policy dry_run=True" in r.getMessage() for r in records)

    await wait_until(started, 10)
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
    r.chat.add_message(handle=OWNER_PHONE, text="/tasks")
    sent = await r.osa.wait_for(1, 10)
    assert sent.handle == OWNER_PHONE
    (audit,) = r.audits("gate.evaluate")
    assert audit["tool"] == "projects" and audit["decision"] == "Allow"
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
    # GT-1: the first ask also offers a standing allow for the tool.
    match = re.fullmatch(
        r"\[([A-Z]\d)\] restart Resonant \(yes always = don't ask before running "
        r"restart_resonant\)\. Reply yes / yes always / no",
        prompt.text,
    )
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


# --- P2-15: slash commands ----------------------------------------------------------------


@pytest.fixture
async def live_ops(resonant_home: Path, chat: FakeChatDB) -> AsyncIterator[Running]:
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


async def test_e2_restart_resonant_runs_with_no_prompt(live_ops: Running) -> None:
    r = live_ops
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
    assert reply == f'⏰ Reminder 1 set for {due}: "stretch"'
    assert (task["status"], cp["text"]) == ("queued", "stretch")

    assert await r.say("/reminders") == [f"Reminders (1):\n1. {due}: stretch"]
    assert await r.say("/unremind 1") == ["Cancelled reminder 1."]
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
            "Failed: update Resonant (working tree has changes; not updating)"
        ]
        assert not any(argv[:2] == (GIT, "pull") for argv in r.ops.argvs())
        assert r.ops.spawned == []


async def test_e9_update_already_current(live_ops: Running) -> None:
    r = live_ops
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
    assert reply.startswith("⏰ Reminder 1 set for ") and reply.endswith('"y"')


# --- P2-17: kill switch, durability, restart, replay and orphans --------------------------


class FlakyOsaRunner(RecordingOsaRunner):
    """Fails the first send of each text in ``fail_once`` (osascript exits 1)."""

    def __init__(self, *fail_once: str) -> None:
        super().__init__()
        self.fail_once = set(fail_once)
        self.failed: list[str] = []

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> OsaResult:
        if argv[-1] in self.fail_once:
            self.fail_once.discard(argv[-1])
            self.failed.append(argv[-1])
            return OsaResult(1, "", "Messages got an error")
        return await super().__call__(argv, timeout_s)


def approvals(r: Running) -> list[sqlite3.Row]:
    return r.rows("SELECT * FROM approvals ORDER BY created_at")


def resolutions(r: Running) -> list[dict[str, Any]]:
    return [a for a in r.audits("approval.resolve") if a["detail"]["ok"]]


async def test_e5_kill_switch_asks_for_everything_above_l0(live_ops: Running) -> None:
    r = live_ops
    assert await r.say("/kill") == [KILL_REPLY]

    # Explicit L3 asks with a plain yes; /kill while it is pending is a command, not a reply.
    (prompt,) = await r.say("/note x")
    code = _code(prompt)
    assert prompt == f'[{code}] add note text="x"? Reply yes / no'
    await r.prompt_recorded()
    assert await r.say("/kill") == [KILL_REPLY]
    assert [a["status"] for a in approvals(r)] == ["pending"]
    assert await r.say("yes") == ['Saved note 1: "x"']

    # Explicit L2 (an op) asks too, and nothing runs until it is approved.
    (prompt,) = await r.say("/restart ollama")
    assert prompt == f"[{_code(prompt)}] restart Ollama? Reply yes / no"
    await r.prompt_recorded()
    assert not any("kickstart" in argv for argv in r.ops.argvs())
    assert await r.say("no") == ["Not done."]

    # L1 still needs its code.
    (prompt,) = await r.say("/notes clear")
    code = _code(prompt)
    assert prompt.endswith(f"This can't be undone. Reply yes {code} / no")
    await r.prompt_recorded()
    assert await r.say("yes") == [f"Reply yes {code} to confirm."]
    assert await r.say("no") == ["Not done."]

    audits = [(a["tool"], a["decision"]) for a in r.audits("gate.evaluate")]
    assert ("add_note", "NeedsApproval") in audits
    assert ("restart_ollama", "NeedsApproval") in audits

    # /resume: explicit commands run directly again.
    assert await r.say("/resume") == [RESUME_REPLY]
    assert await r.say("/note y") == ['Saved note 2: "y"']
    assert await r.say("/restart ollama") == ["Restarted Ollama."]
    assert any("kickstart" in argv for argv in r.ops.argvs())


async def test_e7_reminder_survives_a_restart_before_it_is_due(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    async with start_daemon(resonant_home, chat) as r:
        due = utcnow() + timedelta(seconds=4)
        await r.run_script({"tool": "add_reminder", "args": {"at": due.isoformat(), "text": "x"}})
    assert utcnow() < due  # stopped before it was due

    async with start_daemon(resonant_home, chat) as r:
        sent = await r.osa.wait_for(1, 10)
        assert sent.text == "⏰ x" and utcnow() >= due
        await asyncio.sleep(0.3)
        assert len(r.osa.sent) == 1

    async with start_daemon(resonant_home, chat) as r:  # and never again
        await asyncio.sleep(0.3)
        assert r.osa.sent == []


async def test_e7_reminder_missed_while_down_fires_once_marked_late(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    async with start_daemon(resonant_home, chat) as r:
        (set_reply,) = await r.say("/remind in 20 min stretch")
        assert set_reply.startswith("⏰ Reminder 1 set for ")
        tz = r.daemon.settings.timezone

    # The daemon was down from before the due time until 10 min after it: as far as the
    # store can tell, that is the same as the due time being 10 min ago.
    due = utcnow() - timedelta(minutes=10)
    conn = sqlite3.connect(resonant_home / "resonant.db")
    with conn:
        conn.execute(
            "UPDATE tasks SET wake_at = ?, checkpoint = json_set(checkpoint, '$.at', ?) "
            "WHERE kind = 'reminder'",
            (iso(due), iso(due)),
        )
    conn.close()

    async with start_daemon(resonant_home, chat) as r:
        sent = await r.osa.wait_for(1, 10)
        assert sent.text == f"⏰ stretch (late, due {clock_time(due.astimezone(ZoneInfo(tz)))})"
        await asyncio.sleep(0.3)
        assert len(r.osa.sent) == 1
        (task,) = r.rows("SELECT status FROM tasks WHERE kind = 'reminder'")
        assert task["status"] == "done"


async def test_e12_restart_mid_approval_runs_exactly_once(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    async with start_daemon(resonant_home, chat, dry_run=False) as r:
        r.chat.add_message(handle=OWNER_PHONE, text="restart resonant")
        prompt = await r.osa.wait_for(1, 10)
        assert prompt.text.endswith(
            "restart Resonant (yes always = don't ask before running restart_resonant). "
            "Reply yes / yes always / no"
        )
        await wait_until(lambda: chat_task(r, "ops")["status"] == "waiting", 10)
        await r.prompt_recorded()
        assert r.ops.spawned == []
    # SIGTERM (a graceful stop); then a new stack on the same store and chat.db.

    async with start_daemon(resonant_home, chat, dry_run=False) as r:
        assert [a["status"] for a in approvals(r)] == ["pending"]
        await asyncio.sleep(0.2)
        assert r.osa.sent == []  # no re-prompt and no "back up" on a plain start
        reply(r, "yes")
        done = await r.osa.wait_for(1, 10)
        assert done.text == "Restarting Resonant…"
        await wait_until(lambda: chat_task(r, "ops")["status"] == "done", 10)
        await asyncio.sleep(0.3)
        assert len(r.ops.spawned) == 1 and len(r.osa.sent) == 1
        (ex,) = r.rows("SELECT status FROM executions")
        assert ex["status"] == "succeeded"
        assert [a["status"] for a in approvals(r)] == ["approved"]
        assert approvals(r)[0]["used_at"] is not None


async def test_e13_the_same_row_routed_twice_resolves_once(live: Running) -> None:
    r = live
    r.chat.add_message(handle=OWNER_PHONE, text="restart resonant")
    await r.osa.wait_for(1, 10)
    await r.prompt_recorded()
    rowid = r.chat.add_message(
        handle=OWNER_PHONE, text="yes", at=utcnow() + timedelta(seconds=1), guid="yes-guid"
    )
    await r.osa.wait_for(2, 10)
    await wait_until(lambda: chat_task(r, "ops")["status"] == "done", 10)
    assert len(resolutions(r)) == 1

    # A router retry of the same event: the original result, no write and no reply.
    (row,) = r.rows("SELECT id FROM events WHERE dedupe_key = 'imessage:yes-guid'")
    event = get_event(r.conn, row["id"])
    assert event is not None and r.daemon.imessage is not None
    assert await r.daemon.imessage.router.route(event) is None

    # The reader re-reading the row (cursor rewound): the event is a duplicate.
    kv_set(r.conn, "imessage.cursor", rowid - 1)
    await wait_until(lambda: kv_get(r.conn, "imessage.cursor") == rowid, 10)
    await asyncio.sleep(0.3)
    assert len(r.osa.sent) == 2
    assert len(resolutions(r)) == 1
    assert len(r.ops.spawned) == 1
    assert len(r.rows("SELECT status FROM executions")) == 1


async def test_e13_two_yes_in_one_poll_resolve_and_run_once(live: Running) -> None:
    r = live
    r.chat.add_message(handle=OWNER_PHONE, text="restart resonant")
    await r.osa.wait_for(1, 10)
    await r.prompt_recorded()
    at = utcnow() + timedelta(seconds=1)
    r.chat.conn.execute("BEGIN")
    r.chat.add_message(handle=OWNER_PHONE, text="yes", at=at)
    r.chat.add_message(handle=OWNER_PHONE, text="yes", at=at)
    r.chat.conn.execute("COMMIT")

    await wait_until(lambda: chat_task(r, "ops")["status"] == "done", 10)
    await asyncio.sleep(0.3)
    texts = [m.text for m in r.osa.sent[1:]]
    assert texts.count("Restarting Resonant…") == 1
    assert set(texts) <= {"Restarting Resonant…", NOTHING_WAITING}
    assert len(resolutions(r)) == 1
    assert len(r.ops.spawned) == 1
    assert len(r.rows("SELECT status FROM executions")) == 1


async def test_e13_a_reply_from_before_the_prompt_never_binds(live: Running) -> None:
    r = live
    r.chat.add_message(handle=OWNER_PHONE, text="restart resonant")
    await r.osa.wait_for(1, 10)
    await r.prompt_recorded()
    # A late-delivered "yes" timestamped before the prompt went out.
    r.chat.add_message(handle=OWNER_PHONE, text="yes", at=utcnow() - timedelta(minutes=1))
    refused = await r.osa.wait_for(2, 10)
    assert refused.text == "That reply came before the question. Reply again to answer it."
    assert resolutions(r) == []
    assert [a["status"] for a in approvals(r)] == ["pending"]
    assert r.ops.spawned == []


async def test_e14_cancelling_a_task_orphans_its_approval(running: Running) -> None:
    r = running
    await r.say("/note a")
    (prompt,) = await r.say("/notes clear")
    code = _code(prompt)
    await r.prompt_recorded()
    (task,) = r.rows("SELECT id FROM tasks WHERE status = 'waiting'")
    assert r.daemon.loop.cancel(task["id"])

    assert [a["status"] for a in approvals(r)] == ["expired"]
    assert len(r.audits("approval.orphaned")) == 1
    assert await r.say(f"yes {code}") == [NOTHING_WAITING]
    assert await r.say("/notes") == ["Notes (1):\n1. a"]
    assert resolutions(r) == []


async def test_e14_a_used_approval_never_allows_again(live: Running) -> None:
    r = live
    r.chat.add_message(handle=OWNER_PHONE, text="restart resonant")
    await r.osa.wait_for(1, 10)
    await r.prompt_recorded()
    reply(r, "yes")
    await r.osa.wait_for(2, 10)
    task = chat_task(r, "ops")
    await wait_until(lambda: chat_task(r, "ops")["status"] == "done", 10)
    (approval,) = approvals(r)
    assert approval["status"] == "approved" and approval["used_at"] is not None

    spec = r.components.registry.get(None, "restart_resonant")
    assert spec is not None
    intent = Intent.for_tool(spec, task_id=task["id"], principal="justin", args={})
    assert not isinstance(r.components.gate.evaluate(intent), Allow)
    assert len(r.ops.spawned) == 1


async def test_e14_a_lost_reply_is_resent_from_executions(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    """The reply send fails after the side effect ran: the retry replays the recorded result
    (no second gate call, no second note) and sends the reply."""
    osa = FlakyOsaRunner('Saved note 1: "x"')
    async with start_daemon(resonant_home, chat, osa=osa) as r:
        r.chat.add_message(handle=OWNER_PHONE, text="/note x")
        await wait_until(lambda: bool(osa.failed), 10)
        # Skip the retry backoff rather than wait it out.
        await wait_until(lambda: bool(r.rows("SELECT id FROM tasks WHERE status = 'queued'")), 10)
        r.conn.execute("UPDATE tasks SET wake_at = NULL WHERE status = 'queued'")
        await r.run_script()  # creating a task wakes the loop
        sent = await osa.wait_for(1, 10)
        assert sent.text == 'Saved note 1: "x"'
        assert len(r.audits("gate.evaluate")) == 1
        (ex,) = r.rows("SELECT status FROM executions")
        assert ex["status"] == "succeeded"
        assert await r.say("/notes") == ["Notes (1):\n1. x"]

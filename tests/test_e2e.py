"""Phase 1 acceptance: the whole daemon, in process, with iMessage enabled.

chat.db is a FakeChatDB, osascript is a RecordingOsaRunner and the model is a ScriptedModel,
so nothing here needs Messages.app, a real chat.db, Ollama or the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sqlite3
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from resonant.cli import app
from resonant.config import ModelConfig, Settings, load_settings
from resonant.daemon import Daemon
from resonant.e2e import RecordingOsaRunner, ScriptedModel, wait_until
from resonant.e2e.bench import report, run_e2e_bench
from resonant.e2e.fakes import DEFAULT_FINAL_REPLY
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop.echo import EchoRunner
from resonant.runners.local import MAX_TOOL_ITERATIONS
from resonant.store.db import iso, kv_set, utcnow
from resonant.wiring import local_step_limit

OWNER_PHONE = "+15551234567"
STRANGER = "+15559999999"
SELF = "resonant.agent@icloud.com"
SECRET = "my bank pin is 4321"  # a body that must never reach audit rows

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


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[FakeChatDB]:
    db = FakeChatDB(tmp_path / "chat.db")
    yield db
    db.close()


@dataclass
class Running:
    daemon: Daemon
    osa: RecordingOsaRunner
    model: ScriptedModel
    chat: FakeChatDB
    port: int

    @property
    def conn(self) -> sqlite3.Connection:
        return self.daemon.conn

    def rows(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, args).fetchall())

    def audits(self, action: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.rows("SELECT * FROM audit_log WHERE action = ? ORDER BY id", action):
            d = dict(r)
            d["detail"] = json.loads(d["detail"] or "{}")
            out.append(d)
        return out

    async def idle(self) -> None:
        """Wait until the loop has nothing in flight and nothing unrouted or queued."""
        for _ in range(500):
            busy = (
                self.daemon.loop.inflight
                or self.rows("SELECT 1 FROM events WHERE consumed_at IS NULL LIMIT 1")
                or self.rows("SELECT 1 FROM tasks WHERE status IN ('queued', 'running') LIMIT 1")
            )
            if not busy:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("daemon never became idle")


@pytest.fixture
async def running(resonant_home: Path, chat: FakeChatDB) -> AsyncIterator[Running]:
    (resonant_home / "principals.yaml").write_text(PRINCIPALS)
    port = free_port()
    settings = load_settings(
        api={"port": port},
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
    )
    osa, model = RecordingOsaRunner(), ScriptedModel()
    daemon = Daemon(settings, model=model, osa_runner=osa)
    # A fresh self-test result, so the monitor's first round doesn't send one.
    kv_set(daemon.conn, "imessage.selftest", {"ok": True, "at": iso(utcnow()), "detail": "ok"})
    run = asyncio.create_task(daemon.run())
    assert daemon.imessage is not None
    channel = daemon.imessage.channel
    # The reader's baseline read: rows written before it are history and never routed.
    await wait_until(lambda: channel.health()["last_read_at"] is not None, 10)
    yield Running(daemon, osa, model, chat, port)
    daemon.stop()
    assert await asyncio.wait_for(run, 10) == 0


# --- Phase 0 regression ----------------------------------------------------------------


def test_imessage_disabled_is_phase0() -> None:
    daemon = Daemon(load_settings())
    try:
        assert daemon.imessage is None
        assert daemon.state.monitor is None
        assert daemon.loop.router is None
        assert set(daemon.loop.runners) == {"echo"}
        assert isinstance(daemon.loop.runners["echo"], EchoRunner)
        assert daemon.state.components is not None
        assert daemon.state.components.runners == {}
    finally:
        daemon.conn.close()


async def test_imessage_disabled_daemon_runs_without_channel_or_monitor() -> None:
    port = free_port()
    daemon = Daemon(load_settings(api={"port": port}))
    run = asyncio.create_task(daemon.run())
    async with httpx.AsyncClient() as client:
        for _ in range(200):
            try:
                channels = (await client.get(f"http://127.0.0.1:{port}/api/channels")).json()
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.02)
        else:
            raise AssertionError("daemon never came up")
    assert channels[0]["enabled"] is False and channels[0]["ok"] is None
    daemon.stop()
    assert await asyncio.wait_for(run, 5) == 0


# --- wiring ----------------------------------------------------------------------------


async def test_enabled_daemon_wires_router_runner_and_monitor(running: Running) -> None:
    d = running.daemon
    assert d.imessage is not None
    assert d.state.monitor is d.imessage.monitor
    assert d.loop.router is not None
    assert d.loop.runners["local"] is d.imessage.runner
    assert d.state.components is not None
    assert d.state.components.runners["local"] is d.imessage.runner
    assert d.imessage.runner.max_step_s == local_step_limit(d.settings.model)
    assert d.imessage.monitor.owner == "justin"
    async with httpx.AsyncClient() as client:
        channels = (await client.get(f"http://127.0.0.1:{running.port}/api/channels")).json()
    assert channels[0]["enabled"] is True and channels[0]["ok"] is True


def test_step_limit_outlasts_every_model_call() -> None:
    cfg = ModelConfig(timeout_s=60)
    assert local_step_limit(cfg) > (MAX_TOOL_ITERATIONS + 1) * cfg.timeout_s
    assert local_step_limit(ModelConfig(timeout_s=5)) < local_step_limit(cfg)


# --- the "what's running?" flow (fast path) --------------------------------------------


async def test_whats_running_end_to_end(running: Running) -> None:
    r = running
    r.chat.add_message(handle=SELF, text="hello from myself")  # Resonant's own handle
    r.chat.add_message(handle=STRANGER, text=SECRET)  # nobody in principals.yaml
    r.chat.add_message(handle=OWNER_PHONE, text="what's running?")

    sent = await r.osa.wait_for(1, 10)
    await r.idle()
    await asyncio.sleep(0.3)  # anything else would have been read and answered by now

    # Exactly one send: to the owner, with the fast-path reply. No model call.
    assert len(r.osa.sent) == 1
    assert sent.handle == OWNER_PHONE
    assert sent.text == "Nothing running."
    assert sum(r.model.calls[k] for k in ("label", "tools", "answer")) == 0

    # The event: only the owner's message became one, and it was consumed.
    events = r.rows("SELECT * FROM events")
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "imessage" and ev["type"] == "message"
    assert ev["principal"] == "justin" and ev["consumed_at"] is not None
    assert json.loads(ev["payload"])["handle"] == OWNER_PHONE

    # The task: routed on the fast path, answered by the local runner.
    tasks = r.rows("SELECT * FROM tasks")
    assert len(tasks) == 1
    task = tasks[0]
    assert (task["kind"], task["runner"], task["status"]) == ("reply", "local", "done")
    assert task["origin_event_id"] == ev["id"] and task["trace_id"] == ev["trace_id"]
    assert json.loads(task["checkpoint"])["tool_used"] == "list_tasks"
    assert json.loads(task["result"])["path"] == "fast"

    # The audit trail: the gated read, the send, and the dropped stranger.
    gate = r.audits("gate.evaluate")
    assert [(a["tool"], a["task_id"], a["requested_by"]) for a in gate] == [
        ("list_tasks", f"route:{ev['id']}", "justin")
    ]
    sends = r.audits("imessage.sent")
    assert len(sends) == 1
    assert sends[0]["requested_by"] == "justin"
    assert sends[0]["idempotency_key"] == f"reply:{task['id']}"
    unknown = r.audits("imessage.unknown_sender")
    assert [a["detail"]["handle"] for a in unknown] == [STRANGER]
    # self_handle is skipped silently: never an event, never audited as unknown.
    assert all(SELF not in json.dumps(a["detail"]) for a in unknown)

    trace = r.rows("SELECT name FROM spans WHERE trace_id = ?", ev["trace_id"])
    spans = {row["name"] for row in trace}
    assert {"router.fast_path", "runner.local.send"} <= spans

    # Bodies never reach the audit log.
    for row in r.rows("SELECT detail FROM audit_log"):
        assert SECRET not in (row["detail"] or "")
        assert "what's running" not in (row["detail"] or "")


# --- the labeled path: tools through gate -> executor ----------------------------------


async def test_labeled_question_runs_tools_through_gate_and_executor(running: Running) -> None:
    r = running
    r.chat.add_message(handle=OWNER_PHONE, text="anything queued up right now?")

    sent = await r.osa.wait_for(1, 10)
    await r.idle()
    await asyncio.sleep(0.2)

    assert len(r.osa.sent) == 1
    assert (sent.handle, sent.text) == (OWNER_PHONE, DEFAULT_FINAL_REPLY)
    assert r.model.calls["label"] == 1 and r.model.calls["answer"] == 1

    (task,) = r.rows("SELECT * FROM tasks")
    assert (task["kind"], task["runner"], task["status"]) == ("chat", "local", "done")
    cp = json.loads(task["checkpoint"])
    assert cp["intent"] == "job_control" and cp["toolset"] == ["list_tasks", "task_counts"]
    result = json.loads(task["result"])
    assert result["path"] == "llm" and result["tools_used"] == ["task_counts"]

    gate = [a for a in r.audits("gate.evaluate") if a["task_id"] == task["id"]]
    assert [(a["tool"], a["decision"]) for a in gate] == [("task_counts", "Allow")]
    executed = r.audits("executor.result")
    assert [(a["tool"], a["task_id"]) for a in executed] == [("task_counts", task["id"])]
    assert r.rows("SELECT * FROM executions WHERE task_id = ?", task["id"])
    sends = r.audits("imessage.sent")
    assert [a["idempotency_key"] for a in sends] == [f"reply:{task['id']}"]


# --- shutdown --------------------------------------------------------------------------


async def test_sigterm_stops_channel_monitor_and_loop(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    (resonant_home / "principals.yaml").write_text(PRINCIPALS)
    settings = load_settings(
        api={"port": free_port()},
        channels={"imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05}},
    )
    osa = RecordingOsaRunner()
    daemon = Daemon(settings, model=ScriptedModel(), osa_runner=osa)
    run = asyncio.create_task(daemon.run())
    stack = daemon.imessage
    assert stack is not None
    await wait_until(
        lambda: stack.running and stack.channel.health()["last_read_at"] is not None, 10
    )
    os.kill(os.getpid(), signal.SIGTERM)  # the daemon's handler, not the default one
    assert await asyncio.wait_for(run, 10) == 0
    assert not stack.running
    assert osa.sent == []


# --- bench e2e (fakes) ------------------------------------------------------------------


async def test_bench_e2e_with_fakes() -> None:
    results = await run_e2e_bench(2, warmup=False)
    data = report(results, model_name="fake")
    assert [r["path"] for r in data["results"]] == ["fast", "llm"]
    for r in data["results"]:
        assert r["n"] == 2 and r["failures"] == 0 and r["p50_ms"] <= r["p95_ms"]
    assert data["pass"] is True


def test_bench_e2e_cli_json() -> None:
    out = CliRunner().invoke(app, ["bench", "e2e", "--n", "1", "--json"])
    assert out.exit_code == 0, out.output
    data = json.loads(out.output)
    assert data["pass"] is True and data["model"].startswith("fake")


def test_bench_e2e_cli_rejects_unknown_model() -> None:
    out = CliRunner().invoke(app, ["bench", "e2e", "--model", "gpt"])
    assert out.exit_code == 2


@pytest.mark.integration
async def test_bench_e2e_real_model_meets_targets() -> None:
    """On the Mini: the configured Ollama model, fast p50 <= 1s and llm p50 <= 3s."""
    from resonant.models import OllamaClient

    settings: Settings = load_settings()
    async with OllamaClient(settings.model) as client:
        results = await run_e2e_bench(10, model=client, base=settings)
    data = report(results, model_name=settings.model.name)
    assert data["pass"], data

"""The worker CLI (docs/employee-plan.md D3, EM-3): ``resonant ask``/``note``/``check`` from a
session (the fake claude runs the real shim over the run's real socket), question waits,
answers, owner messages and the socket's checks."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from test_runner_claude import Harness, build, until

from resonant.loop.types import Task
from resonant.runners import worker_cli
from resonant.runners.claude import Refused
from resonant.runners.worker_cli import (
    EXIT_UNREACHABLE,
    Ask,
    Reply,
    Request,
    WorkerServer,
    descends_from,
    parse_request,
)
from resonant.runners.worker_desk import ASK_TIMEOUT, Relayed, WorkerEvent
from resonant.store import questions as qs
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.store.db import iso, utcnow


class Trust:
    """A TrustChecker: answers with ``answer``, after ``gate`` is set when there is one."""

    def __init__(self, answer: str = "ok", gate: asyncio.Event | None = None) -> None:
        self.answer = answer
        self.gate = gate
        self.calls: list[tuple[Any, ...]] = []

    async def request_check(
        self,
        run_code: str,
        system: str,
        action: str,
        irreversible: bool = False,
        cost_usd: float = 0.0,
        what: str = "",
    ) -> Any:
        self.calls.append((run_code, system, action, irreversible, cost_usd, what))
        if self.gate is not None:
            await self.gate.wait()
        return self.answer


def rig(tmp_path: Path, db: sqlite3.Connection, *extra: str, **kw: Any) -> Harness:
    """A run with no repo whose session runs the CLI steps in ``extra``."""
    h = build(tmp_path, db, scenario="scratch", extra=list(extra), **kw)
    h.runner._submit = h.loop.submit  # pyright: ignore[reportPrivateUsage]
    return h


def cli_calls(h: Harness, code: str = "R1") -> list[dict[str, Any]]:
    path = h.tmp(code) / "cli.json"
    return json.loads(path.read_text()) if path.exists() else []


def task_of(h: Harness, code: str = "R1") -> Task:
    task = task_store.get_task(h.conn, cast(str, h.run(code).task_id))
    assert task is not None
    return task


async def start(h: Harness) -> str:
    res = await h.runner.start_run(None, "pick a database and set it up", "owner")
    assert not isinstance(res, Refused), res
    return res.code


# --- ask --------------------------------------------------------------------------------------


async def test_ask_answered_while_the_cli_waits(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = rig(tmp_path, db, "--ask", "Which DB?", "--options", "pg,sqlite", ask_block_s=30)
    events: list[WorkerEvent] = []
    h.runner.events.subscribe(events.append)
    code = await start(h)
    loop = h.spawn_loop()
    await until(lambda: bool(h.runner.open_questions()), what="the question")
    [q] = h.runner.open_questions()
    assert (q.run_code, q.question, q.options) == ("R1", "Which DB?", ("pg", "sqlite"))
    assert "R1 asks: Which DB?\nOptions: pg, sqlite" in h.chan.texts()
    assert [(e.kind, e.run_code, e.question_id) for e in events] == [("question", "R1", q.id)]
    assert h.runner.answer_question("r1", "pg") == Relayed("R1", "Sent to R1.")
    await loop
    run = h.run(code)
    assert run.status == "done"
    [call] = cli_calls(h)
    assert call["rc"] == 0 and call["out"] == "The owner answered: pg"
    assert len(h.argv_log(code)) == 1  # answered inside the turn: no resume
    assert h.runner.open_questions() == []
    assert isinstance(h.runner.answer_question("R1", "again"), Refused)


async def test_no_answer_yet_parks_the_run_and_the_answer_resumes_it(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, "--ask", "Which DB?", ask_block_s=0.3)
    code = await start(h)
    await h.drain()
    run = h.run(code)
    assert run.status == "waiting" and run.wait_reason == "question" and run.step == "session"
    task = task_of(h)
    assert task.status == "waiting" and task.wait_reason == "human_reply"
    assert task.wake_at is not None and task.wake_at > utcnow() + timedelta(hours=3)
    assert h.loop.slots.in_use == 0 and not h.runner._working  # pyright: ignore[reportPrivateUsage]
    [call] = cli_calls(h)
    assert call["rc"] == 3 and call["out"].startswith("No answer yet. Finish what you can")
    pulse = db.execute("SELECT title FROM pulse_items WHERE key = 'run/r1'").fetchone()
    assert "R1 waiting for your answer" in pulse["title"]
    assert not (h.runner.ctl_root / code / worker_cli.TOKEN).exists()  # no session, no token

    assert isinstance(h.runner.answer_question(code, "pg"), Relayed)
    await h.drain()
    run = h.run(code)
    assert run.status == "done", run.failure
    assert run.wait_seconds == 0 and run.wait_reason is None
    _first, second = h.argv_log(code)
    assert "--resume" in second["argv"]
    assert second["prompt"].startswith('The owner answered your question "Which DB?": pg')
    assert run.resume_prompt is None  # the turn ended: nothing left to resend


async def test_an_unanswered_question_times_out(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = rig(tmp_path, db, "--ask", "Which DB?", ask_block_s=0.3)
    code = await start(h)
    await h.drain()
    assert h.run(code).wait_reason == "question"
    # ask_timeout (4 h) passes
    db.execute("UPDATE run_questions SET asked_at = ?", (iso(utcnow() - timedelta(hours=5)),))
    db.execute("UPDATE tasks SET wake_at = ?", (iso(utcnow() - timedelta(seconds=1)),))
    await h.drain()
    assert h.run(code).status == "done"
    [q] = qs.for_run(db, code)
    assert q.status == "timed_out" and q.delivered_at is not None
    assert h.argv_log(code)[1]["prompt"].startswith(f'Your question "Which DB?": {ASK_TIMEOUT}')


async def test_a_repeated_ask_joins_the_open_one(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = rig(tmp_path, db, ask_block_s=0.2)
    code = await start(h)
    run = h.run(code)
    gone: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    runs_store.update(db, code, status="running")
    first = await h.runner.desk.handle(code, Ask("Which DB?"), gone)
    again = await h.runner.desk.handle(code, Ask("Which DB?"), gone)
    other = await h.runner.desk.handle(code, Ask("And the region?"), gone)
    assert first is not None and again is not None and other is not None
    assert first.exit == again.exit == other.exit == 3
    assert "Only one question can be open" in other.text
    assert [t for t in h.chan.texts() if "asks" in t] == ["R1 asks: Which DB?"]
    assert len(qs.for_run(db, run.code)) == 1


# --- note and check ----------------------------------------------------------------------------


async def test_note_updates_the_pulse_item(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = rig(tmp_path, db, "--note", "schema migrated")
    events: list[WorkerEvent] = []
    h.runner.events.subscribe(events.append)
    code = await start(h)
    await h.drain()
    assert h.run(code).status == "done" and h.run(code).last_note == "schema migrated"
    [call] = cli_calls(h)
    assert (call["rc"], call["out"]) == (0, "Noted.")
    assert [(e.kind, e.text, e.urgent) for e in events] == [("note", "schema migrated", False)]


@pytest.mark.parametrize(("answer", "rc", "summary"), [("ok", 0, None), ("denied", 1, "Didn't")])
async def test_check_ok_or_denied(
    tmp_path: Path, db: sqlite3.Connection, answer: str, rc: int, summary: str | None
) -> None:
    trust = Trust(answer)
    h = rig(tmp_path, db, "--check", "Dori-API:deploy:deploy to prod", trust=trust)
    code = await start(h)
    await h.drain()
    assert trust.calls == [("R1", "dori-api", "deploy", False, 0.0, "deploy to prod")]
    [call] = cli_calls(h)
    assert call["rc"] == rc and call["out"].startswith(answer)
    run = h.run(code)
    assert run.status == "done"
    assert (run.summary or "").startswith(summary or "Checked the disk")


async def test_a_check_decided_after_the_cli_gave_up_is_delivered_on_resume(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    gate = asyncio.Event()
    h = rig(tmp_path, db, "--check", "stripe:charge:refund $5", trust=Trust("ok", gate),
            ask_block_s=0.3)  # fmt: skip
    code = await start(h)
    await h.drain()
    assert h.run(code).wait_reason == "question"
    assert cli_calls(h)[0]["rc"] == 3
    gate.set()
    await until(lambda: task_of(h).status != "waiting", what="the wake")
    await h.drain()
    assert h.run(code).status == "done"
    prompt = h.argv_log(code)[1]["prompt"]
    assert prompt.startswith("Your check (charge on stripe: refund $5) is ok: go ahead.")


# --- owner messages ------------------------------------------------------------------------------


async def test_a_message_reaches_the_running_session(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, "--wait-message", "20")
    code = await start(h)
    loop = h.spawn_loop()
    await until(lambda: bool(cli_calls(h)), what="the first note")
    assert isinstance(h.runner.deliver_message(code, "also update the README"), Relayed)
    await loop
    calls = cli_calls(h)
    assert calls[-1]["out"].endswith("Message from the owner: also update the README")
    assert h.run(code).status == "done" and len(h.argv_log(code)) == 1
    assert isinstance(h.runner.deliver_message(code, "one more"), Refused)  # it's over
    assert isinstance(h.runner.deliver_message("R99", "hi"), Refused)


async def test_a_message_resumes_a_run_waiting_for_an_answer(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, "--ask", "Which DB?", ask_block_s=0.3)
    code = await start(h)
    await h.drain()
    assert h.run(code).wait_reason == "question"
    assert isinstance(h.runner.deliver_message(code, "use the staging account"), Relayed)
    await h.drain()
    # resumed with the message; the question is still open, so it waits again
    assert h.argv_log(code)[1]["prompt"].startswith(
        "Message from the owner: use the staging account"
    )
    assert h.run(code).status == "waiting" and h.run(code).wait_reason == "question"
    assert isinstance(h.runner.answer_question(code, "pg"), Relayed)
    await h.drain()
    assert h.run(code).status == "done" and len(h.argv_log(code)) == 3


async def test_a_message_queued_before_the_session_opens_its_first_turn(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db)
    code = await start(h)
    assert isinstance(h.runner.deliver_message(code, "keep it short"), Relayed)
    await h.drain()
    [call] = h.argv_log(code)
    assert (
        call["prompt"] == "pick a database and set it up\n\nMessage from the owner: keep it short"
    )


# --- cancel, kill ---------------------------------------------------------------------------------


async def test_cancel_while_waiting_for_an_answer(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = rig(tmp_path, db, "--ask", "Which DB?", ask_block_s=0.3)
    code = await start(h)
    await h.drain()
    assert h.run(code).wait_reason == "question"
    assert not isinstance(await h.runner.cancel_run(code), Refused)
    run = h.run(code)
    assert run.status == "cancelled" and run.wait_reason is None
    assert [q.status for q in qs.for_run(db, code)] == ["cancelled"]
    assert h.runner.open_questions() == []
    assert isinstance(h.runner.answer_question(code, "pg"), Refused)
    assert not (h.runner.ctl_root / code).exists()


async def test_kill_while_the_cli_waits(tmp_path: Path, db: sqlite3.Connection) -> None:
    from resonant import killswitch

    h = rig(tmp_path, db, "--ask", "Which DB?", ask_block_s=30)
    code = await start(h)
    loop = h.spawn_loop()
    await until(lambda: bool(h.runner.open_questions()), what="the question")
    killswitch.engage(db, actor="test")
    await loop
    assert h.run(code).status == "cancelled"
    assert [q.status for q in qs.for_run(db, code)] == ["cancelled"]
    assert not (h.runner.ctl_root / code).exists()


# --- the shim, the socket and its checks ----------------------------------------------------------


async def test_the_shim_and_control_dir(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = rig(tmp_path, db, "--note", "x")
    code = await start(h)
    await h.drain()
    shim = h.code_dir(code) / "bin" / "resonant"
    first = shim.read_text().splitlines()[0]
    assert first.startswith("#!/") and first.endswith(" -IS")
    venv = Path(os.path.realpath(sys.prefix))  # noqa: ASYNC240
    assert not Path(first[2:].split()[0]).is_relative_to(venv)
    assert stat.S_IMODE(shim.stat().st_mode) == 0o700
    call = h.argv_log(code)[0]
    assert call["env"]["PATH"].split(":")[0] == str(h.code_dir(code) / "bin")
    assert call["env"]["BASH_DEFAULT_TIMEOUT_MS"] == call["env"]["BASH_MAX_TIMEOUT_MS"]
    prompt = call["argv"][call["argv"].index("--append-system-prompt") + 1]
    assert "resonant ask" in prompt and "resonant check" in prompt
    assert not (h.runner.ctl_root / code).exists()  # gone with the run
    out = await asyncio.to_thread(
        subprocess.run, [str(shim), "note", "after"], capture_output=True, text=True
    )
    assert out.returncode == 4 and "can't be reached" in out.stdout
    usage = await asyncio.to_thread(subprocess.run, [str(shim), "ask"], capture_output=True)
    assert usage.returncode == 2


def test_parse_request_validates() -> None:
    assert parse_request({"cmd": "ask", "question": " q ", "options": ["a"]}) == Ask("q", ("a",))
    bads: list[Any] = [
        {"cmd": "ask", "question": ""},
        {"cmd": "ask", "question": "q", "options": "a,b"},
        {"cmd": "ask", "question": "x" * 5000},
        {"cmd": "check", "system": "s", "action": "a", "what": "w", "cost_usd": -1},
        {"cmd": "check", "system": "s", "action": "a", "what": "w", "cost_usd": float("nan")},
        {"cmd": "note", "text": "n", "urgent": "yes"},
        {"cmd": "rm"},
        [],
    ]
    for bad in bads:
        with pytest.raises(worker_cli.BadRequest):
            parse_request(bad)


def test_descends_from_needs_the_same_leader() -> None:
    import psutil

    me = psutil.Process()
    parent = me.parent()
    assert parent is not None
    assert descends_from(me.pid, parent.pid, parent.create_time())
    assert descends_from(me.pid, me.pid, me.create_time())
    assert not descends_from(me.pid, parent.pid, parent.create_time() - 100)  # a reused pid
    assert not descends_from(parent.pid, me.pid, me.create_time())


async def _request(path: Path, obj: dict[str, Any]) -> dict[str, Any]:
    def call() -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            here = os.getcwd()
            os.chdir(path.parent)
            try:
                s.connect(path.name)
            finally:
                os.chdir(here)
            s.sendall(json.dumps(obj).encode() + b"\n")
            return json.loads(s.makefile().readline())

    return await asyncio.to_thread(call)


async def test_the_socket_checks_token_and_caller(tmp_path: Path) -> None:
    ctl = worker_cli.make_ctl_dir(tmp_path / "w", "R1")
    token = worker_cli.write_token(ctl)
    assert stat.S_IMODE((ctl / "token").stat().st_mode) == 0o600
    assert stat.S_IMODE(ctl.stat().st_mode) == 0o700
    seen: list[Request] = []
    allowed = {"ok": True}

    async def handle(req: Request, gone: asyncio.Future[None]) -> Reply:
        seen.append(req)
        return Reply("fine")

    server = WorkerServer(ctl, token, handle, lambda pid: allowed["ok"] and pid == os.getpid())
    await server.start()
    try:
        ask = {"cmd": "ask", "question": "q"}
        assert await _request(ctl / "sock", {**ask, "token": token}) == {"text": "fine", "exit": 0}
        bad = await _request(ctl / "sock", {**ask, "token": token[:-1] + "x"})
        assert bad == {"text": "Not allowed.", "exit": EXIT_UNREACHABLE}
        allowed["ok"] = False  # another process (another run's session) with the right token
        assert (await _request(ctl / "sock", {**ask, "token": token}))["exit"] == EXIT_UNREACHABLE
        assert len(seen) == 1
    finally:
        await server.close()
    assert not (ctl / "sock").exists()
    worker_cli.remove_ctl(tmp_path / "w", "R1")
    assert not ctl.exists()


def test_a_planted_control_dir_link_is_refused(tmp_path: Path) -> None:
    work = tmp_path / "w"
    (work / ".ctl").mkdir(parents=True)
    (work / ".ctl" / "R1").symlink_to(tmp_path)
    with pytest.raises(OSError, match="private directory"):
        worker_cli.make_ctl_dir(work, "R1")


async def test_a_crash_during_a_delivery_turn_resends_it(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db)
    code = await start(h)
    runs_store.update(db, code, resume_prompt="The owner answered your question: pg")
    db.execute("UPDATE runs SET sessions = 1 WHERE code = ?", (code,))
    qs.ask(db, code, "ask", "And the region?", at=utcnow())
    prompt = h.runner._turn_prompt(h.run(code))  # pyright: ignore[reportPrivateUsage]
    assert prompt.startswith("Your session was interrupted.")
    assert "What you were told last turn:\nThe owner answered your question: pg" in prompt
    assert 'You asked the owner: "And the region?". There is no answer yet' in prompt

"""Live worker sessions and Remote Control (RC-1): stream-json input with stdin kept open,
owner messages and late answers written into the running session, Resonant ending each
session (Remote Control off, stdin closed, the kill if it won't go), and the
``remote_control`` control request's success, error and silence.

The fake CLI (``tests/fakes/claude``) speaks the input protocol: it stays alive after its
result until stdin closes (``sticky``: not even then), answers the control request per
``--rc`` and notes every user message in ``turns.json`` and every control request in
``rc.json``."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import psutil
import pytest
from test_runner_claude import Harness, build, until
from test_worker_cli import cli_calls, rig, start

from resonant.runners.claude import REMOTE_KV, StreamState
from resonant.runners.live import (
    ControlReply,
    LiveInput,
    parse_reply,
    session_url,
    short_reason,
)
from resonant.runners.worker_desk import Relayed
from resonant.store import questions as qs
from resonant.store.db import kv_get

FAKE_URL = "https://claude.ai/code/session_fake"


def remote_on(h: Harness) -> None:
    h.runner.cfg = h.cfg.model_copy(update={"remote_control": "on"})


def turns(h: Harness, code: str = "R1") -> list[dict[str, Any]]:
    path = h.tmp(code) / "turns.json"
    return json.loads(path.read_text()) if path.exists() else []


def rc_calls(h: Harness, code: str = "R1") -> list[dict[str, Any]]:
    path = h.tmp(code) / "rc.json"
    return json.loads(path.read_text()) if path.exists() else []


def results(h: Harness, code: str = "R1") -> list[dict[str, Any]]:
    path = h.runner.transcript_dir / f"{code}.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    return [m for m in lines if m.get("type") == "result"]


def audits(h: Harness, action: str) -> list[dict[str, Any]]:
    rows = h.conn.execute("SELECT detail FROM audit_log WHERE action = ?", (action,)).fetchall()
    return [json.loads(r[0]) for r in rows]


# --- the input protocol ---------------------------------------------------------------------


async def test_the_prompt_is_a_user_line_and_the_session_is_named(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db)
    code = await start(h)
    await h.drain()
    [call] = h.argv_log(code)
    argv = call["argv"]
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("-n") + 1] == code
    assert call["prompt"] == "pick a database and set it up"
    assert [t["text"] for t in turns(h, code)] == ["pick a database and set it up"]
    assert rc_calls(h, code) == []  # remote_control off: nothing asked
    assert h.run(code).status == "done"


async def test_a_session_that_waits_on_open_stdin_is_ended_after_its_result(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """The fake (like the real CLI) never exits on its own while stdin is open: Resonant
    closes it after the result, well inside the end grace."""
    h = rig(tmp_path, db, end_grace_s=60.0)
    code = await start(h)
    began = time.monotonic()
    await h.drain()
    assert time.monotonic() - began < 30
    run = h.run(code)
    assert run.status == "done" and run.exit_code == 0 and run.pgid is None


async def test_a_session_that_never_exits_is_killed_after_the_end_grace(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="sticky", end_grace_s=0.5)
    h.runner._submit = h.loop.submit  # pyright: ignore[reportPrivateUsage]
    code = await start(h)
    await h.drain()
    run = h.run(code)
    assert run.status == "done", run.failure  # the result counts, not the kill's exit status
    assert (run.summary or "").startswith("Checked the disk")
    assert run.exit_code != 0 and run.pgid is None
    pid = h.pid_of(code, "claude.pid")
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


# --- live input --------------------------------------------------------------------------------


async def test_a_message_is_written_into_the_live_session(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, "--wait-message", "20")
    code = await start(h)
    loop = h.spawn_loop()
    await until(lambda: bool(cli_calls(h)), what="the first note")
    assert isinstance(h.runner.deliver_message(code, "also update the README"), Relayed)
    await loop
    folded = [t for t in turns(h, code) if t["folded"]]
    assert [t["text"] for t in folded] == ["Message from the owner: also update the README"]
    assert len(h.argv_log(code)) == 1  # no resume
    assert qs.pending_messages(db, code) == []
    assert audits(h, "run.live_input") == [{"run": code}]
    assert h.run(code).status == "done"


async def test_an_answer_after_the_cli_gave_up_reaches_the_live_session(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, "--ask", "Which DB?", "--linger", "20", ask_block_s=0.3)
    code = await start(h)
    loop = h.spawn_loop()
    await until(lambda: bool(cli_calls(h)), what="the ask giving up")
    assert cli_calls(h)[0]["rc"] == 3
    assert isinstance(h.runner.answer_question(code, "pg"), Relayed)
    await loop
    assert h.run(code).status == "done"
    assert len(h.argv_log(code)) == 1  # answered in the live session, not by a resume
    [_prompt, answer] = turns(h, code)
    assert answer["folded"] and answer["text"].startswith(
        'The owner answered your question "Which DB?": pg'
    )
    [q] = qs.for_run(db, code)
    assert q.delivered_at is not None


async def test_a_turn_resonant_did_not_start_is_tolerated(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, "--owner-turn", "how's it going?")
    code = await start(h)
    await h.drain()
    run = h.run(code)
    assert run.status == "done"
    assert len(results(h, code)) == 2  # its own turn and the owner's, in one process
    assert len(h.argv_log(code)) == 1


async def test_a_question_still_open_at_the_turn_end_parks_the_run(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """The fallback: no answer yet when the turn ends, so the session ends and the run waits
    with its slot freed; the answer resumes the session with --resume."""
    h = rig(tmp_path, db, "--ask", "Which DB?", ask_block_s=0.3)
    code = await start(h)
    await h.drain()
    run = h.run(code)
    assert run.status == "waiting" and run.wait_reason == "question"
    assert h.runner._inputs == {}  # pyright: ignore[reportPrivateUsage]
    assert isinstance(h.runner.answer_question(code, "pg"), Relayed)
    await h.drain()
    assert h.run(code).status == "done" and len(h.argv_log(code)) == 2
    assert "--resume" in h.argv_log(code)[1]["argv"]


# --- the turn end, piece by piece ------------------------------------------------------------


class Stdin:
    """An ``asyncio.StreamWriter`` stand-in that keeps what was written."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise BrokenPipeError
        self.data += data

    async def drain(self) -> None: ...

    def close(self) -> None:
        self.closed = True

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.data.splitlines()]


async def _turn_end(h: Harness, code: str, result: dict[str, Any]) -> tuple[Stdin, LiveInput]:
    stdin = Stdin()
    live = LiveInput(stdin)  # pyright: ignore[reportArgumentType]
    state = StreamState()
    await h.runner._turn_ended(h.run(code), live, state, result, asyncio.Event())  # pyright: ignore[reportPrivateUsage]
    return stdin, live


async def test_the_turn_end_sends_what_the_run_has_not_seen(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db)
    code = await start(h)
    qs.add_message(db, code, "use sqlite", at=h.runner.clock())
    stdin, live = await _turn_end(h, code, {"type": "result", "subtype": "success"})
    [line] = stdin.lines()
    assert line["type"] == "user"
    assert line["message"]["content"] == (
        "Message from the owner: use sqlite\n\nCarry on with the task."
    )
    assert live.open and not stdin.closed
    assert qs.pending_messages(db, code) == []
    assert h.run(code).resume_prompt == "Message from the owner: use sqlite"  # resent on a crash


async def test_a_turn_end_with_a_message_written_since_its_result_leaves_it_open(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """A message written after the result was read starts another turn: that turn's result
    decides, so the session isn't closed under it."""
    h = rig(tmp_path, db)
    code = await start(h)
    stdin = Stdin()
    live = LiveInput(stdin)  # pyright: ignore[reportArgumentType]
    async with live.lock:
        await live.write_user("prompt")
    closed = asyncio.Event()
    await h.runner._turn_ended(h.run(code), live, StreamState(), {"type": "result"}, closed, sent=0)  # pyright: ignore[reportPrivateUsage]
    assert live.open and not stdin.closed and not closed.is_set()
    await h.runner._turn_ended(h.run(code), live, StreamState(), {"type": "result"}, closed, sent=1)  # pyright: ignore[reportPrivateUsage]
    assert stdin.closed and closed.is_set()


async def test_the_turn_end_with_nothing_to_send_ends_the_session(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db)
    code = await start(h)
    stdin, live = await _turn_end(h, code, {"type": "result", "subtype": "success"})
    assert stdin.data == b"" and stdin.closed and not live.open
    # an error result ends it even with news waiting (it goes to the next session instead)
    qs.add_message(db, code, "use sqlite", at=h.runner.clock())
    stdin, live = await _turn_end(h, code, {"type": "result", "is_error": True})
    assert stdin.data == b"" and stdin.closed
    assert len(qs.pending_messages(db, code)) == 1


async def test_ending_turns_remote_control_off_first() -> None:
    stdin = Stdin()
    live = LiveInput(stdin)  # pyright: ignore[reportArgumentType]
    live.remote = True
    await live.end(rc_timeout=0.05)  # no answer: best effort, it closes anyway
    [line] = stdin.lines()
    assert line["type"] == "control_request"
    assert line["request"] == {"subtype": "remote_control", "enabled": False}
    assert stdin.closed and not live.open
    async with live.lock:
        assert not await live.write_user("too late")
    await live.end()  # idempotent


async def test_a_control_reply_resolves_its_request() -> None:
    stdin = Stdin()
    live = LiveInput(stdin)  # pyright: ignore[reportArgumentType]
    async with live.lock:
        fut = await live.control({"subtype": "remote_control", "enabled": True})
    assert fut is not None
    rid = stdin.lines()[0]["request_id"]
    ok = {"subtype": "success", "request_id": rid, "response": {"session_url": FAKE_URL + "1"}}
    assert live.feed({"type": "control_response", "response": ok})
    reply = await live.reply(fut, 1)
    assert reply is not None and session_url(reply) == FAKE_URL + "1"
    assert not live.feed({"type": "assistant"})


def test_control_replies_are_parsed_strictly() -> None:
    err = {"subtype": "error", "request_id": "r1", "error": "  Remote Control\nis disabled.  "}
    assert parse_reply({"type": "control_response", "response": err}) == (
        "r1",
        ControlReply(False, {}, "Remote Control is disabled"),
    )
    assert parse_reply({"type": "control_response", "response": "x"}) is None
    assert parse_reply({"type": "result"}) is None
    assert short_reason("") == "an error with no reason"
    assert len(short_reason("x" * 500)) == 80
    for bad in ("https://evil.example/code/session_1", "https://claude.ai/code/a b", 7):
        assert session_url(ControlReply(True, {"session_url": bad})) is None


# --- Remote Control ----------------------------------------------------------------------------


async def test_remote_control_link_is_stored_and_texted_once(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, "--ask", "Which DB?", ask_block_s=0.3)
    remote_on(h)
    code = await start(h)
    await h.drain()
    run = h.run(code)
    assert run.wait_reason == "question"
    assert run.remote_url is None  # the remote session ended with the process
    first = FAKE_URL + str(h.argv_log(code)[0]["pid"])
    texts = [t for _who, t in h.chan.sent if "Remote Control" in t]
    assert texts == [f"{code} Remote Control: {first}"]
    calls = rc_calls(h, code)
    assert [c["request"]["enabled"] for c in calls] == [True, False]  # off at the end
    assert audits(h, "run.remote_control") == [{"run": code, "ok": True, "reason": None}]
    seen = kv_get(db, REMOTE_KV)
    assert isinstance(seen, dict) and seen["state"] == "ok" and seen["run"] == code

    assert isinstance(h.runner.answer_question(code, "pg"), Relayed)
    await h.drain()
    run = h.run(code)
    assert run.status == "done"
    urls = [
        line["response"]["response"]["session_url"]
        for line in map(
            json.loads, (h.runner.transcript_dir / f"{code}.jsonl").read_text().splitlines()
        )
        if line.get("type") == "control_response" and line["response"]["response"]
    ]
    assert urls == [first, FAKE_URL + str(h.argv_log(code)[1]["pid"])]  # the resume's own
    assert len([t for _who, t in h.chan.sent if "Remote Control" in t]) == 1  # told once


async def test_the_remote_url_is_stored_while_the_session_lives(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="hang", max_session_s=3)
    remote_on(h)
    code = await h.start()
    loop = h.spawn_loop()
    await until(lambda: h.run(code).remote_url is not None, what="the remote session")
    assert h.run(code).remote_url == FAKE_URL + h.tmp(code).joinpath("claude.pid").read_text()
    act = h.runner.run_activity(code)
    assert act is not None and act["remote_url"] == h.run(code).remote_url
    await loop
    assert h.run(code).remote_url is None


async def test_kill_does_not_wait_for_remote_control_off(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="hang", extra=["--rc", "none"], rc_off_timeout_s=20.0)
    remote_on(h)
    code = await h.start()
    loop = h.spawn_loop()
    await until(lambda: bool(rc_calls(h, code)), what="the remote control request")
    began = time.monotonic()
    await h.runner.cancel_run(code)
    await loop
    assert h.run(code).status == "cancelled"
    assert time.monotonic() - began < 10  # not the 20 s polite wait


async def test_remote_control_refused_leaves_the_run_going(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, "--rc", "error", "--sleep", "1")
    remote_on(h)
    code = await start(h)
    await h.drain()
    run = h.run(code)
    assert run.status == "done" and run.remote_url is None
    assert f"{code}: Remote Control unavailable: Remote Control requires a claude.ai login." in [
        t for _who, t in h.chan.sent
    ]
    [entry] = audits(h, "run.remote_control")
    assert entry["ok"] is False
    seen = kv_get(db, REMOTE_KV)
    assert isinstance(seen, dict) and seen["state"] == "unavailable"
    assert [c["request"]["enabled"] for c in rc_calls(h, code)] == [True]  # nothing to turn off


async def test_remote_control_with_no_answer_times_out(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, "--rc", "none", "--sleep", "1.5", rc_timeout_s=0.3,
            rc_off_timeout_s=0.2)  # fmt: skip
    remote_on(h)
    code = await start(h)
    await h.drain()
    assert h.run(code).status == "done"
    assert f"{code}: Remote Control unavailable: no answer in 0.3s." in [
        t for _who, t in h.chan.sent
    ]


async def test_a_timeout_turns_remote_control_off_before_the_kill(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """Resonant ends a session it stops too (a timeout here; /cancel and /kill take the same
    path, but delete the run dir this test reads)."""
    h = build(tmp_path, db, scenario="hang", max_session_s=2)
    remote_on(h)
    code = await h.start()
    await h.drain()
    run = h.run(code)
    assert run.status == "failed" and "timed out" in (run.failure or "")
    assert [c["request"]["enabled"] for c in rc_calls(h, code)] == [True, False]
    assert run.pgid is None


@pytest.mark.parametrize("mode", ["off", False])
def test_remote_control_config(mode: object) -> None:
    from resonant.config import RunnerConfig

    assert RunnerConfig().remote_control == "on"
    assert RunnerConfig.model_validate({"remote_control": mode}).remote_control == "off"
    assert RunnerConfig.model_validate({"guard": False}).guard == "off"  # YAML's bare `off`

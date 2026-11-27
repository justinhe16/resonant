"""PER-319: the thread is kept in the loop, end to end on the fake Slack channel.

The real daemon (router, gate, loop, ``ClaudeRunner``) as in ``test_e2e_team``, over
``e2e.FakeChannel("slack", vouch=True)`` only, with a team of three: ``lead`` (owner),
``second`` (approver) and ``member`` (may request ``ops.prod_deploy``, an L1 write: it
always asks).

- One approver's DM failing with a non-refused error doesn't fail the ask: the other copy
  stands, the task waits, and ``approval.send_failed`` names the failed send.
- An approval nobody got (every send failed) ends in one ``Couldn't finish that: …`` line in
  the asking thread; so does a denial. A run's failure is said once, by its end text (PER-347).
- A member's ask from a DM says who it waits on (the prompt isn't in their conversation).
- A run the brain starts says ``On it: starting R1 (…).`` in its thread once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from test_e2e_runner import (
    REPO,
    FakeChatDB,
    RunModel,
    World,
    attach_claude,
    chat,
    make_world,
)
from test_runner_claude import until

from resonant.config import ChannelsConfig
from resonant.daemon import Daemon
from resonant.e2e import FakeChannel
from resonant.e2e.extensions import install_fixture
from resonant.gateway.channel import ChannelSendError
from resonant.ops.fakes import FakeOpsRunner
from resonant.principals import load_principals
from resonant.secrets import SecretStore
from resonant.store.db import open_db

__all__ = ["chat"]  # the fixture

LEAD, SECOND, MEMBER = "U1", "U2", "U3"
TEAM = f"""\
principals:
  lead: {{role: owner, identities: ["slack:{LEAD}"]}}
  second: {{role: approver, identities: ["slack:{SECOND}"]}}
  member:
    identities: ["slack:{MEMBER}"]
    grants: {{ops: {{request: [prod_deploy]}}}}
"""
OPS = """\
name: ops
version: 1
description: Deploys (a test fixture).
tools:
  prod_deploy:
    description: Deploy to production.
    run: ./cli.py deploy {service}
    effect: write
    level: L1
    args: {service: string}
"""
OPS_CLI = r"""
import json, sys
print(json.dumps({"op": sys.argv[1], "arg": sys.argv[2], "ok": True}))
"""
TAB_OFF = "slack: chat.postMessage failed: messages_tab_disabled"
CHANNEL_THREAD = "C0TEAM:1700000000.000100"
CODE = re.compile(r"^\[([A-Z0-9]+)\]")


@contextlib.asynccontextmanager
async def team(
    tmp_path: Path,
    home: Path,
    chat: FakeChatDB,
    mp: pytest.MonkeyPatch,
    *,
    scenario: str = "happy",
    max_attempts: int | None = None,
) -> AsyncGenerator[tuple[World, Daemon, FakeChannel]]:
    """The daemon over the fake Slack adapter only (iMessage off), with the team and ops."""
    w = make_world(tmp_path, home, chat, mp, scenario=scenario)
    (home / "principals.yaml").write_text(TEAM)
    update: dict[str, Any] = {"channels": ChannelsConfig()}
    if max_attempts is not None:
        update["loop"] = w.settings.loop.model_copy(update={"max_attempts": max_attempts})
    w.settings = w.settings.model_copy(update=update)
    conn = open_db(w.settings.db_path)
    try:
        install_fixture(w.settings, conn, OPS, {"cli.py": OPS_CLI})
    finally:
        conn.close()
    fake = FakeChannel("slack", load_principals(home / "principals.yaml"), vouch=True)
    daemon = w.daemon = Daemon(
        w.settings,
        model=RunModel(),
        osa_runner=w.osa,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(w.keychain),
        channels={"slack": fake},
    )
    attach_claude(w, daemon)
    task = asyncio.create_task(daemon.run())
    assert daemon.stack is not None
    hub = daemon.stack.channel
    await until(lambda: hub.health()["last_read_at"] is not None, 10, "the channels to read")
    try:
        yield w, daemon, fake
    finally:
        if not task.done():
            daemon.stop()
        assert await asyncio.wait_for(task, 20) == 0
        w.daemon = None


def run_task_ended(d: Daemon) -> bool:
    """The run's task has ended: anything it says after its end text is out."""
    row = d.conn.execute("SELECT status FROM tasks WHERE runner = 'claude'").fetchone()
    return row is not None and row["status"] in ("done", "failed", "cancelled")


def in_thread(fake: FakeChannel, thread: str, start: str) -> list[str]:
    return [m.text for m in fake.sent if m.thread == thread and m.text.startswith(start)]


async def clear_notes(fake: FakeChannel, uid: str, thread: str) -> None:
    """``/notes clear`` (an L1 builtin: it always asks the owner and the approver), with a
    note to clear."""
    n = len(fake.sent)
    assert fake.inject(uid, "/note buy milk", thread=thread)
    await until(lambda: len(fake.sent) > n, 20, "the note")
    assert fake.inject(uid, "/notes clear", thread=thread)


async def prompt_code(fake: FakeChannel) -> str:
    await until(lambda: any(CODE.match(m.text) for m in fake.sent), 20, "an approval prompt")
    m = next(CODE.match(m.text) for m in fake.sent if CODE.match(m.text))
    assert m is not None
    return m.group(1)


def task_of(d: Daemon, code: str) -> dict[str, Any]:
    row = d.conn.execute(
        "SELECT t.id, t.status FROM approvals a JOIN tasks t ON t.id = a.task_id WHERE a.code = ?",
        (code,),
    ).fetchone()
    assert row is not None
    return {"id": row["id"], "status": row["status"]}


async def test_one_approvers_failed_dm_doesnt_fail_the_ask(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the owner asks in their DM (no shared thread): their copy goes there, the approver's
    # to a DM the Slack app can't post (Messages tab off)
    async with team(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        fake.fail_approval["second"] = ChannelSendError(TAB_OFF)
        dm = "D0LEAD:1700000000.000200"
        await clear_notes(fake, LEAD, dm)
        code = await prompt_code(fake)
        await until(lambda: task_of(d, code)["status"] == "waiting", 20, "the ask to wait")
        prompts = [m for m in fake.sent if m.text.startswith(f"[{code}]")]
        assert [(m.principal, m.thread) for m in prompts] == [("lead", dm)]
        rows = d.conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'approval.send_failed' AND task_id = ?",
            (task_of(d, code)["id"],),
        ).fetchall()
        assert rows, "the failed send is audited"
        detail = json.loads(rows[0]["detail"])
        assert detail["approver"] == "second" and "messages_tab_disabled" in detail["error"]
        assert not [m for m in fake.sent if m.text.startswith("Couldn't finish that")]
        assert not [m for m in fake.sent if m.text.startswith("Waiting on")]  # it's there


async def test_an_approval_nobody_got_says_why_in_the_thread_once(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with team(tmp_path, resonant_home, chat, monkeypatch, max_attempts=1) as (_w, d, fake):
        fake.fail_approval.update(lead=ChannelSendError(TAB_OFF), second=ChannelSendError(TAB_OFF))
        await clear_notes(fake, LEAD, CHANNEL_THREAD)
        want = "Couldn't finish that: I couldn't send the approval (Slack: Messages tab off)."
        await until(lambda: bool(in_thread(fake, CHANNEL_THREAD, "Couldn't")), 20, "the line")
        row = d.conn.execute(
            "SELECT status FROM tasks WHERE runner = 'local' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "failed"
        assert in_thread(fake, CHANNEL_THREAD, "Couldn't") == [want]
        (sent,) = [m for m in fake.sent if m.text == want]
        assert sent.principal == "lead"


async def test_a_denial_in_the_thread_is_one_failure_line(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with team(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        await clear_notes(fake, LEAD, CHANNEL_THREAD)
        code = await prompt_code(fake)
        await until(lambda: task_of(d, code)["status"] == "waiting", 20, "the ask to wait")
        assert fake.inject(SECOND, f"no {code}", thread=CHANNEL_THREAD)
        await until(lambda: task_of(d, code)["status"] == "done", 20, "the ask to end")
        await until(lambda: bool(in_thread(fake, CHANNEL_THREAD, "Couldn't")), 20, "the line")
        assert in_thread(fake, CHANNEL_THREAD, "Couldn't") == [
            "Couldn't finish that: Second said no."
        ]
        # PER-328: the line is the reply: no "Not done." saying it a second time
        assert not in_thread(fake, CHANNEL_THREAD, "Not done")


async def test_a_members_ask_from_a_dm_says_who_it_waits_on(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with team(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        dm = "D0MEMBER:1700000000.000300"
        assert fake.inject(MEMBER, "/ops prod_deploy api", thread=dm)
        code = await prompt_code(fake)
        await until(lambda: task_of(d, code)["status"] == "waiting", 20, "the ask to wait")
        await until(lambda: bool(in_thread(fake, dm, "Waiting")), 20, "the waiting line")
        assert in_thread(fake, dm, "Waiting") == ["Waiting on an approval from Lead or Second."]
        prompts = [m for m in fake.sent if m.text.startswith(f"[{code}]")]
        assert {m.principal for m in prompts} == {"lead", "second"}
        assert all(m.thread is None for m in prompts)


async def test_a_run_the_brain_started_says_so_once(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with team(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        assert d.stack is not None and d.stack.claude is not None
        d.stack.claude.milestone_quiet_s = 0  # PER-325: the line waits for the quiet window
        ref = {"channel": "slack", "thread": CHANNEL_THREAD, "ref": "slack-in:9",
               "principal": "lead"}  # fmt: skip
        started = await d.stack.claude.start_run(REPO, "fix the typo in the README", "lead",
                                                 report="now", channel_ref=ref,
                                                 announce=True)  # fmt: skip
        assert getattr(started, "code", None) == "R1", started
        await until(lambda: any("opened PR #41" in m.text for m in fake.sent), 60, "the end")
        assert in_thread(fake, CHANNEL_THREAD, "On it") == [
            "On it: starting R1 (fix the typo in the README)."
        ]
        assert not in_thread(fake, CHANNEL_THREAD, "Couldn't")


async def test_a_run_shorter_than_the_quiet_window_has_only_its_end_text(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PER-325: the start line waits for the quiet window, so a quick run says it once, in
    its end text."""
    async with team(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        assert d.stack is not None and d.stack.claude is not None
        assert d.stack.claude.milestone_quiet_s >= 60
        ref = {"channel": "slack", "thread": CHANNEL_THREAD, "ref": "slack-in:9",
               "principal": "lead"}  # fmt: skip
        started = await d.stack.claude.start_run(REPO, "fix the typo in the README", "lead",
                                                 report="now", channel_ref=ref,
                                                 announce=True)  # fmt: skip
        assert getattr(started, "code", None) == "R1", started
        await until(lambda: any("opened PR #41" in m.text for m in fake.sent), 60, "the end")
        assert in_thread(fake, CHANNEL_THREAD, "On it") == []


async def test_a_failed_run_says_so_in_its_thread_once(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with team(tmp_path, resonant_home, chat, monkeypatch, scenario="error") as (
        _w,
        d,
        fake,
    ):
        assert d.stack is not None and d.stack.claude is not None
        ref = {"channel": "slack", "thread": CHANNEL_THREAD, "ref": "slack-in:9",
               "principal": "lead"}  # fmt: skip
        await d.stack.claude.start_run(REPO, "fix the typo", "lead", channel_ref=ref)
        await until(lambda: bool(in_thread(fake, CHANNEL_THREAD, "R1 ")), 60, "the end text")
        await until(lambda: run_task_ended(d), 30, "the run task to end")
        # PER-347: the end text is the one failure message; no Couldn't finish that under it
        (line,) = in_thread(fake, CHANNEL_THREAD, "R1 ")
        assert line.startswith("R1 (fix the typo) failed: "), line
        assert not in_thread(fake, CHANNEL_THREAD, "Couldn't")
        assert not in_thread(fake, CHANNEL_THREAD, "On it")  # not announced: /run's reply is

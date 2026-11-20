"""PER-293: a team on the fake Slack channel, end to end.

The real daemon (router, gate, loop, ``ClaudeRunner``, worker CLI) as in
``test_e2e_channels``, over ``e2e.FakeChannel("slack", vouch=True)`` only, with a team:

- justin (owner), brian (approver), miu (member: ``dori`` per-tool grants), and the workspace
  default (any unlisted ``slack:<id>`` the adapter vouches for: ``file_ticket`` only).
- A tiny ``dori`` fixture extension with ``file_ticket`` (L3 write) and ``prod_deploy``
  (L2 write).

Acceptance (the ticket's criteria):
- Brian starts a run, approves a gated builtin, and approves his own request.
- Miu and an unlisted workspace user can call ``file_ticket`` but not ``prod_deploy``;
  ``/run`` and ``/merge`` are refused with who can start work. Claude offers them only
  their tools.
- Brian can't use admin commands.
- Brian's "pause" pauses this instance (``approver:brian``), his "resume" resumes it; he
  can't resume a ``sibling:resonant`` pause; members can do neither.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from test_e2e_runner import REPO, FakeChatDB, RunModel, World, attach_claude, chat, make_world
from test_runner_claude import until

from resonant import killswitch
from resonant.config import ChannelsConfig
from resonant.daemon import Daemon
from resonant.e2e import FakeChannel
from resonant.e2e.extensions import install_fixture
from resonant.ops.fakes import FakeOpsRunner
from resonant.principals import load_principals
from resonant.router.fast_path import KILL_REPLY, NOT_ALLOWED, RESUME_REPLY
from resonant.secrets import SecretStore
from resonant.store.db import kv_get, open_db

__all__ = ["chat"]  # the fixture

JUSTIN, BRIAN, MIU, STRANGER = "U1", "U2", "U3", "U9"
TEAM = f"""\
principals:
  justin: {{role: owner, identities: ["slack:{JUSTIN}"]}}
  brian: {{role: approver, identities: ["slack:{BRIAN}"]}}
  miu:
    identities: ["slack:{MIU}"]
    grants: {{dori: {{request: [file_ticket, find_tickets]}}}}
default: {{channel: slack, grants: {{dori: {{request: [file_ticket]}}}}}}
"""
DORI = """\
name: dori
version: 1
description: DORI tickets and deploys (a test fixture).
tools:
  file_ticket:
    description: File a DORI ticket.
    run: ./cli.py file {title}
    effect: write
    level: L3
    args: {title: string}
  prod_deploy:
    description: Deploy DORI to production.
    run: ./cli.py deploy {service}
    effect: write
    level: L2
    args: {service: string}
"""
DORI_CLI = r"""
import json, sys
print(json.dumps({"op": sys.argv[1], "arg": sys.argv[2], "ok": True}))
"""
WORK_LINE = "Only Justin or Brian can start work. I can file a ticket for it instead."


@contextlib.asynccontextmanager
async def team(
    tmp_path: Path, home: Path, chat: FakeChatDB, mp: pytest.MonkeyPatch
) -> AsyncGenerator[tuple[World, Daemon, FakeChannel]]:
    """The daemon over the fake Slack adapter only (iMessage off), with the team and dori."""
    w = make_world(tmp_path, home, chat, mp, scenario="happy", runner_opts={"ask_block_s": 0.5})
    (home / "principals.yaml").write_text(TEAM)
    w.settings = w.settings.model_copy(update={"channels": ChannelsConfig()})
    conn = open_db(w.settings.db_path)
    try:
        install_fixture(w.settings, conn, DORI, {"cli.py": DORI_CLI})
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


class Slack:
    """Talk to the daemon as one Slack user; ``said`` waits for the next text sent to them."""

    def __init__(self, fake: FakeChannel, uid: str, principal: str) -> None:
        self.fake, self.uid, self.principal = fake, uid, principal
        self.seen = 0

    def texts(self) -> list[str]:
        return [m.text for m in self.fake.sent if m.principal == self.principal]

    async def next(self, what: str) -> str:
        n = self.seen + 1
        await until(lambda: len(self.texts()) >= n, 60, what)
        self.seen = n
        return self.texts()[n - 1]

    async def say(self, text: str, *, thread: str | None = None) -> str:
        assert self.fake.inject(self.uid, text, thread=thread), f"{self.uid} wasn't submitted"
        return await self.next(f"the reply to {text!r}")


async def test_members_file_tickets_but_never_deploy_or_start_work(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with team(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        for uid, name in ((MIU, "miu"), (STRANGER, f"slack:{STRANGER}")):
            user = Slack(fake, uid, name)
            if uid == STRANGER:
                user.seen = len(user.texts())
            filed = await user.say('/dori file_ticket "login is broken"')
            assert filed != NOT_ALLOWED and "file" in filed.lower(), filed
            assert await user.say("/dori prod_deploy api") == NOT_ALLOWED
            assert await user.say(f"/run {REPO} fix the login bug") == WORK_LINE
            assert await user.say("/merge R1") == WORK_LINE
            assert await user.say("/kill") == NOT_ALLOWED
            assert await user.say("/resume") == NOT_ALLOWED
            # Claude offers them only what they may request (no start_run, no prod_deploy)
            assert d.stack is not None and d.stack.brain is not None
            names = {(s.extension, s.name) for s in d.stack.brain.tools_for(name)}
            assert ("dori", "file_ticket") in names and ("dori", "prod_deploy") not in names
            assert (None, "start_run") not in names and (None, "merge_run") not in names
        assert kv_get(d.conn, "paused") is False
        rows = d.conn.execute(
            "SELECT tool, decision FROM audit_log WHERE action = 'gate.evaluate' "
            "AND extension = 'dori' ORDER BY id"
        ).fetchall()
        assert [(r["tool"], r["decision"]) for r in rows] == [("file_ticket", "Allow")] * 2


async def test_brian_starts_a_run_and_approves_his_own_gated_builtin(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with team(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        brian = Slack(fake, BRIAN, "brian")
        started = await brian.say(f"/run {REPO} fix the typo in the README")
        assert started.startswith("On it: R1"), started
        await until(
            lambda: d.conn.execute("SELECT 1 FROM runs WHERE code = 'R1'").fetchone() is not None,
            20,
            "the run row",
        )
        # A gated builtin: clearing notes is L1 and always asks, of justin and brian.
        assert await brian.say("/note buy milk") == 'Saved note 1: "buy milk"'
        prompt = ""
        fake.inject(BRIAN, "/notes clear")
        while not prompt.startswith("["):
            prompt = await brian.next("brian's approval prompt")
        code = re.match(r"^\[([A-Z0-9]+)\]", prompt)
        assert code is not None
        (row,) = d.conn.execute(
            "SELECT approvers, requested_by FROM approvals WHERE code = ?", (code.group(1),)
        ).fetchall()
        assert '"brian"' in row["approvers"] and '"justin"' in row["approvers"]
        justin_texts = [m.text for m in fake.sent if m.principal == "justin"]
        assert any(t.startswith(f"[{code.group(1)}]") for t in justin_texts)  # asked too
        fake.inject(BRIAN, f"yes {code.group(1)}")
        await until(
            lambda: (
                d.conn.execute(
                    "SELECT status FROM approvals WHERE code = ? AND status = 'approved'",
                    (code.group(1),),
                ).fetchone()
                is not None
            ),
            20,
            "brian's own approval",
        )
        (decided,) = d.conn.execute(
            "SELECT decided_by FROM approvals WHERE code = ?", (code.group(1),)
        ).fetchall()
        assert decided["decided_by"] == "brian"


async def test_brian_has_no_admin_and_pauses_this_instance_only(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with team(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        brian = Slack(fake, BRIAN, "brian")
        for admin in ("/update", "/ext list", "/ext run dori/x", "/pulse"):
            assert await brian.say(admin) == NOT_ALLOWED, admin

        assert await brian.say("pause") == KILL_REPLY
        assert kv_get(d.conn, "paused") is True and kv_get(d.conn, "autonomy") == "off"
        assert killswitch.paused_by(d.conn) == "approver:brian"
        miu = Slack(fake, MIU, "miu")
        assert await miu.say("/resume") == NOT_ALLOWED
        assert kv_get(d.conn, "paused") is True
        assert await brian.say("resume") == RESUME_REPLY
        assert kv_get(d.conn, "paused") is False and killswitch.paused_by(d.conn) is None

        killswitch.engage(d.conn, actor=killswitch.SIBLING_SOURCE)  # personal's /kill
        assert await brian.say("resume") == (
            "Justin paused everything from personal Resonant; he resumes it there."
        )
        assert kv_get(d.conn, "paused") is True
        assert killswitch.paused_by(d.conn) == killswitch.SIBLING_SOURCE

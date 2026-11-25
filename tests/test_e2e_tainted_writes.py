"""PER-335 end to end on the fake Slack channel: a team instance with
``gate.tainted_writes: allow`` runs on its own.

The real daemon (router, gate, loop, the Claude brain over ``e2e/fake_claude.py``,
``ClaudeRunner``) as in ``test_e2e_status_lines``. The owner says "robot-surf ..." in a team
thread; Claude reads a page first (the turn is tainted), then calls ``start_run``. With
``allow`` the run starts with no prompt and its start line is posted in the thread; with the
default ``ask`` the same turn asks, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from test_e2e_runner import REPO, FakeChatDB, RunModel, World, attach_claude, chat, make_world
from test_runner_claude import until

from resonant.config import ChannelsConfig, GateConfig
from resonant.daemon import Daemon
from resonant.e2e import FakeChannel
from resonant.ops.fakes import FakeOpsRunner
from resonant.principals import load_principals
from resonant.secrets import SecretStore

__all__ = ["chat"]  # the fixture

LEAD, SECOND = "U1", "U2"
TEAM = f"""\
principals:
  lead: {{role: owner, identities: ["slack:{LEAD}"]}}
  second: {{role: approver, identities: ["slack:{SECOND}"]}}
"""
THREAD = "C0TEAM:1700000000.000100"
PROMPT = re.compile(r"\[[A-Z0-9]+\] yes / no")
ASK = "fix the typo in the README"
START = {"name": "start_run", "args": {"repo": REPO, "ask": ASK}, "after_read": True}


@contextlib.asynccontextmanager
async def team(
    tmp_path: Path, home: Path, chat: FakeChatDB, mp: pytest.MonkeyPatch, *, tainted: str
) -> AsyncGenerator[tuple[World, Daemon, FakeChannel]]:
    """The daemon over the fake Slack adapter only, with ``gate.tainted_writes``."""
    w = make_world(tmp_path, home, chat, mp, scenario="happy")
    (home / "principals.yaml").write_text(TEAM)
    gate = GateConfig.model_validate({**w.settings.gate.model_dump(), "tainted_writes": tainted})
    update: dict[str, Any] = {"channels": ChannelsConfig(), "gate": gate}
    w.settings = w.settings.model_copy(update=update)
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


def in_thread(fake: FakeChannel, start: str) -> list[str]:
    return [m.text for m in fake.sent if m.thread == THREAD and m.text.startswith(start)]


def start_runs(d: Daemon) -> list[dict[str, Any]]:
    rows = d.conn.execute(
        "SELECT decision, json_extract(detail, '$.origin') AS origin,"
        " json_extract(detail, '$.tainted_allowed') AS lifted"
        " FROM audit_log WHERE action = 'gate.evaluate' AND tool = 'start_run' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


async def test_the_owners_tainted_robot_surf_starts_with_no_prompt(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with team(tmp_path, resonant_home, chat, monkeypatch, tainted="allow") as (w, d, fake):
        w.claude(calls=[START], answer="Starting it now.")
        assert fake.inject(LEAD, "robot-surf the README typo", thread=THREAD)
        await until(lambda: bool(in_thread(fake, "On it")), 60, "the start line")
        assert in_thread(fake, "On it") == [f"On it: starting R1 ({ASK})."]
        assert not any(PROMPT.search(m.text) for m in fake.sent)  # no approval prompt
        assert d.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        assert start_runs(d) == [{"decision": "Allow", "origin": "tainted", "lifted": 1}]


async def test_the_default_still_asks(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with team(tmp_path, resonant_home, chat, monkeypatch, tainted="ask") as (w, d, fake):
        w.claude(calls=[START], answer="{r0}")
        assert fake.inject(LEAD, "robot-surf the README typo", thread=THREAD)
        await until(lambda: any(PROMPT.search(m.text) for m in fake.sent), 60, "the prompt")
        assert d.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        rows = start_runs(d)
        assert rows and all(r == {"decision": "NeedsApproval", "origin": "tainted",
                                  "lifted": None} for r in rows)  # fmt: skip

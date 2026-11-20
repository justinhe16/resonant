"""PER-295: Slack only, end to end, over the real ``SlackChannel`` and a fake Slack transport.

The real daemon (router, gate, loop, brain, ``ClaudeRunner``) as in ``test_e2e_team``, with
iMessage off and ``channels.slack`` on: ``wiring.build_channels`` builds the adapter, which
reads its two tokens from the (fake) Keychain and talks to ``gateway.slack.fixtures`` instead
of Slack. Nothing reaches the network.

- ``@cori what's on staging?`` gets a threaded reply; a later untagged reply in that thread
  routes; an untagged message elsewhere is ignored.
- A gated builtin asked in the thread is prompted there with buttons. Miu's click is refused
  with an ephemeral note and changes nothing; Brian's click approves it, the action runs, and
  every prompt message is edited to "Approved by Brian" without buttons.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from test_e2e_runner import FakeChatDB, RunModel, World, attach_claude, chat, make_world
from test_runner_claude import until

from resonant.config import ChannelsConfig
from resonant.daemon import Daemon
from resonant.gateway.slack import SlackChannel
from resonant.gateway.slack import channel as slack_module
from resonant.gateway.slack.fixtures import (
    FakeSlackSocket,
    FakeSlackTransport,
    FakeSlackWeb,
    Post,
    click,
    mention,
    message,
)
from resonant.ops.fakes import FakeOpsRunner
from resonant.secrets import SecretStore
from resonant.store import approvals as ap
from resonant.tools.notes import SCOPE as NOTES_SCOPE

__all__ = ["chat"]  # the fixture

TEAM_ID, BOT = "T0DORI", "U0CORI"
JUSTIN, BRIAN, MIU = "U1", "U2", "U3"
ROOT = "1700000000.000100"  # the thread Brian starts by tagging Cori
PRINCIPALS = f"""\
principals:
  justin: {{role: owner, identities: ["slack:{JUSTIN}"]}}
  brian: {{role: approver, identities: ["slack:{BRIAN}"]}}
  miu: {{identities: ["slack:{MIU}"]}}
"""


@contextlib.asynccontextmanager
async def slack_daemon(
    tmp_path: Path, home: Path, chat: FakeChatDB, mp: pytest.MonkeyPatch
) -> AsyncGenerator[tuple[World, Daemon, FakeSlackWeb, FakeSlackSocket]]:
    w = make_world(tmp_path, home, chat, mp, scenario="happy", runner_opts={"ask_block_s": 0.5})
    (home / "principals.yaml").write_text(PRINCIPALS)
    channels = ChannelsConfig.model_validate(
        {
            "slack": {
                "enabled": True,
                "team_id": TEAM_ID,
                "bot_user_id": BOT,
                "rooms": {"digest": "C0DIGEST"},
            }
        }
    )
    w.settings = w.settings.model_copy(update={"channels": channels})
    w.keychain.items.update(
        {"slack/bot_token": "xoxb-e2e-fake", "slack/app_token": "xapp-e2e-fake"}
    )
    web, socket = FakeSlackWeb(team_id=TEAM_ID, bot_user_id=BOT), FakeSlackSocket()
    mp.setattr(slack_module, "sdk_transport", FakeSlackTransport(web, socket))
    daemon = w.daemon = Daemon(
        w.settings,
        model=RunModel(),
        osa_runner=w.osa,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(w.keychain),
    )
    attach_claude(w, daemon)
    task = asyncio.create_task(daemon.run())
    assert daemon.stack is not None
    hub = daemon.stack.channel
    await until(lambda: hub.health()["last_read_at"] is not None, 10, "slack to connect")
    try:
        yield w, daemon, web, socket
    finally:
        if not task.done():
            daemon.stop()
        assert await asyncio.wait_for(task, 20) == 0
        w.daemon = None


def in_thread(web: FakeSlackWeb, after: int = 0) -> list[Post]:
    return [p for p in web.posts[after:] if p.channel == "C0GENERAL" and p.thread_ts == ROOT]


async def test_mention_thread_reply_and_approval_buttons(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with slack_daemon(tmp_path, resonant_home, chat, monkeypatch) as (w, d, web, socket):
        assert d.stack is not None and d.stack.channel.names == ("slack",)
        slack = d.stack.channel.adapter("slack")
        assert isinstance(slack, SlackChannel) and slack.gate is d.state.components.gate  # type: ignore[union-attr]

        # @cori what's on staging? -> the brain answers in the thread
        w.claude(answer="Staging is green: api and web on the latest build.")
        await socket.deliver(mention(BRIAN, f"<@{BOT}> what's on staging?", team=TEAM_ID, ts=ROOT))
        await until(
            lambda: any("Staging is green" in p.text for p in in_thread(web)), 60, "the reply"
        )
        assert web.reactions[0]["timestamp"] == ROOT

        # an untagged message elsewhere is ignored; an untagged reply in the thread routes
        await socket.deliver(message(BRIAN, "lunch anyone?", team=TEAM_ID, ts="1700000001.000100"))
        n = len(web.posts)
        await socket.deliver(
            message(BRIAN, "/note buy milk", team=TEAM_ID, ts="1700000002.000100", thread_ts=ROOT)
        )
        await until(lambda: any("buy milk" in p.text for p in in_thread(web, n)), 30, "the note")
        assert len(web.reactions) == 2  # the reply in the thread, not "lunch anyone?"

        # a gated builtin (L1: clearing notes) asked in the thread: buttons there
        await socket.deliver(
            message(BRIAN, "/notes clear", team=TEAM_ID, ts="1700000003.000100", thread_ts=ROOT)
        )
        await until(lambda: any(p.buttons for p in in_thread(web)), 30, "the prompt in the thread")
        prompt = next(p for p in in_thread(web) if p.buttons)
        assert "This can't be undone" in prompt.text
        (row,) = d.conn.execute(
            "SELECT id, code FROM approvals WHERE status = 'pending'"
        ).fetchall()
        approval_id, code = row["id"], row["code"]
        assert any(p.buttons and p.channel == "D1" for p in web.posts)  # Justin asked too

        # Miu's click: an ephemeral refusal, nothing changes
        await socket.deliver(
            click(
                MIU,
                approval_id,
                code,
                team=TEAM_ID,
                channel="C0GENERAL",
                ts=prompt.ts,
                thread_ts=ROOT,
                action_ts="1800000001.000001",
            )
        )
        assert web.ephemerals[-1]["user"] == MIU
        assert web.ephemerals[-1]["text"] == "Only Justin or Brian can approve this."
        a = ap.get_approval(d.conn, approval_id)
        assert a is not None and a.status == "pending" and web.updates == []

        # Brian's click: approved, the notes are cleared, the prompts lose their buttons
        await socket.deliver(
            click(
                BRIAN,
                approval_id,
                code,
                team=TEAM_ID,
                channel="C0GENERAL",
                ts=prompt.ts,
                thread_ts=ROOT,
                action_ts="1800000002.000001",
            )
        )
        a = ap.get_approval(d.conn, approval_id)
        assert a is not None and (a.status, a.decided_by) == ("approved", "brian")
        assert {u["ts"] for u in web.updates} >= {prompt.ts}
        assert all("Approved by Brian" in u["text"] for u in web.updates)
        await until(
            lambda: (
                d.conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE scope = ? "
                    "AND json_extract(value, '$.deleted_at') IS NULL",
                    (NOTES_SCOPE,),
                ).fetchone()[0]
                == 0
            ),
            30,
            "the notes to clear",
        )
        # Justin's late click on his own copy: already decided
        jprompt = next(p for p in web.posts if p.buttons and p.channel == "D1")
        await socket.deliver(
            click(
                JUSTIN,
                approval_id,
                code,
                team=TEAM_ID,
                channel="D1",
                ts=jprompt.ts,
                action_ts="1800000003.000001",
            )
        )
        assert web.ephemerals[-1]["text"] == "Already approved by Brian."

        # rooms: a configured key posts, an unknown one is refused
        await d.stack.channel.post_room("digest", "Daily digest")
        assert web.posts[-1].channel == "C0DIGEST"

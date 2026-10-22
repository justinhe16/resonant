"""BR-1, PER-256: the brain runner end to end. The local model only routes.

The whole daemon in process: chat.db is a FakeChatDB, osascript a RecordingOsaRunner, the
model a ScriptedModel (triage answers only), and ``ask_claude`` sessions the fake CLI's ask mode
(``tests/fakes/claude``). Small talk and the news check are answered from code; everything else
is Claude's, whose answer is its own message (no prefix) and whose brain-task reply is silent.
Nothing touches the real ~/.resonant, ~/code, Keychain or claude.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sqlite3
import sys
from collections.abc import AsyncGenerator, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from brain_seed import message

from resonant.config import DEFAULT_READ_DENY, load_settings
from resonant.daemon import Daemon
from resonant.e2e import RecordingOsaRunner, ScriptedModel, wait_until
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop.types import NewTask
from resonant.models import ModelOutputError, ModelUnavailableError
from resonant.runners.ask import UNAVAILABLE_REPLY, AskRunner, AskScope
from resonant.runners.brain import MEMBER_REPLY, NOT_IN_BRAIN, TAINTING_BUILTINS, BrainRunner
from resonant.runners.watch import WatchRunner
from resonant.secrets import CLAUDE_TOKEN_REF, FakeKeychain, SecretStore
from resonant.store import brain as brain_store
from resonant.store import tasks as task_store
from resonant.store.db import atomic, iso, kv_set, utcnow

OWNER_PHONE = "+15551234567"
MEMBER_PHONE = "+15557654321"
SELF = "resonant.agent@icloud.com"
PRINCIPALS = f"""\
principals:
  justin:
    role: owner
    identities: ["imessage:{OWNER_PHONE}"]
  cofounder:
    identities: ["imessage:{MEMBER_PHONE}"]
    grants:
      "builtin:files": [request]
"""
FAKE_CLAUDE = Path(__file__).parent / "fakes" / "claude"
TOKEN = "sk-ant-oat01-FAKETOKENVALUE0123456789"
ANSWER = "Mycelium is in ~/code/mycelium."  # PER-256: no "Claude: " prefix
READS = ["list_dir", "read_file", "read_transcript", "recent_changes", "search", "tail_log"]
GREETING = {"handler": "local", "kind": "general", "message_type": "greeting", "say": "Hey there!"}
NEWS = {"handler": "local", "kind": "general", "message_type": "news_check", "say": ""}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Brain:
    daemon: Daemon
    osa: RecordingOsaRunner
    chat: FakeChatDB
    model: ScriptedModel
    code: Path

    @property
    def conn(self) -> sqlite3.Connection:
        return self.daemon.conn

    @property
    def runner(self) -> BrainRunner:
        assert self.daemon.imessage is not None and self.daemon.imessage.brain is not None
        return self.daemon.imessage.brain

    def rows(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, args).fetchall())

    def audits(self, action: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.rows("SELECT * FROM audit_log WHERE action = ? ORDER BY id", action):
            d = dict(r)
            d["detail"] = json.loads(d["detail"] or "{}")
            out.append(d)
        return out

    def brain_tasks(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM tasks WHERE runner = 'brain' ORDER BY rowid")

    def result(self, task: sqlite3.Row) -> dict[str, Any]:
        (row,) = self.rows("SELECT result FROM tasks WHERE id = ?", task["id"])
        return json.loads(row["result"] or "{}")

    async def say(self, text: str, replies: int = 1, *, phone: str = OWNER_PHONE) -> list[str]:
        """Send ``text`` from ``phone``; return the next ``replies`` sends."""
        n = len(self.osa.sent)
        self.chat.add_message(handle=phone, text=text)
        await self.osa.wait_for(n + replies, 15)
        return [m.text for m in self.osa.sent[n : n + replies]]

    async def prompt_recorded(self) -> None:
        def recorded() -> bool:
            rows = self.rows("SELECT channel_refs FROM approvals WHERE status = 'pending'")
            return bool(rows) and all(json.loads(r["channel_refs"]) for r in rows)

        await wait_until(recorded, 10)
        await asyncio.sleep(0.01)


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[FakeChatDB]:
    db = FakeChatDB(tmp_path / "chat.db")
    yield db
    db.close()


@contextlib.asynccontextmanager
async def start(
    home: Path,
    chat: FakeChatDB,
    tmp_path: Path,
    *,
    ask: dict[str, Any] | None = None,
    delay_s: float = 0.0,
    seed: Callable[[sqlite3.Connection], None] | None = None,
    ask_args: Sequence[str] = (),
    brain: dict[str, Any] | None = None,
    full_cap: bool = False,
    **overrides: Any,
) -> AsyncGenerator[Brain]:
    """``full_cap``: Claude is out of reach, at its daily cap (1, already used today);
    ``"told"``: and the owner already heard about it today."""
    (home / "principals.yaml").write_text(PRINCIPALS)
    if full_cap:
        ask = {**(ask or {}), "daily_cap": 1}
    code = tmp_path / "code"
    code.mkdir(exist_ok=True)
    settings = load_settings(
        dry_run=False,
        api={"port": free_port()},
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
        runner={"code_dir": code},
        brain={"read_roots": [code], "ask_claude": ask or {}, **(brain or {})},
        **overrides,
    )
    osa = RecordingOsaRunner()
    model = ScriptedModel(delay_s=delay_s)
    daemon = Daemon(settings, model=model, osa_runner=osa)
    kv_set(daemon.conn, "imessage.selftest", {"ok": True, "at": iso(utcnow()), "detail": "ok"})
    kv_set(daemon.conn, "autonomy", "on")
    if full_cap:  # today's one ask, already answered
        with atomic(daemon.conn):
            used = task_store.insert_task(daemon.conn, NewTask(kind="ask", runner="ask", priority=1,
                                                               principal="justin"))  # fmt: skip
            daemon.conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (used,))
    assert daemon.imessage is not None and daemon.state.components is not None
    assert daemon.imessage.brain is not None
    daemon.imessage.brain.runner_gaps = None  # the real check asks the Keychain (by name)
    # ask_claude's sessions: the fake CLI's ask mode with a token in a fake Keychain
    notes = tmp_path / "claude-notes"
    notes.mkdir()
    keychain = FakeKeychain()
    keychain.items[CLAUDE_TOKEN_REF] = TOKEN
    asks = AskRunner(
        daemon.conn,
        cfg=settings.brain.ask_claude,
        scope=AskScope((code,), DEFAULT_READ_DENY),
        ask_root=home / "ask",
        channel=daemon.imessage.channel,
        secrets=SecretStore(keychain),
        owner="justin",
        create_task=daemon.loop.create_task,
        claude_cmd=[sys.executable, str(FAKE_CLAUDE), "--notes", str(notes), *list(ask_args)],
        home=tmp_path / "claude-home",
        poll_s=0.05,
        kill_grace_s=0.5,
    )
    daemon.state.components.runners[asks.name] = asks
    daemon.loop.runners[asks.name] = asks
    watcher = daemon.state.components.runners.get("watch")
    if isinstance(watcher, WatchRunner):  # ask watches' answers (BR-7), as wiring does
        asks.on_watch = watcher.ask_answered
    if seed is not None:
        seed(daemon.conn)
    run = asyncio.create_task(daemon.run())
    channel = daemon.imessage.channel
    await wait_until(lambda: channel.health()["last_read_at"] is not None, 10)
    b = Brain(daemon, osa, chat, model, code)
    try:
        yield b
    finally:
        await b.runner.idle()
        daemon.stop()
        assert await asyncio.wait_for(run, 10) == 0


# --- the router: small talk and news from code, the rest to Claude -----------------------


def test_brain_tool_sets_name_registered_builtins(db: sqlite3.Connection) -> None:
    """A typo in TAINTING_BUILTINS or NOT_IN_BRAIN would fail silently."""
    from resonant.api import DaemonState
    from resonant.components import build_components
    from resonant.loop import Loop, SlotPool

    settings = load_settings()
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    components = build_components(settings, db, DaemonState(settings, db, loop, utcnow()))
    builtins = {s.name for s in components.registry.specs() if s.extension is None}
    assert builtins >= TAINTING_BUILTINS | NOT_IN_BRAIN


async def test_slash_commands_and_keywords_still_take_the_fast_path(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as b:
        (reply,) = await b.say("ping")
        assert reply.startswith("pong")
        (reply,) = await b.say("/notes")
        assert "note" in reply.lower()
        assert b.model.calls["triage"] == 0 and b.brain_tasks() == []


async def test_a_greeting_is_answered_by_the_local_model_alone(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as b:
        b.model.triage = GREETING
        assert await b.say("hi") == ["Hey there!"]
        (task,) = b.brain_tasks()
        assert b.result(task)["route"] == "local" and b.result(task)["path"] == "small_talk"
        assert b.rows("SELECT 1 FROM tasks WHERE runner = 'ask'") == []
        # the small prompt: a system line and the message, no tools, no history
        (req,) = b.model.triage_requests
        assert req.tools == [] and [m["role"] for m in req.messages] == ["system", "user"]


async def test_a_small_talk_line_with_a_number_or_a_claim_is_replaced_by_a_canned_one(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as b:
        b.model.triage = {**GREETING, "say": "Hi! 3 runs finished and everything is fine."}
        assert await b.say("hi") == ["Hey!"]


async def test_a_news_check_is_built_from_code(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    from resonant.brain.news import NEWS_NOTHING

    async with start(resonant_home, chat, tmp_path) as b:
        b.model.triage = NEWS
        assert await b.say("anything new?") == [NEWS_NOTHING]
        (task,) = b.brain_tasks()
        assert (
            b.result(task)["route"] == "local"
            and b.rows("SELECT 1 FROM tasks WHERE runner = 'ask'") == []
        )


async def test_a_question_goes_to_claude_whose_answer_is_the_only_message(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as b:
        assert await b.say("where is mycelium?") == [ANSWER]
        await asyncio.sleep(0.2)
        assert len(b.osa.sent) == 1  # fast: no ack, no "Claude: " prefix, no second text
        (task,) = b.brain_tasks()
        result = b.result(task)
        assert result["route"] == "claude" and result["reply_len"] == 0 and result["silent"]
        (ask,) = b.rows("SELECT * FROM tasks WHERE runner = 'ask'")
        assert json.loads(ask["checkpoint"])["requested_by"] == task["id"]
        # the marker the reply guard reads is the dedupe key (store.outbound)
        keys = [r["key"] for r in _recent(b)]
        assert any(k == f"ask:{ask['id']}" for k in keys)


def _recent(b: Brain) -> list[Any]:
    raw = b.conn.execute("SELECT value FROM kv WHERE key LIKE 'outbound.recent:%'").fetchall()
    return [e for r in raw for e in json.loads(r["value"])]


async def test_a_bad_triage_answer_and_a_model_that_is_down_both_go_to_claude(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as b:
        b.model.triages = [ModelOutputError("bad"), ModelUnavailableError("down")]
        assert await b.say("hmm?") == [ANSWER]
        assert await b.say("what now?") == [ANSWER]
        assert len(b.rows("SELECT 1 FROM tasks WHERE runner = 'ask'")) == 2


async def test_why_did_a_run_do_that_goes_to_claude_even_if_triage_says_question(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    """PER-270: the run's transcript is Claude's; the local model never states a reason."""
    from resonant.store import runs

    async with start(resonant_home, chat, tmp_path) as b:
        run = runs.create(b.conn, repo="robot-surf-prompt", ask="Add the surf skills",
                          principal="justin", session_id="s9", max_seconds=60)  # fmt: skip
        runs.update(b.conn, run.code, status="running")
        runs.finish(b.conn, run.code, "no_changes")
        b.model.triage = {**GREETING, "message_type": "ack", "say": "ok"}  # a bad label
        assert await b.say("Why did R1 make no changes") == [ANSWER]
        (task,) = b.brain_tasks()
        assert b.result(task)["route"] == "claude"


async def test_claude_off_says_so_instead_of_answering(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path, ask={"enabled": False}) as b:
        assert await b.say("where is mycelium?") == [UNAVAILABLE_REPLY]
        (task,) = b.brain_tasks()
        assert b.result(task)["refused"] == "off"


async def test_at_the_daily_cap_the_owner_hears_it_once_and_gets_the_canned_reply(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path, full_cap=True) as b:
        n = len(b.osa.sent)
        b.chat.add_message(handle=OWNER_PHONE, text="hmm?")
        await b.osa.wait_for(n + 2, 15)
        texts = {m.text for m in b.osa.sent[n : n + 2]}
        assert texts == {
            "Hit today's Claude cap (1); approvals and /status still work until midnight.",
            UNAVAILABLE_REPLY,
        }
        assert len(b.rows("SELECT 1 FROM tasks WHERE runner = 'ask'")) == 1  # the used one


async def test_members_see_only_granted_tools_and_cant_use_claude(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    """The member's grant is ``builtin:files``: the read tools, no ask_claude. A text that
    needs Claude gets the canned member reply; small talk still works."""
    async with start(resonant_home, chat, tmp_path) as b:
        assert [s.name for s in b.runner.tools_for("cofounder")] == READS
        assert await b.say("look around", phone=MEMBER_PHONE) == [MEMBER_REPLY]
        assert b.rows("SELECT 1 FROM tasks WHERE runner = 'ask'") == []
        b.model.triage = GREETING
        assert await b.say("hi", phone=MEMBER_PHONE) == ["Hey there!"]


async def test_history_folds_after_the_reply(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        for i in range(15):  # earlier exchanges, already answered
            message(conn, f"earlier {i}", reply=f"reply {i}")
        conn.execute("UPDATE tasks SET status = 'done'")

    async with start(resonant_home, chat, tmp_path, seed=seed) as b:
        b.model.triage = GREETING
        assert await b.say("hi") == ["Hey there!"]
        await b.runner.idle()
        summary = brain_store.get_summary(b.conn, "justin")
        assert summary is not None and summary.folded >= 5
        assert b.model.calls.get("fold") == 1

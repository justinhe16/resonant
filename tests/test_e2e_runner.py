"""Runner acceptance (docs/phase5a-plan.md section 5, the R-tests that run through chat):
iMessage in, the real ``Daemon`` (router, tools, gate, loop, ``ClaudeRunner``), iMessage out.

The pieces that would leave the machine are fakes: chat.db is a ``FakeChatDB``, osascript a
``RecordingOsaRunner``, the model a ``ScriptedModel`` that knows two run phrasings, ``claude``
is ``tests/fakes/claude``, the remote a local bare repo and ``gh`` a script. The sandbox is the
identity (its own tests are ``test_sandbox*.py``) except in the one test that runs the session
under the real ``sandbox-exec``, which is local-only. The runner-level R-tests (escapees,
timeouts, usage limits, symlinks, token hygiene in the transcript) live in
``test_runner_claude.py``; this file is what a person texting the Mini sees.

- R1 and R5 ``test_r1_*``  R4 ``test_r4_*``  R6 ``test_r6_*``  R7 ``test_kill_*``
- R13 ``test_r13_*``  R14 ``test_r14_*``  cancel ``test_cancel_*``  no CI ``test_no_ci_*``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sqlite3
import sys
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from github_app_fakes import APP_ID, INSTALLATION_ID, FakeGitHubApi, private_pem_one_line
from test_runner_claude import (
    CLAUDE_TOKEN,
    FAKE,
    FAKE_GH,
    GH_TOKEN,
    GIT,
    check_ok,
    git,
    identity_wrap,
    live,
    seed_remote,
    until,
)

from resonant import killswitch
from resonant.config import Settings, load_settings
from resonant.daemon import Daemon
from resonant.e2e import RecordingOsaRunner, ScriptedModel
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop import Loop
from resonant.models import ChatMessage, ModelReply, SpanFactory, ToolCall
from resonant.ops.fakes import FakeOpsRunner
from resonant.router.fast_path import RESUME_REPLY
from resonant.runner.github_app import GitHubApp
from resonant.runner.workspace import Workspace
from resonant.runners import claude as claude_mod
from resonant.runners import reaper
from resonant.runners.claude import ClaudeRunner, Refused
from resonant.secrets import (
    CLAUDE_TOKEN_REF,
    GITHUB_APP_KEY_REF,
    GITHUB_PUBLIC_TOKEN_REF,
    GITHUB_TOKEN_REF,
    FakeKeychain,
    SecretStore,
)
from resonant.store import pulse as pulse_store
from resonant.store import runs as runs_store
from resonant.store import trust as trust_store
from resonant.store.db import iso, kv_set, utcnow
from resonant.store.repos import Repo
from resonant_sdk import ToolSpec

OWNER_PHONE = "+15551234567"
SELF = "resonant.agent@icloud.com"
PRINCIPALS = f"""\
principals:
  justin:
    role: owner
    identities: ["imessage:{OWNER_PHONE}"]
"""
REPO = "demo"
PR_URL = "https://github.com/own/demo/pull/41"


class RunModel(ScriptedModel):
    """The scripted model plus what the runner model does for the two run phrasings: it
    picks ``start_run`` (repo and ask taken from the message) or ``merge_run`` for R1."""

    def __init__(self) -> None:
        super().__init__(
            rules=(("typo", "run_project"), ("merge it", "run_project")),
            final_reply="Working on it.",
        )

    async def chat_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        max_tokens: int = 512,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        names = {t.name for t in tools}
        current = next(
            (m["content"] for m in reversed(messages) if "<message>" in m["content"]), ""
        ).lower()
        asked = any(
            m["role"] == "assistant" and m["content"].startswith('{"tool_call"') for m in messages
        )
        call: ToolCall | None = None
        if not asked and "merge it" in current and "merge_run" in names:
            call = ToolCall("merge_run", {"code": "R1"})
        elif not asked and "typo" in current and "start_run" in names:
            call = ToolCall("start_run", {"repo": REPO, "ask": "fix the typo in the README"})
        if call is None:
            return await super().chat_tools(messages, tools=tools, max_tokens=max_tokens)
        self.calls["tools"] += 1
        return ModelReply(None, [call], None, 1, 10, 5)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class World:
    root: Path
    chat: FakeChatDB
    settings: Settings
    keychain: FakeKeychain
    osa: RecordingOsaRunner
    remote: Path
    gh_dir: Path
    api: FakeGitHubApi
    daemon: Daemon | None = None
    said: int = 0

    @property
    def conn(self) -> sqlite3.Connection:
        assert self.daemon is not None
        return self.daemon.conn

    @property
    def runner(self) -> ClaudeRunner:
        assert self.daemon is not None and self.daemon.imessage is not None
        assert self.daemon.imessage.claude is not None
        return self.daemon.imessage.claude

    def texts(self) -> list[str]:
        return [m.text for m in self.osa.sent]

    async def say(self, text: str, *, reply: bool = True) -> str:
        """Text the Mini; return its next reply (the next one sent, in order)."""
        self.chat.add_message(handle=OWNER_PHONE, text=text)
        if not reply:
            return ""
        self.said += 1
        return (await self.osa.wait_for(self.said, 30)).text

    async def next_text(self) -> str:
        """The next unclaimed text (a reply nobody asked for: ``done``, ``cancelled``)."""
        self.said += 1
        return (await self.osa.wait_for(self.said, 60)).text

    def run(self, code: str = "R1") -> runs_store.Run:
        run = runs_store.get(self.conn, code)
        assert run is not None
        return run

    def tmp(self, code: str = "R1") -> Path:
        return self.settings.runner.work_dir / code / "tmp"

    def pid_of(self, code: str, name: str) -> int:
        return int((self.tmp(code) / name).read_text())

    def branches(self) -> list[str]:
        return git(self.remote, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()

    def gh_calls(self, verb: str) -> list[dict[str, Any]]:
        path = self.gh_dir / "calls.jsonl"
        calls = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [c for c in calls if c["argv"][:2] == ["pr", verb]]

    def everything(self) -> str:
        """Every text sent and every row stored."""
        dump = "\n".join(self.conn.iterdump())
        return "\n".join([*self.texts(), dump])


def make_world(
    tmp_path: Path,
    home: Path,
    chat: FakeChatDB,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scenario: str = "happy",
    extra: Sequence[str] = (),
    ci: bool = True,
    secrets: bool = True,
    runner_opts: dict[str, Any] | None = None,
    workspace_opts: dict[str, Any] | None = None,
    guard: str = "off",
) -> World:
    root = tmp_path.resolve() / "rr"
    root.mkdir(exist_ok=True)
    (home / "principals.yaml").write_text(PRINCIPALS)
    remote = seed_remote(root, REPO, ci=ci)
    gh = root / "bin" / "gh"
    gh.parent.mkdir()
    gh_dir = root / "gh-state"
    gh_dir.mkdir()
    gh.write_text(FAKE_GH.replace("@PY@", sys.executable).replace("@STATE@", str(gh_dir)))
    gh.chmod(0o755)
    settings = load_settings(
        dry_run=False,
        api={"port": free_port()},
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
        # not this checkout: the daemon's own repo would be registered as "resonant"
        ops={"repo_path": None},
        runner={
            "guard": guard,
            "github_app": {"app_id": APP_ID, "installation_id": INSTALLATION_ID},
            # explicit: a repo's own default is to allow .github/ changes (the r4 tests flip it)
            "repos": {REPO: {"remote": f"github.com/own/{REPO}", "allow_github_changes": False}},
            "work_dir": root / "runs",
            "code_dir": root / "code",
            "mirror_dir": root / "state" / "mirrors",
            "cache_dir": root / "runs" / ".cache",
            "secrets_dir": root / "state" / "secrets",
            "git_path": GIT,
            "gh_path": gh,
        },
    )
    keychain = FakeKeychain()
    if secrets:
        keychain.items[GITHUB_TOKEN_REF] = GH_TOKEN
        keychain.items[CLAUDE_TOKEN_REF] = CLAUDE_TOKEN
        keychain.items[GITHUB_APP_KEY_REF] = private_pem_one_line()
    api = FakeGitHubApi()
    world = World(root, chat, settings, keychain, RecordingOsaRunner(), remote, gh_dir, api)
    assert settings.runner.github_app is not None
    options: dict[str, Any] = {
        "github_app": GitHubApp(
            settings.runner.github_app,
            SecretStore(keychain),
            transport=api.transport(),
            backoff_s=0,
        ),
        "wrap": identity_wrap,
        "self_check": check_ok,
        "home": root,  # the guard is off by default: a fake owner's HOME
        "claude_cmd": [sys.executable, str(FAKE), "--scenario", scenario, *extra],
        "poll_s": 0.05,
        "kill_grace_s": 0.5,
        "defer_s": 0.1,
        **(runner_opts or {}),
    }
    from resonant import wiring

    def make_runner(conn: sqlite3.Connection, **kw: Any) -> ClaudeRunner:
        def url_for(repo: Repo) -> str:
            return str(remote)

        ws_opts: dict[str, Any] = {"url_for": url_for, **(workspace_opts or {})}
        ws = Workspace(settings.runner, conn, **ws_opts)
        return ClaudeRunner(conn, **kw, **options, workspace=ws)

    monkeypatch.setattr(wiring, "ClaudeRunner", make_runner)
    return world


def new_daemon(world: World) -> Daemon:
    daemon = Daemon(
        world.settings,
        model=RunModel(),
        osa_runner=world.osa,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(world.keychain),
    )
    kv_set(daemon.conn, "imessage.selftest", {"ok": True, "at": iso(utcnow()), "detail": "ok"})
    return daemon


@contextlib.asynccontextmanager
async def running(world: World) -> AsyncGenerator[World]:
    """Start the daemon (and start it again after ``crash``), stop it on exit."""
    daemon = world.daemon = new_daemon(world)
    task = asyncio.create_task(daemon.run())
    assert daemon.imessage is not None
    channel = daemon.imessage.channel
    await until(lambda: channel.health()["last_read_at"] is not None, 10, "the channel to read")
    try:
        yield world
    finally:
        if not task.done():
            daemon.stop()
        await asyncio.wait_for(task, 20)
        world.daemon = None


@pytest.fixture
def chat(tmp_path: Path) -> Any:
    db = FakeChatDB(tmp_path / "chat.db")
    yield db
    db.close()


def settled(world: World, code: str, *statuses: str) -> Callable[[], bool]:
    return lambda: (
        runs_store.get(world.conn, code) is not None and world.run(code).status in (statuses)
    )


# --- R1: /run and chat ----------------------------------------------------------------------


async def test_a_worker_check_waits_for_the_owners_yes_across_the_turn(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EM-3 with EM-4's real ``TrustChecks``: a net-new ``resonant check`` asks the owner. The
    CLI gives up waiting, the turn ends and the run parks with the approval still pending;
    the owner's "yes" resumes the session with the verdict."""
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="happy",
                   extra=["--check", "dori-api:deploy:deploy dori-api to prod"],
                   runner_opts={"ask_block_s": 0.5})  # fmt: skip
    async with running(w):
        assert (await w.say(f"/run {REPO} fix the typo in the README")).startswith("On it: R1")
        prompt = await w.next_text()
        assert prompt.startswith("[") and "R1 wants to deploy on dori-api" in prompt, prompt
        await until(settled(w, "R1", "waiting"), 20, "the check wait")
        assert w.run().wait_reason == "question"
        calls = json.loads((w.tmp() / "cli.json").read_text())
        assert calls[0]["rc"] == 3  # no decision within the CLI's wait
        await w.say("yes", reply=False)
        done = await w.next_text()
        while not done.startswith("R1 done"):  # a confirmation may come first
            done = await w.next_text()
        assert done.startswith(f"R1 done: PR #41 (CI green) · {PR_URL}"), done
        prompts = [c["prompt"] for c in json.loads((w.tmp() / "argv.json").read_text())]
        assert prompts[1].startswith(
            "Your check (deploy on dori-api: deploy dori-api to prod) is ok: go ahead."
        )


async def test_a_worker_question_reaches_the_owner_and_the_answer_resumes_it(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EM-3: ``resonant ask`` in the session texts the owner ``R1 asks: …``; the run waits
    (slot freed); the answer (EM-5 will route the owner's reply here) resumes the session
    with it, and the run ends with its PR as usual."""
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="happy",
                   extra=["--ask", "Fix both READMEs or just the top one?"],
                   runner_opts={"ask_block_s": 0.5})  # fmt: skip
    async with running(w):
        started = await w.say(f"/run {REPO} fix the typo in the README")
        assert started.startswith("On it: R1 · demo.")
        asked = await w.next_text()
        assert asked == "R1 asks: Fix both READMEs or just the top one?"
        await until(settled(w, "R1", "waiting"), 20, "the question wait")
        assert w.run().wait_reason == "question"
        item = pulse_store.get(w.conn, "core", "run/r1")
        assert item is not None and item.title.startswith("R1 waiting for your answer · demo")
        [q] = w.runner.open_questions()
        assert q.run_code == "R1"
        assert not isinstance(w.runner.answer_question("R1", "just the top one"), Refused)
        done = await w.next_text()
        assert done.startswith(f"R1 done: PR #41 (CI green) · {PR_URL}"), done
        prompts = [c["prompt"] for c in json.loads((w.tmp() / "argv.json").read_text())]
        assert len(prompts) == 2 and prompts[1].startswith(
            'The owner answered your question "Fix both READMEs or just the top one?": '
            "just the top one"
        )


async def test_r1_run_command_to_a_pr_link_with_the_pulse_item(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(
        tmp_path, resonant_home, chat, monkeypatch, scenario="makefile", extra=["--sleep", "1.5"]
    )
    async with running(w):
        started = await w.say(f"/run {REPO} fix the typo in the README")
        assert started == "On it: R1 · demo. Up to 45m. /runs to check, /cancel R1 to stop."
        # While it works: a normal pulse item, mentioned by the next hourly pulse.
        await until(lambda: pulse_store.get(w.conn, "core", "run/r1") is not None, 10, "pulse")
        item = pulse_store.get(w.conn, "core", "run/r1")
        assert item is not None and item.urgency == "normal" and not item.cleared
        assert item.title.startswith("R1 still running · demo · started ")
        running_line = await w.say("/runs")
        assert running_line.startswith("Runs (1 active):\n- R1 demo · running · ")

        done = await w.next_text()
        assert done.startswith(f"R1 done: PR #41 (CI green) · {PR_URL} · 3 files · "), done
        assert "/merge R1" in done and "⚠ changes .claude/settings.json, Makefile" in done
        assert pulse_store.pending(w.conn, ["normal"], utcnow()) == []  # cleared, nothing owed
        cleared = pulse_store.get(w.conn, "core", "run/r1")
        assert cleared is not None and cleared.cleared

        listing = await w.say("/runs")
        assert listing.startswith("Runs (0 active):\n- R1 demo · ready · PR #41 · changes ")
        assert w.run().status == "ready" and w.run().step == "reply"
        # one PR on the remote, with the fake Claude's commit
        branch = w.run().branch or ""
        assert branch in w.branches() and len(w.gh_calls("create")) == 1
        assert git(w.remote, "log", "-1", "--format=%s", branch) == "Fix the flaky test"
        # no token anywhere: texts, DB; the run's own token was minted for the one repo and
        # is dead again
        assert GH_TOKEN not in w.everything() and CLAUDE_TOKEN not in w.everything()
        assert not any(t in w.everything() for t in w.api.tokens)
        assert [m["repositories"] for m in w.api.mints] == [["demo"]] and w.api.live == set()

        # the same warning on the way to a merge
        merged = await w.say("/merge R1")
        assert merged == f"R1 merged: PR #41.\n⚠ changes .claude/settings.json, Makefile\n{PR_URL}"


async def test_r1_chat_request_starts_a_run(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch)
    async with running(w):
        started = await w.say("fix the typo in the README in demo")
        assert started.endswith("On it: R1 · demo. Up to 45m. /runs to check, /cancel R1 to stop.")
        # EM-6: a run asked for in plain words reports in the digest, not with a text
        await until(settled(w, "R1", "ready"), 60, "R1 to finish")
        assert w.run().ask == "fix the typo in the README" and w.run().principal == "justin"
        detail = (await w.say("/runs R1")).splitlines()
        assert detail[0] == "- R1 demo · ready · PR #41" and PR_URL in detail
        assert not any(t.startswith("R1 done") for t in w.texts())
        digest = await w.say("/pulse now")
        assert digest == f"btw:\nWorkers:\nR1 done: PR #41 (CI green) · {PR_URL}"
        assert await w.next_text() == "Pulse sent."
        assert not any(t.startswith("R1 done") for t in w.texts())  # said once, in the digest


async def test_em6_a_chat_run_that_fails_is_texted_right_away(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="error")
    async with running(w):
        assert (await w.say("fix the typo in the README in demo")).endswith("/cancel R1 to stop.")
        failed = await w.next_text()
        assert failed == f"R1 failed: {w.run().failure}" and w.run().status == "failed"
        assert pulse_store.pending(w.conn, ["normal", "urgent"], utcnow()) == []


async def test_em6_a_notify_now_rule_texts_a_chat_run_when_it_finishes(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch)
    async with running(w):
        trust_store.add_rule(w.conn, scope=REPO, action="done", effect="notify_now",
                             created_from="chat", now=utcnow())  # fmt: skip
        assert (await w.say("fix the typo in the README in demo")).endswith("/cancel R1 to stop.")
        done = await w.next_text()
        assert done.startswith(f"R1 done: PR #41 (CI green) · {PR_URL} · 1 file · ")
        assert pulse_store.pending(w.conn, ["normal", "urgent"], utcnow()) == []


# --- /cancel and /kill ----------------------------------------------------------------------


async def test_cancel_mid_session_ends_the_tree_and_publishes_nothing(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="hang")
    async with running(w):
        assert (await w.say("/run demo fix it")).startswith("On it: R1")
        await until(lambda: (w.tmp() / "claude.pid").exists(), 15, "the session")
        pid = w.pid_of("R1", "claude.pid")
        assert live(pid)
        assert await w.say("/cancel R1") == "R1 cancelled."
        await until(lambda: not live(pid), 15, "the session to end")
        await until(settled(w, "R1", "cancelled"), 10)
        assert (await w.say("/runs")).startswith("Runs (0 active):\n- R1 demo · cancelled")
        assert w.branches() == ["main"] and w.gh_calls("create") == []
        await until(
            lambda: pulse_store.pending(w.conn, ["normal"], utcnow()) == [], 10, "pulse cleared"
        )


async def test_kill_ends_runs_and_the_reply_counts_them(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(
        tmp_path, resonant_home, chat, monkeypatch, scenario="escapee", extra=["--sleep", "60"]
    )
    async with running(w):
        assert (await w.say("/run demo fix it")).startswith("On it: R1")
        await until(lambda: (w.tmp() / "child.pid").exists(), 15, "the escapees")
        pids = [w.pid_of("R1", n) for n in ("claude.pid", "escapee.pid", "orphan.pid", "child.pid")]
        assert all(live(p) for p in pids)
        reply = await w.say("/kill")
        assert reply == "kill switch engaged, 1 run ended", reply
        await until(lambda: not any(live(p) for p in pids), 15, "the whole tree to die")
        assert w.run().status == "cancelled" and w.branches() == ["main"]
        assert w.gh_calls("create") == []
        await until(lambda: "R1 cancelled." in w.texts(), 10, "the cancelled reply")
        # a killed run isn't restarted by /resume, and nothing starts while paused
        await w.say("/resume")
        await until(lambda: RESUME_REPLY in w.texts(), 10, "the resume reply")
        assert w.run().status == "cancelled"
        killswitch.engage(w.conn, actor="test")
        reply = await w.say("/run demo again")
        assert "paused" in reply.lower()
        killswitch.release(w.conn, actor="test")


# --- R6: a crash in the middle of a session -------------------------------------------------


async def test_r6_crash_then_restart_resumes_the_same_session(
    tmp_path: Path,
    resonant_home: Path,
    chat: FakeChatDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="hang_then_ok")
    real_reap = reaper.reap
    async with running(w):
        assert (await w.say("/run demo fix it")).startswith("On it: R1")
        await until(lambda: (w.tmp() / "claude.pid").exists(), 15, "the session")
        old_pid = w.pid_of("R1", "claude.pid")
        session = w.run().session_id

        async def no_reap(*a: Any, **k: Any) -> int:
            return 0

        # The daemon dies without cleaning up: the process tree outlives it.
        monkeypatch.setattr(reaper, "reap", no_reap)
        real_shutdown = Loop._shutdown  # pyright: ignore[reportPrivateUsage]

        async def short_grace(self: Loop) -> None:  # a short grace, then the step is abandoned
            await real_shutdown(self, 0.2)

        monkeypatch.setattr(Loop, "_shutdown", short_grace)
    monkeypatch.setattr(reaper, "reap", real_reap)
    monkeypatch.setattr(Loop, "_shutdown", real_shutdown)
    assert live(old_pid)

    async with running(w):
        # on start the old tree is reaped, then the task resumes the same session
        await until(lambda: not live(old_pid), 15, "the old session to be reaped")
        done = await w.next_text()
        assert done.startswith(f"R1 done: PR #41 (CI green) · {PR_URL}"), done
        run = w.run()
        assert run.status == "ready" and run.session_id == session and run.sessions == 2
        calls = json.loads((w.tmp() / "argv.json").read_text())
        assert len(calls) == 2 and "--session-id" in calls[0]["argv"]
        assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == session
        assert len(w.gh_calls("create")) == 1  # one PR, not two


# --- R13: merge, by command and by chat -----------------------------------------------------


async def test_r13_merge_by_command_needs_green_ci(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch)
    async with running(w):
        assert (await w.say("/merge R1")) == "No run R1."
        assert (await w.say("/run demo fix it")).startswith("On it: R1")
        done = await w.next_text()
        assert done.startswith("R1 done: PR #41")
        (w.gh_dir / "checks_rc").write_text("1")
        assert await w.say("/merge R1") == "R1 not merged: CI isn't green"
        assert w.run().status == "ready"
        (w.gh_dir / "checks_rc").write_text("0")
        assert (await w.say("/merge R1")).startswith("R1 merged: PR #41.")
        [merge] = w.gh_calls("merge")
        argv = merge["argv"]
        assert argv[argv.index("--match-head-commit") + 1] == w.run().head_sha
        assert w.run().status == "merged" and not (w.settings.runner.work_dir / "R1").exists()


async def test_r13_merge_by_chat_asks_yes_first(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="makefile")
    async with running(w):
        assert (await w.say("/run demo fix it")).startswith("On it: R1")
        assert (await w.next_text()).startswith("R1 done: PR #41")
        prompt = await w.say("merge it")
        assert "merge R1: PR #41 in demo " in prompt and PR_URL in prompt, prompt
        assert prompt.endswith("Reply yes / no")
        assert "changes .claude/settings.json, Makefile" in prompt
        assert w.gh_calls("merge") == [] and w.run().status == "ready"  # nothing before the yes
        # chat.db times have one-second resolution, and a reply that is not later than the
        # prompt was "sent before the question" (the binding rule): wait a real second.
        await asyncio.sleep(1.1)
        await w.say("yes", reply=False)
        await until(settled(w, "R1", "merged"), 20, "the merge")
        assert len(w.gh_calls("merge")) == 1
        reply = await w.next_text()
        assert "R1 merged: PR #41." in reply


# --- R14: not set up ------------------------------------------------------------------------


async def test_r14_not_set_up_names_what_is_missing(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, secrets=False)
    async with running(w):
        reply = await w.say("/run demo fix it")
        # RX-1: only the Claude credential blocks a run; the GitHub pieces are notes
        assert reply.startswith(
            "The runner isn't set up: a Claude login (or secret runner/claude_token"
        )
        assert "resonant secret set runner/claude_token" in reply
        assert "github_token" not in reply and "github_app" not in reply
        runs = await w.say("/runs")
        assert runs.startswith(reply + "\nNo push or PRs yet: the GitHub App isn't set up")
        assert "runner/github_token isn't set" in runs
        # The runner never starts anything.
        await w.say("fix the typo in the README in demo")
        assert runs_store.recent(w.conn, 10) == []


async def test_r14_missing_token_only(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch)
    w.keychain.items.pop(CLAUDE_TOKEN_REF)
    async with running(w):
        reply = await w.say("/run demo fix it")
        assert reply.startswith(
            "The runner isn't set up: a Claude login (or secret runner/claude_token"
        )
        assert "runner/github_token" not in reply
        reply = await w.say("fix the typo in the README in demo")
        assert "The runner isn't set up: a Claude login (or secret runner/claude_token" in reply
        assert runs_store.recent(w.conn, 10) == []


# --- refusals: no CI, .github, a token in the diff -------------------------------------------


async def test_no_ci_repo_runs_but_cannot_be_merged(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, ci=False)
    async with running(w):
        assert (await w.say("/run demo fix it")).startswith("On it: R1")
        done = await w.next_text()
        assert done.startswith("R1 done: PR #41") and "no CI: demo has no pull_request" in done
        assert w.run().status == "ready"
        assert await w.say("/merge R1") == (
            "R1 not merged: own/demo has no CI; add a pull_request workflow first "
            "(or ask me to add CI)."
        )
        assert w.gh_calls("merge") == [] and w.run().status == "ready"


async def test_r4_a_github_change_cannot_be_merged(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="github")
    async with running(w):
        assert (await w.say("/run demo fix it")).startswith("On it: R1")
        done = await w.next_text()  # the session pushed and opened the PR itself
        assert done.startswith(f"R1 done: PR #41 (CI green) · {PR_URL}"), done
        assert "⚠ it can't be merged as it is: it changes .github/" in done
        assert w.run().status == "ready" and w.gh_calls("create")
        refused = await w.say("/merge R1")
        assert refused.startswith("R1 not merged: the checks refuse it: it changes .github/")
        assert w.gh_calls("merge") == [] and w.run().status == "ready"


async def test_r4_a_github_change_is_listed_when_the_repo_allows_it(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="github")
    w.settings.runner.repos[REPO] = w.settings.runner.repos[REPO].model_copy(
        update={"allow_github_changes": True}
    )
    async with running(w):
        assert (await w.say("/run demo fix it")).startswith("On it: R1")
        done = await w.next_text()
        assert "⚠ changes .github/workflows/evil.yml" in done and "can't be merged" not in done
        assert w.api.mints[0]["permissions"]["workflows"] == "write"
        merged = await w.say("/merge R1")
        assert merged == f"R1 merged: PR #41.\n⚠ changes .github/workflows/evil.yml\n{PR_URL}"


async def test_r4_a_token_in_the_diff_cannot_be_merged_and_is_never_echoed(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(
        tmp_path, resonant_home, chat, monkeypatch, scenario="tokens", extra=["--leak", GH_TOKEN]
    )
    async with running(w):
        assert (await w.say("/run demo fix it")).startswith("On it: R1")
        done = await w.next_text()
        assert done.startswith("R1 done: PR #41") and "a token appears" in done
        refused = await w.say("/merge R1")
        assert refused.startswith("R1 not merged: the checks refuse it: a token appears")
        assert w.gh_calls("merge") == []
        assert GH_TOKEN not in w.everything() and CLAUDE_TOKEN not in w.everything()


# --- the real sandbox (local only) ----------------------------------------------------------


@pytest.mark.skipif(
    bool(os.environ.get("CI")) or not os.access("/usr/bin/sandbox-exec", os.X_OK),
    reason="runs the session under the real sandbox-exec; local only",
)
async def test_r1_under_the_real_sandbox(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    root = await asyncio.to_thread(tmp_path.resolve)
    python = await asyncio.to_thread(os.path.realpath, sys.executable)
    cache = root / "rr" / "runs" / ".cache"
    cache.mkdir(parents=True)
    fake = cache / "fake-claude"
    shutil.copy(FAKE, fake)
    w = make_world(
        tmp_path,
        resonant_home,
        chat,
        monkeypatch,
        runner_opts={
            "wrap": claude_mod._sandbox_wrap,  # pyright: ignore[reportPrivateUsage]
            # a local bare repo can't be pushed to from inside the sandbox; the real push goes
            # to github.com, which the profile allows
            "claude_cmd": [python, str(fake), "--no-push"],
            "path_dirs": [Path(python).parent],
        },
    )
    async with running(w):
        assert (await w.say("/run demo fix it")).startswith("On it: R1")
        done = await w.next_text()
        assert done.startswith("R1: no changes."), done
        env = json.loads((w.tmp() / "argv.json").read_text())[0]["env"]
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == CLAUDE_TOKEN
        seen = json.loads((w.tmp() / "creds.json").read_text())
        assert seen["token"] == w.api.tokens[0]  # the sandboxed session read its own credentials


# --- EM-7: fork mode and merge per trust rules ------------------------------------------------

PUBLIC_TOKEN = "ghp_" + "Pq7Rs8Tu9V" * 3 + "Wx1Y2Z"


def fork_world(
    tmp_path: Path, home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> World:
    """A world whose ``up/stream`` is someone else's public repo (fork mode) and whose fork
    ``me/stream`` is a bare clone of it."""
    root = tmp_path.resolve() / "rr"
    root.mkdir(exist_ok=True)
    upstream = seed_remote(root, "stream")
    fork = root / "fork-stream.git"
    git(root, "clone", "-q", "--bare", str(upstream), str(fork))
    remotes = {"github.com/up/stream": upstream}

    def url_for(repo: Repo) -> str:
        return str(remotes.get(repo.remote, root / f"{REPO}.git"))

    def fork_url_for(slug: str) -> str:
        return str(fork)

    w = make_world(
        tmp_path, home, chat, monkeypatch, extra=["--no-pr"],
        workspace_opts={"url_for": url_for, "fork_url_for": fork_url_for, "fork_wait_s": 0.0},
    )  # fmt: skip
    w.keychain.items[GITHUB_PUBLIC_TOKEN_REF] = PUBLIC_TOKEN
    w.api.account = "me"
    (w.gh_dir / "fork_remote").write_text(str(fork))
    (w.gh_dir / "forks.json").write_text(json.dumps({"up/stream": "me/stream"}))
    (w.gh_dir / "repos.json").write_text(json.dumps({
        "up/stream": {"full_name": "up/stream", "default_branch": "main", "private": False,
                      "permissions": {"push": False}},
        "me/stream": {"full_name": "me/stream", "fork": True, "private": False,
                      "parent": {"full_name": "up/stream"}},
    }))  # fmt: skip
    return w


def gh_log(world: World) -> list[dict[str, Any]]:
    path = world.gh_dir / "calls.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


async def test_fork_run_asks_external_pr_and_opens_it_on_yes(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = fork_world(tmp_path, resonant_home, chat, monkeypatch)
    async with running(w):
        # /run takes any repo the resolver does (EM-8): a URL registers it on first use
        started = await w.say("/run github.com/up/stream fix the typo")
        assert started.startswith("On it: R1 · stream (new repo: up/stream)."), started
        assert "I'll ask before the PR." in started
        ask = await w.next_text()
        assert 'R1 wants to external_pr on up/stream: "+1/-0, 1 file @ ' in ask, ask
        assert ': Fix the flaky test"' in ask
        assert ask.endswith("Reply yes / yes always / no"), ask
        await until(settled(w, "R1", "waiting"), 10, "the wait")
        assert w.run().wait_reason == "question"
        assert [c for c in gh_log(w) if c["argv"][:2] == ["pr", "create"]] == []
        await asyncio.sleep(1.1)  # the reply must come after the prompt (sent-before rule)
        await w.say("yes", reply=False)
        done = await w.next_text()
        assert done.startswith(
            "R1 done: PR #41 on up/stream · https://github.com/up/stream/pull/41"
        )
        assert "up/stream is upstream; its maintainers merge." in done
        [create] = [c for c in gh_log(w) if c["argv"][:2] == ["pr", "create"]]
        argv = create["argv"]
        assert argv[argv.index("--repo") + 1] == "up/stream"
        assert argv[argv.index("--head") + 1] == f"me:{w.run().branch}"
        assert create["hosts_token"] == PUBLIC_TOKEN
        assert (await w.say("/merge R1")) == "up/stream is upstream; its maintainers merge."
        assert not [c for c in gh_log(w) if c["argv"][:2] == ["pr", "merge"]]
        # the public_repo token went only to the fork and the upstream PR, and never leaked
        for c in gh_log(w):
            if c["hosts_token"] == PUBLIC_TOKEN:
                assert c["argv"][:2] in (
                    ["pr", "create"],
                    ["api", "-X"],
                    ["api", "repos/me/stream"],
                )
        assert PUBLIC_TOKEN not in w.everything()


async def test_chat_merge_runs_without_asking_by_default(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch)
    async with running(w):
        assert (await w.say("/run demo fix it")).startswith("On it: R1")
        assert (await w.next_text()).startswith("R1 done: PR #41")
        # no rule: merge is ok by default, so a chat merge just runs (D4)
        reply = await w.say("merge it")
        assert "R1 merged: PR #41." in reply, reply
        assert len(w.gh_calls("merge")) == 1


async def test_chat_merge_asks_when_a_rule_says_so(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w2 = make_world(tmp_path, resonant_home, chat, monkeypatch)
    async with running(w2):
        trust_store.add_rule(w2.conn, scope=REPO, action="merge", effect="ask",
                             created_from="chat", now=utcnow())  # fmt: skip
        assert (await w2.say("/run demo fix it")).startswith("On it: R1")
        assert (await w2.next_text()).startswith("R1 done: PR #41")
        prompt = await w2.say("merge it")
        assert "merge R1: PR #41 in demo " in prompt and prompt.endswith("Reply yes / no")
        assert w2.gh_calls("merge") == []

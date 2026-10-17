"""GD-1: the guard is off by default, workers load the owner's Claude setup, the Claude
credential falls back to the owner's own login, and a worker can restart or update Resonant
and work on Resonant's own repo.

Uses the harness of ``test_runner_claude.py`` (fake Claude CLI, local bare remotes, fake gh).
No test here reads the real HOME or asks the real ``claude`` anything: the harness passes a
fake owner HOME and ``conftest.never_the_real_claude_login`` answers the login probe.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from test_runner_claude import (
    CLAUDE_TOKEN,
    FAKE,
    GIT,
    GIT_ENV,
    Harness,
    build,
    check_ok,
    git,
    identity_wrap,
    until,
)

from resonant.config import LoopConfig
from resonant.gateway.channel import Channel
from resonant.loop import Loop, SlotPool
from resonant.ops import RESTART_PLANNED_KEY, mark_restart, take_planned
from resonant.runner import claude_auth
from resonant.runner.claude_auth import LoginCheck, auth_source, parse_status, status_env
from resonant.runner.claude_auth import login_status as real_login_status
from resonant.runners import claude as claude_mod
from resonant.runners.claude import RESTART_NOTE, ClaudeRunner, Refused, Started
from resonant.runners.worker_desk import asked_for
from resonant.sandbox import SandboxParams
from resonant.secrets import (
    ANTHROPIC_KEY_REF,
    CLAUDE_TOKEN_REF,
    RunnerSecrets,
    SecretStore,
)
from resonant.store import repos as repo_store
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.store.db import kv_get, utcnow

API_KEY = "sk-ant-api03-" + "Q" * 40


def owner_home(h: Harness) -> Path:
    return h.root / "owner-home"


def boom(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("the guard must not be used with runner.guard: off")


async def yes() -> bool:
    return True


async def no() -> bool:
    return False


def thin(h: Harness) -> None:
    h.runner.cfg = h.cfg.model_copy(update={"guard": "thin"})


# --- the session with the guard off ---------------------------------------------------------


async def test_guard_off_is_the_default_and_the_session_runs_as_the_owner(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, wrap=boom, self_check=boom)  # neither may be called
    assert h.cfg.guard == "off" and h.runner.guard == "off"
    await h.start()
    await h.drain()
    run = h.run()
    assert run.status == "ready" and run.pr_number == 41
    [call] = h.argv_log()
    argv, env = call["argv"], call["env"]
    # the owner's setup loads: no strict MCP, the default setting sources, no override
    assert "--strict-mcp-config" not in argv
    assert "--setting-sources" not in argv and "--settings" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    # the real HOME (here the fake owner's), so ~/.claude is the owner's
    assert env["HOME"] == str(owner_home(h))
    assert "CLAUDE_CONFIG_DIR" not in env
    run_dir = str(h.code_dir("R1"))
    assert env["TMPDIR"] == f"{run_dir}/tmp" and env["CLAUDE_CODE_TMPDIR"] == f"{run_dir}/tmp"
    path = env["PATH"].split(":")
    assert path[0] == f"{run_dir}/bin" and "/opt/homebrew/bin" in path and "/usr/bin" in path
    # one credential, the token; nothing inherited
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == CLAUDE_TOKEN and "ANTHROPIC_API_KEY" not in env
    assert "SSH_AUTH_SOCK" not in env and not any(k.startswith("RESONANT_SECRET") for k in env)
    # the per-run GitHub App token still reaches it by path, not by value
    assert env["GH_CONFIG_DIR"] == f"{run_dir}/creds/gh"
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "`resonant restart`" not in prompt  # no self_ops here: not offered


async def test_guard_thin_still_wraps_checks_and_isolates(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    wrapped: list[SandboxParams] = []
    checked: list[SandboxParams] = []

    def wrap(argv: Any, params: SandboxParams) -> list[str]:
        wrapped.append(params)
        return list(argv)

    def check(params: SandboxParams) -> tuple[bool, str]:
        checked.append(params)
        return True, "test"

    h = build(tmp_path, db, wrap=wrap, self_check=check, login_check=yes)
    thin(h)
    await h.start()
    await h.drain()
    assert h.run().status == "ready"
    assert len(wrapped) == 1 and len(checked) == 1
    assert wrapped[0].protect is None and wrapped[0].deny_exec  # the daemon's defaults
    [call] = h.argv_log()
    argv, env = call["argv"], call["env"]
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert json.loads(argv[argv.index("--settings") + 1]) == {"sandbox": {"enabled": False}}
    run_dir = str(h.code_dir("R1"))
    assert env["HOME"] == f"{run_dir}/home"
    assert env["CLAUDE_CONFIG_DIR"] == f"{run_dir}/home/.claude"


async def test_guard_thin_fails_closed_when_its_check_fails(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    def bad(_params: SandboxParams) -> tuple[bool, str]:
        return False, "write into ~/.resonant gave ok"

    h = build(tmp_path, db, wrap=identity_wrap, self_check=bad)
    thin(h)
    await h.start()
    await h.drain()
    run = h.run()
    assert run.status == "failed" and "sandbox check failed" in (run.failure or "")
    assert not (h.tmp("R1") / "argv.json").exists()  # claude never started


# --- the Claude credential ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "key", "login", "want"),
    [
        ("t", "k", True, "token"),
        ("t", None, False, "token"),
        (None, "k", True, "api_key"),
        (None, None, True, "login"),
        (None, None, False, None),
    ],
)
def test_auth_source_order(token: str | None, key: str | None, login: bool, want: str) -> None:
    assert auth_source(RunnerSecrets(None, token, key), login_ok=login) == want


async def test_api_key_alone_is_api_billing(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, login_check=yes)
    del h.keychain.items[CLAUDE_TOKEN_REF]
    h.keychain.items[ANTHROPIC_KEY_REF] = API_KEY
    h.runner.cfg = h.cfg.model_copy(update={"max_budget_usd": 2.0})
    await h.start()
    await h.drain()
    [call] = h.argv_log()
    assert call["env"]["ANTHROPIC_API_KEY"] == API_KEY
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in call["env"]
    assert call["argv"][call["argv"].index("--max-budget-usd") + 1] == "2"


async def test_the_token_wins_and_the_key_stays_out(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db)
    h.keychain.items[ANTHROPIC_KEY_REF] = API_KEY  # beside the token: it would bill the API
    h.runner.cfg = h.cfg.model_copy(update={"max_budget_usd": 2.0})
    await h.start()
    await h.drain()
    [call] = h.argv_log()
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == CLAUDE_TOKEN
    assert "ANTHROPIC_API_KEY" not in call["env"] and "--max-budget-usd" not in call["argv"]
    assert API_KEY not in json.dumps(call)


async def test_no_secret_runs_on_the_owners_own_login(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    asked: list[int] = []

    async def login() -> bool:
        asked.append(1)
        return True

    h = build(tmp_path, db, login_check=login)
    del h.keychain.items[CLAUDE_TOKEN_REF]
    assert await h.runner.setup_missing() is None
    assert asked
    res = await h.runner.start_run("proj", "fix the flaky test", "owner", report="now")
    assert isinstance(res, Started)
    await h.drain()
    assert h.run().status == "ready"
    [call] = h.argv_log()
    env = call["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["HOME"] == str(owner_home(h))  # where claude finds its stored login


async def test_no_secret_and_no_login_asks_for_a_login_first(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, login_check=no)
    del h.keychain.items[CLAUDE_TOKEN_REF]
    want = (
        f"The runner isn't set up: a Claude login (or secret {CLAUDE_TOKEN_REF} (or "
        f"{ANTHROPIC_KEY_REF})). On the Mini: log in with `claude` in the Mini's terminal, or "
        f"resonant secret set {CLAUDE_TOKEN_REF}."
    )
    assert await h.runner.setup_missing() == want
    assert await h.runner.start_run("proj", "x", "owner") == Refused(want)
    assert runs_store.recent(db) == []


async def test_the_thin_guard_never_uses_the_login(tmp_path: Path, db: sqlite3.Connection) -> None:
    asked: list[int] = []

    async def login() -> bool:
        asked.append(1)
        return True

    h = build(tmp_path, db, login_check=login)
    thin(h)
    del h.keychain.items[CLAUDE_TOKEN_REF]
    text = await h.runner.setup_missing()
    assert text == (
        f"The runner isn't set up: secret {CLAUDE_TOKEN_REF} (or {ANTHROPIC_KEY_REF}). "
        f"On the Mini: resonant secret set {CLAUDE_TOKEN_REF}."
    )
    assert asked == []


async def test_the_default_login_check_asks_the_sessions_claude_with_the_owners_home(
    tmp_path: Path, db: sqlite3.Connection, never_the_real_claude_login: list[tuple[str, ...]]
) -> None:
    h = build(tmp_path, db)
    del h.keychain.items[CLAUDE_TOKEN_REF]
    assert await h.runner.setup_missing() is not None  # the probe says no
    [asked] = never_the_real_claude_login
    assert asked[-1] == str(owner_home(h)) and str(FAKE) in asked


async def test_login_check_caches_both_answers() -> None:
    now = [0.0]
    answers = [True, False]
    calls: list[tuple[tuple[str, ...], Path]] = []

    def probe(claude: Any, home: Path) -> bool:
        calls.append((tuple(claude), home))
        return answers[len(calls) - 1]

    check = LoginCheck(lambda: ["claude"], Path("/h"), probe=probe, clock=lambda: now[0])
    assert await check() is True and await check() is True and len(calls) == 1
    now[0] = 601.0
    assert await check() is False and len(calls) == 2
    now[0] = 650.0
    assert await check() is False and len(calls) == 2  # a no is asked again after 60 s
    missing = LoginCheck(lambda: None, Path("/h"), probe=boom)
    assert await missing() is False  # not installed: never probed


@pytest.mark.parametrize(
    ("rc", "out", "ok"),
    [
        (0, '{"loggedIn": true, "authMethod": "claude.ai"}', True),
        (0, '{"loggedIn": false}', False),
        (1, '{"loggedIn": true}', False),
        (0, "Logged in", False),
        (2, "", False),
    ],
)
def test_parse_status(rc: int, out: str, ok: bool) -> None:
    assert parse_status(rc, out) is ok


def test_login_status_runs_auth_status_with_no_secret_in_env(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, '{"loggedIn": true, "email": "x"}', "")

    assert real_login_status(["/x/claude"], tmp_path, run=run) is True
    assert seen["argv"] == ["/x/claude", "auth", "status", "--json"]
    env = seen["env"]
    assert env == status_env(tmp_path) and env["HOME"] == str(tmp_path)
    assert not {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"} & set(env)

    def broken(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 1)

    assert real_login_status(["/x/claude"], tmp_path, run=broken) is False
    assert claude_auth.login_status is not real_login_status  # the conftest guard is on


# --- Resonant's own repo --------------------------------------------------------------------


def daemon_checkout(h: Harness) -> Path:
    """The daemon's own checkout (``ops.repo_path``): a clone of the ``resonant`` remote whose
    origin is its GitHub URL, outside ``code_dir`` like ``~/resonant``."""
    path = h.root / "daemon-checkout"
    subprocess.run(
        [GIT, "clone", "-q", str(h.remotes["resonant"]), str(path)], env=GIT_ENV, check=True
    )
    git(path, "remote", "set-url", "origin", "git@github.com:own/resonant.git")
    return path


async def test_the_daemons_checkout_is_the_resonant_repo(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, repos=("proj", "resonant"))
    own = daemon_checkout(h)
    # not in runner.repos and not registered: the daemon recognises its own checkout
    h.runner.cfg = h.cfg.model_copy(update={"repos": {"proj": h.cfg.repos["proj"]}})
    db.execute("DELETE FROM repos WHERE slug = 'own/resonant'")
    h.runner.self_repo = own
    await h.runner.start()
    repo = repo_store.find(db, "resonant")
    assert repo is not None and repo.slug == "own/resonant" and repo.path == own
    assert repo.source == "owner"
    await h.runner.start()  # idempotent
    assert [r.slug for r in repo_store.list_all(db)].count("own/resonant") == 1

    head = git(own, "rev-parse", "HEAD")
    res = await h.runner.start_run("resonant", "fix the typo in the README", "owner")
    assert isinstance(res, Started) and res.repo == "resonant"
    await h.drain()
    run = h.run()
    assert run.status == "ready", run.failure
    assert run.checkout == str(own)
    assert run.workspace == str(h.cfg.code_dir / ".worktrees" / "resonant" / "R1")
    [call] = h.argv_log()
    prompt = call["argv"][call["argv"].index("--append-system-prompt") + 1]
    assert "This repo is Resonant itself" in prompt and "`resonant update`" in prompt
    # the daemon's checkout is untouched: same branch, same commit, clean
    assert git(own, "rev-parse", "HEAD") == head
    assert git(own, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(own, "status", "--porcelain") == ""


async def test_a_configured_entry_for_the_daemons_repo_gets_its_path(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, repos=("proj", "resonant"))
    own = daemon_checkout(h)
    h.runner.self_repo = own
    await h.runner.start()
    repo = repo_store.find(db, "resonant")
    assert repo is not None and repo.path == own


async def test_the_registry_keeps_its_own_entry_for_the_daemons_repo(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """Already registered under another handle (say, by first use): kept as it is, no
    rename, and only a missing path is filled in."""
    h = build(tmp_path, db, repos=("proj", "resonant"))
    own = daemon_checkout(h)
    h.runner.cfg = h.cfg.model_copy(update={"repos": {"proj": h.cfg.repos["proj"]}})
    db.execute("UPDATE repos SET name = 'res', source = 'auto' WHERE slug = 'own/resonant'")
    h.runner.self_repo = own
    await h.runner.start()
    repo = repo_store.get(db, "own/resonant")
    assert repo is not None and repo.name == "res" and repo.source == "auto"
    assert repo.path == own
    assert repo_store.find(db, "resonant") is None or repo_store.find(db, "resonant") == repo
    elsewhere = tmp_path / "elsewhere"
    repo_store.set_path(db, "own/resonant", elsewhere)
    await h.runner.start()
    again = repo_store.get(db, "own/resonant")
    assert again is not None and again.path == elsewhere  # the owner's path wins


async def test_the_thin_guard_never_registers_the_daemons_checkout(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, repos=("proj", "resonant"))
    own = daemon_checkout(h)
    h.runner.cfg = h.cfg.model_copy(
        update={"repos": {"proj": h.cfg.repos["proj"]}, "guard": "thin"}
    )
    db.execute("DELETE FROM repos WHERE slug = 'own/resonant'")
    h.runner.self_repo = own
    await h.runner.start()
    assert repo_store.find(db, "resonant") is None


async def test_runs_without_a_github_token_dont_see_the_owners_gh_login(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="scratch")
    res = await h.runner.start_run(None, "how full is the disk?", "owner")
    assert isinstance(res, Started)
    await h.drain()
    [call] = h.argv_log()
    empty = h.code_dir("R1") / "gh-none"
    assert call["env"]["GH_CONFIG_DIR"] == str(empty) and empty.is_dir()
    assert list(empty.iterdir()) == []


async def test_a_failed_session_on_the_login_asks_again(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    probes: list[int] = []

    def probe(claude: Any, home: Path) -> bool:
        probes.append(1)
        return True

    h = build(tmp_path, db, scenario="error")
    h.runner._login_check = LoginCheck(  # pyright: ignore[reportPrivateUsage]
        lambda: ["claude"], owner_home(h), probe=probe
    )
    del h.keychain.items[CLAUDE_TOKEN_REF]
    await h.start()
    await h.drain()
    assert h.run().status == "failed"
    before = len(probes)
    assert await h.runner.login_ok() is True and len(probes) == before + 1  # not cached


async def test_no_checkout_registers_nothing(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db)
    h.runner.self_repo = tmp_path / "not-a-checkout"
    await h.runner.start()
    assert repo_store.find(db, "resonant") is None


# --- restart and update from a worker -------------------------------------------------------


class FakeOps:
    """``tools.ops.Ops`` as the desk uses it: records the call and writes the restart
    marker like the real one (the detached helper is left out)."""

    def __init__(self, conn: sqlite3.Connection, *, up_to_date: bool = False) -> None:
        self.conn = conn
        self.calls: list[str] = []
        self.up_to_date = up_to_date

    async def restart_resonant(self) -> dict[str, Any]:
        self.calls.append("restart")
        mark_restart(self.conn, by="owner", commit="abc1234", now=utcnow())
        return {"label": "com.resonant.daemon", "restarting": True, "delay_s": 3}

    async def update_resonant(self) -> dict[str, Any]:
        self.calls.append("update")
        if self.up_to_date:
            return {"old": "abc1234", "new": "abc1234", "updated": False, "synced": None,
                    "restarting": False}  # fmt: skip
        mark_restart(self.conn, by="owner", commit="def5678", now=utcnow())
        return {"old": "abc1234", "new": "def5678", "updated": True, "synced": True,
                "restarting": True, "delay_s": 3}  # fmt: skip


class Trust:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[Any, ...]] = []

    async def request_check(self, run_code: str, system: str, action: str,
                            irreversible: bool = False, cost_usd: float = 0.0,
                            what: str = "") -> Any:  # fmt: skip
        self.calls.append((run_code, system, action, what))
        return self.answer


def cli_calls(h: Harness, code: str = "R1") -> list[dict[str, Any]]:
    path = h.tmp(code) / "cli.json"
    return json.loads(path.read_text()) if path.exists() else []


@pytest.mark.parametrize(
    ("ask", "op", "ok"),
    [
        ("fix the login bug in resonant, then restart it", "restart", True),
        ("fix it and redeploy it", "restart", True),
        ("deploy resonant", "update", True),
        ("merge it and update resonant", "update", True),
        ("then /restart", "restart", True),
        ("fix the typo in the README", "restart", False),
        ("update the docs in resonant", "update", False),
        ("write a deploy script for dori", "restart", False),
        ("fix the restart bug in resonant", "restart", False),
        ("deploy the fix", "update", False),
    ],
)
def test_asked_for(ask: str, op: Any, ok: bool) -> None:
    assert asked_for(ask, op) is ok


async def test_a_worker_restarts_resonant_and_its_run_resumes(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """The owner asked for a restart: ``resonant restart`` goes through without a check, the
    reply goes out, the restart (a graceful stop, as launchd's SIGTERM gives) stops the
    session, and the next daemon resumes the same session with a note; no attempt spent."""
    ops = FakeOps(db)
    trust = Trust("denied")  # never asked: the ask covers it
    h = build(tmp_path, db, extra=["--self-op", "restart"], self_ops=ops, owner="owner",
              trust=trust)  # fmt: skip
    code = await h.start(ask="fix the flaky test, then restart resonant")
    running = asyncio.ensure_future(h.loop.run())
    await until(lambda: ops.calls == ["restart"], what="the restart")
    await until(lambda: bool(cli_calls(h)), what="the CLI's reply")
    [call] = cli_calls(h)
    assert call["argv"] == ["restart"] and call["rc"] == 0
    system = h.argv_log(code)[0]["argv"]
    assert "launchctl" in system[system.index("--append-system-prompt") + 1]
    assert "Resonant restarts in about 3 s" in call["out"] and "sleep 120" in call["out"]
    assert trust.calls == []
    assert RESTART_NOTE in (h.run(code).resume_prompt or "")
    # launchd stops the daemon: the loop's graceful stop
    h.loop.stop()
    await asyncio.wait_for(running, 30)
    stopped = h.run(code)
    assert stopped.pgid is None and stopped.attempts == 0 and stopped.step == "session"
    task = task_store.get_task(db, cast(str, stopped.task_id))
    assert task is not None and task.status == "queued" and task.attempts == 0
    assert kv_get(db, RESTART_PLANNED_KEY) is not None

    # the next daemon: the runner starts first, then the loop resumes the task
    loop2 = Loop(db, {}, LoopConfig(tick_s=0.05, stuck_after_s=120), SlotPool(3, 1))
    runner2 = ClaudeRunner(
        db,
        runner=h.cfg,
        channel=cast(Channel, h.chan),
        secrets=SecretStore(h.keychain),
        workspace=h.ws,
        create_task=loop2.create_task,
        cancel_task=loop2.cancel,
        wrap=identity_wrap,
        self_check=check_ok,
        claude_cmd=[sys.executable, str(FAKE)],
        poll_s=0.05,
        kill_grace_s=0.5,
        github_app=h.app,
        home=owner_home(h),
        self_ops=ops,
        owner="owner",
    )
    loop2.runners[runner2.name] = runner2
    await runner2.start()
    assert kv_get(db, RESTART_PLANNED_KEY) is None  # taken once
    assert loop2.resume() == 0  # requeued by the graceful stop, not a crash
    await asyncio.wait_for(loop2.run_until_idle(), 60)
    await runner2.aclose()
    final = h.run(code)
    assert final.status == "ready" and final.attempts == 0 and final.sessions == 2
    calls = h.argv_log(code)
    assert len(calls) == 2
    assert calls[1]["argv"][calls[1]["argv"].index("--resume") + 1] == final.session_id
    assert "Your session was interrupted" in calls[1]["prompt"]
    assert RESTART_NOTE in calls[1]["prompt"]
    assert final.resume_prompt is None  # cleared when the resumed turn ended
    assert ops.calls == ["restart"]  # the resumed turn doesn't restart again
    rows = db.execute("SELECT detail FROM audit_log WHERE action = 'run.self_op'").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["detail"]) == {"run": code, "op": "restart", "by_ask": True,
                                             "ok": True}  # fmt: skip


async def test_a_restart_the_ask_didnt_mention_is_a_check(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    ops = FakeOps(db)
    trust = Trust("denied")
    h = build(tmp_path, db, extra=["--self-op", "restart"], self_ops=ops, owner="owner",
              trust=trust)  # fmt: skip
    await h.start(ask="fix the flaky test")
    await h.drain()
    assert trust.calls == [("R1", "resonant", "restart", "restart Resonant (the daemon itself)")]
    assert ops.calls == []
    [call] = cli_calls(h)
    assert call["rc"] == 1 and "denied" in call["out"]
    assert "Didn't restart" in (h.run().summary or "")


async def test_a_check_ok_lets_the_restart_through(tmp_path: Path, db: sqlite3.Connection) -> None:
    ops = FakeOps(db, up_to_date=True)
    trust = Trust("ok")
    # not the owner's run: the ask's words don't count, the check decides
    h = build(tmp_path, db, extra=["--self-op", "update"], self_ops=ops, owner="someone-else",
              trust=trust)  # fmt: skip
    await h.start(ask="deploy resonant")
    await h.drain()
    assert [c[2] for c in trust.calls] == ["update"] and ops.calls == ["update"]
    [call] = cli_calls(h)
    assert call["rc"] == 0
    assert call["out"].startswith("Already up to date (abc1234): nothing to deploy, no restart.")
    assert h.run().status == "ready"  # no restart: the session carried on to its PR


async def test_update_from_a_worker_says_what_it_deployed(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    ops = FakeOps(db)
    h = build(tmp_path, db, extra=["--self-op", "update"], self_ops=ops, owner="owner")
    code = await h.start(ask="merge it and update resonant")
    running = asyncio.ensure_future(h.loop.run())
    await until(lambda: bool(cli_calls(h)), what="the CLI's reply")
    [call] = cli_calls(h)
    assert call["rc"] == 0
    assert call["out"].startswith("Updated abc1234 -> def5678, synced. Resonant restarts")
    h.loop.stop()
    await asyncio.wait_for(running, 30)
    assert h.run(code).attempts == 0 and RESTART_NOTE in (h.run(code).resume_prompt or "")


async def test_no_ops_means_no_restart(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, extra=["--self-op", "restart"], owner="owner")
    await h.start(ask="restart resonant")
    await h.drain()
    [call] = cli_calls(h)
    assert call["rc"] == 1 and "isn't set up here" in call["out"]


async def test_a_requested_restart_is_not_counted_as_a_crash(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """A session killed hard by a requested restart (no graceful stop): the startup reap
    doesn't spend the run's one crash. Without the marker, it does."""
    h = build(tmp_path, db)
    for planned, want in ((True, 0), (False, 1)):
        res = await h.runner.start_run("proj", f"x {planned}", "owner")
        assert isinstance(res, Started)
        runs_store.update(db, res.code, status="running", step="session")
        runs_store.set_process(db, res.code, 2**22 + 7, 2**22 + 7, 1.0)  # long gone
        if planned:
            mark_restart(db, by="owner", commit=None, now=utcnow())
        await h.runner.start()
        run = h.run(res.code)
        assert run.pgid is None and run.attempts == want, (planned, run.attempts)
    assert take_planned(db, utcnow()) is False


def test_the_worker_prompt_says_how_to_restart() -> None:
    prompt = claude_mod.RESTART_PROMPT
    assert "`resonant restart`" in prompt and "`resonant update`" in prompt
    assert "launchctl" in prompt


async def test_the_ask_covers_one_restart_per_run(tmp_path: Path, db: sqlite3.Connection) -> None:
    """A second ``resonant restart`` in the same run (say, a resumed session looping) is a
    check even though the ask mentioned restarting."""
    ops = FakeOps(db, up_to_date=True)
    trust = Trust("denied")
    h = build(tmp_path, db, extra=["--self-op", "update", "--self-op", "update"], self_ops=ops,
              owner="owner", trust=trust)  # fmt: skip
    await h.start(ask="deploy resonant")
    await h.drain()
    assert ops.calls == ["update"]
    assert [c[2] for c in trust.calls] == ["update"]
    assert [c["rc"] for c in cli_calls(h)] == [0, 1]

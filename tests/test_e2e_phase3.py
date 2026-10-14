"""Phase 3 acceptance (docs/phase3-plan.md §1): the whole daemon, in process, with fakes.

chat.db is a FakeChatDB, osascript a RecordingOsaRunner, the model a ScriptedModel and the
Keychain a FakeKeychain. Extensions are tiny temp ``.py`` extensions installed with
``resonant.e2e.extensions.install_fixture`` and run by the real process runner. Each Phase 3
ticket adds its own scenarios here.

Until ``/<ext> <tool>`` commands land (P3-14), extension calls are driven by
``ExtScriptRunner``: it sends each call in its checkpoint through the daemon's own gate and
executor inside a loop step.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import logging
import socket
import sqlite3
import sys
import urllib.parse
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import psutil
import pytest

import resonant_sdk.emit
from resonant.components import Components
from resonant.config import Settings, load_settings
from resonant.daemon import Daemon
from resonant.e2e import RecordingOsaRunner, ScriptedModel, wait_until
from resonant.e2e.extensions import install_fixture
from resonant.e2e.fakes import DEFAULT_FINAL_REPLY
from resonant.extensions.host import tampered_text
from resonant.extensions.notify import mute
from resonant.extensions.scheduler import LAST_TICK_KEY
from resonant.gate import Allow, Intent
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop import NewTask
from resonant.loop.types import PRIORITY_OWNER, Done, StepContext, StepOutcome, Task
from resonant.models import ChatMessage, ModelReply, SpanFactory
from resonant.monitor import health_snapshot
from resonant.ops.fakes import FakeOpsRunner
from resonant.router.fast_path import KILL_REPLY, RESUME_REPLY
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store.db import iso, kv_set, open_db, transaction, utcnow
from resonant.store.extensions import get_extension, mark_due
from resonant.store.tasks import unrouted_events
from resonant_sdk import Event, ToolSpec

OWNER_PHONE = "+15551234567"
SELF = "resonant.agent@icloud.com"
PRINCIPALS = f"""\
principals:
  justin:
    role: owner
    identities: ["imessage:{OWNER_PHONE}"]
"""
# Has '/', '+' and a space, so the URL and JSON forms differ from the raw one.
SENTINEL = "Sentinel/Value+42 xyz"

DUMP_MANIFEST = """\
name: example
version: 1
secrets: [token]
tools:
  dump:
    run: ./dump.py ok
    effect: read
    secrets: [token]
  plain:
    run: ./dump.py ok
    effect: read
  fail:
    run: ./dump.py fail
    effect: read
    secrets: [token]
"""
# Prints its whole environment, plus the secret raw, URL-encoded, as JSON \\u escapes (only
# raw after JSON decoding) and base64 at the three alignments; also to stderr and fd 3.
DUMP_PY = r"""
import base64, json, os, sys, urllib.parse
s = os.environ.get("RESONANT_SECRET_TOKEN", "")
forms = {}
if s:
    sys.stderr.write("stderr " + s + "\n")
    event = {"type": "dump", "text": "event " + s, "dedupe_key": "k:" + s}
    os.write(3, (json.dumps(event) + "\n").encode())
    forms = {
        "raw": s,
        "url": urllib.parse.quote(s, safe=""),
        "b64": [base64.b64encode(b"\0" * k + s.encode()).decode() for k in range(3)],
    }
if sys.argv[1] == "fail":
    print(s)
    sys.exit(1)
u = "".join("\\u%04x" % ord(c) for c in s)
body = json.dumps({"env": dict(os.environ), "forms": forms})
print(body[:-1] + ', "u": "' + u + '"}')
"""
ALLOWED_ENV = {
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "USER",
    "LOGNAME",
    "RESONANT_EXT",
    "RESONANT_EXT_DATA",
    "RESONANT_EVENTS_FD",
    "GIT_TERMINAL_PROMPT",
}
# macOS's interpreter adds this to its own environ at startup; it isn't from the daemon.
PLATFORM_ENV = {"__CF_USER_TEXT_ENCODING"}


def sentinel_forms() -> list[str]:
    """Every encoding of SENTINEL a leak could take, minus the base64 edge characters."""
    forms = [
        SENTINEL,
        urllib.parse.quote(SENTINEL, safe=""),
        json.dumps(SENTINEL)[1:-1],
        "".join(f"\\u{ord(c):04x}" for c in SENTINEL),
    ]
    for k in range(3):
        b64 = base64.b64encode(b"\0" * k + SENTINEL.encode()).decode().rstrip("=")
        forms.append(b64[k + 1 : -2])  # the value-only interior
    return forms


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class ExtScriptRunner:
    """``runner="extscript"``: runs ``checkpoint["calls"]`` (``{extension, tool, args}``)
    through the daemon's gate and executor in one step, explicitly, recording outcomes."""

    components: Components
    name: str = "extscript"
    uses_claude_slot: bool = False
    max_step_s: float | None = 30.0
    results: dict[str, list[dict[str, Any]]] = field(default_factory=dict[str, Any])

    async def step(self, task: Task, events: list[Event], ctx: StepContext) -> StepOutcome:
        out: list[dict[str, Any]] = []
        for i, call in enumerate(task.checkpoint["calls"]):
            spec = self.components.registry.get(call["extension"], call["tool"])
            assert spec is not None and task.principal is not None
            intent = Intent.for_tool(
                spec,
                task_id=task.id,
                principal=task.principal,
                args=call.get("args", {}),
                origin="explicit",
            )
            decision = self.components.gate.evaluate(intent)
            row: dict[str, Any] = {"tool": spec.name, "decision": type(decision).__name__}
            if isinstance(decision, Allow):
                res = await self.components.executor.execute(
                    intent, decision, step_no=task.step_no, call_index=i
                )
                row |= {
                    "ok": res.ok,
                    "data": res.data,
                    "error": res.error,
                    "events": list(res.events),
                    "notice": res.notice,
                }
            out.append(row)
        self.results[task.id] = out
        return Done({"calls": len(out)})


@dataclass
class Running:
    daemon: Daemon
    osa: RecordingOsaRunner
    script: ExtScriptRunner

    @property
    def conn(self) -> sqlite3.Connection:
        return self.daemon.conn

    async def run_calls(self, *calls: dict[str, Any]) -> list[dict[str, Any]]:
        task_id = self.daemon.loop.create_task(
            NewTask(
                kind="script",
                runner="extscript",
                priority=PRIORITY_OWNER,
                principal="justin",
                checkpoint={"calls": list(calls)},
            )
        )

        def finished() -> bool:
            row = self.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return row["status"] in ("done", "failed")

        await wait_until(finished, 20)
        return self.script.results[task_id]


def settings_for(home: Path, chat: FakeChatDB) -> Settings:
    (home / "principals.yaml").write_text(PRINCIPALS)
    return load_settings(
        dry_run=False,
        api={"port": free_port()},
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
    )


class RecordingModel(ScriptedModel):
    """A ``ScriptedModel`` that keeps each ``chat_tools`` prompt and the tool names offered."""

    def __init__(self) -> None:
        super().__init__()
        self.tool_turns: list[tuple[list[ChatMessage], list[str]]] = []

    async def chat_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        max_tokens: int = 512,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        if not any(m["content"].startswith('{"tool_call"') for m in messages):
            self.tool_turns.append((list(messages), [t.name for t in tools]))
        return await super().chat_tools(messages, tools=tools, max_tokens=max_tokens, spans=spans)


@contextlib.asynccontextmanager
async def start_daemon(
    settings: Settings,
    keychain: FakeKeychain,
    osa: RecordingOsaRunner | None = None,
    *,
    model: ScriptedModel | None = None,
) -> AsyncGenerator[Running]:
    osa = osa or RecordingOsaRunner()
    daemon = Daemon(
        settings,
        model=model or ScriptedModel(),
        osa_runner=osa,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(keychain),
    )
    kv_set(daemon.conn, "imessage.selftest", {"ok": True, "at": iso(utcnow()), "detail": "ok"})
    assert daemon.state.components is not None
    script = ExtScriptRunner(daemon.state.components)
    daemon.loop.runners[script.name] = script
    run = asyncio.create_task(daemon.run())
    assert daemon.imessage is not None
    channel = daemon.imessage.channel
    await wait_until(lambda: channel.health()["last_read_at"] is not None, 10)
    try:
        yield Running(daemon, osa, script)
    finally:
        daemon.stop()
        assert await asyncio.wait_for(run, 10) == 0


def install(settings: Settings, manifest: str, files: dict[str, str]) -> None:
    conn = open_db(settings.db_path)
    try:
        install_fixture(settings, conn, manifest, files)
    finally:
        conn.close()


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[FakeChatDB]:
    db = FakeChatDB(tmp_path / "chat.db")
    yield db
    db.close()


def everything_stored(conn: sqlite3.Connection) -> str:
    """Every value in every table (executions, audit_log, spans, events, tasks, kv, …)."""
    out: list[str] = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            out.append(" ".join(str(v) for v in row))
    return "\n".join(out)


# --- E7: secrets -------------------------------------------------------------------------


async def test_e7_env_dump_only_declared_secret_and_never_stored(
    resonant_home: Path,
    chat: FakeChatDB,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("DAEMON_ONLY_VAR", "leak-me")
    settings = settings_for(resonant_home, chat)
    install(settings, DUMP_MANIFEST, {"dump.py": DUMP_PY})
    keychain = FakeKeychain(items={"example/token": SENTINEL, "example/other": "other-1234"})
    async with start_daemon(settings, keychain) as r:
        dump, plain, fail = await r.run_calls(
            {"extension": "example", "tool": "dump"},
            {"extension": "example", "tool": "plain"},
            {"extension": "example", "tool": "fail"},
        )
        # 1. The env is exactly the allowlist plus this tool's own secret; none for plain.
        assert dump["ok"] and plain["ok"], (dump, plain)
        env: dict[str, str] = dump["data"]["env"]
        assert set(env) - PLATFORM_ENV == ALLOWED_ENV | {"RESONANT_SECRET_TOKEN"}
        assert env["RESONANT_SECRET_TOKEN"] == "[redacted]"
        assert env["PATH"] == settings.extensions.process.path_env
        assert env["HOME"] == str(settings.ext_data_dir / "example")
        assert set(plain["data"]["env"]) - PLATFORM_ENV == ALLOWED_ENV
        assert "leak-me" not in json.dumps(dump["data"])
        # 2. Every form is redacted: in results, events, errors and everything stored.
        forms = dump["data"]["forms"]
        assert forms["raw"] == forms["url"] == dump["data"]["u"] == "[redacted]"
        assert all("[redacted]" in b for b in forms["b64"])
        assert dump["events"] == [
            {"type": "dump", "text": "event [redacted]", "dedupe_key": "k:[redacted]", "data": None}
        ]
        assert not fail["ok"] and fail["error"] == "ToolFailed: exit 1"
        stored = everything_stored(r.conn)
        assert "[redacted]" in stored  # the results really were stored
        logged = "\n".join(f"{rec.getMessage()} {rec.__dict__}" for rec in caplog.records)
        returned = json.dumps([dump, plain, fail])
        for form in sentinel_forms():
            assert form not in stored, form
            assert form not in logged, form
            assert form not in returned, form
        assert "other-1234" not in stored + returned  # an undeclared secret is never read
        refs = [c.argv[c.argv.index("-a") + 1] for c in keychain.calls]
        assert set(refs) == {"example/token"}


async def test_e7_missing_secret_replies_how_to_fix(resonant_home: Path, chat: FakeChatDB) -> None:
    settings = settings_for(resonant_home, chat)
    install(settings, DUMP_MANIFEST, {"dump.py": DUMP_PY})
    async with start_daemon(settings, FakeKeychain()) as r:
        (dump,) = await r.run_calls({"extension": "example", "tool": "dump"})
        assert not dump["ok"]
        assert dump["notice"] == (
            "Not done: secret example/token is missing. "
            "On the Mini: resonant secret set example/token"
        )
        assert r.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0


# --- E9: pin and tamper ------------------------------------------------------------------


async def test_e9_tampered_copy_is_disabled_and_owner_alerted_once(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    settings = settings_for(resonant_home, chat)
    install(settings, DUMP_MANIFEST, {"dump.py": DUMP_PY})
    (settings.extensions_dir / "example" / "dump.py").write_text(DUMP_PY + "# edited\n")
    osa = RecordingOsaRunner()
    alert = tampered_text("example")
    async with start_daemon(settings, FakeKeychain(), osa) as r:
        await osa.wait_for(1, 10)
        assert [m.text for m in osa.sent] == [alert]
        c = r.daemon.state.components
        assert c is not None and c.registry.specs("example") == []
        rec = get_extension(r.conn, "example")
        assert rec is not None and rec.status == "disabled_tampered"
    osa2 = RecordingOsaRunner()
    async with start_daemon(settings, FakeKeychain(), osa2):
        await asyncio.sleep(0.2)
        assert alert not in [m.text for m in osa2.sent]


# --- E3: chat routes to an extension (P3-11) ----------------------------------------------

REPO = Path(__file__).resolve().parents[1]
FAKE_GH = REPO / "tests/fixtures/fake_gh.py"
GH_TOKEN = "gh-token-not-real-0123456789"


def install_github(settings: Settings, root: Path) -> Path:
    """The real ``extensions/github`` copy, with a fake ``gh`` first on the child's PATH.
    Returns the fake's state file."""
    src = REPO / "extensions/github"
    files = {rel: (src / rel).read_text() for rel in ("cli.py", "README.md")}
    install(settings, (src / "resonant.yaml").read_text(), files)
    state = root / "gh" / "state.json"
    state.parent.mkdir()
    pr = {
        "number": 40,
        "title": "Router extension intents",
        "url": "https://github.com/justinhe16/resonant/pull/40",
        "isDraft": False,
        "createdAt": "2026-10-10T00:00:00Z",
        "updatedAt": "2026-10-13T00:00:00Z",
        "headRefName": "p3-11",
        "headRefOid": "abc123",
        "reviewDecision": "REVIEW_REQUIRED",
        "repository": {"nameWithOwner": "justinhe16/resonant"},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
    }
    state.write_text(json.dumps({"token": GH_TOKEN, "mine": [pr], "review": [], "runs": {}}))
    return state


def gh_settings(home: Path, chat: FakeChatDB, bin_dir: Path, state: Path) -> Settings:
    (home / "principals.yaml").write_text(PRINCIPALS)
    bin_dir.mkdir()
    shim = bin_dir / "gh"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I -S "{FAKE_GH}" "{state}" "$@"\n')
    shim.chmod(0o755)
    return load_settings(
        dry_run=False,
        api={"port": free_port()},
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
        extensions={"process": {"path": [str(bin_dir), "/usr/bin", "/bin"]}},
    )


async def test_e3_show_my_prs_routes_to_github_and_runs_prs(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    state = tmp_path / "gh" / "state.json"
    settings = gh_settings(resonant_home, chat, tmp_path / "bin", state)
    install_github(settings, tmp_path)
    keychain = FakeKeychain(items={"github/token": GH_TOKEN})
    osa = RecordingOsaRunner()
    model = RecordingModel()
    async with start_daemon(settings, keychain, osa, model=model) as r:
        chat.add_message(handle=OWNER_PHONE, text="what can you do?")
        await osa.wait_for(1, 20)
        chat.add_message(handle=OWNER_PHONE, text="show my PRs")
        await osa.wait_for(2, 20)
        await wait_until(
            lambda: (
                r.conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done', 'failed')"
                ).fetchone()[0]
                == 0
            ),
            10,
        )

        rows = r.conn.execute("SELECT checkpoint, status FROM tasks ORDER BY created_at").fetchall()
        cp = json.loads(rows[-1]["checkpoint"])
        assert rows[-1]["status"] == "done"
        assert cp["intent"] == "extension:github" and cp["title"] == "chat: extension:github"
        # Only github's chat tools: never its jobs (watch, digest), never a builtin.
        assert cp["toolset"] == [
            {"extension": "github", "name": n} for n in ("merge_pr", "pr", "prs", "rerun_failed")
        ]
        # prs ran through the gate as a model-chosen (inferred) call, with the token.
        gate = r.conn.execute(
            "SELECT extension, tool, decision, detail FROM audit_log "
            "WHERE action = 'gate.evaluate' AND extension = 'github' "
            "AND tool NOT LIKE 'job_%'"  # the scheduler may run github's watch meanwhile
        ).fetchall()
        assert [(g["tool"], g["decision"], json.loads(g["detail"])["origin"]) for g in gate] == [
            ("prs", "Allow", "inferred")
        ]
        calls = [
            json.loads(line) for line in (state.parent / "calls.jsonl").read_text().splitlines()
        ]
        assert calls and all(c["token_ok"] is True for c in calls)
        results = [
            row["result"]
            for row in r.conn.execute("SELECT result FROM executions WHERE status = 'succeeded'")
        ]
        assert sum("1 open PRs" in res for res in results) == 1
        assert GH_TOKEN not in everything_stored(r.conn)
        # The earlier reply went back to the model wrapped as untrusted, never as an
        # assistant turn; the toolset the model saw was github's alone.
        messages, tools = model.tool_turns[-1]
        assert tools == ["merge_pr", "pr", "prs", "rerun_failed"]
        history = messages[1:-1]
        assert [m["role"] for m in history] == ["user", "user"]
        assert history[1]["content"] == f"<prior_reply>{DEFAULT_FINAL_REPLY}</prior_reply>"


# --- E4 / E8 / E11: scheduled jobs and the kill switch -----------------------------------

JOBS_MANIFEST = """\
name: example
version: 1
jobs:
  - id: ping
    schedule: "0 * * * *"
    run: ./job.py ping
    effect: read
    catchup: 2h
  - id: sleeper
    schedule: "0 * * * *"
    run: ./job.py sleep
    effect: read
    catchup: 2h
"""
# ``emit.py`` is the SDK's stdlib-only helper, copied next to the script (``-I`` keeps the
# script's dir off sys.path, so it is added explicitly).
JOB_PY = """\
import json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from emit import emit
due = os.environ["RESONANT_JOB_DUE_AT"]
if sys.argv[1] == "ping":
    emit("ping", "ping " + due, "ping:" + due)
    print(json.dumps({"ok": True}))
else:
    child = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    with open(os.path.join(os.environ["RESONANT_EXT_DATA"], "child.pid"), "w") as f:
        f.write(str(child.pid))
    time.sleep(60)
"""


def audit_rows(conn: sqlite3.Connection, action: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM audit_log WHERE action = ?", (action,)).fetchall()


async def test_e8_kill_switch_kills_inflight_job_and_resume_does_not_catch_up(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    settings = settings_for(resonant_home, chat)
    emit_py = inspect.getsource(resonant_sdk.emit)
    install(settings, JOBS_MANIFEST, {"job.py": JOB_PY, "emit.py": emit_py})
    # The daemon was down for the last 90 minutes: the latest due time is caught up, once.
    conn = open_db(settings.db_path)
    with transaction(conn):
        down = utcnow() - timedelta(minutes=90)
        kv_set(conn, LAST_TICK_KEY, iso(down))
        for job in ("ping", "sleeper"):
            mark_due(conn, "example", job, down)
    conn.close()
    osa = RecordingOsaRunner()
    async with start_daemon(settings, FakeKeychain(), osa) as r:
        pidfile = settings.ext_data_dir / "example" / "child.pid"
        await wait_until(lambda: pidfile.exists() and bool(pidfile.read_text()), 10)
        await wait_until(lambda: bool(audit_rows(r.conn, "job.run")), 10)
        (run,) = audit_rows(r.conn, "job.run")
        assert run["tool"] == "job_ping"
        # E4: no task row; the run's event is persisted with its dedupe key.
        assert r.conn.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'job'").fetchone()[0] == 0
        (event,) = r.conn.execute("SELECT * FROM events WHERE source = 'ext:example'").fetchall()
        assert event["type"] == "ext.example.ping"
        assert event["dedupe_key"].startswith("ext:example:ping:")
        child = int(pidfile.read_text())
        chat.add_message(handle=OWNER_PHONE, text="/kill")
        await wait_until(lambda: bool(audit_rows(r.conn, "job.killed")), 10)
        await wait_until(lambda: process_gone(child), 10)
        await wait_until(lambda: KILL_REPLY in [m.text for m in osa.sent], 10)
        chat.add_message(handle=OWNER_PHONE, text="/resume")
        await wait_until(lambda: RESUME_REPLY in [m.text for m in osa.sent], 10)
        await asyncio.sleep(0.3)
        assert len(audit_rows(r.conn, "job.run")) == 1  # nothing caught up after /resume
        assert len(audit_rows(r.conn, "job.killed")) == 1


def process_gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


# --- E12: github watch, deterministic, exactly once ----------------------------------------

GITHUB = REPO / "extensions/github"
RESONANT = "justinhe16/resonant"
# A due time of github/watch (*/10), far enough ahead that the real clock never reaches it.
WATCH_T0 = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(days=1)


def gh_pr(number: int, sha: str) -> dict[str, Any]:
    return {
        "number": number,
        "title": "A change",
        "url": f"https://github.com/{RESONANT}/pull/{number}",
        "isDraft": False,
        "createdAt": "2026-10-10T00:00:00Z",
        "updatedAt": "2026-10-13T00:00:00Z",
        "headRefName": f"branch-{number}",
        "headRefOid": sha,
        "reviewDecision": "REVIEW_REQUIRED",
        "repository": {"nameWithOwner": RESONANT},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
    }


def gh_run(run_id: int, sha: str, conclusion: str) -> dict[str, Any]:
    return {
        "databaseId": run_id,
        "workflowName": "lint",
        "status": "completed",
        "conclusion": conclusion,
        "headSha": sha,
        "createdAt": f"2026-10-14T00:{run_id:02d}:00Z",
    }


class GhWorld:
    """The fake ``gh`` on the extensions' PATH: PR #40 is driven red/green; PR #41 was fixed
    before the install (its ``fixed`` event is part of the first run's baseline)."""

    def __init__(self, root: Path) -> None:
        self.bin = root / "gh-bin"
        self.bin.mkdir()
        self.state_path = root / "gh-state" / "state.json"
        self.state_path.parent.mkdir()
        shim = self.bin / "gh"
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" -I -S "{FAKE_GH}" "{self.state_path}" "$@"\n'
        )
        shim.chmod(0o755)
        self.pr40: list[dict[str, Any]] = []

    def set_pr40(self, sha: str, *runs: dict[str, Any]) -> None:
        self.pr40 = [*runs, *self.pr40]
        state = {
            "token": GH_TOKEN,
            "mine": [gh_pr(40, sha), gh_pr(41, "t2")],
            "review": [],
            "runs": {
                f"{RESONANT}:branch-40": self.pr40,
                f"{RESONANT}:branch-41": [gh_run(6, "t2", "success"), gh_run(5, "t1", "failure")],
            },
        }
        self.state_path.write_text(json.dumps(state))


def github_daemon(settings: Settings, osa: RecordingOsaRunner) -> Daemon:
    """The daemon's parts, not its run loop: the test drives the scheduler and the loop."""
    keychain = FakeKeychain(items={"github/token": GH_TOKEN})
    return Daemon(
        settings,
        model=ScriptedModel(),
        osa_runner=osa,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(keychain),
    )


async def watch(daemon: Daemon, k: int) -> None:
    """github/watch at its k-th due time, twice (a second tick at the same state finds
    nothing new). Routing is left to the caller."""
    for _ in range(2):
        await daemon.scheduler.tick(WATCH_T0 + timedelta(minutes=10 * k))
        await daemon.scheduler.drain()


async def test_e12_github_red_green_red_notifies_exactly_once_across_crashes(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    gh = GhWorld(tmp_path)
    (resonant_home / "principals.yaml").write_text(PRINCIPALS)
    settings = load_settings(
        dry_run=False,
        api={"port": free_port()},
        channels={"imessage": {"enabled": True, "db_path": chat.path, "self_handle": SELF}},
        extensions={"process": {"path": [str(gh.bin), "/usr/bin", "/bin"]}},
    )
    manifest = (GITHUB / "resonant.yaml").read_text()
    install(settings, manifest, {"cli.py": (GITHUB / "cli.py").read_text()})
    osa = RecordingOsaRunner()

    def texts() -> list[str]:
        return [m.text for m in osa.sent]

    def unrouted() -> list[Event]:
        return [e for e in unrouted_events(d.conn) if e.source == "ext:github"]

    # Run 1 (the first after install): #40 green, #41 just fixed. Stored as the baseline,
    # never sent: no "fixed" for a PR that was already green.
    d = github_daemon(settings, osa)
    gh.set_pr40("s1", gh_run(1, "s1", "success"))
    await watch(d, 0)
    await d.loop.run_once()
    assert texts() == []
    stored = d.conn.execute("SELECT payload FROM events WHERE source = 'ext:github'").fetchall()
    assert [json.loads(r["payload"]).get("baseline") for r in stored] == [True]

    # Red, then a crash after the event is stored and before it is sent.
    gh.set_pr40("s2", gh_run(2, "s2", "failure"))
    await watch(d, 1)
    assert len(unrouted()) == 1 and texts() == []
    d.conn.close()
    d = github_daemon(settings, osa)  # the restart delivers it, once
    await d.loop.run_once()
    assert texts() == ["[github] ✗ resonant#40 CI failed (lint)"]

    # Green, then a crash after the send and before the loop consumed the event.
    gh.set_pr40("s3", gh_run(3, "s3", "success"))
    await watch(d, 2)
    (fixed,) = unrouted()
    assert d.imessage is not None and d.imessage.notifier is not None
    await d.imessage.notifier.route(fixed)  # sent; the daemon dies before consuming it
    d.conn.close()
    d = github_daemon(settings, osa)
    await d.loop.run_once()  # routed again: the channel's dedupe record stops a resend
    assert unrouted() == []
    assert texts() == ["[github] ✗ resonant#40 CI failed (lint)", "[github] ✓ resonant#40 CI fixed"]

    # Red again, delivered by the loop.
    gh.set_pr40("s4", gh_run(4, "s4", "failure"))
    await watch(d, 3)
    await d.loop.run_once()
    await watch(d, 4)  # unchanged state: nothing new
    await d.loop.run_once()
    assert texts() == [
        "[github] ✗ resonant#40 CI failed (lint)",
        "[github] ✓ resonant#40 CI fixed",
        "[github] ✗ resonant#40 CI failed (lint)",
    ]
    assert {m.handle for m in osa.sent} == {OWNER_PHONE}

    # Muted: a new failure is stored and held, never sent (not even after the mute ends).
    mute(d.conn, "github", utcnow() + timedelta(hours=1), by="justin")
    gh.set_pr40("s5", gh_run(7, "s5", "failure"))
    await watch(d, 5)
    await d.loop.run_once()
    assert len(texts()) == 3 and unrouted() == []
    assert len(audit_rows(d.conn, "ext.notify_held")) == 2  # the baseline and this one

    # Health: the real `cli.py health` against the fake gh, through the monitor.
    assert d.imessage is not None
    await d.imessage.monitor.evaluate()
    assert health_snapshot(d.conn)["ext:github"]["state"] == "ok"
    d.conn.close()

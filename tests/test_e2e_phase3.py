"""Phase 3 acceptance (docs/phase3-plan.md §1): the whole daemon, in process, with fakes.

chat.db is a FakeChatDB, osascript a RecordingOsaRunner, the model a ScriptedModel and the
Keychain a FakeKeychain. Extensions are tiny temp ``.py`` extensions installed with
``resonant.e2e.extensions.install_fixture`` and run by the real process runner. Each Phase 3
ticket adds its own scenarios here.

The E7 cases drive extension calls with ``ExtScriptRunner``: it sends each call in its
checkpoint through the daemon's own gate and executor inside a loop step. From P3-14 on,
scenarios use ``/<ext> <tool>`` and ``/ext`` over iMessage.

Exit criteria (docs/phase3-plan.md §1) and where each is covered:

- E1, E2, E9.2: ``test_e1_e2_e9_example_install_restart_commands_and_repo_update`` (the
  install screen's details and ``--adopt``: ``test_ext_manager.py``)
- E2, E14: ``test_e2_e14_github_commands_over_imessage``, ``test_ext_commands.py``
- E3: ``test_e3_show_my_prs_routes_to_github_and_runs_prs``, ``test_router_extensions.py``
- E4: ``test_e4_morning_ping_first_run_is_baseline_then_sends_once``, ``test_scheduler.py``
- E5, E6, E11: ``test_scheduler.py`` (fake clock, ``FakeExtProcess``)
- E7: ``test_e7_*``; E8: ``test_e8_*`` here and in ``test_scheduler.py``
- E9.1: ``test_e9_*``; E9.3 and E10: ``test_ext_manager.py``
- E12: ``test_e12_*``; E13: ``test_e13_e14_*``, ``test_briefing.py``
- E15 is live: docs/go-live.md step 12.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import urllib.parse
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psutil
import pytest
from typer.testing import CliRunner

import resonant_sdk.emit
from resonant import cli
from resonant.components import Components
from resonant.config import Settings, load_settings
from resonant.daemon import Daemon
from resonant.e2e import RecordingOsaRunner, ScriptedModel, wait_until
from resonant.e2e.extensions import install_fixture
from resonant.e2e.fakes import DEFAULT_FINAL_REPLY
from resonant.extensions.host import tampered_text
from resonant.extensions.notify import mute
from resonant.extensions.pin import tree_digest
from resonant.extensions.scheduler import LAST_TICK_KEY, persist_events
from resonant.gate import Allow, Intent
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop import NewTask
from resonant.loop.types import PRIORITY_OWNER, Done, StepContext, StepOutcome, Task
from resonant.models import ChatMessage, ModelReply, SpanFactory
from resonant.monitor import health_snapshot
from resonant.ops.fakes import FakeOpsRunner
from resonant.router.fast_path import KILL_REPLY, RESUME_REPLY
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store import trust as trust_store
from resonant.store.db import iso, kv_get, kv_set, open_db, transaction, utcnow
from resonant.store.extensions import get_extension, get_job, mark_due
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
fd = os.environ.get("RESONANT_SECRETS_FD")
s = json.load(os.fdopen(int(fd), "rb"))["token"] if fd else ""
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


def settings_for(home: Path, chat: FakeChatDB, **overrides: Any) -> Settings:
    (home / "principals.yaml").write_text(PRINCIPALS)
    # core/watches (BR-7) runs every 5 minutes; off here so job.run rows are only these tests'
    overrides.setdefault("brain", {"watches": {"enabled": False}})
    return load_settings(
        dry_run=False,
        api={"port": free_port()},
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
        **overrides,
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


def install(
    settings: Settings,
    manifest: str,
    files: dict[str, str],
    argv0_paths: dict[str, str] | None = None,
) -> None:
    conn = open_db(settings.db_path)
    try:
        install_fixture(settings, conn, manifest, files, argv0_paths=argv0_paths)
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
        assert set(env) - PLATFORM_ENV == ALLOWED_ENV | {"RESONANT_SECRETS_FD"}
        assert env["RESONANT_SECRETS_FD"] == "4"  # the fd number, never the value
        assert SENTINEL not in json.dumps(env)
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
    """The real ``extensions/github`` copy, with a fake ``gh`` (``<root>/bin/gh``) pinned as
    its ``bins: [gh]``. Returns the fake's state file."""
    src = REPO / "extensions/github"
    files = {rel: (src / rel).read_text() for rel in ("cli.py", "README.md")}
    pins = {"bin:gh": str(root / "bin" / "gh")}
    install(settings, (src / "resonant.yaml").read_text(), files, pins)
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
    )  # the default extensions.process.path: gh is reached only by its pinned path


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
    # No core pulse/briefing jobs: they would add job.run rows near the hour.
    settings = settings_for(
        resonant_home, chat, briefing={"enabled": False}, pulse={"enabled": False}
    )
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


# --- E2 / E14: /<ext> <tool> and /ext (P3-14) ----------------------------------------------

MEMBER_PHONE = "+15559876543"
MEMBER_PRINCIPAL = """\
  cofounder:
    identities: ["imessage:{phone}"]
"""


def gate_rows(conn: sqlite3.Connection, extension: str) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT tool, decision, detail FROM audit_log WHERE action = 'gate.evaluate' "
        "AND extension = ? ORDER BY id",
        (extension,),
    ).fetchall()
    return [(r["tool"], r["decision"], json.loads(r["detail"])["origin"]) for r in rows]


async def test_e2_e14_github_commands_over_imessage(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    state = tmp_path / "gh" / "state.json"
    settings = gh_settings(resonant_home, chat, tmp_path / "bin", state)
    with (resonant_home / "principals.yaml").open("a") as f:
        f.write(MEMBER_PRINCIPAL.format(phone=MEMBER_PHONE))
    install_github(settings, tmp_path)
    keychain = FakeKeychain(items={"github/token": GH_TOKEN})
    osa = RecordingOsaRunner()
    async with start_daemon(settings, keychain, osa) as r:
        n = 0

        async def say(text: str, handle: str = OWNER_PHONE) -> str:
            nonlocal n
            n += 1
            chat.add_message(handle=handle, text=text)
            return (await osa.wait_for(n, 20)).text

        def manual(tool: str) -> list[tuple[str, str, str]]:
            # The scheduler may run github's jobs meanwhile (origin scheduled): left out.
            return [g for g in gate_rows(r.conn, "github") if g[0] == tool and g[2] == "explicit"]

        # An L0 read: no prompt, gated as explicit, the reply is the result's summary.
        assert await say("/github prs") == (
            "github prs: 1 open PRs, 0 failing CI, 0 awaiting your review"
        )
        assert manual("prs") == [("prs", "Allow", "explicit")]

        # merge_pr is L2 (GT-1): the owner asked for it explicitly, so it runs with no prompt.
        # The extension's own head-SHA check then refuses the stale abc1234, merging nothing.
        reply = await say("/github merge_pr resonant 40 abc1234")
        assert reply.startswith("Failed: github: merge pr "), reply
        assert manual("merge_pr") == [("merge_pr", "Allow", "explicit")]

        # A member without the github grant: not allowed, before any argument parsing.
        assert await say("/github prs", MEMBER_PHONE) == "not allowed"
        assert await say("/ext list", MEMBER_PHONE) == "not allowed"
        assert await say("/ext run github/watch", MEMBER_PHONE) == "not allowed"

        # /help github lists its tools with levels; a typo suggests and never runs.
        help_text = await say("/help github")
        assert help_text.splitlines()[0].startswith("/github: Your GitHub pull requests")
        assert "/github merge_pr <repo> <number> <head_sha>: " in help_text
        assert help_text.splitlines()[-1].endswith("(L3)")  # rerun_failed
        assert await say("/gihub prs") == "Unknown command /gihub. Did you mean /github?"
        assert (
            await say("/github pres") == "Unknown command /github pres. Did you mean /github prs?"
        )
        assert await say("/github job_watch") == (
            "Unknown command /github job_watch. Try /help github."
        )
        assert len(manual("prs")) == 1 and manual("job_watch") == []

        # /ext list: status, pin, tools and each job's next due time.
        listing = await say("/ext list")
        lines = listing.splitlines()
        assert lines[0] == "Extensions (1):"
        assert lines[1].startswith("github · enabled · a1b2c3d4 digest ")
        assert " · 4 tools" in lines[1]
        assert lines[2].startswith("- digest (L0) · next ")
        assert lines[3].startswith("- watch (L0) · next ")
        assert lines[4] == "core · built in"  # core jobs too, next due from config
        assert lines[5].startswith("- briefing (L0) · next ") and "8:00am" in lines[5]

        # /ext run github/watch: the job spec, explicit at its level, recorded as a run.
        # one event: the PR's pr_active for the rollup (GH-2), never texted
        assert await say("/ext run github/watch") == ("github/watch: done: 1 PRs watched, 1 events")
        assert manual("job_watch") == [("job_watch", "Allow", "explicit")]
        runs = [
            json.loads(row["detail"])
            for row in audit_rows(r.conn, "job.run")
            if json.loads(row["detail"]).get("manual")
        ]
        assert [run["job"] for run in runs] == ["github/watch"]
        job = get_job(r.conn, "github", "watch")
        assert job is not None and job.last_outcome == "ok"

        # /ext mute github 1h holds its notifications (kv read by the notifier); off lifts it.
        muted = await say("/ext mute github 1h")
        assert muted.startswith("Muted github until "), muted
        assert isinstance(kv_get(r.conn, "ext.github.muted_until"), str)
        assert len(audit_rows(r.conn, "ext.notify_muted")) == 1
        # A ci_failed event while muted is consumed unsent (held), never delivered, even
        # with the owner's rule that texts it (GH-2: github is quiet without one).
        sent_before = len(osa.sent)
        trust_store.add_rule(r.conn, scope="github", action="ci_failed", effect="notify_now",
                             created_from="chat", now=utcnow())  # fmt: skip
        with transaction(r.conn):
            event = {"type": "ci_failed", "text": "resonant#40 CI failed", "dedupe_key": "m1"}
            persist_events(r.conn, "github", [event])
        await wait_until(lambda: bool(audit_rows(r.conn, "ext.notify_held")), 10)
        (held,) = audit_rows(r.conn, "ext.notify_held")
        assert json.loads(held["detail"])["reason"] == "muted"
        await asyncio.sleep(0.2)
        assert len(osa.sent) == sent_before
        assert await say("/ext mute github off") == "Unmuted github."
        assert kv_get(r.conn, "ext.github.muted_until") is None
        assert GH_TOKEN not in everything_stored(r.conn)


async def test_e13_e14_ext_run_core_briefing_sends_it_now(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    settings = settings_for(resonant_home, chat)
    osa = RecordingOsaRunner()
    async with start_daemon(settings, FakeKeychain(), osa) as r:
        chat.add_message(handle=OWNER_PHONE, text="/ext run core/briefing")
        await osa.wait_for(2, 20)
        # The briefing itself (its content is P3-13's), then the command's reply.
        assert [m.text for m in osa.sent][1:] == ["Briefing sent."]
        (run,) = audit_rows(r.conn, "job.run")
        assert json.loads(run["detail"])["job"] == "core/briefing"
        chat.add_message(handle=OWNER_PHONE, text="/kill")
        chat.add_message(handle=OWNER_PHONE, text="/ext run core/briefing")
        await osa.wait_for(4, 20)
        assert [m.text for m in osa.sent[2:]] == [KILL_REPLY, "Paused: briefing not sent."]


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
    """The fake ``gh``, pinned as the extension's ``bins: [gh]``: PR #40 is driven red/green;
    PR #41 was fixed before the install (its ``fixed`` event is part of the first run's
    baseline)."""

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
    )
    manifest = (GITHUB / "resonant.yaml").read_text()
    pins = {"bin:gh": str(gh.bin / "gh")}
    install(settings, manifest, {"cli.py": (GITHUB / "cli.py").read_text()}, pins)
    osa = RecordingOsaRunner()

    def texts() -> list[str]:
        return [m.text for m in osa.sent]

    ci_types = ("ext.github.ci_failed", "ext.github.ci_fixed")

    def unrouted() -> list[Event]:
        return [e for e in unrouted_events(d.conn) if e.type in ci_types]

    # GH-2: github texts nothing on its own; the owner's rules bring CI texts back (and this
    # test keeps their exactly-once delivery across crashes).
    d = github_daemon(settings, osa)
    for action in ("ci_failed", "ci_fixed"):
        trust_store.add_rule(d.conn, scope="github", action=action, effect="notify_now",
                             created_from="chat", now=utcnow())  # fmt: skip

    # Run 1 (the first after install): #40 green, #41 just fixed. Stored as the baseline,
    # never sent: no "fixed" for a PR that was already green.
    gh.set_pr40("s1", gh_run(1, "s1", "success"))
    await watch(d, 0)
    await d.loop.run_once()
    assert texts() == []
    stored = d.conn.execute("SELECT payload FROM events WHERE type IN (?, ?)", ci_types).fetchall()
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
    assert d.imessage.pulse is not None
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


# --- E1 / E2 / E9.2: the real example, installed from a checkout by the CLI (P3-16) --------

EXAMPLE = REPO / "extensions/example"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    out = subprocess.run(
        ["git", "-C", str(cwd), *args], env=env, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def checkout_with_example(root: Path) -> Path:
    """A Resonant-like checkout with the real ``extensions/example`` committed on main."""
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    shutil.copytree(EXAMPLE, root / "extensions" / "example")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "example")
    return root


async def test_e1_e2_e9_example_install_restart_commands_and_repo_update(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(resonant_home, chat)
    # The shipped manifests name the approver `owner`, as go-live step 4 names the owner.
    (resonant_home / "principals.yaml").write_text(PRINCIPALS.replace("justin:", "owner:"))
    checkout = checkout_with_example(tmp_path / "checkout")
    keychain = FakeKeychain()
    ops = FakeOpsRunner()
    monkeypatch.setenv("RESONANT_OPS__REPO_PATH", str(checkout))

    def daemon_up(settings: Settings) -> bool:
        return True

    monkeypatch.setattr(cli, "_daemon_up", daemon_up)
    monkeypatch.setattr(cli, "_restart_runner", lambda: ops)
    monkeypatch.setattr(cli, "_secret_store", lambda: SecretStore(keychain))

    open_db(settings.db_path).close()  # the daemon has run once (go-live step 6)

    # E1: `resonant ext add repo:example`, confirmed by typing the name.
    result = CliRunner().invoke(cli.app, ["ext", "add", "repo:example"], input="example\n")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert re.fullmatch(r"example repo:example@[0-9a-f]{8} digest [0-9a-f]{8}", lines[0])
    escalations = lines.index("Escalations:")
    assert escalations < next(i for i, line in enumerate(lines) if "get_time" in line)
    assert "Type example to install" in result.output
    assert re.search(r"^Installed example repo:example@[0-9a-f]{8} ", result.output, re.M)
    copy = settings.extensions_dir / "example"
    with contextlib.closing(open_db(settings.db_path)) as conn:
        rec = get_extension(conn, "example")
    assert rec is not None and rec.status == "enabled" and rec.tree_digest == tree_digest(copy)
    assert not os.access(copy / "cli.py", os.W_OK)  # the approved copy is read-only
    (spawned,) = ops.spawned  # the reply-first restart helper, never launchctl here
    assert "resonant.ops.restart_later" in spawned

    # The restarted daemon says it's back, with the install note, then the tools work.
    osa = RecordingOsaRunner()
    async with start_daemon(settings, keychain, osa) as r:
        back = await osa.wait_for(1, 10)
        assert back.handle == OWNER_PHONE
        assert re.fullmatch(r"Back up, down \S+\nInstalled example", back.text), back.text
        n = 1

        async def say(text: str) -> str:
            nonlocal n
            n += 1
            chat.add_message(handle=OWNER_PHONE, text=text)
            return (await osa.wait_for(n, 20)).text

        # E2: an L0 read answers inline; an L3 write runs and reports.
        assert await say("/example get_time Asia/Tokyo") == "example get_time: ok"
        assert await say("/example write_note hi") == 'Done: example: write note text="hi"'
        assert (settings.ext_data_dir / "example" / "notes.txt").read_text() == "hi\n"
        help_text = await say("/help example")
        assert "/example get_time <tz>: Current time in a timezone. (L0)" in help_text
        assert "/example write_note <text>: " in help_text and "(L3)" in help_text
        assert await say("/exmaple hi") == "Unknown command /exmaple. Did you mean /example?"
        assert [g[:2] for g in gate_rows(r.conn, "example") if g[2] == "explicit"] == [
            ("get_time", "Allow"),
            ("write_note", "Allow"),
        ]

    # E9.2: an update of the checkout (what /update's pull does) never touches the copy.
    before = tree_digest(copy)
    git(checkout, "rm", "-q", "extensions/example/README.md")
    (checkout / "extensions/example/cli.py").write_text("raise SystemExit(3)\n")
    git(checkout, "commit", "-qam", "change example in the repo")
    osa2 = RecordingOsaRunner()
    async with start_daemon(settings, keychain, osa2) as r:
        chat.add_message(handle=OWNER_PHONE, text="/example get_time UTC")
        reply = await osa2.wait_for(1, 20)
        assert reply.text == "example get_time: ok"
        assert tamper_alerts(osa2) == []
        rec = get_extension(r.conn, "example")
        assert rec is not None and rec.status == "enabled"
    assert tree_digest(copy) == before and (copy / "README.md").exists()


def tamper_alerts(osa: RecordingOsaRunner) -> list[str]:
    return [m.text for m in osa.sent if "changed on disk" in m.text]


# --- E4: the example's morning-ping, notified exactly once after the baseline (P3-16) -------


def weekday_eight(tz: str, after: datetime) -> datetime:
    """The first weekday 08:00 local strictly after ``after`` (aware UTC)."""
    zone = ZoneInfo(tz)
    day = after.astimezone(zone).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, time(8), zone).astimezone(UTC)


async def test_e4_morning_ping_first_run_is_baseline_then_sends_once(
    resonant_home: Path, chat: FakeChatDB
) -> None:
    # The briefing is also due at 08:00; its own test is E13. No hourly pulse either.
    settings = settings_for(
        resonant_home, chat, briefing={"enabled": False}, pulse={"enabled": False}
    )
    files = {rel: (EXAMPLE / rel).read_text() for rel in ("cli.py", "emit.py", "README.md")}
    # Only the 08:00 job: the example's pnl (:55) and incident (*/5) jobs would also fire
    # whenever the real clock sits on their boundaries.
    manifest = (EXAMPLE / "resonant.yaml").read_text()
    head, rest = manifest.split("  # Reporters:", 1)
    manifest = head + "\ntools:" + rest.split("\ntools:", 1)[1]
    install(settings, manifest, files)
    osa = RecordingOsaRunner()
    d = github_daemon(settings, osa)  # its parts, not its run loop

    async def due(at: datetime) -> None:
        for _ in range(2):  # a second tick at the same time runs nothing more
            await d.scheduler.tick(at)
            await d.scheduler.drain()
        await d.loop.run_once()

    first = weekday_eight(settings.timezone, utcnow())
    await due(first)  # the first run after install: stored as the silent baseline
    assert [m.text for m in osa.sent] == []
    second = weekday_eight(settings.timezone, first)
    await due(second + timedelta(seconds=5))
    assert [(m.handle, m.text) for m in osa.sent] == [(OWNER_PHONE, "[example] ping")]
    # No task rows: only ext_jobs, the job.run audit rows and the two events.
    assert d.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    job = get_job(d.conn, "example", "morning-ping")
    assert job is not None and job.last_due == second and job.last_outcome == "ok"
    ran = [json.loads(r["detail"])["job"] for r in audit_rows(d.conn, "job.run")]
    assert ran == ["example/morning-ping"] * 2
    keys = [r["dedupe_key"] for r in d.conn.execute("SELECT dedupe_key FROM events")]
    assert keys == [f"ext:example:ping:{iso(first)}", f"ext:example:ping:{iso(second)}"]
    d.conn.close()

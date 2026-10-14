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
import json
import logging
import socket
import sqlite3
import sys
import urllib.parse
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from resonant.components import Components
from resonant.config import Settings, load_settings
from resonant.daemon import Daemon
from resonant.e2e import RecordingOsaRunner, ScriptedModel, wait_until
from resonant.e2e.extensions import install_fixture
from resonant.e2e.fakes import DEFAULT_FINAL_REPLY
from resonant.extensions.host import tampered_text
from resonant.gate import Allow, Intent
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop import NewTask
from resonant.loop.types import PRIORITY_OWNER, Done, StepContext, StepOutcome, Task
from resonant.models import ChatMessage, ModelReply, SpanFactory
from resonant.ops.fakes import FakeOpsRunner
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store.db import iso, kv_set, open_db, utcnow
from resonant.store.extensions import get_extension
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
            "WHERE action = 'gate.evaluate' AND extension = 'github'"
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

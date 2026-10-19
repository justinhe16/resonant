"""The gcal extension (CAL-1): Google Calendar's remote MCP server with ``auth: oauth``.

Everything talks to ``tests/fixtures/fake_gcal_server.py`` (a fake Google: an OAuth token
endpoint and Calendar MCP's tools, stdlib, on 127.0.0.1), with plain http to loopback allowed
only by monkeypatching ``checks.ALLOW_LOOPBACK_HTTP``. Nothing contacts Google, the Keychain,
~/.resonant, ~/code or claude.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from resonant.config import DEFAULT_READ_DENY, load_settings
from resonant.daemon import Daemon
from resonant.e2e import RecordingOsaRunner, ScriptedModel, fake_claude, wait_until
from resonant.e2e.extensions import install_fixture
from resonant.extensions import checks
from resonant.extensions.checks import dir_errors
from resonant.extensions.manager import ExtensionManager, ManagerError, confirm_lines
from resonant.extensions.mcp import HttpTarget, McpError, McpServerRuntime, approve, pins_json
from resonant.extensions.mcp_http import (
    HTTP_MESSAGE_CAP,
    OAuthCredentials,
    OAuthToken,
    discover_http,
)
from resonant.extensions.process import ProcessTracker
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.runners.ask import AskRunner, AskScope
from resonant.secrets import CLAUDE_TOKEN_REF, FakeKeychain, SecretStore
from resonant.store.db import iso, kv_set, utcnow
from resonant.wiring import tool_access
from resonant_sdk import Effect, Level, Manifest, load_manifest
from resonant_sdk.manifest import McpHttpServer, parse_yaml

REPO = Path(__file__).resolve().parents[1]
EXT = REPO / "extensions" / "gcal"
GCAL_YAML = (EXT / "resonant.yaml").read_text()
GOOGLE_MCP = "https://calendarmcp.googleapis.com/mcp/v1"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
READS = ("list_calendars", "list_events", "get_event", "search_events", "suggest_time")
WRITES = ("create_event", "update_event", "delete_event", "respond_to_event")
LISTED = (*READS, *WRITES)


def _load_fake() -> ModuleType:
    path = Path(__file__).parent / "fixtures" / "fake_gcal_server.py"
    spec = importlib.util.spec_from_file_location("fake_gcal_server", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fake_mod = _load_fake()
FakeGoogle: Any = fake_mod.FakeGoogle
CLIENT_ID: str = fake_mod.CLIENT_ID
CLIENT_SECRET: str = fake_mod.CLIENT_SECRET
REFRESH_TOKEN: str = fake_mod.REFRESH_TOKEN
INJECTION: str = fake_mod.INJECTION
SECRETS = {
    "gcal/client_id": CLIENT_ID,
    "gcal/client_secret": CLIENT_SECRET,
    "gcal/refresh_token": REFRESH_TOKEN,
}


def gcal_yaml(google: Any) -> str:
    """The shipped manifest pointed at the fake Google."""
    return GCAL_YAML.replace(GOOGLE_MCP, google.url).replace(GOOGLE_TOKEN, google.token_url)


def creds(google: Any, **change: str) -> OAuthCredentials:
    values = {
        "token_url": google.token_url,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        **change,
    }
    return OAuthCredentials(**values)


@pytest.fixture
def loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "ALLOW_LOOPBACK_HTTP", True)


@pytest.fixture
def google(loopback: None) -> Iterator[Any]:
    server = FakeGoogle().start()
    yield server
    server.stop()


# --- the manifest and its levels ----------------------------------------------------------


def test_shipped_manifest_levels() -> None:
    m = load_manifest(EXT / "resonant.yaml")
    assert m.mcp is not None
    server = m.mcp.servers["calendar"]
    assert isinstance(server, McpHttpServer)
    assert server.url == GOOGLE_MCP and server.auth == "oauth" and server.secret is None
    assert server.oauth is not None and server.oauth.token_url == GOOGLE_TOKEN
    assert server.secrets == ("client_id", "client_secret", "refresh_token")
    assert set(m.secrets) == set(server.secrets)
    specs = {s.name: s for s in m.tool_specs()}
    assert set(specs) == {*LISTED, "draft_event"}
    for name in (*READS, "draft_event"):
        assert specs[name].effect is Effect.READ and specs[name].effective_level is Level.L0
    for name in WRITES:  # every change asks a plain yes; each can email people
        spec = specs[name]
        assert spec.effect is Effect.WRITE and spec.effective_level is Level.L2
        assert spec.commit and not spec.standing  # never "yes always"
    assert all(s.secrets == () for s in specs.values())  # no tool process gets a secret
    assert dir_errors(EXT, m) == []
    assert "calendar" in m.description.lower() and m.examples


def test_calendar_writes_never_take_a_standing_allow(db: sqlite3.Connection) -> None:
    """standing: false: no "yes always" offer, and even an allow rule for the tool (from
    /rules allow, or one saved before) never lets it run unasked. Other tools still can."""
    from datetime import UTC, datetime

    from resonant.gate.types import Intent
    from resonant.store import trust as trust_store
    from resonant.trust.tools import StandingAllows

    specs = {s.name: s for s in load_manifest(EXT / "resonant.yaml").tool_specs()}
    now = datetime(2026, 10, 19, tzinfo=UTC)
    for name in WRITES:
        trust_store.add_rule(db, scope="gcal", action=f"tool:gcal.{name}", effect="allow",
                             created_from="command", now=now)  # fmt: skip
    standing = StandingAllows(db)
    for name in WRITES:
        intent = Intent.for_tool(specs[name], task_id="t", principal="justin", args={})
        assert standing.scope(intent) is None
        assert standing.offer(intent) is None and standing.allows(intent) is None
    other = specs["create_event"].model_copy(update={"standing": True})
    intent = Intent.for_tool(other, task_id="t", principal="justin", args={})
    assert standing.allows(intent) is not None  # the same rule widens a standing tool
    with pytest.raises(ValidationError, match="side effect"):
        specs["list_events"].model_validate(
            {**specs["list_events"].model_dump(), "standing": False}
        )


def test_draft_event_is_local_and_has_no_secrets() -> None:
    m = load_manifest(EXT / "resonant.yaml")
    tool = m.tools["draft_event"]
    assert tool.run.startswith("./cli.py draft ") and tool.secrets == ()


def _server(**change: Any) -> dict[str, Any]:
    server: dict[str, Any] = {
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "auth": "oauth",
        "oauth": {
            "token_url": "https://oauth.example.com/token",
            "client_id": "client_id",
            "client_secret": "client_secret",
            "refresh_token": "refresh_token",
        },
        "tools": [{"name": "a", "effect": "read"}],
    }
    server.update(change)
    return {k: v for k, v in server.items() if v is not None}


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"oauth": None}, "auth: oauth needs"),
        ({"auth": None}, "auth: oauth needs"),
        ({"auth": "bearer"}, "auth: bearer needs"),
        ({"secret": "client_id"}, "auth: bearer"),
        (
            {
                "oauth": {
                    "token_url": "http://oauth.example.com/token",
                    "client_id": "client_id",
                    "client_secret": "client_secret",
                    "refresh_token": "refresh_token",
                }
            },
            "token_url: url must be https",
        ),
        (
            {
                "oauth": {
                    "token_url": "https://o.example.com/t",
                    "client_id": "client_id",
                    "client_secret": "client_id",
                    "refresh_token": "refresh_token",
                }
            },
            "must differ",
        ),
        (
            {
                "oauth": {
                    "token_url": "https://o.example.com/t",
                    "client_id": "nope",
                    "client_secret": "client_secret",
                    "refresh_token": "refresh_token",
                }
            },
            "not declared",
        ),
        (
            {
                "oauth": {
                    "token_url": "https://o.example.com/t",
                    "client_id": "client_id",
                    "client_secret": "client_secret",
                    "refresh_token": "refresh_token",
                    "scope": "x",
                }
            },
            "Extra inputs",
        ),
    ],
)
def test_oauth_server_refusals(change: dict[str, Any], match: str) -> None:
    data = {
        "name": "x",
        "version": 1,
        "secrets": ["client_id", "client_secret", "refresh_token"],
        "mcp": {"servers": {"s": _server(**change)}},
    }
    with pytest.raises(ValidationError, match=match):
        Manifest.model_validate(data)


def test_a_loopback_token_url_is_for_tests_only() -> None:
    text = GCAL_YAML.replace(GOOGLE_TOKEN, "http://127.0.0.1:8123/token")
    m = Manifest.model_validate(parse_yaml(text))
    assert dir_errors(EXT, m) == ["mcp server calendar: oauth token_url: url must be https"]


# --- OAuth against the fake ---------------------------------------------------------------


async def test_discover_exchanges_the_refresh_token_and_lists_every_tool(google: Any) -> None:
    tools = await discover_http(google.url, creds(google))
    assert [t["name"] for t in tools] == [*LISTED, "list_colors", "hidden", "leak"]
    (form,) = google.token_requests
    assert form == {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    # the MCP server only ever saw the access token
    sent = json.dumps(google.requests)
    for value in (REFRESH_TOKEN, CLIENT_SECRET):
        assert value not in sent
    assert all(r["headers"]["Authorization"] == "Bearer ya29.fake-1" for r in google.requests)


async def test_a_refused_refresh_token_is_definite_and_never_reaches_mcp(google: Any) -> None:
    with pytest.raises(McpError) as e:
        await discover_http(google.url, creds(google, refresh_token="1//revoked-token-x"))
    assert e.value.reason == "the OAuth token endpoint refused the refresh token (invalid_grant)"
    assert INJECTION not in str(e.value)
    assert google.requests == []


@pytest.mark.parametrize(
    ("status", "why"),
    [
        (302, "redirected (http 302); redirects are refused"),
        (500, "failed (http 500)"),
        (200, "malformed answer"),  # an html body
    ],
)
async def test_token_endpoint_failures(google: Any, status: int, why: str) -> None:
    google.token_status = status
    with pytest.raises(McpError, match=re.escape(why)) as e:
        await OAuthToken(creds(google)).get()
    assert "IGNORE" not in str(e.value)


async def test_an_unreachable_token_endpoint_is_definite() -> None:
    bad = OAuthCredentials("https://127.0.0.1:9/token", "a", "b", "c")
    with pytest.raises(McpError, match=r"not reachable|https"):
        await OAuthToken(bad).get()


def test_credentials_never_show_in_a_repr() -> None:
    c = OAuthCredentials("https://o.example.com/t", CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN)
    text = repr(c) + repr(OAuthToken(c))
    assert CLIENT_SECRET not in text and REFRESH_TOKEN not in text and CLIENT_ID not in text


async def test_the_access_token_is_cached_until_it_nearly_expires(google: Any) -> None:
    now = [1000.0]
    tok = OAuthToken(creds(google), clock=lambda: now[0])
    assert await tok.get() == "ya29.fake-1"
    now[0] += 3000  # 600 s left: still good
    assert await tok.get() == "ya29.fake-1" and tok.fetches == 1
    now[0] += 500  # 100 s left, inside OAUTH_EARLY_S: refreshed
    assert await tok.get() == "ya29.fake-2" and tok.fetches == 2
    tok.expire("ya29.other")  # someone else's: kept
    assert await tok.get() == "ya29.fake-2"
    tok.expire("ya29.fake-2")
    assert await tok.get() == "ya29.fake-3"
    assert tok.text("a ya29.fake-1 b ya29.fake-3") == "a [redacted] b [redacted]"


@pytest.fixture
async def runtime(google: Any, tmp_path: Path) -> AsyncIterator[McpServerRuntime]:
    found = approve("calendar", (*LISTED, "leak"), await discover_http(google.url, creds(google)))
    assert not found.problems
    google.requests.clear()
    google.token_requests.clear()
    store = SecretStore(FakeKeychain(items=dict(SECRETS)))
    m = Manifest.model_validate(parse_yaml(gcal_yaml(google)))
    assert m.mcp is not None
    server = m.mcp.servers["calendar"]
    assert isinstance(server, McpHttpServer)
    rt = McpServerRuntime(
        ext="gcal",
        server_id="calendar",
        argv=(),
        root=tmp_path,
        data_dir=tmp_path / "data",
        path_env="/usr/bin:/bin",
        bins={},
        secret_names=server.secrets,
        approved={name: p["pin"] for name, p in pins_json(found.pins).items()},
        tracker=ProcessTracker(sample_s=0.2, grace_s=0.5),
        secrets=lambda: store,
        message_cap=HTTP_MESSAGE_CAP,
        http=HttpTarget(server.url, None, server.oauth),
    )
    yield rt
    await rt.aclose()


async def test_runtime_reads_with_one_token_and_redacts_it(
    runtime: McpServerRuntime, google: Any
) -> None:
    out = await runtime.call("list_events", {"startTime": "2026-10-20T00:00:00Z"}, 10)
    assert json.loads(out["text"])["events"][0]["id"] == "ev1"
    await runtime.call("list_calendars", {}, 10)
    assert len(google.token_requests) == 1  # one exchange for the whole session
    leaked = (await runtime.call("leak", {}, 10))["text"]
    assert "ya29." not in leaked and "[redacted]" in leaked


async def test_a_revoked_access_token_gets_a_new_one_and_retries_once(
    runtime: McpServerRuntime, google: Any
) -> None:
    await runtime.call("list_calendars", {}, 10)
    google.revoke()  # Google dropped the token mid-session (expired early, revoked)
    out = await runtime.call("list_calendars", {}, 10)
    assert "primary" in out["text"]
    assert len(google.token_requests) == 2
    assert [c[0] for c in google.calls] == ["list_calendars", "list_calendars"]


async def test_a_stateless_server_retries_a_refused_token_once_too(
    runtime: McpServerRuntime, google: Any
) -> None:
    google.stateless = True  # a server that never issues Mcp-Session-Id
    await runtime.call("list_calendars", {}, 10)
    assert all("Mcp-Session-Id" not in r["headers"] for r in google.requests)
    google.revoke()
    assert "primary" in (await runtime.call("list_calendars", {}, 10))["text"]
    assert len(google.token_requests) == 2 and len(google.calls) == 2


async def test_a_bad_refresh_token_in_the_keychain_runs_nothing(
    google: Any, runtime: McpServerRuntime
) -> None:
    store = SecretStore(FakeKeychain(items={**SECRETS, "gcal/refresh_token": "1//bad-token-x"}))
    runtime.secrets = lambda: store
    with pytest.raises(McpError, match="invalid_grant"):
        await runtime.call("create_event", {"summary": "x", "startTime": "a", "endTime": "b"}, 10)
    assert google.calls == [] and google.requests == []


async def test_unmapped_tools_are_never_called(runtime: McpServerRuntime, google: Any) -> None:
    for name in ("hidden", "list_colors"):
        with pytest.raises(McpError, match="not a listed tool"):
            await runtime.call(name, {}, 10)
    assert google.calls == []


# --- ext add: staging, the token prompt, escalations -----------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        env={**os.environ, **GIT_ENV},
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, google: Any) -> Path:
    root = tmp_path / "checkout"
    ext = root / "extensions" / "gcal"
    ext.mkdir(parents=True)
    for name in ("README.md", "cli.py"):
        shutil.copy(EXT / name, ext / name)
    (ext / "resonant.yaml").write_text(gcal_yaml(google))
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "x")
    return root


@pytest.fixture
def conn(resonant_home: Path) -> Iterator[sqlite3.Connection]:
    from resonant.store.db import open_db

    c = open_db(resonant_home / "resonant.db")
    yield c
    c.close()


class Asked(list[tuple[str, str, str]]):
    def __init__(self, answer: bool) -> None:
        super().__init__()
        self.answer = answer

    def __call__(self, sid: str, url: str, ref: str) -> bool:
        self.append((sid, url, ref))
        return self.answer


def make_manager(
    conn: sqlite3.Connection, home: Path, repo: Path, *, asked: Asked, stored: dict[str, str]
) -> ExtensionManager:
    from resonant.components import deny_all_principals

    return ExtensionManager(
        conn,
        extensions_dir=home / "extensions",
        data_dir=home / "ext-data",
        repo_path=repo,
        principals=deny_all_principals(),
        secret_exists=lambda ref: ref in stored,
        secret_value=stored.get,
        allow_token=asked,
        ext_path="/usr/bin:/bin",
        actor="owner",
    )


def test_stage_asks_before_sending_the_refresh_token_and_pins_only_listed_tools(
    conn: sqlite3.Connection, resonant_home: Path, repo: Path, google: Any
) -> None:
    asked = Asked(True)
    mgr = make_manager(conn, resonant_home, repo, asked=asked, stored=dict(SECRETS))
    staged = mgr.stage("repo:gcal")
    try:
        assert asked == [("calendar", google.token_url, "gcal/refresh_token")]
        assert set(staged.mcp_pins) == set(LISTED)  # never list_colors, hidden or leak
        lines = confirm_lines(staged)
        esc = lines[lines.index("Escalations:") + 1 : lines.index("Intent:")]
        assert (
            f'  ! mcp server "calendar" calls "{google.url}" (http, network), sends secrets '
            f'client_id, client_secret, refresh_token to "{google.token_url}" (oauth) and the '
            "access token it gets as its bearer token"
        ) in esc
        for name in WRITES:
            assert any(e.startswith(f'  ! mcp tool "{name}" ("calendar") write L2') for e in esc)
            assert any(f"{name}" in line and "write L2 commit never-yes-always:" in line
                       for line in lines)  # fmt: skip
        assert any("never exposed" in n and "hidden" in n and "list_colors" in n
                   for n in staged.notes)  # fmt: skip
        assert "IGNORE" not in "\n".join(lines)
    finally:
        mgr.discard(staged)
    mgr.install(staged.pin, name="gcal")
    assert len(asked) == 1  # confirmed: not asked again


def test_stage_without_permission_sends_nothing(
    conn: sqlite3.Connection, resonant_home: Path, repo: Path, google: Any
) -> None:
    mgr = make_manager(conn, resonant_home, repo, asked=Asked(False), stored=dict(SECRETS))
    with pytest.raises(ManagerError) as e:
        mgr.stage("repo:gcal")
    assert any("gcal/refresh_token" in p and "didn't allow" in p for p in e.value.problems)
    assert google.token_requests == [] and google.requests == []


def test_stage_with_a_missing_secret_says_which(
    conn: sqlite3.Connection, resonant_home: Path, repo: Path, google: Any
) -> None:
    stored = {k: v for k, v in SECRETS.items() if k != "gcal/client_secret"}
    mgr = make_manager(conn, resonant_home, repo, asked=Asked(True), stored=stored)
    with pytest.raises(ManagerError) as e:
        mgr.stage("repo:gcal")
    assert any("resonant secret set gcal/client_secret" in p for p in e.value.problems)
    assert google.token_requests == []


# --- draft_event ---------------------------------------------------------------------------


def draft(*args: str) -> dict[str, Any]:
    out = subprocess.run(
        [sys.executable, "-I", "-S", str(EXT / "cli.py"), "draft", *args],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    return json.loads(out.stdout)


def test_draft_event_lays_out_what_create_would_do() -> None:
    d = draft(
        "--summary",
        " Lunch  with Sam ",
        "--start",
        "2026-10-23T12:00:00-07:00",
        "--end",
        "2026-10-23T13:00:00-07:00",
        "--attendees",
        "Sam@x.com, sam@x.com; b@y.org",
    )
    assert d["ok"] and d["draft"] and d["summary"] == "Lunch with Sam"
    assert d["duration_minutes"] == 60 and d["attendees"] == ["sam@x.com", "b@y.org"]
    assert "nothing was saved" in d["note"] and "2 people" in d["note"]
    alone = draft("--summary", "Focus", "--start", "2026-10-23", "--end", "2026-10-24",
                  "--attendees", "")  # fmt: skip
    assert alone["ok"] and alone["all_day"] and "email nobody" in alone["note"]


def test_draft_event_reports_problems_without_failing() -> None:
    d = draft("--summary", "", "--start", "2026-10-23T13:00:00Z", "--end",
              "2026-10-23T12:00:00Z", "--attendees", "not an email")  # fmt: skip
    assert d["ok"] is False and d["draft"] is True
    assert set(d["problems"]) == {
        "summary is empty",
        "end must be after start",
        "not an email address: not an email",
    }
    mixed = draft("--summary", "x", "--start", "2026-10-23T13:00:00Z", "--end",
                  "2026-10-23T14:00:00", "--attendees", "")  # fmt: skip
    assert mixed["problems"] == ["start and end must both have a UTC offset, or neither"]


# --- end to end: the daemon, triage, the Claude brain, the gate ------------------------------

OWNER_PHONE = "+15551234567"
SELF = "resonant.agent@icloud.com"
PRINCIPALS = f"""\
principals:
  justin:
    role: owner
    identities: ["imessage:{OWNER_PHONE}"]
"""
FAKE_CLAUDE = Path(fake_claude.__file__)
CLAUDE_TOKEN = "sk-ant-oat01-FAKETOKENVALUE0123456789"
CALENDAR = {"handler": "claude", "kind": "calendar", "say": "looking at your calendar"}
CREATE = {"summary": "Lunch with Sam", "startTime": "2026-10-23T12:00:00-07:00",
          "endTime": "2026-10-23T13:00:00-07:00"}  # fmt: skip


@dataclass
class World:
    daemon: Daemon
    osa: RecordingOsaRunner
    chat: FakeChatDB
    model: ScriptedModel
    plan: Path
    notes: Path
    google: Any

    @property
    def conn(self) -> sqlite3.Connection:
        return self.daemon.conn

    def claude(self, **plan: Any) -> None:
        self.plan.write_text(json.dumps(plan))

    def rows(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, args).fetchall())

    def ask_tasks(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM tasks WHERE runner = 'ask' ORDER BY rowid")

    def session(self) -> dict[str, Any]:
        return json.loads((self.notes / "session.json").read_text())

    def google_calls(self, name: str) -> int:
        return sum(1 for c in self.google.calls if c[0] == name)

    async def say(self, text: str, replies: int = 1) -> list[str]:
        n = len(self.osa.sent)
        self.chat.add_message(handle=OWNER_PHONE, text=text)
        await self.osa.wait_for(n + replies, 20)
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


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def start(
    home: Path,
    chat: FakeChatDB,
    tmp_path: Path,
    google: Any,
    *,
    install: bool = True,
    brain: dict[str, Any] | None = None,
) -> AsyncGenerator[World]:
    (home / "principals.yaml").write_text(PRINCIPALS)
    code = tmp_path / "code"
    code.mkdir(exist_ok=True)
    settings = load_settings(
        dry_run=False,
        api={"port": free_port()},
        channels={
            "imessage": {"enabled": True, "db_path": chat.path, "poll_s": 0.05, "self_handle": SELF}
        },
        runner={"code_dir": code},
        brain={"mode": "loop", "read_roots": [code], **(brain or {})},
    )
    keychain = FakeKeychain(items={CLAUDE_TOKEN_REF: CLAUDE_TOKEN, **SECRETS})
    if install:
        found = approve("calendar", LISTED, await discover_http(google.url, creds(google)))
        assert not found.problems
        from resonant.store.db import open_db

        db = open_db(settings.db_path)
        try:
            install_fixture(
                settings,
                db,
                gcal_yaml(google),
                {"cli.py": (EXT / "cli.py").read_text()},
                mcp_pins=pins_json(found.pins),
            )
        finally:
            db.close()
        google.calls.clear()
    osa = RecordingOsaRunner()
    model = ScriptedModel(script=[])
    daemon = Daemon(settings, model=model, osa_runner=osa, secrets=SecretStore(keychain))
    kv_set(daemon.conn, "imessage.selftest", {"ok": True, "at": iso(utcnow()), "detail": "ok"})
    kv_set(daemon.conn, "autonomy", "on")
    kv_set(daemon.conn, "paused", False)
    assert daemon.imessage is not None and daemon.state.components is not None
    brain_runner = daemon.imessage.brain
    assert brain_runner is not None
    brain_runner.runner_gaps = None
    notes = tmp_path / "claude-notes"
    notes.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"answer": "Nothing to report."}))
    components = daemon.state.components
    asks = AskRunner(
        daemon.conn,
        cfg=settings.brain.ask_claude,
        scope=AskScope((code,), DEFAULT_READ_DENY),
        ask_root=home / "ask",
        channel=daemon.imessage.channel,
        secrets=SecretStore(keychain),
        owner="justin",
        create_task=daemon.loop.create_task,
        claude_cmd=[
            sys.executable,
            "-I",
            str(FAKE_CLAUDE),
            "--script",
            str(plan),
            "--notes",
            str(notes),
        ],
        home=tmp_path / "claude-home",
        tools=tool_access(components, brain_runner, settings),
        poll_s=0.05,
        kill_grace_s=0.5,
        session=settings.brain.claude_session,
    )
    components.runners[asks.name] = asks
    daemon.loop.runners[asks.name] = asks
    run = asyncio.create_task(daemon.run())
    channel = daemon.imessage.channel
    await wait_until(lambda: channel.health()["last_read_at"] is not None, 10)
    w = World(daemon, osa, chat, model, plan, notes, google)
    try:
        yield w
    finally:
        await brain_runner.idle()
        daemon.stop()
        assert await asyncio.wait_for(run, 10) == 0


async def test_calendar_read_runs_create_asks_then_yes_runs_it_once(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path, google: Any, loopback: None
) -> None:
    async with start(resonant_home, chat, tmp_path, google) as w:
        w.model.triage = CALENDAR
        w.claude(
            calls=[
                {"name": "list_events", "args": {"startTime": "2026-10-23T00:00:00Z"}},
                {"name": "create_event", "args": CREATE},
            ],
            answer="Free at noon. {r1}",
        )
        ack, answer, prompt = await w.say("put lunch with Sam on my calendar friday at noon", 3)
        assert ack == CALENDAR["say"]
        assert answer.startswith("Claude: Free at noon. asked the owner:")
        assert "Reply yes" in prompt and "yes always" not in prompt  # the read tainted it
        await w.prompt_recorded()
        # the read ran without asking; the create waits on the owner
        assert w.google_calls("list_events") == 1 and w.google_calls("create_event") == 0
        (ask,) = w.ask_tasks()
        cp = json.loads(ask["checkpoint"])
        assert cp["kind"] == "general" and cp["calendar_ext"] == "gcal"
        assert cp.get("session") is True and cp["tools"] is True
        assert (ask["status"], ask["wait_reason"]) == ("waiting", "approval")
        # Claude got the extension's tools over the bridge, never the unlisted ones
        names = {t["name"] for t in w.session()["tools"]}
        assert {*LISTED, "draft_event"} <= names
        assert not {"hidden", "list_colors", "leak"} & names
        seen = json.dumps(w.session())
        for value in (REFRESH_TOKEN, CLIENT_SECRET, "ya29."):
            assert value not in seen
        await w.say("yes")
        await wait_until(lambda: w.google_calls("create_event") == 1, 10)
        await wait_until(lambda: w.ask_tasks()[0]["status"] == "done", 10)
        await asyncio.sleep(0.3)  # nothing runs twice
        assert w.google_calls("create_event") == 1
        (_, args) = next(c for c in google.calls if c[0] == "create_event")
        assert args == CREATE


async def test_an_untainted_calendar_write_still_asks_without_yes_always(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path, google: Any, loopback: None
) -> None:
    from resonant.store import trust as trust_store

    async with start(resonant_home, chat, tmp_path, google) as w:
        # an allow rule for the tool (as /rules allow would save) still never runs it unasked
        trust_store.add_rule(w.conn, scope="gcal", action="tool:gcal.create_event",
                             effect="allow", created_from="command", now=utcnow())  # fmt: skip
        w.model.triage = CALENDAR
        w.claude(calls=[{"name": "create_event", "args": CREATE}], answer="{r0}")
        _, answer, prompt = await w.say("put lunch with Sam on my calendar friday at noon", 3)
        assert "asked the owner:" in answer
        assert "Reply yes" in prompt and "yes always" not in prompt
        (ask,) = w.ask_tasks()
        assert json.loads(ask["checkpoint"])["tainted"] is False  # no read before it
        assert w.google_calls("create_event") == 0


async def test_calendar_asks_keep_the_connector_path_without_the_extension(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path, google: Any, loopback: None
) -> None:
    async with start(resonant_home, chat, tmp_path, google, install=False) as w:
        w.model.triage = CALENDAR
        ack, answer = await w.say("what's on my calendar tomorrow", 2)
        assert ack == CALENDAR["say"]
        assert "Calendar isn't reachable" in answer  # BR-5's connector check, as before
        (ask,) = w.ask_tasks()
        cp = json.loads(ask["checkpoint"])
        assert cp["kind"] == "calendar" and "calendar_ext" not in cp
        assert cp.get("session") is not True
        assert google.calls == []


def test_calendar_routing_needs_the_extensions_calendar_read() -> None:
    """A loaded gcal whose MCP tools aren't offered (only the local draft_event) keeps the
    connector path: draft_event alone can't say what's on the calendar."""
    from types import SimpleNamespace

    from resonant.runners.ask import AskRunner as Runner
    from resonant_sdk import ToolSpec

    def spec(name: str) -> ToolSpec:
        return ToolSpec(name=name, effect=Effect.READ, extension="gcal", source="ext:gcal")

    offered: list[ToolSpec] = [spec("draft_event")]

    def tools_for(_principal: str) -> list[ToolSpec]:
        return offered

    fake = SimpleNamespace(
        cfg=SimpleNamespace(calendar_extension="gcal", resonant_tools=True),
        tools=SimpleNamespace(tools_for=tools_for),
    )
    assert Runner._calendar_extension(fake, "justin") is None  # type: ignore[arg-type]
    offered.append(spec("list_events"))
    assert Runner._calendar_extension(fake, "justin") == "gcal"  # type: ignore[arg-type]


async def test_calendar_extension_off_in_config_keeps_the_old_path(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path, google: Any, loopback: None
) -> None:
    brain = {"ask_claude": {"calendar_extension": None}}
    async with start(resonant_home, chat, tmp_path, google, brain=brain) as w:
        w.model.triage = CALENDAR
        await w.say("what's on my calendar tomorrow", 2)
        (ask,) = w.ask_tasks()
        assert json.loads(ask["checkpoint"])["kind"] == "calendar"
        assert google.calls == []


def test_gcal_gets_a_router_intent_with_its_examples() -> None:
    from resonant.router.intents import IntentSet

    m = load_manifest(EXT / "resonant.yaml")
    intents = IntentSet.from_manifests([m])
    ext = intents.extension("extension:gcal")
    assert ext is not None and "what's on my calendar tomorrow" in ext.examples

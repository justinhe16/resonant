"""MCP servers over streamable HTTP (MCP-2) and the linear example extension.

Everything talks to ``tests/fixtures/fake_mcp_http_server.py`` (a stdlib HTTP server in a
thread on 127.0.0.1, with Linear-like tools), with plain http to loopback allowed only by
monkeypatching ``checks.ALLOW_LOOPBACK_HTTP``. Nothing contacts Linear.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import shutil
import sqlite3
import subprocess
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from pydantic import ValidationError

from resonant.components import build_components, deny_all_principals
from resonant.config import Settings, load_settings
from resonant.e2e.extensions import install_fixture
from resonant.e2e.fakes import LABEL_RULES
from resonant.executor import CallContext, Executor, ToolFailed
from resonant.extensions import checks
from resonant.extensions.checks import dir_errors, mcp_url_error
from resonant.extensions.host import ExtensionHost
from resonant.extensions.manager import (
    ExtensionManager,
    ManagerError,
    PinChanged,
    confirm_lines,
)
from resonant.extensions.mcp import (
    HttpTarget,
    McpAmbiguous,
    McpError,
    McpServerRuntime,
    McpToolError,
    McpToolHandler,
    approve,
    pins_json,
)
from resonant.extensions.mcp_http import HTTP_MESSAGE_CAP, McpHttpSession, discover_http
from resonant.extensions.process import ExtFailed, FakeExtProcess, ProcessTracker
from resonant.gate import Allow, Intent, NeedsApproval
from resonant.loop import Loop, NewTask, SlotPool
from resonant.ops.fakes import FakeOpsRunner
from resonant.router.intents import IntentSet
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store import extensions as ext_store
from resonant.store.tasks import insert_task
from resonant.tools import ToolRegistry
from resonant_sdk import Level, Manifest, load_manifest
from resonant_sdk.manifest import McpHttpServer, parse_yaml

REPO = Path(__file__).resolve().parents[1]
LINEAR_YAML = (REPO / "extensions" / "linear" / "resonant.yaml").read_text()
TOKEN = "lin_api_Sentinel/Value+42"


def _load_fake() -> ModuleType:
    path = Path(__file__).parent / "fixtures" / "fake_mcp_http_server.py"
    spec = importlib.util.spec_from_file_location("fake_mcp_http_server", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FakeHttpMcp: Any = _load_fake().FakeHttpMcp

TEST_TOOLS = """\
        - {name: leak, effect: read}
        - {name: sleep, effect: write, level: L2, timeout_s: 60}
        - {name: big, effect: read}
        - {name: fail, effect: write, level: L3}
        - {name: mutate, effect: write, level: L3}
"""
LISTED = (
    "list_issues",
    "get_issue",
    "list_comments",
    "list_projects",
    "list_teams",
    "save_issue",
    "save_comment",
    "leak",
    "sleep",
    "big",
    "fail",
    "mutate",
)


def linear_yaml(url: str, extra_tools: str = TEST_TOOLS) -> str:
    """The shipped linear manifest pointed at ``url``, plus the fake's test tools."""
    text = LINEAR_YAML.replace("https://mcp.linear.app/mcp", url)
    return text.rstrip("\n") + "\n" + extra_tools if extra_tools else text


@pytest.fixture
def loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "ALLOW_LOOPBACK_HTTP", True)


@pytest.fixture
def fake(loopback: None) -> Iterator[Any]:
    server = FakeHttpMcp(token=TOKEN).start()
    yield server
    server.stop()


class DriftLog(list[tuple[str, str | None]]):
    def __call__(self, server: str, tool: str, live: str | None) -> None:
        self.append((tool, live))


async def until(check: Callable[[], bool], limit_s: float = 5.0) -> bool:
    for _ in range(int(limit_s / 0.05)):
        if check():
            return True
        await asyncio.sleep(0.05)
    return check()


async def pins_for(url: str) -> dict[str, Any]:
    found = approve("linear", LISTED, await discover_http(url, TOKEN))
    assert not found.problems
    return pins_json(found.pins)


# --- the manifest -------------------------------------------------------------------------


def test_shipped_linear_manifest() -> None:
    m = load_manifest(REPO / "extensions" / "linear" / "resonant.yaml")
    assert m.mcp is not None
    server = m.mcp.servers["linear"]
    assert isinstance(server, McpHttpServer)
    assert server.url == "https://mcp.linear.app/mcp" and server.secrets == ("token",)
    levels = {s.name: s.effective_level for s in m.tool_specs()}
    reads = ("list_issues", "get_issue", "list_comments", "list_projects", "list_teams")
    assert all(levels[n] is Level.L0 for n in reads)
    assert levels["save_issue"] is Level.L2 and levels["save_comment"] is Level.L2
    assert all(s.secrets == () for s in m.tool_specs())  # only the server's bearer
    assert dir_errors(REPO / "extensions" / "linear", m) == []


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"url": "http://mcp.linear.app/mcp"}, "https"),
        ({"url": "http://127.0.0.1/mcp"}, "https"),  # loopback needs an explicit port
        ({"url": "ftp://x.example/"}, "https"),
        ({"url": "https://user:pw@x.example/mcp"}, "user info"),
        ({"url": "https://x.example/mcp#frag"}, "fragment"),
        ({"url": "https:///mcp"}, "host"),
        ({"url": "https://x.example/a b"}, "spaces"),
        ({"auth": None}, "auth: bearer"),
        ({"secret": None}, "auth: bearer"),
        ({"secret": "nope"}, "not declared"),
        ({"command": "./server.py"}, "Extra inputs"),
        ({"tools": [{"name": "a", "effect": "read", "secrets": ["token"]}]}, "no secrets"),
        ({"transport": "sse"}, "transport"),
    ],
)
def test_http_server_refusals(change: dict[str, Any], match: str) -> None:
    server: dict[str, Any] = {
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "auth": "bearer",
        "secret": "token",
        "tools": [{"name": "a", "effect": "read"}],
    }
    server.update(change)
    server = {k: v for k, v in server.items() if v is not None}
    data = {"name": "x", "version": 1, "secrets": ["token"], "mcp": {"servers": {"s": server}}}
    with pytest.raises(ValidationError, match=match):
        Manifest.model_validate(data)


def test_loopback_http_is_for_tests_only(monkeypatch: pytest.MonkeyPatch) -> None:
    assert mcp_url_error("https://mcp.linear.app/mcp") is None
    assert mcp_url_error("http://127.0.0.1:8123/mcp") == "url must be https"
    m = Manifest.model_validate(parse_yaml(linear_yaml("http://127.0.0.1:8123/mcp", "")))
    assert dir_errors(REPO / "extensions" / "linear", m) == ["mcp server linear: url must be https"]
    monkeypatch.setattr(checks, "ALLOW_LOOPBACK_HTTP", True)
    assert mcp_url_error("http://localhost:8123/mcp") is None


async def test_session_refuses_plain_http_without_the_test_switch() -> None:
    session = McpHttpSession(
        "http://127.0.0.1:9/mcp",
        token=TOKEN,
        label="t",
        tracker=ProcessTracker(),
        redact=lambda t: t,
    )
    with pytest.raises(McpError, match="https"):
        await session.start(1)


# --- the session and runtime against the fake ------------------------------------------------


@pytest.fixture
async def runtime(fake: Any, tmp_path: Path) -> AsyncIterator[McpServerRuntime]:
    pins = await pins_for(fake.url)
    fake.requests.clear()  # the discovery session's
    store = SecretStore(FakeKeychain(items={"linear/token": TOKEN}))
    rt = McpServerRuntime(
        ext="linear",
        server_id="linear",
        argv=(),
        root=tmp_path,
        data_dir=tmp_path / "data",
        path_env="/usr/bin:/bin",
        bins={},
        secret_names=("token",),
        approved={name: p["pin"] for name, p in pins.items()},
        tracker=ProcessTracker(sample_s=0.2, grace_s=0.5),
        secrets=lambda: store,
        on_drift=DriftLog(),
        message_cap=HTTP_MESSAGE_CAP,
        http=HttpTarget(fake.url, "token"),
    )
    yield rt
    await rt.aclose()


async def test_discover_lists_every_page_and_needs_the_token(fake: Any) -> None:
    tools = await discover_http(fake.url, TOKEN)
    assert [t["name"] for t in tools] == [*LISTED, "hidden"]
    with pytest.raises(McpError, match=r"refused the token \(http 401\)"):
        await discover_http(fake.url, "wrong")
    with pytest.raises(McpError, match="401"):
        await discover_http(fake.url, None)


async def test_linear_style_long_pattern_is_pinned_without_it(fake: Any) -> None:
    """LN-1: Linear's save_issue has slaBreachesAt with zod's 293-char ISO date-time pattern,
    which once refused the install (``bad 'pattern' value``). It installs; the pattern is
    neither shown nor enforced here, and the pin is the same on every listing."""
    first = await pins_for(fake.url)
    sla = first["save_issue"]["input_schema"]["properties"]["slaBreachesAt"]
    assert sla == {"anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]}
    assert first == await pins_for(fake.url)


@pytest.mark.parametrize("sse", [False, True])
async def test_calls_carry_session_version_and_bearer(
    runtime: McpServerRuntime, fake: Any, sse: bool
) -> None:
    fake.sse = sse
    out = await runtime.call("list_issues", {"assignee": "me"}, 10)
    assert json.loads(out["text"])[0]["id"] == "ENG-1"
    assert out["structured"] == {
        "issues": [{"id": "ENG-1", "title": "Flaky login test", "assignee": "me"}]
    }
    posts = [r for r in fake.requests if r["method"] == "POST"]
    init, rest = posts[0], posts[1:]
    assert init["body"]["method"] == "initialize"
    assert "Mcp-Session-Id" not in init["headers"]
    assert init["headers"]["Accept"] == "application/json, text/event-stream"
    sid = next(iter(fake.sessions))
    for r in rest:
        assert r["headers"]["Mcp-Session-Id"] == sid
        assert r["headers"]["MCP-Protocol-Version"] == "2025-06-18"
        assert r["headers"]["Authorization"] == f"Bearer {TOKEN}"
    if sse:  # the server's ping on the SSE stream got an answer
        assert await until(lambda: any(n.get("id") == "srv-ping" for n in fake.notes))
        ping = next(n for n in fake.notes if n.get("id") == "srv-ping")
        assert ping["result"] == {}


async def test_results_are_redacted_and_the_token_is_never_logged(
    runtime: McpServerRuntime, fake: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    out = await runtime.call("leak", {}, 10)
    assert out["text"] == "got: [redacted]"
    fake.status = 500
    with pytest.raises(McpAmbiguous):
        await runtime.call("leak", {}, 10)
    await runtime.tracker.kill_all()
    await runtime.aclose()
    assert TOKEN not in caplog.text


async def test_unlisted_tool_is_never_called(runtime: McpServerRuntime, fake: Any) -> None:
    with pytest.raises(McpError, match="not a listed tool"):
        await runtime.call("hidden", {}, 10)
    assert [c for c, _ in fake.calls if c == "hidden"] == []


async def test_is_error_and_rpc_errors(runtime: McpServerRuntime) -> None:
    with pytest.raises(McpToolError) as e:
        await runtime.call("fail", {}, 10)
    assert "IGNORE" not in str(e.value)
    runtime.approved = {**runtime.approved, "nope": "x"}
    with pytest.raises(McpError, match=r"^server error -32602$"):
        await runtime.call("nope", {}, 10)


async def test_timeout_is_ambiguous_and_cancelled_on_the_server(
    runtime: McpServerRuntime, fake: Any
) -> None:
    with pytest.raises(McpAmbiguous, match="timeout"):
        await runtime.call("sleep", {"seconds": 1.5}, 0.3)
    assert await until(lambda: len(fake.cancelled) == 1)
    assert (await runtime.call("get_issue", {"id": "ENG-1"}, 10))["text"].startswith("{")


async def test_output_cap_ends_the_session_then_it_restarts(runtime: McpServerRuntime) -> None:
    with pytest.raises(McpAmbiguous, match="output too large"):
        await runtime.call("big", {}, 10)
    first = runtime.session
    assert (await runtime.call("list_teams", {}, 10))["text"] == "[]"
    assert runtime.session is not first


async def test_sse_cap_per_event(runtime: McpServerRuntime, fake: Any) -> None:
    fake.sse = True
    runtime.message_cap = 64 * 1024
    with pytest.raises(McpAmbiguous, match="output too large"):
        await runtime.call("big", {}, 10)


async def test_kill_switch_cancels_in_flight_and_holds(
    runtime: McpServerRuntime, fake: Any
) -> None:
    await runtime.start()
    assert runtime.tracker.live == 1
    call = asyncio.create_task(runtime.call("sleep", {"seconds": 5}, 30))
    assert await until(lambda: any(c == "sleep" for c, _ in fake.calls))
    assert await runtime.tracker.kill_all("killed", hold=True) == 1
    with pytest.raises(McpAmbiguous, match="killed"):
        await asyncio.wait_for(call, 2)
    assert runtime.tracker.live == 0
    with pytest.raises(ExtFailed, match="paused"):
        await runtime.call("list_teams", {}, 10)
    runtime.tracker.resume()
    assert (await runtime.call("list_teams", {}, 10))["text"] == "[]"


async def test_session_expiry_restarts_and_retries_once(
    runtime: McpServerRuntime, fake: Any
) -> None:
    await runtime.call("list_teams", {}, 10)
    first = runtime.session
    fake.expire()
    assert (await runtime.call("list_teams", {}, 10))["text"] == "[]"
    assert runtime.session is not first
    assert len([c for c, _ in fake.calls if c == "list_teams"]) == 2  # the 404 never ran


async def test_pin_mismatch_at_start_disables_the_tool(
    runtime: McpServerRuntime, fake: Any
) -> None:
    fake.variant = " changed"
    await runtime.start()
    assert runtime.disabled == {"list_issues": "changed"}
    with pytest.raises(McpError, match="disabled"):
        await runtime.call("list_issues", {}, 10)
    assert (await runtime.call("list_teams", {}, 10))["text"] == "[]"


async def test_list_changed_on_the_get_stream_rechecks(
    runtime: McpServerRuntime, fake: Any
) -> None:
    await runtime.call("list_teams", {}, 10)
    assert await until(lambda: any(r["method"] == "GET" for r in fake.requests))
    assert await until(lambda: bool(fake._streams))  # pyright: ignore[reportPrivateUsage]
    await runtime.call("mutate", {}, 10)
    assert await until(lambda: "list_issues" in runtime.disabled)
    assert [t for t, _ in cast(DriftLog, runtime.on_drift)] == ["list_issues"]


async def test_no_get_stream_is_fine(runtime: McpServerRuntime, fake: Any) -> None:
    fake.get_stream = False
    assert (await runtime.call("list_teams", {}, 10))["text"] == "[]"
    await asyncio.sleep(0.2)
    assert runtime.session is not None and runtime.session.alive


@pytest.mark.parametrize(
    ("status", "error", "match"),
    [
        (401, McpError, "refused the token"),
        (403, McpError, "refused the token"),
        (400, McpError, r"refused the request \(http 400\)"),
        (429, McpError, r"http 429"),
        (500, McpAmbiguous, r"server error \(http 500\)"),
        (503, McpAmbiguous, r"http 503"),
    ],
)
async def test_http_status_outcomes(
    runtime: McpServerRuntime, fake: Any, status: int, error: type[Exception], match: str
) -> None:
    await runtime.start()
    fake.status = status
    with pytest.raises(error, match=match):
        await runtime.call("save_issue", {"title": "x"}, 10)


async def test_redirects_are_refused_and_never_followed(
    runtime: McpServerRuntime, fake: Any
) -> None:
    other = FakeHttpMcp(token=TOKEN).start()
    try:
        await runtime.start()
        fake.redirect = other.url
        with pytest.raises(McpError, match="redirects are refused"):
            await runtime.call("list_teams", {}, 10)
        assert other.requests == []
    finally:
        other.stop()


async def test_unreachable_is_definite(runtime: McpServerRuntime, fake: Any) -> None:
    fake.stop()
    with pytest.raises(McpError, match=r"did not start|not reachable"):
        await runtime.call("list_teams", {}, 5)


async def test_aclose_deletes_the_session(runtime: McpServerRuntime, fake: Any) -> None:
    await runtime.call("list_teams", {}, 10)
    sid = next(iter(fake.sessions))
    await runtime.aclose()
    assert fake.deleted[-1] == sid and sid not in fake.sessions
    assert runtime.tracker.live == 0


async def test_missing_secret_is_definite(runtime: McpServerRuntime, fake: Any) -> None:
    runtime.secrets = lambda: SecretStore(FakeKeychain())
    with pytest.raises(McpError, match="secret"):
        await runtime.call("list_teams", {}, 10)
    assert fake.requests == []  # the secret is read before anything is sent


@pytest.mark.parametrize("bad", ["two\nlines", "sp ace", ""])
async def test_bad_token_values_are_refused_before_sending(fake: Any, bad: str) -> None:
    session = McpHttpSession(
        fake.url, token=bad, label="t", tracker=ProcessTracker(), redact=lambda t: t
    )
    with pytest.raises(McpError, match="not a valid header value"):
        await session.start(2)
    assert fake.requests == []


# --- the manager (ext add) ----------------------------------------------------------------

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        env={**os.environ, **GIT_ENV},
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, fake: Any) -> Path:
    root = tmp_path / "checkout"
    ext = root / "extensions" / "linear"
    ext.mkdir(parents=True)
    shutil.copy(REPO / "extensions" / "linear" / "README.md", ext / "README.md")
    (ext / "resonant.yaml").write_text(linear_yaml(fake.url, ""))
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
    conn: sqlite3.Connection, home: Path, repo: Path, *, asked: Asked, token: str | None = TOKEN
) -> ExtensionManager:
    return ExtensionManager(
        conn,
        extensions_dir=home / "extensions",
        data_dir=home / "ext-data",
        repo_path=repo,
        principals=deny_all_principals(),
        secret_exists=lambda _ref: token is not None,
        secret_value=lambda ref: token if ref == "linear/token" else None,
        allow_token=asked,
        ext_path="/usr/bin:/bin",
        actor="owner",
    )


def test_stage_asks_before_sending_the_token_and_pins(
    conn: sqlite3.Connection, resonant_home: Path, repo: Path, fake: Any
) -> None:
    asked = Asked(True)
    mgr = make_manager(conn, resonant_home, repo, asked=asked)
    staged = mgr.stage("repo:linear")
    try:
        assert asked == [("linear", fake.url, "linear/token")]
        assert set(staged.mcp_pins) == set(LISTED[:7])
        lines = confirm_lines(staged)
        esc = lines[lines.index("Escalations:") + 1 : lines.index("Intent:")]
        assert (
            f'  ! mcp server "linear" calls "{fake.url}" (http, network), sends secret '
            "token as its bearer token"
        ) in esc
        assert any(e.startswith('  ! mcp tool "save_issue" ("linear") write L2 pin ') for e in esc)
        assert any(e.startswith('  ! mcp tool "list_issues" ("linear") read L0 pin ') for e in esc)
        assert any("never exposed" in n and "hidden" in n for n in staged.notes)
        assert "IGNORE" not in "\n".join(lines)
    finally:
        mgr.discard(staged)
    record = mgr.install(staged.pin, name="linear")
    assert asked == [("linear", fake.url, "linear/token")]  # confirmed: not asked again
    assert record.mcp_pins == staged.mcp_pins
    assert not [k for k in record.argv0_paths if k.startswith("mcp:")]


def test_stage_without_permission_never_sends_the_token(
    conn: sqlite3.Connection, resonant_home: Path, repo: Path, fake: Any
) -> None:
    mgr = make_manager(conn, resonant_home, repo, asked=Asked(False))
    with pytest.raises(ManagerError) as e:
        mgr.stage("repo:linear")
    assert any("didn't allow" in p for p in e.value.problems)
    assert fake.requests == []


def test_stage_with_no_stored_token_says_how(
    conn: sqlite3.Connection, resonant_home: Path, repo: Path, fake: Any
) -> None:
    mgr = make_manager(conn, resonant_home, repo, asked=Asked(True), token=None)
    with pytest.raises(ManagerError) as e:
        mgr.stage("repo:linear")
    assert any("resonant secret set linear/token" in p for p in e.value.problems)
    assert fake.requests == []


def test_install_refuses_a_changed_manifest_before_contacting_anyone(
    conn: sqlite3.Connection, resonant_home: Path, repo: Path, fake: Any
) -> None:
    mgr = make_manager(conn, resonant_home, repo, asked=Asked(True))
    staged = mgr.stage("repo:linear")
    mgr.discard(staged)
    other = FakeHttpMcp(token=TOKEN).start()
    try:
        yaml_path = repo / "extensions" / "linear" / "resonant.yaml"
        yaml_path.write_text(yaml_path.read_text().replace(fake.url, other.url))
        git(repo, "commit", "-qam", "move")
        with pytest.raises(PinChanged):
            mgr.install(staged.pin, name="linear")
        assert other.requests == []  # the token never went to the new URL
    finally:
        other.stop()
    assert ext_store.get_extension(conn, "linear") is None


# --- the host, gate and executor ------------------------------------------------------------


@pytest.fixture
def settings(loopback: None) -> Settings:
    s = load_settings(dry_run=False)
    s.principals_path.write_text(
        'principals:\n  justin:\n    role: owner\n    identities: ["imessage:+15551234567"]\n'
    )
    return s


async def install_linear(settings: Settings, db: sqlite3.Connection, url: str) -> None:
    found = approve("linear", LISTED[:7], await discover_http(url, TOKEN))
    install_fixture(settings, db, linear_yaml(url, ""), {}, mcp_pins=pins_json(found.pins))


async def test_host_registers_linear_tools(
    settings: Settings, db: sqlite3.Connection, fake: Any
) -> None:
    await install_linear(settings, db, fake.url)
    h = ExtensionHost.from_settings(settings, db, ToolRegistry(), runner=FakeExtProcess())
    h.secrets = SecretStore(FakeKeychain(items={"linear/token": TOKEN}))
    h.load()
    try:
        assert "linear" in h.loaded and not h.refused
        spec = h.registry.get("linear", "list_issues")
        assert spec is not None and spec.description.startswith("List or search Linear issues")
        assert "IGNORE" not in json.dumps(spec.model_dump())
        assert spec.args_schema["properties"]["limit"] == {
            "type": "integer",
            "minimum": 1,
            "maximum": 250,
        }
        handler = h.mcp_handlers[("linear", "list_issues")]
        assert isinstance(handler, McpToolHandler)
        ctx = CallContext(secrets={}, idempotency_key="k", task_id="t")
        with pytest.raises(ToolFailed):
            await handler({"limit": 0}, ctx)  # the pinned schema is enforced
        assert fake.calls == []
        assert json.loads((await handler({"assignee": "me"}, ctx))["text"])[0]["id"] == "ENG-1"
        runtime = h.loaded["linear"].mcp["linear"]
        assert runtime.message_cap == HTTP_MESSAGE_CAP
    finally:
        await h.aclose()


async def test_gate_reads_run_writes_ask(
    settings: Settings, db: sqlite3.Connection, fake: Any
) -> None:
    await install_linear(settings, db, fake.url)
    from resonant.api import DaemonState
    from resonant.store.db import utcnow

    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    state = DaemonState(settings, db, loop, started_at=utcnow())
    c = build_components(
        settings,
        db,
        state,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(FakeKeychain(items={"linear/token": TOKEN})),
        ext_runner=FakeExtProcess(),
    )
    host = c.extensions
    assert host is not None
    try:
        task = insert_task(db, NewTask(kind="k", runner="echo", priority=1))
        save = c.registry.get("linear", "save_issue")
        read = c.registry.get("linear", "list_issues")
        assert save is not None and read is not None
        ask = c.gate.evaluate(
            Intent.for_tool(save, task_id=task, principal="justin", args={"title": "x"})
        )
        assert isinstance(ask, NeedsApproval)
        i = Intent.for_tool(read, task_id=task, principal="justin", args={"assignee": "me"})
        decision = c.gate.evaluate(i)
        assert isinstance(decision, Allow)
        assert isinstance(c.executor, Executor)
        res = await c.executor.execute(i, decision, step_no=1)
        assert res.ok
        assert TOKEN not in json.dumps(res.data)
    finally:
        await host.aclose()


# --- the router -----------------------------------------------------------------------------


def test_linear_gets_a_router_intent_with_its_examples() -> None:
    m = load_manifest(REPO / "extensions" / "linear" / "resonant.yaml")
    assert not m.tools  # MCP tools only, and still an intent
    intents = IntentSet.from_manifests([m])
    ext = intents.extension("extension:linear")
    assert ext is not None
    assert "what's on my Linear" in ext.examples
    assert any("Linear ticket" in e for e in ext.examples)
    assert "extension:linear" in intents.labels


def test_scripted_model_routes_linear_texts() -> None:
    rules = dict(LABEL_RULES)
    assert rules.get("my linear") == "extension:linear"
    assert rules.get("linear ticket") == "extension:linear"


EXT_SET = REPO / "evals" / "router.extensions.example.jsonl"


async def test_extension_eval_set_routes_linear_texts() -> None:
    from resonant.e2e.fakes import ScriptedModel
    from resonant.evals.router_eval import load_set, run_eval
    from resonant.router import Labeler

    manifests = [
        load_manifest(REPO / "extensions" / n / "resonant.yaml") for n in ("github", "linear")
    ]
    intents = IntentSet.from_manifests(manifests)
    cases = load_set(EXT_SET, intents)  # every row names a real intent
    texts = {c.text: c.intent for c in cases}
    assert texts["what's on my Linear"] == "extension:linear"
    assert texts["make a Linear ticket for the flaky login test"] == "extension:linear"
    assert texts["explain linear regression in one line"] == "question"  # a near-miss
    key = [c for c in cases if c.text.startswith(("what's on my Linear", "make a Linear"))]
    results = await run_eval(key, Labeler(ScriptedModel(), intents))
    assert [r.predicted for r in results] == ["extension:linear", "extension:linear"]


# --- review follow-ups ----------------------------------------------------------------------


async def test_a_slow_tool_is_bounded_by_its_own_timeout(runtime: McpServerRuntime) -> None:
    runtime.start_timeout_s = 0.5  # was also httpx's read timeout
    assert (await runtime.call("sleep", {"seconds": 1.0}, 10))["text"] == "slept"


async def test_notification_answers_are_never_read(runtime: McpServerRuntime, fake: Any) -> None:
    fake.trickle_202 = True  # a 50 MB body, trickled: only the status matters
    loop = asyncio.get_running_loop()
    began = loop.time()
    assert (await runtime.call("list_teams", {}, 10))["text"] == "[]"
    assert loop.time() - began < 3


async def test_sse_empty_data_lines_count_toward_the_cap() -> None:
    import httpx

    from resonant.extensions import mcp_http

    session = McpHttpSession(
        "https://x.example/mcp",
        token=None,
        label="t",
        tracker=ProcessTracker(),
        redact=lambda t: t,
        message_cap=1024,
    )
    resp = httpx.Response(200, content=b"data:\n" * 5000)
    with pytest.raises(mcp_http._TooLarge):  # pyright: ignore[reportPrivateUsage]
        await session._read_events(resp, None)  # pyright: ignore[reportPrivateUsage]


async def test_a_ping_flood_is_bounded(runtime: McpServerRuntime) -> None:
    session = await runtime.ensure()
    for i in range(500):
        session._dispatch(  # pyright: ignore[reportPrivateUsage]
            json.dumps({"jsonrpc": "2.0", "id": f"p{i}", "method": "ping"}).encode()
        )
    assert len(session._side) <= 33  # pyright: ignore[reportPrivateUsage]

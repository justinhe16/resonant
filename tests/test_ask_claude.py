"""ask_claude (BR-5, docs/brain-plan.md D8): the ``ask`` runner, its argv and allowlist, the
init check, limits, kill paths and the ``ask_claude`` tool. Sessions are the fake CLI's ask
mode (``tests/fakes/claude`` with ``--restricted``); nothing here starts the real ``claude``
or reads the real login or Keychain."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psutil
import pytest

from resonant import killswitch
from resonant.config import DEFAULT_READ_DENY, AskClaudeConfig, load_settings
from resonant.executor import CallContext
from resonant.gate import Allow, Intent, PolicyGate
from resonant.gateway.channel import (
    ApprovalPrompt,
    ChannelSendError,
    MessageRef,
    SelfTestResult,
    Submit,
)
from resonant.loop.types import Done, Fail, NewTask, Task, Wait
from resonant.principals import Principal, Principals
from resonant.redact import Redactor
from resonant.runners import ask as ask_mod
from resonant.runners.ask import (
    LIMIT_KV,
    NOT_SET_UP,
    PAUSED,
    PREFIX,
    READ_BUILTINS,
    STOPPED,
    AskRefused,
    AskRunner,
    AskScope,
    Queued,
    answer_text,
    ask_argv,
    check_attachment,
    check_init,
    is_draft_tool,
    is_write_tool,
    toolset,
)
from resonant.secrets import CLAUDE_TOKEN_REF, FakeKeychain, SecretStore
from resonant.store import tasks as task_store
from resonant.store.db import kv_get, kv_set, utcnow
from resonant.tools import ToolRegistry
from resonant.tools import ask as ask_tool
from resonant.tools.base import BuiltinContext
from resonant.tools.files import ReadScope, default_roots

FAKE = Path(__file__).parent / "fakes" / "claude"
TOKEN = "sk-ant-oat01-FAKETOKENVALUE0123456789"
CAL = "mcp__claude_ai_Google_Calendar__"
GMAIL = "mcp__claude_ai_Gmail__"


# --- fakes -----------------------------------------------------------------------------------


@dataclass
class FakeChannel:
    name: str = "imessage"
    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    keys: set[str] = field(default_factory=set[str])
    fail_with: list[BaseException] = field(default_factory=list[BaseException])

    async def start(self, submit: Submit) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        if self.fail_with:
            raise self.fail_with.pop(0)
        if dedupe_key is None or dedupe_key not in self.keys:
            self.sent.append((principal, text, dedupe_key))
            if dedupe_key is not None:
                self.keys.add(dedupe_key)
        return MessageRef("imessage", f"out:{len(self.sent)}")

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        raise NotImplementedError

    async def self_test(self) -> SelfTestResult:
        return SelfTestResult(True, "fake")

    @property
    def texts(self) -> list[str]:
        return [t for _, t, _ in self.sent]


@dataclass
class Ctx:
    saved: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def save(
        self,
        *,
        checkpoint: dict[str, Any] | None = None,
        claude_session_id: str | None = None,
        worktree_path: str | None = None,
    ) -> bool:
        if checkpoint is not None:
            self.saved.append(dict(checkpoint))
        return True


@dataclass
class Harness:
    runner: AskRunner
    channel: FakeChannel
    notes: Path
    roots: list[Path]
    work: Path
    db: sqlite3.Connection

    def argv_notes(self) -> dict[str, Any]:
        return json.loads((self.notes / "argv.json").read_text())

    def pids(self) -> dict[str, int]:
        return json.loads((self.notes / "pid.json").read_text())

    def task(self, task_id: str) -> Task:
        task = task_store.get_task(self.db, task_id)
        assert task is not None
        return task

    async def ask(self, question: str = "where is mycelium?", **kw: Any) -> tuple[Any, str]:
        queued = self.runner.enqueue(question, principal=kw.pop("principal", "justin"), **kw)
        assert isinstance(queued, Queued), queued
        out = await self.runner.step(self.task(queued.task_id), [], Ctx())
        return out, queued.task_id


def make(
    db: sqlite3.Connection,
    tmp_path: Path,
    *,
    scenario: str = "answer",
    extra: tuple[str, ...] = (),
    token: bool = True,
    cfg: dict[str, Any] | None = None,
    login: bool = False,
    channel: FakeChannel | None = None,
) -> Harness:
    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    roots = [tmp_path / "code", tmp_path / "resonant", tmp_path / "missing-root"]
    for r in roots[:2]:
        r.mkdir(exist_ok=True)
    work = tmp_path / "ask"
    keychain = FakeKeychain()
    if token:
        keychain.items[CLAUDE_TOKEN_REF] = TOKEN
    scope = AskScope(tuple(roots), DEFAULT_READ_DENY)
    cmd = [sys.executable, str(FAKE), "--notes", str(notes), "--scenario", scenario, *extra]

    async def login_check() -> bool:
        return login

    ch = channel or FakeChannel()
    runner = AskRunner(
        db,
        cfg=AskClaudeConfig(**(cfg or {})),
        scope=scope,
        ask_root=work,
        channel=ch,
        secrets=SecretStore(keychain),
        owner="justin",
        timezone="America/Los_Angeles",
        claude_cmd=cmd,
        home=tmp_path / "home",
        login_check=login_check,
        kill_grace_s=0.5,
        poll_s=0.05,
    )
    return Harness(runner, ch, notes, roots, work, db)


def real(p: str | Path) -> str:
    return os.path.realpath(p)


def gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


# --- the allowlist ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "write"),
    [
        (f"{CAL}list_events", False),
        (f"{CAL}get_event", False),
        (f"{CAL}search_events", False),
        (f"{CAL}suggest_time", False),
        (f"{CAL}list_calendars", False),
        (f"{CAL}create_event", True),
        (f"{CAL}update_event", True),
        (f"{CAL}delete_event", True),
        (f"{CAL}respond_to_event", True),
        (f"{GMAIL}search_threads", False),
        (f"{GMAIL}get_thread", False),
        (f"{GMAIL}list_labels", False),
        (f"{GMAIL}send_message", True),
        (f"{GMAIL}create_draft", True),
        (f"{GMAIL}modify_thread_labels", True),
        (f"{GMAIL}trash_message", True),
        ("Read", False),
        ("WebFetch", False),
        ("mcp__x__sendMessage", True),
        ("mcp__x__send-message", True),
        ("mcp__x__sendmessage", True),
        ("mcp__x__createEvent", True),
        ("mcp__x__deleteThread", True),
        ("mcp__x__replyAll", True),
        ("mcp__x__getThread", False),
        ("mcp__x__listEvents", False),
    ],
)
def test_write_names(tool: str, write: bool) -> None:
    assert is_write_tool(tool) is write


@pytest.mark.parametrize(
    ("tool", "draft"),
    [
        (f"{GMAIL}create_draft", True),
        (f"{GMAIL}drafts", True),
        (f"{GMAIL}send_draft", False),
        (f"{GMAIL}delete_draft", False),
        (f"{GMAIL}update_draft", False),
        (f"{GMAIL}send_message", False),
        (f"{GMAIL}search_threads", False),
        ("mcp__x__createDraft", True),
        ("mcp__x__create_draft_reply", False),
        ("mcp__x__sendDraft", False),
    ],
)
def test_draft_names(tool: str, draft: bool) -> None:
    assert is_draft_tool(tool) is draft


def test_toolset_per_kind() -> None:
    cfg = AskClaudeConfig()
    for kind in ("general", "research", "image", "link"):
        ts = toolset(cfg, kind)
        assert ts.allowed == READ_BUILTINS and ts.required == READ_BUILTINS
    cal = toolset(cfg, "calendar")
    assert f"{CAL}list_events" in cal.allowed and f"{CAL}list_events" in cal.required
    assert not any(is_write_tool(t) for t in cal.allowed)
    mail = toolset(cfg, "email")
    assert f"{GMAIL}create_draft" in mail.allowed  # a draft tool, if the connector has one
    assert f"{GMAIL}create_draft" not in mail.required  # ... never required
    assert not any(t.startswith(CAL) for t in mail.allowed)


def test_write_tools_are_never_allowed_whatever_the_config_says() -> None:
    cfg = AskClaudeConfig(
        connector_tools={
            "calendar": [f"{CAL}list_events", f"{CAL}create_event", f"{CAL}respond_to_event"],
            "email": [f"{GMAIL}search_threads", f"{GMAIL}send_message"],
        },
        draft_tools=[f"{GMAIL}create_draft", f"{GMAIL}send_draft"],
    )
    cal, mail = toolset(cfg, "calendar"), toolset(cfg, "email")
    assert cal.allowed == (*READ_BUILTINS, f"{CAL}list_events")
    assert set(cal.blocked) == {f"{CAL}create_event", f"{CAL}respond_to_event"}
    assert mail.allowed == (*READ_BUILTINS, f"{GMAIL}search_threads", f"{GMAIL}create_draft")
    assert set(mail.blocked) == {f"{GMAIL}send_message", f"{GMAIL}send_draft"}
    argv = ask_argv(["claude"], cfg=cfg, scope=AskScope((), ()), kind="email",
                    api_billing=False)  # fmt: skip
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert f"{GMAIL}send_message" not in allowed and f"{GMAIL}send_message" in denied
    assert f"{GMAIL}send_draft" in denied and f"{CAL}create_event" in denied


def test_init_check() -> None:
    cfg = AskClaudeConfig()
    every = [*READ_BUILTINS, *(f"{CAL}{t}" for t in ("list_calendars", "list_events",
             "get_event", "search_events", "suggest_time", "create_event"))]  # fmt: skip
    assert check_init(every, toolset(cfg, "calendar"), "calendar") is None
    assert check_init(list(READ_BUILTINS), toolset(cfg, "general"), "general") is None
    # the probe's result: --restricted loads no connector at all
    why = check_init(list(READ_BUILTINS), toolset(cfg, "calendar"), "calendar")
    assert why is not None and why.startswith("Not done: Calendar isn't reachable")
    assert "list_events" in why
    why = check_init(["Read", "Grep", "Glob"], toolset(cfg, "general"), "general")
    assert why is not None and "WebSearch, WebFetch" in why and "CLI may have changed" in why


def _settings_deny(argv: list[str]) -> list[str]:
    return json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]


def test_argv_shape(tmp_path: Path) -> None:
    cfg = AskClaudeConfig()
    roots = [tmp_path / "a", tmp_path / "b", tmp_path / "a" / "nested", tmp_path / "gone"]
    for r in roots[:3]:
        r.mkdir(parents=True, exist_ok=True)
    scope = AskScope(tuple(roots), DEFAULT_READ_DENY)
    argv = ask_argv(["/bin/claude"], cfg=cfg, scope=scope, kind="general", api_billing=False)
    assert argv[:3] == ["/bin/claude", "-p", "--restricted"]
    flag = {k: argv[argv.index(k) + 1] for k in ("--tools", "--permission-mode",
            "--allowedTools", "--output-format", "--max-turns")}  # fmt: skip
    assert flag == {
        "--tools": "Read,Grep,Glob,WebSearch,WebFetch",
        "--permission-mode": "dontAsk",
        "--allowedTools": "Read,Grep,Glob,WebSearch,WebFetch",
        "--output-format": "stream-json",
        "--max-turns": "15",
    }
    # real top-level roots that exist: the nested one and the missing one are left out
    adds = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert adds == [os.path.realpath(roots[0]), os.path.realpath(roots[1])]
    for absent in ("bypassPermissions", "--resume", "--session-id", "--max-budget-usd",
                   "--dangerously-skip-permissions", "Bash", "Write", "Edit"):  # fmt: skip
        assert absent not in argv
    assert "--no-session-persistence" in argv and "--verbose" in argv
    deny = _settings_deny(argv)
    home = str(Path.home())
    assert f"Read(/{home}/.ssh/**)" in deny and f"Read(/{home}/Library/**)" in deny
    assert "Read(**/.env*)" in deny and "Read(**/*.pem)" in deny
    assert "Read(**/.git/config)" in deny and "Read(**/.ssh/**)" in deny
    assert not any(
        r.startswith("Read(") for r in argv[argv.index("--disallowedTools") + 1].split(",")
    )
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "Never send, create, change" in prompt and "1500 characters" in prompt
    billed = ask_argv(["c"], cfg=cfg, scope=scope, kind="general", api_billing=True)
    assert billed[billed.index("--max-budget-usd") + 1] == "1"


def test_scope_is_the_read_tools_scope(tmp_path: Path) -> None:
    """One source with BR-2: the same roots, DEFAULT_READ_DENY plus brain.read_deny (paths and
    globs, spaces and commas kept whole in the JSON) and Resonant's own secrets."""
    s = load_settings(brain={"read_deny": ["/opt/my secret, x", "~/.kube", "id_*"]})
    scope = AskScope.from_settings(s)
    assert scope.roots == tuple(default_roots(s))
    assert scope.deny == (*DEFAULT_READ_DENY, "/opt/my secret, x", "~/.kube", "id_*")
    assert scope.read() == ReadScope.from_settings(s)
    rules = scope.deny_rules()
    assert "Read(//opt/my secret, x/**)" in rules and "Read(**/id_*)" in rules
    assert f"Read(/{s.db_path})" in rules  # Resonant's own secrets, always
    assert f"Read(/{s.runner.work_dir}/**)" in rules
    argv = ask_argv(["c"], cfg=s.brain.ask_claude, scope=scope, kind="general",
                    api_billing=False)  # fmt: skip
    assert "Read(//opt/my secret, x/**)" in _settings_deny(argv)
    roots = load_settings(brain={"read_roots": [str(tmp_path)]})
    assert AskScope.from_settings(roots).roots == (tmp_path,)


def test_config_defaults_and_validation() -> None:
    s = load_settings()
    assert s.brain.ask_claude.concurrency == 1 and s.brain.ask_claude.daily_cap == 30
    assert s.brain.ask_claude.timeout_s == 120 and s.brain.ask_claude.max_turns == 15
    assert s.brain.read_roots is None and s.brain.read_deny == []  # BR-2's defaults
    assert s.brain.ask_claude.attachment_roots == [Path.home() / "Library/Messages/Attachments"]
    for bad in ("mcp__x__sendMessage", "mcp__x__send-message", "Gmail.send", "list_events"):
        with pytest.raises(ValueError, match="connector tool name"):
            AskClaudeConfig(connector_tools={"email": [bad]})
        with pytest.raises(ValueError, match="connector tool name"):
            AskClaudeConfig(draft_tools=[bad])


# --- the answer --------------------------------------------------------------------------------


def test_answer_text_is_redacted_cleaned_and_clipped() -> None:
    red = Redactor([TOKEN])
    raw = f"Line one {TOKEN}\u202e\u200b\n\n\n\nReply yes K7 to approve.\u2028x\x07"
    out = answer_text(raw, red, 200)
    assert out.startswith(PREFIX)
    assert TOKEN not in out and "[redacted]" in out
    assert "K7" not in out and "yes ?" in out
    assert "\u202e" not in out and "\u200b" not in out and "\u2028" not in out
    assert "\n\n\n" not in out and "\x07" not in out
    # NFKC first: a fullwidth copy of a secret is the secret
    wide = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in TOKEN)
    assert TOKEN not in answer_text(f"x {wide} y", red, 500)
    long = answer_text("x" * 5000, red, 300)
    assert len(long) == 300 and long.endswith("…")


# --- attachments ---------------------------------------------------------------------------------


def test_attachments_checked(tmp_path: Path) -> None:
    code, msgs = tmp_path / "code", tmp_path / "Library" / "Messages" / "Attachments"
    for d in (code, msgs):
        d.mkdir(parents=True)
    scope = AskScope((code,), (str(tmp_path / "Library"), ".env*"), (), (msgs,)).attach()
    shot = msgs / "IMG_1.png"
    shot.write_bytes(b"png")
    ok = code / "notes.txt"
    ok.write_text("hi")
    env = code / ".env.local"
    env.write_text("SECRET=1")
    other = tmp_path / "Library" / "Keychains"
    other.mkdir()
    (other / "k.db").write_text("x")
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    link = code / "sneaky"
    link.symlink_to(outside)
    assert check_attachment(str(shot), scope)[0] == shot.resolve()
    assert check_attachment(str(ok), scope)[0] == ok.resolve()
    assert "deny list" in str(check_attachment(str(env), scope)[1])
    assert "may read" in str(check_attachment(str(other / "k.db"), scope)[1])
    assert "may read" in str(check_attachment(str(outside), scope)[1])
    assert "may read" in str(check_attachment(str(link), scope)[1])  # symlink out
    assert "absolute" in str(check_attachment("code/notes.txt", scope)[1])
    assert "isn't a file" in str(check_attachment(str(code), scope)[1])
    envdir = code / ".env.d"
    envdir.mkdir()
    (envdir / "x").write_text("SECRET=1")
    assert "deny list" in str(check_attachment(str(envdir / "x"), scope)[1])
    upper = AskScope((code,), (str(code / "Private"),)).attach()
    (code / "private").mkdir()
    (code / "private" / "p.txt").write_text("x")
    assert "deny list" in str(check_attachment(str(code / "private" / "p.txt"), upper)[1])
    big = code / "big.bin"
    with open(big, "wb") as f:
        f.truncate(ask_mod.ATTACH_BYTES + 1)
    assert "MB" in str(check_attachment(str(big), scope)[1])


@pytest.mark.skipif(sys.platform != "darwin", reason="F_GETPATH is macOS")
def test_copy_rechecks_the_opened_file(tmp_path: Path) -> None:
    """A parent directory swapped for a link after the check: O_NOFOLLOW covers only the last
    component, so the opened file's own path is checked again and the copy refused."""
    code = tmp_path / "code"
    code.mkdir()
    secret = tmp_path / "ssh"
    secret.mkdir()
    (secret / "id").write_text("KEY")
    scope = AskScope((code,), (str(secret),)).attach()
    (code / "dir").symlink_to(secret)  # as if swapped in after the check passed
    with pytest.raises(OSError, match=r"deny list|may read"):
        ask_mod._copy_in(code / "dir" / "id", tmp_path / "out", scope)  # pyright: ignore[reportPrivateUsage]
    assert not (tmp_path / "out").exists()
    (code / "ok.txt").write_text("hi")
    ask_mod._copy_in(code / "ok.txt", tmp_path / "out2", scope)  # pyright: ignore[reportPrivateUsage]
    assert (tmp_path / "out2").read_text() == "hi"


# --- the runner, end to end with the fake CLI ----------------------------------------------


async def test_answered(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path, extra=("--answer", "Mycelium: ~/code/mycelium.\nProposed: none"))
    out, task_id = await h.ask("where is mycelium?")
    assert isinstance(out, Done)
    assert out.result["status"] == "answered" and out.result["untrusted"] is True
    assert out.result["proposed"] is True
    assert h.channel.sent == [("justin", f"{PREFIX}Mycelium: ~/code/mycelium.\nProposed: none",
                               f"ask:{task_id}")]  # fmt: skip
    seen = h.argv_notes()
    assert seen["creds"] == ["CLAUDE_CODE_OAUTH_TOKEN"]
    assert seen["marker"] == f"ask-{task_id}"
    assert seen["home"] == str(tmp_path / "home")
    assert "Question: where is mycelium?" in seen["prompt"]
    assert real(seen["cwd"]) == real(h.work / task_id)
    assert seen["files"] == []
    argv: list[str] = seen["argv"]
    adds = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    # the missing root is left out; roots are real paths
    assert adds == [real(h.roots[0]), real(h.roots[1])]
    assert "TOKEN" not in " ".join(argv) and TOKEN not in json.dumps(seen)
    deny = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
    assert f"Read(/{Path.home()}/.ssh/**)" in deny
    assert not (h.work / task_id).exists()  # the cwd is removed
    row = db.execute("SELECT detail FROM audit_log WHERE action = 'ask.done'").fetchone()
    assert json.loads(row["detail"])["status"] == "answered"
    assert "mycelium" not in row["detail"]  # never the text


async def test_attachments_are_copied_in(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path)
    shot = h.roots[0] / "screen.PNG"
    shot.write_bytes(b"\x89PNG")
    out, _ = await h.ask("what's this?", kind="image", attachments=[str(shot)])
    assert isinstance(out, Done) and out.result["status"] == "answered"
    seen = h.argv_notes()
    assert seen["files"] == ["attachment-1.png"]
    assert "attachment-1.png" in seen["prompt"] and "Read" in seen["prompt"]


async def test_calendar_refused_when_its_read_tools_are_missing(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """As probed on 2.1.302: --restricted loads no claude.ai connector, so a calendar ask is
    refused at the init event, and the session is killed before it answers."""
    h = make(db, tmp_path, scenario="hang", extra=("--connectors", ""))
    began = time.monotonic()
    out, _ = await h.ask("what's on tomorrow?", kind="calendar")
    assert time.monotonic() - began < 10
    assert isinstance(out, Done) and out.result["status"] == "refused"
    assert h.channel.texts[0].startswith("Not done: Calendar isn't reachable")
    assert gone(h.pids()["leader"])


async def test_calendar_with_its_tools_allows_reads_only(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    h = make(db, tmp_path, extra=("--answer", "Dentist at 3."))
    out, _ = await h.ask("what's on tomorrow?", kind="calendar")
    assert isinstance(out, Done) and out.result["status"] == "answered"
    argv: list[str] = h.argv_notes()["argv"]
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert f"{CAL}list_events" in allowed
    for write in ("create_event", "update_event", "delete_event", "respond_to_event"):
        assert f"{CAL}{write}" not in allowed  # present in the init event, never allowed
        assert f"{CAL}{write}" in argv[argv.index("--disallowedTools") + 1]


async def test_missing_builtin_refuses(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path, scenario="hang", extra=("--drop", "WebFetch"))
    out, _ = await h.ask()
    assert isinstance(out, Done) and out.result["status"] == "refused"
    assert "WebFetch" in h.channel.texts[0]


async def test_timeout_kills_the_group_and_escapees(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path, scenario="escapee", cfg={"timeout_s": 1.0})
    out, _ = await h.ask()
    assert isinstance(out, Done) and out.result["status"] == "timeout"
    assert h.channel.texts == ["Not done: Claude took over 1s; stopped."]
    pids = h.pids()
    assert gone(pids["leader"]) and gone(pids["escapee"])


async def test_kill_switch_stops_a_running_ask(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path, scenario="hang")
    queued = h.runner.enqueue("q", principal="justin")
    assert isinstance(queued, Queued)
    step = asyncio.create_task(h.runner.step(h.task(queued.task_id), [], Ctx()))
    for _ in range(100):
        if (h.notes / "pid.json").exists():
            break
        await asyncio.sleep(0.05)
    killswitch.engage(db, actor="justin")
    out = await asyncio.wait_for(step, 15)
    assert isinstance(out, Done) and out.result["status"] == "killed"
    assert h.channel.texts == [STOPPED]
    assert gone(h.pids()["leader"])
    # and nothing new starts while paused
    refused = h.runner.enqueue("q", principal="justin")
    assert refused == AskRefused(PAUSED)


async def test_cancel_kills_the_session(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path, scenario="hang")
    queued = h.runner.enqueue("q", principal="justin")
    assert isinstance(queued, Queued)
    step = asyncio.create_task(h.runner.step(h.task(queued.task_id), [], Ctx()))
    for _ in range(100):
        if (h.notes / "pid.json").exists():
            break
        await asyncio.sleep(0.05)
    step.cancel()
    with pytest.raises(asyncio.CancelledError):
        await step
    assert gone(h.pids()["leader"])


async def test_paused_before_start(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path)
    queued = h.runner.enqueue("q", principal="justin")
    assert isinstance(queued, Queued)
    killswitch.engage(db, actor="justin")
    out = await h.runner.step(h.task(queued.task_id), [], Ctx())
    assert isinstance(out, Done) and out.result["status"] == "paused"
    assert h.channel.texts == [PAUSED] and not (h.notes / "argv.json").exists()


async def test_usage_limit_backs_off_and_tells_the_owner_once(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    h = make(db, tmp_path, scenario="usage_limit")
    first = h.runner.enqueue("q1", principal="justin")
    second = h.runner.enqueue("q2", principal="justin")
    member = h.runner.enqueue("q3", principal="cofounder")
    assert isinstance(first, Queued) and isinstance(second, Queued)
    assert isinstance(member, Queued)
    out = await h.runner.step(h.task(first.task_id), [], Ctx())
    assert isinstance(out, Done) and out.result["status"] == "usage_limit"
    until = h.runner.limited_until()
    assert until is not None and until > utcnow() + timedelta(minutes=50)
    assert kv_get(db, LIMIT_KV) is not None
    # queued before the limit: no new session; the owner isn't told again
    (h.notes / "argv.json").unlink()
    out2 = await h.runner.step(h.task(second.task_id), [], Ctx())
    out3 = await h.runner.step(h.task(member.task_id), [], Ctx())
    assert isinstance(out2, Done) and isinstance(out3, Done)
    assert not (h.notes / "argv.json").exists()
    owner_texts = [t for p, t, _ in h.channel.sent if p == "justin"]
    assert len(owner_texts) == 1 and "usage limit; ask_claude is off until" in owner_texts[0]
    assert [t for p, t, _ in h.channel.sent if p == "cofounder"] == [ask_mod.LIMIT_OTHER]
    refused = h.runner.enqueue("q4", principal="justin")
    assert isinstance(refused, AskRefused) and "usage limit until" in refused.text


async def test_errors_and_no_answer(db: sqlite3.Connection, tmp_path: Path) -> None:
    for scenario, want in (
        ("error", "Not done: Claude couldn't answer (error_during_execution)."),
        ("maxturns", ask_mod.OUT_OF_TURNS),
        ("noresult", ask_mod.NO_ANSWER),
    ):
        h = make(db, tmp_path / scenario, scenario=scenario)
        out, _ = await h.ask()
        assert isinstance(out, Done) and out.result["status"] == "failed", scenario
        assert h.channel.texts == [want]


async def test_not_set_up(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path, token=False)
    out, _ = await h.ask()
    assert isinstance(out, Done) and out.result["status"] == "not_set_up"
    assert h.channel.texts == [NOT_SET_UP]


async def test_the_owners_login_when_no_token(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path, token=False, login=True)
    out, _ = await h.ask()
    assert isinstance(out, Done) and out.result["status"] == "answered"
    assert h.argv_notes()["creds"] == []  # the stored login: neither variable set
    h.runner.guard = "thin"  # not under the thin guard (as for workers)
    out, _ = await h.ask()
    assert isinstance(out, Done) and out.result["status"] == "not_set_up"


async def test_concurrency_waits_without_a_worker_slot(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    h = make(db, tmp_path)
    assert h.runner.uses_claude_slot is False
    queued = h.runner.enqueue("q", principal="justin")
    assert isinstance(queued, Queued)
    h.runner._working.add("someone-else")  # pyright: ignore[reportPrivateUsage]
    out = await h.runner.step(h.task(queued.task_id), [], Ctx())
    assert isinstance(out, Wait) and out.reason == "usage_reset"
    assert not (h.notes / "argv.json").exists()


async def test_daily_cap(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path, cfg={"daily_cap": 2})
    assert isinstance(h.runner.enqueue("a", principal="justin"), Queued)
    assert isinstance(h.runner.enqueue("b", principal="justin"), Queued)
    third = h.runner.enqueue("c", principal="justin")
    assert third == AskRefused("I've asked Claude 2 times today, the daily cap.")
    assert h.runner.asked_today() == 2


def test_enqueue_refusals(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path)
    assert isinstance(h.runner.enqueue("  ", principal="justin"), AskRefused)
    assert isinstance(h.runner.enqueue("x" * 4001, principal="justin"), AskRefused)
    bad_kind = h.runner.enqueue("q", kind=cast(Any, "shell"), principal="justin")
    assert isinstance(bad_kind, AskRefused)
    outside = h.runner.enqueue("q", attachments=[str(tmp_path)], principal="justin")
    assert isinstance(outside, AskRefused) and outside.text.startswith("Can't attach that")
    off = make(db, tmp_path / "off", cfg={"enabled": False})
    assert isinstance(off.runner.enqueue("q", principal="justin"), AskRefused)


async def test_a_retry_only_resends(db: sqlite3.Connection, tmp_path: Path) -> None:
    ch = FakeChannel(fail_with=[ChannelSendError("down")])
    h = make(db, tmp_path, channel=ch)
    queued = h.runner.enqueue("q", principal="justin")
    assert isinstance(queued, Queued)
    ctx = Ctx()
    out = await h.runner.step(h.task(queued.task_id), [], ctx)
    assert isinstance(out, Fail) and out.retryable and out.checkpoint is not None
    (h.notes / "argv.json").unlink()
    task = h.task(queued.task_id)
    again = await h.runner.step(_with_cp(task, out.checkpoint), [], Ctx())
    assert isinstance(again, Done) and again.result["status"] == "answered"
    assert not (h.notes / "argv.json").exists()  # no second session
    assert len(ch.sent) == 1


async def test_a_session_left_by_a_crash_is_reaped(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path)
    left = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(600)", start_new_session=True
    )
    created = psutil.Process(left.pid).create_time()
    queued = h.runner.enqueue("q", principal="justin")
    assert isinstance(queued, Queued)
    task = h.task(queued.task_id)
    cp = {**task.checkpoint, "pid": left.pid, "created": created}
    out = await h.runner.step(_with_cp(task, cp), [], Ctx())
    assert isinstance(out, Done) and out.result["status"] == "answered"
    await asyncio.wait_for(left.wait(), 10)
    assert gone(left.pid)


def _with_cp(task: Task, cp: dict[str, Any]) -> Task:
    from dataclasses import replace

    return replace(task, checkpoint=cp)


# --- the tool ----------------------------------------------------------------------------------


def test_tool_constants_agree_with_the_runner() -> None:
    assert ask_tool.QUESTION_MAX == ask_mod.QUESTION_MAX
    assert ask_tool.ATTACH_MAX == ask_mod.ATTACH_MAX
    assert ask_tool.KIND_VALUES == ask_mod.KINDS
    spec = ask_tool.ASK_CLAUDE
    assert spec.effect.value == "read" and spec.effective_level.value == "L0"
    assert spec.extension is None


def _tool_ctx(db: sqlite3.Connection, runner: AskRunner | None) -> BuiltinContext:
    components = SimpleNamespace(runners={} if runner is None else {"ask": runner})
    state = cast(Any, SimpleNamespace(components=components))
    principals = Principals(principals={"justin": Principal(role="owner")})
    return BuiltinContext(conn=db, state=state, registry=ToolRegistry(), principals=principals)


async def test_tool_queues_for_the_calling_task(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path)
    caller = task_store.insert_task(
        db,
        NewTask(
            kind="chat",
            runner="local",
            priority=2,
            principal="cofounder",
            channel_ref={"chat": "x"},
            trace_id="t-1",
        ),
    )
    tool = ask_tool.AskClaude(_tool_ctx(db, h.runner))
    ctx = CallContext(secrets={}, idempotency_key="k", task_id=caller)
    out = await tool({"question": "look up X", "kind": "research"}, ctx)
    assert out["queued"] is True and out["text"] == "Looking into it…"
    task = h.task(out["task_id"])
    assert (task.runner, task.kind, task.principal) == ("ask", "ask", "cofounder")
    assert task.channel_ref == {"chat": "x"} and task.trace_id == "t-1"
    assert task.checkpoint["requested_by"] == caller and task.checkpoint["kind"] == "research"
    plain = await tool({"question": "q"})  # no task: the owner gets it
    assert h.task(plain["task_id"]).principal == "justin"


async def test_tool_refusals(db: sqlite3.Connection, tmp_path: Path) -> None:
    missing = ask_tool.AskClaude(_tool_ctx(db, None))
    assert await missing({"question": "q"}) == {"queued": False, "text": ask_tool.NOT_RUNNING}
    h = make(db, tmp_path, cfg={"daily_cap": 0})
    tool = ask_tool.AskClaude(_tool_ctx(db, h.runner))
    out = await tool({"question": "q"})
    assert out["queued"] is False and "daily cap" in out["text"]
    from resonant.executor import ToolFailed

    for bad in ({}, {"question": ""}, {"question": "q", "kind": "shell"},
                {"question": "q", "attachments": "x"}, {"question": "q", "x": 1},
                {"question": "q", "attachments": ["a"] * 5}):  # fmt: skip
        with pytest.raises(ToolFailed):
            await tool(bad)


def test_tainted_task_may_still_ask(db: sqlite3.Connection) -> None:
    """L0 read: a tainted task's ask_claude runs without asking (the gate asks for L2/L3)."""
    registry = ToolRegistry()
    registry.register(ask_tool.ASK_CLAUDE)
    principals = Principals(principals={"justin": Principal(role="owner")})
    kv_set(db, "autonomy", "on")
    gate = PolicyGate(db, principals, registry, dry_run=True, approval_ttl_s=60)
    task_id = task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    intent = Intent.for_tool(ask_tool.ASK_CLAUDE, task_id=task_id, principal="justin",
                             args={"question": "q"}, origin="tainted")  # fmt: skip
    assert isinstance(gate.evaluate(intent), Allow)

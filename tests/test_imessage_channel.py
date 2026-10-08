"""iMessage sender + IMessageChannel. A fake OsaRunner stands in for osascript and a
FakeChatDB for chat.db; Messages.app and the real chat.db are never touched."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from resonant.cli import app
from resonant.config import IMessageConfig
from resonant.gateway.channel import (
    Channel,
    ChannelRateLimited,
    ChannelRefused,
    ChannelSendError,
)
from resonant.gateway.imessage import IMessageChannel, IMessageReader, split_text
from resonant.gateway.imessage.channel import (
    DEDUPE_PREFIX,
    MAX_CHUNK,
    SELFTEST_KV,
    sweep_sent,
)
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.gateway.imessage.reader import CURSOR_KEY, SELFTEST_PREFIX
from resonant.gateway.imessage.sender import (
    AUTOMATION_DENIED,
    OSASCRIPT,
    SEND_SCRIPT,
    OsaResult,
    explain,
    send_argv,
)
from resonant.principals import Principals
from resonant.store.db import iso, kv_get, kv_set
from resonant_sdk import Event

OWNER_PHONE = "+15551234567"
OWNER_EMAIL = "justin@example.com"
MEMBER = "+15557654321"
SELF = "resonant.agent@icloud.com"
SECRET = "my bank pin is 4321"  # a body that must never reach logs or audit rows


class FakeRunner:
    """Records argv. Optionally writes the sent row (and the incoming copy) to a chat.db."""

    def __init__(
        self,
        chat: FakeChatDB | None = None,
        *,
        result: OsaResult | None = None,
        exc: BaseException | None = None,
        copy: bool = True,
        nonce_override: str | None = None,
        from_handle: str | None = None,
    ) -> None:
        self.chat = chat
        self.result = result
        self.exc = exc
        self.copy = copy
        self.nonce_override = nonce_override
        self.from_handle = from_handle
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> OsaResult:
        self.calls.append(list(argv))
        self.timeouts.append(timeout_s)
        if self.exc is not None:
            raise self.exc
        if self.result is not None:
            return self.result
        if self.chat is not None:
            handle, text = argv[3], argv[4]
            if self.nonce_override is not None:
                text = SELFTEST_PREFIX + self.nonce_override
            handle = self.from_handle or handle
            self.chat.add_message(handle=handle, text=text, is_from_me=True)
            if self.copy:
                self.chat.add_message(handle=handle, text=text)
        return OsaResult(0, "", "")

    @property
    def texts(self) -> list[str]:
        return [c[4] for c in self.calls]


class FakeTime:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.mono

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.mono += s


@pytest.fixture
def principals() -> Principals:
    return Principals.model_validate(
        {
            "principals": {
                "justin": {
                    "role": "owner",
                    "identities": [
                        "slack:U01",
                        f"imessage:{OWNER_PHONE}",
                        f"imessage:{OWNER_EMAIL}",
                    ],
                },
                "ana": {"identities": [f"imessage:{MEMBER}"]},
                "slackonly": {"identities": ["slack:U02"]},
            }
        }
    )


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[FakeChatDB]:
    c = FakeChatDB(tmp_path / "chat.db")
    yield c
    c.close()


@pytest.fixture
def ft() -> FakeTime:
    return FakeTime()


def make(
    principals: Principals,
    db: sqlite3.Connection,
    runner: FakeRunner,
    ft: FakeTime,
    **cfg: Any,
) -> IMessageChannel:
    return IMessageChannel(
        IMessageConfig(**cfg),
        principals,
        db,
        runner=runner,
        clock=ft.clock,
        monotonic=ft.monotonic,
        sleep=ft.sleep,
    )


def audits(db: sqlite3.Connection, action: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT requested_by, idempotency_key, decision, detail FROM audit_log "
        "WHERE action = ? ORDER BY id",
        (action,),
    ).fetchall()
    return [
        {
            "requested_by": r["requested_by"],
            "idempotency_key": r["idempotency_key"],
            "decision": r["decision"],
            **json.loads(r["detail"]),
        }
        for r in rows
    ]


def all_audit_text(db: sqlite3.Connection) -> str:
    return "\n".join(str(tuple(r)) for r in db.execute("SELECT * FROM audit_log"))


# --- protocol & argv ----------------------------------------------------------------------


def test_satisfies_channel_protocol(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime
) -> None:
    channel: Channel = make(principals, db, FakeRunner(), ft)  # pyright checks conformance
    assert channel.name == "imessage"


async def test_text_goes_through_argv_verbatim(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime
) -> None:
    runner = FakeRunner()
    channel = make(principals, db, runner, ft)
    nasty = (
        'say "hi" \\" & quit\nend tell\ntell application "Finder" to delete every file\n'
        'do shell script "rm -rf ~" -- \' `whoami` $(id)'
    )
    await channel.send("justin", nasty)
    assert runner.calls == [[OSASCRIPT, "-e", SEND_SCRIPT, OWNER_PHONE, nasty]]
    assert runner.calls[0] == send_argv(OWNER_PHONE, nasty)
    assert runner.timeouts == [15.0]
    # The script itself never contains the text or the handle.
    assert "theText" in SEND_SCRIPT and OWNER_PHONE not in SEND_SCRIPT


async def test_request_approval_is_phase_2(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime
) -> None:
    from resonant.gateway.channel import ApprovalPrompt

    channel = make(principals, db, FakeRunner(), ft)
    prompt = ApprovalPrompt("a1", "K7", "s", "read", "L1", datetime.now(UTC))
    with pytest.raises(NotImplementedError, match="Phase 2"):
        await channel.request_approval("justin", prompt)


# --- outbound allowlist -------------------------------------------------------------------


async def test_send_targets_first_imessage_identity(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime
) -> None:
    runner = FakeRunner()
    channel = make(principals, db, runner, ft)
    ref = await channel.send("justin", "hello")
    await channel.send("ana", "hey")
    assert [c[3] for c in runner.calls] == [OWNER_PHONE, MEMBER]
    assert ref.channel == "imessage" and ref.ref.startswith("imessage-out:")
    sent = audits(db, "imessage.sent")
    assert sent[0]["requested_by"] == "justin"
    assert (sent[0]["handle"], sent[0]["length"], sent[0]["chunks"]) == (OWNER_PHONE, 5, 1)


async def test_preferred_handle_must_be_one_of_the_identities(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime
) -> None:
    runner = FakeRunner()
    channel = make(principals, db, runner, ft, preferred_handles={"justin": OWNER_EMAIL})
    await channel.send("justin", "a")
    rogue = make(principals, db, runner, ft, preferred_handles={"justin": "evil@example.com"})
    await rogue.send("justin", "b")
    assert [c[3] for c in runner.calls] == [OWNER_EMAIL, OWNER_PHONE]


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        (OWNER_PHONE, "unknown principal"),  # a raw handle is not a principal
        (f"imessage:{OWNER_PHONE}", "unknown principal"),
        ("mallory", "unknown principal"),
        ("slackonly", "principal has no imessage identity"),
    ],
)
async def test_raw_handles_and_unknown_principals_are_refused(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime, target: str, reason: str
) -> None:
    runner = FakeRunner()
    channel = make(principals, db, runner, ft)
    with pytest.raises(ChannelRefused):
        await channel.send(target, "hi")
    assert runner.calls == []
    assert audits(db, "imessage.send_refused") == [
        {"requested_by": target, "idempotency_key": None, "decision": None, "reason": reason}
    ]


# --- dedupe -------------------------------------------------------------------------------


async def test_dedupe_key_sends_once(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime
) -> None:
    runner = FakeRunner()
    channel = make(principals, db, runner, ft)
    first = await channel.send("justin", "done", dedupe_key="task:1")
    again = await channel.send("justin", "done", dedupe_key="task:1")
    # A fresh channel (daemon restart) still sees the record in kv.
    restarted = await make(principals, db, runner, ft).send("justin", "x", dedupe_key="task:1")
    other = await channel.send("justin", "done", dedupe_key="task:2")
    assert first == again == restarted != other
    assert len(runner.calls) == 2
    assert [a["idempotency_key"] for a in audits(db, "imessage.sent")] == ["task:1", "task:2"]
    keys = [r[0] for r in db.execute("SELECT key FROM kv WHERE key LIKE 'imessage.sent:%'")]
    assert len(keys) == 2 and all("task" not in k for k in keys)  # hashed


async def test_concurrent_sends_with_the_same_key_send_once(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime
) -> None:
    runner = FakeRunner()
    channel = make(principals, db, runner, ft)
    refs = await asyncio.gather(*(channel.send("justin", "x", dedupe_key="k") for _ in range(5)))
    assert len(set(refs)) == 1 and len(runner.calls) == 1


async def test_dedupe_records_expire_after_7_days_and_are_swept_on_start(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime, chat: FakeChatDB
) -> None:
    runner = FakeRunner()
    channel = make(principals, db, runner, ft, db_path=chat.path)
    await channel.send("justin", "a", dedupe_key="old")
    ft.now += timedelta(days=6)
    await channel.send("justin", "b", dedupe_key="new")
    ft.now += timedelta(days=1, seconds=1)
    await channel.send("justin", "a", dedupe_key="old")  # expired: sends again
    assert len(runner.calls) == 3

    kv_set(db, DEDUPE_PREFIX + "ancient", {"ref": "r", "at": iso(ft.now - timedelta(days=30))})
    kv_set(db, DEDUPE_PREFIX + "garbage", "not a record")
    assert sweep_sent(db, ft.now) == 2
    await channel.start(lambda _: True)
    await channel.stop()
    remaining = db.execute("SELECT COUNT(*) FROM kv WHERE key LIKE 'imessage.sent:%'")
    assert remaining.fetchone()[0] == 2  # "new" and the re-sent "old"
    ft.now += timedelta(days=8)
    assert sweep_sent(db, ft.now) == 2


# --- rate limit ---------------------------------------------------------------------------


async def test_61st_send_within_a_minute_is_delayed_and_audited(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime
) -> None:
    runner = FakeRunner()
    channel = make(principals, db, runner, ft)
    for i in range(60):
        await channel.send("justin", f"m{i}")
    assert ft.slept == []
    await channel.send("ana", "other principals have their own bucket")
    assert ft.slept == []
    await channel.send("justin", "m60")
    assert ft.slept == [pytest.approx(1.0)]  # pyright: ignore[reportUnknownMemberType]
    assert len(runner.calls) == 62
    (limited,) = audits(db, "imessage.send_rate_limited")
    assert limited["requested_by"] == "justin" and limited["decision"] == "delayed"
    assert limited["limit_per_minute"] == 60


async def test_send_needing_more_than_30s_is_refused_and_audited(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime
) -> None:
    runner = FakeRunner()
    channel = make(principals, db, runner, ft, outbound_per_minute=1)
    await channel.send("justin", "one")
    with pytest.raises(ChannelRateLimited):
        await channel.send("justin", "two", dedupe_key="k2")
    assert len(runner.calls) == 1 and ft.slept == []
    (limited,) = audits(db, "imessage.send_rate_limited")
    assert (limited["decision"], limited["idempotency_key"]) == ("refused", "k2")
    ft.mono += 60  # refilled
    await channel.send("justin", "two", dedupe_key="k2")
    assert len(runner.calls) == 2


# --- splitting ----------------------------------------------------------------------------


def test_short_text_is_not_split() -> None:
    assert split_text("hi") == ["hi"]
    assert split_text("x" * MAX_CHUNK) == ["x" * MAX_CHUNK]


def test_long_text_splits_on_paragraphs_then_sentences() -> None:
    para = ("This is a sentence. " * 60).strip()  # ~1200 chars
    text = "\n\n".join([para, para, para])
    parts = split_text(text)
    assert len(parts) == 3
    assert [p[-5:] for p in parts] == ["(1/3)", "(2/3)", "(3/3)"]
    assert all(len(p) <= MAX_CHUNK for p in parts)
    assert parts[0] == f"{para} (1/3)"

    one_para = ("Sentence number one is here. " * 100).strip()  # ~2900 chars, no \n\n
    parts = split_text(one_para)
    assert len(parts) == 2
    assert parts[0].removesuffix(" (1/2)").endswith("here.")  # cut after a sentence
    assert parts[1].startswith("Sentence")


def test_unbroken_text_is_hard_cut() -> None:
    parts = split_text("x" * 5000)
    assert len(parts) == 3 and all(len(p) <= MAX_CHUNK for p in parts)
    assert "".join(p.rsplit(" (", 1)[0] for p in parts) == "x" * 5000


async def test_long_text_is_sent_in_parts(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime
) -> None:
    runner = FakeRunner()
    channel = make(principals, db, runner, ft)
    await channel.send("justin", "word " * 1000)
    assert [t[-5:] for t in runner.texts] == ["(1/3)", "(2/3)", "(3/3)"]
    (sent,) = audits(db, "imessage.sent")
    assert sent["chunks"] == 3


# --- send failures ------------------------------------------------------------------------


async def test_automation_denied_is_actionable_and_audited_without_body(
    principals: Principals,
    db: sqlite3.Connection,
    ft: FakeTime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stderr = f'execution error: Not authorized to send Apple events to Messages. "{SECRET}" (-1743)'
    runner = FakeRunner(result=OsaResult(1, "", stderr))
    channel = make(principals, db, runner, ft)
    with caplog.at_level(logging.DEBUG), pytest.raises(ChannelSendError) as e:
        await channel.send("justin", SECRET, dedupe_key="k")
    assert "Automation" in str(e.value) and SECRET not in str(e.value)
    (failed,) = audits(db, "imessage.send_failed")
    assert failed["code"] == -1743 and failed["reason"] == AUTOMATION_DENIED
    assert SECRET not in all_audit_text(db) and SECRET not in caplog.text
    # Not recorded as sent: a retry tries again.
    runner.result = None
    await channel.send("justin", SECRET, dedupe_key="k")
    assert len(runner.calls) == 2


async def test_osascript_timeout_is_reported(
    principals: Principals, db: sqlite3.Connection, ft: FakeTime
) -> None:
    channel = make(principals, db, FakeRunner(exc=TimeoutError()), ft)
    with pytest.raises(ChannelSendError, match="timed out after 15s"):
        await channel.send("justin", "hi")


@pytest.mark.parametrize(
    ("stderr", "needle"),
    [
        ("execution error: Messages got an error: App isn't running. (-600)", "not running"),
        ('execution error: Can\'t get participant "+1" of account id "x". (-1728)', "handle"),
        ("execution error: Messages got an error: Something odd. (-10000)", "error -10000"),
        ("", "exit 1"),
    ],
)
def test_explain_maps_osascript_errors(stderr: str, needle: str) -> None:
    err = explain(stderr, 1)
    assert needle in str(err)
    assert '"+1"' not in str(err)  # stderr (which can echo argv) is never passed through


# --- self-test ----------------------------------------------------------------------------


def selftest_channel(
    principals: Principals, db: sqlite3.Connection, runner: FakeRunner, chat: FakeChatDB
) -> IMessageChannel:
    cfg = IMessageConfig(self_handle=SELF, db_path=chat.path, poll_s=0.1)
    return IMessageChannel(cfg, principals, db, runner=runner)


async def test_selftest_passes_with_fake_runner_and_fixture_chatdb(
    principals: Principals, db: sqlite3.Connection, chat: FakeChatDB
) -> None:
    chat.add_message(handle=OWNER_PHONE, text="old history")
    kv_set(db, CURSOR_KEY, 0)  # the daemon's cursor must not move
    runner = FakeRunner(chat)
    result = await selftest_channel(principals, db, runner, chat).self_test()
    assert result.ok, result.detail
    assert "read back" in result.detail and "not seen" not in result.detail
    (argv,) = runner.calls
    assert argv[3] == SELF and argv[4].startswith(SELFTEST_PREFIX)
    stored = kv_get(db, SELFTEST_KV)
    assert stored["ok"] is True and stored["detail"] == result.detail and stored["at"]
    assert [a["decision"] for a in audits(db, "imessage.selftest")] == ["ok"]
    assert kv_get(db, CURSOR_KEY) == 0


async def test_selftest_without_incoming_copy_still_passes(
    principals: Principals, db: sqlite3.Connection, chat: FakeChatDB, monkeypatch: Any
) -> None:
    monkeypatch.setattr("resonant.gateway.imessage.channel.SELFTEST_COPY_GRACE_S", 0.2)
    runner = FakeRunner(chat, copy=False)
    result = await selftest_channel(principals, db, runner, chat).self_test()
    assert result.ok and "incoming copy was not seen" in result.detail


async def test_selftest_automation_denied(
    principals: Principals, db: sqlite3.Connection, chat: FakeChatDB
) -> None:
    runner = FakeRunner(chat, result=OsaResult(1, "", "execution error: (-1743)"))
    result = await selftest_channel(principals, db, runner, chat).self_test()
    assert not result.ok and "grant Automation" in result.detail
    assert kv_get(db, SELFTEST_KV)["ok"] is False
    assert [a["decision"] for a in audits(db, "imessage.selftest")] == ["failed"]


async def test_selftest_without_full_disk_access(
    principals: Principals, db: sqlite3.Connection, chat: FakeChatDB
) -> None:
    runner = FakeRunner(chat)
    chat.path.chmod(0)
    try:
        result = await selftest_channel(principals, db, runner, chat).self_test()
    finally:
        chat.path.chmod(0o600)
    assert not result.ok and "Full Disk Access" in result.detail
    assert runner.calls == []  # nothing is sent if we can't read it back


async def test_selftest_timeout(
    principals: Principals, db: sqlite3.Connection, chat: FakeChatDB
) -> None:
    runner = FakeRunner()  # "sends" but nothing lands in chat.db
    result = await selftest_channel(principals, db, runner, chat).self_test(timeout_s=0.3)
    assert not result.ok and "did not appear in chat.db within 0.3s" in result.detail
    assert SELF in result.detail


@pytest.mark.parametrize(
    "runner_kw",
    [
        {"nonce_override": "someoneElsesNonce"},  # right handle, wrong nonce
        {"from_handle": OWNER_PHONE},  # right nonce, wrong handle
    ],
)
async def test_selftest_ignores_foreign_sightings(
    principals: Principals, db: sqlite3.Connection, chat: FakeChatDB, runner_kw: dict[str, Any]
) -> None:
    runner = FakeRunner(chat, **runner_kw)
    result = await selftest_channel(principals, db, runner, chat).self_test(timeout_s=0.4)
    assert not result.ok


async def test_selftest_requires_self_handle(
    principals: Principals, db: sqlite3.Connection, chat: FakeChatDB
) -> None:
    cfg = IMessageConfig(db_path=chat.path)
    channel = IMessageChannel(cfg, principals, db, runner=FakeRunner())
    result = await channel.self_test()
    assert not result.ok and "self_handle" in result.detail


async def test_selftest_on_running_channel_is_never_routed(
    principals: Principals, db: sqlite3.Connection, chat: FakeChatDB
) -> None:
    """The daemon path: self-test rows reach the checker, never submit()."""
    events: list[Event] = []

    def submit(e: Event) -> bool:
        events.append(e)
        return True

    runner = FakeRunner(chat)
    channel = selftest_channel(principals, db, runner, chat)
    await channel.start(submit)
    try:
        result = await channel.self_test()  # right after start: waits for the baseline
        # Even an owner-sent nonce message is swallowed, not answered.
        chat.add_message(handle=OWNER_PHONE, text=f"{SELFTEST_PREFIX}ownerSentThis")
        chat.add_message(handle=OWNER_PHONE, text="real message")
        for _ in range(100):
            if events:
                break
            await asyncio.sleep(0.05)
    finally:
        await channel.stop()
    assert result.ok, result.detail
    assert [e.payload["text"] for e in events] == ["real message"]
    assert kv_get(db, "imessage.selftest.cursor") is None  # used the running reader


async def test_reader_used_by_selftest_has_its_own_cursor(
    principals: Principals, db: sqlite3.Connection, chat: FakeChatDB
) -> None:
    cfg = IMessageConfig(self_handle=SELF, db_path=chat.path)
    reader = IMessageReader(cfg, principals, db, cursor_key="other.cursor")
    reader.poll_once(lambda _: True)
    reader.close()
    assert kv_get(db, "other.cursor") == 0 and kv_get(db, CURSOR_KEY) is None


# --- CLI ----------------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, chat: FakeChatDB) -> list[FakeRunner]:
    monkeypatch.setenv("RESONANT_CHANNELS__IMESSAGE__SELF_HANDLE", SELF)
    monkeypatch.setenv("RESONANT_CHANNELS__IMESSAGE__DB_PATH", str(chat.path))
    made: list[FakeRunner] = []
    return made


def test_cli_selftest_exits_0_on_success(
    monkeypatch: pytest.MonkeyPatch, chat: FakeChatDB, cli_env: list[FakeRunner]
) -> None:
    def factory() -> FakeRunner:
        r = FakeRunner(chat)
        cli_env.append(r)
        return r

    monkeypatch.setattr("resonant.gateway.imessage.sender.OsascriptRunner", factory)
    result = CliRunner().invoke(app, ["selftest", "imessage"])
    assert result.exit_code == 0, result.output
    assert "imessage self-test ok" in result.output
    assert len(cli_env) == 1 and len(cli_env[0].calls) == 1


def test_cli_selftest_exits_1_on_failure(
    monkeypatch: pytest.MonkeyPatch, chat: FakeChatDB, cli_env: list[FakeRunner]
) -> None:
    monkeypatch.setattr(
        "resonant.gateway.imessage.sender.OsascriptRunner",
        lambda: FakeRunner(chat, result=OsaResult(1, "", "(-1743)")),
    )
    result = CliRunner().invoke(app, ["selftest", "imessage", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False and "Automation" in data["detail"]


def test_cli_lists_selftest() -> None:
    result = CliRunner().invoke(app, ["selftest", "--help"])
    assert result.exit_code == 0 and "imessage" in result.output


@pytest.mark.parametrize("handle", ["-x@evil.com", "-e", "--", "x@-evil.com", "+0123456789"])
async def test_sender_refuses_handles_osascript_could_misparse(handle: str) -> None:
    from resonant.gateway.imessage import IMessageSender, SendError

    runner = FakeRunner()
    with pytest.raises(SendError, match="refusing"):
        await IMessageSender(runner).send(handle, "hi")
    assert runner.calls == []


def test_principals_reject_handles_starting_with_dash() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Principals.model_validate(
            {"principals": {"j": {"role": "owner", "identities": ["imessage:-x@evil.com"]}}}
        )

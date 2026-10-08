"""iMessage inbound reader. Every test uses FakeChatDB; the real chat.db is never touched."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from resonant.config import IMessageConfig, Settings
from resonant.gateway.imessage import IMessageReader, SelfTestSighting
from resonant.gateway.imessage.attributed_body import parse_attributed_body
from resonant.gateway.imessage.fixtures import TAPBACK_LOVE, FakeChatDB, to_apple_ns
from resonant.gateway.imessage.reader import (
    CURSOR_KEY,
    from_apple_time,
    normalize_handle,
)
from resonant.principals import Principals
from resonant.store import tasks as repo
from resonant.store.db import kv_get, kv_set, transaction
from resonant_sdk import Event

FIXTURES = Path(__file__).parent / "fixtures" / "imessage"
OWNER_PHONE = "+15551234567"
OWNER_EMAIL = "justin@example.com"
MEMBER = "+15557654321"
STRANGER = "+15559990000"
SELF = "resonant.agent@icloud.com"
SECRET = "my bank pin is 4321"  # a body that must never reach logs or audit rows


def blob(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class Sink:
    """A submit() that behaves like Loop.submit: persists to the store and dedupes."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.events: list[Event] = []
        self.fail_on: int | None = None  # raise on the n-th call (1-based)
        self.calls = 0

    def __call__(self, event: Event) -> bool:
        self.calls += 1
        if self.fail_on == self.calls:
            raise RuntimeError("store down")
        with transaction(self.conn):
            inserted = repo.insert_event(self.conn, event)
        if inserted:
            self.events.append(event)
        return inserted


@pytest.fixture
def principals() -> Principals:
    return Principals.model_validate(
        {
            "principals": {
                "justin": {
                    "role": "owner",
                    "identities": [f"imessage:{OWNER_PHONE}", f"imessage:{OWNER_EMAIL}"],
                },
                "cofounder": {"identities": [f"imessage:{MEMBER}"]},
            }
        }
    )


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[FakeChatDB]:
    c = FakeChatDB(tmp_path / "chat.db")
    c.add_message(handle=OWNER_PHONE, text="old history, never replayed")
    yield c
    c.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_reader(
    chat: FakeChatDB,
    principals: Principals,
    db: sqlite3.Connection,
    clock: FakeClock | None = None,
    **cfg: Any,
) -> IMessageReader:
    config = IMessageConfig(self_handle=SELF, db_path=chat.path, **cfg)
    return IMessageReader(config, principals, db, clock or FakeClock())


@pytest.fixture
def reader(chat: FakeChatDB, principals: Principals, db: sqlite3.Connection) -> IMessageReader:
    r = make_reader(chat, principals, db)
    r.poll_once(Sink(db))  # first start: cursor goes to the current end
    return r


def audits(db: sqlite3.Connection, prefix: str = "imessage.") -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT action, requested_by, detail FROM audit_log WHERE action LIKE ? ORDER BY id",
        (prefix + "%",),
    ).fetchall()
    return [
        {"action": r["action"], "requested_by": r["requested_by"], **json.loads(r["detail"])}
        for r in rows
    ]


# --- happy path ------------------------------------------------------------------------


def test_first_start_starts_at_end_without_replay(
    chat: FakeChatDB, principals: Principals, db: sqlite3.Connection
) -> None:
    reader = make_reader(chat, principals, db)
    sink = Sink(db)
    assert reader.poll_once(sink) == 0
    assert sink.events == []
    assert kv_get(db, CURSOR_KEY) == chat.max_rowid()
    assert reader.health()["ok"] is True


def test_known_handle_becomes_exactly_one_event(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    at = datetime(2026, 10, 8, 9, 30, 15, 123000, tzinfo=UTC)
    rowid = chat.add_message(handle=OWNER_PHONE, text="what's on today?", at=at, guid="G-1")
    sink = Sink(db)
    assert reader.poll_once(sink) == 1
    assert reader.poll_once(sink) == 0
    [event] = sink.events
    assert event.source == "imessage"
    assert event.type == "message"
    assert event.principal == "justin"
    assert event.dedupe_key == "imessage:G-1"
    assert event.occurred_at == at
    assert event.task_id is None
    assert event.payload == {
        "text": "what's on today?",
        "guid": "G-1",
        "handle": OWNER_PHONE,
        "chat_guid": f"iMessage;-;{OWNER_PHONE}",
        "at": "2026-10-08T09:30:15.123+00:00",
        "truncated": False,
    }
    assert kv_get(db, CURSOR_KEY) == rowid


def test_phone_and_email_map_to_same_principal(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    chat.add_message(handle=OWNER_PHONE, text="from phone")
    chat.add_message(handle="Justin@Example.COM", text="from mac")
    sink = Sink(db)
    reader.poll_once(sink)
    assert [e.principal for e in sink.events] == ["justin", "justin"]
    assert sink.events[1].payload["handle"] == OWNER_EMAIL


def test_member_principal_is_resolved(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    chat.add_message(handle=MEMBER, text="hey")
    sink = Sink(db)
    reader.poll_once(sink)
    assert [e.principal for e in sink.events] == ["cofounder"]


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("plain.bin", "hello from the owner"),
        ("emoji.bin", "on my way 🚗💨 see you soon 👋🏽"),
        ("multiline.bin", "line one\nline two\n\nline four"),
    ],
)
def test_attributed_body_only_messages_are_parsed(
    chat: FakeChatDB,
    reader: IMessageReader,
    db: sqlite3.Connection,
    fixture: str,
    expected: str,
) -> None:
    chat.add_message(handle=OWNER_PHONE, text=None, attributed_body=blob(fixture))
    sink = Sink(db)
    reader.poll_once(sink)
    assert [e.payload["text"] for e in sink.events] == [expected]


def test_huge_message_is_truncated(
    chat: FakeChatDB, principals: Principals, db: sqlite3.Connection
) -> None:
    reader = make_reader(chat, principals, db, max_body_chars=10)
    sink = Sink(db)
    reader.poll_once(sink)
    chat.add_message(handle=OWNER_PHONE, text="x" * 50)
    reader.poll_once(sink)
    [event] = sink.events
    assert event.payload["text"] == "x" * 10
    assert event.payload["truncated"] is True


# --- ignored rows ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"handle": OWNER_PHONE, "is_from_me": True}, id="is_from_me"),
        pytest.param({"handle": SELF}, id="self_handle"),
        pytest.param({"handle": OWNER_PHONE, "service": "SMS"}, id="sms"),
        pytest.param(
            {"handle": OWNER_PHONE, "associated_message_type": TAPBACK_LOVE}, id="tapback"
        ),
        pytest.param({"handle": OWNER_PHONE, "group": True}, id="group"),
        pytest.param({"handle": OWNER_PHONE, "in_chat": False}, id="no_chat"),
        pytest.param({"handle": OWNER_PHONE, "text": "   "}, id="empty"),
        pytest.param({"handle": OWNER_PHONE, "text": "\ufffc"}, id="attachment_only"),
    ],
)
def test_ignored_rows_never_become_events(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection, kwargs: dict[str, Any]
) -> None:
    kwargs.setdefault("text", "Loved “hi”")
    rowid = chat.add_message(**kwargs)
    sink = Sink(db)
    assert reader.poll_once(sink) == 0
    assert sink.events == []
    assert kv_get(db, CURSOR_KEY) == rowid  # skipped rows are still consumed


def test_self_handle_and_own_messages_are_not_audited(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    chat.add_message(handle=SELF, text="hi")
    chat.add_message(handle=OWNER_PHONE, text="hi", is_from_me=True)
    reader.poll_once(Sink(db))
    assert audits(db) == []


def test_group_chat_is_audited(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    chat.add_message(handle=OWNER_PHONE, text=SECRET, group=True, guid="G-grp")
    reader.poll_once(Sink(db))
    [row] = audits(db)
    assert row["action"] == "imessage.group_ignored"
    assert row["guid"] == "G-grp"
    assert SECRET not in json.dumps(row)


def test_selftest_nonce_goes_to_callback_never_to_events(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    seen: list[SelfTestSighting] = []
    unregister = reader.register_selftest_callback(seen.append)
    # The send (is_from_me) and the received copy (from Resonant's own handle).
    chat.add_message(handle=SELF, text="resonant-selftest:abcDEF12_-", is_from_me=True)
    chat.add_message(handle=SELF, text=None, attributed_body=blob("selftest.bin"))
    # A known principal sending a nonce-shaped message still never routes.
    chat.add_message(handle=OWNER_PHONE, text="resonant-selftest:zzzzzzzzzz")
    sink = Sink(db)
    assert reader.poll_once(sink) == 0
    assert sink.events == []
    assert [(s.nonce, s.is_from_me, s.handle) for s in seen] == [
        ("abcDEF12_-", True, SELF),
        ("abcDEF12_-", False, SELF),
        ("zzzzzzzzzz", False, OWNER_PHONE),
    ]
    unregister()
    chat.add_message(handle=SELF, text="resonant-selftest:abcdefgh")
    reader.poll_once(sink)
    assert len(seen) == 3


def test_near_miss_nonce_is_a_normal_message(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    seen: list[SelfTestSighting] = []
    reader.register_selftest_callback(seen.append)
    chat.add_message(handle=OWNER_PHONE, text="resonant-selftest:short")
    chat.add_message(handle=OWNER_PHONE, text="hi resonant-selftest:abcdefgh")
    sink = Sink(db)
    assert reader.poll_once(sink) == 2
    assert seen == []


def test_failing_selftest_callback_does_not_break_reads(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    def boom(_: SelfTestSighting) -> None:
        raise RuntimeError("checker bug")

    reader.register_selftest_callback(boom)
    chat.add_message(handle=SELF, text="resonant-selftest:abcdefgh")
    chat.add_message(handle=OWNER_PHONE, text="real")
    sink = Sink(db)
    assert reader.poll_once(sink) == 1


def test_unknown_sender_dropped_and_audited_without_body(
    chat: FakeChatDB,
    reader: IMessageReader,
    db: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    chat.add_message(handle=STRANGER, text=SECRET, guid="G-x")
    sink = Sink(db)
    assert reader.poll_once(sink) == 0
    [row] = audits(db)
    assert row == {
        "action": "imessage.unknown_sender",
        "requested_by": None,
        "guid": "G-x",
        "handle": STRANGER,
    }
    assert SECRET not in caplog.text


def test_unparseable_attributed_body_is_audited_without_body(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    chat.add_message(handle=OWNER_PHONE, text=None, attributed_body=b"streamtyped garbage")
    sink = Sink(db)
    assert reader.poll_once(sink) == 0
    assert [(r["action"], r["handle"]) for r in audits(db)] == [
        ("imessage.parse_failed", OWNER_PHONE)
    ]


def test_rate_limit_per_principal(
    chat: FakeChatDB, principals: Principals, db: sqlite3.Connection, clock: FakeClock
) -> None:
    reader = make_reader(chat, principals, db, clock, inbound_per_minute=3)
    sink = Sink(db)
    reader.poll_once(sink)
    for i in range(5):
        chat.add_message(handle=OWNER_PHONE, text=f"{SECRET} {i}")
    chat.add_message(handle=MEMBER, text="other principal is unaffected")
    reader.poll_once(sink)
    assert [e.principal for e in sink.events] == ["justin"] * 3 + ["cofounder"]
    limited = audits(db)
    assert [(r["action"], r["requested_by"]) for r in limited] == [
        ("imessage.rate_limited", "justin")
    ] * 2
    assert SECRET not in json.dumps(limited)
    clock.advance(seconds=61)
    chat.add_message(handle=OWNER_PHONE, text="later")
    reader.poll_once(sink)
    assert len(sink.events) == 5


# --- cursor and crashes ------------------------------------------------------------------


def test_cursor_persists_across_restarts(
    chat: FakeChatDB, principals: Principals, db: sqlite3.Connection
) -> None:
    sink = Sink(db)
    make_reader(chat, principals, db).poll_once(sink)
    chat.add_message(handle=OWNER_PHONE, text="while down 1")
    chat.add_message(handle=OWNER_PHONE, text="while down 2")
    make_reader(chat, principals, db).poll_once(sink)  # a fresh process
    assert [e.payload["text"] for e in sink.events] == ["while down 1", "while down 2"]


def test_crash_after_submit_before_cursor_write_has_no_duplicates(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    before = kv_get(db, CURSOR_KEY)
    chat.add_message(handle=OWNER_PHONE, text="one")
    chat.add_message(handle=OWNER_PHONE, text="two")
    sink = Sink(db)
    reader.poll_once(sink)
    kv_set(db, CURSOR_KEY, before)  # as if the cursor write was lost in a crash
    assert reader.poll_once(sink) == 0  # replayed, but deduped
    assert [e.payload["text"] for e in sink.events] == ["one", "two"]
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_submit_failure_keeps_unsubmitted_rows_for_next_read(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    first = chat.add_message(handle=OWNER_PHONE, text="one")
    chat.add_message(handle=OWNER_PHONE, text="two")
    sink = Sink(db)
    sink.fail_on = 2
    reader._safe_poll(sink)  # pyright: ignore[reportPrivateUsage]
    assert kv_get(db, CURSOR_KEY) == first
    health = reader.health()
    assert health["ok"] is False
    assert health["last_error"] == "submit failed: RuntimeError"
    reader._safe_poll(sink)  # pyright: ignore[reportPrivateUsage]
    assert [e.payload["text"] for e in sink.events] == ["one", "two"]
    assert reader.health()["ok"] is True


def test_cursor_past_end_resets(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    kv_set(db, CURSOR_KEY, 10_000)  # chat.db was recreated with fewer rows
    sink = Sink(db)
    reader.poll_once(sink)
    assert kv_get(db, CURSOR_KEY) == chat.max_rowid()
    assert [r["action"] for r in audits(db)] == ["imessage.cursor_reset"]
    chat.add_message(handle=OWNER_PHONE, text="new")
    reader.poll_once(sink)
    assert len(sink.events) == 1


def test_reads_200_rows_under_100ms(
    chat: FakeChatDB, principals: Principals, db: sqlite3.Connection
) -> None:
    reader = make_reader(chat, principals, db, inbound_per_minute=1000)
    reader.poll_once(Sink(db))
    for i in range(200):
        if i % 2:
            chat.add_message(handle=OWNER_PHONE, text=f"message {i}")
        else:
            chat.add_message(handle=OWNER_EMAIL, text=None, attributed_body=blob("emoji.bin"))
    received: list[Event] = []

    def submit(event: Event) -> bool:
        received.append(event)
        return True

    start = time.perf_counter()
    assert reader.poll_once(submit) == 200
    elapsed = time.perf_counter() - start
    assert elapsed < 0.1, f"read took {elapsed * 1000:.1f}ms"


# --- failures and health ------------------------------------------------------------------


def test_missing_full_disk_access(
    chat: FakeChatDB, principals: Principals, db: sqlite3.Connection
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    reader = make_reader(chat, principals, db)
    chat.path.chmod(0)
    try:
        reader._safe_poll(Sink(db))  # pyright: ignore[reportPrivateUsage]
    finally:
        chat.path.chmod(0o600)
    health = reader.health()
    assert health["fda_ok"] is False
    assert health["ok"] is False
    assert "Full Disk Access" in health["last_error"]
    reader._safe_poll(Sink(db))  # pyright: ignore[reportPrivateUsage]
    recovered = reader.health()
    assert (recovered["ok"], recovered["fda_ok"], recovered["last_error"]) == (True, True, None)


def test_missing_database_does_not_crash(
    tmp_path: Path, principals: Principals, db: sqlite3.Connection
) -> None:
    config = IMessageConfig(db_path=tmp_path / "nope.db")
    reader = IMessageReader(config, principals, db)
    reader._safe_poll(Sink(db))  # pyright: ignore[reportPrivateUsage]
    health = reader.health()
    assert health["ok"] is False
    assert health["fda_ok"] is True
    assert "not found" in health["last_error"]


@pytest.mark.parametrize(("table", "column"), [("message", "attributedBody"), ("chat", "style")])
def test_unsupported_schema_is_reported(
    tmp_path: Path, principals: Principals, db: sqlite3.Connection, table: str, column: str
) -> None:
    chat = FakeChatDB(tmp_path / "chat.db", omit={table: {column}})
    reader = IMessageReader(IMessageConfig(db_path=chat.path), principals, db)
    reader._safe_poll(Sink(db))  # pyright: ignore[reportPrivateUsage]
    health = reader.health()
    assert health["ok"] is False
    assert health["last_error"] == f"unsupported chat.db schema: missing {table}.{column}"
    chat.close()


def test_locked_database_is_retried_next_poll(
    chat: FakeChatDB,
    reader: IMessageReader,
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = IMessageReader.poll_once
    calls = 0

    def flaky(self: IMessageReader, submit: Any) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(self, submit)

    monkeypatch.setattr(IMessageReader, "poll_once", flaky)
    chat.add_message(handle=OWNER_PHONE, text="hi")
    sink = Sink(db)
    reader._safe_poll(sink)  # pyright: ignore[reportPrivateUsage]
    assert reader.health()["last_error"] == "chat.db busy, retrying: database is locked"
    reader._safe_poll(sink)  # pyright: ignore[reportPrivateUsage]
    assert len(sink.events) == 1
    assert reader.health()["ok"] is True


def test_chat_db_is_opened_read_only(
    chat: FakeChatDB, reader: IMessageReader, db: sqlite3.Connection
) -> None:
    ro = reader._open()  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("CREATE TABLE intruder (x)")


def test_health_before_first_read(principals: Principals, db: sqlite3.Connection) -> None:
    reader = IMessageReader(IMessageConfig(), principals, db)
    assert reader.health() == {
        "ok": False,
        "last_read_at": None,
        "last_error": None,
        "fda_ok": True,
    }


# --- run loop ---------------------------------------------------------------------------


async def test_run_reads_promptly_on_wal_change_and_stops(
    chat: FakeChatDB, principals: Principals, db: sqlite3.Connection
) -> None:
    reader = make_reader(chat, principals, db, poll_s=30)  # only the WAL watch can wake it
    sink = Sink(db)
    task = asyncio.create_task(reader.run(sink))
    for _ in range(100):
        if kv_get(db, CURSOR_KEY) is not None:
            break
        await asyncio.sleep(0.01)
    chat.add_message(handle=OWNER_PHONE, text="ping")
    for _ in range(200):
        if sink.events:
            break
        await asyncio.sleep(0.01)
    assert [e.payload["text"] for e in sink.events] == ["ping"]
    reader.stop()
    await asyncio.wait_for(task, 1)


async def test_run_survives_errors(
    tmp_path: Path, principals: Principals, db: sqlite3.Connection
) -> None:
    reader = IMessageReader(
        IMessageConfig(db_path=tmp_path / "nope.db", poll_s=0.01), principals, db
    )
    task = asyncio.create_task(reader.run(Sink(db)))
    await asyncio.sleep(0.05)
    assert not task.done()
    reader.stop()
    await asyncio.wait_for(task, 1)
    assert reader.health()["ok"] is False


# --- helpers and config -----------------------------------------------------------------


def test_normalize_handle() -> None:
    assert normalize_handle(" Justin@Example.COM ") == OWNER_EMAIL
    assert normalize_handle(OWNER_PHONE) == OWNER_PHONE


def test_apple_time_nanoseconds_and_seconds() -> None:
    at = datetime(2026, 10, 8, 12, 0, 1, 500000, tzinfo=UTC)
    assert from_apple_time(to_apple_ns(at)) == at
    assert from_apple_time(1) == datetime(2001, 1, 1, 0, 0, 1, tzinfo=UTC)  # legacy seconds
    assert from_apple_time(0) is None
    assert from_apple_time(None) is None


def test_imessage_config_defaults() -> None:
    config = Settings().channels.imessage
    assert config.enabled is False
    assert config.db_path == Path("~/Library/Messages/chat.db").expanduser()
    assert config.poll_s == 2.0
    assert config.inbound_per_minute == 120
    assert config.max_body_chars == 4000


def test_attributed_body_fixtures_round_trip() -> None:
    assert parse_attributed_body(blob("long.bin")) == "abcdefghij" * 30


async def test_stop_before_run_is_honored(
    chat: FakeChatDB, principals: Principals, db: sqlite3.Connection
) -> None:
    reader = make_reader(chat, principals, db, poll_s=30)
    reader.stop()
    await asyncio.wait_for(reader.run(Sink(db)), 1)

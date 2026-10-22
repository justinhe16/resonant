"""PER-256: the news check built from code (``brain/news.py``) and the usage-limit notice
(``runners/ask.limit_notice``). The real store; nothing here calls a model."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from resonant.brain.news import NEWS_NOTHING, news_reply
from resonant.config import Settings
from resonant.runners import ask
from resonant.runners.claude_session import LIMIT_KNOWN_KV
from resonant.store import pulse as pulse_store
from resonant.store import runs as run_store
from resonant.store import tasks as repo
from resonant.store.db import kv_get, kv_set, transaction
from resonant_sdk import Event

OWNER = "justin"
LA = ZoneInfo("America/Los_Angeles")


def message(conn: sqlite3.Connection, text: str, at: datetime) -> str:
    event = Event(source="imessage", type="message", principal=OWNER, payload={"text": text},
                  occurred_at=at)  # fmt: skip
    with transaction(conn):
        repo.insert_event(conn, event)
    return event.id


def ended_run(conn: sqlite3.Connection, status: str, *, told: bool) -> str:
    created = run_store.create(conn, repo="dori", ask="fix the flaky test", principal=OWNER,
                               session_id="s", max_seconds=60)  # fmt: skip
    run_store.update(conn, created.code, status="running")
    run_store.finish(conn, created.code, "failed" if status == "failed" else "done",
                     failure="it crashed" if status == "failed" else None)  # fmt: skip
    if told:
        now = datetime.now(UTC) + timedelta(seconds=5)
        _c, item = pulse_store.upsert(
            conn, "core", pulse_store.run_key(created.code), f"{created.code} failed", "normal",
            None, True, pulse_store.DEFAULT_TTL_S, now,
        )  # fmt: skip
        pulse_store.mark_told(conn, [item], now)
    return created.code


def test_nothing_new_and_nothing_waiting(db: sqlite3.Connection) -> None:
    now = datetime.now(UTC)
    message(db, "hi", now - timedelta(minutes=30))
    asking = message(db, "anything for me?", now)
    assert news_reply(db, Settings(), OWNER, event_id=asking, now=now) == NEWS_NOTHING


def test_a_run_that_ended_since_the_last_message_is_news(db: sqlite3.Connection) -> None:
    now = datetime.now(UTC)
    message(db, "hi", now - timedelta(minutes=30))
    code = ended_run(db, "failed", told=False)
    asking = message(db, "anything new?", now + timedelta(minutes=1))
    text = news_reply(db, Settings(), OWNER, event_id=asking, now=now + timedelta(minutes=1))
    assert text is not None
    assert text.startswith("New since we last talked:") and f"{code} (dori) failed" in text
    assert "Waiting on you:" not in text


def test_a_run_the_owner_was_told_about_is_not_repeated(db: sqlite3.Connection) -> None:
    now = datetime.now(UTC)
    message(db, "hi", now - timedelta(minutes=30))
    ended_run(db, "failed", told=True)
    asking = message(db, "anything new?", now + timedelta(minutes=1))
    got = news_reply(db, Settings(), OWNER, event_id=asking, now=now + timedelta(minutes=1))
    assert got == NEWS_NOTHING


def test_an_unreadable_store_is_none_so_the_text_goes_to_claude(db: sqlite3.Connection) -> None:
    now = datetime.now(UTC)
    message(db, "hi", now - timedelta(minutes=30))
    asking = message(db, "anything new?", now)
    db.execute("DROP TABLE run_questions")
    assert news_reply(db, Settings(), OWNER, event_id=asking, now=now) is None


# --- the usage-limit notice ----------------------------------------------------------------


def test_the_limit_is_said_once_per_reset_then_briefly(db: sqlite3.Connection) -> None:
    now = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)  # 1pm in LA
    until = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)  # 3pm
    first, was_first = ask.limit_notice(db, until, LA, now)
    assert was_first and first == (
        "I'm at my Claude limit until 3pm; I can still do approvals and /status."
    )
    again, was_first = ask.limit_notice(db, until, LA, now)
    assert not was_first and again == "Still at my Claude limit until 3pm."
    assert kv_get(db, ask.LIMIT_TOLD_KV) is not None
    # the next reset is news again
    later = until + timedelta(hours=5)
    nxt, was_first = ask.limit_notice(db, later, LA, now)
    assert was_first and nxt.startswith("I'm at my Claude limit until ")


def test_a_reset_claude_didnt_give_has_no_time_in_it(db: sqlite3.Connection) -> None:
    now = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
    kv_set(db, LIMIT_KNOWN_KV, False)
    text, _ = ask.limit_notice(db, now + timedelta(hours=1), LA, now)
    assert text == "I'm at my Claude limit for now; I can still do approvals and /status."


def test_clock_text_names_the_day_only_when_it_is_not_today() -> None:
    now = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
    assert ask.clock_text(datetime(2026, 10, 22, 22, 30, tzinfo=UTC), LA, now) == "3:30pm"
    assert ask.clock_text(datetime(2026, 10, 22, 22, 0, tzinfo=UTC), LA, now) == "3pm"
    assert ask.clock_text(datetime(2026, 10, 24, 22, 0, tzinfo=UTC), LA, now) == "Sat 3pm"

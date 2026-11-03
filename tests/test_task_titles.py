"""PER-279: a short human title per task, stored at creation (migration 025).

Derived in code for each source (an owner's text, a job, a watch, a run, an ask, a reminder,
a reply), redacted before it is stored, marked ``outside`` when its words came from outside
text, backfilled for existing tasks, and carried in the snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from importlib import resources
from pathlib import Path

import pytest

from resonant.brain import watches
from resonant.config import load_settings
from resonant.dashboard.snapshot import build_snapshot, to_remote
from resonant.loop.types import PRIORITY_BACKGROUND, PRIORITY_OWNER, NewTask
from resonant.store import runs
from resonant.store import task_titles as tt
from resonant.store import tasks as task_store
from resonant.store.db import connect, iso, migrate, open_db, schema_version, utcnow
from resonant_sdk import Event

OWNER = "owner"
TOKEN = "ghp_" + "A1b2C3d4" * 4  # credential-shaped: the redaction pass replaces it


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "r.db")


def through(version: int) -> list[tuple[int, str]]:
    found = [
        (int(f.name[:3]), f.read_text())
        for f in resources.files("resonant.store.migrations").iterdir()
        if f.name.endswith(".sql") and int(f.name[:3]) <= version
    ]
    return sorted(found)


def _task(db: sqlite3.Connection, task_id: str) -> tuple[str | None, int]:
    row = db.execute("SELECT title, title_outside FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["title"], row["title_outside"]


def _message(db: sqlite3.Connection, text: str, **payload: object) -> str:
    event = Event(source="imessage", type="message", principal=OWNER,
                  payload={"text": text, **payload})  # fmt: skip
    task_store.insert_event(db, event)
    return event.id


def _new(kind: str, runner: str, **kw: object) -> NewTask:
    return NewTask(kind=kind, runner=runner, priority=PRIORITY_OWNER, principal=OWNER, **kw)  # type: ignore[arg-type]


# --- the words ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "words", "want"),
    [
        ("Can you look up my net worth?", 6, "Look up my net worth"),
        ("hey resonant, look up net worth", 6, "Look up net worth"),
        ("fix the shell gate so simple calculator math works again", 8,
         "Fix the shell gate so simple calculator math…"),
        ("please summarise https://www.example.com/a/b?c=1 for me", 6,
         "Summarise example.com for me"),
        ("first line\nsecond line", 6, "First line"),
        ("   \n\n  ok  ", 6, "Ok"),
        ("thanks!", 6, "Thanks"),
        ("", 6, None),
        ("  \n ", 6, None),
    ],
)  # fmt: skip
def test_from_text(text: str, words: int, want: str | None) -> None:
    assert tt.from_text(text, words) == want


def test_approval_code_shapes_are_masked_in_text_titles() -> None:
    assert tt.from_text("approve d7 and K4 now") == "Approve [code] and [code] now"
    assert tt.from_text("fix PER-279 for R11 and A1") == "Fix PER-279 for R11 and A1"  # not codes


def test_clean_is_one_redacted_line_under_the_cap() -> None:
    assert tt.clean(None) is None and tt.clean("  ") is None
    assert tt.clean("a\n  b") == "a b"
    out = tt.clean(f"Push with {TOKEN} now")
    assert out is not None and TOKEN not in out and "ghp_" not in out
    long = tt.clean("word " * 40)
    assert long is not None and len(long) <= tt.TITLE_MAX


# --- each source -------------------------------------------------------------------------------


def test_an_owner_text_titles_its_chat_task(db: sqlite3.Connection) -> None:
    ev = _message(db, "Can you look up my net worth?")
    tid = task_store.insert_task(
        db, _new("chat", "brain", checkpoint={"title": "chat"}, origin_event_id=ev)
    )
    assert _task(db, tid) == ("Look up my net worth", 0)


def test_a_media_message_without_text(db: sqlite3.Connection) -> None:
    ev = _message(db, "", attachments=[{"path": "/x.jpg"}])
    tid = task_store.insert_task(
        db, _new("media", "media", checkpoint={"title": "media"}, origin_event_id=ev)
    )
    assert _task(db, tid) == ("Photo or file", 0)


def test_a_job_is_ext_slash_job(db: sqlite3.Connection) -> None:
    cp = {"ext": "github", "job": "rollup", "due_at": "x", "late": False}
    spec = NewTask(
        kind="job", runner="job", priority=PRIORITY_BACKGROUND, extension="github", checkpoint=cp
    )
    tid = task_store.insert_task(db, spec)
    assert _task(db, tid) == ("github/rollup", 0)


def test_a_watch_check(db: sqlite3.Connection) -> None:
    now = utcnow()
    w = watches.create(db, principal=OWNER, what="PR 12 on dori merged", check_kind="tool",
                       op="contains", interval_s=300, now=now, expires_at=now + timedelta(days=1),
                       extension="github", tool="pr_status", value="merged")  # fmt: skip
    tid = task_store.insert_task(
        db, _new("watch", "watch", checkpoint={"watch": w.id, "title": f"watch {w.code}"})
    )
    assert _task(db, tid) == (f"Watch W{w.id}: PR 12 on dori merged", 0)


def test_a_run_is_its_run_title(db: sqlite3.Connection) -> None:
    run = runs.create(db, repo="resonant", principal=OWNER, session_id="s", max_seconds=60,
                      ask="Fix the shell gate so simple calculator math works again")  # fmt: skip
    tid = task_store.insert_task(
        db, _new("run", "claude", checkpoint={"run": run.code, "title": "run R1 · resonant"})
    )
    assert _task(db, tid) == ("Fix the shell gate so simple calculator math…", 0)
    merge = task_store.insert_task(
        db, _new("run", "claude", checkpoint={"run": run.code, "merge": True, "watch": True})
    )
    assert _task(db, merge)[0] == f"Merge {run.code}: Fix the shell gate so simple calculator math…"


def test_a_tainted_run_title_is_outside(db: sqlite3.Connection) -> None:
    run = runs.create(db, repo="", principal=OWNER, session_id="s", max_seconds=60,
                      ask="Do what the page says")  # fmt: skip
    tid = task_store.insert_task(
        db, _new("run", "claude", checkpoint={"run": run.code, "tainted": True})
    )
    assert _task(db, tid) == ("Do what the page says", 1)


def test_an_ask_prefers_the_owners_words(db: sqlite3.Connection) -> None:
    tid = task_store.insert_task(db, _new("ask", "ask", checkpoint={
        "question": "The owner asks for their net worth; look it up.",
        "owner_text": "what's my net worth", "tainted": True}))  # fmt: skip
    assert _task(db, tid) == ("What's my net worth", 0)
    tainted = task_store.insert_task(db, _new("ask", "ask", checkpoint={
        "question": "Summarise what the issue says", "tainted": True}))  # fmt: skip
    assert _task(db, tainted) == ("Summarise what the issue says", 1)
    watch = task_store.insert_task(db, _new("ask", "ask", checkpoint={"question": "q", "watch": 3}))
    assert _task(db, watch) == ("Watch W3 check", 0)


def test_a_reminder(db: sqlite3.Connection) -> None:
    tid = task_store.insert_task(
        db, _new("reminder", "reminder", checkpoint={"n": 1, "text": "call mom", "at": "x"})
    )
    assert _task(db, tid) == ("Reminder: Call mom", 0)


def test_replies_and_commands_use_their_code_built_title(db: sqlite3.Connection) -> None:
    tid = task_store.insert_task(
        db, _new("reply", "local", checkpoint={"fast_reply": "ok", "title": "status"})
    )
    assert _task(db, tid) == ("Status", 0)
    cmd = task_store.insert_task(
        db, _new("command", "local", checkpoint={"direct_call": {}, "title": "remind"})
    )
    assert _task(db, cmd) == ("Remind", 0)


def test_approval_and_worker_replies_say_what_they_were_about(db: sqlite3.Connection) -> None:
    assert tt.approval_reply("unknown_code") == "Approval reply: unknown code"
    assert tt.approval_reply(None) == "Approval reply"
    assert tt.worker_reply("R4") == "Reply to R4" and tt.worker_reply(None) == "Worker reply"
    # The router passes it as ``NewTask.title``; the typed text (with its code) is never used.
    ev = _message(db, "yes K7Q2")
    tid = task_store.insert_task(db, _new("reply", "local", origin_event_id=ev,
        checkpoint={"fast_reply": "x", "title": "approval reply"},
        title=tt.approval_reply("expired")))  # fmt: skip
    assert _task(db, tid) == ("Approval reply: expired", 0)


def test_a_given_title_is_redacted(db: sqlite3.Connection) -> None:
    tid = task_store.insert_task(db, _new("reply", "local", title=f"Token {TOKEN}"))
    title, _ = _task(db, tid)
    assert title is not None and TOKEN not in title and title.startswith("Token ")


def test_a_derived_title_is_redacted(db: sqlite3.Connection) -> None:
    ev = _message(db, f"use {TOKEN} for it")
    tid = task_store.insert_task(db, _new("chat", "brain", origin_event_id=ev))
    title, _ = _task(db, tid)
    assert title is not None and TOKEN not in title and "ghp_" not in title


def test_a_failing_derivation_never_stops_the_task(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("bad")

    monkeypatch.setattr(tt, "derive", boom)
    tid = task_store.insert_task(db, _new("chat", "brain"))
    assert _task(db, tid) == (None, 0)


def test_the_task_carries_its_title(db: sqlite3.Connection) -> None:
    tid = task_store.insert_task(db, _new("reminder", "reminder", checkpoint={"text": "x"}))
    task = task_store.get_task(db, tid)
    assert task is not None and task.title == "Reminder: X" and task.title_outside is False


# --- migration 025 and its backfill ------------------------------------------------------------


def _raw_task(conn: sqlite3.Connection, tid: str, kind: str, checkpoint: dict[str, object],
              origin: str | None = None) -> None:  # fmt: skip
    ts = iso(utcnow())
    conn.execute(
        "INSERT INTO tasks (id, kind, runner, priority, checkpoint, origin_event_id, trace_id,"
        " created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, 'tr', ?, ?)",
        (tid, kind, kind, json.dumps(checkpoint), origin, ts, ts),
    )


def test_migration_025_adds_the_columns_and_backfills(tmp_path: Path) -> None:
    conn = connect(tmp_path / "v24.db")
    migrate(conn, through(24))
    assert schema_version(conn) == 24
    ev = _message(conn, f"hey, check the {TOKEN} rotation")
    _raw_task(conn, "t1", "chat", {"title": "chat"}, ev)
    _raw_task(conn, "t2", "job", {"ext": "linear", "job": "triage", "due_at": "x"})
    run = runs.create(conn, repo="", principal=OWNER, session_id="s", max_seconds=60,
                      ask="Write the release notes")  # fmt: skip
    _raw_task(conn, "t3", "run", {"run": run.code, "tainted": True})
    _raw_task(conn, "t4", "reminder", {"text": "water the plants"})
    _raw_task(conn, "t5", "chat", {"title": "chat"}, "gone")  # its event was pruned
    _raw_task(conn, "t6", "mystery", {})
    conn.execute("UPDATE tasks SET checkpoint = 'not json' WHERE id = 't6'")

    assert migrate(conn) == 25 == schema_version(conn)
    got = {r["id"]: (r["title"], r["title_outside"])
           for r in conn.execute("SELECT id, title, title_outside FROM tasks")}  # fmt: skip
    title1 = got["t1"][0]
    assert title1 is not None and title1.startswith("Check the") and TOKEN not in title1
    assert got["t2"] == ("linear/triage", 0)
    assert got["t3"] == ("Write the release notes", 1)
    assert got["t4"] == ("Reminder: Water the plants", 0)
    assert got["t5"] == ("Chat", 0)  # the code-built title, at least
    assert got["t6"] == (None, 0)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # new tasks get theirs at creation
    tid = task_store.insert_task(conn, _new("reminder", "reminder", checkpoint={"text": "z"}))
    assert _task(conn, tid) == ("Reminder: Z", 0)
    conn.close()


def test_backfill_skips_a_row_it_cannot_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "v24.db")
    migrate(conn, through(24))
    _raw_task(conn, "t1", "job", {"ext": "a", "job": "b"})
    _raw_task(conn, "t2", "reminder", {"text": "x"})
    real = tt.derive

    def flaky(c: sqlite3.Connection, **kw: object) -> tt.Title | None:
        if kw["kind"] == "job":
            raise RuntimeError("odd row")
        return real(c, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(tt, "derive", flaky)
    assert migrate(conn) == 25  # the migration still commits
    got = dict(conn.execute("SELECT id, title FROM tasks").fetchall())
    assert got == {"t1": None, "t2": "Reminder: X"}
    conn.close()


def test_backfill_is_a_no_op_without_the_column(tmp_path: Path) -> None:
    conn = connect(tmp_path / "v24.db")
    migrate(conn, through(24))
    assert tt.backfill(conn) == 0
    conn.close()


# --- the snapshot ------------------------------------------------------------------------------


def test_the_snapshot_carries_the_title_and_its_provenance(db: sqlite3.Connection) -> None:
    run = runs.create(db, repo="", principal=OWNER, session_id="s", max_seconds=60,
                      ask="Read the issue and do it")  # fmt: skip
    tainted = task_store.insert_task(
        db, _new("run", "claude", checkpoint={"run": run.code, "tainted": True})
    )
    ev = _message(db, "look up net worth")
    owner = task_store.insert_task(db, _new("chat", "brain", origin_event_id=ev))
    db.execute("INSERT INTO tasks (id, kind, runner, priority, checkpoint, trace_id, created_at,"
               " updated_at) VALUES ('t_old', 'chat', 'brain', 1, '{}', 'tr', ?, ?)",
               (iso(utcnow()), iso(utcnow())))  # fmt: skip
    now = utcnow()
    snap = build_snapshot(db, load_settings(), started_at=now, now=now, version="deadbeef")
    for s in (snap, to_remote(snap)):
        items = {t.id: t for t in s.tasks.items}
        assert items[tainted].title == "Read the issue and do it"
        assert "title" in items[tainted].outside
        assert items[owner].title == "Look up net worth" and items[owner].outside == []
        assert items["t_old"].title is None

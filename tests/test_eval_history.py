"""PER-248: drafting a task-eval set from the owner's past texts (``resonant eval draft``).

Every test builds a fixture store in the temp RESONANT_HOME (conftest): never ~/.resonant,
~/code, a real model or iMessage. The e2e test makes its store the real way, by running a
text through the task-eval harness, then drafts from it and grades the reviewed row.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from resonant.cli import app
from resonant.config import Settings
from resonant.evals import history as hist
from resonant.evals import task_eval as te
from resonant.loop import NewTask
from resonant.loop.types import TaskStatus
from resonant.store import repos as repo_store
from resonant.store import runs as run_store
from resonant.store import tasks as task_store
from resonant.store.audit import audit
from resonant.store.db import iso, open_db, transaction
from resonant_sdk import Event

OWNER_HANDLE = "+15550100100"
FRIEND_HANDLE = "friend@example.org"
PRINCIPALS = f"""\
principals:
  owner:
    role: owner
    identities: ["imessage:{OWNER_HANDLE}"]
  sam:
    role: member
    identities: ["imessage:{FRIEND_HANDLE}"]
"""
NOW = datetime.now(UTC).replace(microsecond=0)
REMIND = {"tool": "add_reminder", "effect": "write", "level": "L3"}
START = {"tool": "start_run", "effect": "write", "level": "L2"}


# --- a fixture store -----------------------------------------------------------------------


@pytest.fixture
def home(resonant_home: Path) -> Path:
    (resonant_home / "principals.yaml").write_text(PRINCIPALS)
    return resonant_home


@pytest.fixture
def store(home: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(home / "resonant.db")
    yield conn
    conn.close()


def settings(home: Path) -> Settings:
    return Settings(home=home)


def chat(
    conn: sqlite3.Connection,
    text: str,
    *,
    minutes_ago: float = 60,
    reply: str = "Done.",
    calls: list[dict[str, Any]] | None = None,
    route: str | None = "local",
    status: TaskStatus = "done",
    principal: str = "owner",
    claude: str | None = None,
    rejected: list[str] | None = None,
    result: dict[str, Any] | None = None,
    runner: str = "brain",
) -> str:
    """One past chat as the daemon stores it: the message event, the chat task (its reply
    and result), a ``gate.evaluate`` audit row per call, optional rejected approvals and a
    Claude ask task on the same trace."""
    at = NOW - timedelta(minutes=minutes_ago)
    event = Event(
        source="imessage",
        type="message",
        principal=principal,
        payload={"text": text, "handle": OWNER_HANDLE, "guid": uuid.uuid4().hex},
        occurred_at=at,
    )
    res: dict[str, Any] = dict(result or {})
    if route is not None:
        res["route"] = route
    with transaction(conn):
        task_store.insert_event(conn, event)
        task_id = task_store.insert_task(
            conn,
            NewTask(
                kind="chat",
                runner=runner,
                priority=10,
                principal=principal,
                checkpoint={"title": "chat", "reply": reply},
                origin_event_id=event.id,
                trace_id=event.trace_id,
            ),
        )
        task_store.attach_event(conn, event.id, task_id)
        conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (json.dumps(res), task_id))
        for c in calls or []:
            audit(
                conn,
                "gate.evaluate",
                task_id=task_id,
                requested_by=principal,
                tool=c["tool"],
                extension=c.get("extension"),
                effect=c.get("effect", "read"),
                level=c.get("level", "L0"),
                decision=c.get("decision", "Allow"),
                trace_id=event.trace_id,
                approval_id=c.get("approval_id"),
            )
        for tool in rejected or []:
            conn.execute(
                """INSERT INTO approvals (id, task_id, intent_hash, tool, effect, level, summary,
                       status, expires_at, created_at)
                   VALUES (?, ?, ?, ?, 'write', 'L2', 'x', 'rejected', ?, ?)""",
                (uuid.uuid4().hex, task_id, uuid.uuid4().hex, tool, iso(NOW), iso(at)),
            )
        if claude is not None:
            task_store.insert_task(
                conn,
                NewTask(
                    kind="ask",
                    runner="ask",
                    priority=10,
                    principal=principal,
                    checkpoint={"reply": claude},
                    trace_id=event.trace_id,
                ),
            )
    task_store.set_status(conn, task_id, status)
    return task_id


def draft(home: Path, **kw: Any) -> list[dict[str, Any]]:
    report = hist.draft(settings(home), **kw)
    return rows(report.path)


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def by_text(drafted: list[dict[str, Any]], needle: str) -> dict[str, Any]:
    (row,) = [r for r in drafted if needle in r["text"]]
    return row


# --- drafting --------------------------------------------------------------------------------


def test_rows_get_suggested_expectations_from_what_happened(
    home: Path, store: sqlite3.Connection
) -> None:
    chat(store, "what notes do I have?", calls=[{"tool": "list_notes"}], reply="Two: milk, eggs.")
    chat(
        store,
        "remind me to call the vet at 5",
        calls=[{**REMIND, "decision": "NeedsApproval"}, {**REMIND, "approval_id": "a1"}],
    )
    chat(
        store,
        "jot down: buy stamps",
        calls=[{"tool": "add_note", "effect": "write", "level": "L3"}],
    )
    chat(
        store,
        "kick off a run on dori to fix the login",
        calls=[{"tool": "list_repos"}, {**START, "decision": "NeedsApproval"}],
        rejected=["start_run"],
    )
    chat(
        store,
        "summarize the mycelium design doc",
        route="claude",
        result={"handoff": "triage"},
        reply="Looking into it…",
        claude="It's a three-layer design.",
    )
    chat(store, "delete the note", reply="Which one: milk or eggs?", calls=[{"tool": "list_notes"}])
    chat(store, "any PRs failing?", calls=[{"tool": "prs", "extension": "github"}])
    chat(store, "what's on the calendar", calls=[{"tool": "events", "extension": "gcal"}])
    chat(store, "tail the daemon log", calls=[{"tool": "tail_log"}])
    chat(
        store,
        "restart ollama then list runs",
        calls=[{"tool": "restart_ollama", "effect": "write", "decision": "Deny"},
               {"tool": "list_runs"}],
    )  # fmt: skip
    chat(store, "hi", minutes_ago=10, runner="local", route=None, reply="Hi!")
    drafted = draft(home)

    assert len(drafted) == 11
    assert all(r["review"] is True and r["id"].startswith("own-") for r in drafted)
    te.load_set(te.owner_set_path(settings(home)))  # the file is a valid task set

    notes = by_text(drafted, "what notes")
    assert notes["expect"] == {"route": "local", "tools": ["list_notes"], "ask": False}
    assert notes["category"] == "notes"
    assert notes["draft"]["reply"] == "Two: milk, eggs." and notes["draft"]["status"] == "done"

    remind = by_text(drafted, "call the vet")["expect"]
    assert remind["ask"] == "approval" and remind["tools"] == ["add_reminder"]
    assert "writes" not in remind  # it ran only after the owner's yes

    jot = by_text(drafted, "buy stamps")["expect"]
    assert jot["writes"] == ["add_note"] and jot["ask"] is False

    run = by_text(drafted, "kick off a run")
    # the owner said no: asking was right, so it's an approval row, not a forbidden tool
    assert run["expect"] == {"route": "local", "tools": ["list_repos", "start_run"],
                             "ask": "approval"}  # fmt: skip
    assert "you said no to start_run" in run["note"] and run["category"] == "runs"

    denied = by_text(drafted, "restart ollama")["expect"]
    assert denied["tools_not"] == ["restart_ollama"] and denied["tools"] == ["list_runs"]

    research = by_text(drafted, "mycelium")
    assert research["expect"]["route"] == "claude" and research["category"] == "research"
    assert research["draft"]["reply"] == "It's a three-layer design."  # Claude's answer
    assert "grades routing" in research["note"]

    assert by_text(drafted, "delete the note")["expect"]["ask"] is True  # it asked back

    prs = by_text(drafted, "PRs failing")
    assert prs["world"]["extensions"] == {"github": {}}
    assert prs["expect"]["tools"] == ["prs"] and prs["category"] == "extensions"
    assert "canned defaults" in prs["note"]

    assert "'gcal' has no eval fixture" in by_text(drafted, "calendar")["note"]
    assert "tail_log read real files" in by_text(drafted, "daemon log")["note"]

    hi = by_text(drafted, "hi")
    assert "route" not in hi["expect"]  # the intents path has no route
    assert [r["text"] for r in drafted][-1] == "hi"  # oldest first


def test_only_the_owners_chat_texts_are_drafted(home: Path, store: sqlite3.Connection) -> None:
    chat(store, "what is sam doing", principal="sam")
    chat(store, "yes K4")  # an approval reply that reached the chat path: never drafted
    chat(store, "no")
    chat(store, "what's running?")
    with transaction(store):  # a reminder firing, not a text
        task_store.insert_task(
            store,
            NewTask(kind="reminder", runner="reminder", priority=10, principal="owner",
                    checkpoint={"text": "x", "at": iso(NOW)}, trace_id=uuid.uuid4().hex),
        )  # fmt: skip
    report = hist.draft(settings(home))
    assert [r["text"] for r in rows(report.path)] == ["what's running?"]
    assert report.skipped["approval_reply"] == 2


def test_redaction_drops_codes_secrets_and_other_peoples_handles(
    home: Path, store: sqlite3.Connection
) -> None:
    prompt = "[K4] Start a run on dori? Reply yes / no"
    chat(store, "earlier question", minutes_ago=62, reply=prompt)
    chat(
        store,
        f"text {FRIEND_HANDLE} and +1 (555) 010-0177 and ask @alice about PR 42 on 2026-10-01",
        minutes_ago=61,
        reply="Approve with yes K4. I'll ping bob@example.com.",
    )
    chat(store, "my github token is ghp_abcdefghijklmnopqrstuvwxyz0123456789", minutes_ago=60)
    chat(store, f"call {OWNER_HANDLE} back", minutes_ago=59)
    report = hist.draft(settings(home))
    drafted = rows(report.path)
    # Mask ULIDs before scanning: a random one can contain "K4" (or any short code) by chance.
    raw = re.sub(r"\b[0-9A-HJKMNP-TV-Z]{26}\b", "<ulid>", report.path.read_text())
    for leaked in (FRIEND_HANDLE, "010-0177", "@alice", "bob@example.com", "ghp_", OWNER_HANDLE,
                   "K4"):  # fmt: skip
        assert leaked not in raw, leaked
    assert report.skipped["secret"] == 1
    asked = by_text(drafted, "about PR 42")
    assert asked["text"] == (
        "text [handle] and [handle] and ask [handle] about PR 42 on 2026-10-01"
    )  # a PR number and a date are not phone numbers
    assert asked["history"] == [
        {"user": "earlier question", "reply": "[?] Start a run on dori? Reply yes / no"}
    ]
    assert "redacted: code, handle" in asked["note"]
    assert by_text(drafted, "back")["text"] == "call [handle] back"


def test_unicode_tricks_are_normalized_before_redaction(
    home: Path, store: sqlite3.Connection
) -> None:
    chat(store, "approve \uff21\uff17 and text fri\u200bend@example.org", minutes_ago=3)
    path = hist.draft(settings(home)).path
    (row,) = rows(path)
    assert row["text"] == "approve ? and text [handle]"


def test_a_line_separator_in_a_text_stays_on_its_row(home: Path, store: sqlite3.Connection) -> None:
    chat(store, "first line\u2028second\u2029third\x85end", minutes_ago=3)
    chat(store, "what's running?", minutes_ago=2)
    path = hist.draft(settings(home)).path
    assert len(path.read_text().split("\n")) == 3  # two rows and the final newline
    assert next(r.text for r in te.load_set(path)) == "first line\u2028second\u2029third\x85end"
    assert hist.draft(settings(home)).drafted == 0  # and the next draft reads it back


def test_context_holding_a_credential_is_left_out(home: Path, store: sqlite3.Connection) -> None:
    chat(store, "my key is sk-ant-abcdefghijklmnopqrstuvwxyz", minutes_ago=5, reply="Got it.")
    chat(store, "yes", minutes_ago=4, reply="Done.")
    chat(store, "and the deploy?", minutes_ago=3,
         reply="Use token ghp_abcdefghijklmnopqrstuvwxyz0123456789")  # fmt: skip
    path = hist.draft(settings(home)).path
    raw = path.read_text()
    assert "sk-ant" not in raw and "ghp_" not in raw
    row = by_text(rows(path), "deploy")
    assert "history" not in row  # the secret turn and the approval reply are both dropped
    assert row["draft"]["reply"] == hist.WITHHELD


def test_unreadable_principals_exit_2_without_their_values(
    home: Path, store: sqlite3.Connection
) -> None:
    chat(store, "what's running?")
    (home / "principals.yaml").write_text("principals:\n  x: {role: owner, identities: 5550199}\n")
    result = CliRunner().invoke(app, ["eval", "draft", "--from-history"])
    assert result.exit_code == 2 and "cannot read" in result.output
    assert "5550199" not in result.output
    (home / "principals.yaml").write_text("principals: [unclosed\n")
    result = CliRunner().invoke(app, ["eval", "draft", "--from-history"])
    assert result.exit_code == 2 and "cannot read" in result.output


def test_run_set_never_grades_a_row_awaiting_review() -> None:
    reviewed = te.TaskCase(1, "a", "chat", "hi", (), {}, te.Expect(), ["Hi."])
    pending = te.TaskCase(2, "b", "chat", "yo", (), {}, te.Expect(), ["Yo."], review=True)
    out = asyncio.run(te.run_set([reviewed, pending], ["loop"], model=None))
    assert [r.case.id for r in out["loop"]] == ["a"]


def test_near_identical_texts_are_drafted_once_newest_kept(
    home: Path, store: sqlite3.Connection
) -> None:
    chat(store, "status?", minutes_ago=50)
    chat(store, "Status!", minutes_ago=40)
    newest = chat(store, "status", minutes_ago=30)
    chat(store, "whats running on the mini rn", minutes_ago=20)
    chat(store, "what's running on the mini rn?", minutes_ago=10)
    chat(store, "what's on my linear", minutes_ago=5)
    report = hist.draft(settings(home))
    drafted = rows(report.path)
    assert [r["text"] for r in drafted] == [
        "status",
        "what's running on the mini rn?",
        "what's on my linear",
    ]
    assert drafted[0]["draft"]["task"] == newest
    assert report.skipped["duplicate"] == 3


def test_a_rerun_keeps_the_owners_edits_and_adds_only_new_texts(
    home: Path, store: sqlite3.Connection
) -> None:
    chat(store, "what notes do I have?", minutes_ago=30)
    path = hist.draft(settings(home)).path
    (row,) = rows(path)
    row["review"] = False
    row["text"] = "what notes do i have"  # the owner's own edit
    row["expect"]["facts"] = ["milk"]
    edited = json.dumps(row)
    path.write_text(edited + "\n")
    chat(store, "What notes do I have??", minutes_ago=20)  # near-identical to the edit
    chat(store, "anything failing?", minutes_ago=10)
    report = hist.draft(settings(home))
    lines = path.read_text().splitlines()
    assert lines[0] == edited  # kept byte for byte
    assert [json.loads(ln)["text"] for ln in lines[1:]] == ["anything failing?"]
    assert report.drafted == 1 and report.total == 2 and report.pending_review == 1
    assert report.skipped["already_drafted"] == 1
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    again = hist.draft(settings(home))
    assert again.drafted == 0 and path.read_text().splitlines() == lines


def test_an_empty_set_file_is_drafted_into(home: Path, store: sqlite3.Connection) -> None:
    path = te.owner_set_path(settings(home))
    path.parent.mkdir()
    path.write_text("\n")
    chat(store, "what's running?")
    assert [r["text"] for r in draft(home)] == ["what's running?"]


def test_since_and_limit(home: Path, store: sqlite3.Connection) -> None:
    asks = ["where is dori", "restart ollama", "any PRs failing", "what's on linear",
            "how full is the disk", "note: call mom 5"]  # fmt: skip
    for i, text in enumerate(asks):
        chat(store, text, minutes_ago=60 * 24 * (6 - i))
    since = hist.parse_since("3d", NOW)
    texts = [r["text"] for r in draft(home, since=since)]
    assert len(texts) == 3 and texts[-1].endswith("5")
    te.owner_set_path(settings(home)).unlink()
    newest = ["how full is the disk", "note: call mom 5"]
    assert [r["text"] for r in draft(home, limit=2)] == newest


@pytest.mark.parametrize(
    ("raw", "delta"), [("30d", timedelta(days=30)), ("12h", timedelta(hours=12)),
                       ("2w", timedelta(weeks=2))]
)  # fmt: skip
def test_parse_since(raw: str, delta: timedelta) -> None:
    assert hist.parse_since(raw, NOW) == NOW - delta
    assert hist.parse_since("2026-09-01", NOW) == datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="--since"):
        hist.parse_since("last tuesday", NOW)


def test_the_world_is_the_store_as_it_was_then(home: Path, store: sqlite3.Connection) -> None:
    old = iso(NOW - timedelta(days=30))
    repo_store.add(store, "owner/dori", base="main", source="test", name="dori")
    repo_store.add(store, "owner/later", base="main", source="test", name="later")
    store.execute("UPDATE repos SET added_at = ? WHERE slug = 'owner/dori'", (old,))
    at = NOW - timedelta(minutes=60)
    # a run that started 20 minutes before the text and finished after it
    r1 = run_store.create(store, repo="dori", ask="fix the login", principal="owner",
                          session_id="s1", max_seconds=60)  # fmt: skip
    run_store.update(store, r1.code, status="running")
    run_store.finish(store, r1.code, "done", pr_number=7, pr_url="https://example.com/pr/7")
    store.execute(
        "UPDATE runs SET created_at = ?, started_at = ?, ended_at = ? WHERE code = ?",
        (iso(at - timedelta(minutes=20)), iso(at - timedelta(minutes=20)), iso(NOW), r1.code),
    )
    # one that started after the text
    run_store.create(store, repo="dori", ask="later run", principal="owner", session_id="s2",
                     max_seconds=60)  # fmt: skip
    notes = [("1", {"text": "milk", "at": old, "deleted_at": None}),
             ("2", {"text": "gone", "at": old, "deleted_at": iso(at - timedelta(minutes=5))}),
             ("3", {"text": "after", "at": iso(NOW), "deleted_at": None})]  # fmt: skip
    for key, value in notes:
        store.execute(
            "INSERT INTO memory (scope, key, value, created_at, updated_at)"
            " VALUES ('notes', ?, ?, ?, ?)",
            (key, json.dumps(value), old, old),
        )
    chat(
        store,
        "how's the dori run, and my notes?",
        minutes_ago=60,
        calls=[{"tool": "list_runs"}, {"tool": "list_notes"}],
    )
    (row,) = draft(home)
    world = row["world"]
    assert world["repos"] == [{"name": "dori", "slug": "owner/dori", "base": "main"}]
    (run,) = world["runs"]
    assert run["ask"] == "fix the login" and run["status"] == "running"
    assert run["minutes_ago"] == 20 and "pr_number" not in run  # the PR came after
    assert world["notes"] == ["milk"]
    assert "runs as they stood then" in row["note"]


def test_no_store_and_a_malformed_set_exit_2_and_touch_nothing(home: Path) -> None:
    result = CliRunner().invoke(app, ["eval", "draft", "--from-history"])
    assert result.exit_code == 2 and "no store" in result.output
    assert not (home / "resonant.db").exists()  # read-only: never created
    conn = open_db(home / "resonant.db")
    chat(conn, "what's running?")
    conn.close()
    bad = home / "evals" / "task.jsonl"
    bad.parent.mkdir()
    bad.write_text("{not json\n")
    result = CliRunner().invoke(app, ["eval", "draft", "--from-history"])
    assert result.exit_code == 2 and "malformed" in result.output
    assert bad.read_text() == "{not json\n"
    result = CliRunner().invoke(app, ["eval", "draft"])
    assert result.exit_code == 2 and "--from-history" in result.output


def test_the_store_is_opened_read_only(home: Path, store: sqlite3.Connection) -> None:
    chat(store, "what's running?")
    conn = hist.open_store(home / "resonant.db")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM kv")
    conn.close()


# --- the review gate -------------------------------------------------------------------------


def _task(*args: str) -> Any:
    return CliRunner().invoke(app, ["eval", "task", "--model", "scripted", *args])


def test_rows_awaiting_review_are_listed_not_graded(home: Path, store: sqlite3.Connection) -> None:
    chat(store, "what notes do I have?", minutes_ago=30, calls=[{"tool": "list_notes"}])
    chat(store, "anything failing?", minutes_ago=20)
    result = CliRunner().invoke(app, ["eval", "draft", "--from-history"])
    assert result.exit_code == 0, result.output
    assert "drafted 2 row(s)" in result.output and "2 awaiting review" in result.output
    assert "notes do I have" not in result.output  # counts only, never a text

    out = home / "results"
    result = _task("--set", "owner", "--out", str(out))
    assert result.exit_code == 2 and "awaiting review (2)" in result.output

    path = te.owner_set_path(settings(home))
    drafted = rows(path)
    drafted[0]["review"] = False
    drafted[0]["script"] = [[["list_notes", {}]], "No notes yet."]
    path.write_text("".join(json.dumps(r) + "\n" for r in drafted))
    result = _task("--set", "owner", "--out", str(out), "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["meta"]["cases"] == 1 and data["modes"]["loop"]["accuracy"] == 1.0
    assert data["meta"]["review"] == {
        "pending": 1, "cases": [{"id": drafted[1]["id"], "line": 2}],
    }  # fmt: skip
    (saved,) = out.glob("task-*.json")
    assert "anything failing" not in saved.read_text()
    text = _task("--set", "owner", "--out", str(out))
    assert "awaiting review (1)" in text.output and drafted[1]["id"] in text.output


def test_review_must_be_a_bool(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"id": "a", "category": "chat", "text": "hi", "review": "yes"}))
    with pytest.raises(te.EvalSetError, match="'review' must be true or false"):
        te.load_set(path)


def test_set_owner_resolves_to_the_owners_file(tmp_path: Path) -> None:
    s = Settings(home=tmp_path)
    assert te.resolve_set_path(s, Path("owner")) == tmp_path / "evals" / "task.jsonl"
    assert te.resolve_set_path(s, Path("x.jsonl")) == Path("x.jsonl")


# --- e2e: a real turn, drafted and replayed --------------------------------------------------


def test_e2e_a_real_turn_drafts_a_row_that_replays(home: Path) -> None:
    """The store comes from the real stack (router, brain, gate, executor, channel) running
    one text with a scripted model in this home; the draft reads it back; the row, once
    reviewed, replays through ``eval task --set owner`` and passes."""
    case = te.TaskCase(
        line=1, id="seed", category="notes", text="save a note: renew passport", history=(),
        world={"notes": ["milk"]}, expect=te.Expect(), script=None,
    )  # fmt: skip
    script: list[Any] = [[["list_notes", {}]], [["add_note", {"text": "renew passport"}]],
                         "Saved: renew passport."]  # fmt: skip
    model = te.RecordingModel(te.ScriptedRoot(script))

    async def go() -> te.Outcome:
        with te.world_home(home):
            return await te._run_in(  # pyright: ignore[reportPrivateUsage]
                case, "loop", model, home, None, te.SCRIPTED_TIMEOUT_S, lambda: 0.0
            )

    outcome = asyncio.run(go())
    assert outcome.reply.startswith("Saved: renew passport."), outcome

    result = CliRunner().invoke(app, ["eval", "draft", "--from-history", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["drafted"] == 1
    path = te.owner_set_path(settings(home))
    (row,) = rows(path)
    assert row["text"] == "save a note: renew passport"
    assert row["expect"]["tools"] == ["list_notes", "add_note"]
    assert row["expect"]["writes"] == ["add_note"]  # autonomy on: L3 ran without asking
    assert row["expect"]["ask"] is False
    assert row["world"]["notes"] == ["milk"]  # as it was when the text came in
    assert row["draft"]["reply"].startswith("Saved: renew passport.")

    row["review"] = False
    row["script"] = script
    path.write_text(json.dumps(row) + "\n")
    result = _task("--set", "owner", "--out", str(home / "results"), "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["modes"]["loop"]["accuracy"] == 1.0

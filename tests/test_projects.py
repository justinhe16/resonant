"""The ``projects`` tool (``tools/projects.py``) and its renderer (``router/render.py``)."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from resonant.executor import ToolFailed
from resonant.loop import NewTask
from resonant.loop.types import TERMINAL, TaskStatus, WaitReason
from resonant.router import commands
from resonant.router.commands import UsageError
from resonant.router.fast_path import COMMAND_RENDERS, match_fast_path
from resonant.router.render import MAX_REPLY_CHARS, render_projects, state_label
from resonant.store.db import iso, utcnow
from resonant.store.tasks import insert_task, set_status
from resonant.tools.projects import PROJECTS_ALL, PROJECTS_DEFAULT, projects

BODY = "please wire $500 to my landlord at 12 Elm St"


def add(
    db: sqlite3.Connection,
    kind: str = "chat",
    *,
    status: TaskStatus = "queued",
    wait_reason: WaitReason | None = None,
    age_s: float = 0,
    checkpoint: dict[str, Any] | None = None,
) -> str:
    task_id = insert_task(
        db, NewTask(kind=kind, runner="local", priority=1, checkpoint=checkpoint or {})
    )
    if status != "queued":
        set_status(db, task_id, status, wait_reason=wait_reason)
    db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        (iso(utcnow() - timedelta(seconds=age_s)), task_id),
    )
    return task_id


# --- tool --------------------------------------------------------------------------------


def test_empty(db: sqlite3.Connection) -> None:
    assert projects(db, {}) == {"items": [], "count": 0, "more": 0, "all": False}
    assert render_projects(projects(db, {})) == "No tasks yet."


def test_item_shape(db: sqlite3.Connection) -> None:
    task_id = add(
        db, status="waiting", wait_reason="approval", age_s=90, checkpoint={"title": "/update"}
    )
    out = projects(db, {})
    json.dumps(out, allow_nan=False)
    (item,) = out["items"]
    assert item == {
        "id": task_id,
        "state": "waiting",
        "wait_reason": "approval",
        "age_s": item["age_s"],
        "kind": "chat",
        "title": "/update",
    }
    assert 89 <= item["age_s"] <= 91


def test_open_first_then_updated_at_desc(db: sqlite3.Connection) -> None:
    # Inserted oldest-id-first, but ages say otherwise: ordering is by updated_at, not id.
    old_done = add(db, status="done", age_s=600)
    new_done = add(db, status="failed", age_s=5)
    old_open = add(db, status="running", age_s=3000)
    new_open = add(db, status="queued", age_s=30)
    mid_done = add(db, status="cancelled", age_s=60)
    ids = [i["id"] for i in projects(db, {})["items"]]
    assert ids == [new_open, old_open, new_done, mid_done, old_done]


def test_open_work_is_never_hidden_by_recent_replies(db: sqlite3.Connection) -> None:
    stuck = add(db, status="waiting", wait_reason="human_reply", age_s=86400)
    for _ in range(PROJECTS_DEFAULT + 5):
        add(db, "reply", status="done")
    out = projects(db, {})
    assert out["items"][0]["id"] == stuck
    assert out["count"] == PROJECTS_DEFAULT


@pytest.mark.parametrize(("args", "cap"), [({}, PROJECTS_DEFAULT), ({"all": True}, PROJECTS_ALL)])
def test_caps(db: sqlite3.Connection, args: dict[str, Any], cap: int) -> None:
    for _ in range(PROJECTS_ALL + 7):
        add(db, status="done")
    out = projects(db, args)
    assert out["count"] == cap == len(out["items"])
    assert out["more"] == PROJECTS_ALL + 7 - cap
    assert projects(db, {"all": False})["count"] == PROJECTS_DEFAULT


@pytest.mark.parametrize("args", [{"all": "yes"}, {"all": 1}, {"limit": 5}, {"x": 1}])
def test_bad_args(db: sqlite3.Connection, args: dict[str, Any]) -> None:
    with pytest.raises(ToolFailed):
        projects(db, args)


def test_titles(db: sqlite3.Connection) -> None:
    add(db, "command", checkpoint={"title": "/note", "args": {"text": BODY}})
    add(db, "chat", checkpoint={"intent": "question", "title": "chat: question", "text": BODY})
    add(db, "reminder", checkpoint={"text": "call mom about the   flights to Lisbon", "at": "x"})
    add(db, "reminder", checkpoint={"text": "  "})
    add(db, "chat", checkpoint={"body": BODY, "messages": [BODY], "text": BODY})
    add(db, "chat", checkpoint={"title": {"nested": BODY}})
    add(db, "chat", checkpoint={"title": "x" * 100})
    titles = {i["title"] for i in projects(db, {})["items"]}
    assert titles == {
        "/note",
        "chat: question",
        "⏰ call mom about the flights to…",
        "reminder",
        "chat",
        "x" * 39 + "…",
    }


def test_never_returns_message_bodies(db: sqlite3.Connection) -> None:
    for kind in ("chat", "reply", "run_project"):
        add(db, kind, checkpoint={"fast_reply": BODY, "text": BODY, "body": BODY, "prompt": BODY})
    out = projects(db, {"all": True})
    assert "landlord" not in json.dumps(out)
    assert "landlord" not in render_projects(out)


def test_terminal_states_sort_after_open_ones(db: sqlite3.Connection) -> None:
    """The query spells out ``loop.types.TERMINAL``; pin it to the real set."""
    for status in ("done", "failed", "cancelled", "queued", "running", "waiting"):
        add(db, status=status, wait_reason="ci" if status == "waiting" else None, age_s=0)
    states = [i["state"] for i in projects(db, {})["items"]]
    assert set(states[3:]) == TERMINAL and not TERMINAL & set(states[:3])


# --- renderer ----------------------------------------------------------------------------


def item(
    state: str, *, reason: str | None = None, kind: str = "chat", title: str = ""
) -> dict[str, Any]:
    return {
        "id": "01X",
        "state": state,
        "wait_reason": reason,
        "age_s": 125,
        "kind": kind,
        "title": title or kind,
    }


def test_every_state_is_labeled() -> None:
    data: dict[str, Any] = {
        "items": [
            item("done", kind="reply", title="/status"),
            item("queued", kind="reminder", title="⏰ call mom"),
            item("cancelled"),
            item("waiting", reason="approval", title="chat: ops"),
            item("failed"),
            item("running", kind="run_project", title="dori deploy"),
            item("waiting", reason="human_reply"),
        ],
        "count": 7,
        "more": 0,
        "all": False,
    }
    assert render_projects(data) == (
        "Projects (4 open, 3 recent):\n"
        "- running · run_project · dori deploy · 2m\n"
        "- waiting(approval) · chat · chat: ops · 2m\n"
        "- waiting(human_reply) · chat · 2m\n"
        "- queued · reminder · ⏰ call mom · 2m\n"
        "- done · reply · /status · 2m\n"
        "- cancelled · chat · 2m\n"
        "- failed · chat · 2m"
    )


@pytest.mark.parametrize(
    ("data", "label"),
    [
        ({"state": "waiting", "wait_reason": "veto"}, "waiting(veto)"),
        ({"state": "waiting"}, "waiting(?)"),
        ({"state": "running", "wait_reason": "ci"}, "running"),
        ({}, "?"),
    ],
)
def test_state_label(data: dict[str, Any], label: str) -> None:
    assert state_label(data) == label


def test_more_footer() -> None:
    data: dict[str, Any] = {"items": [item("done")], "count": 1, "more": 4, "all": False}
    assert render_projects(data).endswith("\n+4 more (/projects all)")
    assert render_projects({**data, "all": True}).endswith("\n+4 more")


def test_render_fits_one_imessage(db: sqlite3.Connection) -> None:
    for n in range(PROJECTS_ALL + 10):
        add(
            db,
            "run_project_with_a_long_kind",
            status="waiting",
            wait_reason="human_reply",
            age_s=n * 86400,
            checkpoint={"title": "t" * 200},
        )
    text = render_projects(projects(db, {"all": True}))
    assert len(text) <= MAX_REPLY_CHARS
    hidden = int(text.rsplit("+", 1)[1].split(" ")[0])
    shown = text.count("\n- ")
    assert shown + hidden == PROJECTS_ALL + 10


def test_render_tolerates_bad_data() -> None:
    assert render_projects({}) == "No tasks yet."
    assert render_projects({"items": "nope"}) == "No tasks yet."
    assert render_projects({"items": [1, None, {"state": "done"}]}) == (
        "Projects (0 open, 1 recent):\n- done · ? · ?"
    )


# --- command -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "args"),
    [
        ("/projects", {}),
        ("/projects all", {"all": True}),
        ("/Projects ALL?", {"all": True}),
        ("/tasks", {}),
        ("/tasks all", {"all": True}),
    ],
)
def test_projects_command(text: str, args: dict[str, Any]) -> None:
    cmd = match_fast_path(text)
    assert cmd is not None and cmd.tool == "projects" and cmd.args == args
    assert cmd.render is render_projects
    assert COMMAND_RENDERS[cmd.name] is render_projects


def test_projects_command_usage() -> None:
    cmd = commands.get("/projects")
    assert cmd is not None
    with pytest.raises(UsageError):
        cmd.parse("everything", commands.ParseContext())
    assert cmd.usage_reply() == "Usage: /projects [all]"


def test_malformed_checkpoint_row_is_skipped(db: sqlite3.Connection) -> None:
    good = add(db)
    bad = add(db)
    db.execute("UPDATE tasks SET checkpoint = '{not json' WHERE id = ?", (bad,))
    out = projects(db, {})
    assert [i["id"] for i in out["items"]] == [good] and out["more"] == 0


def fast_reply(db: sqlite3.Connection, command: str, **kw: Any) -> str:
    cp = {"fast_reply": BODY, "tool_used": None, "intent": "system", "title": command}
    return add(db, "reply", checkpoint=cp, **kw)


def test_fast_replies_are_left_out(db: sqlite3.Connection) -> None:
    chat = add(db, status="done", age_s=600, checkpoint={"title": "chat: question"})
    reminder = add(db, "reminder", checkpoint={"text": "stretch"})
    for n in range(20):
        fast_reply(db, ("/status", "/help", "/unknown", "/projects")[n % 4], status="done")
    fast_reply(db, "/projects", status="running")  # the /projects reply being built now
    for args in ({}, {"all": True}):
        out = projects(db, args)
        assert [i["id"] for i in out["items"]] == [reminder, chat]
        assert out["count"] == 2 and out["more"] == 0
    assert "/projects" not in render_projects(projects(db, {}))
    # Only replies with a fast_reply checkpoint are bookkeeping; a waiting one still shows.
    waiting = fast_reply(db, "/update", status="waiting", wait_reason="approval")
    plain = add(db, "reply", status="done")
    ids = [i["id"] for i in projects(db, {})["items"]]
    assert waiting in ids and plain in ids


def test_fast_replies_never_take_cap_slots(db: sqlite3.Connection) -> None:
    for _ in range(PROJECTS_DEFAULT):
        add(db, status="done", age_s=600)
    for _ in range(20):
        fast_reply(db, "/status", status="done")
    out = projects(db, {})
    assert out["count"] == PROJECTS_DEFAULT and out["more"] == 0
    assert {i["kind"] for i in out["items"]} == {"chat"}

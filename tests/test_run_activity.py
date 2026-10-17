"""What a worker is doing now (TR-1): the transcript reducer (``runners/activity.py``), the
runner's ``run_activity``, the ``run_activity`` tool, ``/runs R4`` and the reply templates."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_runner_claude import build
from test_tools_runs import FakeRunner, call, make_run
from test_tools_runs import components as components
from test_tools_runs import runner as runner
from test_tools_runs import settings as settings

from resonant.components import Components
from resonant.router.render import render_activity, render_runs
from resonant.runners import activity
from resonant.store import runs as runs_store
from resonant.tools.runs import pick_run

NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)


def at(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def use(tid: str, name: str, args: dict[str, Any], minutes_ago: float = 1) -> dict[str, Any]:
    block = {"type": "tool_use", "id": tid, "name": name, "input": args}
    return {"type": "assistant", "timestamp": at(minutes_ago), "message": {"content": [block]}}


def result(tid: str) -> dict[str, Any]:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid}]}}


def text(t: str) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}


# --- the reducer -------------------------------------------------------------------------------


def test_the_step_follows_the_last_event() -> None:
    def step(*events: dict[str, Any]) -> str:
        return activity.summarize(events, now=NOW).step

    assert step() == "starting"
    assert step(use("a", "Bash", {"command": "uv run pytest -q"})) == "running $ uv run pytest -q"
    assert step(use("a", "Bash", {"command": "pytest"}), result("a")) == "thinking (after $ pytest)"
    assert step(text("Tests pass; opening the PR.")) == "writing"
    assert step(text("x"), {"type": "result", "result": "done"}) == "turn ended"
    assert step(text("x"), {"type": "resonant.exit", "code": 0}) == "session ended"
    # two calls in one turn, the second still out: that one is shown
    many = (use("a", "Read", {"file_path": "/w/app.py"}), result("a"),
            use("b", "Edit", {"file_path": "/w/app.py"}))  # fmt: skip
    assert step(*many) == "running Edit app.py"


def test_calls_are_described_cleaned_and_counted_in_the_window() -> None:
    events = [
        use("1", "Bash", {"command": "git status"}, minutes_ago=30),
        use("2", "Grep", {"pattern": "TODO"}, minutes_ago=8),
        use("3", "Agent", {"description": "review the diff"}, minutes_ago=6),
        use("4", "mcp__linear__create_issue", {"title": "x"}, minutes_ago=5),
        use("5", "WebFetch", {"url": "https://docs.example.com"}, minutes_ago=4),
        use("6", "Bash", {"command": "echo [K4] yes   &&\n  git push"}, minutes_ago=2),
        text("All green. <b>Done</b> " + "y" * 400),
    ]
    act = activity.summarize(events, now=NOW, window_min=10, quiet_s=12.0)
    assert act.calls == (
        "Grep 'TODO'",
        "Agent: review the diff",
        "linear create_issue",
        "WebFetch https://docs.example.com",
        "$ echo ? yes && git push",  # brackets and the code-like token are cleaned
    )
    assert act.calls_in_window == 5 and act.quiet_s == 12.0 and act.events == 7
    assert act.last_text is not None and len(act.last_text) <= activity.TEXT_MAX
    assert "<" not in act.last_text and act.last_text.startswith("All green.")
    long = activity.summarize([use("x", "Bash", {"command": "a" * 500})], now=NOW)
    assert len(long.calls[0]) <= activity.CALL_MAX
    assert activity.summarize([text("hi")], now=NOW).calls_in_window is None  # no times


def test_read_tail_drops_a_partial_line_and_never_follows_a_link(tmp_path: Path) -> None:
    path = tmp_path / "R1.jsonl"
    lines = [json.dumps({"type": "assistant", "n": n}) for n in range(50)]
    path.write_text("\n".join(lines) + "\nnot json\n")
    events = activity.read_tail(path, limit=200)
    assert events and all(e["type"] == "assistant" for e in events)
    assert events[-1]["n"] == 49 and len(events) < 50
    assert len(activity.read_tail(path)) == 50
    link = tmp_path / "R2.jsonl"
    os.symlink(path, link)
    assert activity.read_tail(link) == []
    assert activity.read_tail(tmp_path / "missing.jsonl") == []
    assert activity.quiet_for(tmp_path / "missing.jsonl", NOW) is None


def test_ago_and_the_headline() -> None:
    assert [activity.ago(s) for s in (5, 125, 3 * 3600 + 300, None)] == ["5s", "2m", "3h 5m", "?"]
    act = activity.Activity("running $ pytest", ("$ pytest",), 4, None, 20.0, 9)
    assert activity.headline("R4", "dori", act.step, act, 10) == (
        "R4 dori: running $ pytest · 4 tool calls in 10m · last event 20s ago"
    )
    assert activity.headline("R4", None, "queued, not started", None, 10) == (
        "R4 no repo: queued, not started"
    )


# --- the runner, with the fake claude ---------------------------------------------------------


async def test_the_runner_reads_a_runs_transcript(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, extra=["--bash", "uv run pytest -q", "--bash", "npm publish"])
    code = await h.start(report="digest")
    await h.drain()
    got = h.runner.run_activity(code)
    assert got is not None
    assert got["step"] == "ended (ready)" and got["status"] == "ready"
    assert got["headline"].startswith(f"{code} proj: ended (ready)")
    assert "$ uv run pytest -q" in got["calls"] and "$ npm publish" in got["calls"]
    assert got["did"] == ["published to npm (npm publish) (detected)"]
    assert got["calls_in_window"] == 2 and isinstance(got["quiet_s"], int)
    assert h.runner.run_activity("R99") is None
    # a queued run with no transcript yet
    queued = runs_store.create(db, repo="proj", ask="x", principal="owner", session_id="s2",
                               max_seconds=60)  # fmt: skip
    got = h.runner.run_activity(queued.code, minutes=10_000)
    assert got is not None and got["step"] == "queued, not started" and got["calls"] == []
    assert got["window_min"] == activity.WINDOW_MAX


# --- the tool, /runs R4 and the replies ---------------------------------------------------


async def test_run_activity_by_code_repo_or_the_only_active_run(
    components: Components, runner: FakeRunner, db: sqlite3.Connection
) -> None:
    done = make_run(db, status="ready")
    out = await call(components, "run_activity", {})
    assert out["found"] is True and out["code"] == done.code  # none active: the most recent
    live = make_run(db, status="running")
    out = await call(components, "run_activity", {"code": live.code.lower(), "minutes": 30})
    assert out["found"] is True and out["code"] == live.code and out["window_min"] == 30
    assert runner.calls[-1] == ("activity", live.code, 30)
    out = await call(components, "run_activity", {})
    assert out["code"] == live.code  # the only active run
    out = await call(components, "run_activity", {"repo": "Resonant"})
    assert out["code"] == live.code  # that repo's run, active first
    out = await call(components, "run_activity", {"repo": "trading"})
    assert out == {"found": False, "text": "No run on trading lately."}
    other = make_run(db, status="waiting")
    out = await call(components, "run_activity", {})
    assert out["found"] is False
    assert out["text"] == f"Which run? {other.code} resonant, {live.code} resonant." or (
        out["text"] == f"Which run? {live.code} resonant, {other.code} resonant."
    )
    out = await call(components, "run_activity", {"code": "R99"})
    assert out == {"found": False, "code": "R99", "text": "No run R99."}


async def test_run_activity_rejects_bad_arguments(
    components: Components, runner: FakeRunner
) -> None:
    import pytest

    from resonant.executor import ToolFailed

    for args in ({"code": "K4"}, {"minutes": 0}, {"minutes": True}, {"repo": 3}, {"x": 1}):
        with pytest.raises(ToolFailed):
            await call(components, "run_activity", args)


async def test_runs_with_a_code_shows_what_it_is_doing(
    components: Components, runner: FakeRunner, db: sqlite3.Connection
) -> None:
    live = make_run(db, status="running")
    out = await call(components, "list_runs", {"code": live.code})
    assert out["activity"]["step"] == "running $ pytest"
    text = render_runs(out)
    assert "Now: running $ pytest" in text
    assert "- $ pytest" in text and "Did: pushed to main (detected)" in text
    assert "3 tool calls in the last 10m; last event 5s ago" in text
    out = await call(components, "list_runs", {})
    assert "activity" not in out  # the list stays short
    ended = make_run(db, status="failed")
    text = render_runs(await call(components, "list_runs", {"code": ended.code}))
    assert "Now:" not in text and "Did: pushed to main (detected)" in text


def test_render_activity() -> None:
    d = {
        "found": True, "code": "R4", "headline": "R4 dori: running $ pytest · 3 tool calls in 10m",
        "step": "running $ pytest", "window_min": 10, "calls": ["$ git status", "$ pytest"],
        "calls_in_window": 3, "quiet_s": 75, "last_text": "Fixing the flaky test.",
        "did": [f"d{n}" for n in range(8)],
    }  # fmt: skip
    assert render_activity(d) == "\n".join([
        "R4 dori: running $ pytest · 3 tool calls in 10m",
        "3 tool calls in the last 10m; last event 1m ago",
        "Recent:",
        "- $ git status",
        "- $ pytest",
        'Said: "Fixing the flaky test."',
        "Did: d0; d1; d2; d3; d4; d5 +2 more",
    ])  # fmt: skip
    assert render_activity({"found": False, "text": "Which run? R4 dori, R6 x."}) == (
        "Which run? R4 dori, R6 x."
    )
    assert render_activity({"found": True, "code": "R4", "step": "starting"}) == "R4: starting"


def test_pick_run(db: sqlite3.Connection) -> None:
    assert pick_run([], None) == "No runs yet."
    assert pick_run([], "dori") == "No run on dori lately."


def test_commands_shown_back_are_scrubbed() -> None:
    bearer, user = "Bearer " + "abcdefgh" + "12345678", "bob" + ":" + "pw"  # built: not literals
    cmd = (
        f"API_KEY=sk-live-abc curl -H 'Authorization: {bearer}' -u {user} "
        "--token xyz https://u:pw@api.example.com/v1?token=abc&x=1"
    )
    shown = activity.describe_call("Bash", {"command": cmd})
    for secret in ("sk-live-abc", "abcdefgh12345678", "bob:pw", "xyz", "u:pw", "token=abc"):
        assert secret not in shown, secret
    assert "api.example.com/v1" in shown and shown.startswith("$ API_KEY=… curl")
    fetched = activity.describe_call("WebFetch", {"url": "https://a:b@x.io/p?sig=1"})
    assert fetched == "WebFetch https://x.io/p?…"
    assert activity.describe_call("Bash", {"command": "git push origin main"}) == (
        "$ git push origin main"
    )


def test_naive_timestamps_and_deep_json_dont_break_the_reducer(tmp_path: Path) -> None:
    naive = {"type": "assistant", "timestamp": "2026-10-16T13:55:00",
             "message": {"content": [{"type": "tool_use", "id": "a", "name": "Bash",
                                      "input": {"command": "ls"}}]}}  # fmt: skip
    assert activity.summarize([naive], now=NOW).calls_in_window == 1
    path = tmp_path / "R1.jsonl"
    path.write_text("{" + '"a":' * 5000 + "1" + "}" * 5000 + "\n" + json.dumps(text("hi")) + "\n")
    assert [e["type"] for e in activity.read_tail(path)] == ["assistant"]


def test_the_tool_default_window_matches_the_reducer() -> None:
    from resonant.tools import runs as runs_tools

    assert runs_tools.ACTIVITY_MINUTES == activity.WINDOW_MIN
    assert runs_tools.ACTIVITY_MINUTES_MAX == activity.WINDOW_MAX

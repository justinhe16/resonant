"""The dashboard snapshot (docs/dashboard-plan.md §3, PER-276): the builder, the caps, the two
tiers (identical but for the Remote Control links and approval codes) and the schema file."""

from __future__ import annotations

import copy
import json
import random
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast, get_args

import pytest
from panel_helpers import panel_manifest, raw_panel, store_panel
from pydantic import BaseModel, ValidationError

from resonant import __version__
from resonant.config import load_settings
from resonant.dashboard import pusher as pusher_mod
from resonant.dashboard import snapshot as snap
from resonant.dashboard.pusher import data_size
from resonant.dashboard.snapshot import Snapshot, build_snapshot, schema_json, to_remote
from resonant.extensions import notify
from resonant.extensions.panels import Normalized
from resonant.loop import SlotPool
from resonant.loop.types import NewTask
from resonant.redact import CUT_MARK, Redactor, redact_secrets
from resonant.runners import side_effects
from resonant.runners.reports import (
    MergedBy,
    record_detected,
    record_did,
    set_merged,
    set_merged_by,
)
from resonant.runners.side_effects import SideEffect
from resonant.store import approvals as ap
from resonant.store import extensions as ext_store
from resonant.store import pulse, questions, repos, runs, trust
from resonant.store import tasks as task_store
from resonant.store.audit import audit
from resonant.store.claude_brain import SESSION_KV, TOOL_ACTION
from resonant.store.db import iso, kv_set, utcnow
from resonant_sdk import Effect, Level

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "docs" / "snapshot.schema.json"
SECRET = "SEKRIT"  # planted in every text field of the busy store
_TEXT_CAP = 800  # nothing in the snapshot is longer


# --- the schema ---------------------------------------------------------------------------


def test_schema_file_is_current() -> None:
    assert SCHEMA_FILE.read_text() == schema_json(), (
        "docs/snapshot.schema.json is stale: run "
        "`uv run python -m resonant.dashboard.snapshot > docs/snapshot.schema.json`"
    )


def _string_nodes(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every string-typed node (and dict key schema) of the schema, with its path."""
    defs = cast(dict[str, Any], schema.get("$defs", {}))
    found: list[tuple[str, dict[str, Any]]] = []

    def walk(node: dict[str, Any], path: str) -> None:
        if "$ref" in node:
            walk(defs[node["$ref"].rsplit("/", 1)[1]], path)
            return
        for key in ("anyOf", "oneOf", "allOf"):
            for i, sub in enumerate(node.get(key, [])):
                walk(sub, f"{path}|{i}")
        if node.get("type") == "string":
            found.append((path, node))
        if "items" in node:
            walk(node["items"], f"{path}[]")
        if "propertyNames" in node:
            found.append((f"{path}{{key}}", node["propertyNames"]))
        elif isinstance(node.get("additionalProperties"), dict):
            found.append((f"{path}{{key}}", {"type": "string"}))
        if isinstance(node.get("additionalProperties"), dict):
            walk(node["additionalProperties"], f"{path}{{value}}")
        for name, sub in cast(dict[str, Any], node.get("properties", {})).items():
            walk(sub, f"{path}.{name}")

    walk(schema, "$")
    return found


def test_free_text_is_capped_in_the_schema() -> None:
    """Every string that is not an enum, a date-time or a pattern has a ``maxLength``, and
    none is over 800, so the relay's size is bounded by the caps. The only strings without
    one are the local-only Remote Control links and approval codes."""
    nodes = _string_nodes(Snapshot.model_json_schema())
    assert nodes
    free = {
        path: node
        for path, node in nodes
        if not ("enum" in node or "const" in node or "pattern" in node)
        and node.get("format") != "date-time"
    }
    uncapped = sorted(p for p, n in free.items() if "maxLength" not in n)
    assert uncapped == [
        "$.approvals.items[].code|0",
        "$.approvals.recent[].code|0",
        "$.claude_brain|0.remote_url|0",
        "$.runs.items[].remote_url|0",
    ]
    assert all(n["maxLength"] <= _TEXT_CAP for n in free.values() if "maxLength" in n)
    assert free["$.runs.items[].ask"]["maxLength"] == 300
    assert free["$.runs.items[].summary|0"]["maxLength"] == 800


def test_local_only_fields_are_marked() -> None:
    """Exactly three things differ between the tiers (PER-276), so a rename can't silently move
    one, and a new local-only field is a decision, not an accident."""
    models = [
        m
        for m in vars(snap).values()
        if isinstance(m, type) and issubclass(m, BaseModel) and m is not BaseModel
    ]
    marked = {(m.__name__, f) for m in models for f in m.model_fields if snap.is_local_only(m, f)}
    assert marked == {
        ("RunItem", "remote_url"),
        ("ClaudeBrain", "remote_url"),
        ("ApprovalItem", "code"),
        ("Panel", "local_url"),
        ("PanelHistory", "pending"),  # PER-286: days the relay doesn't have yet
    }


def test_effect_kinds_mirror_the_detector() -> None:
    """``EffectKind`` copies ``runners.side_effects.Kind`` (an import cycle stops a reuse)."""
    assert get_args(snap.EffectKind) == get_args(side_effects.Kind)


# --- tiers ---------------------------------------------------------------------------------


def _local_view(dump: dict[str, Any]) -> dict[str, Any]:
    """A local dump as the remote tier is: tier remote, and no link or approval code."""
    out = copy.deepcopy(dump)
    out["tier"] = "remote"
    for run in out["runs"]["items"]:
        run.pop("remote_url", None)
    if out.get("claude_brain"):
        out["claude_brain"].pop("remote_url", None)
    for key in ("items", "recent"):
        for a in out["approvals"][key]:
            a.pop("code", None)
    return out


def _check_remote(remote: Snapshot, local: Snapshot | None = None) -> None:
    data = remote.model_dump(mode="json")
    assert data["tier"] == "remote"
    text = json.dumps(data)
    assert '"remote_url"' not in text and "claude.ai/code" not in text
    for key in ("items", "recent"):
        assert all("code" not in a for a in data["approvals"][key])
    assert Snapshot.model_validate(data) == remote  # still a valid snapshot (what DB-3 pushes)
    assert to_remote(remote) == remote
    if local is not None:
        assert data == _local_view(local.model_dump(mode="json"))  # nothing else differs


def test_remote_control_url_is_local_only(db: sqlite3.Connection) -> None:
    """RC-1: a running worker's Remote Control link is on the local dashboard only (and only
    while it runs); the remote tier and the relay never carry it."""
    now = utcnow()
    url = f"https://claude.ai/code/session_{SECRET}"
    live = runs.create(db, repo="dori", ask="fix", principal="justin", session_id="s1",
                       max_seconds=60)  # fmt: skip
    runs.update(db, live.code, status="running", remote_url=url)
    gone = runs.create(db, repo="dori", ask="fix", principal="justin", session_id="s2",
                       max_seconds=60)  # fmt: skip
    runs.update(db, gone.code, status="ready", remote_url=url)
    s = build_snapshot(db, load_settings(), started_at=now, last_tick=now, slots=SlotPool(3, 1),
                       runner_missing=[], now=now, version="deadbeef")  # fmt: skip
    by_code = {r["code"]: r for r in s.model_dump(mode="json")["runs"]["items"]}
    assert by_code[live.code]["remote_url"] == url
    assert "remote_url" not in by_code[gone.code]  # its session is over
    _check_remote(to_remote(s), s)


def test_brain_tool_calls_are_on_both_tiers(db: sqlite3.Connection) -> None:
    """PER-258: the Claude brain's last tool calls (command, cwd, decision) and today's counts
    show on both tiers; only its Remote Control link stays local."""
    now = utcnow()
    url = "https://claude.ai/code/session_abc"
    kv_set(db, SESSION_KV, {"session_id": "s", "turns": 2, "pid": 1, "remote_url": url})
    for command, write, status in [("git log -1", False, "ok"), (f"git push {SECRET}", True, "ok"),
                                   ("rm -rf x", True, "error")]:  # fmt: skip
        audit(db, TOOL_ACTION, tool="Bash", decision="gate" if write else "allowed",
              command=command, cwd="/Users/x/code/r", status=status, write=write)  # fmt: skip
    s = build_snapshot(db, load_settings(), started_at=now, now=now, version="deadbeef")
    local = s.model_dump(mode="json")
    assert local["claude_brain"]["remote_url"] == url
    tools = local["claude_brain"]["tools"]
    assert [t["command"] for t in tools] == ["rm -rf x", f"git push {SECRET}", "git log -1"]
    assert tools[1]["decision"] == "gate" and tools[1]["write"] is True
    assert all(t["outside"] == ["command"] for t in tools)
    assert local["brain_activity"] == {
        "calls_today": 3, "writes_today": 1, "state": "up", "turns": 2
    }  # fmt: skip
    remote = to_remote(s)
    _check_remote(remote, s)
    rd = remote.model_dump(mode="json")
    assert rd["claude_brain"]["tools"] == tools and "remote_url" not in rd["claude_brain"]


def _approval(
    db: sqlite3.Connection,
    task_id: str,
    n: int,
    level: Level,
    at: datetime,
    *,
    tool: str = "save_issue",
    extension: str | None = "linear",
    summary: str | None = None,
) -> str:
    return ap.insert_approval(
        db,
        ap.NewApproval(
            task_id=task_id,
            intent_hash=f"h{n}",
            tool=tool,
            effect=Effect.WRITE,
            level=level,
            summary=summary or f"change {n} {SECRET}",
            approvers=("justin",),
            code=f"C{n}",
            expires_at=at + timedelta(minutes=30),
            extension=extension,
        ),
        at,
    )


def _waiting_task(db: sqlite3.Connection) -> str:
    """A task an approval can be answered on (``approvals.resolve`` wants it waiting)."""
    task = task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    task_store.set_status(db, task, "waiting", wait_reason="approval")
    return task


def test_a_batch_is_one_row(db: sqlite3.Connection) -> None:
    """PER-254: approving any approval of a batch approves all of it, so the dashboard shows
    the batch as one row: its first approval's id and code, its changes in order, its strictest
    level, one expiry. The code stays on the Mini."""
    now = utcnow().replace(microsecond=0)
    lone_task = task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    batch_task = task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    lone1 = _approval(db, lone_task, 1, Level.L3, now - timedelta(minutes=10))
    a = _approval(db, batch_task, 2, Level.L3, now - timedelta(minutes=5))
    b = _approval(db, batch_task, 3, Level.L1, now - timedelta(minutes=4))
    c = _approval(
        db, batch_task, 4, Level.L2, now - timedelta(minutes=3), tool="allow_tools", extension=None
    )
    _approval(db, lone_task, 5, Level.L2, now - timedelta(minutes=1))
    asked = now - timedelta(minutes=2)
    batch = ap.insert_batch(db, "justin", [a, b, c], asked)
    ap.set_expiry(db, batch.approval_ids, asked + timedelta(minutes=20))

    s = build_snapshot(db, load_settings(), started_at=now, now=now, version="0.1.0")
    d = s.model_dump(mode="json")
    assert d["approvals"]["open"] == 3
    items = d["approvals"]["items"]
    assert [i["code"] for i in items] == ["C1", "C2", "C5"]
    assert items[0]["id"] == lone1 and items[0]["member_ids"] == [lone1]
    assert items[0]["changes"] is None and items[2]["changes"] is None
    row = items[1]
    assert (row["id"], row["member_ids"]) == (a, [a, b, c])
    assert row["summary"] == "3 changes"
    assert row["level"] == "L1"  # the strictest change's
    assert row["needs_code"] is True  # a change widens trust, as the gate decides
    assert row["status"] == "pending" and row["decided_at"] is None
    assert datetime.fromisoformat(row["asked_at"]) == asked
    assert datetime.fromisoformat(row["expires_at"]) == asked + timedelta(minutes=20)
    assert row["changes"] == [
        {"summary": f"change 2 {SECRET}", "level": "L3"},
        {"summary": f"change 3 {SECRET}", "level": "L1"},
        {"summary": f"widens trust: change 4 {SECRET}", "level": "L2"},
    ]
    assert d["approvals"]["recent"] == []

    remote = to_remote(s)
    _check_remote(remote, s)
    ra = remote.model_dump(mode="json")["approvals"]
    assert '"code"' not in json.dumps(ra) and ra["items"][1]["summary"] == "3 changes"
    assert ra["items"][1]["changes"] == row["changes"]


def test_a_batch_row_expires_with_its_first_expiry(db: sqlite3.Connection) -> None:
    now = utcnow().replace(microsecond=0)
    task = task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    a = _approval(db, task, 1, Level.L3, now - timedelta(minutes=5))
    b = _approval(db, task, 2, Level.L2, now - timedelta(minutes=10))  # expires first
    ap.insert_batch(db, "justin", [a, b], now)
    s = build_snapshot(db, load_settings(), started_at=now, now=now, version="0.1.0")
    [row] = s.model_dump(mode="json")["approvals"]["items"]
    assert row["code"] == "C1"  # the batch's first approval's, though C2 is older
    assert row["level"] == "L2"
    assert datetime.fromisoformat(row["expires_at"]) == now + timedelta(minutes=20)


def test_a_split_batch_shows_what_is_left_as_lone_rows(db: sqlite3.Connection) -> None:
    """A batch with an approval decided on its own can't be answered as one: what is left
    of it shows as lone rows, never as a batch under a code that no longer binds."""
    now = utcnow().replace(microsecond=0)
    task = task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    a = _approval(db, task, 1, Level.L3, now - timedelta(minutes=5))
    b = _approval(db, task, 2, Level.L2, now - timedelta(minutes=4))
    c = _approval(db, task, 3, Level.L3, now - timedelta(minutes=3))
    ap.insert_batch(db, "justin", [a, b, c], now)
    ap.expire_one(db, a, now, reason="test")
    s = build_snapshot(db, load_settings(), started_at=now, now=now, version="0.1.0")
    d = s.model_dump(mode="json")
    assert d["approvals"]["open"] == 2
    assert [(i["code"], i["summary"]) for i in d["approvals"]["items"]] == [
        ("C2", f"change 2 {SECRET}"),
        ("C3", f"change 3 {SECRET}"),
    ]
    assert all(i["changes"] is None for i in d["approvals"]["items"])


def test_recently_decided_approvals(db: sqlite3.Connection) -> None:
    """The last week's decided approvals, newest decision first, with who and when; older
    ones and pending ones are not in ``recent``."""
    now = utcnow().replace(microsecond=0)
    task = _waiting_task(db)
    old = _approval(db, task, 1, Level.L2, now - timedelta(minutes=20))
    yes = _approval(db, task, 2, Level.L2, now - timedelta(minutes=19))
    no = _approval(db, task, 3, Level.L1, now - timedelta(minutes=18))
    gone = _approval(db, task, 4, Level.L2, now - timedelta(minutes=17))
    _approval(db, task, 5, Level.L2, now - timedelta(minutes=1))  # still pending
    by = "justin"
    assert (
        ap.resolve(db, yes, approved=True, decided_by=by, now=now - timedelta(minutes=10)) is None
    )
    assert ap.resolve(db, no, approved=False, decided_by=by, now=now - timedelta(minutes=5)) is None
    ap.expire_one(db, gone, now - timedelta(minutes=2), reason="test")
    db.execute(
        "UPDATE approvals SET status = 'expired', decided_at = ? WHERE id = ?",
        (iso(now - timedelta(days=8)), old),
    )
    s = build_snapshot(db, load_settings(), started_at=now, now=now, version="0.1.0")
    d = s.model_dump(mode="json")
    assert d["approvals"]["open"] == 1
    recent = d["approvals"]["recent"]
    assert [(r["id"], r["status"]) for r in recent] == [
        (gone, "expired"),
        (no, "rejected"),
        (yes, "approved"),
    ]
    assert recent[2]["decided_by"] == "justin" and recent[2]["decided_at"] is not None
    assert recent[0]["decided_by"] is None
    _check_remote(to_remote(s), s)


def test_untold_counts_what_the_owner_has_not_heard(db: sqlite3.Connection) -> None:
    """PER-263: ``pulse.untold`` counts every item whose latest version is untold, muted
    sources and owed last words included, and nothing already heard."""
    now = utcnow()
    _, told = pulse.upsert(db, "github", "ci/a", "told", "normal", None, False, 3600, now)
    pulse.mark_told(db, [told], now)
    pulse.upsert(db, "github", "ci/b", "new", "low", None, False, 3600, now)
    pulse.upsert(db, "linear", "x", "muted", "urgent", None, False, 3600, now)
    notify.mute(db, "linear", now + timedelta(hours=1), by="justin")
    pulse.upsert(db, "core", "gone", "cleared, owed", "normal", None, True, 3600, now)
    s = build_snapshot(db, load_settings(), started_at=now, now=now, version="deadbeef")
    assert (s.pulse.open, s.pulse.untold) == (3, 3)
    rp = to_remote(s).model_dump(mode="json")["pulse"]
    assert (rp["open"], rp["untold"]) == (3, 3)
    # Most urgent first, then newest; told items say when.
    assert [(i["source"], i["urgency"], i["state"]) for i in rp["items"]] == [
        ("linear", "urgent", "new"),
        ("github", "normal", "told"),
        ("github", "low", "new"),
    ]
    assert rp["items"][1]["told_at"] is not None and rp["items"][0]["told_at"] is None


def test_pulse_outside_provenance_and_run_links(db: sqlite3.Connection) -> None:
    """A pulse item is outside text unless it is a core notice; a worker's item links its run."""
    now = utcnow()
    pulse.upsert(db, "core", "run/r8", "R8 is running", "normal", None, False, 3600, now)
    pulse.upsert(db, "core", "notice/watch/w1/ended", "W1 ended", "normal", None, False, 3600, now)
    pulse.upsert(db, "github", "pr/1", "PR 1 needs review", "low", None, False, 3600, now)
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    by_key = {i.key: i for i in s.pulse.items}
    assert by_key["run/r8"].run_code == "R8" and by_key["run/r8"].outside == ["title"]
    assert by_key["notice/watch/w1/ended"].outside == [] and by_key["pr/1"].outside == ["title"]
    assert by_key["pr/1"].run_code is None


def test_empty_db(db: sqlite3.Connection) -> None:
    settings = load_settings()
    s = build_snapshot(db, settings, started_at=utcnow(), version="0.1.0")
    data = s.model_dump(mode="json")
    assert data["schema"] == 2 and data["tier"] == "local" and data["trimmed"] == "none"
    assert data["limits"] == {
        "runs": 50, "run_detail": 20, "tasks": 50, "task_detail": 20, "approvals_recent": 20,
        "approvals_pending": 20, "questions": 20, "pulse": 60, "brain_tools": 20,
    }  # fmt: skip
    assert data["autonomy"] == "on" and data["paused"] is False and data["dry_run"] is True
    assert [h["name"] for h in data["health"]] == ["model", "imessage", "loop", "store", "backup"]
    assert {h["state"] for h in data["health"]} == {"unknown"}
    assert all(h["reason"] is None for h in data["health"])
    assert data["tasks"] == {"by_state": {}, "items": []}
    assert data["slots"] == {"used": 0, "max": settings.claude.max_concurrent}
    assert data["runs"] == {"by_state": {}, "detected": {"by_kind": {}, "high": 0}, "items": []}
    assert data["approvals"] == {"open": 0, "items": [], "recent": []}
    assert data["questions"] == {"open": 0, "items": []}
    assert data["pulse"] == {"open": 0, "untold": 0, "items": []}
    assert data["extensions"] == [] and data["core_jobs"] == []
    assert data["runner"]["set_up"] is False and data["runner"]["missing"]
    assert data["runner"]["gaps"] == 1
    assert data["trust_rules"] == [] and data["repos"] == []
    assert data["trust_rule_count"] == 0 and data["repo_count"] == 0
    assert data["brain_activity"] == {
        "calls_today": 0, "writes_today": 0, "state": None, "turns": 0
    }  # fmt: skip
    assert data["claude_brain"] is None
    _check_remote(to_remote(s), s)


# --- the builder over a store -------------------------------------------------------------


def _busy(db: sqlite3.Connection, now: datetime) -> None:
    """A store with something in every section, with SECRET in every text."""
    kv_set(db, "autonomy", "off")
    kv_set(db, "paused", True)
    kv_set(
        db,
        "health.model",
        {"state": "degraded", "since": iso(now), "detail": f"probe failed {SECRET}"},
    )
    kv_set(db, "health.ext:github", {"state": "ok", "since": iso(now), "detail": None})
    task_id = task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    task_store.set_status(db, task_id, "waiting", wait_reason="approval", wake_at=now)

    r1 = runs.create(
        db, repo="dori", ask=f"build {SECRET}", principal="justin", session_id="s1", max_seconds=60
    )
    runs.update(
        db,
        r1.code,
        status="waiting",
        wait_reason="question",
        branch=f"resonant/{SECRET}",
        last_note=f"note {SECRET}",
    )
    r2 = runs.create(db, repo="", ask=f"scratch {SECRET}", principal="justin", session_id="s2",
                     max_seconds=60)  # fmt: skip
    runs.update(db, r2.code, status="ready", pr_url=f"https://x/{SECRET}", pr_number=7)
    questions.ask(db, r1.code, "ask", f"which db {SECRET}?", options=["a", SECRET], at=now)
    record_detected(db, r1.code, SideEffect("push", f"pushed to main {SECRET}", high=True))
    record_detected(db, r1.code, SideEffect("http", f"POST {SECRET}"))
    record_detected(db, r2.code, SideEffect("push", f"pushed to dev {SECRET}"))
    kv_set(db, SESSION_KV, {"session_id": "s", "turns": 2, "pid": 1})  # the brain's session

    ap.insert_approval(
        db,
        ap.NewApproval(
            task_id=task_id,
            intent_hash="h1",
            tool="restart_resonant",
            effect=Effect.WRITE,
            level=Level.L2,
            summary=f"restart {SECRET}",
            approvers=("justin",),
            code="K4",
            expires_at=now + timedelta(minutes=30),
        ),
        now,
    )
    pulse.upsert(db, "github", "ci/dori", f"CI red {SECRET}", "urgent", None, False, 3600, now)
    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name="github",
            source="repo:github",
            commit="abc1234",
            tree_digest="d",
            manifest_sha="m",
            manifest={
                "name": "github",
                "jobs": [
                    {"id": "ci-watch", "schedule": "*/5 * * * *"},
                    {"id": "morning", "schedule": "0 8 * * *", "tz": "UTC"},
                ],
            },
            installed_at=now,
            installed_by="justin",
        ),
    )
    ext_store.record_run(db, "github", "ci-watch", at=now, outcome="failed", ok=False)
    notify.mute(db, "github", now + timedelta(hours=1), by="justin")
    ext_store.record_run(db, "github", "morning", at=now, outcome=f"weird {SECRET}", ok=True)
    trust.add_rule(db, scope="dori", action="merge", effect="ask", created_from="chat", now=now)
    repos.add(db, "justinhe16/dori", base="main", source="test", name="dori", ci=True)


def test_busy_db(db: sqlite3.Connection) -> None:
    now = utcnow()
    _busy(db, now)
    pool = SlotPool(3, 1)
    assert pool.try_acquire("t1", oncall=False)
    s = build_snapshot(
        db,
        load_settings(),
        started_at=now - timedelta(hours=1),
        last_tick=now,
        slots=pool,
        runner_missing=[],
        now=now,
        version="deadbeef",
    )
    d = s.model_dump(mode="json")
    assert d["version"] == "deadbeef"
    assert d["autonomy"] == "off" and d["paused"] is True
    health = {h["name"]: h for h in d["health"]}
    assert health["model"]["state"] == "degraded"
    assert health["model"]["reason"] == f"probe failed {SECRET}"
    assert health["model"]["outside"] == []  # a code-built check
    assert d["tasks"]["by_state"] == {"waiting": 1, "queued": 1}
    assert {t["status"] for t in d["tasks"]["items"]} == {"queued", "waiting"}
    assert d["slots"] == {"used": 1, "max": 3}

    assert d["runs"]["by_state"] == {"waiting": 1, "ready": 1}
    by_code = {r["code"]: r for r in d["runs"]["items"]}
    assert by_code["R1"]["ask"] == f"build {SECRET}"
    assert by_code["R1"]["waiting_reason"] == "question"
    assert by_code["R1"]["last_note"] == f"note {SECRET}"
    assert by_code["R1"]["github_repo"] == "justinhe16/dori"
    assert by_code["R2"]["github_repo"] is None
    assert by_code["R2"]["repo"] == "" and by_code["R2"]["pr_number"] == 7
    assert by_code["R2"]["pr_url"] is None  # not a github.com pull request link
    assert d["runs"]["items"][0]["code"] == "R1"  # active first
    assert (by_code["R1"]["detected"], by_code["R2"]["detected"]) == (2, 1)
    assert [(e["kind"], e["high"]) for e in by_code["R1"]["effects"]] == [
        ("push", True),
        ("http", False),
    ]
    assert d["runs"]["detected"] == {"by_kind": {"push": 2, "http": 1}, "high": 1}
    assert by_code["R1"]["question_ids"] == [1] and by_code["R1"]["detail"] is True
    assert {"effects", "last_note"} <= set(by_code["R1"]["outside"])

    assert d["approvals"]["open"] == 1
    [a] = d["approvals"]["items"]
    assert (a["code"], a["summary"], a["level"], a["effect"], a["tool"]) == (
        "K4", f"restart {SECRET}", "L2", "write", "restart_resonant"
    )  # fmt: skip
    assert a["needs_code"] is False and a["status"] == "pending" and a["task_id"] is not None
    assert d["questions"]["open"] == 1
    [q] = d["questions"]["items"]
    assert (q["text"], q["options"], q["run_code"], q["kind"]) == (
        f"which db {SECRET}?", ["a", SECRET], "R1", "ask"
    )  # fmt: skip
    assert q["urgent"] is False and q["outside"] == ["text", "options"]
    assert d["pulse"]["open"] == 1 and d["pulse"]["untold"] == 1
    [item] = d["pulse"]["items"]
    assert (item["source"], item["key"], item["urgency"], item["state"]) == (
        "github",
        "ci/dori",
        "urgent",
        "new",
    )

    [ext] = d["extensions"]
    assert (ext["name"], ext["status"], ext["health"]) == ("github", "enabled", "ok")
    assert ext["source_kind"] == "repo" and ext["commit"] == "abc1234"
    muted = datetime.fromisoformat(ext["muted_until"]) - (now + timedelta(hours=1))
    assert abs(muted) < timedelta(seconds=1)  # stored in milliseconds
    jobs = {j["id"]: j for j in ext["jobs"]}
    assert jobs["ci-watch"]["last_outcome"] == "failed" and jobs["ci-watch"]["fail_streak"] == 1
    assert jobs["ci-watch"]["schedule"] == "*/5 * * * *" and jobs["morning"]["tz"] == "UTC"
    assert jobs["morning"]["last_outcome"] == "other"  # unknown outcomes never pass through
    assert datetime.fromisoformat(jobs["ci-watch"]["next_due"]) > now
    morning_due = datetime.fromisoformat(jobs["morning"]["next_due"])
    assert (morning_due.hour, morning_due.minute) == (8, 0)

    assert d["runner"] == {"set_up": True, "missing": [], "gaps": 0}
    assert d["trust_rules"] == [{"n": 1, "text": "ask before merge in dori"}]
    assert d["repos"] == [
        {"name": "dori", "mode": "push", "base": "main", "ci": True, "slug": "justinhe16/dori"}
    ]

    remote = to_remote(s)
    _check_remote(remote, s)
    rd = remote.model_dump(mode="json")
    # The same data on both tiers, texts included (PER-276): the page reads the same.
    assert rd["runs"]["items"][0]["ask"] == f"build {SECRET}"
    assert rd["approvals"]["items"][0]["summary"] == f"restart {SECRET}"
    assert '"code"' not in json.dumps(rd["approvals"])  # the approval's code stays on the Mini
    assert rd["claude_brain"] == d["claude_brain"]  # no link in this store
    assert (rd["trust_rule_count"], rd["repo_count"]) == (1, 1)


def test_rows_with_bad_identifiers_are_left_out(db: sqlite3.Connection) -> None:
    """A stale or hand-edited row never breaks the dashboard."""
    now = utcnow()
    kv_set(db, "health.ext:Bad Name", {"state": "degraded", "since": iso(now), "detail": "x"})
    kv_set(db, "health.ext:github", {"state": "ok", "since": iso(now), "detail": None})

    def ext(name: str, jobs: list[dict[str, Any]]) -> ext_store.ExtensionRecord:
        return ext_store.ExtensionRecord(
            name=name,
            source=f"repo:{name}",
            commit="abc",
            tree_digest="d",
            manifest_sha="m",
            manifest={"name": name, "jobs": jobs},
            installed_at=now,
            installed_by="justin",
        )

    ext_store.save_extension(db, ext("Bad.Ext", []))
    jobs = [{"id": "Bad.Id", "schedule": "0 8 * * *"}, {"id": "ok-job", "schedule": "0 8 * * *"}]
    ext_store.save_extension(db, ext("github", jobs))
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    assert [h.name for h in s.health][-1] == "ext:github"
    assert "ext:Bad Name" not in {h.name for h in s.health}
    assert [(e.name, [j.id for j in e.jobs]) for e in s.extensions] == [("github", ["ok-job"])]
    _check_remote(to_remote(s), s)


def test_runs_carry_their_detail_and_history(db: sqlite3.Connection) -> None:
    now = utcnow().replace(microsecond=0)
    task_id = task_store.insert_task(db, NewTask(kind="run", runner="claude", priority=1))
    repos.add(db, "me/dori", base="main", source="test", name="dori", ci=True)
    run = runs.create(db, repo="dori", ask="PER-12: fix it, see PER-13 and PER-12", principal="j",
                      session_id="s", max_seconds=60)  # fmt: skip
    runs.update(
        db, run.code, task_id=task_id, status="ready", branch="resonant/r1-per-14-fix",
        base_branch="main", head_sha="abcdef0123456789", sessions=2, attempts=1, step="reply",
        exit_code=0, cost_usd=1.25, tokens_in=1000, tokens_out=200, session_seconds=61.5,
        wait_seconds=3.0, files_changed=4, flagged_paths=["Makefile", "package.json"],
        summary="did the thing", failure=None, pr_number=9,
        pr_url="https://github.com/me/dori/pull/9",
    )  # fmt: skip
    record_did(db, run.code, "merged PR #9 with gh")
    set_merged(db, run.code, ["PR #9"])
    set_merged_by(db, run.code, MergedBy("resonant", "M6"))
    record_detected(db, run.code, SideEffect("merge", "merged PR #9", high=True))
    # The run's audit rows, and another run's, in order; ``run.`` is dropped from the names.
    audit(db, "run.start", task_id=task_id, run=run.code)
    audit(db, "run.side_effect", run=run.code, kind="merge")
    audit(db, "run.ready", run=run.code, pr=9)
    audit(db, "run.start", run="R99")
    audit(db, "something.else", run=run.code)
    s = build_snapshot(db, load_settings(), started_at=now, now=now, version="0.1.0")
    d = s.model_dump(mode="json")
    [r] = d["runs"]["items"]
    assert [e["event"] for e in r["history"]] == ["start", "side_effect", "ready"]
    assert r["history"][0]["at"] <= r["history"][-1]["at"]
    assert (r["head_sha"], r["base_branch"], r["step"]) == ("abcdef0123456789", "main", "reply")
    assert (r["sessions"], r["attempts"], r["exit_code"], r["files_changed"]) == (2, 1, 0, 4)
    assert (r["cost_usd"], r["tokens_in"], r["tokens_out"]) == (1.25, 1000, 200)
    assert (r["session_seconds"], r["wait_seconds"]) == (61.5, 3.0)
    assert r["flagged_paths"] == ["Makefile", "package.json"] and r["summary"] == "did the thing"
    assert r["did"] == ["merged PR #9 with gh"] and r["merged"] == ["PR #9"]
    assert r["merged_by"] == "resonant"
    assert r["effects"] == [{"kind": "merge", "what": "merged PR #9", "high": True}]
    assert r["pr_url"] == "https://github.com/me/dori/pull/9"
    assert r["github_repo"] == "me/dori" and r["task_id"] == task_id
    assert r["linear_ids"] == ["PER-12", "PER-13", "PER-14"]  # ask then branch, once each
    assert r["created_at"] is not None and r["detail"] is True
    assert {"summary", "did", "effects", "flagged_paths"} <= set(r["outside"])
    assert "ask" not in r["outside"]  # the owner's own ask, in an untainted task
    # The task points back at the run.
    [t] = d["tasks"]["items"]
    assert t["run_code"] == run.code
    assert [e["event"] for e in t["history"]] == ["run.start"]


def test_history_is_bounded_and_keeps_the_newest(db: sqlite3.Connection) -> None:
    now = utcnow()
    task_id = task_store.insert_task(db, NewTask(kind="run", runner="claude", priority=1))
    run = runs.create(db, repo="", ask="x", principal="j", session_id="s", max_seconds=60)
    runs.update(db, run.code, task_id=task_id)
    audit(db, "run.start", task_id=task_id, run=run.code)
    for i in range(30):
        audit(db, f"run.step{i}", run=run.code)
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    [r] = s.runs.items
    assert [e.event for e in r.history] == [f"step{i}" for i in range(18, 30)]


def test_linear_ids() -> None:
    linear_ids = snap._linear_ids  # pyright: ignore[reportPrivateUsage]
    assert linear_ids("fix PER-276 and PER-277, not per276 or AB-1234567", "x-per-278-y") == [
        "PER-276",
        "PER-277",
        "PER-278",  # its key is in the ask
    ]
    assert linear_ids("try gpt-4 and use GPT 4", "resonant/r3-try-gpt-4") == []
    assert linear_ids("fix the login", "justin/per-276-fix-login") == ["PER-276"]
    assert linear_ids("A-1 ZZZZZZZZZZZ-1 ok", None) == []
    assert len(linear_ids(" ".join(f"AB-{i}" for i in range(1, 12)))) == 5


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://github.com/me/dori/pull/9", True),
        ("https://github.com/me.x/do-ri_1/pull/12345", True),
        ("http://github.com/me/dori/pull/9", False),
        ("https://github.com/me/dori/issues/9", False),
        ("https://github.com/me/dori/pull/9?x=1", False),
        ("https://github.com.evil.example/me/dori/pull/9", False),
        ("https://evil.example/https://github.com/me/dori/pull/9", False),
        ("https://github.com/me/dori/pull/9/files", False),
        ("javascript:alert(1)", False),
    ],
)
def test_pr_url_is_an_allowlist(db: sqlite3.Connection, url: str, kept: bool) -> None:
    now = utcnow()
    run = runs.create(db, repo="", ask="x", principal="j", session_id="s", max_seconds=60)
    runs.update(db, run.code, pr_url=url, pr_number=9)
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    assert s.runs.items[0].pr_url == (url if kept else None)


def test_related_approvals_and_questions(db: sqlite3.Connection) -> None:
    now = utcnow().replace(microsecond=0)
    task_id = _waiting_task(db)
    run = runs.create(db, repo="", ask="x", principal="j", session_id="s", max_seconds=60)
    runs.update(db, run.code, task_id=task_id, status="waiting", wait_reason="question")
    pending = _approval(db, task_id, 1, Level.L2, now - timedelta(minutes=5))
    decided = _approval(db, task_id, 2, Level.L2, now - timedelta(minutes=4))
    assert ap.resolve(db, decided, approved=True, decided_by="justin", now=now) is None
    q = questions.ask(db, run.code, "ask", "which?", options=["a", "b"], at=now)
    other = runs.create(db, repo="", ask="y", principal="j", session_id="s2", max_seconds=60)
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    by_code = {r.code: r for r in s.runs.items}
    assert by_code[run.code].approval_ids == [pending, decided]
    assert by_code[run.code].question_ids == [q.id]
    assert by_code[other.code].approval_ids == [] and by_code[other.code].question_ids == []
    [t] = s.tasks.items
    assert t.approval_ids == [pending, decided] and t.run_code == run.code
    [pa] = s.approvals.items
    assert pa.run_code == run.code and pa.task_id == task_id


def test_taint_provenance(db: sqlite3.Connection) -> None:
    """Texts a tainted task chose are marked outside (as ``runners/approve.py`` and the system
    texts mark them); the owner's own, and a code-built row, are not."""
    now = utcnow().replace(microsecond=0)
    clean = task_store.insert_task(db, NewTask(kind="run", runner="claude", priority=1))
    dirty = task_store.insert_task(
        db, NewTask(kind="run", runner="claude", priority=1, checkpoint={"tainted": True})
    )
    batch_item = task_store.insert_task(
        db,
        NewTask(kind="run", runner="claude", priority=1, checkpoint={"batch": [{"tainted": True}]}),
    )
    codes: dict[str, str] = {}
    for name, tid in (("clean", clean), ("dirty", dirty), ("batch", batch_item)):
        run = runs.create(db, repo="", ask=name, principal="j", session_id=name, max_seconds=60)
        runs.update(db, run.code, task_id=tid)
        codes[name] = run.code
        _approval(db, tid, len(codes), Level.L2, now)
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    by_code = {r.code: r for r in s.runs.items}
    assert "ask" not in by_code[codes["clean"]].outside
    assert "ask" in by_code[codes["dirty"]].outside and "ask" in by_code[codes["batch"]].outside
    approvals_by_task = {a.task_id: a for a in s.approvals.items}
    assert approvals_by_task[clean].outside == []
    assert approvals_by_task[dirty].outside == ["summary", "changes"]
    assert approvals_by_task[batch_item].outside == ["summary", "changes"]


def test_tasks_never_ship_their_internals(db: sqlite3.Connection) -> None:
    now = utcnow()
    tid = task_store.insert_task(
        db,
        NewTask(
            kind="chat", runner="local", priority=1, extension="linear", principal="justin",
            checkpoint={"title": "plan the week", "claude_reply": "PRIVATE-REPLY"},
            scope={"repo": "PRIVATE-SCOPE"}, channel_ref={"chat": "PRIVATE-CHANNEL"},
        ),
    )  # fmt: skip
    db.execute(
        "UPDATE tasks SET claude_session_id = 'PRIVATE-SESSION', worktree_path = '/PRIVATE/wt',"
        " result = '{\"x\": \"PRIVATE-RESULT\"}', error = ?, status = 'failed', attempts = 2,"
        " step_no = 3 WHERE id = ?",
        (f"boom {SECRET}", tid),
    )
    audit(db, "approval.requested", task_id=tid)
    audit(db, "tool.started", task_id=tid, tool="x")
    job = task_store.insert_task(
        db,
        NewTask(kind="job", runner="job", priority=3, extension="github",
                checkpoint={"ext": "github", "job": "ci-watch", "due_at": iso(now)}),
    )  # fmt: skip
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    text = s.model_dump_json()
    for private in ("PRIVATE", "claude_reply", "channel_ref", "worktree", "trace_id"):
        assert private not in text
    by_id = {t.id: t for t in s.tasks.items}
    t = by_id[tid]
    assert (t.label, t.kind, t.runner, t.extension) == ("plan the week", "chat", "local", "linear")
    assert (t.status, t.attempts, t.step_no) == ("failed", 2, 3)
    assert t.error == f"boom {SECRET}" and t.outside == ["error"]
    assert [e.event for e in t.history] == ["approval.requested", "tool.started"]
    assert by_id[job].label == "github/ci-watch" and by_id[job].outside == []


def test_core_jobs(db: sqlite3.Connection) -> None:
    now = datetime(2026, 11, 2, 12, 30, tzinfo=UTC)
    ext_store.record_run(db, "core", "pulse", at=now, outcome="ok", ok=True)
    ext_store.record_run(db, "core", "backup", at=now, outcome="failed", ok=False)
    cores = [
        ("pulse", "0 * * * *", "UTC"),
        ("backup", "30 3 * * *", "UTC"),
        ("Bad.Id", "* * * * *", "UTC"),
    ]
    s = build_snapshot(db, load_settings(), started_at=now, now=now, core_jobs=cores)
    by_id = {j.id: j for j in s.core_jobs}
    assert set(by_id) == {"pulse", "backup"}
    assert by_id["pulse"].schedule == "0 * * * *" and by_id["pulse"].tz == "UTC"
    assert (by_id["pulse"].effect, by_id["pulse"].level) == ("read", "L0")
    assert by_id["pulse"].next_due == datetime(2026, 11, 2, 13, 0, tzinfo=UTC)
    assert by_id["pulse"].last_outcome == "ok" and by_id["backup"].fail_streak == 1
    assert by_id["backup"].next_due == datetime(2026, 11, 3, 3, 30, tzinfo=UTC)
    # Without the scheduler's list: the store's jobs, with no schedule.
    bare = build_snapshot(db, load_settings(), started_at=now, now=now)
    assert {(j.id, j.schedule, j.next_due) for j in bare.core_jobs} == {
        ("backup", None, None), ("pulse", None, None)
    }  # fmt: skip
    _check_remote(to_remote(s), s)


def test_extension_detail_never_ships_commands_pins_or_paths(db: sqlite3.Connection) -> None:
    now = utcnow().replace(microsecond=0)
    manifest: dict[str, Any] = {
        "name": "tools",
        "description": "Useful tools",
        "secrets": ["api_token", "other"],
        "repos": ["me/dori"],
        "report": {"max_urgency": "normal"},
        "jobs": [{"id": "daily", "schedule": "0 8 * * *", "run": "PRIVATE-JOB-CMD --x",
                  "effect": "write", "level": "L1"}],
        "tools": {
            "lookup": {"run": "PRIVATE-CMD {q}", "description": "Looks things up",
                       "effect": "read", "args": {"q": "string"}},
            "send": {"run": "PRIVATE-SEND", "effect": "pay", "secrets": ["api_token"]},
        },
        "mcp": {"servers": {
            "remote": {"transport": "http", "url": "https://mcp.example.com/PRIVATE/path?k=PRIVATE",
                       "auth": "bearer", "secret": "api_token",
                       "tools": [{"name": "search", "description": "Searches", "effect": "read"}]},
            "local": {"transport": "stdio", "command": "./PRIVATE-server.py", "args": ["--PRIVATE"],
                      "tools": [{"name": "edit", "effect": "write"}]},
        }},
    }  # fmt: skip
    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name="tools", source="path:/Users/PRIVATE/ext", commit="0123456789abcdef0123",
            tree_digest="d", manifest_sha="m", manifest=manifest, installed_at=now,
            installed_by="justin", argv0_paths={"x": "/PRIVATE/bin/x"},
            mcp_pins={"search": {"server": "remote", "pin": "PRIVATE-PIN", "input_schema": {}}},
        ),
    )  # fmt: skip
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    text = s.model_dump_json()
    assert "PRIVATE" not in text and "mcp.example.com/" not in text
    [e] = s.extensions
    assert (e.description, e.source_kind, e.commit) == ("Useful tools", "path", "0123456789ab")
    assert e.installed_at == now and e.secrets == ["api_token", "other"]
    assert e.repos == ["me/dori"] and e.report_max == "normal"
    tools = {t.name: t for t in e.tools}
    assert [(t.via, t.server, t.effect, t.level) for t in tools.values()] == [
        ("cli", None, "read", "L0"), ("cli", None, "pay", "L1"),
        ("mcp", "remote", "read", "L0"), ("mcp", "local", "write", "L2"),
    ]  # fmt: skip
    assert tools["lookup"].description == "Looks things up"
    assert [(m.id, m.transport, m.host) for m in e.mcp_servers] == [
        ("remote", "http", "mcp.example.com"), ("local", "stdio", None)
    ]  # fmt: skip
    [j] = e.jobs
    assert (j.effect, j.level, j.schedule) == ("write", "L1", "0 8 * * *")


def test_lists_keep_to_their_caps(db: sqlite3.Connection) -> None:
    now = utcnow()
    for i in range(60):
        runs.create(db, repo="", ask=f"a{i}", principal="j", session_id=f"s{i}", max_seconds=60)
        task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    for i in range(70):
        pulse.upsert(db, "core", f"k/{i}", f"t{i}", "normal", None, False, 3600, now)
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    assert len(s.runs.items) == snap.RUN_ITEMS and len(s.tasks.items) == snap.TASK_ITEMS
    assert len(s.pulse.items) == snap.PULSE_ITEMS and s.pulse.open == 70
    assert [r.detail for r in s.runs.items] == [True] * 20 + [False] * 30
    assert s.runs.items[0].code == "R60"  # the newest first
    assert all(not r.history and not r.did for r in s.runs.items[20:])
    assert s.limits.runs == snap.RUN_ITEMS and s.limits.pulse == snap.PULSE_ITEMS


def test_to_remote_validates(db: sqlite3.Connection) -> None:
    """``to_remote`` re-validates, so a snapshot built with ``model_copy`` past the checks
    can't be pushed."""
    s = build_snapshot(db, load_settings(), started_at=utcnow())
    bad = s.model_copy(update={"version": "has spaces"})
    with pytest.raises(ValidationError):
        to_remote(bad)


def test_code_version_is_a_sha_or_the_package_version() -> None:
    v = snap.code_version()
    assert re.fullmatch(r"[0-9a-f]{8}", v) or v == __version__


# --- one redaction and truncation pass --------------------------------------------------------


def _unicode(rng: random.Random) -> str:
    def char() -> str:
        kind = rng.random()
        if kind < 0.6:
            return chr(rng.randrange(0x20, 0x7F))
        if kind < 0.8:
            return chr(rng.randrange(0xA0, 0xD800))
        if kind < 0.9:
            return chr(rng.randrange(0xE000, 0xFFFE))
        if kind < 0.97:
            return chr(rng.randrange(0x1F300, 0x1FAFF))
        return "\n"

    return "".join(char() for _ in range(rng.choice([0, 1, 50, 299, 300, 301, 900, 5000])))


def _seed_texts(db: sqlite3.Connection, now: datetime, text: Any) -> None:
    """Every free-text source of the snapshot, filled by ``text()``."""
    kv_set(db, "health.model", {"state": "degraded", "since": iso(now), "detail": text()})
    kv_set(db, "health.ext:github", {"state": "failing", "since": iso(now), "detail": text()})
    kv_set(db, "health.relay", {"state": "failing", "since": iso(now), "detail": text()})
    task_id = task_store.insert_task(
        db, NewTask(kind="run", runner="claude", priority=1, checkpoint={"title": text()})
    )
    db.execute("UPDATE tasks SET error = ?, status = 'failed' WHERE id = ?", (text(), task_id))
    run = runs.create(db, repo="dori", ask=text(), principal="j", session_id="s", max_seconds=60)
    runs.update(
        db, run.code, task_id=task_id, branch=text(), last_note=text(), summary=text(),
        failure=text(), flagged_paths=[text() for _ in range(8)], status="running",
    )  # fmt: skip
    for _ in range(12):
        record_did(db, run.code, text())
        record_detected(db, run.code, SideEffect("http", text()))
    set_merged(db, run.code, [text() for _ in range(7)])
    questions.ask(db, run.code, "ask", text(), options=[text() for _ in range(9)], at=now)
    _approval(db, task_id, 1, Level.L2, now, summary=text() or "x")
    a = _approval(db, task_id, 2, Level.L2, now, summary=text() or "x")
    b = _approval(db, task_id, 3, Level.L1, now, summary=text() or "x")
    ap.insert_batch(db, "justin", [a, b], now)
    pulse.upsert(db, "core", "k/1", text().strip() or "t", "normal", None, False, 3600, now)
    pulse.upsert(db, "github", "k/2", text().strip() or "t", "low", None, False, 3600, now)
    audit(db, TOOL_ACTION, tool=text(), decision=text(), command=text(), cwd=text(),
          status="ok", write=True)  # fmt: skip
    # The approval's decided_by, tool and extension; a pulse item's source and key; a repo's
    # name, base and slug: stored past their validators, as a hand-edited row would be.
    db.execute(
        "UPDATE approvals SET status = 'approved', decided_at = ?, decided_by = ?, tool = ?,"
        " extension = ? WHERE id = ?",
        (iso(now), text(), text(), text(), a),
    )
    db.execute(
        "INSERT INTO pulse_items (source, key, title, urgency, fingerprint, cleared, updated_at,"
        " expires_at) VALUES (?, ?, 't', 'low', 'f', 0, ?, ?)",
        (text(), text() or "k", iso(now), iso(now + timedelta(hours=1))),
    )
    repos.add(db, "me/dori", base="main", source="test", name="dori")
    db.execute(
        "UPDATE repos SET name = ?, base = ?, slug = ? WHERE name = 'dori'",
        (text() or "n", text(), text() or "s"),
    )
    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name="github", source="repo:github", commit="abc1234", tree_digest="d",
            manifest_sha="m", installed_at=now, installed_by="justin",
            manifest={"name": "github", "description": text(), "repos": [text()],
                      "jobs": [{"id": "j", "schedule": text(), "tz": text(),
                                "description": text()}],
                      "launch_agents": [{"label": "com.github.watcher", "description": text()}],
                      "tools": {"t": {"effect": "read", "description": text()}},
                      "mcp": {"servers": {"m": {"transport": "stdio", "tools": [
                          {"name": "u", "effect": "read", "description": text()}]}}},
                      "panels": [{"id": "p", "title": text(), "description": text(),
                                  "refresh": {"idle": text(), "active": text()}, "tz": text()}]},
        ),
    )  # fmt: skip
    # PER-280: a panel's stored copy (written as the store holds it, not through normalize, so
    # the snapshot's own text pass is what is tested), and its last error.
    views = [{"id": "v", "title": "V", "widgets": [
        {"type": "text", "text": text()[:800]},
        {"type": "details", "rows": [{"depth": 0, "key": "k", "kind": "string",
                                      "value": text()[:200]}]},
    ]}]  # fmt: skip
    ext_store.save_panel_ok(
        db, "github", "p", data=json.dumps({"views": views, "health": "ok"}), updated_at=now,
        ran_at=now, bytes=10, expect_by=now + timedelta(hours=1),
    )  # fmt: skip
    ext_store.record_panel_failure(db, "github", "p", ran_at=now, error=text())
    db.execute(
        "INSERT INTO trust_rules (scope, action, effect, created_from, created_at)"
        " VALUES (?, 'merge', 'ask', 'chat', ?)",
        (text() or "x", iso(now)),
    )


@pytest.mark.parametrize("seed", range(25))
def test_every_text_is_capped_over_random_unicode(db: sqlite3.Connection, seed: int) -> None:
    """Random 0-5000 character texts through every source: building never exceeds a cap
    (the models validate them), nothing is over 800, and ``to_remote`` changes nothing else."""
    rng = random.Random(seed)
    now = utcnow()
    _seed_texts(db, now, lambda: _unicode(rng))
    s = build_snapshot(db, load_settings(), started_at=now, now=now,
                       core_jobs=[("pulse", _unicode(rng), _unicode(rng))],
                       runner_missing=[_unicode(rng) for _ in range(12)])  # fmt: skip
    data = s.model_dump(mode="json")

    def walk(node: Any) -> None:
        if isinstance(node, str):
            assert len(node) <= _TEXT_CAP, node[:40]
        elif isinstance(node, dict):
            for v in cast(dict[str, Any], node).values():
                walk(v)
        elif isinstance(node, list):
            for v in cast(list[Any], node):
                walk(v)

    walk(data)
    assert data["runner"]["gaps"] == 12 and len(data["runner"]["missing"]) == 10
    _check_remote(to_remote(s), s)


def test_text_is_redacted_then_cut(db: sqlite3.Connection) -> None:
    now = utcnow()
    token = "9f2c" * 16
    long = "x" * 400
    runs.create(db, repo="", ask=f"use ghp_{'A1b2' * 8} and {token} then {long}", principal="j",
                session_id="s", max_seconds=60)  # fmt: skip
    s = build_snapshot(db, load_settings(), started_at=now, now=now, redactor=Redactor([token]))
    [r] = s.runs.items
    assert len(r.ask) == 300 and r.ask.endswith(CUT_MARK)
    assert r.ask.startswith("use [redacted] and [redacted] then x")
    assert token not in r.ask and "ghp_" not in r.ask


# The raw secret parts, per family: what must never reach the relay.
SECRET_SAMPLES: dict[str, tuple[str, str]] = {
    "ghp": ("ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"),
    "github_pat": (
        "github_pat_" + "11ABCDEFG0abcdefghij_klmnopqrstuvwxyz0123456789",
        "klmnopqrstuvwxyz0123456789",
    ),
    "sk-ant": (
        "sk-ant-" + "api03-Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp",
        "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp",
    ),
    "sk": ("sk-" + "proj1234567890abcdefghijklmnop", "proj1234567890abcdefghijklmnop"),
    "xoxb": ("xoxb-" + "1234567890-abcdefghijklmnop", "1234567890-abcdefghijklmnop"),
    "aws": ("AKIA" + "IOSFODNN7EXAMPLE", "AKIA" + "IOSFODNN7EXAMPLE"),
    "bearer": ("Bearer abcdefghijklmnop1234567890", "abcdefghijklmnop1234567890"),
    "password": ("password=hunter2hunter2", "hunter2hunter2"),
    "jwt": (
        "eyJhbGciOiJIUzI1NiJ9"
        + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        + ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
    ),
    "pem": (
        "-----BEGIN RSA "
        + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA "
        + "PRIVATE KEY-----",
        "MIIEowIBAAKCAQEA",
    ),
}
FAKE_TOKEN = "9f2c1e7ab3d4" * 5 + "9f2c"  # a fake 64-hex Keychain token  gitleaks:allow


def seed_secret(db: sqlite3.Connection, now: datetime, secret: str, token: str) -> None:
    """``secret`` and the relay token in every free-text source the snapshot has."""
    _seed_texts(db, now, lambda: f"leak {secret} and {token} end")


@pytest.mark.parametrize("family", sorted(SECRET_SAMPLES))
def test_no_secret_reaches_either_tier(db: sqlite3.Connection, family: str) -> None:
    secret, raw = SECRET_SAMPLES[family]
    now = utcnow()
    seed_secret(db, now, secret, FAKE_TOKEN)
    leak = f"leak {secret} {FAKE_TOKEN}"
    s = build_snapshot(
        db, load_settings(), started_at=now, now=now, redactor=Redactor([FAKE_TOKEN]),
        runner_missing=[leak], core_jobs=[("pulse", leak, leak)],
    )  # fmt: skip
    for tier in (s, to_remote(s)):
        text = json.dumps(tier.model_dump(mode="json"), ensure_ascii=False)
        assert raw not in text and FAKE_TOKEN not in text
        assert "leak" in text  # the texts are there, cleaned
        assert redact_secrets(text) == text  # nothing credential-shaped is left


RC = "https://claude.ai/code/session_" + "AbC123xyz"


def test_remote_control_links_in_text_never_ship(db: sqlite3.Connection) -> None:
    """A worker or the brain may echo a Remote Control link into any text: it is scrubbed from
    both tiers (the link fields themselves stay on the local tier only)."""
    now = utcnow()
    kv_set(db, SESSION_KV, {"session_id": "s", "turns": 1, "pid": 1, "remote_url": RC})
    _seed_texts(db, now, lambda: f"open {RC} now")
    odd = "https://claude.ai/code/odd" + "-link"  # held as a value, whatever its shape
    run = runs.create(db, repo="", ask=f"x {odd}", principal="j", session_id="s2", max_seconds=60)
    runs.update(db, run.code, status="running", remote_url=odd)
    s = build_snapshot(db, load_settings(), started_at=now, now=now, runner_missing=[RC])
    local = _local_view(s.model_dump(mode="json"))
    assert "open [remote control link] now" in json.dumps(local)
    for dump in (local, to_remote(s).model_dump(mode="json")):
        text = json.dumps(dump)
        assert "claude.ai/code" not in text and "AbC123xyz" not in text and "odd-link" not in text


def test_remote_dumps_have_no_link_key_and_no_approval_code(db: sqlite3.Connection) -> None:
    now = utcnow()
    kv_set(db, SESSION_KV, {"session_id": "s", "turns": 1, "pid": 1, "remote_url": RC})
    _seed_texts(db, now, lambda: "x")
    run = runs.create(db, repo="", ask="y", principal="j", session_id="s2", max_seconds=60)
    runs.update(db, run.code, status="running", remote_url=RC)
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    local = s.model_dump(mode="json")
    assert any(r.get("remote_url") for r in local["runs"]["items"])
    assert local["claude_brain"]["remote_url"] == RC
    assert any(a.get("code") for a in local["approvals"]["items"])
    remote = to_remote(s).model_dump(mode="json")
    assert '"remote_url"' not in json.dumps(remote)
    for key in ("items", "recent"):
        assert all(a.get("code") is None and "code" not in a for a in remote["approvals"][key])


# --- sizes ---------------------------------------------------------------------------------


def seed_typical(db: sqlite3.Connection, now: datetime) -> None:
    """A realistic store: ~40 runs and ~30 tasks with history, 5 extensions, a few approvals,
    questions and pulse items."""
    ask = "Please look into why the nightly build is failing on the dori repo and fix it"
    summary = "Fixed the flaky test by pinning the clock; added a regression test. " * 5
    tasks: list[str] = []
    for i in range(30):
        tid = task_store.insert_task(
            db,
            NewTask(kind="run" if i % 2 else "chat", runner="claude", priority=1,
                    checkpoint={"title": f"task {i}: look at the build"}),
        )  # fmt: skip
        audit(db, "run.start" if i % 2 else "task.step", task_id=tid, run=f"R{i + 1}")
        for _ in range(4):
            audit(db, "tool.started", task_id=tid)
        task_store.set_status(db, tid, "done")
        tasks.append(tid)
    for i in range(40):
        run = runs.create(
            db, repo="dori", ask=ask, principal="j", session_id=f"s{i}", max_seconds=60
        )
        branch = f"resonant/r{i}-fix-the-nightly-build"
        waiting = i < 3
        runs.update(
            db, run.code, task_id=tasks[i % 30], branch=branch, base_branch="main",
            status="waiting" if waiting else "failed" if i % 5 == 0 else "ready",
            wait_reason="question" if waiting else None,
            head_sha="a" * 40, sessions=1, attempts=1, cost_usd=1.5, tokens_in=90000,
            tokens_out=4000, session_seconds=300.0, files_changed=3, summary=summary,
            flagged_paths=["Makefile"], pr_number=i + 1, step="reply",
            pr_url=f"https://github.com/justinhe16/dori/pull/{i + 1}",
            failure="CI failed on the lint job after the second attempt" if i % 5 == 0 else None,
        )  # fmt: skip
        for what in ("merged PR with gh", "pushed to a branch", "ran gh api"):
            record_did(db, run.code, what)
        record_detected(db, run.code, SideEffect("push", f"git push origin {branch}"))
        for ev in ("start", "side_effect", "ready", "done"):
            audit(db, f"run.{ev}", run=run.code, task_id=tasks[i % 30] if ev == "start" else None)
    repos.add(db, "justinhe16/dori", base="main", source="test", name="dori", ci=True)
    for i in range(5):
        ext_store.save_extension(
            db,
            ext_store.ExtensionRecord(
                name=f"ext{i}", source="repo:ext", commit="abcdef0123456789", tree_digest="d",
                manifest_sha="m", installed_at=now, installed_by="justin",
                manifest={
                    "name": f"ext{i}", "description": "Does useful things for the owner",
                    "secrets": ["token"], "report": {"max_urgency": "normal"},
                    "jobs": [{"id": "watch", "schedule": "*/10 * * * *", "run": "x"}],
                    "tools": {
                        f"tool_{j}": {"run": "x", "effect": "read", "description": "A tool " * 8}
                        for j in range(8)
                    },
                },
            ),
        )  # fmt: skip
    for i in range(3):
        tid = task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
        _approval(db, tid, 10 + i, Level.L2, now, summary="Merge PR 12 on dori into main")
        questions.ask(db, f"R{i + 1}", "ask", "Which database should the migration use?",
                      options=["postgres", "sqlite"], at=now)  # fmt: skip
        pulse.upsert(db, "github", f"pr/{i}", "PR #12 on dori needs review", "normal", None, False,
                     3600, now)  # fmt: skip
    for _ in range(10):
        audit(db, TOOL_ACTION, tool="Bash", decision="allowed", command="git status --short",
              cwd="/Users/justin/code/dori", status="ok", write=False)  # fmt: skip
    kv_set(db, SESSION_KV, {"session_id": "s", "turns": 12, "pid": 1})


def seed_worst(db: sqlite3.Connection, now: datetime) -> None:
    """Every list full at every cap, with every text at its cap."""

    def x(n: int) -> str:
        return "x" * n

    tasks: list[str] = []
    for _ in range(60):
        tid = task_store.insert_task(
            db, NewTask(kind="run", runner="claude", priority=1, checkpoint={"title": x(300)})
        )
        db.execute("UPDATE tasks SET error = ?, status = 'failed' WHERE id = ?", (x(500), tid))
        for _ in range(12):
            audit(db, "approval.requested", task_id=tid)
        tasks.append(tid)
    for i in range(70):
        run = runs.create(db, repo=x(100), ask=x(400), principal="j", session_id=f"s{i}",
                          max_seconds=60)  # fmt: skip
        runs.update(
            db, run.code, task_id=tasks[i % 60], branch=x(150), base_branch=x(150),
            status="waiting" if i < 25 else "ready", wait_reason="question" if i < 25 else None,
            head_sha="a" * 40, summary=x(1200), failure=x(600), last_note=x(300),
            flagged_paths=[x(200)] * 8, pr_number=i, cost_usd=12.34, tokens_in=10**7,
            tokens_out=10**6, session_seconds=1e5, wait_seconds=1e5, files_changed=999,
            pr_url="https://github.com/justinhe16/dori/pull/99999", step="reply", sessions=9,
            attempts=9, exit_code=1,
        )  # fmt: skip
        for j in range(14):
            record_did(db, run.code, x(100))
            record_detected(db, run.code, SideEffect("http", f"{j:02d}" + x(150)))
        set_merged(db, run.code, [x(60)] * 7)
        set_merged_by(db, run.code, MergedBy("run", "M6"))
        for ev in range(14):
            audit(db, f"run.{'e' * 30}{ev}", run=run.code,
                  task_id=tasks[i % 60] if ev == 0 else None)  # fmt: skip
        if i < 25:
            questions.ask(db, run.code, "ask", x(900), options=[x(120)] * 8, at=now)
    for i in range(45):
        tid = tasks[i]
        a = _approval(db, tid, 100 + i, Level.L2, now, summary=x(500))
        b = _approval(db, tid, 200 + i, Level.L1, now, summary=x(500))
        if i < 25:
            ap.insert_batch(db, "justin", [a, b], now)
        else:
            task_store.set_status(db, tid, "waiting", wait_reason="approval")
            assert ap.resolve(db, a, approved=True, decided_by=x(80), now=now) is None
            assert ap.resolve(db, b, approved=False, decided_by=x(80), now=now) is None
    for i in range(70):
        pulse.upsert(db, x(80), f"k/{i}" + x(100), x(300), "urgent", None, False, 3600, now)
    for i in range(25):
        name = f"x{i:02d}".ljust(60, "x")
        kv_set(db, f"health.ext:{name}", {"state": "degraded", "since": iso(now), "detail": x(300)})
        ext_store.save_extension(
            db,
            ext_store.ExtensionRecord(
                name=name, source="repo:" + x(50), commit="a" * 40, tree_digest="d",
                manifest_sha="m", installed_at=now, installed_by="justin",
                manifest={
                    "name": name, "description": x(200), "secrets": [f"s{k}" for k in range(12)],
                    "repos": [x(150)] * 25, "report": {"max_urgency": "urgent"},
                    "jobs": [{"id": f"j{k}".ljust(64, "x"), "schedule": "*/5 * * * *",
                              "tz": x(80), "run": "x", "description": x(200)} for k in range(5)],
                    "launch_agents": [{"label": f"com.{name}.a{k}", "description": x(200)}
                                      for k in range(10)],
                    "tools": {f"t{k}".ljust(80, "x"): {"effect": "write", "description": x(300)}
                              for k in range(50)},
                    "mcp": {"servers": {f"m{k}".ljust(40, "x"): {
                        "transport": "http", "url": "https://" + x(100) + ".example.com/p",
                        "tools": [{"name": f"u{k}", "effect": "pay", "description": x(300)}],
                    } for k in range(7)}},
                },
            ),
        )  # fmt: skip
    for k in range(60):
        db.execute(
            "INSERT INTO trust_rules (scope, action, effect, created_from, created_at)"
            " VALUES (?, 'merge', 'ask', 'chat', ?)",
            (x(300), iso(now)),
        )
        repos.add(db, f"owner{k}/{x(80)}", base=x(200), source="test", name=f"r{k}")
    kv_set(db, SESSION_KV, {"session_id": "s", "turns": 999, "pid": 1})
    for _ in range(30):
        audit(db, TOOL_ACTION, tool=x(80), decision="granted", command=x(400), cwd=x(400),
              status="error", write=True)  # fmt: skip
    # PER-280: an extension (sorted first, so the 20-extension cap keeps it) with 8 full panels,
    # about 190 KB: over ``PANEL_BUDGET_BYTES``, so the builder drops the biggest ones.
    ids = tuple(f"p{k}" for k in range(8))
    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name="aa-panels", source="repo:aa", commit="a" * 40, tree_digest="d",
            manifest_sha="m", installed_at=now, installed_by="justin",
            manifest=panel_manifest("aa-panels", ids),
        ),
    )  # fmt: skip
    for k, pid in enumerate(ids):
        store_panel(db, "aa-panels", pid, raw_panel(rows=50 - k, items=50, details=100), now)


def remote_dump(db: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    s = build_snapshot(db, load_settings(), started_at=now, now=now, version="deadbeef",
                       runner_missing=["gap " + "x" * 200] * 12)  # fmt: skip
    return to_remote(s).model_dump(mode="json")


def test_a_typical_store_fits_without_trimming(db: sqlite3.Connection) -> None:
    """~40 runs, ~33 tasks, 5 extensions and a few approvals, questions and pulse items fit
    the relay's cap as built: ``shrink`` is not needed, and ``trimmed`` stays ``none``."""
    now = utcnow()
    seed_typical(db, now)
    data = remote_dump(db, now)
    assert len(data["runs"]["items"]) == 40 and len(data["tasks"]["items"]) == 33
    assert len(data["questions"]["items"]) == 3 and len(data["extensions"]) == 5
    assert data["trimmed"] == "none"
    size = data_size(data)
    # Half the cap for the lists; the diagram (PER-277, which used up this headroom) is extra
    # and the first thing the pusher drops. PER-279's task titles add about 1.2 KB here.
    lists = data_size({k: v for k, v in data.items() if k != "diagram"})
    assert lists < pusher_mod.MAX_BYTES // 2, lists
    assert size < pusher_mod.MAX_BYTES * 3 // 4, size
    assert pusher_mod.shrink(data) is data  # fits: untouched


def test_the_worst_case_shrinks_to_fit(db: sqlite3.Connection) -> None:
    """Every list full at every cap with the longest texts is over the relay's cap as built
    (that is what ``shrink`` is for), and fits after it, as a valid snapshot."""
    now = utcnow()
    seed_worst(db, now)
    data = remote_dump(db, now)
    assert len(data["runs"]["items"]) == snap.RUN_ITEMS
    assert len(data["tasks"]["items"]) == snap.TASK_ITEMS
    assert len(data["approvals"]["items"]) == snap.APPROVALS_PENDING
    assert len(data["approvals"]["recent"]) == snap.APPROVALS_RECENT
    assert len(data["questions"]["items"]) == snap.QUESTION_ITEMS
    assert len(data["pulse"]["items"]) == snap.PULSE_ITEMS
    assert len(data["claude_brain"]["tools"]) == snap.BRAIN_TOOL_ITEMS
    assert len(data["extensions"]) == snap.EXTENSION_ITEMS
    assert len(data["trust_rules"]) == snap.TRUST_RULE_ITEMS
    assert len(data["repos"]) == snap.REPO_ITEMS
    size = data_size(data)
    assert size > pusher_mod.MAX_BYTES  # else the caps would not need the shrink
    out = pusher_mod.shrink(data)
    assert data_size(out) <= pusher_mod.MAX_BYTES
    Snapshot.model_validate(out)


UI_FIXTURES = Path(__file__).resolve().parents[1] / "dashboard" / "src" / "test" / "fixtures"


@pytest.mark.parametrize("name", ["empty", "local", "remote"])
def test_the_ui_fixtures_match_the_schema(name: str) -> None:
    """DB-2's fixtures are real snapshots, so the UI is tested on the shape the daemon sends."""
    data = json.loads((UI_FIXTURES / f"{name}.json").read_text())
    parsed = Snapshot.model_validate(data)  # extra="forbid": nothing extra, nothing renamed
    assert parsed.tier == ("remote" if name == "remote" else "local")
    if parsed.tier == "remote":
        _check_remote(parsed)


def test_the_remote_fixture_is_the_local_one_without_its_links_and_codes() -> None:
    local = Snapshot.model_validate(json.loads((UI_FIXTURES / "local.json").read_text()))
    remote = json.loads((UI_FIXTURES / "remote.json").read_text())
    assert to_remote(local).model_dump(mode="json") == remote


def test_the_v1_remote_fixture_is_schema_one() -> None:
    """A snapshot an old daemon pushed: the page still renders it (legacy adapter)."""
    data = json.loads((UI_FIXTURES / "remote-v1.json").read_text())
    assert data["schema"] == 1 and data["tier"] == "remote"


# --- extension panels (PER-280) -------------------------------------------------------------


def _panel_ext(
    db: sqlite3.Connection,
    now: datetime,
    name: str = "ledger",
    ids: tuple[str, ...] = ("paper", "oncall"),
    status: str = "enabled",
) -> None:
    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name=name, source=f"repo:{name}", commit="a" * 40, tree_digest="d", manifest_sha="m",
            installed_at=now, installed_by="justin", manifest=panel_manifest(name, ids),
            status=cast(Any, status),
        ),
    )  # fmt: skip


def _panels(db: sqlite3.Connection, now: datetime, name: str = "ledger") -> dict[str, Any]:
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    (ext,) = [e for e in s.model_dump(mode="json")["extensions"] if e["name"] == name]
    return {p["id"]: p for p in ext["panels"]}


def test_a_panel_shows_its_copy_state_and_marks_its_words_as_outside(
    db: sqlite3.Connection,
) -> None:
    now = utcnow()
    _panel_ext(db, now, ids=("paper", "oncall", "never"))
    got = store_panel(db, "ledger", "paper", raw_panel(), now - timedelta(minutes=2))
    ext_store.save_panel_ok(
        db, "ledger", "oncall", data=json.dumps({"views": [], "truncated": True}),
        updated_at=now, ran_at=now, bytes=2, expect_by=now + timedelta(minutes=20),
    )  # fmt: skip
    ext_store.record_panel_failure(db, "ledger", "oncall", ran_at=now, error="exit 1")
    panels = _panels(db, now)
    paper = panels["paper"]
    assert paper["title"] == "Panel paper" and paper["description"] == "The paper panel"
    assert paper["schedule"] == "*/15 * * * *" and paper["active"] == "*/1 9-16 * * 1-5"
    assert paper["tz"] == "America/New_York" and paper["ok"] is True and paper["stale"] is False
    assert paper["fail_streak"] == 0 and paper["bytes"] == got.bytes and paper["health"] == "ok"
    assert paper["local_url"] == "http://127.0.0.1:8765/ledger" and paper["truncated"] is True
    assert paper["updated_at"] is not None and paper["last_run_at"] is not None
    assert [v["id"] for v in paper["views"]] == ["overview", "system"]
    assert paper["outside"] == ["views"] and paper["error"] is None
    stat = paper["views"][0]["widgets"][0]
    assert stat == {
        "type": "stat", "label": "Equity", "value": 10432.5, "format": "money", "delta": 12.5,
        "health": "ok",
    }  # fmt: skip
    oncall = panels["oncall"]
    assert oncall["ok"] is False and oncall["stale"] is True and oncall["fail_streak"] == 1
    assert oncall["error"] == "exit 1" and oncall["outside"] == ["error"] and oncall["views"] == []
    never = panels["never"]
    assert never["updated_at"] is None and never["last_run_at"] is None
    assert never["stale"] is True and never["ok"] is False and never["bytes"] == 0
    assert never["views"] == [] and never["outside"] == []
    assert list(panels) == ["paper", "oncall", "never"]  # manifest order


def test_the_stale_rule(db: sqlite3.Connection) -> None:
    now = utcnow()
    _panel_ext(db, now, ids=("a", "b", "c", "d"))
    soon, past = now + timedelta(minutes=5), now - timedelta(seconds=1)
    for pid, expect in (("a", soon), ("b", soon), ("c", past), ("d", None)):
        ext_store.save_panel_ok(
            db, "ledger", pid, data='{"views": []}', updated_at=now, ran_at=now, bytes=2,
            expect_by=expect or soon,
        )  # fmt: skip
    db.execute("UPDATE ext_panels SET expect_by = NULL WHERE panel_id = 'd'")
    ext_store.record_panel_failure(db, "ledger", "b", ran_at=now, error="x")
    p = _panels(db, now)
    assert [p[i]["stale"] for i in "abcd"] == [False, True, True, False]
    # an extension that is not enabled has nothing fresh
    ext_store.set_status(db, "ledger", "disabled")
    assert all(x["stale"] for x in _panels(db, now).values())


def test_an_unreadable_stored_copy_shows_no_views_and_says_so(db: sqlite3.Connection) -> None:
    now = utcnow()
    _panel_ext(db, now, ids=("a", "b"))
    bad_view = {"id": "v", "title": "x" * 50, "widgets": [{"type": "text", "text": "t"}]}
    for pid, data in (("a", json.dumps({"views": [bad_view]})), ("b", "not json{")):
        ext_store.save_panel_ok(
            db, "ledger", pid, data=data, updated_at=now, ran_at=now, bytes=9,
            expect_by=now + timedelta(hours=1),
        )  # fmt: skip
    for panel in _panels(db, now).values():
        assert panel["views"] == [] and panel["error"] == "stored copy unreadable"
        assert panel["stale"] is True and panel["outside"] == ["error"] and panel["bytes"] == 0


def test_local_url_is_local_tier_only(db: sqlite3.Connection) -> None:
    now = utcnow()
    _panel_ext(db, now)
    store_panel(db, "ledger", "paper", raw_panel(), now)
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    local = s.model_dump(mode="json")
    assert _panels(db, now)["paper"]["local_url"] == "http://127.0.0.1:8765/ledger"
    assert '"local_url"' in json.dumps(local)
    remote = to_remote(s).model_dump(mode="json")
    assert '"local_url"' not in json.dumps(remote) and "127.0.0.1:8765" not in json.dumps(remote)
    # a stored url that is not loopback http is dropped, not shipped
    db.execute(
        "UPDATE ext_panels SET data_json = replace(data_json, 'http://127.0.0.1:8765', "
        "'https://evil.example') WHERE panel_id = 'paper'"
    )
    assert "local_url" not in _panels(db, now)["paper"]


def _json_keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(cast(dict[str, Any], node)) | {
            k for v in cast(dict[str, Any], node).values() for k in _json_keys(v)
        }
    if isinstance(node, list):
        return {k for v in cast(list[Any], node) for k in _json_keys(v)}
    return set()


def test_panel_words_are_redacted_in_both_tiers_even_from_a_stored_copy(
    db: sqlite3.Connection,
) -> None:
    now = utcnow()
    _panel_ext(db, now, ids=("a",))
    ghp = "ghp_" + "a1B2" * 9
    views = [{"id": "v", "title": "V", "widgets": [
        {"type": "text", "text": f"token {ghp} link {RC} ok"},
        {"type": "kv", "items": [{"key": "k", "value": f"{ghp}"}]},
        {"type": "details", "rows": [{"depth": 0, "key": "remote_url", "kind": "string",
                                      "value": RC}]},
    ]}]  # fmt: skip
    ext_store.save_panel_ok(
        db, "ledger", "a", data=json.dumps({"views": views}), updated_at=now, ran_at=now,
        bytes=9, expect_by=now + timedelta(hours=1),
    )  # fmt: skip
    ext_store.record_panel_failure(db, "ledger", "a", ran_at=now, error=f"boom {ghp}")
    s = build_snapshot(db, load_settings(), started_at=now, now=now, redactor=Redactor(["tok-123"]))
    for tier in (s, to_remote(s)):
        text = json.dumps(tier.model_dump(mode="json"))
        assert ghp not in text and "claude.ai/code" not in text and "AbC123xyz" not in text
        assert "remote_url" not in _json_keys(tier.model_dump(mode="json"))  # a value, not a key
        assert "ok" in text and redact_secrets(text) == text


def test_the_panel_budget_drops_the_largest_then_the_oldest_and_says_truncated(
    db: sqlite3.Connection,
) -> None:
    now = utcnow()
    ids = ("old", "new", "mid", "small")
    _panel_ext(db, now, ids=ids)
    sizes: dict[str, Normalized] = {}
    plan: list[tuple[str, dict[str, Any], int]] = [
        ("old", {"rows": 50, "items": 50, "details": 100}, 90),  # the two largest are equal
        ("new", {"rows": 50, "items": 50, "details": 100}, 1),
        ("mid", {"rows": 10, "items": 10, "details": 10}, 1),
        ("small", {"rows": 2, "items": 2, "details": 2}, 1),
    ]
    for pid, kw, age in plan:
        at = now - timedelta(minutes=age)
        sizes[pid] = store_panel(db, "ledger", pid, raw_panel(updated_at=at, **kw), at)
    total = sum(n.bytes for n in sizes.values())
    assert sizes["old"].bytes == sizes["new"].bytes > sizes["mid"].bytes > sizes["small"].bytes
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    assert all(p.views for p in s.extensions[0].panels) and total < snap.PANEL_BUDGET_BYTES
    exts = [e.model_dump(mode="json") for e in s.extensions]
    assert snap.panel_bytes(exts) == total
    assert snap.fit_panels(exts, total) is False  # fits: nothing dropped

    def held() -> dict[str, bool]:
        return {p["id"]: bool(p["views"]) for p in exts[0]["panels"]}

    assert snap.fit_panels(exts, total - 1) is True  # one must go: the oldest of the two largest
    assert held() == {"old": False, "new": True, "mid": True, "small": True}
    gone = next(p for p in exts[0]["panels"] if p["id"] == "old")
    assert gone["truncated"] is True and "views" not in gone["outside"] and gone["ok"] is True
    assert snap.panel_bytes(exts) <= total - 1
    assert snap.fit_panels(exts, snap.panel_bytes(exts) - 1) is True  # then the other large one
    assert held() == {"old": False, "new": False, "mid": True, "small": True}
    assert snap.fit_panels(exts, 0) is True
    assert snap.panel_bytes(exts) == 0 and snap.fit_panels(exts, 0) is False
    Snapshot.model_validate({**s.model_dump(mode="json"), "extensions": exts})


def test_the_builder_enforces_the_panel_budget_before_the_diagram(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = utcnow()
    ids = tuple(f"p{i}" for i in range(6))
    _panel_ext(db, now, ids=ids)
    for pid in ids:
        store_panel(db, "ledger", pid, raw_panel(rows=50, items=50, details=100), now)
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    panels = s.extensions[0].panels
    held = [p for p in panels if p.views]
    assert 0 < len(held) < len(panels), "six 26 KB panels are over the 100 KB budget"
    assert sum(p.bytes for p in held) <= snap.PANEL_BUDGET_BYTES
    dropped = [p for p in panels if not p.views]
    assert all(p.truncated and "views" not in p.outside for p in dropped)
    # a dropped panel keeps its state and still has its diagram node
    assert {n.panel for n in (s.diagram.nodes if s.diagram else []) if n.kind == "panel"} == set(
        ids
    )


def test_a_typical_store_with_two_full_panels_still_fits_the_relay(db: sqlite3.Connection) -> None:
    now = utcnow()
    seed_typical(db, now)
    without = data_size(remote_dump(db, now))
    _panel_ext(db, now)
    for pid in ("paper", "oncall"):
        store_panel(db, "ledger", pid, raw_panel(), now)
    data = remote_dump(db, now)
    size = data_size(data)
    assert data["trimmed"] == "none" and pusher_mod.shrink(data) is data
    assert 40_000 < size - without < 70_000, (without, size)  # two ~24 KB panels
    assert size < pusher_mod.MAX_BYTES * 3 // 4, size


def test_the_panel_schema_is_in_the_snapshot_schema_and_is_flat() -> None:
    schema = Snapshot.model_json_schema()
    defs = schema["$defs"]
    assert {"Panel", "PanelView", "StatWidget", "DetailRow", "DetailsWidget"} <= set(defs)
    assert (
        "panels" in defs["Extension"]["properties"] and "panel" in defs["DiagramNode"]["properties"]
    )
    assert "panels" in schema["properties"]["trimmed"]["enum"]
    assert defs["Panel"]["properties"]["local_url"]["x-tier"] == "local"

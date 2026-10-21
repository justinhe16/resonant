"""The dashboard snapshot (docs/dashboard-plan.md §3): tiers, the builder, the schema file."""

from __future__ import annotations

import json
import random
import re
import sqlite3
import string
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast, get_args

import pytest
from pydantic import BaseModel, Field, ValidationError

from resonant import __version__
from resonant.config import load_settings
from resonant.dashboard import pusher as pusher_mod
from resonant.dashboard import snapshot as snap
from resonant.dashboard.pusher import data_size
from resonant.dashboard.snapshot import (
    ApprovalBrief,
    ApprovalItem,
    Approvals,
    BrainActivity,
    BrainToolBrief,
    Extension,
    HealthRow,
    Job,
    JobOutcome,
    Pulse,
    PulseBrief,
    PulseItem,
    QuestionBrief,
    QuestionItem,
    Questions,
    RepoItem,
    RunBrief,
    RunItem,
    Runner,
    Runs,
    Slots,
    Snapshot,
    Tasks,
    TrustRule,
    build_snapshot,
    schema_json,
    to_remote,
)
from resonant.loop import SlotPool
from resonant.loop.types import NewTask, TaskStatus
from resonant.store import approvals as ap
from resonant.store import extensions as ext_store
from resonant.store import pulse, questions, repos, runs, trust
from resonant.store import tasks as task_store
from resonant.store.audit import audit
from resonant.store.claude_brain import SESSION_KV, TOOL_ACTION
from resonant.store.db import iso, kv_set, utcnow
from resonant.store.runs import Status as RunStatus
from resonant_sdk import Effect, Level

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "docs" / "snapshot.schema.json"
SECRET = "SEKRIT"  # planted in every text field; must never reach the remote tier
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


# --- the schema ---------------------------------------------------------------------------


def test_schema_file_is_current() -> None:
    assert SCHEMA_FILE.read_text() == schema_json(), (
        "docs/snapshot.schema.json is stale: run "
        "`uv run python -m resonant.dashboard.snapshot > docs/snapshot.schema.json`"
    )


def _remote_strings(schema: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every string-typed node (and dict key schema) reachable without passing a local-only
    property, with its path."""
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
        if "propertyNames" in node:  # dict keys are strings, typed or not
            found.append((f"{path}{{key}}", node["propertyNames"]))
        elif isinstance(node.get("additionalProperties"), dict):
            found.append((f"{path}{{key}}", {"type": "string"}))  # unconstrained dict keys
        if isinstance(node.get("additionalProperties"), dict):
            walk(node["additionalProperties"], f"{path}{{value}}")
        for name, sub in cast(dict[str, Any], node.get("properties", {})).items():
            if sub.get("x-tier") == "local":
                continue
            walk(sub, f"{path}.{name}")

    walk(schema, "$")
    return found


def test_remote_strings_are_never_free_text() -> None:
    """Every string the remote tier keeps is an enum, a date-time or one of the three short
    identifier patterns (never just any pattern: ``^.*$`` would be free text)."""
    strings = _remote_strings(Snapshot.model_json_schema())
    assert strings  # the walk found the remote fields
    loose = [
        path
        for path, node in strings
        if not ("enum" in node or "const" in node or node.get("format") == "date-time")
        and node.get("pattern") not in (snap.IDENT_PATTERN, snap.VERSION_PATTERN, snap.CODE_PATTERN)
    ]
    assert loose == []
    by_path = {p: n.get("pattern") for p, n in strings}
    assert by_path["$.version"] == snap.VERSION_PATTERN  # used only where meant
    assert [p for p, pat in by_path.items() if pat == snap.VERSION_PATTERN] == ["$.version"]
    assert sorted(p for p, pat in by_path.items() if pat == snap.CODE_PATTERN) == [
        "$.questions.brief[].run_code",
        "$.runs.brief[].code",
    ]


def test_the_walk_flags_free_text() -> None:
    class Leaky(BaseModel):
        state: Literal["a", "b"]
        text: str
        counts: dict[str, int]
        note: str | None = Field(default=None, json_schema_extra=snap.LOCAL_ONLY)

    loose = [p for p, node in _remote_strings(Leaky.model_json_schema()) if "enum" not in node]
    assert loose == ["$.text", "$.counts{key}"]


def test_local_only_fields_are_marked() -> None:
    # The §3 local-only fields, so a rename can't silently move one to the remote tier.
    marked = {
        (m.__name__, f)
        for m in (
            Snapshot,
            HealthRow,
            Runs,
            RunItem,
            Approvals,
            Questions,
            Pulse,
            Runner,
            snap.ClaudeBrain,
        )
        for f in m.model_fields
        if snap.is_local_only(m, f)
    }
    assert marked == {
        ("Snapshot", "trust_rules"),
        ("Snapshot", "repos"),
        ("Snapshot", "claude_brain"),  # BR-11
        ("ClaudeBrain", "remote_url"),
        ("HealthRow", "reason"),
        ("Runs", "items"),
        ("RunItem", "remote_url"),
        ("Approvals", "items"),
        ("Questions", "items"),
        ("Pulse", "items"),
        ("Runner", "missing"),
    }


# --- tier filtering, property-checked over random snapshots ------------------------------


def _text(rng: random.Random) -> str:
    alphabet = string.ascii_letters + string.digits + " :/._-é✓\n\"'<>{}"
    noise = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
    return f"{noise}{SECRET}{rng.randint(0, 10**6)}"


def _ident(rng: random.Random) -> str:
    return rng.choice(string.ascii_lowercase) + "".join(
        rng.choice(string.ascii_lowercase + string.digits + "-") for _ in range(rng.randint(0, 20))
    )


def _code(rng: random.Random) -> str:
    return rng.choice("RW") + str(rng.randint(1, 999999))


def _dt(rng: random.Random) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=rng.randint(0, 10**8))


def _maybe[T](rng: random.Random, value: T) -> T | None:
    return value if rng.random() < 0.7 else None


def _counts[K: str](rng: random.Random, keys: tuple[K, ...]) -> dict[K, int]:
    return {k: rng.randint(0, 50) for k in rng.sample(keys, rng.randint(0, len(keys)))}


def _random_snapshot(rng: random.Random) -> Snapshot:
    def n(k: int = 6) -> range:
        return range(rng.randint(0, k))

    run_states = cast(tuple[RunStatus, ...], get_args(RunStatus))
    task_states = cast(tuple[TaskStatus, ...], get_args(TaskStatus))
    return Snapshot(
        schema=1,
        tier="local",
        taken_at=_dt(rng),
        version=rng.choice(["0.1.0", "deadbeef"]),
        started_at=_dt(rng),
        last_tick=_maybe(rng, _dt(rng)),
        autonomy=rng.choice(["on", "off"]),
        paused=rng.random() < 0.5,
        dry_run=rng.random() < 0.5,
        health=[
            HealthRow(
                name=_ident(rng),
                state=rng.choice(["ok", "degraded", "failing", "unknown"]),
                since=_maybe(rng, _dt(rng)),
                reason=_maybe(rng, _text(rng)),
            )
            for _ in n()
        ],
        tasks=Tasks(by_state=_counts(rng, task_states)),
        slots=Slots(used=rng.randint(0, 5), max=5),
        runs=Runs(
            by_state=_counts(rng, run_states),
            items=[
                RunItem(
                    code=_text(rng),
                    state=rng.choice(run_states),
                    repo=_text(rng),
                    ask=_text(rng),
                    branch=_maybe(rng, _text(rng)),
                    pr_url=_maybe(rng, _text(rng)),
                    pr_number=_maybe(rng, rng.randint(1, 999)),
                    started_at=_maybe(rng, _dt(rng)),
                    ended_at=_maybe(rng, _dt(rng)),
                    waiting_reason=_maybe(rng, rng.choice(["usage_reset", "question"])),
                    last_note=_maybe(rng, _text(rng)),
                )
                for _ in n()
            ],
            brief=[
                RunBrief(
                    code=_code(rng),
                    state=rng.choice(run_states),
                    scratch=rng.random() < 0.5,
                    has_pr=rng.random() < 0.5,
                    started_at=_maybe(rng, _dt(rng)),
                    ended_at=_maybe(rng, _dt(rng)),
                    waiting_reason=cast(Any, _maybe(rng, rng.choice(["usage_reset", "question"]))),
                )
                for _ in n()
            ],
        ),
        approvals=Approvals(
            open=rng.randint(0, 9),
            items=[
                ApprovalItem(
                    code=_maybe(rng, _text(rng)),
                    summary=_text(rng),
                    level=rng.choice(["L0", "L1", "L2", "L3"]),
                    asked_at=_dt(rng),
                    expires_at=_dt(rng),
                )
                for _ in n()
            ],
            brief=[
                ApprovalBrief(
                    level=rng.choice(["L0", "L1", "L2", "L3"]),
                    effect=rng.choice(["read", "write", "pay"]),
                    needs_code=rng.random() < 0.5,
                    asked_at=_dt(rng),
                    expires_at=_dt(rng),
                )
                for _ in n()
            ],
        ),
        questions=Questions(
            open=rng.randint(0, 9),
            items=[
                QuestionItem(
                    run_code=_text(rng),
                    text=_text(rng),
                    options=[_text(rng) for _ in n(3)],
                    asked_at=_dt(rng),
                )
                for _ in n()
            ],
            brief=[
                QuestionBrief(run_code=_code(rng), options=rng.randint(0, 4), asked_at=_dt(rng))
                for _ in n()
            ],
        ),
        pulse=Pulse(
            open=rng.randint(0, 9),
            items=[
                PulseItem(
                    source=_text(rng),
                    key=_text(rng),
                    title=_text(rng),
                    urgency=rng.choice(["urgent", "normal", "low"]),
                    state=rng.choice(["new", "told"]),
                    updated_at=_dt(rng),
                )
                for _ in n()
            ],
            brief=[
                PulseBrief(
                    source=_ident(rng),
                    urgency=rng.choice(["urgent", "normal", "low"]),
                    state=rng.choice(["new", "told"]),
                    updated_at=_dt(rng),
                )
                for _ in n()
            ],
        ),
        extensions=[
            Extension(
                name=_ident(rng),
                status=rng.choice(["enabled", "disabled", "disabled_tampered"]),
                health=rng.choice(["ok", "degraded", "unknown"]),
                jobs=[
                    Job(
                        id=_ident(rng),
                        next_due=_maybe(rng, _dt(rng)),
                        last_run_at=_maybe(rng, _dt(rng)),
                        last_outcome=_maybe(rng, rng.choice(get_args(JobOutcome))),
                        fail_streak=rng.randint(0, 9),
                    )
                    for _ in n(3)
                ],
            )
            for _ in n(4)
        ],
        runner=Runner(
            set_up=rng.random() < 0.5,
            missing=_maybe(rng, [_text(rng) for _ in n(3)]),
            gaps=rng.randint(0, 3),
        ),
        brain_activity=BrainActivity(
            calls_today=rng.randint(0, 99),
            writes_today=rng.randint(0, 9),
            state=cast(Any, _maybe(rng, rng.choice(["up", "down"]))),
            turns=rng.randint(0, 99),
            recent=[
                BrainToolBrief(
                    at=_dt(rng),
                    tool=_ident(rng),
                    decision=rng.choice(get_args(snap.BrainDecision)),
                    status=rng.choice(["ok", "error"]),
                    write=rng.random() < 0.5,
                )
                for _ in n()
            ],
        ),
        trust_rule_count=rng.randint(0, 9),
        repo_count=rng.randint(0, 9),
        trust_rules=_maybe(rng, [TrustRule(n=i, text=_text(rng)) for i in n()]),
        repos=_maybe(
            rng,
            [
                RepoItem(
                    name=_text(rng),
                    mode=rng.choice(["push", "fork"]),
                    base=_text(rng),
                    ci=_maybe(rng, True),
                )
                for _ in n()
            ],
        ),
    )


def _strings(value: Any) -> list[str]:
    """Every string in a JSON value, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        d = cast(dict[str, Any], value)
        return [*d.keys(), *(s for v in d.values() for s in _strings(v))]
    if isinstance(value, list):
        return [s for v in cast(list[Any], value) for s in _strings(v)]
    return []


def _check_remote(remote: Snapshot) -> None:
    data = remote.model_dump(mode="json")
    assert data["tier"] == "remote"
    assert SECRET not in json.dumps(data, ensure_ascii=False)
    for s in _strings(data):
        assert (
            _DATE_TIME.match(s)
            or re.fullmatch(r"[a-z][a-z0-9:_-]{0,63}", s)  # identifiers, enums, keys
            or re.fullmatch(r"[A-Z][0-9]{1,6}", s)  # run codes
            or re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", s)  # version
        ), s
    # No local-only key at all, at any depth (left out, not null).
    for key in ("reason", "items", "missing", "trust_rules", "repos", "remote_url"):
        assert f'"{key}"' not in json.dumps(data), key
    # It is still a valid snapshot (the schema DB-3 pushes).
    assert Snapshot.model_validate(data) == remote


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
    remote = to_remote(s)
    _check_remote(remote)
    assert "claude.ai" not in json.dumps(remote.model_dump(mode="json"))


def test_brain_tool_calls_are_local_and_their_counts_remote(db: sqlite3.Connection) -> None:
    """PER-258: the Claude brain's last tool calls (command, cwd, decision) show on the local
    dashboard only; the remote tier carries today's counts."""
    now = utcnow()
    kv_set(db, SESSION_KV, {"session_id": "s", "turns": 2, "pid": 1})
    for command, write, status in [("git log -1", False, "ok"), (f"git push {SECRET}", True, "ok"),
                                   ("rm -rf x", True, "error")]:  # fmt: skip
        audit(db, TOOL_ACTION, tool="Bash", decision="gate" if write else "allowed",
              command=command, cwd="/Users/x/code/r", status=status, write=write)  # fmt: skip
    s = build_snapshot(db, load_settings(), started_at=now, now=now, version="deadbeef")
    local = s.model_dump(mode="json")
    tools = local["claude_brain"]["tools"]
    assert [t["command"] for t in tools] == ["rm -rf x", f"git push {SECRET}", "git log -1"]
    assert tools[1]["decision"] == "gate" and tools[1]["write"] is True
    activity = local["brain_activity"]
    assert (activity["calls_today"], activity["writes_today"]) == (3, 1)
    assert (activity["state"], activity["turns"]) == ("up", 2)
    assert [(t["tool"], t["decision"], t["status"], t["write"]) for t in activity["recent"]] == [
        ("bash", "gate", "error", True),
        ("bash", "gate", "ok", True),
        ("bash", "allowed", "ok", False),
    ]
    remote = to_remote(s)
    _check_remote(remote)
    dumped = remote.model_dump(mode="json")
    assert dumped["brain_activity"] == activity
    assert "claude_brain" not in dumped
    assert "git push" not in json.dumps(dumped) and SECRET not in json.dumps(dumped)


@pytest.mark.parametrize(
    ("name", "kind"),
    [("Bash", "bash"), ("Read", "read"), ("mcp__claude_ai_Gmail__send", "mcp"),
     ("Web Fetch", "other"), (f"{SECRET}/x", "other")],
)  # fmt: skip
def test_brain_tool_names_are_generalized(name: str, kind: str) -> None:
    assert snap._tool_kind(name) == kind  # pyright: ignore[reportPrivateUsage]


def test_pulse_brief_sources_are_slugs_and_most_urgent_first(db: sqlite3.Connection) -> None:
    now = utcnow()
    pulse.upsert(db, "core", "a", f"low {SECRET}", "low", None, False, 3600, now)
    pulse.upsert(db, "core", "b", f"urgent {SECRET}", "urgent", None, False, 3600, now)
    db.execute("UPDATE pulse_items SET source = 'Not A Slug' WHERE key = 'a'")
    s = build_snapshot(db, load_settings(), started_at=now, now=now)
    assert [(p.source, p.urgency) for p in s.pulse.brief] == [("core", "urgent"), ("other", "low")]
    _check_remote(to_remote(s))


@pytest.mark.parametrize("seed", range(300))
def test_remote_carries_no_text_over_random_snapshots(seed: int) -> None:
    rng = random.Random(seed)
    local = _random_snapshot(rng)
    _check_remote(to_remote(local))
    # The local tier keeps what it was given; to_remote doesn't mutate it.
    assert local.tier == "local"
    assert to_remote(local) == to_remote(to_remote(local))


# --- the builder over a store -------------------------------------------------------------


def test_empty_db(db: sqlite3.Connection) -> None:
    settings = load_settings()
    s = build_snapshot(db, settings, started_at=utcnow(), version="0.1.0")
    data = s.model_dump(mode="json")
    assert data["schema"] == 1 and data["tier"] == "local"
    assert data["autonomy"] == "on" and data["paused"] is False and data["dry_run"] is True
    assert [h["name"] for h in data["health"]] == ["model", "imessage", "loop", "store", "backup"]
    assert {h["state"] for h in data["health"]} == {"unknown"}
    assert all("reason" not in h for h in data["health"])
    assert data["tasks"] == {"by_state": {}}
    assert data["slots"] == {"used": 0, "max": settings.claude.max_concurrent}
    assert data["runs"] == {"by_state": {}, "items": [], "brief": []}
    assert data["approvals"] == {"open": 0, "items": [], "brief": []}
    assert data["questions"] == {"open": 0, "items": [], "brief": []}
    assert data["pulse"] == {"open": 0, "items": [], "brief": []}
    assert data["extensions"] == []
    assert data["runner"]["set_up"] is False and data["runner"]["missing"]
    assert data["runner"]["gaps"] == 1
    assert data["trust_rules"] == [] and data["repos"] == []
    assert data["trust_rule_count"] == 0 and data["repo_count"] == 0
    assert data["brain_activity"] == {
        "calls_today": 0, "writes_today": 0, "state": None, "turns": 0, "recent": []
    }  # fmt: skip
    _check_remote(to_remote(s))


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
            commit="abc",
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
    assert "reason" not in health["ext:github"]
    assert d["tasks"]["by_state"] == {"waiting": 1, "queued": 1}
    assert d["slots"] == {"used": 1, "max": 3}

    assert d["runs"]["by_state"] == {"waiting": 1, "ready": 1}
    by_code = {r["code"]: r for r in d["runs"]["items"]}
    assert by_code["R1"]["ask"] == f"build {SECRET}"
    assert by_code["R1"]["waiting_reason"] == "question"
    assert by_code["R1"]["last_note"] == f"note {SECRET}"
    assert by_code["R2"]["repo"] == "" and by_code["R2"]["pr_number"] == 7
    assert d["runs"]["items"][0]["code"] == "R1"  # active first

    assert d["approvals"]["open"] == 1
    assert d["approvals"]["items"][0] | {"asked_at": None, "expires_at": None} == {
        "code": "K4",
        "summary": f"restart {SECRET}",
        "level": "L2",
        "asked_at": None,
        "expires_at": None,
    }
    assert d["questions"]["open"] == 1
    assert d["questions"]["items"][0]["text"] == f"which db {SECRET}?"
    assert d["questions"]["items"][0]["options"] == ["a", SECRET]
    assert d["pulse"]["open"] == 1
    item = d["pulse"]["items"][0]
    assert (item["source"], item["key"], item["urgency"], item["state"]) == (
        "github",
        "ci/dori",
        "urgent",
        "new",
    )

    [ext] = d["extensions"]
    assert (ext["name"], ext["status"], ext["health"]) == ("github", "enabled", "ok")
    jobs = {j["id"]: j for j in ext["jobs"]}
    assert jobs["ci-watch"]["last_outcome"] == "failed" and jobs["ci-watch"]["fail_streak"] == 1
    assert jobs["morning"]["last_outcome"] == "other"  # unknown outcomes never pass through
    assert datetime.fromisoformat(jobs["ci-watch"]["next_due"]) > now
    morning_due = datetime.fromisoformat(jobs["morning"]["next_due"])
    assert (morning_due.hour, morning_due.minute) == (8, 0)

    assert d["runner"] == {"set_up": True, "gaps": 0}
    assert d["trust_rules"] == [{"n": 1, "text": "ask before merge in dori"}]
    assert d["repos"] == [{"name": "dori", "mode": "push", "base": "main", "ci": True}]

    remote = to_remote(s)
    _check_remote(remote)
    rd = remote.model_dump(mode="json")

    # The remote tier keeps the counts, states, codes, kinds and times, and nothing else.
    def at(d: dict[str, Any], *keys: str) -> dict[str, Any]:
        return d | dict.fromkeys(keys)

    assert [at(r, "started_at", "ended_at") for r in rd["runs"]["brief"]] == [
        {"code": "R1", "state": "waiting", "scratch": False, "has_pr": False,
         "started_at": None, "ended_at": None, "waiting_reason": "question"},
        {"code": "R2", "state": "ready", "scratch": True, "has_pr": True,
         "started_at": None, "ended_at": None, "waiting_reason": None},
    ]  # fmt: skip
    assert rd["runs"]["by_state"] == {"waiting": 1, "ready": 1}
    assert rd["runs"].keys() == {"by_state", "brief"}
    assert rd["approvals"]["open"] == 1
    assert [at(a, "asked_at", "expires_at") for a in rd["approvals"]["brief"]] == [
        {"level": "L2", "effect": "write", "needs_code": False,
         "asked_at": None, "expires_at": None},
    ]  # fmt: skip
    assert "K4" not in json.dumps(rd)  # the approval's code stays local
    assert rd["questions"]["open"] == 1
    assert [at(q, "asked_at") for q in rd["questions"]["brief"]] == [
        {"run_code": "R1", "options": 2, "asked_at": None}
    ]
    assert rd["pulse"]["open"] == 1
    assert [at(p, "updated_at") for p in rd["pulse"]["brief"]] == [
        {"source": "github", "urgency": "urgent", "state": "new", "updated_at": None}
    ]
    assert "ci/dori" not in json.dumps(rd) and "dori" not in json.dumps(rd)
    assert rd["runner"] == {"set_up": True, "gaps": 0}
    assert (rd["trust_rule_count"], rd["repo_count"]) == (1, 1)
    assert rd["extensions"] == d["extensions"]
    assert [h | {"reason": None} for h in rd["health"]] == [
        h | {"reason": None} for h in d["health"]
    ]


def test_remote_tier_stays_under_the_relay_cap(db: sqlite3.Connection) -> None:
    """Every brief list past its cap, the longest slugs, ten extensions: the remote tier still
    fits the relay's 30 KiB with room to spare, as the pusher measures it (``data::text``)."""
    now = utcnow()
    task_id = task_store.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    for i in range(60):
        r = runs.create(db, repo="r", ask=f"a{i}", principal="justin", session_id=f"s{i}",
                        max_seconds=60)  # fmt: skip
        runs.update(db, r.code, status="waiting", wait_reason="question", pr_number=i)
        questions.ask(db, r.code, "ask", f"q{i}?", options=["a", "b", "c"], at=now)
    for i in range(60):
        ap.insert_approval(
            db,
            ap.NewApproval(
                task_id=task_id,
                intent_hash=f"h{i}",
                tool="t",
                effect=Effect.WRITE,
                level=Level.L2,
                summary="s",
                approvers=("justin",),
                code=f"K{i}",
                expires_at=now + timedelta(minutes=30),
            ),
            now,
        )
    long = "x" * 63
    for i in range(60):
        pulse.upsert(db, f"s{long}", f"k/{i}", "t", "urgent", None, False, 3600, now)
    for i in range(10):
        name = f"x{i:02d}".ljust(60, "x")  # ``ext:<name>`` is 64 too
        kv_set(db, f"health.ext:{name}", {"state": "degraded", "since": iso(now), "detail": "x"})
        ext_store.save_extension(
            db,
            ext_store.ExtensionRecord(
                name=name, source="repo:x", commit="c", tree_digest="d", manifest_sha="m",
                manifest={"name": name, "jobs": [
                    {"id": f"j{j}".ljust(64, "x"), "schedule": "*/5 * * * *"} for j in range(3)
                ]},
                installed_at=now, installed_by="justin",
            ),
        )  # fmt: skip
    kv_set(db, SESSION_KV, {"session_id": "s", "turns": 999, "pid": 1})
    for _ in range(40):
        audit(db, TOOL_ACTION, tool=long, decision="granted", command="c", cwd="/x",
              status="error", write=True)  # fmt: skip
    s = build_snapshot(db, load_settings(), started_at=now, now=now, version="deadbeef")
    remote = to_remote(s)
    _check_remote(remote)
    data = remote.model_dump(mode="json")
    assert len(data["runs"]["brief"]) == snap.RUN_BRIEF_ITEMS
    assert len(data["approvals"]["brief"]) == snap.APPROVAL_BRIEF_ITEMS
    assert len(data["questions"]["brief"]) == snap.QUESTION_BRIEF_ITEMS
    assert len(data["pulse"]["brief"]) == snap.PULSE_BRIEF_ITEMS
    assert len(data["brain_activity"]["recent"]) == snap.BRAIN_BRIEF_ITEMS
    assert len(data["extensions"]) == 10 and len(data["health"]) >= 10
    assert data_size(data) < 0.75 * pusher_mod.MAX_BYTES


@pytest.mark.parametrize("seed", range(20))
def test_remote_over_random_stores(db: sqlite3.Connection, seed: int) -> None:
    """Random texts written through the store never reach the remote tier."""
    rng = random.Random(seed)
    now = utcnow()
    for _ in range(rng.randint(0, 5)):
        run = runs.create(db, repo=rng.choice(["", "dori"]), ask=_text(rng), principal="j",
                          session_id="s", max_seconds=60)  # fmt: skip
        runs.update(db, run.code, last_note=_text(rng), status=rng.choice(runs.ACTIVE))
        if rng.random() < 0.5:
            questions.ask(db, run.code, "ask", _text(rng), options=[_text(rng)], at=now)
    for i in range(rng.randint(0, 5)):
        pulse.upsert(db, "core", f"k{i}", _text(rng), "normal", None, False, 3600, now)
    kv_set(db, "health.loop", {"state": "degraded", "since": iso(now), "detail": _text(rng)})
    local = build_snapshot(
        db, load_settings(), started_at=now, runner_missing=[_text(rng)], now=now
    )
    assert SECRET in json.dumps(local.model_dump(mode="json"))
    _check_remote(to_remote(local))


def test_rows_with_bad_identifiers_are_left_out(db: sqlite3.Connection) -> None:
    """A stale or hand-edited row never breaks the dashboard (or reaches the remote tier)."""
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
    _check_remote(to_remote(s))


def test_to_remote_validates() -> None:
    """``to_remote`` re-validates, so a snapshot built with ``model_copy`` past the checks
    can't be pushed."""
    bad = _random_snapshot(random.Random(1)).model_copy(update={"version": "has spaces"})
    with pytest.raises(ValidationError):
        to_remote(bad)


def test_code_version_is_a_sha_or_the_package_version() -> None:
    v = snap.code_version()
    assert re.fullmatch(r"[0-9a-f]{8}", v) or v == __version__


UI_FIXTURES = Path(__file__).resolve().parents[1] / "dashboard" / "src" / "test" / "fixtures"


@pytest.mark.parametrize("name", ["empty", "local", "remote"])
def test_the_ui_fixtures_match_the_schema(name: str) -> None:
    """DB-2's fixtures are real snapshots, so the UI is tested on the shape the daemon sends."""
    data = json.loads((UI_FIXTURES / f"{name}.json").read_text())
    parsed = Snapshot.model_validate(data)  # extra="forbid": nothing extra, nothing renamed
    if parsed.tier == "remote":
        _check_remote(parsed)
        assert to_remote(parsed) == parsed

"""The dashboard's architecture diagram (PER-277): the curated core skeleton against the code
and docs/architecture.md, the graph built from installed manifests, the launchd scan and its
filter, the sentences, redaction and the relay's size and trim."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from resonant.config import load_settings
from resonant.dashboard import diagram
from resonant.dashboard import pusher as pusher_mod
from resonant.dashboard.diagram import AgentState
from resonant.dashboard.snapshot import Snapshot, build_snapshot, to_remote
from resonant.monitor import CHECKS
from resonant.redact import redact_secrets
from resonant.store import extensions as ext_store
from resonant.store import repos
from resonant.store.db import iso, kv_set, utcnow
from resonant_sdk.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = ROOT / "docs" / "architecture.md"
PACKAGE_SRC = ROOT / "packages" / "resonant" / "src"

# The skeleton the ticket asks for. Renaming or removing one is a decision: update this list,
# the module in ``CORE`` and docs/architecture.md together.
EXPECTED_CORE = (
    "gateway", "loop", "router", "brain", "toolcore", "gate", "executor", "scheduler",
    "workers", "pulse", "store", "backups", "dashboard", "relay", "supabase", "fly",
)  # fmt: skip

# A weekday 16:10 New York job that asks, an agent it declares, a repo it is about.
TRADE_JEV: dict[str, Any] = {
    "name": "trade-jev",
    "version": 1,
    "description": "Paper trading on the jev strategy.",
    "repos": ["jhe/trade-jev"],
    "jobs": [
        {
            "id": "close-summary",
            "schedule": "10 16 * * 1-5",
            "tz": "America/New_York",
            "run": "python3 cli.py close",
            "effect": "read",
            "description": "reads paper.db and adds the day's paper P&L to the hourly text",
        }
    ],
    "tools": {
        "positions": {"run": "python3 cli.py positions", "effect": "read",
                      "description": "Open paper positions."},
        "flatten": {"run": "python3 cli.py flatten", "effect": "write", "level": "L1",
                    "description": "Close every paper position."},
    },
    "launch_agents": [
        {"label": "com.tradejev.paper", "description": "The paper trading loop, every minute."},
        {"label": "com.tradejev.view", "description": "Serves the trading view on localhost."},
    ],
}  # fmt: skip


def _install(db: sqlite3.Connection, manifest: dict[str, Any], now: datetime) -> None:
    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name=manifest["name"], source=f"repo:{manifest['name']}", commit="abc1234",
            tree_digest="d", manifest_sha="m", installed_at=now, installed_by="justin",
            manifest=manifest,
        ),
    )  # fmt: skip


def _real(name: str) -> dict[str, Any]:
    """A first-party extension's manifest as ``ext add`` stores it."""
    return load_manifest(ROOT / "extensions" / name / "resonant.yaml").model_dump(mode="json")


AGENTS = [
    AgentState("com.resonant.daemon", plist=True, loaded=True, running=True, last_exit=0),
    AgentState("com.resonant.ollama", plist=True, loaded=True, running=True, last_exit=0),
    AgentState("com.tradejev.paper", plist=True, loaded=True, running=True, last_exit=0),
    AgentState("com.tradejev.tvtoken", plist=True, loaded=True, running=False, last_exit=0),
    AgentState("com.dori.sync", plist=True, loaded=False, running=False),
    AgentState("com.apple.Safari.something", plist=False, loaded=True, running=True),
    AgentState("com.owner.private-thing", plist=True, loaded=True, running=True, last_exit=0),
    AgentState("homebrew.mxcl.postgresql", plist=True, loaded=True, running=False, last_exit=1),
]


def _build(db: sqlite3.Connection, now: datetime, **kw: Any) -> Snapshot:
    return build_snapshot(
        db,
        load_settings(),
        started_at=now,
        now=now,
        version="deadbeef",
        core_jobs=[("pulse", "0 * * * *", "America/Los_Angeles"),
                   ("backup", "30 3 * * *", "America/Los_Angeles")],
        **kw,
    )  # fmt: skip


def _nodes(s: Snapshot) -> dict[str, Any]:
    assert s.diagram is not None
    return {n.id: n for n in s.diagram.nodes}


# --- the core skeleton ------------------------------------------------------------------


def test_the_core_skeleton_matches_the_code_and_the_architecture_doc() -> None:
    """Fails when a core component is renamed or removed: its module must exist and
    docs/architecture.md must still name it."""
    doc = ARCHITECTURE.read_text()
    assert tuple(c.id for c in diagram.CORE) == EXPECTED_CORE
    for c in diagram.CORE:
        base = PACKAGE_SRC if c.module.startswith("resonant/") else ROOT
        path = base / c.module
        assert path.exists(), f"core component {c.id}: {c.module} is gone (renamed?)"
        assert path.is_dir() == c.module.endswith("/"), c.module
        needle = c.module if c.module.startswith("resonant/") else f"`{c.module}`"
        assert needle in doc, f"docs/architecture.md no longer names {c.module} ({c.id})"
        assert c.text.endswith(".") and len(c.text) <= 300
        assert c.check is None or c.check in (*CHECKS, "relay")


def test_core_edges_join_core_components() -> None:
    ids = {c.id for c in diagram.CORE}
    for source, target, label in diagram.CORE_EDGES:
        assert source in ids and target in ids and len(label) <= 40
    # The ticket's flows: text in -> router -> brain -> gate -> tool core -> worker/extension;
    # reports -> pulse -> iMessage out; daemon -> relay.
    edges = {(s, t) for s, t, _ in diagram.CORE_EDGES}
    for pair in [("gateway", "loop"), ("loop", "router"), ("router", "brain"),
                 ("brain", "toolcore"), ("toolcore", "gate"), ("gate", "executor"),
                 ("executor", "workers"), ("workers", "pulse"), ("pulse", "gateway"),
                 ("store", "relay"), ("relay", "supabase"), ("supabase", "fly")]:  # fmt: skip
        assert pair in edges, pair


def test_an_empty_store_still_has_the_skeleton(db: sqlite3.Connection) -> None:
    s = _build(db, utcnow())
    nodes = _nodes(s)
    assert [n for n in nodes if n.startswith("core:")] == [f"core:{c}" for c in EXPECTED_CORE]
    assert nodes["core:brain"].health == "unknown"  # never started
    assert nodes["core:workers"].health == "degraded"  # no runner in this process
    # Not scanned: the daemon's own agents, state unknown.
    assert s.diagram is not None and s.diagram.processes_scanned is False
    procs = [n for n in nodes.values() if n.kind == "process"]
    assert [p.label for p in procs] == list(diagram.KNOWN_AGENTS)
    assert {(p.health, p.state) for p in procs} == {("unknown", "not scanned")}
    assert nodes["job:core/pulse"].parent == "core:scheduler"
    assert "hourly" in nodes["job:core/pulse"].text


# --- the graph from manifests -------------------------------------------------------------


def test_the_graph_comes_from_the_installed_manifests(db: sqlite3.Connection) -> None:
    now = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    for m in (_real("github"), _real("linear"), TRADE_JEV):
        _install(db, m, now)
    repos.add(db, "jhe/trade-jev", base="main", source="test", name="trade-jev")
    repos.add(db, "jhe/dori", base="main", source="test", name="dori")
    kv_set(db, "health.ext:linear", {"state": "degraded", "since": iso(now), "detail": "slow"})
    s = _build(db, now, launch_agents=AGENTS)
    nodes = _nodes(s)
    assert s.diagram is not None
    edges = {(e.source, e.target) for e in s.diagram.edges}

    linear = nodes["ext:linear"]
    assert linear.text.startswith("linear: MCP connector to mcp.linear.app; 5 reads run without")
    assert "2 writes ask first" in linear.text and linear.health == "degraded"
    assert linear.check == "ext:linear"
    assert nodes["mcp:linear/linear"].text.startswith("linear/linear: hosted MCP server at "
                                                      "mcp.linear.app")  # fmt: skip
    save = nodes["tool:linear/linear/save_issue"]
    assert save.parent == "ext:linear" and save.tags == ["mcp", "write", "L2", "asks first"]
    assert save.text.startswith("linear/save_issue: hosted MCP (mcp.linear.app) write tool, asks")
    read = nodes["tool:linear/linear/list_issues"]
    assert "read tool, runs without asking" in read.text
    assert ("mcp:linear/linear", save.id) in edges and ("ext:linear", "mcp:linear/linear") in edges

    github = nodes["ext:github"]
    assert github.text.startswith("github: local CLI tools; 3 reads run without asking")
    assert "1 write runs, then tells you, 1 write asks first, 2 scheduled jobs" in github.text
    watch = nodes["job:github/watch"]
    assert watch.text.startswith("github/watch: every 10 min job (read, runs without asking)")
    assert (watch.ext, watch.job) == ("github", "watch")

    job = nodes["job:trade-jev/close-summary"]
    assert job.text == (
        "trade-jev/close-summary: weekdays 16:10 EST job (read, runs without asking): reads "
        "paper.db and adds the day's paper P&L to the hourly text."
    )
    assert nodes["tool:trade-jev/flatten"].tags[-1] == "always asks for yes <code>"
    assert nodes["repo:trade-jev/jhe/trade-jev"].parent == "ext:trade-jev"

    # Every extension hangs off the executor (tools) and, with jobs, the scheduler and pulse.
    for name in ("github", "linear", "trade-jev"):
        assert ("core:executor", f"ext:{name}") in edges
    assert ("core:scheduler", "ext:github") in edges and ("ext:github", "core:pulse") in edges
    assert ("core:scheduler", "ext:linear") not in edges

    # Processes: the daemon's own, what trade-jev declares (running or not), com.<repo>.* for
    # a registered repo; never the owner's other agents.
    procs = {n.label: n for n in nodes.values() if n.kind == "process"}
    assert set(procs) == {
        "com.resonant.daemon", "com.resonant.ollama", "com.resonant.caffeinate",
        "com.tradejev.paper", "com.tradejev.view", "com.tradejev.tvtoken", "com.dori.sync",
    }  # fmt: skip
    assert (procs["com.resonant.daemon"].health, procs["com.resonant.daemon"].state) == (
        "ok", "running")  # fmt: skip
    assert procs["com.resonant.caffeinate"].state == "not loaded"
    assert procs["com.tradejev.view"].state == "not loaded"  # declared, not installed
    assert procs["com.tradejev.tvtoken"].state == "idle"
    assert procs["com.tradejev.paper"].text == "The paper trading loop, every minute."
    assert procs["com.tradejev.paper"].ext == "trade-jev"
    assert ("ext:trade-jev", "proc:com.tradejev.paper") in edges
    assert ("proc:com.resonant.ollama", "core:router") in edges
    text = s.model_dump_json()
    assert "private-thing" not in text and "Safari" not in text and "postgresql" not in text

    # Every edge joins two nodes, and every child's parent is a node.
    for e in s.diagram.edges:
        assert e.source in nodes and e.target in nodes
    assert all(n.parent in nodes for n in nodes.values() if n.parent)


def test_health_and_last_runs_come_from_the_store(db: sqlite3.Connection) -> None:
    now = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    _install(db, TRADE_JEV, now)
    ext_store.record_run(db, "trade-jev", "close-summary", at=now, outcome="failed", ok=False)
    kv_set(db, "health.imessage", {"state": "degraded", "since": iso(now), "detail": "no db"})
    nodes = _nodes(_build(db, now))
    job = nodes["job:trade-jev/close-summary"]
    assert job.last_run_at == now and job.last_outcome == "failed" and job.health == "degraded"
    assert nodes["ext:trade-jev"].last_run_at == now
    assert nodes["core:gateway"].health == "degraded" and nodes["core:gateway"].check == "imessage"


def test_old_manifests_without_the_new_fields_still_draw(db: sqlite3.Connection) -> None:
    """An extension installed before PER-277 has no job descriptions or launch agents."""
    now = utcnow()
    _install(db, {"name": "old", "jobs": [{"id": "x", "schedule": "5 4 * * *"}]}, now)
    nodes = _nodes(_build(db, now))
    assert nodes["job:old/x"].text.startswith("old/x: daily 04:05")
    assert nodes["ext:old"].text == "old: no tools; 1 scheduled job."


# --- launchd ------------------------------------------------------------------------------

LISTING = (
    "PID\tStatus\tLabel\n"
    "412\t0\tcom.resonant.daemon\n"
    "-\t0\tcom.tradejev.tvtoken\n"
    "-\t78\tcom.tradejev.paper\n"
    "9\t0\tcom.apple.thing\n"
    "x\ty\tbad label with spaces\n"
)


def test_the_scan_reads_names_and_launchctl_only(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.resonant.daemon.plist").write_text("<plist>SECRET PATH</plist>")
    (agents / "com.tradejev.view.plist").write_text("x")
    (agents / "not a label!.plist").write_text("x")
    found = diagram.scan_launch_agents(agents, lambda: LISTING)
    assert found is not None
    by = {a.label: a for a in found}
    assert set(by) == {
        "com.resonant.daemon", "com.tradejev.view", "com.tradejev.tvtoken",
        "com.tradejev.paper", "com.apple.thing",
    }  # fmt: skip
    assert by["com.resonant.daemon"] == AgentState("com.resonant.daemon", True, True, True, 0)
    assert by["com.tradejev.view"] == AgentState("com.tradejev.view", True, False, False, None)
    assert diagram.agent_health(by["com.tradejev.paper"]) == ("degraded", "exited 78")
    assert diagram.agent_health(by["com.tradejev.tvtoken"]) == ("ok", "idle")
    # Neither source readable: not scanned.
    assert diagram.scan_launch_agents(tmp_path / "missing", lambda: None) is None


def test_the_test_suite_never_asks_the_real_launchctl() -> None:
    """conftest stubs it: a test's agents come from its temp HOME only."""
    assert not diagram.cached_scan()


@pytest.mark.parametrize(
    ("label", "allowed"),
    [
        ("com.resonant.daemon", True),
        ("com.resonant.anything", True),
        ("com.tradejev.declared", True),
        ("com.tradejev.paper", True),  # repo trade-jev
        ("com.dori.sync", True),  # repo dori (four letters, the shortest that counts)
        ("com.apple.Safari", False),
        ("com.owner.personal", False),
        ("org.tradejev.paper", False),
        ("com.tradejevx.paper", False),
        ("homebrew.mxcl.ollama", False),
        ("com.apple.Safari", False),  # a repo named apple claims nothing
        ("com.docker.helper", False),
        ("com.abc.x", False),  # too short a repo name
    ],
)
def test_only_known_agents_are_allowed(label: str, allowed: bool) -> None:
    repo_names = ["trade-jev", "dori", "apple", "docker", "abc"]
    assert diagram.agent_allowed(label, ["com.tradejev.declared"], repo_names) is allowed


# --- sentences ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "tz", "words"),
    [
        ("*/10 * * * *", None, "every 10 min"),
        ("0 * * * *", None, "hourly"),
        ("15 * * * *", None, "hourly at :15"),
        ("50 7 * * *", None, "daily 07:50"),
        ("10 16 * * 1-5", "America/New_York", "weekdays 16:10 EST"),
        ("0 9 * * 1", "UTC", "Mondays 09:00 UTC"),
        ("* * * * *", None, "every minute"),
        ("0 9 1 * *", None, "cron 0 9 1 * *"),
        ("nonsense", None, "on an unreadable schedule"),
        ("0 9 * * *", "Not/AZone", "daily 09:00"),
        ("0 9 * * *", "../../etc/passwd", "daily 09:00"),
    ],
)
def test_cron_words(expr: str, tz: str | None, words: str) -> None:
    assert diagram.cron_words(expr, tz, datetime(2026, 11, 2, 12, tzinfo=UTC)) == words


# --- redaction, size, trim ------------------------------------------------------------------

SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
RC = "https://claude.ai/code/session_" + "AbC123xyz"


def test_descriptions_are_redacted_on_both_tiers(db: sqlite3.Connection) -> None:
    now = utcnow()
    leak = f"uses {SECRET} see {RC} ok"
    manifest = json.loads(json.dumps(TRADE_JEV))
    manifest["description"] = leak
    manifest["jobs"][0]["description"] = leak
    manifest["tools"]["positions"]["description"] = leak
    manifest["launch_agents"][0]["description"] = leak
    _install(db, manifest, now)
    s = _build(db, now, launch_agents=AGENTS)
    for tier in (s, to_remote(s)):
        text = json.dumps(tier.model_dump(mode="json")["diagram"])
        assert "uses [redacted] see [remote control link] ok" in text
        assert "A1b2C3d4" not in text and "claude.ai/code" not in text
        assert redact_secrets(text) == text


def test_the_diagram_is_on_the_remote_tier_whole(db: sqlite3.Connection) -> None:
    now = utcnow()
    _install(db, _real("linear"), now)
    s = _build(db, now, launch_agents=AGENTS)
    remote = to_remote(s)
    assert remote.diagram == s.diagram  # nothing in it is local-only
    data = remote.model_dump(mode="json")
    Snapshot.model_validate(data)
    assert pusher_mod.data_size(data) < 64 * 1024


def test_the_trim_drops_diagram_children_and_keeps_edges_whole(db: sqlite3.Connection) -> None:
    now = utcnow()
    for m in (_real("github"), _real("linear"), TRADE_JEV):
        _install(db, m, now)
    data = to_remote(_build(db, now, launch_agents=AGENTS)).model_dump(mode="json")
    step1 = json.loads(json.dumps(data))
    pusher_mod._cut_history(step1)  # pyright: ignore[reportPrivateUsage]
    kids = [n for n in step1["diagram"]["nodes"] if n["kind"] in ("tool", "job", "repo")]
    assert kids and all(n["text"] == "" for n in kids)
    Snapshot.model_validate(step1)
    pusher_mod._cut_detail(step1)  # pyright: ignore[reportPrivateUsage]
    kinds = {n["kind"] for n in step1["diagram"]["nodes"]}
    assert kinds == {"core", "process", "extension", "job"}
    assert all(n["ext"] == "core" for n in step1["diagram"]["nodes"] if n["kind"] == "job")
    ids = {n["id"] for n in step1["diagram"]["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in step1["diagram"]["edges"])
    Snapshot.model_validate(step1)


def test_a_full_install_is_capped_flagged_and_still_fits(db: sqlite3.Connection) -> None:
    """Many big extensions: the diagram stops at its caps, says so, and the relay's shrink
    still brings the whole snapshot under its cap as a valid snapshot."""
    from resonant.dashboard import snapshot as snap

    now = utcnow()
    for i in range(20):
        name = f"e{i:02d}".ljust(60, "x")
        _install(db, {
            "name": name, "description": "d" * 120,
            "repos": [f"o/{'r' * 90}{k}" for k in range(20)],
            "jobs": [{"id": f"j{k}", "schedule": "*/5 * * * *", "description": "x" * 160}
                     for k in range(5)],
            "tools": {f"t{k}".ljust(64, "x"): {"effect": "write", "description": "y" * 300}
                      for k in range(40)},
        }, now)  # fmt: skip
    s = _build(db, now, launch_agents=AGENTS)
    assert s.diagram is not None and s.diagram.truncated
    assert len(s.diagram.nodes) == snap.DIAGRAM_NODES
    assert len(s.diagram.edges) <= snap.DIAGRAM_EDGES
    out = pusher_mod.shrink(to_remote(s).model_dump(mode="json"))
    assert pusher_mod.data_size(out) <= pusher_mod.MAX_BYTES
    Snapshot.model_validate(out)


def test_the_briefing_can_skip_the_diagram(db: sqlite3.Connection) -> None:
    assert _build(db, utcnow(), diagram=False).diagram is None

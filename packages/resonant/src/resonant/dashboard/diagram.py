"""The architecture diagram's parts (PER-277, PER-278): the curated core skeleton and the
high-level groups the Diagram tab starts from, the launchd scan and the plain-words sentences.
``dashboard.snapshot`` assembles them, with the extensions from the installed manifests and
health and last runs from the store, into ``Snapshot.diagram``.

Nothing here is hand-kept per install. The only curated part is :data:`CORE`, Resonant's own
skeleton (the boxes of docs/architecture.md), and :data:`CORE_EDGES` between them; a test
checks every component's ``module`` still exists and docs/architecture.md still names it, so a
rename or removal fails it instead of leaving a stale box. Extensions, their tools, jobs, MCP
servers, repos and launchd agents all come from the approved manifests, the repo registry and
the scan.

The launchd scan (:func:`scan_launch_agents`) reads plist file NAMES in ~/Library/LaunchAgents
and ``launchctl list`` (label, pid, last exit), never a plist's contents (paths, arguments,
environment). It returns every label it saw, in memory only: :func:`agent_allowed` keeps
Resonant's own (``com.resonant.*``), the labels an extension declares (``launch_agents`` in its
manifest) and labels under a registered repo's name (``com.<repo>.*``, the repo name without
punctuation: repo ``trade-jev`` knows ``com.tradejev.paper``); the owner's other personal agents
never reach the snapshot.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from resonant.launchd import CAFFEINATE_LABEL, DAEMON_LABEL, OLLAMA_LABEL, agents_dir


@dataclass(frozen=True)
class CoreComponent:
    id: str  # the node is ``core:<id>``
    label: str
    module: str  # where it lives, repo-relative (``resonant/...`` is under packages/resonant/src)
    text: str  # one sentence on its job
    check: str | None = None  # its health check (``monitor.CHECKS`` or ``relay``)
    group: str = ""  # the top-level box it sits in (a :data:`GROUPS` id)


@dataclass(frozen=True)
class DiagramGroupDef:
    id: str
    label: str
    text: str  # one sentence on what the group does


# The high-level boxes the Diagram tab starts from (PER-278), in flow order. Every core
# component names one; core jobs sit in ``scheduler``, launchd agents in ``processes`` and
# every extension (its tools, jobs, MCP servers and repos nest under it) in ``extensions``.
GROUPS: Final[tuple[DiagramGroupDef, ...]] = (
    DiagramGroupDef(
        "messages", "Messages in and out",
        "Texts come in over iMessage, the event loop queues them and the qwen router triages "
        "them; replies, approval asks, pulses and briefings go back out the same way.",
    ),
    DiagramGroupDef(
        "brain", "Claude brain",
        "One long-lived Claude Code session answers every text the router hands on and calls "
        "Resonant's tools through the gate.",
    ),
    DiagramGroupDef(
        "toolcore", "Gate and tool core",
        "Every tool call takes one path: the registry, the gate (which asks you by text when it "
        "must) and the executor, which runs it once and audits it.",
    ),
    DiagramGroupDef(
        "workers", "Workers",
        "Claude Code runs on their own branch copies of a repo; they push PRs and report back.",
    ),
    DiagramGroupDef(
        "scheduler", "Scheduler and jobs",
        "In-daemon cron for Resonant's own jobs and every extension's jobs, all through the "
        "tool core.",
    ),
    DiagramGroupDef(
        "extensions", "Extensions",
        "What you installed: each extension with its MCP servers, tools, jobs and repos, from "
        "the manifest you approved.",
    ),
    DiagramGroupDef(
        "store", "Store and backups",
        "One SQLite file holds tasks, approvals, runs and the audit log, copied every day.",
    ),
    DiagramGroupDef(
        "relay", "Dashboard and relay",
        "This page, served on the Mini and, from the snapshot pushed to Supabase every minute, "
        "by the Fly app that also watches for silence.",
    ),
    DiagramGroupDef(
        "processes", "launchd agents",
        "The Mac's launchd agents Resonant knows: its own, those an extension declares and those "
        "named after a registered repo.",
    ),
)  # fmt: skip
GROUP_IDS: Final = frozenset(g.id for g in GROUPS)


# The skeleton, in the order a text flows through it. Each ``module`` is named in
# docs/architecture.md (tests/test_dashboard_diagram.py checks both).
CORE: Final[tuple[CoreComponent, ...]] = (
    CoreComponent(
        "gateway", "iMessage gateway", "resonant/gateway/imessage/",
        "Reads new texts from Messages' chat.db and sends Resonant's replies, approval asks, "
        "pulses and briefings back over iMessage.",
        check="imessage",
        group="messages",
    ),
    CoreComponent(
        "loop", "Event loop", "resonant/loop/",
        "Turns every text, job and worker event into a durable task and runs the tasks by "
        "priority within the slot pool.",
        check="loop",
        group="messages",
    ),
    CoreComponent(
        "router", "qwen router", "resonant/router/",
        "The local qwen3 model on Ollama triages each text: it answers a greeting or ack "
        "itself, builds the news check in code and hands everything else to Claude; it never "
        "writes a fact.",
        check="model",
        group="messages",
    ),
    CoreComponent(
        "brain", "Claude brain", "resonant/runners/brain.py",
        "One long-lived Claude Code session on the subscription answers every other text in "
        "Resonant's one voice and calls Resonant's tools through the gate.",
        group="brain",
    ),
    CoreComponent(
        "toolcore", "Tool core", "resonant/toolcore/",
        "The one call path for the brain, jobs and watches: the registry, then the gate, then "
        "the executor; a boundary test keeps every call on it.",
        group="toolcore",
    ),
    CoreComponent(
        "gate", "Gate and policy", "resonant/gate/",
        "Weighs each call's level against who chose it, applies trust rules and standing "
        "allows, and asks the owner by text (yes, yes always or yes <code>) when it must.",
        group="toolcore",
    ),
    CoreComponent(
        "executor", "Executor", "resonant/executor.py",
        "Runs an allowed call once (idempotency key), reads its Keychain secrets just before "
        "the call and writes the audit row.",
        group="toolcore",
    ),
    CoreComponent(
        "scheduler", "Scheduler", "resonant/extensions/scheduler.py",
        "In-daemon cron: runs Resonant's own jobs (pulse, briefing, backups, prune, MCP drift) "
        "and every extension's jobs through the tool core; due runs are skipped while paused.",
        group="scheduler",
    ),
    CoreComponent(
        "workers", "Claude workers", "resonant/runners/claude.py",
        "Claude Code runs, each on its own branch copy of a repo in ~/code or a scratch dir, push "
        "PRs with a one-hour GitHub App token for one repo and ask or note over the worker desk.",
        group="workers",
    ),
    CoreComponent(
        "pulse", "Reports, pulse and briefing", "resonant/extensions/pulse_job.py",
        "Collects run outcomes and extension reports as pulse items: urgent ones text at once, "
        "the rest go in the hourly pulse or the 08:00 briefing.",
        group="messages",
    ),
    CoreComponent(
        "store", "Store (SQLite)", "resonant/store/",
        "One SQLite file (WAL) with tasks, events, approvals, runs, trust rules, repos, pulse "
        "items, the audit log and kv state.",
        check="store",
        group="store",
    ),
    CoreComponent(
        "backups", "Backups", "resonant/backup.py",
        "core/backup copies the store every day with SQLite's online backup and keeps 7 daily "
        "and 4 weekly copies; core/prune trims old rows after a good backup.",
        check="backup",
        group="store",
    ),
    CoreComponent(
        "dashboard", "Local dashboard", "resonant/api.py",
        "Serves this page and /api/dashboard on 127.0.0.1:7777, loopback only.",
        group="relay",
    ),
    CoreComponent(
        "relay", "Relay pusher", "resonant/dashboard/pusher.py",
        "Pushes the redacted remote snapshot to Supabase every minute; nothing connects into "
        "the Mini.",
        check="relay",
        group="relay",
    ),
    CoreComponent(
        "supabase", "Supabase", "supabase/",
        "Keeps the latest snapshot behind an insert-only RPC that refuses secret-shaped text; "
        "only the owner, with password and TOTP, can read it.",
        group="relay",
    ),
    CoreComponent(
        "fly", "Fly dashboard and dead-man switch", "relay/",
        "The Fly app serves this page from Supabase and checks the newest snapshot every "
        "minute; a stale snapshot or a failing check alerts the owner on ntfy.",
        group="relay",
    ),
)  # fmt: skip

# How the core talks: (source, target, label), core ids.
CORE_EDGES: Final[tuple[tuple[str, str, str], ...]] = (
    ("gateway", "loop", "text in"),
    ("loop", "router", "triage"),
    ("router", "gateway", "greeting / ack"),
    ("router", "brain", "everything else"),
    ("brain", "toolcore", "tool call"),
    ("toolcore", "gate", "check"),
    ("gate", "executor", "allowed"),
    ("gate", "gateway", "approval ask"),
    ("executor", "workers", "start run"),
    ("scheduler", "toolcore", "jobs"),
    ("workers", "pulse", "run reports"),
    ("pulse", "gateway", "texts out"),
    ("brain", "gateway", "answer"),
    ("executor", "store", "audit"),
    ("store", "backups", "daily copy"),
    ("store", "dashboard", "snapshot"),
    ("store", "relay", "snapshot"),
    ("relay", "supabase", "push / min"),
    ("supabase", "fly", "read"),
)  # fmt: skip

# The daemon's own launchd agents (``resonant daemon install``), always shown.
KNOWN_AGENTS: Final[Mapping[str, str]] = {
    DAEMON_LABEL: "The Resonant daemon: one Python process with the loop, gate, scheduler and "
    "local API; launchd starts it at login and restarts it if it exits.",
    OLLAMA_LABEL: "Ollama serving the local qwen model the router uses, kept loaded in memory.",
    CAFFEINATE_LABEL: "caffeinate -i -m -s keeps the Mini awake on AC power so the daemon "
    "never sleeps.",
}
# Which core node a known agent runs (an edge agent -> core).
AGENT_RUNS: Final[Mapping[str, str]] = {DAEMON_LABEL: "loop", OLLAMA_LABEL: "router"}

# What each core job does (``core_jobs`` ids; an unknown one gets its schedule only).
CORE_JOB_TEXT: Final[Mapping[str, str]] = {
    "pulse": "sends the hourly pulse: what you haven't heard yet, in one short text",
    "briefing": "sends the morning briefing with everything still open",
    "watches": "runs the watches you set up by text",
    "remind": "re-asks approvals halfway to their expiry",
    "mcp": "re-checks MCP tool lists for drift and applies the answers to repin asks",
    "backup": "copies the store and checks the copy",
    "prune": "trims old rows from the store after a good backup",
    "run-cleanup": "removes old run folders and their branch checkouts",
}


# --- launchd ------------------------------------------------------------------------------

LABEL_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_LABEL = re.compile(LABEL_PATTERN)
_LAUNCHCTL = "/bin/launchctl"  # SIP-protected; never looked up by name


@dataclass(frozen=True)
class AgentState:
    """One launchd agent as the scan saw it (in memory only; filtered before it ships)."""

    label: str
    plist: bool  # a plist file of this name in ~/Library/LaunchAgents
    loaded: bool  # in ``launchctl list``
    running: bool  # it has a pid
    last_exit: int | None = None


def _launchctl_list() -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, absolute binary
            [_LAUNCHCTL, "list"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def scan_launch_agents(
    directory: Path | None = None, listing: Callable[[], str | None] | None = None
) -> list[AgentState] | None:
    """Every agent label in ``directory`` (plist names) or ``launchctl list``; None when
    neither could be read (not a Mac, say)."""
    folder = agents_dir() if directory is None else directory
    plists: set[str] = set()
    try:
        for path in folder.glob("*.plist"):
            if _LABEL.fullmatch(path.stem):
                plists.add(path.stem)
        seen_dir = folder.is_dir()
    except OSError:
        seen_dir = False
    raw = (listing or _launchctl_list)()
    loaded: dict[str, tuple[bool, int | None]] = {}
    for line in (raw or "").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 3 or not _LABEL.fullmatch(parts[2]):
            continue
        pid, status, label = parts
        try:
            code: int | None = int(status)
        except ValueError:
            code = None
        loaded[label] = (pid.strip() not in ("", "-"), code)
    if raw is None and not seen_dir:
        return None
    return [
        AgentState(
            label=label,
            plist=label in plists,
            loaded=label in loaded,
            running=loaded.get(label, (False, None))[0],
            last_exit=loaded.get(label, (False, None))[1],
        )
        for label in sorted(plists | set(loaded))
    ]


_cache_lock = threading.Lock()
_cache: tuple[float, list[AgentState] | None] | None = None
SCAN_TTL_S: Final = 60.0


def cached_scan(ttl: float = SCAN_TTL_S) -> list[AgentState] | None:
    """:func:`scan_launch_agents`, at most once per ``ttl`` seconds per process (the page
    polls every 10 s; launchd state is not that urgent)."""
    global _cache
    with _cache_lock:
        now = time.monotonic()
        if _cache is None or now - _cache[0] > ttl:
            _cache = (now, scan_launch_agents())
        return _cache[1]


def _squash(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


# A repo name that is a vendor's (``com.apple.*``, ``com.docker.*``) or too short to mean
# anything never claims agents.
_VENDORS: Final = frozenset({
    "apple", "google", "microsoft", "docker", "adobe", "mozilla", "brave", "spotify",
    "dropbox", "zoom", "slack", "jetbrains", "github", "openssh", "homebrew", "oracle",
})  # fmt: skip
_MIN_REPO_KEY = 4


def agent_allowed(label: str, declared: Iterable[str], repo_names: Iterable[str]) -> bool:
    """Resonant's own agents, the ones an extension declares, and ``com.<repo>.*`` for a
    registered repo; never any other agent of the owner's."""
    if label.startswith("com.resonant.") or label in KNOWN_AGENTS or label in set(declared):
        return True
    parts = label.split(".")
    if len(parts) < 3 or parts[0] != "com":
        return False
    key = parts[1].lower()
    if len(key) < _MIN_REPO_KEY or key in _VENDORS:
        return False
    return any(key == _squash(r) for r in repo_names)


def agent_health(a: AgentState | None) -> tuple[str, str]:
    """(health, state) of an agent: running is ok; loaded and idle after a clean exit is ok
    (a calendar job between runs); a non-zero last exit is degraded."""
    if a is None:
        return "unknown", "not loaded"
    if a.running:
        return "ok", "running"
    if not a.loaded:
        return "degraded", "not loaded"
    if a.last_exit not in (None, 0):
        return "degraded", f"exited {a.last_exit}"
    return "ok", "idle"


# --- sentences ----------------------------------------------------------------------------

_DOW: Final = {
    "*": "daily", "1-5": "weekdays", "MON-FRI": "weekdays", "0,6": "weekends",
    "6,0": "weekends", "SAT,SUN": "weekends", "0": "Sundays", "7": "Sundays", "1": "Mondays",
    "2": "Tuesdays", "3": "Wednesdays", "4": "Thursdays", "5": "Fridays", "6": "Saturdays",
}  # fmt: skip


def _zone_abbr(tz: str | None, now: datetime) -> str:
    if not tz:
        return ""
    try:
        name = now.astimezone(ZoneInfo(tz)).tzname() or ""
    except Exception:  # any odd stored string is just not named
        return ""
    return f" {name}" if re.fullmatch(r"[A-Za-z]{2,5}|[+-]\d{2,4}", name) else ""


def cron_words(expr: str | None, tz: str | None, now: datetime) -> str:
    """A 5-field cron in words where it is a common shape (``every 10 min``, ``daily 07:50``,
    ``weekdays 16:10 EST``), else ``cron <expr>``. Like ``extensions.manager.schedule_words``
    (the install screen's), plus single weekdays and the zone's abbreviation; that module is
    too heavy an import for the snapshot."""
    fields = (expr or "").split()
    if len(fields) != 5:
        return "on an unreadable schedule"
    minute, hour, dom, month, dow = fields
    zone = _zone_abbr(tz, now)
    if dom == "*" and month == "*":
        m = re.fullmatch(r"\*/(\d{1,2})", minute)
        if m and hour == "*" and dow == "*":
            return f"every {int(m.group(1))} min"
        if minute.isdigit() and hour == "*" and dow == "*":
            return "hourly" if int(minute) == 0 else f"hourly at :{int(minute):02d}"
        if minute.isdigit() and hour.isdigit() and dow.upper() in _DOW:
            return f"{_DOW[dow.upper()]} {int(hour):02d}:{int(minute):02d}{zone}"
        if minute == "*" and hour == "*" and dow == "*":
            return "every minute"
    return f"cron {expr}{zone}"


def level_words(effect: str, level: str) -> str:
    """What the gate does with a call the model or a job chose, at ``level``."""
    if level == "L0":
        return "runs without asking"
    if level == "L3":
        return "runs, then tells you"
    if level == "L1":
        return "always asks for yes <code>"
    return "asks first"


def tool_kind(via: str, transport: str | None, host: str | None) -> str:
    if via == "cli":
        return "local CLI"
    if transport == "http":
        return f"hosted MCP ({host})" if host else "hosted MCP"
    return "local MCP"


def extension_sentence(
    name: str,
    description: str | None,
    tools: Sequence[tuple[str, str, str]],  # (effect, level, kind)
    hosts: Sequence[str],
    jobs: int,
) -> str:
    """``linear: MCP connector to mcp.linear.app; 5 reads run without asking, 2 writes ask
    first. <description>``"""
    kinds: list[str] = []
    if hosts:
        kinds.append("MCP connector to " + ", ".join(hosts))
    if any(k == "local MCP" for _e, _l, k in tools):
        kinds.append("local MCP server")
    if any(k == "local CLI" for _e, _l, k in tools):
        kinds.append("local CLI tools")
    groups: dict[str, list[str]] = {}
    for effect, level, _kind in tools:
        groups.setdefault(level_words(effect, level), []).append(effect)
    counts: list[str] = []
    for words in ("runs without asking", "runs, then tells you", "asks first",
                  "always asks for yes <code>"):  # fmt: skip
        effects = groups.get(words)
        if not effects:
            continue
        n = len(effects)
        noun = effects[0] if len(set(effects)) == 1 else "tool"
        verb = words if n == 1 else _plural_verb(words)
        counts.append(f"{n} {noun}{'' if n == 1 else 's'} {verb}")
    if jobs:
        counts.append(f"{jobs} scheduled job{'s' if jobs != 1 else ''}")
    head = f"{name}: {' and '.join(kinds) or 'no tools'}"
    if counts:
        head += "; " + ", ".join(counts)
    desc = (description or "").strip().rstrip(".")
    return f"{head}. {desc}." if desc else f"{head}."


def _plural_verb(words: str) -> str:
    return words.replace("runs", "run").replace("asks", "ask").replace("tells", "tell")


def tool_sentence(
    ext: str, name: str, kind: str, effect: str, level: str, description: str | None
) -> str:
    head = f"{ext}/{name}: {kind} {effect} tool, {level_words(effect, level)}"
    desc = (description or "").strip().rstrip(".")
    return f"{head}. {desc}." if desc else f"{head}."


def job_sentence(
    owner: str,
    job_id: str,
    schedule: str | None,
    tz: str | None,
    effect: str | None,
    level: str | None,
    what: str | None,
    now: datetime,
) -> str:
    when = cron_words(schedule, tz, now) if schedule else "scheduled"
    head = f"{owner}/{job_id}: {when} job"
    if effect and level and owner != "core":
        head += f" ({effect}, {level_words(effect, level)})"
    desc = (what or "").strip().rstrip(".")
    return f"{head}: {desc}." if desc else f"{head}."

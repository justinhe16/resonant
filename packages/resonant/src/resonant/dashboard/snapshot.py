"""The dashboard snapshot (docs/dashboard-plan.md §3): Pydantic models and the builder.

One schema, two tiers (plan D3):

- ``local`` is served on loopback only (``GET /api/dashboard``). It may carry text: run asks,
  PR links, approval and question texts, pulse titles, trust rules and repos.
- ``remote`` is what may leave the Mini (the relay, DB-3). It is a strict subset: health,
  counts, states, identifiers and times only. Each local-only list of texts (``runs.items``,
  ``approvals.items``, ...) has a ``brief`` beside it on both tiers: one row per item with its
  code, kind, state and times and none of its text, so the remote page can list them too.

Every field that only the local tier carries is declared with :data:`LOCAL_ONLY` (it shows as
``"x-tier": "local"`` in the JSON schema), defaults to None and is left out of the output when
None. :func:`to_remote` clears every such field, at any depth, so a new local field is covered
by declaring it, not by remembering a filter. Every string the remote tier keeps is a
``Literal``, a ``datetime`` or matches a short identifier pattern, so it can't hold free text;
``tests/test_dashboard_snapshot.py`` checks both over the schema and over random data.

``docs/snapshot.schema.json`` is generated from :class:`Snapshot`:
``uv run python -m resonant.dashboard.snapshot > docs/snapshot.schema.json``. A test fails
when it is stale. ``dashboard/src/types.ts`` (DB-2) mirrors it.

The builder only reads the store (and the live state the caller passes in). It never reads a
secret's value: the runner's gaps come from ``ClaudeRunner.setup_gaps``, by name.
"""

from __future__ import annotations

import functools
import json
import re
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal, cast, get_args
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic.config import JsonDict

from resonant import __version__
from resonant.config import Settings
from resonant.extensions.cron import CronError, parse_schedule
from resonant.loop.slots import SlotPool
from resonant.loop.types import TaskStatus
from resonant.monitor import EXT_CHECK_PREFIX, health_snapshot
from resonant.store import approvals, pulse, questions, repos, runs, trust
from resonant.store import extensions as ext_store
from resonant.store import tasks as task_store
from resonant.store.claude_brain import recent_tools, tool_counts
from resonant.store.claude_brain import status_of as claude_status
from resonant.store.db import kv_get, utcnow
from resonant.store.extensions import ExtensionStatus
from resonant.store.pulse import Urgency
from resonant.store.repos import Mode
from resonant.store.runs import Status as RunStatus
from resonant.store.runs import WaitReason

SCHEMA_VERSION: Final = 1
RUN_ITEMS = 50  # active runs first, then the most recent (``runs.recent``)
BRAIN_TOOL_ITEMS = 20  # the Claude brain's last tool calls (PER-258)
# The briefs' size caps, so the remote tier stays well under the relay's 30 KiB: the first
# runs (active first, as ``runs.recent`` orders them), the first open approvals and questions,
# and the most urgent open pulse items. ``tests/test_dashboard_snapshot.py`` fills every one.
# All of them full take about 10 KB (about 18 KB with ten extensions, in the test); the pusher
# drops them if a snapshot is still too large.
RUN_BRIEF_ITEMS = 20
APPROVAL_BRIEF_ITEMS = 10
QUESTION_BRIEF_ITEMS = 10
PULSE_BRIEF_ITEMS = 20
BRAIN_BRIEF_ITEMS = 10  # ``brain_activity.recent``: the newest of ``claude_brain.tools``

# A field only the local tier carries. ``to_remote`` clears it; None is left out of the output.
LOCAL_ONLY: Final[JsonDict] = {"x-tier": "local"}

Tier = Literal["local", "remote"]
HealthState = Literal["ok", "degraded", "failing", "unknown"]
# What ``Scheduler`` records (``_Result.outcome``); anything else shows as ``other``.
JobOutcome = Literal["ok", "failed", "killed", "denied", "dry_run", "skipped_paused", "other"]
PulseState = Literal["new", "told"]  # new: the owner hasn't heard this version yet

# Identifiers the remote tier may carry: health check names (``model``, ``ext:github``),
# extension names and job ids (the SDK's slugs, owner-chosen but never message content).
# A row whose identifier doesn't match is left out of the snapshot, never an error.
IDENT_PATTERN: Final = r"^[a-z][a-z0-9:_-]{0,63}$"
Ident = Annotated[str, StringConstraints(pattern=IDENT_PATTERN)]
# A run or watch code (``R3``, ``W2``): a letter and a counter, never content.
CODE_PATTERN: Final = r"^[A-Z][0-9]{1,6}$"
Code = Annotated[str, StringConstraints(pattern=CODE_PATTERN)]
# A git sha (8 hex) or the package version when there is no git checkout.
VERSION_PATTERN: Final = r"^[0-9A-Za-z.+-]{1,40}$"
Version = Annotated[str, StringConstraints(pattern=VERSION_PATTERN)]

# The relay's own health row (``resonant.dashboard.pusher``): {state, since, detail}. Unlike the
# monitor's checks it can be ``failing``. Absent while the relay is off.
RELAY_HEALTH_KV: Final = "health.relay"
RELAY_CHECK: Final = "relay"

_GIT = "/usr/bin/git"  # SIP-protected; never looked up by name
_SHA = re.compile(r"[0-9a-f]{40}")
_IDENT = re.compile(IDENT_PATTERN)
_CODE = re.compile(CODE_PATTERN)
_OUTCOMES: frozenset[str] = frozenset(get_args(JobOutcome))
_HEALTH_STATES: frozenset[str] = frozenset(get_args(HealthState))
_TASK_STATES: frozenset[str] = frozenset(get_args(TaskStatus))
_RUN_STATES: frozenset[str] = frozenset(get_args(RunStatus))


def _ident(value: str) -> bool:
    return _IDENT.fullmatch(value) is not None


def _code(value: str) -> bool:
    return _CODE.fullmatch(value) is not None


def _counts(counts: Mapping[str, int], known: frozenset[str]) -> dict[Any, int]:
    """``counts`` with only the known states (a state added later never breaks the page)."""
    return {k: n for k, n in counts.items() if k in known}


def _local() -> Any:
    """A local-only field: optional, None by default and left out of the output when None."""
    return Field(default=None, exclude_if=lambda v: v is None, json_schema_extra=LOCAL_ONLY)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HealthRow(_Model):
    name: Ident
    state: HealthState
    since: datetime | None
    reason: str | None = _local()  # the check's detail (scrubbed by the monitor)


class Tasks(_Model):
    by_state: dict[TaskStatus, int]


class Slots(_Model):
    used: int
    max: int


class RunItem(_Model):
    code: str
    state: RunStatus
    repo: str  # empty for a scratch run
    ask: str
    branch: str | None
    pr_url: str | None
    pr_number: int | None
    started_at: datetime | None
    ended_at: datetime | None
    waiting_reason: WaitReason | None
    last_note: str | None
    remote_url: str | None = _local()  # RC-1: the running session's Remote Control link


class RunBrief(_Model):
    """A run on both tiers: no ask, repo, branch, note or link."""

    code: Code
    state: RunStatus
    scratch: bool  # no repo
    has_pr: bool
    started_at: datetime | None
    ended_at: datetime | None
    waiting_reason: WaitReason | None


class Runs(_Model):
    by_state: dict[RunStatus, int]
    items: list[RunItem] | None = _local()
    brief: list[RunBrief] = Field(default_factory=list[RunBrief])


class ApprovalItem(_Model):
    code: str | None
    summary: str
    level: Literal["L0", "L1", "L2", "L3"]
    asked_at: datetime
    expires_at: datetime


class ApprovalBrief(_Model):
    """A pending approval on both tiers: no summary, and not its code (the owner types it to
    say yes, so it stays on the Mini and iMessage)."""

    level: Literal["L0", "L1", "L2", "L3"]
    effect: Literal["read", "write", "pay"]
    needs_code: bool
    asked_at: datetime
    expires_at: datetime


class Approvals(_Model):
    open: int
    items: list[ApprovalItem] | None = _local()
    brief: list[ApprovalBrief] = Field(default_factory=list[ApprovalBrief])


class QuestionItem(_Model):
    run_code: str
    text: str
    options: list[str]
    asked_at: datetime


class QuestionBrief(_Model):
    """An open worker question on both tiers: whose, how many options, when. No text."""

    run_code: Code
    options: int
    asked_at: datetime


class Questions(_Model):
    open: int
    items: list[QuestionItem] | None = _local()
    brief: list[QuestionBrief] = Field(default_factory=list[QuestionBrief])


class PulseItem(_Model):
    source: str  # ``core`` or the extension that reported it
    key: str
    title: str
    urgency: Urgency
    state: PulseState
    updated_at: datetime


class PulseBrief(_Model):
    """An open pulse item on both tiers: who reported it, how urgent, when. No key or title."""

    source: Ident  # ``core``, the extension, or ``other``
    urgency: Urgency
    state: PulseState
    updated_at: datetime


class Pulse(_Model):
    open: int
    items: list[PulseItem] | None = _local()
    brief: list[PulseBrief] = Field(default_factory=list[PulseBrief])


class Job(_Model):
    id: Ident
    next_due: datetime | None  # None when the extension is disabled or the schedule is bad
    last_run_at: datetime | None
    last_outcome: JobOutcome | None
    fail_streak: int


class Extension(_Model):
    name: Ident
    status: ExtensionStatus
    health: HealthState  # its ``ext:<name>`` check
    jobs: list[Job]


class Runner(_Model):
    set_up: bool
    missing: list[str] | None = _local()  # what to set up, in words
    gaps: int = 0  # how many


class BrainTool(_Model):
    """PER-258: one of the Claude brain's tool calls (its ``brain.tool`` audit row)."""

    at: datetime
    tool: str
    decision: str  # allowed (the read list), read, gate, granted, asked, refused, denied, resonant
    command: str  # redacted and clipped by the bridge
    cwd: str
    status: Literal["ok", "error"]
    write: bool


class ClaudeBrain(_Model):
    """BR-11: the Claude brain's session (local tier only, like the rest of the claude
    details): up or down, its turns, its Remote Control link while one is on, and its last
    tool calls (PER-258)."""

    state: Literal["up", "down"]
    turns: int
    remote_url: str | None = _local()
    tools: list[BrainTool] = Field(default_factory=list[BrainTool])


BrainDecision = Literal[
    "allowed", "read", "gate", "granted", "asked", "refused", "denied", "resonant", "other"
]


class BrainToolBrief(_Model):
    """One of the Claude brain's tool calls on both tiers: the tool's kind (``bash``,
    ``read``, ``mcp``; ``other`` for a name that isn't a plain slug), the decision and the
    outcome. Never its command or directory."""

    at: datetime
    tool: Ident
    decision: BrainDecision
    status: Literal["ok", "error"]
    write: bool


class BrainActivity(_Model):
    """PER-258: the Claude brain's tool calls today (``settings.timezone``), counts only, so
    the remote tier has them too: every call, and the changes that ran. With its session's
    state and turns (None before its first start) and its last calls, briefly."""

    calls_today: int
    writes_today: int
    state: Literal["up", "down"] | None = None
    turns: int = 0
    recent: list[BrainToolBrief] = Field(default_factory=list[BrainToolBrief])


class TrustRule(_Model):
    n: int  # the rule's id, as ``/trust`` lists it
    text: str


class RepoItem(_Model):
    name: str
    mode: Mode
    base: str
    ci: bool | None  # CI seen on the repo (None: not checked yet)


class Snapshot(_Model):
    """Everything the dashboard shows, at ``taken_at``."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        serialize_by_alias=True,
        validate_by_name=True,
        validate_by_alias=True,
        title="Snapshot",
    )

    schema_: Literal[1] = Field(alias="schema")
    tier: Tier
    taken_at: datetime
    version: Version
    started_at: datetime  # when the daemon started
    last_tick: datetime | None
    autonomy: Literal["on", "off"]
    paused: bool
    dry_run: bool
    health: list[HealthRow]
    tasks: Tasks
    slots: Slots
    runs: Runs
    approvals: Approvals
    questions: Questions
    pulse: Pulse
    extensions: list[Extension]
    runner: Runner
    trust_rules: list[TrustRule] | None = _local()
    repos: list[RepoItem] | None = _local()
    claude_brain: ClaudeBrain | None = _local()  # BR-11; None before its first start
    brain_activity: BrainActivity = Field(default_factory=lambda: BrainActivity(
        calls_today=0, writes_today=0))  # fmt: skip
    trust_rule_count: int = 0  # the rules and repos themselves are local only
    repo_count: int = 0


# --- tiers --------------------------------------------------------------------------------


def is_local_only(model: type[BaseModel], field: str) -> bool:
    return model.model_fields[field].json_schema_extra == LOCAL_ONLY


def _strip(value: Any) -> Any:
    if isinstance(value, BaseModel):
        cls = type(value)
        update = {
            name: None if is_local_only(cls, name) else _strip(getattr(value, name))
            for name in cls.model_fields
        }
        return value.model_copy(update=update)
    if isinstance(value, list):
        return [_strip(v) for v in cast(list[Any], value)]
    return value


def to_remote(snapshot: Snapshot) -> Snapshot:
    """The same snapshot with every local-only field cleared, at any depth, validated again
    (``model_copy`` doesn't validate) so a pusher can only ever send a valid remote tier."""
    stripped = cast(Snapshot, _strip(snapshot)).model_copy(update={"tier": "remote"})
    return Snapshot.model_validate(stripped.model_dump())


def schema_json() -> str:
    """``docs/snapshot.schema.json``: the JSON schema of :class:`Snapshot`."""
    schema = Snapshot.model_json_schema()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": "Generated from resonant/dashboard/snapshot.py; do not edit.",
        **schema,
    }
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


# --- builder ------------------------------------------------------------------------------


@functools.cache
def code_version() -> str:
    """The checkout's git sha (8 hex), else the package version. Read once per process."""
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, absolute git
            [_GIT, "-C", str(here), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return __version__
    sha = out.stdout.strip()
    return sha[:8] if out.returncode == 0 and _SHA.fullmatch(sha) else __version__


def _health_state(raw: object) -> HealthState:
    return cast(HealthState, raw) if raw in _HEALTH_STATES else "unknown"


def _since(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _health(conn: sqlite3.Connection) -> dict[str, HealthRow]:
    rows: dict[str, HealthRow] = {}
    for name, h in health_snapshot(conn).items():
        if not _ident(name):  # a stale or hand-edited kv key
            continue
        detail = h.get("detail")
        rows[name] = HealthRow(
            name=name,
            state=_health_state(h.get("state")),
            since=_since(h.get("since")),
            reason=detail if isinstance(detail, str) and detail else None,
        )
    raw = kv_get(conn, RELAY_HEALTH_KV)
    if isinstance(raw, dict):
        relay = cast(dict[str, Any], raw)
        detail = relay.get("detail")
        rows[RELAY_CHECK] = HealthRow(
            name=RELAY_CHECK,
            state=_health_state(relay.get("state")),
            since=_since(relay.get("since")),
            reason=detail if isinstance(detail, str) and detail else None,
        )
    return rows


def _jobs(
    record: ext_store.ExtensionRecord,
    states: Mapping[str, ext_store.JobState],
    settings: Settings,
    now: datetime,
) -> list[Job]:
    jobs: list[Job] = []
    for spec in cast(list[dict[str, Any]], record.manifest.get("jobs") or []):
        job_id = str(spec.get("id", ""))
        if not _ident(job_id):
            continue
        next_due: datetime | None = None
        if record.status == "enabled":
            try:
                schedule = parse_schedule(
                    str(spec.get("schedule", "")), spec.get("tz") or settings.timezone
                )
                next_due = schedule.next_after(now)
            except (CronError, ValueError):
                next_due = None
        st = states.get(job_id)
        outcome = None if st is None else st.last_outcome
        jobs.append(
            Job(
                id=job_id,
                next_due=next_due,
                last_run_at=None if st is None else st.last_run_at,
                last_outcome=None
                if outcome is None
                else cast(JobOutcome, outcome if outcome in _OUTCOMES else "other"),
                fail_streak=0 if st is None else st.fail_streak,
            )
        )
    return jobs


def _extensions(
    conn: sqlite3.Connection,
    health: Mapping[str, HealthRow],
    settings: Settings,
    now: datetime,
) -> list[Extension]:
    out: list[Extension] = []
    for record in ext_store.list_extensions(conn):
        if not _ident(record.name):
            continue
        states = {j.job_id: j for j in ext_store.list_jobs(conn, record.name)}
        check = health.get(EXT_CHECK_PREFIX + record.name)
        out.append(
            Extension(
                name=record.name,
                status=record.status,
                health="unknown" if check is None else check.state,
                jobs=_jobs(record, states, settings, now),
            )
        )
    return out


def build_snapshot(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    started_at: datetime,
    last_tick: datetime | None = None,
    slots: SlotPool | None = None,
    runner_missing: Sequence[str] | None = None,
    now: datetime | None = None,
    version: str | None = None,
) -> Snapshot:
    """The local-tier snapshot (``to_remote`` derives the remote one).

    ``slots`` is the loop's Claude slot pool (without it: none used, ``claude.max_concurrent``
    max). ``runner_missing`` is ``ClaudeRunner.setup_gaps()``'s list, or None when no Claude
    runner is wired in this process (then the runner is not set up).
    """
    now = now or utcnow()
    health = _health(conn)
    run_rows = runs.recent(conn, limit=RUN_ITEMS)
    pending = approvals.pending_all(conn, now)
    asks = questions.open_asks(conn)
    items = pulse.open_items(conn, now)
    autonomy = kv_get(conn, "autonomy", "on")
    brain = _claude_brain(conn)
    if runner_missing is None:
        runner = Runner(
            set_up=False, missing=["no Claude runner in this daemon (iMessage is off)"], gaps=1
        )
    else:
        runner = Runner(
            set_up=not runner_missing,
            missing=list(runner_missing) or None,
            gaps=len(runner_missing),
        )
    rules = trust.list_rules(conn)
    repo_rows = repos.list_all(conn)
    return Snapshot(
        schema=SCHEMA_VERSION,
        tier="local",
        taken_at=now,
        version=version or code_version(),
        started_at=started_at,
        last_tick=last_tick,
        autonomy="off" if autonomy == "off" else "on",
        paused=kv_get(conn, "paused", False) is True,
        dry_run=settings.dry_run,
        health=list(health.values()),
        tasks=Tasks(by_state=_counts(task_store.count_by_status(conn), _TASK_STATES)),
        slots=Slots(
            used=0 if slots is None else slots.in_use,
            max=settings.claude.max_concurrent if slots is None else slots.capacity,
        ),
        runs=Runs(
            by_state=_counts(runs.count_by_status(conn), _RUN_STATES),
            items=[
                RunItem(
                    code=r.code,
                    state=r.status,
                    repo=r.repo,
                    ask=r.ask,
                    branch=r.branch,
                    pr_url=r.pr_url,
                    pr_number=r.pr_number,
                    started_at=r.started_at,
                    ended_at=r.ended_at,
                    waiting_reason=r.wait_reason if r.status == "waiting" else None,
                    last_note=r.last_note,
                    remote_url=r.remote_url if r.status == "running" else None,
                )
                for r in run_rows
            ],
            brief=[
                RunBrief(
                    code=r.code,
                    state=r.status,
                    scratch=not r.repo,
                    has_pr=r.pr_url is not None or r.pr_number is not None,
                    started_at=r.started_at,
                    ended_at=r.ended_at,
                    waiting_reason=r.wait_reason if r.status == "waiting" else None,
                )
                for r in [r for r in run_rows if _code(r.code)][:RUN_BRIEF_ITEMS]
            ],
        ),
        approvals=Approvals(
            open=len(pending),
            items=[
                ApprovalItem(
                    code=a.code,
                    summary=a.summary,
                    level=a.level.value,
                    asked_at=a.created_at,
                    expires_at=a.expires_at,
                )
                for a in pending
            ],
            brief=[
                ApprovalBrief(
                    level=a.level.value,
                    effect=a.effect.value,
                    needs_code=a.require_code,
                    asked_at=a.created_at,
                    expires_at=a.expires_at,
                )
                for a in pending[:APPROVAL_BRIEF_ITEMS]
            ],
        ),
        questions=Questions(
            open=len(asks),
            items=[
                QuestionItem(
                    run_code=q.run_code,
                    text=q.question,
                    options=list(q.options),
                    asked_at=q.asked_at,
                )
                for q in asks
            ],
            brief=[
                QuestionBrief(run_code=q.run_code, options=len(q.options), asked_at=q.asked_at)
                for q in [q for q in asks if _code(q.run_code)][:QUESTION_BRIEF_ITEMS]
            ],
        ),
        pulse=Pulse(
            open=len(items),
            items=[
                PulseItem(
                    source=i.source,
                    key=i.key,
                    title=i.title,
                    urgency=i.urgency,
                    state="new" if i.changed else "told",
                    updated_at=i.updated_at,
                )
                for i in items
            ],
            brief=[
                PulseBrief(
                    source=i.source if _ident(i.source) else "other",
                    urgency=i.urgency,
                    state="new" if i.changed else "told",
                    updated_at=i.updated_at,
                )
                for i in sorted(
                    items, key=lambda i: (_URGENCY_RANK[i.urgency], -i.updated_at.timestamp())
                )[:PULSE_BRIEF_ITEMS]
            ],
        ),
        extensions=_extensions(conn, health, settings, now),
        runner=runner,
        trust_rules=[TrustRule(n=r.id, text=r.text) for r in rules],
        repos=[RepoItem(name=r.name, mode=r.mode, base=r.base, ci=r.ci) for r in repo_rows],
        claude_brain=brain,
        brain_activity=_brain_activity(conn, settings, now, brain),
        trust_rule_count=len(rules),
        repo_count=len(repo_rows),
    )


_URGENCY_RANK: Final[Mapping[str, int]] = {u: n for n, u in enumerate(pulse.URGENCIES)}


_RC_URL = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_-]{1,200}")


def _claude_brain(conn: sqlite3.Connection) -> ClaudeBrain | None:
    found = claude_status(conn)
    if found is None:
        return None
    url = found.get("remote_url")
    up = found.get("state") == "up"
    return ClaudeBrain(
        state="up" if up else "down",
        turns=int(found.get("turns") or 0),
        remote_url=url if up and isinstance(url, str) and _RC_URL.fullmatch(url) else None,
        tools=_brain_tools(conn),
    )


def _brain_tools(conn: sqlite3.Connection) -> list[BrainTool]:
    out: list[BrainTool] = []
    for row in recent_tools(conn, BRAIN_TOOL_ITEMS):
        try:
            out.append(
                BrainTool(
                    at=datetime.fromisoformat(str(row.get("at"))),
                    tool=str(row.get("tool") or "?")[:64],
                    decision=str(row.get("decision") or "?")[:16],
                    command=str(row.get("command") or "")[:200],
                    cwd=str(row.get("cwd") or "")[:300],
                    status="error" if row.get("status") == "error" else "ok",
                    write=row.get("write") is True,
                )
            )
        except ValueError:
            continue
    return out


_DECISIONS: frozenset[str] = frozenset(get_args(BrainDecision))


def _tool_kind(name: str) -> str:
    """A tool's name as a slug for the remote tier: ``Bash`` is ``bash``, every MCP tool is
    ``mcp`` (its server and tool names stay local), anything else not a slug is ``other``."""
    if name.startswith("mcp__"):
        return "mcp"
    kind = name.lower()
    return kind if _ident(kind) else "other"


def _brain_activity(
    conn: sqlite3.Connection, settings: Settings, now: datetime, brain: ClaudeBrain | None
) -> BrainActivity:
    local = now.astimezone(ZoneInfo(settings.timezone))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    counts = tool_counts(conn, midnight)
    return BrainActivity(
        calls_today=counts["calls"],
        writes_today=counts["writes"],
        state=None if brain is None else brain.state,
        turns=0 if brain is None else brain.turns,
        recent=[]
        if brain is None
        else [
            BrainToolBrief(
                at=t.at,
                tool=_tool_kind(t.tool),
                decision=cast(BrainDecision, t.decision if t.decision in _DECISIONS else "other"),
                status=t.status,
                write=t.write,
            )
            for t in brain.tools[:BRAIN_BRIEF_ITEMS]
        ],
    )


if __name__ == "__main__":
    print(schema_json(), end="")

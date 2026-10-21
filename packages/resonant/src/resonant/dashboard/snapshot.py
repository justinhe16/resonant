"""The dashboard snapshot (docs/dashboard-plan.md §3): Pydantic models and the builder.

One schema, two tiers (plan D3):

- ``local`` is served on loopback only (``GET /api/dashboard``). It may carry text: run asks,
  PR links, approval and question texts, pulse titles, trust rules and repos.
- ``remote`` is what may leave the Mini (the relay, DB-3). It is a strict subset: health,
  counts, states, identifiers and times only.

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
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal, cast, get_args
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic.config import JsonDict

from resonant import __version__
from resonant.config import Settings
from resonant.extensions.cron import CronError, parse_schedule
from resonant.extensions.notify import muted_until
from resonant.gate.policy import change_line, strictest
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

# A field only the local tier carries. ``to_remote`` clears it; None is left out of the output.
LOCAL_ONLY: Final[JsonDict] = {"x-tier": "local"}

Tier = Literal["local", "remote"]
HealthState = Literal["ok", "degraded", "failing", "unknown"]
# What ``Scheduler`` records (``_Result.outcome``); anything else shows as ``other``.
JobOutcome = Literal["ok", "failed", "killed", "denied", "dry_run", "skipped_paused", "other"]
PulseState = Literal["new", "told"]  # new: the owner hasn't heard this version yet
# ``runners.side_effects.Kind`` (TR-1), mirrored: importing ``resonant.runners`` here is a cycle
# (it imports ``brain.briefing``, which imports this module). A test keeps the two equal; a kind
# missing here is left out of the counts, never an error.
EffectKind = Literal[
    "push", "merge", "release", "repo", "deploy", "publish", "http", "self", "message", "mcp",
    "github",
]  # fmt: skip

# Identifiers the remote tier may carry: health check names (``model``, ``ext:github``),
# extension names and job ids (the SDK's slugs, owner-chosen but never message content).
# A row whose identifier doesn't match is left out of the snapshot, never an error.
IDENT_PATTERN: Final = r"^[a-z][a-z0-9:_-]{0,63}$"
Ident = Annotated[str, StringConstraints(pattern=IDENT_PATTERN)]
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
_OUTCOMES: frozenset[str] = frozenset(get_args(JobOutcome))
_HEALTH_STATES: frozenset[str] = frozenset(get_args(HealthState))
_TASK_STATES: frozenset[str] = frozenset(get_args(TaskStatus))
_RUN_STATES: frozenset[str] = frozenset(get_args(RunStatus))
_EFFECT_KINDS: frozenset[str] = frozenset(get_args(EffectKind))
_URGENCIES: tuple[Urgency, ...] = get_args(Urgency)


def _ident(value: str) -> bool:
    return _IDENT.fullmatch(value) is not None


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
    detected: int  # side effects detected in its commands (TR-1, ``run.seen:<code>``)
    remote_url: str | None = _local()  # RC-1: the running session's Remote Control link


class DetectedEffects(_Model):
    """PER-263: the side effects detected in the commands of the runs the snapshot lists
    (TR-1, the last ``RUN_ITEMS`` runs), counts only, so the remote tier has them too:
    by kind, and how many of them were high-impact (a base push, a publish, a delete)."""

    by_kind: dict[EffectKind, int]
    high: int


class Runs(_Model):
    by_state: dict[RunStatus, int]
    detected: DetectedEffects
    items: list[RunItem] | None = _local()


ApprovalLevel = Literal["L0", "L1", "L2", "L3"]


class ApprovalChange(_Model):
    """One change of a batch (BA-1): its line in the batch's prompt, and its own level."""

    summary: str
    level: ApprovalLevel


class ApprovalItem(_Model):
    """What one answer decides: a lone approval, or a batch (PER-254) as one row with the
    batch's code (its first approval's), the summary ``N changes``, its strictest
    level, when it was asked and its one expiry. ``changes`` lists a batch's changes in
    order; it is absent for a lone approval."""

    code: str | None
    summary: str
    level: ApprovalLevel
    asked_at: datetime
    expires_at: datetime
    changes: list[ApprovalChange] | None = _local()


class Approvals(_Model):
    open: int  # rows (what the owner answers): a batch counts once
    items: list[ApprovalItem] | None = _local()


class QuestionItem(_Model):
    run_code: str
    text: str
    options: list[str]
    asked_at: datetime


class Questions(_Model):
    open: int
    items: list[QuestionItem] | None = _local()


class PulseItem(_Model):
    source: str  # ``core`` or the extension that reported it
    key: str
    title: str
    urgency: Urgency
    state: PulseState
    updated_at: datetime


class Pulse(_Model):
    open: int
    # PER-263: items whose latest version the owner hasn't heard (``pulse.pending`` at every
    # urgency, muted sources included, and cleared items whose last word is owed), so the
    # remote tier shows a held or muted reporter's backlog without the titles.
    untold: int
    items: list[PulseItem] | None = _local()


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
    muted_until: datetime | None  # PER-263: ``/ext mute``'s end while it is muted (notify)
    jobs: list[Job]


class Runner(_Model):
    set_up: bool
    missing: list[str] | None = _local()  # what to set up, in words


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


class BrainActivity(_Model):
    """PER-258: the Claude brain's tool calls today (``settings.timezone``), counts only, so
    the remote tier has them too: every call, and the changes that ran."""

    calls_today: int
    writes_today: int


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
                muted_until=muted_until(conn, record.name, now),
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
    pending = _approval_items(conn, approvals.pending_all(conn, now))
    asks = questions.open_asks(conn)
    items = pulse.open_items(conn, now)
    untold = pulse.pending(conn, _URGENCIES, now)
    detected = {r.code: _detected(conn, r.code) for r in run_rows}
    autonomy = kv_get(conn, "autonomy", "on")
    if runner_missing is None:
        runner = Runner(set_up=False, missing=["no Claude runner in this daemon (iMessage is off)"])
    else:
        runner = Runner(set_up=not runner_missing, missing=list(runner_missing) or None)
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
            detected=_detected_counts(detected.values()),
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
                    detected=len(detected[r.code]),
                    remote_url=r.remote_url if r.status == "running" else None,
                )
                for r in run_rows
            ],
        ),
        approvals=Approvals(open=len(pending), items=pending),
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
        ),
        pulse=Pulse(
            open=len(items),
            untold=len(untold),
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
        ),
        extensions=_extensions(conn, health, settings, now),
        runner=runner,
        trust_rules=[TrustRule(n=r.id, text=r.text) for r in trust.list_rules(conn)],
        repos=[
            RepoItem(name=r.name, mode=r.mode, base=r.base, ci=r.ci) for r in repos.list_all(conn)
        ],
        claude_brain=_claude_brain(conn),
        brain_activity=_brain_activity(conn, settings, now),
    )


def _detected(conn: sqlite3.Connection, code: str) -> list[tuple[EffectKind, bool]]:
    """Run ``code``'s detected side effects as (kind, high), unknown kinds left out."""
    from resonant.runners.reports import detected_list  # a cycle at import time (EffectKind)

    # ``detected_list`` casts the stored kind unchecked: a stale kv row can hold anything.
    return [(e.kind, e.high) for e in detected_list(conn, code) if e.kind in _EFFECT_KINDS]


def _detected_counts(per_run: Iterable[list[tuple[EffectKind, bool]]]) -> DetectedEffects:
    by_kind: dict[EffectKind, int] = {}
    high = 0
    for effects in per_run:
        for kind, is_high in effects:
            by_kind[kind] = by_kind.get(kind, 0) + 1
            high += is_high
    return DetectedEffects(by_kind=by_kind, high=high)


def _approval_items(
    conn: sqlite3.Connection, pending: Sequence[approvals.Approval]
) -> list[ApprovalItem]:
    """``pending`` (oldest first) as what one answer decides: each lone approval, and each
    batch (BA-1) once, where its oldest pending approval is. One answer decides all of it
    (``yes`` / ``no``, or ``yes 1,3`` for some and the rest dropped, PER-255), so it is one
    row. A batch's code is its first approval's and its
    changes are in its order (both as its prompt showed them); it expires as one. A batch
    whose approvals aren't all pending any more (one decided on its own: a race) can't be
    answered as one (the gate's ``_units``; the runner drops the rest), so what is left of it
    shows as lone rows, never under a code that no longer binds."""
    index = approvals.batches_of(conn, [a.id for a in pending])
    by_id = {a.id: a for a in pending}
    out: list[ApprovalItem] = []
    seen: set[str] = set()
    for a in pending:
        batch = index.get(a.id)
        if batch is None or any(i not in by_id for i in batch.approval_ids):
            out.append(
                ApprovalItem(
                    code=a.code,
                    summary=a.summary,
                    level=a.level.value,
                    asked_at=a.created_at,
                    expires_at=a.expires_at,
                )
            )
            continue
        if batch.id in seen:
            continue
        seen.add(batch.id)
        members = [by_id[i] for i in batch.approval_ids]
        out.append(
            ApprovalItem(
                code=members[0].code,
                summary=f"{len(members)} changes",
                level=strictest([m.level for m in members]).value,
                asked_at=batch.created_at,
                expires_at=min(m.expires_at for m in members),
                changes=[
                    ApprovalChange(summary=change_line(m), level=m.level.value) for m in members
                ],
            )
        )
    return out


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


def _brain_activity(conn: sqlite3.Connection, settings: Settings, now: datetime) -> BrainActivity:
    local = now.astimezone(ZoneInfo(settings.timezone))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    counts = tool_counts(conn, midnight)
    return BrainActivity(calls_today=counts["calls"], writes_today=counts["writes"])


if __name__ == "__main__":
    print(schema_json(), end="")

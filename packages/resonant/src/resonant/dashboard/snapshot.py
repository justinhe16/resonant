"""The dashboard snapshot (docs/dashboard-plan.md §3, PER-276): Pydantic models and the builder.

One schema, two tiers that carry the same data (PER-276, which supersedes plan D3's text-free
remote tier): ``local`` is served on loopback only (``GET /api/dashboard``) and ``remote`` is
what the relay stores in Supabase, where only the owner (password and TOTP) can read it. The
two differ in exactly two things, both declared with :data:`LOCAL_ONLY` (``"x-tier": "local"``
in the JSON schema), optional, and left out of the output when None: ``remote_url`` (a running
worker's, and the Claude brain's, Remote Control link: whoever holds it can drive the session)
and an approval's ``code`` (the second factor the owner types for an L1 approval).
:func:`to_remote` clears those, at any depth, so a new local-only field is covered by
declaring it, not by remembering a filter.

Every free text (asks, summaries, approval and question texts, pulse titles, health reasons,
tool commands, trust rules, ...) passes through one function, :func:`_Text`: ``redact.clip_text``
(credential-shaped text replaced, then the known secret values the caller passes, then cut to
the field's cap with a ``… [cut]`` marker). The caps are in the field types (``Txt64`` is a
string of at most 64 characters, ``maxLength`` in the schema); nothing is longer than 800, and
a test checks every free-text node of the schema. A model with text that came from outside the
owner's conversation (a worker, an extension, a web page) lists the names of those fields in
``outside``, so the page can say so. The relay's SQL refuses any secret-shaped text that still
gets there (``redact.SECRET_SQL_FAMILY``), and the pusher scans the dump once more with the
secrets it knows.

Size: the lists are capped (:class:`Limits`, quoted by the page) and ``pusher.shrink`` trims
them further, in a fixed order, when the JSON is still over the relay's cap (``trimmed`` says
how far). ``docs/snapshot.schema.json`` is generated from :class:`Snapshot`:
``uv run python -m resonant.dashboard.snapshot > docs/snapshot.schema.json``. A test fails
when it is stale. ``dashboard/src/types.ts`` mirrors it.

The builder only reads the store (and the live state the caller passes in). It never reads a
secret's value: the runner's gaps come from ``ClaudeRunner.setup_gaps``, by name. What it never
ships at all: task checkpoints, results and scopes, channel refs, session ids, worktree and
workspace paths, run commands, MCP pins and input schemas, an extension's source path.
"""

from __future__ import annotations

import functools
import json
import re
import sqlite3
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, Literal, cast, get_args
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
)
from pydantic.config import JsonDict

from resonant import __version__
from resonant.config import Settings
from resonant.dashboard import diagram as diagram_parts
from resonant.dashboard.diagram import LABEL_PATTERN as AGENT_LABEL_PATTERN
from resonant.dashboard.diagram import AgentState
from resonant.extensions.cron import CronError, parse_schedule
from resonant.extensions.notify import muted_until
from resonant.gate.policy import change_line, strictest, widens_trust
from resonant.loop.slots import SlotPool
from resonant.loop.types import Task, TaskStatus
from resonant.loop.types import WaitReason as TaskWait
from resonant.monitor import EXT_CHECK_PREFIX, health_snapshot
from resonant.redact import Redactor, clip_text
from resonant.store import approvals, pulse, questions, repos, runs, system_texts, trust
from resonant.store import extensions as ext_store
from resonant.store import tasks as task_store
from resonant.store.audit import EVENT_NAME, run_events, task_events
from resonant.store.claude_brain import recent_tools, tool_counts
from resonant.store.claude_brain import status_of as claude_status
from resonant.store.db import kv_get, utcnow
from resonant.store.extensions import ExtensionStatus
from resonant.store.pulse import Urgency
from resonant.store.repos import Mode
from resonant.store.runs import Status as RunStatus
from resonant.store.runs import Step as RunStep
from resonant.store.runs import WaitReason
from resonant_sdk import DEFAULT_LEVEL, Effect

SCHEMA_VERSION: Final = 2

# The caps. ``Limits`` quotes the first group to the page ("it keeps the newest 50 runs").
RUN_ITEMS = 50  # active runs first, then the most recent (``runs.recent``)
RUN_DETAIL = 20  # of those, the first this many carry their text and history
TASK_ITEMS = 50
TASK_DETAIL = 20
APPROVALS_RECENT = 20  # decided in the last ``RECENT_APPROVALS_DAYS`` days, newest first
APPROVALS_PENDING = 20
QUESTION_ITEMS = 20
PULSE_ITEMS = 60
BRAIN_TOOL_ITEMS = 20  # the Claude brain's last tool calls (PER-258)
EXTENSION_ITEMS = 20
CORE_JOB_ITEMS = 12
TRUST_RULE_ITEMS = 50
REPO_ITEMS = 50
RECENT_APPROVALS_DAYS = 7
RUN_HISTORY = 12  # a run's last audit rows (``run.start``, ``run.ready``, ...)
TASK_HISTORY = 10
RUN_DID = 10
RUN_EFFECTS = 10
RUN_MERGED = 5
RUN_FLAGGED = 5
RUN_LINEAR_IDS = 5
RELATED_IDS = 10  # a run's or task's linked approvals
QUESTION_OPTIONS = 6
APPROVAL_CHANGES = 10
EXT_TOOLS = 40
EXT_MCP_SERVERS = 5
EXT_SECRETS = 10
EXT_REPOS = 20
RUNNER_MISSING = 10

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
EffectName = Literal["read", "write", "pay"]
ApprovalLevel = Literal["L0", "L1", "L2", "L3"]
Trimmed = Literal["none", "history", "lists", "detail", "items"]

# Identifiers: health check names (``model``, ``ext:github``), extension names and job ids (the
# SDK's slugs, owner-chosen but never message content). A row whose identifier doesn't match
# is left out of the snapshot, never an error.
IDENT_PATTERN: Final = r"^[a-z][a-z0-9:_-]{0,63}$"
Ident = Annotated[str, StringConstraints(pattern=IDENT_PATTERN)]
# A git sha (8 hex) or the package version when there is no git checkout.
VERSION_PATTERN: Final = r"^[0-9A-Za-z.+-]{1,40}$"
Version = Annotated[str, StringConstraints(pattern=VERSION_PATTERN)]
# An audit action as a short event name (``start``, ``approval.requested``).
EVENT_PATTERN: Final = EVENT_NAME.pattern
EventName = Annotated[str, StringConstraints(pattern=EVENT_PATTERN)]
SHA_PATTERN: Final = r"^[0-9a-f]{7,40}$"
Sha = Annotated[str, StringConstraints(pattern=SHA_PATTERN)]
LINEAR_PATTERN: Final = r"^[A-Z]{2,10}-\d{1,6}$"
LinearId = Annotated[str, StringConstraints(pattern=LINEAR_PATTERN)]
PR_URL_PATTERN: Final = r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$"
PrUrl = Annotated[str, StringConstraints(pattern=PR_URL_PATTERN)]
# The name of a field of the same object (``summary``, ``flagged_paths``).
OutsideName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z_]{0,31}$")]


def _txt(n: int) -> Any:
    return StringConstraints(max_length=n)


# Free text, by cap (characters, the marker included).
Txt16 = Annotated[str, _txt(16)]
Txt32 = Annotated[str, _txt(32)]
Txt40 = Annotated[str, _txt(40)]
Txt64 = Annotated[str, _txt(64)]
Txt80 = Annotated[str, _txt(80)]
Txt100 = Annotated[str, _txt(100)]
Txt120 = Annotated[str, _txt(120)]
Txt160 = Annotated[str, _txt(160)]
Txt200 = Annotated[str, _txt(200)]
Txt300 = Annotated[str, _txt(300)]
Txt400 = Annotated[str, _txt(400)]
Txt600 = Annotated[str, _txt(600)]
Txt800 = Annotated[str, _txt(800)]

# The relay's own health row (``resonant.dashboard.pusher``): {state, since, detail}. Unlike the
# monitor's checks it can be ``failing``. Absent while the relay is off.
RELAY_HEALTH_KV: Final = "health.relay"
RELAY_CHECK: Final = "relay"

_GIT = "/usr/bin/git"  # SIP-protected; never looked up by name
_SHA = re.compile(r"[0-9a-f]{40}")
_IDENT = re.compile(IDENT_PATTERN)
_PR_URL = re.compile(PR_URL_PATTERN)
_HEAD_SHA = re.compile(SHA_PATTERN)
# Linear ids: upper case in an ask (``PER-276``; ``gpt-4`` is not one). In a branch (lower case,
# a slug of the ask) only a key the ask named, or one starting a path segment as Linear's own
# branch names do (``justin/per-276-...``).
_LINEAR = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,10}-\d{1,6})(?![A-Za-z0-9])")
_LINEAR_BRANCH = re.compile(r"(?<![a-z0-9])([a-z]{2,10})-(\d{1,6})(?![a-z0-9])")
_RUN_KEY = re.compile(r"run/(r\d{1,6})")
_AGENT_LABEL = re.compile(AGENT_LABEL_PATTERN)
_OUTCOMES: frozenset[str] = frozenset(get_args(JobOutcome))
_HEALTH_STATES: frozenset[str] = frozenset(get_args(HealthState))
_TASK_STATES: frozenset[str] = frozenset(get_args(TaskStatus))
_RUN_STATES: frozenset[str] = frozenset(get_args(RunStatus))
_EFFECT_KINDS: frozenset[str] = frozenset(get_args(EffectKind))
_EFFECT_NAMES: frozenset[str] = frozenset(get_args(EffectName))
_LEVELS: frozenset[str] = frozenset(get_args(ApprovalLevel))
_URGENCIES: tuple[Urgency, ...] = get_args(Urgency)
_URGENCY_RANK: Final[Mapping[str, int]] = {u: n for n, u in enumerate(pulse.URGENCIES)}


def _ident(value: str) -> bool:
    return _IDENT.fullmatch(value) is not None


def _counts(counts: Mapping[str, int], known: frozenset[str]) -> dict[Any, int]:
    """``counts`` with only the known states (a state added later never breaks the page)."""
    return {k: n for k, n in counts.items() if k in known}


def _local() -> Any:
    """A local-only field: optional, None by default and left out of the output when None."""
    return Field(default=None, exclude_if=lambda v: v is None, json_schema_extra=LOCAL_ONLY)


def _list() -> Any:
    """An optional list field: empty by default."""
    return Field(default_factory=list)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Limits(_Model):
    """How much of each list the snapshot keeps, so the page can say "the newest 50"."""

    runs: int = RUN_ITEMS
    run_detail: int = RUN_DETAIL
    tasks: int = TASK_ITEMS
    task_detail: int = TASK_DETAIL
    approvals_recent: int = APPROVALS_RECENT
    approvals_pending: int = APPROVALS_PENDING
    questions: int = QUESTION_ITEMS
    pulse: int = PULSE_ITEMS
    brain_tools: int = BRAIN_TOOL_ITEMS


class Event(_Model):
    """One audit row of a run or task: when, and which action (a run's lose their ``run.``)."""

    at: datetime
    event: EventName


class HealthRow(_Model):
    name: Ident
    state: HealthState
    since: datetime | None
    reason: Txt200 | None = None  # the check's detail
    outside: list[OutsideName] = _list()  # ``["reason"]`` for an extension's own check


class TaskItem(_Model):
    """One durable task (a chat turn, a run, a job that needs approval, a reminder, a watch
    check). Never its checkpoint, result, scope, channel ref, session id or worktree."""

    id: Txt64
    kind: Txt32
    runner: Txt32
    extension: Txt64 | None
    status: TaskStatus
    wait_reason: TaskWait | None
    wake_at: datetime | None
    attempts: int
    step_no: int
    created_at: datetime
    updated_at: datetime
    label: Txt120 | None  # the checkpoint's title, or ``ext/job`` for a job
    error: Txt300 | None
    run_code: Txt16 | None
    history: list[Event] = _list()
    approval_ids: list[Txt64] = _list()
    outside: list[OutsideName] = _list()


class Tasks(_Model):
    by_state: dict[TaskStatus, int]
    items: list[TaskItem] = _list()


class Slots(_Model):
    used: int
    max: int


class RunEffect(_Model):
    """A side effect Resonant read in a run's commands (TR-1)."""

    kind: EffectKind
    what: Txt80
    high: bool


class RunItem(_Model):
    code: Txt16
    state: RunStatus
    waiting_reason: WaitReason | None
    started_at: datetime | None
    ended_at: datetime | None
    detected: int  # side effects detected in its commands (TR-1, ``run.seen:<code>``)
    repo: Txt100  # empty for a scratch run
    ask: Txt300
    branch: Txt120 | None
    base_branch: Txt120 | None
    head_sha: Sha | None
    pr_url: PrUrl | None  # only a github.com pull request link
    pr_number: int | None
    github_repo: Txt100 | None  # ``owner/name`` of the repo; None for a scratch run
    created_at: datetime
    remote_url: str | None = _local()  # RC-1: the running session's Remote Control link
    task_id: Txt64 | None
    step: RunStep
    attempts: int
    sessions: int
    exit_code: int | None
    cost_usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    session_seconds: float
    wait_seconds: float
    files_changed: int | None
    flagged_paths: list[Txt120] = _list()
    summary: Txt800 | None
    failure: Txt400 | None
    last_note: Txt200 | None
    did: list[Txt80] = _list()  # what the worker said it did (``did:`` notes), newest last
    effects: list[RunEffect] = _list()
    merged: list[Txt40] = _list()
    merged_by: Literal["resonant", "run"] | None
    history: list[Event] = _list()  # oldest first
    approval_ids: list[Txt64] = _list()
    question_ids: list[int] = _list()
    linear_ids: list[LinearId] = _list()  # as plain text: no workspace is configured
    detail: bool  # False past ``Limits.run_detail``: its text and history are left out
    outside: list[OutsideName] = _list()


class DetectedEffects(_Model):
    """PER-263: the side effects detected in the commands of the runs the snapshot lists
    (TR-1), counts only: by kind, and how many of them were high-impact (a base push, a
    publish, a delete)."""

    by_kind: dict[EffectKind, int]
    high: int


class Runs(_Model):
    by_state: dict[RunStatus, int]
    detected: DetectedEffects
    items: list[RunItem] = _list()


class ApprovalChange(_Model):
    """One change of a batch (BA-1): its line in the batch's prompt, and its own level."""

    summary: Txt200
    level: ApprovalLevel


class ApprovalItem(_Model):
    """What one answer decides: a lone approval, or a batch (PER-254) as one row with its first
    approval's id, the summary ``N changes``, its strictest level, when it was asked and its
    one expiry. ``changes`` lists a batch's changes in order; it is absent for a lone
    approval. ``code`` (the second factor an L1 yes needs) is local only."""

    id: Txt64
    member_ids: list[Txt64]
    code: str | None = _local()
    summary: Txt300
    level: ApprovalLevel
    effect: EffectName
    needs_code: bool
    tool: Txt64
    extension: Txt64 | None
    status: Literal["pending", "approved", "rejected", "expired"]
    asked_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decided_by: Txt40 | None
    task_id: Txt64 | None
    run_code: Txt16 | None
    changes: list[ApprovalChange] | None = None
    outside: list[OutsideName] = _list()


class Approvals(_Model):
    open: int  # rows (what the owner answers): a batch counts once
    items: list[ApprovalItem] = _list()  # pending, oldest first
    recent: list[ApprovalItem] = _list()  # decided in the last week, newest first


class QuestionItem(_Model):
    id: int
    run_code: Txt16
    kind: Txt16
    text: Txt600
    options: list[Txt80]
    urgent: bool
    asked_at: datetime
    outside: list[OutsideName] = _list()


class Questions(_Model):
    open: int
    items: list[QuestionItem] = _list()


class PulseItem(_Model):
    source: Txt64  # ``core`` or the extension that reported it
    key: Txt120
    title: Txt200
    urgency: Urgency
    state: PulseState
    updated_at: datetime
    expires_at: datetime
    told_at: datetime | None
    run_code: Txt16 | None
    outside: list[OutsideName] = _list()


class Pulse(_Model):
    open: int
    # PER-263: items whose latest version the owner hasn't heard (``pulse.pending`` at every
    # urgency, muted sources included, and cleared items whose last word is owed).
    untold: int
    items: list[PulseItem] = _list()


class Job(_Model):
    id: Ident
    schedule: Txt64 | None = None  # cron
    tz: Txt64 | None = None
    effect: EffectName | None = None
    level: ApprovalLevel | None = None
    next_due: datetime | None  # None when the extension is disabled or the schedule is bad
    last_run_at: datetime | None
    last_outcome: JobOutcome | None
    fail_streak: int


class ExtTool(_Model):
    name: Txt64
    via: Literal["cli", "mcp"]
    server: Txt64 | None
    effect: EffectName
    level: ApprovalLevel
    description: Txt160 | None


class McpServer(_Model):
    id: Txt64
    transport: Literal["stdio", "http"]
    host: Txt120 | None  # an http server's host only: never its path or query


class Extension(_Model):
    name: Ident
    status: ExtensionStatus
    health: HealthState  # its ``ext:<name>`` check
    muted_until: datetime | None  # PER-263: ``/ext mute``'s end while it is muted (notify)
    jobs: list[Job]
    description: Txt120 | None = None
    source_kind: Literal["repo", "path", "other"] = "other"
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,12}$")] | None = None
    installed_at: datetime | None = None
    tools: list[ExtTool] = _list()
    mcp_servers: list[McpServer] = _list()
    secrets: list[Ident] = _list()  # names only
    report_max: Urgency | None = None
    repos: list[Txt100] = _list()


class Runner(_Model):
    set_up: bool
    missing: list[Txt120] = _list()  # what to set up, in words
    gaps: int = 0  # how many


class BrainTool(_Model):
    """PER-258: one of the Claude brain's tool calls (its ``brain.tool`` audit row)."""

    at: datetime
    tool: Txt64
    decision: (
        Txt16  # allowed (the read list), read, gate, granted, asked, refused, denied, resonant
    )
    command: Txt200
    cwd: Txt120
    status: Literal["ok", "error"]
    write: bool
    outside: list[OutsideName] = _list()  # ``["command"]``


class ClaudeBrain(_Model):
    """BR-11: the Claude brain's session: up or down, its turns, its Remote Control link while
    one is on (local only), and its last tool calls (PER-258)."""

    state: Literal["up", "down"]
    turns: int
    remote_url: str | None = _local()
    tools: list[BrainTool] = _list()


class BrainActivity(_Model):
    """PER-258: the Claude brain's tool calls today (``settings.timezone``), counts only: every
    call, and the changes that ran. With its session's state and turns (None before its first
    start)."""

    calls_today: int
    writes_today: int
    state: Literal["up", "down"] | None = None
    turns: int = 0


class TrustRule(_Model):
    n: int  # the rule's id, as ``/trust`` lists it
    text: Txt200


class RepoItem(_Model):
    name: Txt64
    mode: Mode
    base: Txt120
    ci: bool | None  # CI seen on the repo (None: not checked yet)
    slug: Txt100


# --- the diagram (PER-277) ------------------------------------------------------------------

DiagramKind = Literal["core", "process", "extension", "server", "tool", "job", "repo"]
# What the page opens for a node: the PER-276 view it already has (``extension``, ``job``,
# ``health``) or the node's own view.
DIAGRAM_NODES = 250  # core and processes first, then each extension with its children
DIAGRAM_EDGES = 400
DIAGRAM_PROCESSES = 30
_ID_MAX = 200  # ``DiagramNode.id``; an id built longer is left out, never a failed snapshot


class DiagramNode(_Model):
    """One box of the architecture diagram (``Snapshot.diagram``): a core component, a launchd
    agent, an extension, or one of an extension's MCP servers, tools, jobs or repos."""

    id: Txt200  # ``core:gate``, ``proc:com.resonant.daemon``, ``ext:linear``, ``tool:linear/x``
    kind: DiagramKind
    label: Txt100
    parent: Txt200 | None = None  # the node it belongs to (an extension's child, a core job)
    text: Txt300  # one sentence on its job
    tags: list[Txt32] = _list()  # ``mcp``, ``read``, ``asks first``, ``running``
    health: HealthState | None = None
    check: Ident | None = None  # its health row (``#/health/<check>``)
    state: Txt32 | None = None  # a process's launchd state, an extension's status
    module: Txt120 | None = None  # a core component's place in the repo
    last_run_at: datetime | None = None
    last_outcome: JobOutcome | None = None
    ext: Ident | None = None  # the extension (``core`` for a core job) it opens in its view
    job: Ident | None = None  # a job node's job id
    # PER-278: its top-level box (``Diagram.groups``), on nodes without a parent (a child is in
    # its parent's); None from a daemon before it
    group: Ident | None = None

    @model_serializer(mode="wrap")
    def _drop_nulls(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Unset fields are left out (PER-278): a node is mostly nulls, and with up to
        ``DIAGRAM_NODES`` of them that is a few KB of the relay's budget. The page reads a
        missing field as null."""
        out = cast(dict[str, Any], handler(self))
        return {k: v for k, v in out.items() if v is not None}


class DiagramGroup(_Model):
    """One of the high-level boxes the Diagram tab starts from (``dashboard.diagram.GROUPS``)."""

    id: Ident
    label: Txt100
    text: Txt300


class DiagramEdge(_Model):
    source: Txt200
    target: Txt200
    label: Txt40 | None = None


class Diagram(_Model):
    """How Resonant is put together right now: the curated core skeleton
    (``dashboard.diagram.CORE``), the launchd agents it knows, and every installed extension
    from its approved manifest; health and last runs from the store."""

    nodes: list[DiagramNode] = _list()
    edges: list[DiagramEdge] = _list()
    groups: list[DiagramGroup] = _list()  # PER-278; empty from a daemon before it
    processes_scanned: bool = False  # False: the launchd scan didn't run (states unknown)
    truncated: bool = False  # True: over ``DIAGRAM_NODES``/``DIAGRAM_EDGES``, the rest left out


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

    schema_: Literal[2] = Field(alias="schema")
    tier: Tier
    taken_at: datetime
    version: Version
    started_at: datetime  # when the daemon started
    last_tick: datetime | None
    autonomy: Literal["on", "off"]
    paused: bool
    dry_run: bool
    trimmed: Trimmed = "none"  # how far the pusher cut the lists to fit the relay
    limits: Limits
    health: list[HealthRow]
    tasks: Tasks
    slots: Slots
    runs: Runs
    approvals: Approvals
    questions: Questions
    pulse: Pulse
    extensions: list[Extension]
    core_jobs: list[Job]
    runner: Runner
    trust_rules: list[TrustRule]
    repos: list[RepoItem]
    claude_brain: ClaudeBrain | None = None  # BR-11; None before its first start
    brain_activity: BrainActivity = Field(default_factory=lambda: BrainActivity(
        calls_today=0, writes_today=0))  # fmt: skip
    trust_rule_count: int = 0
    repo_count: int = 0
    diagram: Diagram | None = None  # PER-277; None from a daemon before it


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


class _Text:
    """The one pass every free text goes through (``redact.clip_text``): credential-shaped
    text, then the caller's known secret values, then the field's cap."""

    def __init__(self, known: Redactor | None) -> None:
        self.known = known

    def __call__(self, value: str, cap: int) -> str:
        return clip_text(value, cap, known=self.known) or ""

    def opt(self, value: str | None, cap: int) -> str | None:
        return None if value is None else clip_text(value, cap, known=self.known)

    def many(self, values: Iterable[str], cap: int) -> list[str]:
        return [self(v, cap) for v in values]


def _health_state(raw: object) -> HealthState:
    return cast(HealthState, raw) if raw in _HEALTH_STATES else "unknown"


def _since(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _health(conn: sqlite3.Connection, t: _Text) -> dict[str, HealthRow]:
    rows: dict[str, HealthRow] = {}
    for name, h in health_snapshot(conn).items():
        if not _ident(name):  # a stale or hand-edited kv key
            continue
        detail = h.get("detail")
        reason = t.opt(detail, 200) if isinstance(detail, str) and detail else None
        rows[name] = HealthRow(
            name=name,
            state=_health_state(h.get("state")),
            since=_since(h.get("since")),
            reason=reason,
            outside=["reason"] if reason and name.startswith(EXT_CHECK_PREFIX) else [],
        )
    raw = kv_get(conn, RELAY_HEALTH_KV)
    if isinstance(raw, dict):
        relay = cast(dict[str, Any], raw)
        detail = relay.get("detail")
        rows[RELAY_CHECK] = HealthRow(
            name=RELAY_CHECK,
            state=_health_state(relay.get("state")),
            since=_since(relay.get("since")),
            reason=t.opt(detail, 200) if isinstance(detail, str) and detail else None,
        )
    return rows


def _effect(raw: object) -> EffectName | None:
    return cast(EffectName, raw) if isinstance(raw, str) and raw in _EFFECT_NAMES else None


def _level(raw: object, effect: EffectName | None) -> ApprovalLevel | None:
    """The declared level, else the effect's default (``DEFAULT_LEVEL``)."""
    if isinstance(raw, str) and raw in _LEVELS:
        return cast(ApprovalLevel, raw)
    return None if effect is None else DEFAULT_LEVEL[Effect(effect)].value


def _job(
    job_id: str,
    st: ext_store.JobState | None,
    *,
    schedule: str | None,
    tz: str | None,
    effect: EffectName | None,
    level: ApprovalLevel | None,
    next_due: datetime | None,
    t: _Text,
) -> Job:
    outcome = None if st is None else st.last_outcome
    return Job(
        id=job_id,
        schedule=t.opt(schedule, 64),
        tz=t.opt(tz, 64),
        effect=effect,
        level=level,
        next_due=next_due,
        last_run_at=None if st is None else st.last_run_at,
        last_outcome=None
        if outcome is None
        else cast(JobOutcome, outcome if outcome in _OUTCOMES else "other"),
        fail_streak=0 if st is None else st.fail_streak,
    )


def _next_due(expr: object, tz: str, now: datetime) -> datetime | None:
    try:
        return parse_schedule(str(expr or ""), tz).next_after(now)
    except (CronError, ValueError):
        return None


def _jobs(
    record: ext_store.ExtensionRecord,
    states: Mapping[str, ext_store.JobState],
    settings: Settings,
    now: datetime,
    t: _Text,
) -> list[Job]:
    jobs: list[Job] = []
    for spec in cast(list[dict[str, Any]], record.manifest.get("jobs") or []):
        job_id = str(spec.get("id", ""))
        if not _ident(job_id):
            continue
        zone = str(spec.get("tz") or settings.timezone)
        next_due = (
            _next_due(spec.get("schedule"), zone, now) if record.status == "enabled" else None
        )
        effect = _effect(spec.get("effect", "write"))
        jobs.append(
            _job(
                job_id,
                states.get(job_id),
                schedule=None if spec.get("schedule") is None else str(spec["schedule"]),
                tz=None if spec.get("tz") is None else str(spec["tz"]),
                effect=effect,
                level=_level(spec.get("level"), effect),
                next_due=next_due,
                t=t,
            )
        )
    return jobs


def _core_jobs(
    conn: sqlite3.Connection,
    core_jobs: Sequence[tuple[str, str, str]],
    settings: Settings,
    now: datetime,
    t: _Text,
) -> list[Job]:
    """The daemon's own jobs. ``core_jobs`` is the scheduler's ``(id, cron, tz)``; without it
    (a process with no scheduler) only the jobs the store has a state for, with no schedule."""
    states = {j.job_id: j for j in ext_store.list_jobs(conn, ext_store.CORE_EXT)}
    out: list[Job] = []
    if core_jobs:
        for job_id, cron, tz in core_jobs:
            if _ident(job_id):
                out.append(
                    _job(
                        job_id,
                        states.get(job_id),
                        schedule=cron,
                        tz=tz,
                        effect="read",
                        level="L0",
                        next_due=_next_due(cron, tz or settings.timezone, now),
                        t=t,
                    )
                )
    else:
        out = [
            _job(j.job_id, j, schedule=None, tz=None, effect=None, level=None, next_due=None, t=t)
            for j in states.values()
            if _ident(j.job_id)
        ]
    return out[:CORE_JOB_ITEMS]


def _tools_of(manifest: Mapping[str, Any], t: _Text) -> tuple[list[ExtTool], list[McpServer]]:
    """The tools and MCP servers an extension declares. Never a command line, an input
    schema, a pin or an argument; an http server shows its host only."""
    tools: list[ExtTool] = []
    cli = manifest.get("tools")
    for name, spec in (cast(dict[str, Any], cli) if isinstance(cli, dict) else {}).items():
        if not isinstance(spec, dict):
            continue
        fields = cast(dict[str, Any], spec)
        effect = _effect(fields.get("effect"))
        level = _level(fields.get("level"), effect)
        if effect is None or level is None:
            continue
        desc = fields.get("description")
        tools.append(
            ExtTool(
                name=t(str(name), 64),
                via="cli",
                server=None,
                effect=effect,
                level=level,
                description=t.opt(desc, 160) if isinstance(desc, str) and desc else None,
            )
        )
    servers: list[McpServer] = []
    mcp = manifest.get("mcp")
    raw_servers: object = (
        cast(dict[str, Any], mcp).get("servers") if isinstance(mcp, dict) else None
    )
    for sid, server in (
        cast(dict[str, Any], raw_servers) if isinstance(raw_servers, dict) else {}
    ).items():
        if not isinstance(server, dict):
            continue
        sv = cast(dict[str, Any], server)
        http = sv.get("transport") == "http"
        host = None
        if http:
            try:
                host = urlsplit(str(sv.get("url", ""))).hostname
            except ValueError:
                host = None
        servers.append(
            McpServer(
                id=t(str(sid), 64),
                transport="http" if http else "stdio",
                host=t.opt(host, 120),
            )
        )
        for tool in cast(list[Any], sv.get("tools") or []):
            if not isinstance(tool, dict):
                continue
            tf = cast(dict[str, Any], tool)
            effect = _effect(tf.get("effect"))
            level = _level(tf.get("level"), effect)
            if effect is None or level is None or not isinstance(tf.get("name"), str):
                continue
            desc = tf.get("description")
            tools.append(
                ExtTool(
                    name=t(str(tf["name"]), 64),
                    via="mcp",
                    server=t(str(sid), 64),
                    effect=effect,
                    level=level,
                    description=t.opt(desc, 160) if isinstance(desc, str) and desc else None,
                )
            )
    return tools[:EXT_TOOLS], servers[:EXT_MCP_SERVERS]


def _extension(
    conn: sqlite3.Connection,
    record: ext_store.ExtensionRecord,
    health: Mapping[str, HealthRow],
    settings: Settings,
    now: datetime,
    t: _Text,
) -> Extension:
    manifest = record.manifest
    states = {j.job_id: j for j in ext_store.list_jobs(conn, record.name)}
    check = health.get(EXT_CHECK_PREFIX + record.name)
    tools, servers = _tools_of(manifest, t)
    desc = manifest.get("description")
    secrets = manifest.get("secrets")
    report = manifest.get("report")
    max_urgency = (
        cast(dict[str, Any], report).get("max_urgency") if isinstance(report, dict) else None
    )
    ext_repos = manifest.get("repos")
    kind = record.source.split(":", 1)[0]
    commit = (record.commit or "")[:12].lower()
    return Extension(
        name=record.name,
        status=record.status,
        health="unknown" if check is None else check.state,
        muted_until=muted_until(conn, record.name, now),
        jobs=_jobs(record, states, settings, now, t),
        description=t.opt(desc, 120) if isinstance(desc, str) and desc else None,
        source_kind="repo" if kind == "repo" else "path" if kind == "path" else "other",
        commit=commit if re.fullmatch(r"[0-9a-f]{7,12}", commit) else None,
        installed_at=record.installed_at,
        tools=tools,
        mcp_servers=servers,
        secrets=[
            s
            for s in (cast(list[Any], secrets) if isinstance(secrets, list) else [])
            if isinstance(s, str) and _ident(s)
        ][:EXT_SECRETS],
        report_max=max_urgency if max_urgency in _URGENCIES else None,
        repos=t.many(
            [r for r in (cast(list[Any], ext_repos) if isinstance(ext_repos, list) else [])
             if isinstance(r, str)][:EXT_REPOS],
            100,
        ),
    )  # fmt: skip


def _extensions(
    conn: sqlite3.Connection,
    health: Mapping[str, HealthRow],
    settings: Settings,
    now: datetime,
    t: _Text,
) -> list[Extension]:
    return [
        _extension(conn, record, health, settings, now, t)
        for record in ext_store.list_extensions(conn)
        if _ident(record.name)
    ][:EXTENSION_ITEMS]


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
    core_jobs: Sequence[tuple[str, str, str]] = (),
    redactor: Redactor | None = None,
    launch_agents: Sequence[AgentState] | None = None,
    diagram: bool = True,
) -> Snapshot:
    """The local-tier snapshot (``to_remote`` derives the remote one).

    ``slots`` is the loop's Claude slot pool (without it: none used, ``claude.max_concurrent``
    max). ``runner_missing`` is ``ClaudeRunner.setup_gaps()``'s list, or None when no Claude
    runner is wired in this process (then the runner is not set up). ``core_jobs`` is the
    scheduler's ``(id, cron, tz)`` per core job. ``redactor`` holds secret values the text
    passes must also remove (the relay pusher's token and key). ``launch_agents`` is
    ``diagram.cached_scan()``'s list (None: not scanned); only the agents the diagram allows
    (``diagram.agent_allowed``) are shipped.
    """
    now = now or utcnow()
    run_rows = runs.recent(conn, limit=RUN_ITEMS)
    # Every Remote Control link we hold is also a known value (a worker may echo one into a
    # text; ``clip_text`` scrubs the shape, this catches the exact value whatever its shape).
    links = [r.remote_url for r in run_rows if r.remote_url]
    session = claude_status(conn)
    if session is not None and isinstance(session.get("remote_url"), str):
        links.append(session["remote_url"])
    t = _Text(Redactor([*(redactor.values if redactor else ()), *links]))
    health = _health(conn, t)
    ctx = _Context(conn, now)

    task_rows = task_store.recent(conn, TASK_ITEMS)
    asks = questions.open_asks(conn)
    pending_rows = approvals.pending_all(conn, now)
    recent_rows = approvals.recent_decided(
        conn, now - timedelta(days=RECENT_APPROVALS_DAYS), APPROVALS_RECENT * 5
    )
    ctx.add_tasks([r.task_id for r in run_rows if r.task_id])
    ctx.add_tasks([r.id for r in task_rows])
    ctx.add_tasks([a.task_id for a in (*pending_rows, *recent_rows) if a.task_id])
    ctx.load(task_rows)

    pending = _approval_items(conn, pending_rows, ctx, t, pending=True)
    recent_items = _approval_items(conn, recent_rows, ctx, t, pending=False)
    pending_shown = pending[:APPROVALS_PENDING]
    recent_shown = recent_items[:APPROVALS_RECENT]
    visible = {m for a in (*pending_shown, *recent_shown) for m in a.member_ids}
    related = approvals.ids_by_task(conn, ctx.task_ids, now - timedelta(days=RECENT_APPROVALS_DAYS))

    items = pulse.open_items(conn, now)
    untold = pulse.pending(conn, _URGENCIES, now)
    from resonant.runners import reports  # an import cycle at module level (EffectKind)

    detected = {r.code: _detected(conn, r.code, reports) for r in run_rows}
    events = run_events(conn, [(r.code, r.task_id) for r in run_rows], RUN_HISTORY)
    task_history = task_events(conn, [r.id for r in task_rows[:TASK_DETAIL]], TASK_HISTORY)
    slug_of = {r.name: r.slug for r in repos.list_all(conn)}
    open_question_ids: dict[str, list[int]] = {}
    for q in asks[:QUESTION_ITEMS]:
        open_question_ids.setdefault(q.run_code, []).append(q.id)

    autonomy = kv_get(conn, "autonomy", "on")
    brain = _claude_brain(conn, t)
    if runner_missing is None:
        runner = Runner(
            set_up=False,
            missing=[t("no Claude runner in this daemon (iMessage is off)", 120)],
            gaps=1,
        )
    else:
        runner = Runner(
            set_up=not runner_missing,
            missing=t.many(runner_missing[:RUNNER_MISSING], 120),
            gaps=len(runner_missing),
        )
    rules = trust.list_rules(conn)
    repo_rows = repos.list_all(conn)

    def related_ids(task_id: str | None) -> list[str]:
        ids = [i for i in related.get(task_id or "", []) if i in visible]
        return ids[-RELATED_IDS:]

    extension_rows = _extensions(conn, health, settings, now, t)
    core_job_rows = _core_jobs(conn, core_jobs, settings, now, t)
    graph = (
        None
        if not diagram
        else _diagram(
            conn,
            extensions=extension_rows,
            core_jobs=core_job_rows,
            health=health,
            brain=brain,
            runner=runner,
            repo_names=[r.name for r in repo_rows],
            agents=launch_agents,
            now=now,
            t=t,
        )
    )

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
        limits=Limits(),
        health=list(health.values()),
        tasks=Tasks(
            by_state=_counts(task_store.count_by_status(conn), _TASK_STATES),
            items=[
                _task_item(
                    task,
                    ctx,
                    t,
                    history=task_history.get(task.id, []) if n < TASK_DETAIL else [],
                    approval_ids=related_ids(task.id),
                )
                for n, task in enumerate(task_rows)
            ],
        ),
        slots=Slots(
            used=0 if slots is None else slots.in_use,
            max=settings.claude.max_concurrent if slots is None else slots.capacity,
        ),
        runs=Runs(
            by_state=_counts(runs.count_by_status(conn), _RUN_STATES),
            detected=_detected_counts(d for d in detected.values()),
            items=[
                _run_item(
                    r,
                    conn,
                    ctx,
                    t,
                    reports,
                    detail=n < RUN_DETAIL,
                    detected=detected[r.code],
                    history=events.get(r.code, []),
                    github_repo=slug_of.get(r.repo) if r.repo else None,
                    approval_ids=related_ids(r.task_id),
                    question_ids=open_question_ids.get(r.code, []),
                )
                for n, r in enumerate(run_rows)
            ],
        ),
        approvals=Approvals(
            open=len(pending),
            items=pending_shown,
            recent=recent_shown,
        ),
        questions=Questions(
            open=len(asks),
            items=[
                QuestionItem(
                    id=q.id,
                    run_code=t(q.run_code, 16),
                    kind=t(q.kind, 16),
                    text=t(q.question, 600),
                    options=t.many(q.options[:QUESTION_OPTIONS], 80),
                    urgent=q.urgent,
                    asked_at=q.asked_at,
                    outside=["text", "options"],
                )
                for q in asks[:QUESTION_ITEMS]
            ],
        ),
        pulse=Pulse(
            open=len(items),
            untold=len(untold),
            items=[
                _pulse_item(i, t)
                for i in sorted(
                    items, key=lambda i: (_URGENCY_RANK[i.urgency], -i.updated_at.timestamp())
                )[:PULSE_ITEMS]
            ],
        ),
        extensions=extension_rows,
        core_jobs=core_job_rows,
        runner=runner,
        trust_rules=[TrustRule(n=r.id, text=t(r.text, 200)) for r in rules[:TRUST_RULE_ITEMS]],
        repos=[
            RepoItem(
                name=t(r.name, 64), mode=r.mode, base=t(r.base, 120), ci=r.ci, slug=t(r.slug, 100)
            )
            for r in repo_rows[:REPO_ITEMS]
        ],
        claude_brain=brain,
        brain_activity=_brain_activity(conn, settings, now, brain),
        trust_rule_count=len(rules),
        repo_count=len(repo_rows),
        diagram=graph,
    )


# --- the diagram ----------------------------------------------------------------------------


def _declared_agents(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """label -> (extension, description) of every ``launch_agents`` entry an installed
    extension's approved manifest declares."""
    out: dict[str, tuple[str, str]] = {}
    for record in ext_store.list_extensions(conn):
        raw = record.manifest.get("launch_agents")
        for entry in cast(list[Any], raw) if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            fields = cast(dict[str, Any], entry)
            label = fields.get("label")
            desc = fields.get("description")
            if isinstance(label, str) and _AGENT_LABEL.fullmatch(label) and _ident(record.name):
                out.setdefault(label, (record.name, desc if isinstance(desc, str) else ""))
    return out


def _job_descriptions(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for record in ext_store.list_extensions(conn):
        for spec in cast(list[Any], record.manifest.get("jobs") or []):
            if isinstance(spec, dict):
                fields = cast(dict[str, Any], spec)
                desc = fields.get("description")
                if isinstance(desc, str) and desc.strip():
                    out[(record.name, str(fields.get("id", "")))] = desc
    return out


_HEALTH_RANK: Final[Mapping[str, int]] = {"ok": 0, "unknown": 1, "degraded": 2, "failing": 3}


def worst_health(states: Iterable[HealthState]) -> HealthState:
    return cast(HealthState, max(states, key=_HEALTH_RANK.__getitem__, default="unknown"))


def _job_health(j: Job) -> HealthState:
    if j.fail_streak > 0:
        return "degraded" if j.fail_streak < 3 else "failing"
    return "ok" if j.last_outcome in ("ok", "dry_run", "skipped_paused") else "unknown"


def _diagram(
    conn: sqlite3.Connection,
    *,
    extensions: Sequence[Extension],
    core_jobs: Sequence[Job],
    health: Mapping[str, HealthRow],
    brain: ClaudeBrain | None,
    runner: Runner,
    repo_names: Sequence[str],
    agents: Sequence[AgentState] | None,
    now: datetime,
    t: _Text,
) -> Diagram:
    nodes: list[DiagramNode] = []
    edges: list[DiagramEdge] = []
    ids: set[str] = set()
    cut = False

    def add(node: DiagramNode) -> bool:
        nonlocal cut
        if node.id in ids:
            return False
        if len(nodes) >= DIAGRAM_NODES:
            cut = True
            return False
        ids.add(node.id)
        nodes.append(node)
        return True

    def link(source: str, target: str, label: str | None = None) -> None:
        nonlocal cut
        if source in ids and target in ids and len(edges) >= DIAGRAM_EDGES:
            cut = True
        elif source in ids and target in ids:
            edges.append(DiagramEdge(source=source, target=target, label=t.opt(label, 40)))

    # The core skeleton, health from its check (or the state the snapshot already has).
    for c in diagram_parts.CORE:
        row = health.get(c.check) if c.check else None
        state: HealthState | None = row.state if row else ("unknown" if c.check else None)
        if c.id == "brain":
            state = "unknown" if brain is None else ("ok" if brain.state == "up" else "degraded")
        elif c.id == "workers":
            state = "ok" if runner.set_up else "degraded"
        elif c.id == "scheduler" and core_jobs:
            state = worst_health([_job_health(j) for j in core_jobs])
        last = max((j.last_run_at for j in core_jobs if j.last_run_at), default=None)
        add(DiagramNode(
            id=f"core:{c.id}", kind="core", label=c.label, text=t(c.text, 300), health=state,
            group=c.group,
            check=c.check if row else None, module=c.module,
            last_run_at=last if c.id == "scheduler" else None,
            state=None if brain is None or c.id != "brain" else brain.state,
        ))  # fmt: skip
    for source, target, label in diagram_parts.CORE_EDGES:
        link(f"core:{source}", f"core:{target}", label)
    for j in core_jobs:
        what = diagram_parts.CORE_JOB_TEXT.get(j.id)
        node_id = f"job:core/{j.id}"
        if add(DiagramNode(
            id=node_id, kind="job", label=j.id, parent="core:scheduler",
            text=t(diagram_parts.job_sentence("core", j.id, j.schedule, j.tz, None, None, what,
                                              now), 300),
            tags=["core job"], health=_job_health(j), last_run_at=j.last_run_at,
            last_outcome=j.last_outcome, ext="core", job=j.id,
        )):  # fmt: skip
            link("core:scheduler", node_id)

    # launchd agents: the daemon's own, com.resonant.*, those an extension declares, and
    # com.<repo>.* for a registered repo. Nothing else the scan saw ever gets here.
    declared = _declared_agents(conn)
    seen = {a.label: a for a in agents or ()}
    allowed = {
        label for label in seen if diagram_parts.agent_allowed(label, declared, repo_names)
    } | set(declared)
    known = list(diagram_parts.KNOWN_AGENTS)
    ordered = [*known, *sorted(allowed - set(known))]
    for label in [x for x in ordered if t(x, 128) == x][:DIAGRAM_PROCESSES]:
        a = seen.get(label)
        agent_health, agent_state = diagram_parts.agent_health(a) if agents is not None else (
            "unknown", "not scanned")  # fmt: skip
        owner = declared.get(label)
        if label in diagram_parts.KNOWN_AGENTS:
            text = diagram_parts.KNOWN_AGENTS[label]
        elif owner is not None:
            text = owner[1] or f"A launchd agent the {owner[0]} extension declares."
        elif label.startswith("com.resonant."):
            text = "A Resonant launchd agent."
        else:
            text = "A launchd agent named after a repo in the registry."
        node_id = f"proc:{label}"
        tags = ["launchd"] + (["plist"] if a is not None and a.plist else [])
        add(DiagramNode(
            id=node_id, kind="process", label=t(label, 100), text=t(text, 300),
            tags=tags, health=cast(HealthState, agent_health), state=t(agent_state, 32),
            ext=owner[0] if owner else None, group="processes",
        ))  # fmt: skip
        if label in diagram_parts.AGENT_RUNS:
            link(node_id, f"core:{diagram_parts.AGENT_RUNS[label]}", "runs")

    # Extensions, from their approved manifests (already redacted and capped in ``extensions``).
    job_text = _job_descriptions(conn)
    for e in extensions:
        hosts = [m.host for m in e.mcp_servers if m.transport == "http" and m.host]
        transport = {m.id: (m.transport, m.host) for m in e.mcp_servers}
        kinds = [
            (tool.effect, tool.level,
             diagram_parts.tool_kind(tool.via, *transport.get(tool.server or "", (None, None))))
            for tool in e.tools
        ]  # fmt: skip
        ext_id = f"ext:{e.name}"
        last = max((j.last_run_at for j in e.jobs if j.last_run_at), default=None)
        sentence = diagram_parts.extension_sentence(
            e.name, e.description, kinds, hosts, len(e.jobs)
        )
        if not add(DiagramNode(
            id=ext_id, kind="extension", label=e.name, text=t(sentence, 300),
            tags=sorted({k for _e, _l, k in kinds}), health=e.health,
            check=EXT_CHECK_PREFIX + e.name if EXT_CHECK_PREFIX + e.name in health else None,
            state=e.status, last_run_at=last, ext=e.name, group="extensions",
        )):  # fmt: skip
            continue
        link("core:executor", ext_id, "tool calls")
        if e.jobs:
            link("core:scheduler", ext_id, "jobs")
        if e.jobs or e.report_max:
            link(ext_id, "core:pulse", "reports")
        for m in e.mcp_servers:
            server_id = f"mcp:{e.name}/{m.id}"  # at most 4 + 64 + 1 + 64
            where = (
                f"hosted MCP server at {m.host}"
                if m.transport == "http" and m.host
                else (
                    "local MCP server over stdio" if m.transport == "stdio" else "hosted MCP server"
                )
            )
            if add(DiagramNode(
                id=server_id, kind="server", label=m.id, parent=ext_id,
                text=t(f"{e.name}/{m.id}: {where}; Resonant lists and pins its tools at install "
                       "and re-checks them for drift.", 300),
                tags=[m.transport], ext=e.name,
            )):  # fmt: skip
                link(ext_id, server_id, "MCP")
        for tool, (effect, level, kind) in zip(e.tools, kinds, strict=True):
            tool_id = f"tool:{e.name}/{tool.server + '/' if tool.server else ''}{tool.name}"
            words = diagram_parts.level_words(effect, level)
            if len(tool_id) <= _ID_MAX and add(DiagramNode(
                id=tool_id, kind="tool", label=tool.name, parent=ext_id,
                text=t(diagram_parts.tool_sentence(e.name, tool.name, kind, effect, level,
                                                   tool.description), 300),
                tags=[tool.via, effect, level, words], ext=e.name,
            )):  # fmt: skip
                server_id = f"mcp:{e.name}/{tool.server}"
                link(server_id if tool.server and server_id in ids else ext_id, tool_id)
        for j in e.jobs:
            job_id = f"job:{e.name}/{j.id}"
            what = job_text.get((e.name, j.id))
            if add(DiagramNode(
                id=job_id, kind="job", label=j.id, parent=ext_id,
                text=t(diagram_parts.job_sentence(e.name, j.id, j.schedule, j.tz, j.effect,
                                                  j.level, what, now), 300),
                tags=[x for x in (j.effect, j.level) if x], health=_job_health(j),
                last_run_at=j.last_run_at, last_outcome=j.last_outcome, ext=e.name, job=j.id,
            )):  # fmt: skip
                link(ext_id, job_id, "cron")
        for r in e.repos:
            repo_id = f"repo:{e.name}/{r}"
            if len(repo_id) <= _ID_MAX and add(DiagramNode(
                id=repo_id, kind="repo", label=r, parent=ext_id,
                text=t(f"{e.name} is about {r}: a request that names {e.name} means this repo "
                       "for a worker run. The link grants nothing.", 300),
                tags=["repo"], ext=e.name,
            )):  # fmt: skip
                link(ext_id, repo_id, "linked")
        for label, (owner, _desc) in declared.items():
            if owner == e.name:
                link(ext_id, f"proc:{label}", "owns")
    groups = [DiagramGroup(id=g.id, label=g.label, text=g.text) for g in diagram_parts.GROUPS]
    return Diagram(
        nodes=nodes,
        edges=edges,
        groups=groups,
        processes_scanned=agents is not None,
        truncated=cut,
    )


class _Context:
    """The tasks the builder looks at more than once (taint, run codes), read once."""

    def __init__(self, conn: sqlite3.Connection, now: datetime) -> None:
        self.conn = conn
        self.now = now
        self.task_ids: list[str] = []
        self._tasks: dict[str, Task | None] = {}
        self._codes: dict[str, str] | None = None

    def add_tasks(self, ids: Iterable[str]) -> None:
        for i in ids:
            if i not in self.task_ids:
                self.task_ids.append(i)

    def load(self, tasks: Sequence[Task]) -> None:
        for task in tasks:
            self._tasks[task.id] = task

    def task(self, task_id: str | None) -> Task | None:
        if task_id is None:
            return None
        if task_id not in self._tasks:
            self._tasks[task_id] = task_store.get_task(self.conn, task_id)
        return self._tasks[task_id]

    def tainted(self, task_id: str | None) -> bool:
        task = self.task(task_id)
        return task is not None and system_texts.tainted(task.checkpoint)

    def run_code(self, task_id: str | None) -> str | None:
        if task_id is None:
            return None
        if self._codes is None:
            self._codes = runs.codes_by_task(self.conn, self.task_ids)
        return self._codes.get(task_id)


def _events(rows: Sequence[tuple[datetime, str]], *, strip: str = "") -> list[Event]:
    out: list[Event] = []
    for at, action in rows:
        name = action.removeprefix(strip) if strip else action
        if EVENT_NAME.fullmatch(name):
            out.append(Event(at=at, event=name))
    return out


def _task_label(task: Task, t: _Text) -> str | None:
    title = task.checkpoint.get("title")
    if isinstance(title, str) and title.strip():
        return t(title.strip(), 120)
    if task.kind == "job":
        from resonant.extensions.scheduler import job_checkpoint  # a cycle at import time

        found = job_checkpoint(task.checkpoint)
        if found is not None:
            return t(f"{found[0]}/{found[1]}", 120)
    return None


def _task_item(
    task: Task,
    ctx: _Context,
    t: _Text,
    *,
    history: Sequence[tuple[datetime, str]],
    approval_ids: list[str],
) -> TaskItem:
    error = t.opt(task.error, 300)
    return TaskItem(
        id=t(task.id, 64),
        kind=t(task.kind, 32),
        runner=t(task.runner, 32),
        extension=t.opt(task.extension, 64),
        status=task.status,
        wait_reason=task.wait_reason,
        wake_at=task.wake_at,
        attempts=task.attempts,
        step_no=task.step_no,
        created_at=task.created_at,
        updated_at=task.updated_at,
        label=_task_label(task, t),
        error=error,
        run_code=ctx.run_code(task.id),
        history=_events(history)[-TASK_HISTORY:],
        approval_ids=approval_ids,
        outside=["error"] if error else [],
    )


def _linear_ids(ask: str | None, branch: str | None = None) -> list[str]:
    seen: list[str] = []
    for m in _LINEAR.finditer((ask or "")[:2000]):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    keys = {i.split("-", 1)[0] for i in seen}
    text = (branch or "")[:300]
    for m in _LINEAR_BRANCH.finditer(text):
        key = m.group(1).upper()
        starts_segment = m.start() == 0 or text[m.start() - 1] == "/"
        ident = f"{key}-{m.group(2)}"
        if (key in keys or starts_segment) and ident not in seen:
            seen.append(ident)
    return seen[:RUN_LINEAR_IDS]


def _run_item(
    r: runs.Run,
    conn: sqlite3.Connection,
    ctx: _Context,
    t: _Text,
    reports: Any,
    *,
    detail: bool,
    detected: Sequence[tuple[EffectKind, str, bool]],
    history: Sequence[tuple[datetime, str]],
    github_repo: str | None,
    approval_ids: list[str],
    question_ids: list[int],
) -> RunItem:
    did = t.many(reports.did_list(conn, r.code)[-RUN_DID:], 80) if detail else []
    effects = (
        [
            RunEffect(kind=k, what=t(what, 80), high=high)
            for k, what, high in detected[-RUN_EFFECTS:]
        ]
        if detail
        else []
    )
    flagged = t.many(r.flagged_paths[:RUN_FLAGGED], 120) if detail else []
    summary = t.opt(r.summary, 800) if detail else None
    failure = t.opt(r.failure, 400) if detail else None
    note = t.opt(r.last_note, 200)
    merged_by = reports.merged_by_of(conn, r.code)
    outside = [
        name
        for name, present in (
            ("summary", summary),
            ("failure", failure),
            ("last_note", note),
            ("did", did),
            ("effects", effects),
            ("flagged_paths", flagged),
        )
        if present
    ]
    if ctx.tainted(r.task_id):
        outside.insert(0, "ask")
    return RunItem(
        code=t(r.code, 16),
        state=r.status,
        waiting_reason=r.wait_reason if r.status == "waiting" else None,
        started_at=r.started_at,
        ended_at=r.ended_at,
        detected=len(detected),
        repo=t(r.repo, 100),
        ask=t(r.ask, 300),
        branch=t.opt(r.branch, 120),
        base_branch=t.opt(r.base_branch, 120),
        head_sha=r.head_sha if r.head_sha and _HEAD_SHA.fullmatch(r.head_sha) else None,
        pr_url=r.pr_url if r.pr_url and _PR_URL.fullmatch(r.pr_url) else None,
        pr_number=r.pr_number,
        github_repo=t.opt(github_repo, 100),
        created_at=r.created_at,
        remote_url=r.remote_url if r.status == "running" else None,
        task_id=t.opt(r.task_id, 64),
        step=r.step,
        attempts=r.attempts,
        sessions=r.sessions,
        exit_code=r.exit_code,
        cost_usd=r.cost_usd,
        tokens_in=r.tokens_in,
        tokens_out=r.tokens_out,
        session_seconds=r.session_seconds,
        wait_seconds=r.wait_seconds,
        files_changed=r.files_changed,
        flagged_paths=flagged,
        summary=summary,
        failure=failure,
        last_note=note,
        did=did,
        effects=effects,
        merged=t.many(reports.merged_list(conn, r.code)[:RUN_MERGED], 40),
        merged_by=None if merged_by is None else merged_by.by,
        history=_events(history, strip="run.")[-RUN_HISTORY:] if detail else [],
        approval_ids=approval_ids,
        question_ids=question_ids,
        linear_ids=_linear_ids(r.ask, r.branch),
        detail=detail,
        outside=outside,
    )


def _detected(
    conn: sqlite3.Connection, code: str, reports: Any
) -> list[tuple[EffectKind, str, bool]]:
    """Run ``code``'s detected side effects as (kind, what, high), unknown kinds left out."""
    # ``detected_list`` casts the stored kind unchecked: a stale kv row can hold anything.
    return [
        (cast(EffectKind, e.kind), cast(str, e.what), cast(bool, e.high))
        for e in reports.detected_list(conn, code)
        if e.kind in _EFFECT_KINDS
    ]


def _detected_counts(per_run: Iterable[Sequence[tuple[EffectKind, str, bool]]]) -> DetectedEffects:
    by_kind: dict[EffectKind, int] = {}
    high = 0
    for effects in per_run:
        for kind, _what, is_high in effects:
            by_kind[kind] = by_kind.get(kind, 0) + 1
            high += is_high
    return DetectedEffects(by_kind=by_kind, high=high)


def _pulse_item(i: pulse.Item, t: _Text) -> PulseItem:
    from resonant.extensions.pulse_job import CORE, NOTICE_PREFIX  # a cycle at import time

    code = _RUN_KEY.fullmatch(i.key) if i.source == CORE else None
    notice = i.source == CORE and i.key.startswith(NOTICE_PREFIX)  # mirrors ``is_outside``
    return PulseItem(
        source=t(i.source, 64),
        key=t(i.key, 120),
        title=t(i.title, 200),
        urgency=i.urgency,
        state="new" if i.changed else "told",
        updated_at=i.updated_at,
        expires_at=i.expires_at,
        told_at=i.told_at,
        run_code=None if code is None else code.group(1).upper(),
        outside=[] if notice else ["title"],
    )


def _approval_items(
    conn: sqlite3.Connection,
    rows: Sequence[approvals.Approval],
    ctx: _Context,
    t: _Text,
    *,
    pending: bool,
) -> list[ApprovalItem]:
    """``rows`` as what one answer decides: each lone approval, and each batch (BA-1) once,
    where its first row is. One answer decides all of a batch (``yes`` / ``no``, or ``yes
    1,3`` for some and the rest dropped, PER-255), so it is one row. Its id and code are its
    first approval's and its changes are in its order (both as its prompt showed them); it
    expires as one. A batch whose approvals aren't all in ``rows`` (one decided on its own: a
    race) can't be answered as one (the gate's ``_units``; the runner drops the rest), so what
    is left of it shows as lone rows, never under a code that no longer binds."""
    index = approvals.batches_of(conn, [a.id for a in rows])
    by_id = {a.id: a for a in rows}
    out: list[ApprovalItem] = []
    seen: set[str] = set()
    for a in rows:
        batch = index.get(a.id)
        if batch is None or any(i not in by_id for i in batch.approval_ids):
            out.append(_approval_item([a], batch=False, ctx=ctx, t=t, pending=pending))
            continue
        if batch.id in seen:
            continue
        seen.add(batch.id)
        members = [by_id[i] for i in batch.approval_ids]
        out.append(
            _approval_item(
                members, batch=True, ctx=ctx, t=t, pending=pending, asked_at=batch.created_at
            )
        )
    return out


_EFFECT_RANK: Final[Mapping[str, int]] = {"read": 0, "write": 1, "pay": 2}


def _approval_item(
    members: Sequence[approvals.Approval],
    *,
    batch: bool,
    ctx: _Context,
    t: _Text,
    pending: bool,
    asked_at: datetime | None = None,
) -> ApprovalItem:
    """One row: its level (a batch: the strictest), the strictest effect of its approvals,
    and whether a yes needs the code (as the gate's ``_Unit.needs_code``: any approval needs
    it, or a batch has a change that widens trust)."""
    first = members[0]
    effect = max((m.effect.value for m in members), key=_EFFECT_RANK.__getitem__)
    decided = [m.decided_at for m in members if m.decided_at is not None]
    return ApprovalItem(
        id=t(first.id, 64),
        member_ids=[t(m.id, 64) for m in members],
        code=first.code,
        summary=t(f"{len(members)} changes" if batch else first.summary, 300),
        level=strictest([m.level for m in members]).value,
        effect=cast(EffectName, effect),
        needs_code=any(m.require_code for m in members)
        or (batch and any(widens_trust(m.tool, m.extension) for m in members)),
        tool=t(first.tool, 64),
        extension=t.opt(first.extension, 64),
        status="pending" if pending else first.status,
        asked_at=asked_at or first.created_at,
        expires_at=min(m.expires_at for m in members),
        decided_at=None if pending or not decided else max(decided),
        decided_by=None
        if pending
        else t.opt(next((m.decided_by for m in members if m.decided_by), None), 40),
        task_id=t.opt(first.task_id, 64),
        run_code=ctx.run_code(first.task_id),
        changes=[
            ApprovalChange(summary=t(change_line(m), 200), level=m.level.value)
            for m in members[:APPROVAL_CHANGES]
        ]
        if batch
        else None,
        outside=["summary", "changes"] if ctx.tainted(first.task_id) else [],
    )


_RC_URL = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_-]{1,200}")


def _claude_brain(conn: sqlite3.Connection, t: _Text) -> ClaudeBrain | None:
    found = claude_status(conn)
    if found is None:
        return None
    url = found.get("remote_url")
    up = found.get("state") == "up"
    return ClaudeBrain(
        state="up" if up else "down",
        turns=int(found.get("turns") or 0),
        remote_url=url if up and isinstance(url, str) and _RC_URL.fullmatch(url) else None,
        tools=_brain_tools(conn, t),
    )


def _brain_tools(conn: sqlite3.Connection, t: _Text) -> list[BrainTool]:
    out: list[BrainTool] = []
    for row in recent_tools(conn, BRAIN_TOOL_ITEMS):
        try:
            out.append(
                BrainTool(
                    at=datetime.fromisoformat(str(row.get("at"))),
                    tool=t(str(row.get("tool") or "?"), 64),
                    decision=t(str(row.get("decision") or "?"), 16),
                    command=t(str(row.get("command") or ""), 200),
                    cwd=t(str(row.get("cwd") or ""), 120),
                    status="error" if row.get("status") == "error" else "ok",
                    write=row.get("write") is True,
                    outside=["command"],
                )
            )
        except ValueError:
            continue
    return out


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
    )


if __name__ == "__main__":
    print(schema_json(), end="")

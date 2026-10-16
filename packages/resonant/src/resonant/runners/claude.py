"""ClaudeRunner (``runner="claude"``): one Claude Code session per run, in a worktree of the
owner's checkout or, with no repo, a scratch dir (docs/phase5a-plan.md D2 to D4,
docs/employee-plan.md D1).

A run is a task of kind ``run`` plus a row in ``runs`` (migration 006) that holds all the state a
restart needs. One ``step`` drives a run from its saved step to the next stopping point, holding
a Claude slot the whole time (two runs on one repo go at once, each in its own worktree):

    prepare   clone the checkout on first use, fetch it, note whether the base has CI, add the
              run's worktree on a new branch and link the checkout's local config into it
              (``Workspace.prepare``); a run with no repo gets an empty scratch dir
    session   a per-run GitHub App token for the one repo (``runner.github_app``), the cached
              sandbox self-check (fails closed), then ``claude -p`` under ``sandbox.wrap`` with a
              from-scratch environment. The session itself pushes its branch, opens the PR,
              watches CI and fixes it (docs/repos-plan.md D10); the token is revoked when the
              session ends, however it ends. A run with no repo gets no token and ends ``done``
              with Claude's summary
    harvest   (the saved step keeps this name) find the PR by its head branch and check it the
              way a merge will; no PR and no pushed commits is ``no_changes``
    reply     the code-built message to the requester

Each step is saved in ``runs.step`` before it starts, so a crash resumes at the last one; the
session is resumed by id (``--resume``) in the same run dir, after the old process tree has been
reaped. After the session starts the daemon writes into the run dir only to refresh the token
files, through ``O_NOFOLLOW`` directory fds. Merging (``merge_run``) fetches the PR's head into
the daemon-only mirror and runs the checks first; no merge path skips them.

Outcomes. A usage limit parks the run (``Wait("usage_reset")``: slot and lock freed; the wait
doesn't count as session time or an attempt, and 24 h of waiting in total fails the run). The
time limit, the disk limit, the turn or dollar limit and a Claude error fail the run and keep its
dir and worktree ("work kept"); what the session already pushed stays on GitHub. ``/cancel`` and
``/kill`` end the whole process tree, revoke the token and delete the run dir and the worktree
(or scratch dir); a merge deletes them too, and the daily job ``runner.keep_days`` after the run
ended. The owner's checkout is never deleted.

Worker CLI (EM-3, ``worker_cli``/``worker_desk``). A session reaches the daemon with
``resonant ask|note|check`` over its run's socket, open only while a session runs. A turn that
ends with a question open parks the run (``Wait("human_reply")``; run ``waiting``, reason
``question``, slot freed, time not counted). The answer, an owner message or the ask timeout
resumes the same session with ``--resume`` and the news as the prompt. API for the relay
(EM-5): :meth:`~ClaudeRunner.open_questions`, :meth:`~ClaudeRunner.answer_question`,
:meth:`~ClaudeRunner.deliver_message`, :attr:`~ClaudeRunner.events`.

Public API for the tools and slash commands (P5A-4): :meth:`ClaudeRunner.start_run`,
:meth:`~ClaudeRunner.list_runs`, :meth:`~ClaudeRunner.cancel_run`,
:meth:`~ClaudeRunner.merge_run`. Their results are plain dataclasses; every reply text is built
here, from code, never from model output: Claude's closing summary is cleaned, redacted,
clipped, and only shown after a code-built ``R3:`` prefix.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import psutil

from resonant import killswitch
from resonant.config import RunnerConfig
from resonant.gate.codes import clean_external
from resonant.gateway.channel import Channel, ChannelError, ChannelRefused
from resonant.loop.types import (
    PRIORITY_OWNER,
    Done,
    Fail,
    NewTask,
    StepContext,
    StepOutcome,
    Task,
    Wait,
)
from resonant.redact import Redactor
from resonant.runner import github_app as github_app_mod
from resonant.runner.checks import CheckFailed, GitError
from resonant.runner.cleanup import (
    RUN_CODE,
    cleanup,
    delete_run_dir,
    rmtree_nofollow,
    watch_disk,
    wipe_secret_dirs,
)
from resonant.runner.github_app import GitHubApp, GitHubAppError
from resonant.runner.resolve import (
    Ambiguous,
    GhGitHub,
    GitHub,
    GitHubError,
    NoAccess,
    NotFound,
    Resolver,
)
from resonant.runner.workspace import (
    CREDS_DIR,
    UPSTREAM_LATER,
    MergeRefused,
    NoChanges,
    Prepared,
    RunnerError,
    Workspace,
    read_session_token,
    session_git_env,
    write_session_credentials,
)
from resonant.runners import reaper, worker_cli
from resonant.runners.reaper import MARKER_ENV, Target
from resonant.runners.worker_cli import WorkerServer
from resonant.runners.worker_desk import (
    BLOCK_S,
    OpenQuestion,
    Relayed,
    RelayRefused,
    TrustChecker,
    WorkerDesk,
    WorkerEvents,
)
from resonant.sandbox import (
    SandboxError,
    SandboxParams,
    default_deny_exec,
    default_read_paths,
    link_tools,
    prepare_dirs,
    resolve_tool,
    scratch_env,
    wrap,
)
from resonant.sandbox.probes import cached_self_check
from resonant.secrets import (
    GITHUB_APP_KEY_REF,
    RunnerSecrets,
    SecretError,
    SecretStore,
    load_runner_secrets,
    missing_runner_secrets,
)
from resonant.store import pulse as pulse_store
from resonant.store import questions as questions_store
from resonant.store import repos as repo_store
from resonant.store import runs
from resonant.store import tasks as task_store
from resonant.store.audit import audit
from resonant.store.db import atomic, transaction, utcnow
from resonant.store.runs import Run
from resonant_sdk import Event

if TYPE_CHECKING:
    from resonant.executor import CallContext
    from resonant.extensions.scheduler import CoreDeps, CoreJob

log = logging.getLogger(__name__)

RUNNER_NAME = "claude"
TASK_KIND = "run"
MAX_ATTEMPTS = 2  # a session, and one resume after a daemon crash
MAX_WAIT_S = 24 * 3600.0  # usage-limit waiting, in total
DEFAULT_WAIT_S = 3600.0  # when the limit message names no reset time
MIN_WAIT_S = 60.0
DEFER_S = 15.0  # how long a run waits for a free repo lock or a free slot
MAX_LINE = 1024 * 1024  # one stream-json line, in the transcript and in memory
TRUNCATED_TAIL = 256  # dropped from a truncated line: a secret may be cut at the limit
MAX_TRANSCRIPT = 256 * 1024 * 1024
MAX_ASK = 4000
SUMMARY_MAX = 600
REASON_MAX = 240
MAX_TURNS = 200
CANCEL_GRACE_S = 2.0  # /kill and /cancel: SIGTERM, then SIGKILL after this
STEP_SLACK_S = 600.0  # prepare, harvest, import and publish on top of the session limit
SAVE_EVERY_S = 30.0
PULSE_TTL_S = 48 * 3600
PULSE_SOURCE = "core"
KILL_REASON = "stopped by /kill"
CHECK_CACHE = "sandbox-check.json"
TOOL_NAMES = ("claude", "uv")
MAX_FIX_ATTEMPTS = 3  # CI fixes the session may push after the first push
TOKEN_REFRESH_S = 50 * 60  # installation tokens last an hour: a fresh one after 50 minutes
# in all: the app's three tries (each up to its own timeout) and the backoff between them
REVOKE_TIMEOUT_S = (
    github_app_mod.ATTEMPTS * github_app_mod.TIMEOUT_S
    + github_app_mod.BACKOFF_S * (2**github_app_mod.ATTEMPTS - 1)
    + 5.0
)

RUNNER_PROMPT = (
    "You are working in a git worktree of {repo} (your current directory), on branch {branch}, "
    "which is already checked out. The checkout's local config (.env files and the like) is "
    "linked in and kept out of git: use it, never commit it. git and gh are set up with "
    "credentials for this repo only. Commit your work "
    "with clear messages, then push it with `git push -u origin {branch}`. Open a pull request "
    "against {base} with `gh pr create --base {base} --head {branch}` and a short title and "
    "description. Then wait for CI with `gh pr checks --watch`. If CI fails, read the failing "
    "logs (`gh run view --log-failed`), fix the problem, commit and push again; do this at most "
    "{attempts} times. Never push to {base} or to any branch but {branch}, don't force-push, and "
    "don't change remotes, git config or the credential files. The credentials are refreshed in "
    "place about every 50 minutes: if gh or git fails with an authentication error, run it "
    "again. Stop when CI is green or you are "
    "out of attempts. End with a 1-3 line summary: what changed, the PR number, whether CI is "
    "green, and anything left undone."
)
SCRATCH_PROMPT = (
    "You are working in an empty scratch directory (your current directory); this task is not "
    "tied to a repo and you have no GitHub credentials. Do what is asked, and end with a 1-5 "
    "line summary of what you did, what you found and anything left undone: it is what the "
    "owner reads."
)
WORKER_PROMPT = (
    "\n\nYou can reach the owner with the `resonant` command on your PATH (plain-text replies). "
    '`resonant ask "<question>" [--options a,b,c] [--urgent]`: only when you are blocked on a '
    "decision only the owner can make. It waits for the answer; if it says there is no answer "
    "yet, finish what you can, then end your turn with a short summary: you'll be resumed with "
    'the answer. `resonant note "<progress>" [--urgent]`: on meaningful progress (--urgent only '
    "for what the owner must see now). `resonant check --system <s> --action <kind> "
    '[--irreversible] [--cost <usd>] "<what>"`: before anything that could be net new: a '
    "deploy, a write to production data, an outgoing email or message, spending money, deleting "
    "data, a force-push, or a system you haven't used in this task. Go ahead only on ok; on "
    "denied, don't. Replies can end with messages from the owner: follow them."
)
RESUME_PROMPT = (
    "Your session was interrupted. Continue the task you were given. If it is already done, "
    "make sure the work is pushed, the pull request is open and its CI has been checked, and "
    "give the closing summary."
)

_LIMIT = re.compile(r"usage limit|rate limit|hit your limit|limit reached", re.IGNORECASE)
_EPOCH = re.compile(r"\|\s*(\d{10})\b")


# --- results of the public API -------------------------------------------------------------


@dataclass(frozen=True)
class Started:
    code: str
    repo: str
    max_minutes: int
    new_repo: str | None = None  # the slug, when this run registered the repo
    warning: str | None = None  # e.g. the base branch isn't protected

    @property
    def text(self) -> str:
        new = f" (new repo: {self.new_repo})" if self.new_repo else ""
        where = self.repo or "no repo"
        text = (
            f"On it: {self.code} · {where}{new}. Up to {self.max_minutes}m. "
            f"/runs to check, /cancel {self.code} to stop."
        )
        return f"{text}\n{self.warning}" if self.warning else text


@dataclass(frozen=True)
class Refused:
    """A request the runner won't take. ``reason`` is a code-built sentence."""

    reason: str


@dataclass(frozen=True)
class Cancelled:
    code: str

    @property
    def text(self) -> str:
        return f"{self.code} cancelled."


@dataclass(frozen=True)
class Merged:
    code: str
    pr_number: int
    flagged: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        line = f"{self.code} merged: PR #{self.pr_number}."
        return f"{line}\n⚠ changes {', '.join(self.flagged)}" if self.flagged else line


# --- code-built replies --------------------------------------------------------------------


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return f"{int(seconds)}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def reply_text(run: Run, ci: str | None = None, *, no_ci: bool = False) -> str | None:
    """The message for an ended run (None for a state that has none). ``ci`` is the PR's CI
    as a word (``green``, ``red``, ``pending``, ``none``), looked up when the reply is sent.
    ``no_ci``: the repo has no pull_request workflow, so the run can't be merged from here."""
    took = fmt_duration(run.session_seconds)
    summary = f"\n{run.code}: {run.summary}" if run.summary else ""
    match run.status:
        case "ready":
            label = "no CI" if ci == "none" else f"CI {ci or 'unknown'}"
            text = f"{run.code} done: PR #{run.pr_number} ({label}) · {run.pr_url}"
            if run.files_changed is not None:
                text += f" · {_plural(run.files_changed, 'file')}"
            text += f" · {took}"
            if run.flag_line:
                text += f"\n{run.flag_line}"
            if run.failure:  # the merge-time checks already refuse it
                text += f"\n⚠ it can't be merged as it is: {run.failure}"
            if no_ci:
                return text + f"\nno CI: {run.repo} has no pull_request workflow." + summary
            hint = "to merge it" if ci == "green" else "when CI is green"
            return text + f"\n/merge {run.code} {hint}." + summary
        case "no_changes":
            return f"{run.code}: no changes." + (f" {run.summary}" if run.summary else "")
        case "done":  # a run with no repo: Claude's summary is the answer
            return f"{run.code} done ({took})" + (f": {run.summary}" if run.summary else ".")
        case "failed":
            return f"{run.code} failed: {run.failure or 'unknown error'}"
        case "cancelled":
            text = f"{run.code} cancelled."
            if run.pr_url:
                text += f" Its PR was already open: {run.pr_url}"
            return text
        case _:
            return None


def not_set_up_text(missing: Sequence[str], how: Sequence[str]) -> str:
    return f"The runner isn't set up: {', '.join(missing)}. On the Mini: {'; '.join(how)}."


# --- the transcript ------------------------------------------------------------------------


async def read_lines(
    stream: asyncio.StreamReader, cap: int = MAX_LINE
) -> AsyncIterator[tuple[bytes, bool]]:
    """Lines of ``stream`` as ``(bytes, truncated)``, holding at most ``cap`` bytes of a line
    (the rest of a longer line is dropped as it arrives)."""
    buf = bytearray()
    truncated = False
    while chunk := await stream.read(1 << 16):
        start = 0
        while True:
            end = chunk.find(b"\n", start)
            part = chunk[start:] if end < 0 else chunk[start:end]
            room = cap - len(buf)
            if len(part) > room:
                truncated = True
            buf += part[: max(room, 0)]
            if end < 0:
                break
            yield bytes(buf), truncated
            buf.clear()
            truncated = False
            start = end + 1
    if buf or truncated:
        yield bytes(buf), truncated


class TranscriptWriter:
    """Appends a session's stream-json lines to ``<dir>/<code>.jsonl``, outside the run dir.

    The file is opened with ``O_NOFOLLOW`` (a planted symlink is an error, not a write
    elsewhere), mode 0600. Every line is redacted for every secret value before it is written;
    a line over ``cap`` is cut and written as a ``resonant.truncated`` record, with the last
    ``TRUNCATED_TAIL`` bytes dropped so a secret cut at the limit leaves no readable prefix.
    """

    def __init__(
        self, path: Path, redactor: Redactor, *, cap: int = MAX_LINE, limit: int = MAX_TRANSCRIPT
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.redactor = redactor
        self.cap = cap
        self.limit = limit
        self.written = 0
        self._full = False
        self._fd: int | None = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )

    def write(self, raw: bytes, truncated: bool = False) -> None:
        if self._fd is None or self._full:
            return
        text = self.redactor.text(raw.decode(errors="replace"))
        if truncated:
            keep = text[: max(len(text) - TRUNCATED_TAIL, 0)]
            text = json.dumps({"type": "resonant.truncated", "kept_chars": len(keep), "text": keep})
        data = text.encode() + b"\n"
        if self.written + len(data) > self.limit:
            self._full = True
            data = b'{"type":"resonant.truncated","text":"transcript size limit reached"}\n'
        os.write(self._fd, data)
        self.written += len(data)

    def note(self, kind: str, **fields: Any) -> None:
        """A record the runner adds itself (a stderr line, the exit status)."""
        self.write(json.dumps({"type": f"resonant.{kind}", **fields}).encode())

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)


@dataclass
class StreamState:
    """What the runner keeps from the stream-json messages."""

    session_id: str | None = None
    result: dict[str, Any] | None = None
    lines: int = 0

    def feed(self, raw: bytes) -> None:
        self.lines += 1
        if not raw.startswith(b"{"):
            return
        try:
            obj: Any = json.loads(raw)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        msg: dict[str, Any] = obj  # pyright: ignore[reportUnknownVariableType]
        if msg.get("type") == "system" and isinstance(msg.get("session_id"), str):
            self.session_id = msg["session_id"]
        elif msg.get("type") == "result":
            self.result = msg


@dataclass(frozen=True)
class LimitHit:
    reset_at: datetime | None


def usage_limit(result: Mapping[str, Any]) -> LimitHit | None:
    """Whether a final stream-json ``result`` is a usage or rate limit, and when it resets.

    The shape is Claude Code's, read defensively: an error result whose subtype or text names
    a limit; the reset is an epoch in ``resets_at``/``reset_at`` or after a ``|`` in the text.
    """
    if result.get("is_error") is not True:
        return None
    subtype, text = str(result.get("subtype") or ""), str(result.get("result") or "")
    if not (re.search(r"usage_limit|rate_limit", subtype) or _LIMIT.search(text)):
        return None
    epoch: float | None = None
    for key in ("resets_at", "reset_at", "reset_time"):
        value = result.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            epoch = float(value)
    if epoch is None and (m := _EPOCH.search(text)):
        epoch = float(m[1])
    return LimitHit(None if epoch is None else datetime.fromtimestamp(epoch, UTC))


def wake_time(hit: LimitHit, now: datetime) -> datetime:
    """When to try again: the reset time (a minute late), at least a minute and at most a day
    away; an hour when the message named none."""
    if hit.reset_at is None:
        return now + timedelta(seconds=DEFAULT_WAIT_S)
    wake = hit.reset_at + timedelta(seconds=MIN_WAIT_S)
    return min(max(wake, now + timedelta(seconds=MIN_WAIT_S)), now + timedelta(seconds=MAX_WAIT_S))


# --- the runner ----------------------------------------------------------------------------


class _Cancelled(Exception):
    """The run was cancelled (or Resonant paused) while the step was working."""


@dataclass
class _Work:
    """One step's working set. ``redactor`` knows every secret of the run."""

    task: Task
    ctx: StepContext
    secrets: RunnerSecrets = field(repr=False)
    redactor: Redactor = field(repr=False)


Wrap = Callable[[Sequence[str], SandboxParams], Sequence[str]]
SelfCheck = Callable[[SandboxParams], tuple[bool, str]]


class ClaudeRunner:
    name = RUNNER_NAME
    uses_claude_slot = True

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        runner: RunnerConfig,
        channel: Channel,
        secrets: SecretStore,
        timezone: str = "UTC",
        max_runs: int = 2,
        workspace: Workspace | None = None,
        github: Callable[[RunnerSecrets], GitHub] | None = None,
        github_app: GitHubApp | None = None,
        create_task: Callable[[NewTask], str] | None = None,
        cancel_task: Callable[[str], bool] | None = None,
        wrap: Wrap | None = None,
        self_check: SelfCheck | None = None,
        claude_cmd: Sequence[str] | None = None,
        read_paths: Sequence[Path] | None = None,
        max_session_s: int | None = None,
        max_turns: int = MAX_TURNS,
        poll_s: float = 0.25,
        kill_grace_s: float = reaper.GRACE_S,
        disk_interval_s: float = 60.0,
        disk_limit_bytes: int | None = None,
        cache_limit_bytes: int | None = None,
        defer_s: float = DEFER_S,
        refresh_s: float = TOKEN_REFRESH_S,
        clock: Callable[[], datetime] = utcnow,
        trust: TrustChecker | None = None,
        submit: Callable[[Event], bool] | None = None,
        ask_block_s: float = BLOCK_S,
    ) -> None:
        """``max_runs``: sessions at once (the Claude slots minus the on-call reserve).
        ``github`` builds the resolver's GitHub client from the secrets (tests pass a fake; by
        default ``gh`` through the workspace). ``create_task`` inserts the run's task and wakes
        the loop (``Loop.create_task``) and
        ``cancel_task`` cancels one (``Loop.cancel``); without them the runner writes the task
        itself and relies on its own polling. ``github_app`` mints each run's token (by default
        from ``runner.github_app`` and the Keychain; tests pass one with a fake endpoint) and
        ``refresh_s`` is how long a token is used before a fresh one replaces it. ``wrap``,
        ``self_check`` and ``claude_cmd`` are the seams for tests; by default the Seatbelt
        sandbox, the cached self-probe and the ``claude`` on the daemon's PATH. ``trust``
        decides ``resonant check`` (EM-4's ``TrustChecks``; without it every check is
        denied), ``submit`` wakes a run's task with an event (``Loop.submit``) and
        ``ask_block_s`` is how long one ``resonant ask`` call waits."""
        self.conn = conn
        self.cfg = runner
        self.channel = channel
        self.secrets = secrets
        self.tz = ZoneInfo(timezone)
        self.max_runs = max_runs
        self.workspace = workspace or Workspace(runner, conn)
        self.resolver = Resolver(conn)
        self._github: Callable[[RunnerSecrets], GitHub] = github or self._gh_github
        self.app = github_app or (
            GitHubApp(runner.github_app, secrets) if runner.github_app is not None else None
        )
        self.refresh_s = refresh_s
        self._tokens: dict[str, list[str]] = {}  # run code -> tokens minted and not yet revoked
        self._spent: dict[str, list[str]] = {}  # ... and revoked ones (still redacted)
        self._expiry: dict[str, datetime] = {}  # token -> when GitHub expires it
        self._unrevoked: dict[str, datetime] = {}  # run code -> latest expiry of a token left live
        self._create_task = create_task or self._insert_task
        self._cancel_task = cancel_task
        self._wrap: Wrap = wrap or _sandbox_wrap
        self._self_check: SelfCheck = self_check or self._cached_self_check
        self._claude_cmd = None if claude_cmd is None else tuple(claude_cmd)
        self._read_paths = tuple(default_read_paths() if read_paths is None else read_paths)
        self.max_session_s = max_session_s or runner.max_minutes * 60
        self.max_turns = max_turns
        self.poll_s = poll_s
        self.kill_grace_s = kill_grace_s
        self.disk_interval_s = disk_interval_s
        self.disk_limit_bytes = disk_limit_bytes or runner.max_run_dir_mb * 1024 * 1024
        self.cache_limit_bytes = cache_limit_bytes or runner.max_cache_gb * 1024**3
        self.cancel_grace_s = min(kill_grace_s, CANCEL_GRACE_S)
        self.defer_s = defer_s
        self._blocked = False  # start() began and hasn't finished: nothing may run
        self.clock = clock
        self.max_step_s: float | None = self.max_session_s + STEP_SLACK_S
        self.state_dir = runner.mirror_dir.parent
        self.transcript_dir = self.state_dir / "transcripts"
        self.last_killed = 0  # runs the last kill switch engage ended
        self._working: set[str] = set()  # codes with a step in flight
        self._live: dict[str, Target] = {}  # codes with a session process
        self._cleanup_locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._unhook: Callable[[], None] | None = None
        self.ctl_root = worker_cli.ctl_root(runner.work_dir)
        self.ask_block_s = ask_block_s
        self._submit: Callable[[Event], bool] = submit or self._insert_event
        self._run_redactors: dict[str, Redactor] = {}
        ask_timeout_s = runner.ask_timeout_minutes * 60
        self.desk = WorkerDesk(
            conn,
            channel=channel,
            trust=trust,
            clock=clock,
            ask_timeout_s=ask_timeout_s,
            wake=self._wake,
            redactor_for=lambda code: self._run_redactors.get(code) or Redactor(),
            on_note=self._on_note,
            block_s=ask_block_s,
        )

    def _gh_github(self, secrets: RunnerSecrets) -> GitHub:
        return GhGitHub(self.workspace, secrets)

    def github_for(self, secrets: RunnerSecrets) -> GitHub:
        """The GitHub client for the repo tools (``tools/repos.py``)."""
        return self._github(secrets)

    # -- lifecycle --

    async def start(self) -> int:
        """Daemon start, before the loop resumes anything: end every process a crash left,
        wipe leftover token dirs, end runs whose task is gone, and hook the kill switch.
        Returns how many runs had processes. If it raises, the runner stays blocked (every
        step defers) so no session is resumed before the old processes are known to be gone."""
        self._blocked = True
        try:
            reaped = await self._startup()
        except Exception:
            log.exception("runner startup failed; runs are held until it succeeds")
            with contextlib.suppress(Exception), atomic(self.conn):
                audit(self.conn, "run.startup_failed")
            raise
        self._blocked = False
        return reaped

    async def _startup(self) -> int:
        # runner.repos is only a seed: import it into the registry (idempotent).
        with atomic(self.conn):
            imported = repo_store.import_config(self.conn, self.cfg.repos)
        for name, reason in imported.skipped:
            log.warning("runner.repos entry %s not imported: %s", name, reason)
        for old, new in imported.renamed:  # the mirror follows the repo's name
            async with self.workspace.mirror_lock(old):
                await asyncio.to_thread(self._move_mirror, old, new)
        reaped = 0
        for run in runs.active(self.conn):
            has_process = run.pgid is not None or run.leader_pid is not None
            if not (has_process or run.status == "running"):
                continue
            target = self._target(run)
            if await reaper.reap(target, grace_s=self.kill_grace_s):
                reaped += 1
            if has_process:
                runs.clear_process(self.conn, run.code, crashed=True)
        for run in runs.with_process(self.conn):  # a run that ended with a process recorded
            if not run.active:
                await reaper.reap(self._target(run), grace_s=self.kill_grace_s)
                runs.clear_process(self.conn, run.code)
        await self._sweep_credentials()
        await asyncio.to_thread(wipe_secret_dirs, self.cfg.secrets_dir, self.cfg.mirror_dir)
        self.desk.startup()  # no CLI call survived the restart
        await asyncio.to_thread(self._wipe_ctl_dirs)
        await self._reconcile()
        for run in runs.active(self.conn):  # a check closed by the restart is news for it
            if run.status == "waiting" and run.wait_reason == "question":
                self._wake(run, f"startup:{uuid.uuid4().hex}")
        if self._unhook is None:
            self._unhook = killswitch.add_pause_hook(self._on_pause)
        return reaped

    async def _sweep_credentials(self) -> None:
        """Revoke and delete the token left in every ``<work_dir>/R*/creds`` (whatever state
        its run is in: a crash can come after the row says the run ended but before the
        token was revoked). A token minted but not yet written when the daemon died is
        nowhere; it expires within the hour."""

        def codes() -> list[str]:
            try:
                names = sorted(os.listdir(self.cfg.work_dir))
            except OSError:
                return []
            return [n for n in names if RUN_CODE.fullmatch(n)
                    and os.path.lexists(self.cfg.work_dir / n / CREDS_DIR)]  # fmt: skip

        for code in await asyncio.to_thread(codes):
            await self._deprovision(code)

    def _wipe_ctl_dirs(self) -> None:
        """No session survived a restart: every run's socket and token goes."""
        with contextlib.suppress(OSError):
            for name in os.listdir(self.ctl_root):
                if RUN_CODE.fullmatch(name):
                    worker_cli.remove_ctl(self.cfg.work_dir, name)

    async def aclose(self) -> None:
        """Remove the kill switch hook and wait for the kills it started."""
        if self._unhook is not None:
            self._unhook()
            self._unhook = None
        await self.desk.aclose()
        tasks = list(self._background)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _target(self, run: Run) -> Target:
        return Target(
            run.pgid,
            run.leader_pid,
            run.leader_started_at,
            run_dir=self.cfg.work_dir / run.code,
            marker=run.session_id,
        )

    def _move_mirror(self, old: str, new: str) -> None:
        src, dst = self.workspace.mirror_path(old), self.workspace.mirror_path(new)
        if src.is_dir() and not dst.exists():
            src.rename(dst)

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- public API --

    async def setup_missing(self) -> str | None:
        """``The runner isn't set up: …`` naming exactly what is missing, or None."""
        missing: list[str] = []
        how: list[str] = []
        try:
            refs = await missing_runner_secrets(self.secrets)
        except SecretError as e:
            missing.append(f"the Keychain can't be read ({e.reason})")
            how.append("unlock the login keychain")
        else:
            for ref in refs:
                missing.append(f"secret {ref}")
                if ref == GITHUB_APP_KEY_REF:  # a multi-line PEM comes from a file
                    how.append(
                        f"resonant secret set {ref} --from-file <the downloaded .pem>, "
                        "then delete the .pem"
                    )
                else:
                    how.append(f"resonant secret set {ref.split(' ', 1)[0]}")
        if self.cfg.github_app is None:
            missing.append("runner.github_app isn't set in the config file")
            how.append("add runner.github_app with app_id and installation_id")
        if self._claude_cmd is None and resolve_tool("claude") is None:
            missing.append("the claude CLI isn't installed")
            how.append("install Claude Code")
        return not_set_up_text(missing, how) if missing else None

    async def start_run(self, repo: str | None, ask: str, principal: str) -> Started | Refused:
        """Create a run and its task. ``repo`` is free text (a name, ``owner/name``, a URL):
        the resolver turns it into a registered repo, registering one it hasn't seen. No repo
        (None or blank) is a run in a scratch dir, with no GitHub token and no PR. Refuses
        (with a sentence for the requester) when the runner isn't set up, the repo is
        ambiguous or unknown, or Resonant is paused. Two runs on one repo go at once, each in
        its own worktree."""
        if (text := await self.setup_missing()) is not None:
            return Refused(text)
        ask = ask.strip()
        if not ask:
            return Refused("What should I do? Give the run something to work on.")
        if len(ask) > MAX_ASK:
            return Refused(f"That ask is too long ({len(ask)} characters, at most {MAX_ASK}).")
        if self._paused():
            return Refused("Resonant is paused. /resume first.")
        found: repo_store.Repo | None = None
        new = False
        if repo is not None and repo.strip():
            resolved = await self._resolve(repo)
            if isinstance(resolved, Refused):
                return resolved
            found, new = resolved
        repo = "" if found is None else found.name
        run = runs.create(
            self.conn,
            repo=repo,
            ask=ask,
            principal=principal,
            session_id=str(uuid.uuid4()),
            max_seconds=self.max_session_s,
            max_budget_usd=self.cfg.max_budget_usd,
        )
        spec = NewTask(
            kind=TASK_KIND,
            runner=RUNNER_NAME,
            priority=PRIORITY_OWNER,
            principal=principal,
            checkpoint={"run": run.code, "title": f"run {run.code} · {repo or 'no repo'}"},
        )
        try:
            task_id = self._create_task(spec)
        except Exception:
            runs.finish(self.conn, run.code, "failed", failure="couldn't queue the run")
            raise
        runs.update(self.conn, run.code, task_id=task_id)
        with atomic(self.conn):
            audit(self.conn, "run.start", task_id=task_id, requested_by=principal, run=run.code,
                  repo=repo)  # fmt: skip
        if found is None:
            return Started(run.code, "", max(1, self.max_session_s // 60))
        with atomic(self.conn):
            repo_store.touch(self.conn, found.slug)
        return Started(
            run.code,
            repo,
            max(1, self.max_session_s // 60),
            found.slug if new else None,
            await self._base_warning(found),
        )

    async def _base_warning(self, repo: repo_store.Repo) -> str | None:
        """A sentence for the start reply when the base branch is not protected: the session's
        token can push to it, and a direct push skips the checks a merge runs."""
        try:
            secrets = await load_runner_secrets(self.secrets)
            protected = await self.workspace.base_protected(repo.name, secrets)
        except Exception:  # the start reply must go out whatever GitHub says
            log.warning("base protection unknown", extra={"repo": repo.name})
            return None
        if protected is False:
            return f"{repo.base} isn't protected — the session could push to it directly."
        return None

    async def _resolve(self, text: str) -> tuple[repo_store.Repo, bool] | Refused:
        """The registered repo for the requester's words, and whether it is new."""
        try:
            secrets = await load_runner_secrets(self.secrets)
            resolution = await self.resolver.resolve(text, self._github(secrets))
        except SecretError:
            return Refused((await self.setup_missing()) or "The runner's secrets can't be read.")
        except GitHubError as e:
            return Refused(f"Couldn't reach GitHub: {e}")
        if isinstance(resolution, Ambiguous):
            return Refused(resolution.text)
        if isinstance(resolution, NoAccess):
            return Refused(resolution.text)
        if isinstance(resolution, NotFound):
            recent = ", ".join(r.name for r in repo_store.list_all(self.conn)[:3])
            return Refused(
                f"No repo matches {text[:60]!r}. Which repo?" + (f" {recent}" if recent else "")
            )
        if resolution.repo.mode == "fork":
            return Refused(UPSTREAM_LATER)
        return resolution.repo, resolution.new

    def get_run(self, code: str) -> Run | None:
        return runs.get(self.conn, code)

    def list_runs(self, limit: int = 20) -> list[Run]:
        """Active runs first, then the most recent."""
        return runs.recent(self.conn, limit)

    async def cancel_run(self, code: str, *, reason: str = "cancelled") -> Cancelled | Refused:
        """End an active run: its processes, its dir, its task. Never publishes."""
        run = runs.get(self.conn, code)
        if run is None:
            return Refused(f"No run {code}.")
        if not run.active:
            return Refused(f"{run.code} isn't running ({run.status}).")
        if not self._cancel(run, reason):
            return Refused(f"{run.code} isn't running.")
        if run.code not in self._working:  # queued or waiting: no step will clean up
            await self._finish_cancelled(run.code)
        return Cancelled(run.code)

    async def merge_run(self, code: str) -> Merged | Refused:
        """Squash-merge a ready run's PR (``--match-head-commit``, CI must be green). The
        approval to merge is the caller's; the flagged paths are in the result."""
        run = runs.get(self.conn, code)
        if run is None:
            return Refused(f"No run {code}.")
        if (
            run.status != "ready"
            or run.pr_number is None
            or run.head_sha is None
            or run.branch is None
        ):
            return Refused(f"{run.code} isn't ready to merge ({run.status}).")
        try:
            secrets = await load_runner_secrets(self.secrets)
        except SecretError:
            return Refused((await self.setup_missing()) or "The runner's secrets can't be read.")
        try:  # fetches the PR head and runs the checks before it merges (D10)
            checked = await self.workspace.merge(
                run.repo, run.pr_number, run.head_sha, secrets, branch=run.branch
            )
        except (MergeRefused, RunnerError, GitError) as e:
            reason = e.message if isinstance(e, GitError) else str(e)
            return Refused(f"{run.code} not merged: {Redactor(secrets.values()).text(reason)}")
        with atomic(self.conn):
            runs.update(self.conn, run.code, status="merged", flagged_paths=checked.flagged)
            audit(self.conn, "run.merged", run=run.code, pr=run.pr_number)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(delete_run_dir, self.cfg.work_dir, run.code)
        await self._remove_workspace(run)
        return Merged(run.code, run.pr_number, checked.flagged)

    # -- the worker CLI's owner side (EM-5's API) --

    @property
    def events(self) -> WorkerEvents:
        """Worker events (``question``, ``note``, ``check``): ``events.subscribe(fn)``."""
        return self.desk.events

    def open_questions(self) -> list[OpenQuestion]:
        """Open ``resonant ask`` questions of active runs, oldest first."""
        return self.desk.open_questions()

    def answer_question(self, code: str, text: str) -> Relayed | Refused:
        """The owner's answer to run ``code``'s open question. A CLI call still waiting gets
        it at once; else the session is resumed with it (``claude --resume``)."""
        try:
            return self.desk.answer_question(code.upper(), text)
        except RelayRefused as e:
            return Refused(str(e))

    def deliver_message(self, code: str, text: str) -> Relayed | Refused:
        """An owner message for a running worker: appended to its next ``resonant`` reply,
        or the prompt of its next turn. Refused once the run's session part is over."""
        run = runs.get(self.conn, code)
        if run is not None and run.active and run.step not in ("prepare", "session"):
            return Refused(f"{run.code} has finished its work; start a new run for more.")
        try:
            return self.desk.deliver_message(code.upper(), text)
        except RelayRefused as e:
            return Refused(str(e))

    def _wake(self, run: Run, key: str) -> None:
        """Something arrived for the run: an event for its task, which requeues it if it is
        waiting (and, if a step is in flight, once that step returns ``Wait``)."""
        if run.task_id is None:
            return
        if run.status == "waiting" and run.wait_reason != "question":
            return  # a usage wait: the news is in the store for its natural wake
        event = Event(source="runner", type="run.inbox", task_id=run.task_id,
                      principal=run.principal, dedupe_key=f"run:{run.code}:{key}")  # fmt: skip
        try:
            self._submit(event)
        except Exception:
            log.exception("couldn't wake a run", extra={"run": run.code})

    def _insert_event(self, event: Event) -> bool:
        """``Loop.submit`` without a loop: insert the event and requeue a waiting task."""
        with transaction(self.conn):
            inserted = task_store.insert_event(self.conn, event)
            if inserted and event.task_id:
                task_store.set_status(self.conn, event.task_id, "queued", expect=("waiting",))
        return inserted

    def _on_note(self, code: str) -> None:
        run = runs.get(self.conn, code)
        if run is not None and run.status == "running":
            self._pulse_open(run)

    def _peer_ok(self, code: str, pid: int | None) -> bool:
        """A CLI caller speaks for run ``code`` only from inside its live session."""
        target = self._live.get(code)
        if pid is None or target is None or target.leader_pid is None:
            return False
        return worker_cli.descends_from(pid, target.leader_pid, target.leader_started_at)

    # -- the loop's entry point --

    async def step(self, task: Task, events: list[Any], ctx: StepContext) -> StepOutcome:
        code = task.checkpoint.get("run")
        run = runs.get(self.conn, code) if isinstance(code, str) else None
        if run is None:
            return Fail("run not found")
        if not run.active:
            return await self._conclude(run)
        if self._blocked:
            return self._defer(task, "starting")
        if self._paused():
            return self._defer(task, "paused")
        if len(self._working) >= self.max_runs:
            return self._defer(task, "busy")  # waits without holding a Claude slot
        self._working.add(run.code)  # nothing awaited since the check
        try:
            return await self._drive(task, run, ctx)
        except asyncio.CancelledError:
            await self._on_cancelled(run.code)
            raise
        finally:
            self._working.discard(run.code)

    def _defer(self, task: Task, why: str) -> Wait:
        # The loop has no "resource" wait reason; usage_reset is the closest, and the run's own
        # status (queued) is what /runs shows.
        log.info("run deferred", extra={"task": task.id, "why": why})
        return Wait(
            "usage_reset", dict(task.checkpoint), self.clock() + timedelta(seconds=self.defer_s)
        )

    async def _drive(self, task: Task, run: Run, ctx: StepContext) -> StepOutcome:
        try:
            secrets = await load_runner_secrets(self.secrets)
        except SecretError as e:
            return await self._fail(run, f"the runner isn't set up ({e})", Redactor())
        w = _Work(task, ctx, secrets, Redactor(secrets.values()))
        try:
            if run.status == "waiting" and run.wait_reason == "question":
                self.desk.time_out_due(run.code)
                if not self.desk.has_deliveries(run.code):
                    until = self.desk.wait_until(run.code)
                    if until is not None:  # nothing new: keep waiting, the row untouched
                        return Wait("human_reply", dict(task.checkpoint), until)
                run = self._end_wait(run)
                self._pulse_open(run)
            elif run.status == "waiting":
                run = self._end_wait(run)
                self._pulse_open(run)
                if run.wait_seconds > MAX_WAIT_S:
                    return await self._fail(run, "usage limit didn't reset within 24h", w.redactor)
            if run.step == "prepare":
                run = await self._prepare(w, run)
            p = self._prepared(run)
            while run.step == "session":  # one -p turn each; answers and messages resume it
                outcome = await self._session(w, run, p)
                if outcome is not None:
                    return outcome
                run = self._must_get(run.code)
            if run.step in ("harvest", "publish") and not run.scratch:
                run = await self._collect(w, run, p)  # "publish" only in an older build's run
        except _Cancelled:
            return await self._conclude(self._must_get(run.code))
        except NoChanges:
            return await self._end(run, "no_changes")
        except (RunnerError, CheckFailed) as e:
            return await self._fail(run, str(e), w.redactor)
        except GitError as e:
            return await self._fail(run, f"git failed: {e.message[-120:]}", w.redactor)
        run = self._must_get(run.code)
        if run.active:  # a step ran out without ending the run: never leave it running
            return await self._fail(run, "the run stopped in an unexpected state", w.redactor)
        return await self._conclude(run)

    # -- steps --

    async def _prepare(self, w: _Work, run: Run) -> Run:
        self._check_stop(run.code)
        if run.scratch:
            p = await self.workspace.prepare_scratch(run.code)
        else:
            p = await self.workspace.prepare(
                run.code, run.repo, run.ask, github_token=w.secrets.github_token
            )
        runs.update(
            self.conn,
            run.code,
            workspace=str(p.workspace),
            checkout=None if p.checkout is None else str(p.checkout),
        )
        params = self._params(p)
        try:
            await _finish_thread(self._prepare_session_dirs, params)
        except (SandboxError, OSError) as e:
            raise RunnerError(f"the sandbox can't be set up: {e}") from None
        runs.update(
            self.conn,
            run.code,
            status="running",
            step="session",
            branch=p.branch or None,
            base_branch=p.base_branch or None,
            base_sha=p.base_sha or None,
            started_at=self.clock(),
        )
        w.ctx.save(claude_session_id=run.session_id, worktree_path=str(p.workspace))
        self._pulse_open(self._must_get(run.code))
        self._check_stop(run.code)
        return self._must_get(run.code)

    def _prepare_session_dirs(self, params: SandboxParams) -> None:
        """Daemon-side, before the first session: the run-local ``bin/`` and the caches."""
        prepare_dirs(params)
        tools: dict[str, os.PathLike[str] | str] = {}
        for name in TOOL_NAMES:
            if name == "claude" and self._claude_cmd is not None:
                continue
            if (found := resolve_tool(name)) is not None:
                tools[name] = found
        if (git := resolve_tool("git")) is not None:
            tools["git"] = git
        # the session drives its own PR with gh (the configured binary, else the PATH's)
        gh = self.cfg.gh_path if self.cfg.gh_path.exists() else resolve_tool("gh")
        if gh is not None:
            tools["gh"] = gh
        link_tools(params, tools)
        # the worker CLI: written now, before any session can touch bin/
        if params.ctl_dir is not None:
            ctl = worker_cli.make_ctl_dir(self.cfg.work_dir, params.run_dir.name)
            worker_cli.write_shim(params.run_dir / "bin", ctl, self.ask_block_s)

    def _prepared(self, run: Run) -> Prepared:
        run_dir = self.cfg.work_dir / run.code
        if run.scratch:
            workspace = run.workspace or self.workspace.scratch_path(run.code)
            return Prepared(run.code, "", run_dir, Path(workspace))
        assert run.branch and run.base_branch and run.base_sha
        if run.workspace is None:  # prepared by a build before worktrees (EM-2)
            raise RunnerError("it was started by an older version of Resonant; start it again")
        checkout = None if run.checkout is None else Path(run.checkout)
        return Prepared(run.code, run.repo, run_dir, Path(run.workspace), checkout, run.branch,
                        run.base_branch, run.base_sha)  # fmt: skip

    def _params(self, p: Prepared) -> SandboxParams:
        # the session works in its worktree, and git writes the checkout's object store and
        # refs; a scratch dir is all a run with no repo has
        writes = [p.workspace]
        if p.checkout is not None:
            writes.append(p.checkout / ".git")
        return SandboxParams(
            run_dir=p.run_dir,
            work_dir=self.cfg.work_dir,
            cache_dir=self.cfg.cache_dir,
            secrets_dir=self.cfg.secrets_dir,
            read_paths=self._read_paths,
            deny_exec=default_deny_exec(),
            deny_read=tuple(self.cfg.deny_read),
            write_paths=tuple(writes),
            ctl_root=self.ctl_root,
            ctl_dir=self.ctl_root / p.code,
        )

    def _cached_self_check(self, params: SandboxParams) -> tuple[bool, str]:
        return cached_self_check(params, self.state_dir / CHECK_CACHE)

    def _env(
        self, params: SandboxParams, marker: str, secrets: RunnerSecrets | None, p: Prepared
    ) -> dict[str, str]:
        """The sandbox environment: from scratch, the one allowed secret only when asked. The
        run's GitHub token is not in it: ``gh`` finds it by ``GH_CONFIG_DIR`` (a path) and git
        through the credential helper named in ``GIT_CONFIG_*`` (a path; nothing is written
        to the checkout's config). A run with no repo has neither."""
        bins = [p for p in params.read_paths if p.name == "bin"]
        env = scratch_env(
            params,
            path_dirs=bins,
            claude_token=None if secrets is None else secrets.claude_token,
            anthropic_key=None if secrets is None else secrets.anthropic_key,
        )
        env[MARKER_ENV] = marker  # the run's session id: unique, never reused
        if p.scratch:
            env.update(session_git_env(None))
        else:
            creds = Path(env["HOME"]).parent / CREDS_DIR
            env["GH_CONFIG_DIR"] = str(creds / "gh")
            env.update(session_git_env(creds / "credential-helper", p.exclude_file))
        # `resonant ask` waits up to ask_block_s: the Bash tool must not cut it off first
        bash_ms = str(int((self.ask_block_s + 120) * 1000))
        env["BASH_DEFAULT_TIMEOUT_MS"] = bash_ms
        env["BASH_MAX_TIMEOUT_MS"] = bash_ms
        env["GH_PROMPT_DISABLED"] = "1"
        env["GH_NO_UPDATE_NOTIFIER"] = "1"
        return env

    def _argv(
        self, run: Run, p: Prepared, params: SandboxParams, *, api_billing: bool
    ) -> list[str]:
        head = list(self._claude_cmd or [str(params.run_dir / "bin" / "claude")])
        argv = [
            *head,
            "-p",
            "--resume" if run.sessions else "--session-id",
            run.session_id,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
            "--strict-mcp-config",
            "--setting-sources",
            "project",
            "--settings",
            json.dumps({"sandbox": {"enabled": False}}),
            "--append-system-prompt",
            (
                SCRATCH_PROMPT
                if p.scratch
                else RUNNER_PROMPT.format(
                    repo=self._slug(run.repo),
                    branch=p.branch,
                    base=p.base_branch,
                    attempts=MAX_FIX_ATTEMPTS,
                )
            )
            + WORKER_PROMPT,
            "--max-turns",
            str(self.max_turns),
        ]
        if api_billing and run.max_budget_usd is not None:  # a subscription has no dollar budget
            argv += ["--max-budget-usd", f"{run.max_budget_usd:g}"]
        return argv

    async def _session(self, w: _Work, run: Run, p: Prepared) -> StepOutcome | None:
        """Run Claude until it exits, a limit is hit or the run is stopped. None means it
        finished cleanly and the run moves on to harvest."""
        if run.attempts >= MAX_ATTEMPTS:
            return await self._fail(
                run, f"the daemon crashed {run.attempts} times during the session "
                f"(work kept: /runs {run.code})", w.redactor)  # fmt: skip
        if run.session_seconds >= run.max_seconds:
            return await self._timed_out(run, w)
        params = self._params(p)
        try:
            ok, detail = await asyncio.to_thread(self._self_check, params)
        except SandboxError as e:
            ok, detail = False, str(e)
        if not ok:
            log.error("sandbox self-check failed", extra={"run": run.code, "detail": detail})
            with atomic(self.conn):
                audit(self.conn, "run.sandbox_failed", run=run.code, detail=detail[:300])
            return await self._fail(
                run, "the sandbox check failed, see resonant sandbox check", w.redactor
            )
        self._check_stop(run.code)
        if not p.scratch:  # a run with no repo gets no GitHub token
            await self._provision(w, run, p)
        try:
            return await self._run_session(w, run, p, params)
        finally:
            # However the session ended (or the step was cancelled): the token dies with it.
            await self._deprovision(run.code)

    async def _run_session(
        self, w: _Work, run: Run, p: Prepared, params: SandboxParams
    ) -> StepOutcome | None:
        argv = list(
            self._wrap(
                self._argv(run, p, params, api_billing=bool(w.secrets.anthropic_key)), params
            )
        )
        env = self._env(params, run.session_id, w.secrets, p)
        prompt = self._turn_prompt(run).encode()
        server = await self._open_ctl(run, w)
        try:
            return await self._spawn_session(w, run, p, argv, env, prompt, server)
        finally:
            await self._close_ctl(run.code, server)

    def _turn_prompt(self, run: Run) -> str:
        """The prompt of this turn: the ask for a new session; for a resumed one, the
        answers and owner messages it hasn't seen (saved on the run until the turn ends, so
        a crash resends them), else the plain resume prompt."""
        with atomic(self.conn):  # taken and saved together: a crash loses neither
            fresh = self.desk.resume_text(run.code)
            saved = run.resume_prompt
            if fresh is not None:
                saved = f"{saved}\n\n{fresh}" if saved else fresh
                runs.update(self.conn, run.code, resume_prompt=saved)
        if not run.sessions:
            return run.ask + (f"\n\n{saved}" if saved else "")
        if fresh is not None:
            return f"{saved}\n\nCarry on with the task."
        prompt = RESUME_PROMPT
        if saved:
            prompt += f"\n\nWhat you were told last turn:\n{saved}"
        for q in questions_store.open_for_run(self.conn, run.code, "ask"):
            prompt += (
                f'\n\nYou asked the owner: "{q.question}". There is no answer yet; if you still '
                "need it, finish what you can and end your turn: you'll be resumed with it."
            )
        return prompt

    async def _open_ctl(self, run: Run, w: _Work) -> WorkerServer | None:
        """The run's socket for this session, with a fresh token. None (the CLI says it can't
        reach Resonant) when it can't be opened: the session still runs."""
        self._run_redactors[run.code] = w.redactor
        try:
            ctl = await asyncio.to_thread(worker_cli.make_ctl_dir, self.cfg.work_dir, run.code)
            token = await asyncio.to_thread(worker_cli.write_token, ctl)
            server = WorkerServer(
                ctl, token,
                lambda req, gone: self.desk.handle(run.code, req, gone),
                lambda pid: self._peer_ok(run.code, pid),
            )  # fmt: skip
            await server.start()
        except OSError as e:
            log.warning("worker CLI socket not opened", extra={"run": run.code, "reason": str(e)})
            return None
        return server

    async def _close_ctl(self, code: str, server: WorkerServer | None) -> None:
        if server is not None:
            await server.close()
        self._run_redactors.pop(code, None)
        with contextlib.suppress(OSError):
            os.unlink(self.ctl_root / code / worker_cli.TOKEN)

    async def _spawn_session(
        self,
        w: _Work,
        run: Run,
        p: Prepared,
        argv: list[str],
        env: dict[str, str],
        prompt: bytes,
        server: WorkerServer | None,
    ) -> StepOutcome | None:
        writer = TranscriptWriter(self.transcript_dir / f"{run.code}.jsonl", w.redactor)
        state = StreamState()
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=p.workspace,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            writer.close()
            return await self._fail(run, f"Claude couldn't be started ({e.strerror})", w.redactor)
        # Saved at once and before anything awaits: a crash from here on can find the tree.
        try:
            created = psutil.Process(proc.pid).create_time()
        except psutil.Error:
            created = time.time()
        target = Target(proc.pid, proc.pid, created, run_dir=p.run_dir, marker=run.session_id)
        self._live[run.code] = target
        runs.set_process(self.conn, run.code, proc.pid, proc.pid, created)
        stop = await self._supervise(w, run, p, proc, target, writer, state, prompt, server)
        used = run.session_seconds + (time.monotonic() - started)
        result = state.result
        usage: dict[str, Any] = {}
        if result is not None and isinstance(result.get("usage"), dict):
            usage = result["usage"]
        cost = result.get("total_cost_usd") if result is not None else None
        runs.update(
            self.conn,
            run.code,
            session_seconds=used,
            exit_code=proc.returncode,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            tokens_in=_int(usage.get("input_tokens")),
            tokens_out=_int(usage.get("output_tokens")),
        )
        run = self._must_get(run.code)
        if run.pgid is not None:  # the tree wouldn't die: never harvest under a live session
            return await self._fail(run, "the session's processes wouldn't stop", w.redactor)
        if stop == "cancelled":
            raise _Cancelled
        if stop == "timeout":
            return await self._timed_out(run, w)
        if stop == "disk":
            over = _size(self.disk_limit_bytes)
            return await self._fail(
                run, f"used over {over} of disk (work kept: /runs {run.code})", w.redactor
            )
        if stop == "cache":
            over = _size(self.cache_limit_bytes)
            return await self._fail(
                run, f"the runner cache grew over {over} (work kept: /runs {run.code})", w.redactor
            )
        if result is not None and (hit := usage_limit(result)) is not None:
            return self._wait_for_limit(w, run, hit)
        return await self._after_exit(w, run, proc.returncode, result)

    async def _supervise(
        self,
        w: _Work,
        run: Run,
        p: Prepared,
        proc: asyncio.subprocess.Process,
        target: Target,
        writer: TranscriptWriter,
        state: StreamState,
        prompt: bytes,
        server: WorkerServer | None = None,
    ) -> str | None:
        """Feed the prompt, pump the output into the transcript and watch the clock, the run
        row, the kill switch and the disk. Always ends with the whole process tree gone."""
        assert proc.stdin and proc.stdout and proc.stderr
        stop: str | None = None

        async def feed() -> None:
            assert proc.stdin
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(prompt)
                await proc.stdin.drain()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.close()

        async def pump_out() -> None:
            assert proc.stdout
            counted = False
            async for raw, truncated in read_lines(proc.stdout):
                writer.write(raw, truncated)
                if not truncated:
                    state.feed(raw)
                if state.session_id and not counted:
                    # Only a session Claude has written can be resumed.
                    counted = True
                    runs.mark_session(self.conn, run.code)

        async def pump_err() -> None:
            assert proc.stderr
            async for raw, truncated in read_lines(proc.stderr, 8192):
                writer.note("stderr", text=raw.decode(errors="replace"), truncated=truncated)

        async def disk_exceeded(_size: int) -> None:
            nonlocal stop
            stop = stop or "disk"

        async def cache_exceeded(_size: int) -> None:
            nonlocal stop
            stop = stop or "cache"

        pumps = [asyncio.create_task(c) for c in (feed(), pump_out(), pump_err())]
        disk = asyncio.create_task(
            watch_disk(
                p.run_dir, self.disk_limit_bytes, disk_exceeded, interval_s=self.disk_interval_s
            )
        )
        work_disk = asyncio.create_task(  # the worktree or scratch dir, on the same limit
            watch_disk(
                p.workspace, self.disk_limit_bytes, disk_exceeded, interval_s=self.disk_interval_s
            )
        )
        cache = asyncio.create_task(
            watch_disk(
                self.cfg.cache_dir,
                self.cache_limit_bytes,
                cache_exceeded,
                interval_s=self.disk_interval_s,
            )
        )
        exited = asyncio.create_task(proc.wait())
        began = time.monotonic()
        last_save = began
        bucket = _bucket(run.session_seconds)
        refresh_due = began + self.refresh_s
        refreshing: asyncio.Task[bool] | None = None
        try:
            while True:
                done, _ = await asyncio.wait({exited}, timeout=self.poll_s)
                if done:
                    break
                reaper.sample(target)
                if refreshing is not None and refreshing.done():
                    failed = (
                        refreshing.cancelled()
                        or refreshing.exception() is not None
                        or refreshing.result() is not True
                    )
                    refreshing = None
                    if failed:
                        # try again soon: the old token has ten minutes left
                        refresh_due = time.monotonic() + min(self.refresh_s, 60.0)
                if refreshing is None and time.monotonic() >= refresh_due:
                    refresh_due = time.monotonic() + self.refresh_s
                    refreshing = asyncio.create_task(self._refresh_token(w, run, p, writer))
                used = run.session_seconds + (time.monotonic() - began)
                if stop is None:
                    if self._stopped(run.code):
                        stop = "cancelled"
                    elif used >= run.max_seconds:
                        stop = "timeout"
                if stop is not None:
                    break
                if time.monotonic() - last_save >= SAVE_EVERY_S:
                    last_save = time.monotonic()
                    runs.update(self.conn, run.code, session_seconds=used)
                if _bucket(used) != bucket:
                    bucket = _bucket(used)
                    self._pulse_open(self._must_get(run.code), bucket)
        finally:
            # Also on cancellation: nothing of the session may outlive the step. The socket
            # goes first, so no CLI call still in flight texts anyone after a stop.
            if server is not None:
                await server.close()
            disk.cancel()
            work_disk.cancel()
            cache.cancel()
            if refreshing is not None:
                refreshing.cancel()
                await asyncio.wait([refreshing])
            hurry = stop == "cancelled" or sys.exc_info()[0] is not None
            await reaper.reap(target, grace_s=self.cancel_grace_s if hurry else self.kill_grace_s)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
            exited.cancel()
            done_pumps, pending = await asyncio.wait(pumps, timeout=5)
            for t in pending:
                t.cancel()
            await asyncio.wait([*done_pumps, *pending, disk, work_disk, cache, exited])
            self._live.pop(run.code, None)
            writer.note("exit", code=proc.returncode, stop=stop)
            writer.close()
            # The time used counts even when the step is being cancelled, and the saved process
            # is forgotten only once it is really gone (else the next start reaps it as a crash).
            used = run.session_seconds + (time.monotonic() - began)
            runs.update(self.conn, run.code, session_seconds=used)
            if not await asyncio.to_thread(reaper.alive, target):
                runs.clear_process(self.conn, run.code)
        return stop

    async def _after_exit(
        self, w: _Work, run: Run, returncode: int | None, result: dict[str, Any] | None
    ) -> StepOutcome | None:
        """Classify a session that ended by itself."""
        keep = f"(work kept: /runs {run.code})"
        if result is None:
            return await self._fail(
                run, f"Claude ended without a result (exit {returncode}) {keep}", w.redactor
            )
        subtype = str(result.get("subtype") or "")
        if subtype == "error_max_turns":
            return await self._fail(run, f"Claude hit the turn limit {keep}", w.redactor)
        if subtype == "error_max_budget_usd":
            budget = f"${run.max_budget_usd:g}" if run.max_budget_usd else "dollar"
            return await self._fail(run, f"Claude hit the {budget} budget {keep}", w.redactor)
        if result.get("is_error") is True or returncode != 0:
            return await self._fail(run, f"Claude failed (exit {returncode}) {keep}", w.redactor)
        text = result.get("result")
        summary = _summary(text, w.redactor) if isinstance(text, str) else None
        # The turn ended. From here to the step change below nothing is awaited, so an
        # answer or message can't slip in between the look and the move (they also wake the
        # task, which a returned Wait honours).
        runs.update(self.conn, run.code, resume_prompt=None)
        self.desk.time_out_due(run.code)
        if self.desk.has_deliveries(run.code):  # answers or messages: another turn
            runs.update(self.conn, run.code, summary=summary)
            return None
        if (until := self.desk.wait_until(run.code)) is not None:
            return self._wait_for_answer(w, run, summary, until)
        if run.scratch:  # nothing to collect: the summary is the answer
            if not runs.finish(self.conn, run.code, "done", step="reply", summary=summary):
                raise _Cancelled
            with atomic(self.conn):
                audit(self.conn, "run.done", run=run.code)
            return None
        runs.update(self.conn, run.code, summary=summary, step="harvest")  # then ``_collect``
        return None

    async def _timed_out(self, run: Run, w: _Work) -> StepOutcome:
        return await self._fail(
            run,
            f"timed out at {fmt_duration(run.max_seconds)} (work kept: /runs {run.code})",
            w.redactor,
        )

    def _wait_for_limit(self, w: _Work, run: Run, hit: LimitHit) -> StepOutcome:
        now = self.clock()
        wake = wake_time(hit, now)
        runs.update(self.conn, run.code, status="waiting", waiting_since=now,
                    wait_reason="usage_reset")  # fmt: skip
        self._pulse_open(self._must_get(run.code), "waiting")
        return Wait("usage_reset", dict(w.task.checkpoint), wake)

    def _wait_for_answer(
        self, w: _Work, run: Run, summary: str | None, until: datetime
    ) -> StepOutcome:
        """The turn ended with a question open: the run waits (its slot freed, the time not
        counted against max_minutes) until the answer, a message, or the ask timeout."""
        runs.update(self.conn, run.code, status="waiting", wait_reason="question",
                    waiting_since=self.clock(), summary=summary)  # fmt: skip
        self._pulse_open(self._must_get(run.code), "question")
        with atomic(self.conn):
            audit(self.conn, "run.waiting_question", run=run.code)
        return Wait("human_reply", dict(w.task.checkpoint), until)

    def _end_wait(self, run: Run) -> Run:
        """A waiting run is woken and running again. A usage wait is added up (24 h of it in
        all fails the run); a question wait isn't."""
        waited = 0.0
        if run.waiting_since is not None and run.wait_reason != "question":
            waited = max((self.clock() - run.waiting_since).total_seconds(), 0.0)
        runs.update(
            self.conn, run.code, status="running", waiting_since=None, wait_reason=None,
            wait_seconds=run.wait_seconds + waited,
        )  # fmt: skip
        return self._must_get(run.code)

    async def _collect(self, w: _Work, run: Run, p: Prepared) -> Run:
        """After the session: what it left on GitHub. The PR is found by its head branch (the
        session's own words are not trusted for it), then checked the way a merge will check
        it so the reply and the merge prompt can say what is in it. A PR the checks refuse
        still makes the run ``ready``, with the reason, since the PR exists and the owner may
        want to look at it; ``merge_run`` refuses it again."""
        self._check_stop(run.code)
        await self._check_base_untouched(w, run, p)
        pr = await self.workspace.find_pr(run.repo, p.branch, w.secrets)
        if pr is None:
            if await self.workspace.pushed_commits(p, w.secrets) == 0:
                raise NoChanges("the session pushed nothing")
            raise RunnerError(
                f"the session pushed {p.branch} but opened no PR (work kept: /runs {run.code})"
            )
        self._check_stop(run.code)
        if pr.state == "MERGED":  # the session merged it itself: no check ever ran on it
            with contextlib.suppress(ChannelError):
                await self.channel.send(
                    run.principal,
                    f"⚠ {run.code}: PR #{pr.number} was already merged, not by Resonant "
                    f"(the session did it). Look at {self._slug(run.repo)}.",
                    dedupe_key=f"run:{run.code}:pr-merged",
                )
            raise RunnerError(f"PR #{pr.number} was merged by the session, not by Resonant")
        if pr.state != "OPEN":
            raise RunnerError(f"PR #{pr.number} is {pr.state.lower()}, not open")
        if pr.base != p.base_branch:
            raise RunnerError(f"PR #{pr.number} targets {pr.base}, not {p.base_branch}")
        fields: dict[str, Any] = {"pr_number": pr.number, "pr_url": pr.url, "head_sha": pr.head_sha}
        if pr.files is not None:
            fields["files_changed"] = pr.files
        refusal: str | None = None
        try:
            checked = await self.workspace.check_pr(
                run.repo,
                pr.number,
                p.branch,
                w.secrets,
                expect_head=pr.head_sha,  # gitleaks:allow
            )
        except (CheckFailed, MergeRefused) as e:
            refusal = _clip(w.redactor.text(str(e)), REASON_MAX)
        except GitError as e:
            why = f"checking the PR failed: {e.message[-120:]}"
            refusal = _clip(w.redactor.text(why), REASON_MAX)
        else:
            fields.update(files_changed=checked.files, flagged_paths=checked.flagged)
        if not runs.finish(self.conn, run.code, "ready", step="reply", failure=refusal, **fields):
            runs.update(self.conn, run.code, pr_number=pr.number, pr_url=pr.url)
            raise _Cancelled  # cancelled just now: the reply says its PR is open
        with atomic(self.conn):
            audit(self.conn, "run.ready", run=run.code, pr=pr.number)
        return self._must_get(run.code)

    async def _check_base_untouched(self, w: _Work, run: Run, p: Prepared) -> None:
        """A push (or PR merge) straight onto the base branch skips every check a merge runs
        (the base is already live, and the checks only look at what the PR adds). Protection
        should stop it, but a ruleset the app can bypass would not, so the activity feed is
        always asked whether the app's bot put anything on the base during the run. If it did,
        the run fails, the owner is told at once and it is never merged. If the feed can't be
        read the run fails too: it can't be shown clean."""
        if self.app is None:
            return
        try:
            bot = await self.app.bot_login()
        except GitHubAppError as e:
            raise RunnerError(f"couldn't check {p.base_branch} for direct pushes: {e}") from None
        since = (run.started_at or run.created_at) - timedelta(minutes=1)
        shas = await self.workspace.base_pushes(run.repo, w.secrets, actor=bot, since=since)
        if not shas:
            return
        short = ", ".join(sha[:7] for sha in shas[:5])
        with atomic(self.conn):
            audit(self.conn, "run.base_push", run=run.code, base=p.base_branch, shas=short)
        with contextlib.suppress(ChannelError):
            await self.channel.send(
                run.principal,
                f"⚠ {run.code} pushed directly to {p.base_branch}: {short}. "
                f"Look at {self._slug(run.repo)} now; this run will not be merged.",
                dedupe_key=f"run:{run.code}:base-push",
            )
        raise RunnerError(f"pushed directly to {p.base_branch} ({short}); not merged")

    # -- the session's GitHub token --

    def _slug(self, repo_name: str) -> str:
        repo = repo_store.find(self.conn, repo_name)
        if repo is None:
            raise RunnerError(f"{repo_name} isn't a registered repo")
        return repo.slug

    def _redactor(self, w: _Work, code: str) -> Redactor:
        return Redactor(
            [*w.secrets.values(), *self._tokens.get(code, ()), *self._spent.get(code, ())]
        )

    async def _mint(self, run: Run) -> str:
        """A new token for the run's repo, remembered so it is revoked at the end."""
        repo = repo_store.find(self.conn, run.repo)
        if repo is None:
            raise RunnerError(f"{run.repo} isn't a registered repo")
        if self.app is None:
            raise RunnerError("the runner isn't set up: runner.github_app isn't in the config")
        try:
            minted = await self.app.mint(
                repo.slug.partition("/")[2], workflows=repo.allow_github_changes
            )
        except GitHubAppError as e:
            raise RunnerError(str(e)) from None
        self._tokens.setdefault(run.code, []).append(minted.token)
        self._expiry[minted.token] = minted.expires() or self.clock() + timedelta(hours=1)
        return minted.token

    async def _provision(self, w: _Work, run: Run, p: Prepared) -> None:
        """Mint the session's token and put it in the run's ``creds`` dir (git's helper and
        gh's hosts.yml read it there). A token an earlier, crashed session left is revoked
        first."""
        await self._deprovision(run.code, remove_dir=False)
        token = await self._mint(run)
        w.redactor = self._redactor(w, run.code)
        await _finish_thread(write_session_credentials, p.run_dir, token)

    async def _refresh_token(
        self, w: _Work, run: Run, p: Prepared, writer: TranscriptWriter
    ) -> bool:
        """A fresh token over the same files, for a session that has run for most of an hour.
        The old one is revoked at once, so only the newest is ever on disk (a crash leaves
        one token to find, not two). A gh or git call already running with the old token
        fails once; the prompt tells the session to run it again."""
        try:
            token = await self._mint(run)
            w.redactor = writer.redactor = self._redactor(w, run.code)
            await _finish_thread(write_session_credentials, p.run_dir, token)
            old = [t for t in self._tokens.get(run.code, []) if t != token]
            self._tokens[run.code] = [token]
            self._spent.setdefault(run.code, []).extend(old)
            for t in old:
                await self._revoke(run.code, t)
        except RunnerError as e:
            log.warning("run token refresh failed", extra={"run": run.code, "reason": str(e)})
            with contextlib.suppress(Exception), atomic(self.conn):
                audit(self.conn, "run.token_refresh_failed", run=run.code, reason=str(e)[:200])
            return False
        return True

    async def _deprovision(self, code: str, *, remove_dir: bool = True) -> None:
        """Revoke every token minted for the run (and the one on disk, which a crash may have
        left) and remove the ``creds`` dir. Best effort and safe to repeat; a token that
        can't be revoked is noted for the reply and expires within the hour anyway."""
        run_dir = self.cfg.work_dir / code
        tokens = self._tokens.pop(code, [])
        on_disk = await asyncio.to_thread(read_session_token, run_dir)
        if on_disk is not None and on_disk not in tokens:
            tokens.append(on_disk)
        for token in tokens:
            await self._revoke(code, token)
        if remove_dir:
            self._spent.pop(code, None)
            with contextlib.suppress(Exception):
                await _finish_thread(rmtree_nofollow, run_dir / CREDS_DIR)

    async def _revoke(self, code: str, token: str) -> None:
        """Revoke one token (the app tries three times). On failure remember when it expires,
        for the reply: ``R3 done … token not revoked, expires 14:05``."""
        expiry = self._expiry.pop(token, None) or self.clock() + timedelta(hours=1)
        if self.app is None:
            return
        try:
            await asyncio.wait_for(self.app.revoke(token), REVOKE_TIMEOUT_S)
        except (GitHubAppError, TimeoutError) as e:
            log.warning("couldn't revoke a run's token", extra={"run": code, "reason": str(e)})
            self._unrevoked[code] = max(expiry, self._unrevoked.get(code, expiry))
            with contextlib.suppress(Exception), atomic(self.conn):
                audit(self.conn, "run.token_revoke_failed", run=code)

    # -- ending a run --

    async def _fail(self, run: Run, reason: str, red: Redactor) -> StepOutcome:
        reason = _clip(red.text(reason), REASON_MAX)
        if not runs.finish(self.conn, run.code, "failed", failure=reason, step="reply"):
            return await self._conclude(self._must_get(run.code))  # cancelled first
        with atomic(self.conn):
            audit(self.conn, "run.failed", run=run.code, reason=reason)
        return await self._conclude(self._must_get(run.code))

    async def _end(self, run: Run, status: runs.Status) -> StepOutcome:
        runs.finish(self.conn, run.code, status, step="reply")
        return await self._conclude(self._must_get(run.code))

    async def _conclude(self, run: Run) -> StepOutcome:
        """Everything after a run's row has its final status: clear the pulse item, send the
        reply (once), and tell the loop how the task ended."""
        if run.status == "cancelled":
            await self._finish_cancelled(run.code, send=False)
        else:
            self._pulse_clear(run)
            await self._deprovision(run.code)
            await self._end_cli(run.code)
        try:
            await self._send_reply(run)
        except ChannelError as e:
            return Fail(f"reply failed: {e}", retryable=True)
        if run.status == "failed":
            return Fail(run.failure or "failed")
        return Done({"run": run.code, "status": run.status})

    async def _ci_word(self, run: Run) -> str | None:
        """The PR's CI as a word for the reply; None when it can't be read."""
        if run.status != "ready" or run.pr_number is None:
            return None
        try:
            secrets = await load_runner_secrets(self.secrets)
            state = await self.workspace.ci_state(run.repo, run.pr_number, secrets)
        except Exception:  # a reply must go out whatever gh or the Keychain say
            log.warning("CI state unavailable for the reply", extra={"run": run.code})
            return None
        return None if state == "error" else state

    async def _send_reply(self, run: Run) -> None:
        repo = repo_store.find(self.conn, run.repo)
        no_ci = repo is not None and repo.mode == "push" and repo.ci is False
        text = reply_text(run, await self._ci_word(run), no_ci=no_ci)
        if text is not None and (expires := self._unrevoked.pop(run.code, None)) is not None:
            at = expires.astimezone(self.tz).strftime("%H:%M")
            text += f"\n⚠ the run's GitHub token wasn't revoked; it expires at {at}."
        if text is None:
            return
        try:
            await self.channel.send(run.principal, text, dedupe_key=f"run:{run.code}:{run.status}")
        except ChannelRefused:
            log.warning("run reply refused", extra={"run": run.code})

    async def _finish_cancelled(self, code: str, *, send: bool = True) -> None:
        """What a cancelled run still needs once its processes are gone: its dir deleted, its
        pulse item cleared, and (once) the reply. Safe to repeat."""
        async with self._cleanup_locks.setdefault(code, asyncio.Lock()):
            run = self._must_get(code)
            # The live session if there is one, and anything else of the run (a harvest
            # still going) by marker and cwd: nothing may outlive the dir deleted below.
            target = self._live.get(code) or self._target(run)
            await reaper.reap(target, grace_s=self.cancel_grace_s)
            await self._deprovision(code)
            await self._end_cli(code)
            with contextlib.suppress(Exception):
                # two deletes of one tree would trip over each other, hence the lock
                await _finish_thread(delete_run_dir, self.cfg.work_dir, code)
            await self._remove_workspace(run)
            self._pulse_clear(run)
            if send:
                with contextlib.suppress(ChannelError):
                    await self._send_reply(run)

    async def _end_cli(self, code: str) -> None:
        """The run ended: its open questions close and its control dir goes."""
        self.desk.close_run(code)
        with contextlib.suppress(OSError):
            await asyncio.to_thread(worker_cli.remove_ctl, self.cfg.work_dir, code)

    async def _on_cancelled(self, code: str) -> None:
        """The step was cancelled from outside: by ``/cancel`` or ``/kill`` (the run row says
        so), by a shutdown or by the stuck-step limit (it doesn't: the run stays as it is and
        resumes later)."""
        run = runs.get(self.conn, code)
        if run is not None and run.status == "cancelled":
            await self._finish_cancelled(code)

    # -- cancel, kill switch --

    def _cancel(self, run: Run, reason: str, *, task: bool = True) -> bool:
        """Mark a run cancelled and cancel its task. The step (if any) sees it and cleans up.
        ``task=False`` when the caller is that step."""
        with atomic(self.conn):
            if not runs.cancel(self.conn, run.code, reason):
                return False
            audit(self.conn, "run.cancelled", run=run.code, reason=reason)
        if task and run.task_id is not None:
            if self._cancel_task is not None:
                self._cancel_task(run.task_id)
            else:
                with atomic(self.conn):
                    task_store.set_status(
                        self.conn, run.task_id, "cancelled",
                        expect=("queued", "running", "waiting"),
                    )  # fmt: skip
        return True

    def _on_pause(self, conn: sqlite3.Connection, paused: bool) -> None:
        """The kill switch hook (``/kill``): every active run is cancelled, and the sessions
        of runs with a step in flight are killed at once."""
        if not paused or conn is not self.conn:
            return
        active = runs.active(self.conn)
        ended = [r for r in active if self._cancel(r, KILL_REASON)]
        self.last_killed = len(ended)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # not on the loop (the CLI's kill is another process): polling handles it
        for run in ended:
            self._spawn(self._finish_cancelled(run.code))

    def _paused(self) -> bool:
        try:
            return killswitch.is_paused(self.conn)
        except killswitch.PausedStateUnknownError:
            return True  # unknown counts as paused

    def _stopped(self, code: str) -> bool:
        """The run was cancelled, or Resonant is paused (which cancels the run)."""
        run = runs.get(self.conn, code)
        if run is None or run.status == "cancelled":
            return True
        if self._paused():
            self._cancel(run, KILL_REASON, task=False)
            return True
        return False

    def _check_stop(self, code: str) -> None:
        if self._stopped(code):
            raise _Cancelled

    # -- housekeeping --

    async def _reconcile(self) -> None:
        """End runs whose task is already over (a task fails by itself after too many
        crashes) or was never created."""
        ended: list[Run] = []
        for run, task_status, error in runs.orphans(self.conn):
            if task_status == "cancelled":
                if runs.cancel(self.conn, run.code, "cancelled"):
                    ended.append(run)
            elif runs.finish(self.conn, run.code, "failed", step="reply",
                             failure=error or "the task ended unexpectedly"):  # fmt: skip
                ended.append(run)
        cutoff = self.clock() - timedelta(minutes=2)
        for run in runs.active(self.conn):
            lost = run.task_id is None and run.created_at < cutoff
            if lost and runs.finish(
                self.conn, run.code, "failed", failure="lost before it started"
            ):
                ended.append(run)
        for run in ended:
            self._pulse_clear(run)
            if self._must_get(run.code).status == "cancelled":
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(delete_run_dir, self.cfg.work_dir, run.code)
                await self._remove_workspace(self._must_get(run.code))

    async def _remove_workspace(self, run: Run) -> None:
        """Remove the run's worktree (and its local branch) or scratch dir; never the checkout.
        Best effort: a failure is logged and the daily job tries again."""
        workspace, checkout = run.workspace, run.checkout
        if workspace is None:  # not recorded yet (or an older row): where prepare puts it
            if run.scratch:
                workspace = str(self.workspace.scratch_path(run.code))
            else:
                workspace = str(self.workspace.worktree_path(run.repo, run.code))
                if (repo := repo_store.find(self.conn, run.repo)) is not None:
                    checkout = str(self.workspace.checkout_path(repo))
        try:
            await self.workspace.remove_workspace(
                run.code, workspace=workspace, checkout=checkout, branch=run.branch,
                expect=run.head_sha or run.base_sha,
            )  # fmt: skip
        except Exception as e:
            log.warning("run workspace not removed", extra={"run": run.code, "reason": str(e)})
            return
        runs.update(self.conn, run.code, workspace=None, checkout=None)  # nothing left to do

    def _insert_task(self, spec: NewTask) -> str:
        with transaction(self.conn):
            return task_store.insert_task(self.conn, spec)

    def _must_get(self, code: str) -> Run:
        run = runs.get(self.conn, code)
        assert run is not None
        return run

    # -- pulse --

    def _pulse_open(self, run: Run, bucket: str | None = None) -> None:
        """The ``normal`` pulse item while a run goes: mentioned by the next hourly pulse."""
        started = run.started_at or self.clock()
        at = started.astimezone(self.tz).strftime("%H:%M")
        labels = {"waiting": "waiting for usage reset", "question": "waiting for your answer"}
        what = labels.get(bucket or "", "still running")
        title = f"{run.code} {what} · {run.repo or 'no repo'} · started {at}"
        if run.last_note and bucket != "question":
            title += f" · {run.last_note}"
        state = {"bucket": bucket or _bucket(run.session_seconds)}
        now = self.clock()
        with atomic(self.conn):
            pulse_store.upsert(self.conn, PULSE_SOURCE, _pulse_key(run.code), title, "normal",
                               state, False, PULSE_TTL_S, now)  # fmt: skip

    def _pulse_clear(self, run: Run) -> None:
        """Clear the item silently: the owner gets the direct reply, not a pulse mention."""
        key, now = _pulse_key(run.code), self.clock()
        with atomic(self.conn):
            if pulse_store.get(self.conn, PULSE_SOURCE, key) is None:
                return
            _change, item = pulse_store.upsert(
                self.conn, PULSE_SOURCE, key, f"{run.code} finished · {run.repo or 'no repo'}",
                "normal",
                {"bucket": "done"}, True, PULSE_TTL_S, now,
            )  # fmt: skip
            pulse_store.mark_told(self.conn, [item], now)


# --- helpers -------------------------------------------------------------------------------


async def _finish_thread[**P, T](fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """``to_thread`` that a cancellation can't leave running behind the caller: the thread is
    waited for before the cancellation goes on (a cancel handler that deletes the run dir
    must not race a thread that is still creating it)."""
    fut = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        await asyncio.wait([fut])
        raise


def _pulse_key(code: str) -> str:
    return f"run/{code.lower()}"


def _bucket(seconds: float) -> str:
    return "<30m" if seconds < 1800 else "30m+"


def _clip(text: str, limit: int) -> str:
    flat = re.sub(r"\s+", " ", "".join(c if " " <= c < "\x7f" else " " for c in text)).strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _summary(text: str, red: Redactor) -> str | None:
    """Claude's closing text, made safe to show: redacted, cleaned of anything that could
    pass for an approval code, clipped. Only ever shown after a code-built ``R3:`` prefix."""
    flat = clean_external(red.text(text))
    cleaned = flat if len(flat) <= SUMMARY_MAX else flat[: SUMMARY_MAX - 3].rstrip() + "..."
    return cleaned or None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _size(n: int) -> str:
    return f"{n / 1e9:g} GB" if n >= 1_000_000_000 else f"{n // 1_000_000} MB"


def _sandbox_wrap(argv: Sequence[str], params: SandboxParams) -> Sequence[str]:
    return wrap(argv, params)


# --- the daily cleanup job -----------------------------------------------------------------

CLEANUP_JOB_ID = "run-cleanup"
CLEANUP_SCHEDULE = "30 4 * * *"


def cleanup_job(deps: CoreDeps) -> CoreJob | None:
    """The ``CORE_JOBS`` factory: delete run dirs, worktrees (with their local branches) and
    scratch dirs of runs that ended more than ``runner.keep_days`` ago, prune the cache to
    ``runner.max_cache_gb`` and wipe stale token dirs. Checkouts are never deleted. None when
    no repo is registered and no run, code or cache dir exists."""
    from resonant.extensions.scheduler import CoreJob

    cfg, conn = deps.settings.runner, deps.conn
    if (
        not cfg.repos
        and not repo_store.list_all(conn)
        and not (cfg.work_dir.exists() or cfg.cache_dir.exists() or cfg.code_dir.exists())
    ):
        return None  # no repo registered and nothing on disk

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        ended = runs.ended_at_map(conn)
        result = await asyncio.to_thread(
            cleanup,
            cfg.work_dir,
            cfg.cache_dir,
            secrets_dir=cfg.secrets_dir,
            mirror_dir=cfg.mirror_dir,
            ended_at=ended,
            keep_days=cfg.keep_days,
            max_cache_bytes=cfg.max_cache_gb * 1024**3,
        )
        for error in result.errors:
            log.warning("run cleanup: %s", error)
        workspace = Workspace(cfg, conn)  # its checkout locks are the runner's (per loop)
        cutoff = time.time() - cfg.keep_days * 86400
        removed = 0
        for code, at in sorted(ended.items()):
            old = runs.get(conn, code)
            if at > cutoff or old is None or old.workspace is None:
                continue
            try:
                removed += await workspace.remove_workspace(
                    code, workspace=old.workspace, checkout=old.checkout, branch=old.branch,
                    expect=old.head_sha or old.base_sha,
                )  # fmt: skip
            except Exception as e:  # one bad worktree must not stop the rest
                log.warning("run cleanup: %s: %s", code, e)
                continue
            runs.update(conn, code, workspace=None, checkout=None)  # done: not tried again
        return {"removed": len(result.removed_runs), "worktrees": removed,
                "freed_bytes": result.cache_freed_bytes, "errors": len(result.errors)}  # fmt: skip

    return CoreJob(
        id=CLEANUP_JOB_ID, schedule=CLEANUP_SCHEDULE, run=run, catchup=timedelta(hours=12)
    )

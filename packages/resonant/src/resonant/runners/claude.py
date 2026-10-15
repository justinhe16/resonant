"""ClaudeRunner (``runner="claude"``): one Claude Code session per run, in a sandboxed throwaway
clone (docs/phase5a-plan.md D2 to D4).

A run is a task of kind ``run`` plus a row in ``runs`` (migration 006) that holds all the state a
restart needs. One ``step`` drives a run from its saved step to the next stopping point, holding
a Claude slot and the repo's run lock the whole time:

    prepare   refresh the mirror, check the repo has CI, clone, branch (``Workspace.prepare``)
    session   the cached sandbox self-check (fails closed), then ``claude -p`` under
              ``sandbox.wrap`` with a from-scratch environment
    harvest   ``git bundle`` inside the sandbox, fd-based import into the mirror, checks
    publish   re-check cancel/pause, push, ``gh pr create``
    reply     the code-built message to the requester

Each step is saved in ``runs.step`` before it starts, so a crash resumes at the last one; the
session is resumed by id (``--resume``) in the same run dir, after the old process tree has been
reaped. Nothing after the session starts writes into the run dir except through the sandbox.

Outcomes. A usage limit parks the run (``Wait("usage_reset")``: slot and lock freed; the wait
doesn't count as session time or an attempt, and 24 h of waiting in total fails the run). The
time limit, the disk limit, the turn or dollar limit and a Claude error fail the run and keep its
dir ("work kept"), nothing is pushed. ``/cancel`` and ``/kill`` end the whole process tree,
delete the dir and never publish: ``publish`` re-reads the run and the kill switch before the
push and again before the PR.

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
from resonant.runner.checks import CheckFailed, GitError
from resonant.runner.cleanup import cleanup, delete_run_dir, watch_disk, wipe_secret_dirs
from resonant.runner.workspace import (
    BundleRefused,
    MergeRefused,
    NoChanges,
    Prepared,
    PublishAborted,
    RunnerError,
    Workspace,
    committer_env,
)
from resonant.runners import reaper
from resonant.runners.reaper import MARKER_ENV, Target
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
    RunnerSecrets,
    SecretError,
    SecretStore,
    load_runner_secrets,
    missing_runner_secrets,
)
from resonant.store import pulse as pulse_store
from resonant.store import runs
from resonant.store import tasks as task_store
from resonant.store.audit import audit
from resonant.store.db import atomic, transaction, utcnow
from resonant.store.runs import Run

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
STEP_SLACK_S = 600.0  # prepare, harvest, import and publish on top of the session limit
SAVE_EVERY_S = 30.0
PULSE_TTL_S = 48 * 3600
PULSE_SOURCE = "core"
KILL_REASON = "stopped by /kill"
CHECK_CACHE = "sandbox-check.json"
TOOL_NAMES = ("claude", "uv")

RUNNER_PROMPT = (
    "You are working in a throwaway clone, on branch {branch}, which is already checked out. "
    "Commit your work with clear messages. Do not push, do not change remotes, and do not "
    "touch git config. End with a 1-3 line summary of what changed and anything left undone."
)
RESUME_PROMPT = (
    "Your session was interrupted. Continue the task you were given. If it is already done, "
    "make sure the work is committed and give the closing summary."
)

_LIMIT = re.compile(r"usage limit|rate limit|hit your limit|limit reached", re.IGNORECASE)
_EPOCH = re.compile(r"\|\s*(\d{10})\b")


# --- results of the public API -------------------------------------------------------------


@dataclass(frozen=True)
class Started:
    code: str
    repo: str
    max_minutes: int

    @property
    def text(self) -> str:
        return (
            f"On it: {self.code} · {self.repo}. Up to {self.max_minutes}m. "
            f"/runs to check, /cancel {self.code} to stop."
        )


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


def reply_text(run: Run) -> str | None:
    """The message for an ended run (None for a state that has none)."""
    took = fmt_duration(run.session_seconds)
    summary = f"\n{run.code}: {run.summary}" if run.summary else ""
    match run.status:
        case "ready":
            files = _plural(run.files_changed or 0, "file")
            text = f"{run.code} done: PR #{run.pr_number} · {run.pr_url} · {files} · {took}"
            if run.flag_line:
                text += f"\n{run.flag_line}"
            return text + f"\n/merge {run.code} when CI is green." + summary
        case "no_changes":
            return f"{run.code}: no changes." + (f" {run.summary}" if run.summary else "")
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
        create_task: Callable[[NewTask], str] | None = None,
        cancel_task: Callable[[str], bool] | None = None,
        wrap: Wrap | None = None,
        self_check: SelfCheck | None = None,
        claude_cmd: Sequence[str] | None = None,
        read_paths: Sequence[Path] | None = None,
        max_session_s: int | None = None,
        max_turns: int = MAX_TURNS,
        poll_s: float = 1.0,
        kill_grace_s: float = reaper.GRACE_S,
        disk_interval_s: float = 60.0,
        disk_limit_bytes: int | None = None,
        defer_s: float = DEFER_S,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """``max_runs``: sessions at once (the Claude slots minus the on-call reserve).
        ``create_task`` inserts the run's task and wakes the loop (``Loop.create_task``) and
        ``cancel_task`` cancels one (``Loop.cancel``); without them the runner writes the task
        itself and relies on its own polling. ``wrap``, ``self_check`` and ``claude_cmd`` are
        the seams for tests; by default the Seatbelt sandbox, the cached self-probe and the
        ``claude`` on the daemon's PATH."""
        self.conn = conn
        self.cfg = runner
        self.channel = channel
        self.secrets = secrets
        self.tz = ZoneInfo(timezone)
        self.max_runs = max_runs
        self.workspace = workspace or Workspace(_with_real_git(runner))
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
        self.defer_s = defer_s
        self.clock = clock
        self.max_step_s: float | None = self.max_session_s + STEP_SLACK_S
        self.state_dir = runner.mirror_dir.parent
        self.transcript_dir = self.state_dir / "transcripts"
        self.last_killed = 0  # runs the last kill switch engage ended
        self._locks: dict[str, asyncio.Lock] = {}  # one run at a time per repo
        self._working: set[str] = set()  # codes with a step in flight
        self._live: dict[str, Target] = {}  # codes with a session process
        self._cleanup_locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._unhook: Callable[[], None] | None = None

    # -- lifecycle --

    async def start(self) -> int:
        """Daemon start, before the loop resumes anything: end every process a crash left,
        wipe leftover token dirs, end runs whose task is gone, and hook the kill switch.
        Returns how many runs had processes."""
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
        await asyncio.to_thread(wipe_secret_dirs, self.cfg.secrets_dir, self.cfg.mirror_dir)
        await self._reconcile()
        if self._unhook is None:
            self._unhook = killswitch.add_pause_hook(self._on_pause)
        return reaped

    async def aclose(self) -> None:
        """Remove the kill switch hook and wait for the kills it started."""
        if self._unhook is not None:
            self._unhook()
            self._unhook = None
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
            marker=run.code,
        )

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- public API --

    async def setup_missing(self) -> str | None:
        """``The runner isn't set up: …`` naming exactly what is missing, or None."""
        missing: list[str] = []
        how: list[str] = []
        if not self.cfg.repos:
            missing.append("no repos in runner.repos")
            how.append("name them under runner.repos in ~/.resonant/config.yaml")
        try:
            refs = await missing_runner_secrets(self.secrets)
        except SecretError as e:
            missing.append(f"the Keychain can't be read ({e.reason})")
            how.append("unlock the login keychain")
        else:
            for ref in refs:
                missing.append(f"secret {ref}")
                how.append(f"resonant secret set {ref.split(' ', 1)[0]}")
        if self._claude_cmd is None and resolve_tool("claude") is None:
            missing.append("the claude CLI isn't installed")
            how.append("install Claude Code")
        return not_set_up_text(missing, how) if missing else None

    async def start_run(self, repo: str, ask: str, principal: str) -> Started | Refused:
        """Create a run and its task. Refuses (with a sentence for the requester) when the
        runner isn't set up, the repo isn't listed, or Resonant is paused. Two runs on one repo
        queue: the second waits for the first."""
        if (text := await self.setup_missing()) is not None:
            return Refused(text)
        if repo not in self.cfg.repos:
            return Refused(f"No repo named {repo}. Known: {', '.join(sorted(self.cfg.repos))}.")
        ask = ask.strip()
        if not ask:
            return Refused("What should I do? Give the run something to work on.")
        if len(ask) > MAX_ASK:
            return Refused(f"That ask is too long ({len(ask)} characters, at most {MAX_ASK}).")
        if self._paused():
            return Refused("Resonant is paused. /resume first.")
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
            checkpoint={"run": run.code, "title": f"run {run.code} · {repo}"},
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
        return Started(run.code, repo, max(1, self.max_session_s // 60))

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
        if run.status != "ready" or run.pr_number is None or run.head_sha is None:
            return Refused(f"{run.code} isn't ready to merge ({run.status}).")
        try:
            secrets = await load_runner_secrets(self.secrets)
        except SecretError:
            return Refused((await self.setup_missing()) or "The runner's secrets can't be read.")
        try:
            await self.workspace.merge(run.repo, run.pr_number, run.head_sha, secrets)
        except MergeRefused as e:
            return Refused(f"{run.code} not merged: {Redactor(secrets.values()).text(str(e))}")
        except RunnerError as e:
            return Refused(f"{run.code} not merged: {Redactor(secrets.values()).text(str(e))}")
        with atomic(self.conn):
            runs.update(self.conn, run.code, status="merged")
            audit(self.conn, "run.merged", run=run.code, pr=run.pr_number)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(delete_run_dir, self.cfg.work_dir, run.code)
        return Merged(run.code, run.pr_number, run.flagged_paths)

    # -- the loop's entry point --

    async def step(self, task: Task, events: list[Any], ctx: StepContext) -> StepOutcome:
        code = task.checkpoint.get("run")
        run = runs.get(self.conn, code) if isinstance(code, str) else None
        if run is None:
            return Fail("run not found")
        if not run.active:
            return await self._conclude(run)
        if self._paused():
            return self._defer(task, "paused")
        lock = self._locks.setdefault(run.repo, asyncio.Lock())
        if lock.locked() or len(self._working) >= self.max_runs:
            return self._defer(task, "busy")  # waits without holding a Claude slot
        await lock.acquire()  # nothing awaited since the check: it can't have been taken
        self._working.add(run.code)
        try:
            return await self._drive(task, run, ctx)
        except asyncio.CancelledError:
            await self._on_cancelled(run.code)
            raise
        finally:
            self._working.discard(run.code)
            lock.release()

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
            if run.status == "waiting":
                run = self._end_wait(run)
                if run.wait_seconds > MAX_WAIT_S:
                    return await self._fail(run, "usage limit didn't reset within 24h", w.redactor)
            if run.step == "prepare":
                run = await self._prepare(w, run)
            p = self._prepared(run)
            if run.step == "session":
                outcome = await self._session(w, run, p)
                if outcome is not None:
                    return outcome
                run = self._must_get(run.code)
            if run.step == "harvest":
                run = await self._harvest(w, run, p)
            if run.step == "publish":
                run = await self._publish(w, run, p)
        except _Cancelled:
            return await self._conclude(self._must_get(run.code))
        except NoChanges:
            return await self._end(run, "no_changes")
        except (RunnerError, CheckFailed, BundleRefused) as e:
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
        p = await self.workspace.prepare(
            run.code, run.repo, run.ask, github_token=w.secrets.github_token
        )
        params = self._params(p.run_dir)
        try:
            await _finish_thread(self._prepare_session_dirs, params)
        except (SandboxError, OSError) as e:
            raise RunnerError(f"the sandbox can't be set up: {e}") from None
        runs.update(
            self.conn,
            run.code,
            status="running",
            step="session",
            branch=p.branch,
            base_branch=p.base_branch,
            base_sha=p.base_sha,
            started_at=self.clock(),
        )
        w.ctx.save(claude_session_id=run.session_id, worktree_path=str(p.run_dir))
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
        link_tools(params, tools)

    def _prepared(self, run: Run) -> Prepared:
        assert run.branch and run.base_branch and run.base_sha
        run_dir = self.cfg.work_dir / run.code
        return Prepared(run.code, run.repo, run_dir, run_dir / "repo", run.branch,
                        run.base_branch, run.base_sha)  # fmt: skip

    def _params(self, run_dir: Path) -> SandboxParams:
        return SandboxParams(
            run_dir=run_dir,
            cache_dir=self.cfg.cache_dir,
            secrets_dir=self.cfg.secrets_dir,
            read_paths=self._read_paths,
            deny_exec=default_deny_exec(),
        )

    def _cached_self_check(self, params: SandboxParams) -> tuple[bool, str]:
        return cached_self_check(params, self.state_dir / CHECK_CACHE)

    def _env(
        self, params: SandboxParams, code: str, secrets: RunnerSecrets | None
    ) -> dict[str, str]:
        """The sandbox environment: from scratch, the one allowed secret only when asked."""
        bins = [p for p in params.read_paths if p.name == "bin"]
        env = scratch_env(
            params,
            path_dirs=bins,
            claude_token=None if secrets is None else secrets.claude_token,
            anthropic_key=None if secrets is None else secrets.anthropic_key,
        )
        env[MARKER_ENV] = code
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
            json.dumps({"sandbox": {"enabled": False}, "autoUpdates": False}),
            "--append-system-prompt",
            RUNNER_PROMPT.format(branch=p.branch),
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
        params = self._params(p.run_dir)
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
        argv = list(
            self._wrap(
                self._argv(run, p, params, api_billing=bool(w.secrets.anthropic_key)), params
            )
        )
        env = self._env(params, run.code, w.secrets)
        prompt = (RESUME_PROMPT if run.sessions else run.ask).encode()
        writer = TranscriptWriter(self.transcript_dir / f"{run.code}.jsonl", w.redactor)
        state = StreamState()
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=p.repo_dir,
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
        target = Target(proc.pid, proc.pid, created, run_dir=p.run_dir, marker=run.code)
        self._live[run.code] = target
        runs.set_process(self.conn, run.code, proc.pid, proc.pid, created)
        stop = await self._supervise(w, run, p, proc, target, writer, state, prompt)
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
        runs.clear_process(self.conn, run.code)
        run = self._must_get(run.code)
        if stop == "cancelled":
            raise _Cancelled
        if stop == "timeout":
            return await self._timed_out(run, w)
        if stop == "disk":
            over = _size(self.disk_limit_bytes)
            return await self._fail(
                run, f"used over {over} of disk (work kept: /runs {run.code})", w.redactor
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
            async for raw, truncated in read_lines(proc.stdout):
                writer.write(raw, truncated)
                if not truncated:
                    state.feed(raw)

        async def pump_err() -> None:
            assert proc.stderr
            async for raw, truncated in read_lines(proc.stderr, 8192):
                writer.note("stderr", text=raw.decode(errors="replace"), truncated=truncated)

        async def disk_exceeded(_size: int) -> None:
            nonlocal stop
            stop = stop or "disk"

        pumps = [asyncio.create_task(c) for c in (feed(), pump_out(), pump_err())]
        disk = asyncio.create_task(
            watch_disk(
                p.run_dir, self.disk_limit_bytes, disk_exceeded, interval_s=self.disk_interval_s
            )
        )
        exited = asyncio.create_task(proc.wait())
        began = time.monotonic()
        last_save = began
        bucket = _bucket(run.session_seconds)
        try:
            while True:
                done, _ = await asyncio.wait({exited}, timeout=self.poll_s)
                if done:
                    break
                reaper.sample(target)
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
            # Also on cancellation: nothing of the session may outlive the step.
            disk.cancel()
            await reaper.reap(target, grace_s=self.kill_grace_s)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
            exited.cancel()
            done_pumps, pending = await asyncio.wait(pumps, timeout=5)
            for t in pending:
                t.cancel()
            await asyncio.wait([*done_pumps, *pending, disk, exited])
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
        runs.update(self.conn, run.code, summary=summary, step="harvest")
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
        runs.update(self.conn, run.code, status="waiting", waiting_since=now)
        return Wait("usage_reset", dict(w.task.checkpoint), wake)

    def _end_wait(self, run: Run) -> Run:
        """A waiting run is woken: its wait is added up and the run is running again."""
        waited = 0.0
        if run.waiting_since is not None:
            waited = max((self.clock() - run.waiting_since).total_seconds(), 0.0)
        runs.update(
            self.conn, run.code, status="running", waiting_since=None,
            wait_seconds=run.wait_seconds + waited,
        )  # fmt: skip
        return self._must_get(run.code)

    async def _harvest(self, w: _Work, run: Run, p: Prepared) -> Run:
        self._check_stop(run.code)
        params = self._params(p.run_dir)
        env = self._env(params, run.code, None)  # no token: the harvest doesn't need one
        env.update(committer_env())
        await self.workspace.harvest(p, lambda argv: self._wrap(argv, params), env)
        self._check_stop(run.code)
        head = await self.workspace.import_bundle(p)
        self._check_stop(run.code)
        runs.update(self.conn, run.code, head_sha=head, step="publish")
        return self._must_get(run.code)

    async def _publish(self, w: _Work, run: Run, p: Prepared) -> Run:
        assert run.head_sha
        self._check_stop(run.code)
        checked = await self.workspace.check(p, run.head_sha, w.secrets)
        runs.update(self.conn, run.code, files_changed=checked.files,
                    flagged_paths=checked.flagged)  # fmt: skip

        async def still_ok() -> bool:
            return not self._stopped(run.code)

        try:
            pr = await self.workspace.publish(
                p, checked, w.secrets, ask=run.ask, summary=run.summary or "", still_ok=still_ok
            )
        except PublishAborted:
            raise _Cancelled from None
        if not runs.finish(self.conn, run.code, "ready", step="reply", pr_number=pr.number,
                           pr_url=pr.url, head_sha=checked.head_sha):  # fmt: skip
            runs.update(self.conn, run.code, pr_number=pr.number, pr_url=pr.url)
            raise _Cancelled  # cancelled just after the PR was opened
        with atomic(self.conn):
            audit(self.conn, "run.ready", run=run.code, pr=pr.number)
        return self._must_get(run.code)

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
        try:
            await self._send_reply(run)
        except ChannelError as e:
            return Fail(f"reply failed: {e}", retryable=True)
        if run.status == "failed":
            return Fail(run.failure or "failed")
        return Done({"run": run.code, "status": run.status})

    async def _send_reply(self, run: Run) -> None:
        text = reply_text(run)
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
            target = self._live.get(code)
            if target is not None:
                await reaper.reap(target, grace_s=self.kill_grace_s)
            with contextlib.suppress(Exception):
                # two deletes of one tree would trip over each other, hence the lock
                await _finish_thread(delete_run_dir, self.cfg.work_dir, code)
            self._pulse_clear(run)
            if send:
                with contextlib.suppress(ChannelError):
                    await self._send_reply(run)

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
        title = f"{run.code} still running · {run.repo} · started {at}"
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
                self.conn, PULSE_SOURCE, key, f"{run.code} finished · {run.repo}", "normal",
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


def _with_real_git(cfg: RunnerConfig) -> RunnerConfig:
    """The workspace's git also runs inside the sandbox (the harvest), where Apple's
    ``/usr/bin/git`` shim can't write its cache; use the real binary."""
    if cfg.git_path == Path("/usr/bin/git") and (real := resolve_tool("git")) is not None:
        return cfg.model_copy(update={"git_path": real})
    return cfg


# --- the daily cleanup job -----------------------------------------------------------------

CLEANUP_JOB_ID = "run-cleanup"
CLEANUP_SCHEDULE = "30 4 * * *"


def cleanup_job(deps: CoreDeps) -> CoreJob | None:
    """The ``CORE_JOBS`` factory: delete run dirs that ended more than ``runner.keep_days``
    ago, prune the cache to ``runner.max_cache_gb`` and wipe stale token dirs. None when no
    repo is configured (there is nothing to clean)."""
    from resonant.extensions.scheduler import CoreJob

    cfg, conn = deps.settings.runner, deps.conn
    if not cfg.repos:
        return None

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        result = await asyncio.to_thread(
            cleanup,
            cfg.work_dir,
            cfg.cache_dir,
            secrets_dir=cfg.secrets_dir,
            mirror_dir=cfg.mirror_dir,
            ended_at=runs.ended_at_map(conn),
            keep_days=cfg.keep_days,
            max_cache_bytes=cfg.max_cache_gb * 1024**3,
        )
        for error in result.errors:
            log.warning("run cleanup: %s", error)
        return {"removed": len(result.removed_runs), "freed_bytes": result.cache_freed_bytes,
                "errors": len(result.errors)}  # fmt: skip

    return CoreJob(
        id=CLEANUP_JOB_ID, schedule=CLEANUP_SCHEDULE, run=run, catchup=timedelta(hours=12)
    )

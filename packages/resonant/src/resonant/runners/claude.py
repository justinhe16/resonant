"""ClaudeRunner (``runner="claude"``): one Claude Code session per run, in a worktree of the
owner's checkout or, with no repo, a scratch dir (docs/employee-plan.md D1, D3 to D6).

A run is a task of kind ``run`` plus a row in ``runs`` (migration 006) that holds all the state a
restart needs. One ``step`` drives a run from its saved step to the next stopping point, holding
a Claude slot the whole time (two runs on one repo go at once, each in its own worktree):

    prepare   clone the checkout on first use (a fork run: fork the repo first), fetch the base
              into the daemon-only mirror, note whether the base has CI, add the run's
              worktree on a new branch and link the checkout's local config into it
              (``Workspace.prepare``); a run with no repo gets an empty scratch dir
    session   a per-run GitHub App token for the one repo (``runner.github_app``), the cached
              sandbox self-check (fails closed), then ``claude -p`` under ``sandbox.wrap`` with a
              from-scratch environment. The session itself pushes its branch, opens the PR,
              watches CI and fixes it (docs/repos-plan.md D10); the token is revoked when the
              session ends, however it ends. A run with no repo gets no token and ends ``done``
              with Claude's summary
    harvest   (a historical name, kept because the runs CHECK names it) find the PR by its head
              branch and check it the way a merge will; no PR and no pushed commits is
              ``no_changes``. A fork run's pushed branch is checked here instead
    publish   fork runs only: ask the owner (an ``external_pr`` check), then open the
              upstream PR with the ``public_repo`` token (EM-7)
    reply     the code-built message to the requester, by urgency (EM-6,
              ``runners/reports.py``): texted now (a failure, ``/run``, a ``notify_now`` rule)
              or a line in the hourly digest's ``Workers:`` section, either with the run's
              ``did:`` list (side effects: ``did:`` notes, a worker's restart or update)

Each step is saved in ``runs.step`` before it starts, so a crash resumes at the last one; the
session is resumed by id (``--resume``) in the same run dir, after the old process tree has been
reaped. After the session starts the daemon writes into the run dir only to refresh the token
files, through ``O_NOFOLLOW`` directory fds. Merging (``merge_run``) fetches the PR's head into
the daemon-only mirror and runs the checks first; no merge path skips them.

Outcomes. A usage limit parks the run (``Wait("usage_reset")``: slot freed; the wait
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
resumes the same session with ``--resume`` and the news as the prompt.

Live sessions (RC-1, ``runners/live.py``). Every session runs with ``--input-format
stream-json`` and ``-n R<n>``; the prompt is a user line and stdin stays open while the process
lives. An owner message, or an answer whose CLI call already gave up, is written into the live
session at once (``_push_live``). Each ``result`` is a turn's end (``_turn_ended``): what the
run hasn't seen yet goes in as the next turn, else Resonant ends the session (Remote Control
off, stdin closed, the reaper after ``end_grace_s`` of quiet). The park and ``--resume`` path
above stays for news that comes once the process is gone. With ``runner.remote_control: on``
each session first sends the ``remote_control`` control request: the session URL is stored on
the run (``runs.remote_url``) and texted once per run; an error or no answer within
``rc_timeout_s`` is texted once instead, and the run goes on. API for the relay
(EM-5): :meth:`~ClaudeRunner.open_questions`, :meth:`~ClaudeRunner.answer_question`,
:meth:`~ClaudeRunner.deliver_message`, :attr:`~ClaudeRunner.events`.

Public API for the tools and slash commands: :meth:`ClaudeRunner.start_run`,
:meth:`~ClaudeRunner.list_runs`, :meth:`~ClaudeRunner.cancel_run`,
:meth:`~ClaudeRunner.merge_run`. Their results are plain dataclasses; every reply text is built
here, from code, never from model output: Claude's closing summary is cleaned, redacted,
clipped, and only shown after a code-built ``R3:`` prefix.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import psutil

from resonant import killswitch
from resonant.config import RunnerConfig, RunnerRepo
from resonant.extensions.notify import clean_text
from resonant.gate import actions as action_ids
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
from resonant.ops import take_planned
from resonant.redact import Redactor
from resonant.runner import github_app as github_app_mod
from resonant.runner.checks import CheckFailed, GitError
from resonant.runner.claude_auth import LOGIN_HOW, LoginCheck, auth_source
from resonant.runner.cleanup import (
    RUN_CODE,
    DirSize,
    DiskLimit,
    cleanup,
    delete_run_dir,
    free_bytes,
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
    NeedsToken,
    NoAccess,
    NotFound,
    OfflineGitHub,
    Resolved,
    Resolver,
)
from resonant.runner.workspace import (
    CREDS_DIR,
    FORK_NEEDS_PUBLIC_REPO,
    GITHUB_TOKEN_HOW,
    CiPending,
    MergeRefused,
    NoChanges,
    Prepared,
    RunnerError,
    Workspace,
    needs_github_token,
    read_session_token,
    session_git_env,
    upstream_merges,
    write_session_credentials,
)
from resonant.runners import activity, reaper, worker_cli
from resonant.runners.live import (
    RC_OFF_TIMEOUT_S,
    RC_TIMEOUT_S,
    ControlReply,
    LiveInput,
    session_url,
)
from resonant.runners.reaper import MARKER_ENV, Target
from resonant.runners.reports import (
    HELD_KINDS,
    MergedBy,
    Report,
    WorkerReports,
    alerts,
    clear_alerts,
    deps_line,
    detected_list,
    end_event,
    end_title,
    merged_by_of,
    merged_list,
    merged_names,
    record_detected,
    set_alert,
    set_merged,
    set_merged_by,
    shown_did,
)
from resonant.runners.side_effects import RunContext, SideEffect, detect
from resonant.runners.worker_cli import Check, WorkerServer
from resonant.runners.worker_desk import (
    BLOCK_S,
    CHECK_OK,
    RESTARTED,
    OpenQuestion,
    Relayed,
    RelayRefused,
    SelfOps,
    TrustChecker,
    WorkerDesk,
    WorkerEvent,
    WorkerEvents,
)
from resonant.sandbox import (
    SandboxError,
    SandboxParams,
    default_deny_exec,
    default_path_dirs,
    link_tools,
    owner_env,
    prepare_dirs,
    resolve_tool,
    scratch_env,
    wrap,
)
from resonant.sandbox.probes import cached_self_check
from resonant.secrets import (
    CLAUDE_MISSING,
    GITHUB_APP_KEY_REF,
    GITHUB_PUBLIC_TOKEN_REF,
    GITHUB_TOKEN_REF,
    RunnerSecrets,
    SecretError,
    SecretStore,
    load_runner_secrets,
    missing_runner_secrets,
)
from resonant.store import approvals as approvals_store
from resonant.store import questions as questions_store
from resonant.store import repos as repo_store
from resonant.store import runs, system_texts
from resonant.store import tasks as task_store
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_set, transaction, utcnow
from resonant.store.runs import Run
from resonant.trust import merge_decision
from resonant_sdk import Event

if TYPE_CHECKING:
    from resonant.executor import CallContext
    from resonant.extensions.scheduler import CoreDeps, CoreJob

log = logging.getLogger(__name__)

RUNNER_NAME = "claude"
SELF_REPO_NAME = "resonant"  # the daemon's own checkout, registered at start (GD-1)
NO_GH_DIR = "gh-none"  # an empty gh config dir for runs with no GitHub token
TASK_KIND = "run"
MAX_ATTEMPTS = 2  # a session, and one resume after a daemon crash
MAX_WAIT_S = 24 * 3600.0  # usage-limit waiting, in total
DEFAULT_WAIT_S = 3600.0  # when the limit message names no reset time
MIN_WAIT_S = 60.0
DEFER_S = 15.0  # how long a run waits for a free slot
MAX_LINE = 1024 * 1024  # one stream-json line, in the transcript and in memory
TRUNCATED_TAIL = 256  # dropped from a truncated line: a secret may be cut at the limit
MAX_TRANSCRIPT = 256 * 1024 * 1024
MAX_ASK = 4000
SUMMARY_MAX = 600
REASON_MAX = 240
MAX_TURNS = 200
CANCEL_GRACE_S = 2.0  # /kill and /cancel: SIGTERM, then SIGKILL after this
END_GRACE_S = 15.0  # stdin closed after the last result: this long quiet, then the kill (RC-1)
REMOTE_KV = "runner.remote_control"  # the latest Remote Control answer: {state, at, run, detail}
STEP_SLACK_S = 600.0  # prepare, the PR lookup and publish on top of the session limit
SAVE_EVERY_S = 30.0
KILL_REASON = "stopped by /kill"
CHECK_CACHE = "sandbox-check.json"
TOOL_NAMES = ("claude", "uv")
MAX_FIX_ATTEMPTS = 3  # CI fixes the session may push after the first push
TOKEN_REFRESH_S = 50 * 60  # installation tokens last an hour: a fresh one after 50 minutes
# in all: the app's three tries (each up to its own timeout) and the backoff between them
MERGE_WAIT_S = 2 * 3600.0  # auto-merge waits this long after the run for CI to finish
MERGE_POLL_S = 120.0  # ... looking at CI this often
EXTERNAL_PR = "external_pr"  # the trust action of opening a PR on someone else's repo
# flock'd by the runner that owns runner.work_dir (the daemon) from start() to aclose():
# only it sweeps the run dirs' credentials and ctl sockets at startup (PER-271)
WORK_DIR_LOCK = ".daemon.lock"
LOCK_WAIT_S = 20.0  # how long start() waits for a predecessor to let go of it
LOCK_POLL_S = 0.25
# a push run's session left commits it couldn't push: the run fails with this reason, and
# the reply leads with it (PER-271: not "made no changes")
COULDNT_PUSH = "couldn't push: "
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
# SF-1: a push run with the guard off loads the owner's ~/.claude, surf skills included. The
# runner can't tell a code change from ops or research, so the prompt states when to use them.
SURF_PROMPT = (
    "\n\nThe owner's surf skills: use them only when all three hold: this run has a repo, "
    "the ask is a code change, and the result is meant to be a pull request that gets merged. "
    "Then work through /robot-surf (for a Linear ticket) or /robot-surf-prompt (for a freeform "
    "ask), or another surf skill when it fits better, here in this worktree on {branch} (don't "
    "create another worktree or branch): build the change, run the tests, open the pull "
    "request, get CI green, then run the code-reviewer agent and fix what it finds until it "
    "has no must-fix left, with CI green again after the fixes. For these runs green CI alone "
    "is not the place to stop. Skip the skill's wait for human review: stop then and give the "
    "summary. The rules above still hold. Merging stays with Resonant: it merges a green pull "
    "request per the owner's trust rules, so don't merge it yourself. In every other case "
    "(no repo, ops, research, an investigation, a review only, a one-off or unusual ask) "
    "don't invoke a surf skill; just do what the ask says. If the ask asks for a different "
    "flow, the ask wins."
)
FORK_PROMPT = (
    "You are working in a git worktree of {fork}, the owner's fork of {upstream} (your current "
    "directory), on branch {branch}, which is already checked out and starts from {upstream}'s "
    "{base}. The checkout's local config (.env files and the like) is linked in and kept out of "
    "git: use it, never commit it. git is set up with credentials for {fork} only. Commit your "
    "work with clear messages; the last commit's first line becomes the pull request's title "
    "and the rest of its message the description, so write them for {upstream}'s maintainers. "
    "Push it with `git push -u origin {branch}`, and then stop: do not open a pull request. "
    "Resonant checks the branch and asks the owner before it opens the pull request on "
    "{upstream}. Never push to any branch but {branch}, don't force-push, and don't change "
    "remotes, git config or the credential files. If git fails with an authentication error, "
    "run it again. End with a 1-3 line summary: what changed and anything left undone."
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
    "for what the owner must see now). After any side effect outside your branch and PR (a "
    "merge with gh, a deploy, a push to another repo, an email, a payment), run `resonant note "
    '"did: <what you did>"`: those go in your report to the owner. '
    "`resonant check --system <s> --action <kind> "
    '[--irreversible] [--cost <usd>] "<what>"`: before anything that could be net new: a '
    "deploy, a write to production data, an outgoing email or message, spending money, deleting "
    "data, a force-push, or a system you haven't used in this task. Go ahead only on ok; on "
    "denied, don't. Replies can end with messages from the owner: follow them."
)
# GD-1, when the daemon can restart itself for a worker (``self_ops``).
RESTART_PROMPT = (
    " `resonant restart` restarts Resonant itself and `resonant update` deploys it (pulls its "
    "main, syncs, restarts); your session is stopped by the restart and resumed after it. Never "
    "restart or stop Resonant any other way (launchctl, kill, `resonant daemon`): your session "
    "runs under it."
)
# GD-1: a run on Resonant's own repo (the daemon's checkout, ``ops.repo_path``).
SELF_REPO_PROMPT = (
    "\n\nThis repo is Resonant itself: the running daemon is the checkout at {checkout}. "
    "Work only in your worktree; never edit, commit in or switch branches in that checkout. "
    "A change reaches the running daemon only once it is merged to main and deployed with "
    "`resonant update`; `resonant restart` only restarts."
)
# Put in front of the resume prompt after a restart the run asked for (``resonant restart``).
RESTART_NOTE = (
    "Resonant restarted as you asked; the restart stopped your session and this resumes it. "
    "Check that what you wanted from the restart happened, then carry on."
)
RESUME_PROMPT = (
    "Your session was interrupted. Continue the task you were given. If it is already done, "
    "make sure the work is pushed, the pull request is open and its CI has been checked, and "
    "give the closing summary."
)
# RX-1: a repo run that can't push yet (the GitHub App, or the daemon's own token, isn't set
# up). The session gets no GitHub credentials and leaves its work on its local branch.
LOCAL_PROMPT = (
    "You are working in a git worktree of {repo} (your current directory), on branch {branch}, "
    "which is already checked out and starts from {base}. The checkout's local config (.env "
    "files and the like) is linked in and kept out of git: use it, never commit it. This run "
    "has no GitHub credentials ({why}): don't push and don't open a pull request; the commits "
    "on {branch} are what the owner gets. Commit your work on {branch} with clear messages. "
    "Never commit to {base} or to any other branch, and don't change remotes or git config. "
    "End with a 1-3 line summary: what changed and anything left undone."
)
LOCAL_RESUME_PROMPT = (
    "Your session was interrupted. Continue the task you were given. If it is already done, "
    "make sure the work is committed on your branch (don't push), and give the closing summary."
)
# Why a repo run can't push, by the key kept in its task checkpoint (``local``): the short
# form for the start ack, the long one for the end reply.
NO_PUSH_SHORT = {"app": "GitHub App not set up", "token": "runner/github_token not set"}
NO_PUSH_LONG = {"app": "the GitHub App isn't set up", "token": "runner/github_token isn't set"}
APP_KEY_HOW = (
    f"resonant secret set {GITHUB_APP_KEY_REF} --from-file <the downloaded .pem>, "
    "then delete the .pem"
)
APP_CONFIG_HOW = "add runner.github_app with app_id and installation_id"
# A run with no repo has nothing to push and no PR (EM-2's scratch dir).
SCRATCH_RESUME_PROMPT = (
    "Your session was interrupted. Continue the task you were given. If it is already done, "
    "give the closing summary."
)

_LIMIT = re.compile(r"usage limit|rate limit|hit your limit|limit reached", re.IGNORECASE)
_EPOCH = re.compile(r"\|\s*(\d{10})\b")
# "5-hour limit reached ∙ resets 3pm", "resets at 3:30 PM": a clock time, no date (PER-256).
_RESETS = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b", re.IGNORECASE)


# --- results of the public API -------------------------------------------------------------


@dataclass(frozen=True)
class Started:
    code: str
    repo: str
    max_minutes: int
    new_repo: str | None = None  # the slug, when this run registered the repo
    warning: str | None = None  # e.g. the base branch isn't protected
    note: str | None = None  # how it ends: merged once CI is green, or a PR upstream after a yes
    via: str | None = None  # the extension whose link named the new repo
    limits: str | None = None  # RX-1: what this run can't do, said up front

    @property
    def text(self) -> str:
        by = f" via {self.via}" if self.via else ""
        new = f" (new repo: {self.new_repo}{by})" if self.new_repo else ""
        where = self.repo or "no repo (scratch dir)"
        text = (
            f"On it: {self.code} · {where}{new}. Up to {self.max_minutes}m. "
            f"/runs to check, /cancel {self.code} to stop."
        )
        for extra in (self.limits, self.note, self.warning):
            if extra:
                text += f"\n{extra}"
        return text


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
    deps: tuple[str, ...] = ()  # dependency files the PR changed (listed, not flagged)
    no_ci: str | None = None  # the repo, when an owner's merge went in without CI

    @property
    def text(self) -> str:
        lines = [f"{self.code} merged: PR #{self.pr_number}."]
        if self.no_ci:
            lines.append(f"⚠ merged without CI: {self.no_ci} has no CI checks")
        if self.flagged:
            lines.append(f"⚠ changes {', '.join(self.flagged)}")
        if self.deps:
            lines.append(deps_line(self.deps))
        return "\n".join(lines)


@dataclass(frozen=True)
class MergeQueued:
    """An owner's merge that waits for CI: the run's merge watch takes it from here."""

    code: str
    pr_number: int

    @property
    def text(self) -> str:
        return f"{self.code}: CI is still running; I'll merge PR #{self.pr_number} when it's green."


# --- code-built replies --------------------------------------------------------------------


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return f"{int(seconds)}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


NAME_MAX = 36  # a run's human name in a text, after its code: ``R4 (fix the login bug)``
END_EXTRA_LINES = 2  # an end text is a headline and at most this many short lines (PER-268)


def run_name(run: Run) -> str:
    """The run's human name for a text: the first words of its ask, cleaned and clipped at a
    word (PER-268: a code alone means nothing on a phone)."""
    flat = clean_text(run.ask or "")
    if len(flat) <= NAME_MAX:
        return flat
    cut = flat[: NAME_MAX - 1]
    if " " in cut[NAME_MAX // 2 :]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def run_who(run: Run) -> str:
    """``R4 (fix the login bug)``: the code (replies and /cancel need it) and the name."""
    name = run_name(run)
    return f"{run.code} ({name})" if name else run.code


def and_list(items: Sequence[str]) -> str:
    """``PR #1``, ``PR #1 and PR #2``, ``PR #1, PR #2 and PR #3``."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def end_parts(
    run: Run,
    ci: str | None = None,
    *,
    no_ci: bool = False,
    upstream: str | None = None,
    watching: bool = False,
    merged: Sequence[str] = (),
    merged_by: MergedBy | None = None,
) -> tuple[str, list[str], list[str]] | None:
    """An ended run's headline, its further lines (most important first) and the worker's
    own summary line (``said``, kept apart so a caller can put the side effects before it);
    None for a state that has none. Written like a short note from a coworker (PER-268):
    past tense, the code with the run's name. ``ci`` is the PR's CI as a word (``green``, ``red``,
    ``pending``, ``none``), looked up when the reply is sent. ``no_ci``: the repo has no
    pull_request workflow: only the owner's ``/merge`` merges it (without CI, with a
    warning), never an auto-merge. ``upstream``: a fork-mode run's original repo (its
    ``done`` names the PR opened there, or why none was). ``watching``: the run merges itself
    once CI is green. ``merged``: the PRs a run merged onto its base (``PR #121``): its
    ``done`` says so. ``merged_by`` (PER-269): the run's own PR was found merged and Resonant
    has a record of it: Resonant merged it (``R8's PR #2 is merged into main (you approved it
    in M6).``), or the run did on the owner's yes to its check."""
    took = fmt_duration(run.session_seconds)
    who = run_who(run)
    said = [f"{run.code}: {run.summary}"] if run.summary else []
    files = "" if run.files_changed is None else f", {_plural(run.files_changed, 'file')}"
    match run.status:
        case "ready":
            label = "no CI" if ci == "none" else f"CI {ci or 'unknown'}"
            head = f"{who} opened PR #{run.pr_number} ({label}{files}, {took}): {run.pr_url}"
            lines: list[str] = []
            if run.flag_line:
                lines.append(run.flag_line)
            if run.failure:  # the merge-time checks already refuse it
                lines.append(f"⚠ It can't be merged as it is: {run.failure}")
            if no_ci:
                lines.append(
                    f"No CI: {run.repo} has no pull_request workflow; "
                    f"/merge {run.code} merges it without CI."
                )
            elif watching:
                lines.append("I'll merge it when CI is green.")
            else:
                hint = "to merge it" if ci == "green" else "when CI is green"
                lines.append(f"/merge {run.code} {hint}.")
            return head, lines, said
        case "no_changes":
            return f"{who} made no changes" + (f": {run.summary}" if run.summary else "."), [], []
        case "done" if upstream is not None:  # a fork-mode run
            if run.pr_url:
                head = (
                    f"{who} opened PR #{run.pr_number} on {upstream} "
                    f"({files.removeprefix(', ') + ', ' if files else ''}{took}): {run.pr_url}"
                )
                return head, [f"{upstream} is upstream; its maintainers merge."], said
            why = run.failure or "it wasn't approved"
            head = (
                f"{who} finished in {took}: {run.branch} is on your fork, with no PR on "
                f"{upstream} ({why})."
            )
            return head, [], said
        case "done" if merged and merged_by is not None and merged_by.by == "resonant":
            base = run.base_branch or "the base"  # PER-269: Resonant merged the run's PR
            approved = f" (you approved it in {merged_by.code})" if merged_by.code else ""
            return f"{run.code}'s {and_list(merged)} is merged into {base}{approved}.", [], said
        case "done" if merged:  # PER-268: it merged a PR onto its base, as asked
            base = run.base_branch or "the base"
            approved = (
                f"; you approved it in {merged_by.code}"
                if merged_by is not None and merged_by.code
                else ""
            )
            return f"{who} merged {and_list(merged)} into {base} ({took}{approved}).", [], said
        case "done" if run.repo:  # RX-1: a run that couldn't push; the work is on its branch
            where = _home(run.checkout) if run.checkout else run.repo
            why = run.failure or "it can't push yet"
            head = (
                f"{who} finished in {took}: the work is on local branch {run.branch} in "
                f"{where}; {why}."
            )
            return head, [], said
        case "done":  # a run with no repo: Claude's summary is the answer
            return (
                f"{who} finished in {took}" + (f": {run.summary}" if run.summary else "."),
                [],
                [],
            )
        case "failed" if run.failure and run.failure.startswith(COULDNT_PUSH):
            return f"{who} {run.failure}", [], said  # PER-271: the work is on its branch
        case "failed":
            return f"{who} failed: {run.failure or 'unknown error'}", [], []
        case "cancelled":
            head = f"{who} was cancelled."
            if run.pr_url:
                head += f" Its PR was already open: {run.pr_url}"
            return head, [], []
        case _:
            return None


def end_text(head: str, lines: Sequence[str], limit: int = END_EXTRA_LINES) -> str:
    """The headline and at most ``limit`` further lines (PER-268): the ⚠ lines always stay
    (a direct push is said on its own line), then the rest in order while there is room."""
    room = limit - sum(1 for line in lines if line.startswith("⚠"))
    kept: list[str] = []
    for line in lines:
        if line.startswith("⚠"):
            kept.append(line)
        elif room > 0:
            kept.append(line)
            room -= 1
    return "\n".join([head, *kept])


def reply_text(
    run: Run,
    ci: str | None = None,
    *,
    no_ci: bool = False,
    upstream: str | None = None,
    watching: bool = False,
    merged: Sequence[str] = (),
    merged_by: MergedBy | None = None,
) -> str | None:
    """The message for an ended run, from :func:`end_parts` (None for a state that has
    none), at most a headline and ``END_EXTRA_LINES`` lines."""
    parts = end_parts(run, ci, no_ci=no_ci, upstream=upstream, watching=watching, merged=merged,
                      merged_by=merged_by)  # fmt: skip
    if parts is None:
        return None
    head, lines, said = parts
    return end_text(head, [*lines, *said])


def not_set_up_text(missing: Sequence[str], how: Sequence[str]) -> str:
    return f"The runner isn't set up: {', '.join(missing)}. On the Mini: {'; '.join(how)}."


def _home(path: str) -> str:
    """``~/code/proj`` for a path under the home dir (the reply is read on a phone)."""
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


@dataclass(frozen=True)
class Capabilities:
    """What the runner can do beyond starting a Claude session (RX-1). The Claude credential
    and the CLI are required for any run (``setup_missing``); the GitHub pieces each unlock
    more: the daemon's token (lookups, private clones, the PR checks, merges, new repos, fork
    runs) and the GitHub App (a session token, so the worker can push and open its PR)."""

    github_token: bool
    app_key: bool
    app_config: bool

    @property
    def app(self) -> bool:
        return self.app_key and self.app_config

    @property
    def no_push(self) -> str | None:
        """Why a repo run can't push (a key of ``NO_PUSH_SHORT``), or None. Pushing needs the
        App (the session's token) and the daemon's token (Resonant finds and checks the PR
        with it, and watches the base for direct pushes)."""
        if not self.app:
            return "app"
        if not self.github_token:
            return "token"
        return None

    def notes(self) -> list[str]:
        """One line per missing piece: what it unlocks and how to set it."""
        notes: list[str] = []
        if not self.app:
            how = ([] if self.app_key else [APP_KEY_HOW]) + (
                [] if self.app_config else [APP_CONFIG_HOW]
            )
            notes.append(
                "No push or PRs yet: the GitHub App isn't set up, so workers commit to a "
                f"local branch. On the Mini: {'; '.join(how)}."
            )
        if not self.github_token:
            push = "" if not self.app else "push, PRs, "
            notes.append(
                f"No {push}merges, repo lookups, private clones, new repos or fork runs yet: "
                f"runner/github_token isn't set. On the Mini: {GITHUB_TOKEN_HOW}."
            )
        return notes


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
    result: dict[str, Any] | None = None  # the last one: a live session may have several
    lines: int = 0
    ended: bool = False  # Resonant ended the session after a result (RC-1)

    def feed(self, raw: bytes) -> dict[str, Any] | None:
        """Keep what the runner needs from one line; the parsed message, or None."""
        self.lines += 1
        if not raw.startswith(b"{"):
            return None
        try:
            obj: Any = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        msg: dict[str, Any] = obj  # pyright: ignore[reportUnknownVariableType]
        if msg.get("type") == "system" and isinstance(msg.get("session_id"), str):
            self.session_id = msg["session_id"]
        elif msg.get("type") == "result":
            self.result = msg
        return msg


@dataclass(frozen=True)
class LimitHit:
    reset_at: datetime | None


def reset_clock(text: str, tz: ZoneInfo, now: datetime) -> datetime | None:
    """The next occurrence, after ``now``, of the clock time a limit message names
    (``resets 3pm``) in ``tz``, or None when it names none."""
    m = _RESETS.search(text)
    if m is None:
        return None
    hour, minute = int(m[1]), int(m[2] or 0)
    if not (1 <= hour <= 12 and minute < 60):
        return None
    hour = hour % 12 + (12 if m[3].lower() == "p" else 0)
    local = now.astimezone(tz)
    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= local:
        at += timedelta(days=1)
    return at.astimezone(UTC)


def usage_limit(
    result: Mapping[str, Any], *, tz: ZoneInfo | None = None, now: datetime | None = None
) -> LimitHit | None:
    """Whether a final stream-json ``result`` is a usage or rate limit, and when it resets.

    The shape is Claude Code's, read defensively: an error result whose subtype or text names
    a limit; the reset is an epoch in ``resets_at``/``reset_at`` or after a ``|`` in the text,
    or (with ``tz``) a clock time in it (``resets 3pm``: the next one in ``tz``, after ``now``).
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
    if epoch is None and tz is not None:
        at = reset_clock(text, tz, now or datetime.now(UTC))
        return LimitHit(at)
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
    local: str | None = None  # RX-1: why the run can't push (a ``NO_PUSH_LONG`` key)


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
        path_dirs: Sequence[Path] | None = None,
        max_session_s: int | None = None,
        max_turns: int = MAX_TURNS,
        poll_s: float = 0.25,
        kill_grace_s: float = reaper.GRACE_S,
        disk_interval_s: float = 60.0,
        disk_limit_bytes: int | None = None,
        cache_limit_bytes: int | None = None,
        disk_free: Callable[[Path], int | None] = free_bytes,
        defer_s: float = DEFER_S,
        refresh_s: float = TOKEN_REFRESH_S,
        merge_wait_s: float = MERGE_WAIT_S,
        merge_poll_s: float = MERGE_POLL_S,
        clock: Callable[[], datetime] = utcnow,
        trust: TrustChecker | None = None,
        submit: Callable[[Event], bool] | None = None,
        ask_block_s: float = BLOCK_S,
        home: Path | None = None,
        login_check: Callable[[], Awaitable[bool]] | None = None,
        self_ops: SelfOps | None = None,
        owner: str | None = None,
        self_repo: Path | None = None,
        rc_timeout_s: float = RC_TIMEOUT_S,
        rc_off_timeout_s: float = RC_OFF_TIMEOUT_S,
        end_grace_s: float = END_GRACE_S,
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
        ``ask_block_s`` is how long one ``resonant ask`` call waits. ``merge_wait_s`` and
        ``merge_poll_s`` bound an auto-merge's wait for CI (EM-7).

        GD-1: ``runner.guard`` picks the wall. ``off``: no ``wrap`` and no self-check, the
        session's ``HOME`` is ``home`` (the owner's; tests pass a fake), and with no Claude
        secret the owner's own ``claude`` login is used when ``login_check`` says it works
        (by default ``claude auth status``, cached). ``self_ops`` (``tools.ops.Ops``) is what a
        worker's ``resonant restart|update`` runs, ``owner`` the principal whose ask can
        include that, and ``self_repo`` the daemon's own checkout (``ops.repo_path``),
        registered at start as the repo ``resonant`` with that ``path``.

        RC-1: ``rc_timeout_s`` is how long a session's Remote Control request waits for its
        answer and ``rc_off_timeout_s`` turning it off at the end; ``end_grace_s`` is how long
        a session whose stdin Resonant closed may stay quiet before it is killed."""
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
        self.merge_wait_s = merge_wait_s
        self.merge_poll_s = merge_poll_s
        self._merge_locks: dict[str, asyncio.Lock] = {}
        self._forks: dict[str, str] = {}  # run code -> the owner's fork (fork-mode runs)
        self._tokens: dict[str, list[str]] = {}  # run code -> tokens minted and not yet revoked
        self._spent: dict[str, list[str]] = {}  # ... and revoked ones (still redacted)
        self._expiry: dict[str, datetime] = {}  # token -> when GitHub expires it
        self._unrevoked: dict[str, datetime] = {}  # run code -> latest expiry of a token left live
        self._create_task = create_task or self._insert_task
        self._cancel_task = cancel_task
        self._wrap: Wrap = wrap or _sandbox_wrap
        self._self_check: SelfCheck = self_check or self._cached_self_check
        self._claude_cmd = None if claude_cmd is None else tuple(claude_cmd)
        self._path_dirs = tuple(default_path_dirs() if path_dirs is None else path_dirs)
        self.max_session_s = max_session_s or runner.max_minutes * 60
        self.max_turns = max_turns
        self.poll_s = poll_s
        self.kill_grace_s = kill_grace_s
        self.disk_interval_s = disk_interval_s
        self.disk_limit_bytes = disk_limit_bytes or runner.max_run_dir_mb * 1024 * 1024
        self.cache_limit_bytes = cache_limit_bytes or runner.max_cache_gb * 1024**3
        self.min_free_bytes = runner.min_free_disk_mb * 1024 * 1024
        self._disk_free = disk_free
        self._disk_hit: dict[str, tuple[DiskLimit, int]] = {}  # run code -> the limit and value
        self.cancel_grace_s = min(kill_grace_s, CANCEL_GRACE_S)
        self.rc_timeout_s = rc_timeout_s
        self.rc_off_timeout_s = rc_off_timeout_s
        self.end_grace_s = end_grace_s
        self.defer_s = defer_s
        self._blocked = False  # start() began and hasn't finished: nothing may run
        self.clock = clock
        self.max_step_s: float | None = self.max_session_s + STEP_SLACK_S
        self.state_dir = runner.mirror_dir.parent
        self.transcript_dir = self.state_dir / "transcripts"
        self.last_killed = 0  # runs the last kill switch engage ended
        self._working: set[str] = set()  # codes with a step in flight
        self._live: dict[str, Target] = {}  # codes with a session process
        self._inputs: dict[str, LiveInput] = {}  # ... and its stdin, once the prompt is in (RC-1)
        self._cleanup_locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._unhook: Callable[[], None] | None = None
        self.ctl_root = worker_cli.ctl_root(runner.work_dir)
        self._work_dir_lock: int | None = None  # the fd holding WORK_DIR_LOCK (PER-271)
        self.lock_wait_s = LOCK_WAIT_S
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
            self_ops=self_ops,
            owner=owner,
            on_restart=self._on_self_restart,
            on_self_op=self._on_self_op,
        )
        self.home = home or Path.home()
        self._login_check: Callable[[], Awaitable[bool]] = login_check or LoginCheck(
            self._claude_argv, self.home
        )
        self.self_repo = self_repo
        self.reports = WorkerReports(conn, clock=clock, tz=self.tz)
        self.desk.events.subscribe(self._on_worker_event)

    @property
    def guard(self) -> str:
        """``runner.guard``: ``off`` (the default) or ``thin``."""
        return self.cfg.guard

    def _claude_argv(self) -> list[str] | None:
        """The ``claude`` a session runs (the test seam, or the install), or None."""
        if self._claude_cmd is not None:
            return list(self._claude_cmd)
        found = resolve_tool("claude")
        return None if found is None else [str(found)]

    async def login_ok(self) -> bool:
        """Whether a session can use the owner's own ``claude`` login: only with the guard off
        (under ``thin`` the session has a run-local HOME and no Keychain)."""
        if self.guard != "off":
            return False
        try:
            return await self._login_check()
        except Exception:
            log.exception("claude login check failed")
            return False

    def _on_self_restart(self, code: str) -> None:
        """A restart this run asked for is about to stop its session: the resumed turn says
        why (kept with the run like an undelivered answer, cleared when a turn ends)."""
        run = runs.get(self.conn, code)
        if run is None or not run.active:
            return
        saved = run.resume_prompt
        if saved and RESTART_NOTE in saved:
            return
        runs.update(self.conn, code, resume_prompt=f"{saved}\n\n{RESTART_NOTE}" if saved
                    else RESTART_NOTE)  # fmt: skip

    async def _self_repo_seed(self) -> dict[str, RunnerRepo]:
        """``runner.repos`` plus the daemon's own checkout (``self_repo``), so "fix X in
        resonant" works in a worktree of it. Guard off only: ``thin`` write-protects that
        checkout. A ``runner.repos`` entry or registry row for its origin keeps its name,
        source and settings and only gets a missing ``path`` (never renamed). An origin the
        registry doesn't know is seeded as ``resonant`` when that name is free, so a repo
        the owner removed comes back at the next start."""
        seeds = dict(self.cfg.repos)
        path = self.self_repo
        if self.guard != "off" or path is None:
            return seeds
        if not await asyncio.to_thread(os.path.isdir, path / ".git"):
            return seeds
        slug = await self.workspace.origin_slug(path)
        if slug is None:
            return seeds
        for name, entry in seeds.items():
            if entry.slug.lower() == slug.lower():
                if entry.path is None:
                    seeds[name] = entry.model_copy(update={"path": path})
                return seeds
        known = repo_store.find(self.conn, slug)  # slugs compare case-insensitively
        if known is not None:
            if known.path is None:
                with atomic(self.conn):
                    repo_store.set_path(self.conn, known.slug, path)
            return seeds
        if SELF_REPO_NAME not in seeds and repo_store.find(self.conn, SELF_REPO_NAME) is None:
            seeds[SELF_REPO_NAME] = RunnerRepo(remote=f"github.com/{slug}", path=path)
        return seeds

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
        # runner.repos is only a seed: import it into the registry (idempotent), with the
        # daemon's own checkout as the repo "resonant" (GD-1).
        try:
            seeds = await self._self_repo_seed()
        except Exception:
            log.exception("Resonant's own checkout couldn't be read; not registered")
            seeds = dict(self.cfg.repos)
        with atomic(self.conn):
            imported = repo_store.import_config(self.conn, seeds)
        for name, reason in imported.skipped:
            log.warning("runner.repos entry %s not imported: %s", name, reason)
        for old, new in imported.renamed:  # the mirror follows the repo's name
            async with self.workspace.mirror_lock(old):
                await asyncio.to_thread(self._move_mirror, old, new)
        reaped = 0
        if not await asyncio.to_thread(self._own_work_dir):
            # PER-271: another live process (the daemon) owns these run dirs. Its sessions'
            # processes, credentials and sockets are not ours to end or wipe.
            log.warning(
                "runner.work_dir %s is owned by another live process; not sweeping its runs",
                self.cfg.work_dir,
            )
            with atomic(self.conn):
                audit(self.conn, "run.startup_sweep_skipped", work_dir=str(self.cfg.work_dir))
            if self._unhook is None:
                self._unhook = killswitch.add_pause_hook(self._on_pause)
            return reaped
        # A session killed by a restart someone asked for (/restart, /update, a worker's
        # `resonant restart`) isn't a crash of its run: no attempt is spent on it.
        planned = take_planned(self.conn, self.clock())
        for run in runs.active(self.conn):
            has_process = run.pgid is not None or run.leader_pid is not None
            if not (has_process or run.status == "running"):
                continue
            target = self._target(run)
            if await reaper.reap(target, grace_s=self.kill_grace_s):
                reaped += 1
            if has_process:
                runs.clear_process(self.conn, run.code, crashed=not planned)
        for run in runs.with_process(self.conn):  # a run that ended with a process recorded
            if not run.active:
                await reaper.reap(self._target(run), grace_s=self.kill_grace_s)
                runs.clear_process(self.conn, run.code)
        await self._sweep_credentials()
        await asyncio.to_thread(wipe_secret_dirs, self.cfg.secrets_dir)
        self.desk.startup()  # no CLI call survived the restart
        await asyncio.to_thread(self._wipe_ctl_dirs)
        await self._reconcile()
        for run in runs.active(self.conn):  # a check closed by the restart is news for it
            if run.status == "waiting" and run.wait_reason == "question":
                self._wake(run, f"startup:{uuid.uuid4().hex}")
        if self._unhook is None:
            self._unhook = killswitch.add_pause_hook(self._on_pause)
        return reaped

    def _own_work_dir(self) -> bool:
        """Take ``<work_dir>/.daemon.lock`` (``flock``, held until ``aclose`` or exit) for
        this runner, or False when another process holds it: only the runner that owns the
        run dirs may reap their sessions and wipe their credentials and ctl sockets at
        startup (PER-271: a test or a second runner must never sweep the live daemon's)."""
        if self._work_dir_lock is not None:
            return True
        self.cfg.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.cfg.work_dir / WORK_DIR_LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
                     0o600)  # fmt: skip
        # A predecessor still exiting (/restart, /update, a kickstart overlap) holds it for
        # a moment: wait that long before treating the holder as a live daemon.
        deadline = time.monotonic() + self.lock_wait_s
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        os.close(fd)
                        return False
                    time.sleep(LOCK_POLL_S)
        except BaseException:
            os.close(fd)
            raise
        self._work_dir_lock = fd
        with contextlib.suppress(OSError):  # who holds it, for a person looking
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        return True

    def _release_work_dir(self) -> None:
        fd, self._work_dir_lock = self._work_dir_lock, None
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

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
        try:
            if self._unhook is not None:
                self._unhook()
                self._unhook = None
            await self.desk.aclose()
            tasks = list(self._background)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._release_work_dir()

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
        """``The runner isn't set up: …`` naming exactly what is missing for any run (the
        Claude credential and the claude CLI), or None. The GitHub pieces aren't blockers:
        ``capability_notes`` says what each unlocks."""
        missing, how = await self.setup_gaps()
        return not_set_up_text(missing, how) if missing else None

    async def setup_gaps(self) -> tuple[list[str], list[str]]:
        """What the runner still needs for any run and how to fix each; both empty when it is
        set up. Secrets are checked by name only (no value is read)."""
        missing: list[str] = []
        how: list[str] = []
        try:
            refs = await missing_runner_secrets(self.secrets)
        except SecretError as e:
            missing.append(f"the Keychain can't be read ({e.reason})")
            how.append("unlock the login keychain")
        else:
            installed = self._claude_argv() is not None
            if refs == [CLAUDE_MISSING] and installed and await self.login_ok():
                refs = []  # GD-1: the owner's own claude login is enough with the guard off
            for ref in refs:
                missing.append(f"secret {ref}")
                cmd = f"resonant secret set {ref.split(' ', 1)[0]}"
                if ref == CLAUDE_MISSING and self.guard == "off":
                    missing[-1] = f"a Claude login (or secret {ref})"
                    cmd = f"{LOGIN_HOW}, or {cmd}"
                how.append(cmd)
        if self._claude_argv() is None:
            missing.append("the claude CLI isn't installed")
            how.append("install Claude Code")
        return missing, how

    async def capabilities(self) -> Capabilities:
        """Which GitHub pieces are set up (names only; no value is read). A Keychain that
        can't answer counts as not set up."""

        async def have(ref: str) -> bool:
            try:
                return await self.secrets.exists(ref)
            except SecretError:
                return False

        return Capabilities(
            github_token=await have(GITHUB_TOKEN_REF),
            app_key=await have(GITHUB_APP_KEY_REF),
            app_config=self.app is not None,
        )

    async def capability_notes(self) -> list[str]:
        """What workers can't do yet and how to unlock it (``/runs``); empty when all set."""
        return (await self.capabilities()).notes()

    async def start_run(
        self,
        repo: str | None,
        ask: str,
        principal: str,
        *,
        merge: bool = False,
        report: Report = "digest",
    ) -> Started | Refused:
        """Create a run and its task. ``repo`` is free text (a name, ``owner/name``, a URL):
        the resolver turns it into a registered repo, registering one it hasn't seen. No repo
        (None or blank) is a run in a scratch dir, with no GitHub token and no PR. Refuses
        (with a sentence for the requester) when the runner isn't set up, the repo is
        ambiguous or unknown, a fork-mode repo can't be worked on here (no ``public_repo``
        token, or no way to ask the owner), or Resonant is paused. Two runs on one repo go at
        once, each in its own worktree.

        ``merge``: the ask included merging. A push repo's run then merges its own PR once CI
        is green and the checks are clean, if the trust rules allow a merge there
        (``merge_decision``); it is kept in the run's task checkpoint. Ignored for a fork
        repo, which is never merged by Resonant. ``report="now"`` texts the run's end right away
        (``/run``, or the owner asked to hear); by default it goes to the digest
        (``runners/reports.py``).

        Graduated capability (RX-1): only the Claude credential and CLI are required. A repo
        run that can't push (no GitHub App, or no daemon token) still starts: its session gets
        no GitHub credentials and the run ends ``done`` with its local branch (``local`` in
        the task checkpoint), and the start reply says so up front. Without the daemon's
        token a repo needs a checkout already, or must be public (cloned anonymously)."""
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
        resolution: Resolved | None = None
        if repo is not None and repo.strip():
            resolved = await self._resolve(repo)
            if isinstance(resolved, Refused):
                return resolved
            resolution, found = resolved, resolved.repo
        caps = await self.capabilities()
        fork = found is not None and found.mode == "fork"
        if fork and (refusal := await self._fork_refusal(caps)) is not None:
            return refusal
        local = None if found is None else caps.no_push
        if (
            found is not None
            and not caps.github_token
            and not await self.workspace.can_start_tokenless(found.name)
        ):
            return Refused(self.workspace.no_clone_text(found))
        auto_merge = merge and found is not None and found.mode == "push" and local is None
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
        self.reports.set_report(run.code, report)
        spec = NewTask(
            kind=TASK_KIND,
            runner=RUNNER_NAME,
            priority=PRIORITY_OWNER,
            principal=principal,
            checkpoint={
                "run": run.code,
                "title": f"run {run.code} · {repo or 'no repo'}",
                **({"merge": True} if auto_merge else {}),
                **({"local": local} if local is not None else {}),
            },
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
        note = limits = None
        if local is not None:
            then = "so no PR and no merge" if merge else "the work stays on a local branch"
            limits = f"Heads up: no push yet ({NO_PUSH_SHORT[local]}); {then}."
        elif found.mode == "fork":
            note = (
                f"{found.slug} is upstream: the work goes to your fork, and I'll ask before the PR."
            )
        elif auto_merge:
            note = "I'll merge it when CI is green, unless a rule says to ask."
        assert resolution is not None
        warning = None
        if found.mode != "fork" and local is None:
            # The resolver's warning (a new repo's base) is the same fact said shorter; the
            # live check covers every run, so it is only the fallback.
            warning = await self._base_warning(found)
            if warning is None and resolution.warning:
                warning = f"{resolution.warning} — the session could push to it directly."
        return Started(
            run.code,
            repo,
            max(1, self.max_session_s // 60),
            found.slug if resolution.new else None,
            warning,
            note,
            resolution.via if resolution.new else None,
            limits,
        )

    async def _fork_refusal(self, caps: Capabilities) -> Refused | None:
        """Why a fork-mode run can't start here, or None. The ``public_repo`` token is
        optional for the runner as a whole, so it is only asked for here; a fork run also
        needs the daemon's token (the fork's checks) and the App (pushing to the fork)."""
        if not caps.github_token:
            return Refused(needs_github_token("A fork run"))
        if not caps.app:
            return Refused(
                "A fork run pushes to your fork with the GitHub App, which isn't set up yet. "
                f"On the Mini: {APP_KEY_HOW if not caps.app_key else APP_CONFIG_HOW}."
            )
        if self.desk.trust is None:
            return Refused("A fork run asks you before its upstream PR, and asking isn't set up.")
        try:
            have = await self.secrets.exists(GITHUB_PUBLIC_TOKEN_REF)
        except SecretError:
            have = False
        return None if have else Refused(FORK_NEEDS_PUBLIC_REPO)

    async def _base_warning(self, repo: repo_store.Repo) -> str | None:
        """A sentence for the start reply when the base branch is not protected: the session's
        token can push to it, and a direct push skips the checks a merge runs."""
        try:
            secrets = await load_runner_secrets(self.secrets, claude=False)
            protected = await self.workspace.base_protected(repo.name, secrets)
        except Exception:  # the start reply must go out whatever GitHub says
            log.warning("base protection unknown", extra={"repo": repo.name})
            return None
        if protected is False:
            return f"{repo.base} isn't protected — the session could push to it directly."
        return None

    async def _resolve(self, text: str) -> Resolved | Refused:
        """The registered repo for the requester's words (``new`` when this call registered
        it, ``via`` the extension whose link named it, ``warning`` about its base)."""
        try:
            secrets = await load_runner_secrets(self.secrets, claude=False)
            # No daemon token: only the registry answers (RX-1); a lookup says what it needs.
            github = OfflineGitHub() if secrets.github_token is None else self._github(secrets)
            resolution = await self.resolver.resolve(text, github)
        except SecretError:
            return Refused((await self.setup_missing()) or "The runner's secrets can't be read.")
        except NeedsToken:
            return Refused(needs_github_token(f"Looking up {text[:60]!r} on GitHub"))
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
        return resolution

    def get_run(self, code: str) -> Run | None:
        return runs.get(self.conn, code)

    def list_runs(self, limit: int = 20) -> list[Run]:
        """Active runs first, then the most recent."""
        return runs.recent(self.conn, limit)

    def run_activity(self, code: str, minutes: int = activity.WINDOW_MIN) -> dict[str, Any] | None:
        """What run ``code`` is doing, from its transcript's tail (TR-1, ``run_activity``):
        a code-built ``headline``, the current ``step``, the last few tool ``calls``, its
        ``last_text``, ``detected``/``did`` side effects and seconds since its last event.
        Deterministic (no model); None for an unknown run. Reads at most
        ``activity.TAIL_BYTES`` of the file."""
        run = runs.get(self.conn, code)
        if run is None:
            return None
        minutes = min(max(int(minutes), 1), activity.WINDOW_MAX)
        path = self.transcript_dir / f"{run.code}.jsonl"
        now = self.clock()
        events = activity.read_tail(path)
        act = (
            activity.summarize(
                events, now=now, window_min=minutes, quiet_s=activity.quiet_for(path, now)
            )
            if events
            else None
        )
        step = act.step if act is not None else "no transcript yet"
        if run.status == "queued":
            step = "queued, not started"
        elif run.status == "waiting" and run.wait_reason == "question":
            step = "waiting for your answer"
        elif run.status == "waiting":
            step = "waiting for the usage limit to reset"
        elif not run.active:
            step = f"ended ({run.status})"
        did = shown_did(self.conn, run.code)
        return {
            "code": run.code,
            "repo": run.repo or None,
            "status": run.status,
            "elapsed_s": round(run.session_seconds),
            "headline": activity.headline(run.code, run.repo or None, step, act, minutes),
            "step": step,
            "window_min": minutes,
            "calls": list(act.calls) if act is not None else [],
            "calls_in_window": act.calls_in_window if act is not None else None,
            "last_text": act.last_text if act is not None else None,
            "quiet_s": round(act.quiet_s) if act is not None and act.quiet_s is not None else None,
            "did": did,
            "last_note": activity.clip(run.last_note, 120) if run.last_note else None,
            # RC-1: the live session in the Claude app, while it runs
            "remote_url": run.remote_url if run.status == "running" else None,
        }

    async def cancel_run(self, code: str, *, reason: str = "cancelled") -> Cancelled | Refused:
        """End an active run: its processes, its dir, its task. Never publishes."""
        run = runs.get(self.conn, code)
        if run is None:
            return Refused(f"No run {code}.")
        if not run.active:
            if run.status == "ready" and self._stop_watch(run):
                return Refused(
                    f"{run.code} won't be merged automatically; its PR stays open. "
                    f"/merge {run.code} to merge it."
                )
            return Refused(f"{run.code} isn't running ({run.status}).")
        if not self._cancel(run, reason):
            return Refused(f"{run.code} isn't running.")
        if run.code not in self._working:  # queued or waiting: no step will clean up
            await self._finish_cancelled(run.code)
        return Cancelled(run.code)

    def _stop_watch(self, run: Run) -> bool:
        """``/cancel`` on a ready run that is waiting to merge itself: the watch ends (its
        task is cancelled), the PR stays. False when nothing was watching."""
        task = task_store.get_task(self.conn, run.task_id) if run.task_id else None
        if task is None or task.status != "waiting" or not task.checkpoint.get("merge"):
            return False
        if self._cancel_task is not None:
            self._cancel_task(task.id)
        else:
            with atomic(self.conn):
                task_store.set_status(self.conn, task.id, "cancelled", expect=("waiting",))
        with atomic(self.conn):
            audit(self.conn, "run.auto_merge_stopped", run=run.code)
        return True

    async def merge_run(self, code: str) -> Merged | MergeQueued | Refused:
        """Squash-merge a ready run's PR (``--match-head-commit``, CI must be green). Whether
        to ask first is the caller's (the gate: ``/merge`` is explicit; chosen from chat it
        follows ``merge_decision``); the flagged paths and dependency files are in the
        result. A fork-mode repo's PR is never merged here.

        This is the owner's merge (``merge_run`` is an owner-only builtin, run by ``/merge``
        or by a chat call the owner allowed), so it is looser than the auto-merge in two ways:
        CI still running is not a refusal but a wait (``MergeQueued``: the run's merge watch,
        the auto-merge's bounded wait, merges it once CI is green and reports it the way an
        auto-merge does), and a repo with no CI at all merges with a warning in the reply.
        Red CI and the checks still refuse it."""
        run = runs.get(self.conn, code)
        if run is None:
            return Refused(f"No run {code}.")
        repo = repo_store.find(self.conn, run.repo) if run.repo else None
        if repo is not None and repo.mode == "fork":
            return Refused(upstream_merges(repo.slug))
        if not _mergeable(run):
            return Refused(f"{run.code} isn't ready to merge ({run.status}).")
        try:
            secrets = await load_runner_secrets(self.secrets, claude=False)
        except SecretError:
            return Refused("The runner's secrets can't be read.")
        if secrets.github_token is None:
            return Refused(needs_github_token(f"Merging {run.code}"))
        result = await self._merge(run.code, secrets, by="owner", allow_no_ci=True, wait_ci=True)
        if isinstance(result, MergeQueued):
            self._watch_merge(self._must_get(run.code))
        return result

    def _watch_merge(self, run: Run) -> None:
        """Start the owner's merge watch for a ready run whose CI is still running: a task
        (checkpoint ``merge``, ``watch``, ``by: owner``, ``since``) that ``_auto_merge`` steps
        like an auto-merge's, with the owner's looser rules (``_owner_merge_blocker``) and
        ``merge_wait_s`` counted from now. An auto-merge watch already waiting on the run is
        replaced; an owner's watch already waiting is kept."""
        task = task_store.get_task(self.conn, run.task_id) if run.task_id else None
        # A watch caught mid-step (running) is replaced too: Loop.cancel cancels its step, else
        # it would go on beside the owner's watch.
        live = task is not None and task.status in ("queued", "running", "waiting")
        if task is not None and live and task.checkpoint.get("merge"):
            if task.checkpoint.get("by") == "owner":
                return
            if self._cancel_task is not None:
                self._cancel_task(task.id)
            else:
                with atomic(self.conn):
                    task_store.set_status(
                        self.conn, task.id, "cancelled", expect=("queued", "running", "waiting")
                    )
        spec = NewTask(
            kind=TASK_KIND,
            runner=RUNNER_NAME,
            priority=PRIORITY_OWNER,
            principal=run.principal,
            checkpoint={
                "run": run.code,
                "title": f"merge {run.code} · {run.repo}",
                "merge": True,
                "watch": True,
                "by": "owner",
                "since": self.clock().isoformat(),
            },
        )
        task_id = self._create_task(spec)
        runs.update(self.conn, run.code, task_id=task_id)
        with atomic(self.conn):
            audit(self.conn, "run.merge_watch", run=run.code, pr=run.pr_number, task_id=task_id)

    async def _merge(
        self,
        code: str,
        secrets: RunnerSecrets,
        *,
        by: str,
        allow_flagged: bool = True,
        allow_no_ci: bool = False,
        wait_ci: bool = False,
        replied: bool | None = None,
    ) -> Merged | MergeQueued | Refused:
        """The one merge path of both ``merge_run`` and the auto-merge, one at a time per run
        (the row is read again under the lock, so two merges of one PR can't both go).
        ``Workspace.merge`` fetches the PR head and runs the checks first (D10), then CI,
        then ``--match-head-commit`` against the head the run recorded. ``allow_no_ci``: the
        owner's merge (a repo with no CI merges, with a warning); ``wait_ci``: CI still
        running is ``MergeQueued``, not a refusal. ``replied``: the owner already got the
        reply (default: ``by == "owner"``), so the merged digest line is marked told;
        otherwise it goes by the ``merged`` level."""
        async with self._merge_locks.setdefault(code, asyncio.Lock()):
            run = runs.get(self.conn, code)
            if run is None or not _mergeable(run):
                status = "gone" if run is None else run.status
                return Refused(f"{code} isn't ready to merge ({status}).")
            assert run.pr_number is not None and run.head_sha is not None and run.branch
            try:
                done = await self.workspace.merge(
                    run.repo, run.pr_number, run.head_sha, secrets, branch=run.branch,
                    allow_flagged=allow_flagged, allow_no_ci=allow_no_ci,
                )  # fmt: skip
            except CiPending as e:
                if wait_ci:
                    return MergeQueued(run.code, run.pr_number)
                return Refused(f"{run.code} not merged: {Redactor(secrets.values()).text(str(e))}")
            except (MergeRefused, RunnerError, GitError) as e:
                reason = e.message if isinstance(e, GitError) else str(e)
                return Refused(f"{run.code} not merged: {Redactor(secrets.values()).text(reason)}")
            checked = done.checks
            with atomic(self.conn):
                runs.update(self.conn, run.code, status="merged", flagged_paths=checked.flagged)
                audit(self.conn, "run.merged", run=run.code, pr=run.pr_number, by=by,
                      no_ci=done.no_ci)  # fmt: skip
                merged = self._pr_target(run, run.pr_number)
                if merged is not None:  # PER-269: the open asks for it are moot
                    approvals_store.obsolete_for_target(self.conn, merged, self.clock())
        told = by == "owner" if replied is None else replied
        await self._report_merged(self._must_get(run.code), by_owner=told, deps=checked.deps)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(delete_run_dir, self.cfg.work_dir, run.code)
        await self._remove_workspace(run)
        return Merged(
            run.code, run.pr_number, checked.flagged, checked.deps,
            no_ci=run.repo if done.no_ci else None,
        )  # fmt: skip

    # -- the worker CLI's owner side (EM-5's API) --

    @property
    def events(self) -> WorkerEvents:
        """Worker events (``question``, ``note``, ``check``): ``events.subscribe(fn)``."""
        return self.desk.events

    def open_questions(self) -> list[OpenQuestion]:
        """Open ``resonant ask`` questions of active runs, oldest first."""
        return self.desk.open_questions()

    def answer_question(
        self, code: str, text: str, *, question_id: int | None = None
    ) -> Relayed | Refused:
        """The owner's answer to run ``code``'s open question (``question_id``: exactly that
        one). A CLI call still waiting gets it at once; else the session is resumed with it
        (``claude --resume``)."""
        try:
            return self.desk.answer_question(code.upper(), text, question_id=question_id)
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
        """Something arrived for the run: written into its live session if it has one (RC-1),
        and an event for its task, which requeues it if it is waiting (and, if a step is in
        flight, once that step returns ``Wait``)."""
        if run.code in self._inputs:
            self._spawn(self._push_live(run.code))
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

    async def _on_worker_event(self, event: WorkerEvent) -> None:
        """EM-6: notes by urgency. A plain note only updates the run's digest line (the
        desk's ``on_note``); an urgent one is texted now unless a rule holds it for the
        digest. Questions and checks are texted by the relay (EM-5) and the trust prompt
        (EM-4), never here."""
        if event.kind != "note":
            return
        run = runs.get(self.conn, event.run_code)
        if run is None or run.status != "running":
            return
        kind = "urgent" if event.urgent else "note"
        level = self.reports.level(run, kind)
        self.reports.audit_report(run, kind, level)
        if level != "now" or (text := self.reports.note_text(run)) is None:
            return
        item = self.reports.running(run, _bucket(run.session_seconds))
        key = f"run:{run.code}:note:{uuid.uuid4().hex}"  # each urgent note is its own news
        try:
            await self.channel.send(run.principal, text, dedupe_key=key)
        except ChannelError as e:  # the digest line still carries the note
            log.warning("note not texted", extra={"run": run.code, "reason": str(e)})
            return
        self.reports.told(item)

    async def _on_self_op(self, code: str, op: str, what: str) -> None:
        """A worker's restart or update of Resonant went through (already on the run's
        ``did:`` list): its line is news for the digest, and it is texted now unless a
        ``notify_digest`` rule on ``restart``/``update`` holds it (``runners/reports.py``)."""
        run = runs.get(self.conn, code)
        if run is None or run.status != "running":
            return
        item = self.reports.running(run, _bucket(run.session_seconds))
        level = self.reports.level(run, op)
        self.reports.audit_report(run, op, level)
        if level != "now":
            return
        key = f"run:{run.code}:{op}:{uuid.uuid4().hex}"
        try:
            await self.channel.send(run.principal, self.reports.self_op_text(run, what),
                                    dedupe_key=key)  # fmt: skip
        except ChannelError as e:  # the digest line still carries it
            log.warning("self op not texted", extra={"run": run.code, "reason": str(e)})
            return
        self.reports.told(item)

    # -- side effects read from the session's commands (TR-1) --

    def _side_effect_context(self, run: Run, p: Prepared) -> RunContext:
        """What tells the run's own pushes apart: its repo's slug, base and branch (from the
        prepared workspace, which the run row may not have caught up with). Never raises."""
        try:
            found = repo_store.find(self.conn, run.repo) if run.repo else None
        except Exception:
            log.exception("side effect context", extra={"run": run.code})
            found = None
        repo = found.slug if found is not None else (run.repo or None)
        return RunContext(
            repo=repo,
            base=p.base_branch or run.base_branch or None,
            branch=p.branch or run.branch or None,
        )

    def _detect(self, run: Run, msg: dict[str, Any], ctx: RunContext) -> None:
        """Record the outside actions in one assistant message's tool calls on the run's
        ``did:`` list (``reports.record_detected``); a high-impact one is texted per the trust
        rules. Never raises: the stream must keep flowing."""
        try:
            found = detect(msg, ctx)
        except Exception:
            log.exception("side effect detection failed", extra={"run": run.code})
            return
        for effect in found:
            try:
                new = record_detected(self.conn, run.code, effect)
            except Exception:
                log.exception("side effect not recorded", extra={"run": run.code})
                continue
            if not new:
                continue
            with contextlib.suppress(Exception), atomic(self.conn):
                audit(self.conn, "run.side_effect", run=run.code, kind=effect.kind,
                      high=effect.high)  # fmt: skip
            self._spawn(self._on_detected(run.code, effect))

    async def _on_detected(self, code: str, effect: SideEffect) -> None:
        """A new detected side effect: the run's digest line is news again; a high-impact one
        (base push, publish, delete) is also texted now unless a ``notify_digest`` rule on its
        kind holds it, or the run already used its immediate texts this hour."""
        try:
            run = runs.get(self.conn, code)
            if run is None or run.status != "running":
                return
            item = self.reports.running(run, _bucket(run.session_seconds))
            if not effect.high or effect.kind in HELD_KINDS:  # a merge: said in the end text
                return
            level = self.reports.level(run, "side_effect", effect.kind)
            self.reports.audit_report(run, "side_effect", level)
            if level != "now" or (text := self.reports.detected_text(run, effect.what)) is None:
                return
        except Exception:  # the digest line is best effort; the stream goes on
            log.exception("side effect report failed", extra={"run": code})
            return
        key = f"run:{run.code}:seen:{uuid.uuid4().hex}"
        try:
            await self.channel.send(run.principal, text, dedupe_key=key)
        except ChannelError as e:  # the digest line still lists it
            log.warning("side effect not texted", extra={"run": run.code, "reason": str(e)})
            return
        self.reports.told(item)

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
            if run.status == "ready" and task.checkpoint.get("merge"):
                # watching, or a retry before the first look (the reply is deduped)
                return await self._auto_merge(task, run, first=not task.checkpoint.get("watch"))
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
        # A busy slot or repo lock is a `resource` wait (PER-259), not a usage reset; the run's
        # own status (queued) is what /runs shows.
        log.info("run deferred", extra={"task": task.id, "why": why})
        return Wait(
            "resource", dict(task.checkpoint), self.clock() + timedelta(seconds=self.defer_s)
        )

    async def _drive(self, task: Task, run: Run, ctx: StepContext) -> StepOutcome:
        try:
            secrets = await load_runner_secrets(self.secrets, claude=False)
        except SecretError as e:
            return await self._fail(run, f"the runner isn't set up ({e})", Redactor())
        login = not (secrets.claude_token or secrets.anthropic_key) and await self.login_ok()
        if auth_source(secrets, login_ok=login) is None and run.step in ("prepare", "session"):
            # no token, no key and no working login: nothing could start a session
            why = "no Claude token, API key or working claude login"
            return await self._fail(run, f"the runner isn't set up ({why})", Redactor())
        local = task.checkpoint.get("local")
        if not (isinstance(local, str) and local in NO_PUSH_LONG) or run.scratch:
            local = None
        w = _Work(task, ctx, secrets, Redactor(secrets.values()), local)
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
            fork = not run.scratch and self._repo_of(run).mode == "fork"
            if w.local is not None and run.step in ("harvest", "publish"):
                run = await self._collect_local(w, run, p)
            elif run.step == "harvest" and fork:
                run = await self._collect_fork(w, run, p)
            if run.step == "publish" and fork:
                outcome = await self._publish(w, run, p)
                if outcome is not None:
                    return outcome
                run = self._must_get(run.code)
            elif run.step in ("harvest", "publish") and not run.scratch:
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
        if run.status == "ready" and task.checkpoint.get("merge"):
            return await self._auto_merge(task, run, first=True)
        return await self._conclude(run)

    # -- steps --

    async def _prepare(self, w: _Work, run: Run) -> Run:
        self._check_stop(run.code)
        if not run.scratch and w.local is None and w.secrets.github_token is None:
            # started to push, and the token went since: never prepare a push run tokenless
            raise RunnerError(
                needs_github_token("Pushing") + " Start it again to have it commit locally."
            )
        if run.scratch:
            p = await self.workspace.prepare_scratch(run.code)
        else:
            fork = None
            if self._repo_of(run).mode == "fork":
                fork = await self._fork_of(run, w.secrets)
            p = await self.workspace.prepare(
                run.code, run.repo, run.ask, github_token=w.secrets.github_token, fork=fork
            )
        runs.update(
            self.conn,
            run.code,
            workspace=str(p.workspace),
            checkout=None if p.checkout is None else str(p.checkout),
        )
        if p.from_checkout:  # said in the end reply; kept across restarts
            w.task.checkpoint["base_from"] = "checkout"
            w.ctx.save(checkpoint=dict(w.task.checkpoint))
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
        (params.run_dir / NO_GH_DIR).mkdir(mode=0o700, exist_ok=True)
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
        thin = self.guard == "thin"
        # With the guard off nothing is protected (the daemon's own checkout may be the
        # repo, GD-1); the params then only lay out the run dir, its bin/ and the caches.
        return SandboxParams(
            run_dir=p.run_dir,
            work_dir=self.cfg.work_dir,
            cache_dir=self.cfg.cache_dir,
            secrets_dir=self.cfg.secrets_dir,
            home=Path.home() if thin else self.home,
            path_dirs=self._path_dirs,
            deny_exec=default_deny_exec() if thin else (),
            deny_read=tuple(self.cfg.deny_read) if thin else (),
            protect=None if thin else (),
            write_paths=tuple(writes),
            ctl_root=self.ctl_root,
            ctl_dir=self.ctl_root / p.code,
        )

    def _cached_self_check(self, params: SandboxParams) -> tuple[bool, str]:
        return cached_self_check(params, self.state_dir / CHECK_CACHE)

    def _env(
        self,
        params: SandboxParams,
        marker: str,
        secrets: RunnerSecrets | None,
        p: Prepared,
        *,
        local: bool = False,
    ) -> dict[str, str]:
        """The session's environment, from scratch either way, with at most one Claude
        credential: the token, else the API key, else none (the owner's own login, guard off
        only; ``runner.claude_auth``). Guard ``thin``: ``HOME`` and ``CLAUDE_CONFIG_DIR`` in
        the run. Guard ``off``: the owner's real ``HOME`` (their ``~/.claude``), the install
        dirs on ``PATH``. The run's GitHub token is not in it: ``gh`` finds it by
        ``GH_CONFIG_DIR`` (a path) and git through the credential helper named in
        ``GIT_CONFIG_*`` (a path; nothing is written to the checkout's config). A run with no
        repo has neither, and neither has a ``local`` one (RX-1: it can't push yet), which
        still gets the exclude file."""
        token = None if secrets is None else secrets.claude_token
        key = None if secrets is None or token else secrets.anthropic_key
        if self.guard == "thin":
            env = scratch_env(params, path_dirs=params.path_dirs, claude_token=token,
                              anthropic_key=key)  # fmt: skip
        else:
            env = owner_env(params, home=self.home, path_dirs=params.path_dirs,
                            claude_token=token, anthropic_key=key)  # fmt: skip
        env[MARKER_ENV] = marker  # the run's session id: unique, never reused
        if p.scratch or local:
            # No GitHub credentials: with the real HOME, gh would otherwise find the owner's
            # own login (~/.config/gh). An empty config dir keeps RX-1's "can't push yet".
            env["GH_CONFIG_DIR"] = str(params.run_dir / NO_GH_DIR)
            env.update(session_git_env(None, None if p.scratch else p.exclude_file))
        else:
            creds = params.run_dir / CREDS_DIR
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
        self,
        run: Run,
        p: Prepared,
        params: SandboxParams,
        *,
        api_billing: bool,
        local: str | None = None,
    ) -> list[str]:
        head = list(self._claude_cmd or [str(params.run_dir / "bin" / "claude")])
        argv = [
            *head,
            "-p",
            "--resume" if run.sessions else "--session-id",
            run.session_id,
            "-n",
            run.code,
            # RC-1: the prompt is a user line and stdin stays open, so Resonant can write more
            # into the live session and decide when it ends (runners/live.py)
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
        ]
        if self.guard == "thin":
            # Project settings only and no MCP servers, and Claude Code's own sandbox off:
            # Seatbelt can't nest inside ours. With the guard off (GD-1) the owner's setup
            # loads as at the keyboard: user, project and local settings, ~/.claude skills,
            # the user CLAUDE.md and MCP servers, untouched by any --settings override.
            argv += [
                "--strict-mcp-config",
                "--setting-sources",
                "project",
                "--settings",
                json.dumps({"sandbox": {"enabled": False}}),
            ]
        argv += [
            "--append-system-prompt",
            self._prompt(run, p, local=local)
            + WORKER_PROMPT
            + (RESTART_PROMPT if self.desk.self_ops is not None else ""),
            "--max-turns",
            str(self.max_turns),
        ]
        if api_billing and run.max_budget_usd is not None:  # a subscription has no dollar budget
            argv += ["--max-budget-usd", f"{run.max_budget_usd:g}"]
        return argv

    def _prompt(self, run: Run, p: Prepared, *, local: str | None = None) -> str:
        """The session's system prompt: scratch, a push repo's (it drives its own PR, through
        the surf skills for a code change when the owner's setup is loaded), a
        fork's (it pushes to the fork and stops; Resonant opens the upstream PR) or a run's
        that can't push yet (``local``, RX-1: it commits and stops)."""
        if p.scratch:
            return SCRATCH_PROMPT
        repo = self._repo_of(run)
        own = ""
        if (
            self.guard == "off"
            and self.self_repo is not None
            and p.checkout is not None
            and _same(p.checkout, self.self_repo)
        ):
            own = SELF_REPO_PROMPT.format(checkout=_home(str(self.self_repo)))
        if local is not None:
            return (
                LOCAL_PROMPT.format(
                    repo=repo.slug, branch=p.branch, base=p.base_branch, why=NO_PUSH_LONG[local]
                )
                + own
            )
        if repo.mode == "fork":
            fork = self._forks.get(run.code)
            if fork is None:  # set when the session's token was minted
                raise RunnerError("the fork isn't known yet")
            return FORK_PROMPT.format(
                fork=fork, upstream=repo.slug, branch=p.branch, base=p.base_branch
            )
        return (
            RUNNER_PROMPT.format(
                repo=repo.slug, branch=p.branch, base=p.base_branch, attempts=MAX_FIX_ATTEMPTS
            )
            # the skills load only with the owner's setup (GD-1); thin runs never see them
            + (SURF_PROMPT.format(branch=p.branch) if self.guard == "off" else "")
            + own
        )

    async def _session(self, w: _Work, run: Run, p: Prepared) -> StepOutcome | None:
        """Run Claude until it exits, a limit is hit or the run is stopped. None means it
        finished cleanly and the run moves on to the PR lookup (step ``harvest``)."""
        if run.attempts >= MAX_ATTEMPTS:
            return await self._fail(
                run, f"the daemon crashed {run.attempts} times during the session "
                f"(work kept: /runs {run.code})", w.redactor)  # fmt: skip
        if run.session_seconds >= run.max_seconds:
            return await self._timed_out(run, w)
        params = self._params(p)
        ok, detail = True, ""
        if self.guard == "thin":  # the guard is checked before each session it wraps
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
        if not p.scratch and w.local is None:  # no repo, or can't push yet: no GitHub token
            await self._provision(w, run, p)
        try:
            return await self._run_session(w, run, p, params)
        finally:
            # However the session ended (or the step was cancelled): the token dies with it.
            await self._deprovision(run.code)

    async def _run_session(
        self, w: _Work, run: Run, p: Prepared, params: SandboxParams
    ) -> StepOutcome | None:
        argv = self._argv(run, p, params, api_billing=bool(w.secrets.anthropic_key), local=w.local)
        if self.guard == "thin":
            argv = list(self._wrap(argv, params))
        env = self._env(params, run.session_id, w.secrets, p, local=w.local is not None)
        prompt = self._turn_prompt(run, local=w.local is not None)
        server = await self._open_ctl(run, w)
        try:
            return await self._spawn_session(w, run, p, argv, env, prompt, server)
        finally:
            await self._close_ctl(run.code, server)

    def _turn_prompt(self, run: Run, *, local: bool = False) -> str:
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
        if run.scratch:
            prompt = SCRATCH_RESUME_PROMPT
        else:
            prompt = LOCAL_RESUME_PROMPT if local else RESUME_PROMPT
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
        prompt: str,
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
        disk_hit = self._disk_hit.pop(run.code, ("cap", 0))
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
        if run.pgid is not None:  # the tree wouldn't die: no PR lookup under a live session
            return await self._fail(run, "the session's processes wouldn't stop", w.redactor)
        if stop == "cancelled":
            raise _Cancelled
        if stop == "timeout":
            return await self._timed_out(run, w)
        if stop in ("disk", "cache"):
            why, which = self._disk_text(stop, *disk_hit)
            text = f"{why} ({which}; work kept: /runs {run.code})"
            return await self._fail(run, text, w.redactor)
        if result is not None and (hit := usage_limit(result)) is not None:
            return self._wait_for_limit(w, run, hit)
        return await self._after_exit(w, run, proc.returncode, result, ended=state.ended)

    def _disk_text(self, stop: str, limit: DiskLimit, value: int) -> tuple[str, str]:
        """Why the disk watchdog stopped a run, and which limit it hit."""
        if limit == "free":
            floor = _size(self.min_free_bytes)
            return "the disk is nearly full", f"{_size(value)} free, under the {floor} floor"
        if stop == "cache":
            return f"the runner cache grew over {_size(self.cache_limit_bytes)}", "the cache cap"
        mib = f"{round(self.disk_limit_bytes / 1024**2, 1):g} MiB"
        return f"used over {_size(self.disk_limit_bytes)} of disk", f"{mib} folder cap"

    async def _supervise(
        self,
        w: _Work,
        run: Run,
        p: Prepared,
        proc: asyncio.subprocess.Process,
        target: Target,
        writer: TranscriptWriter,
        state: StreamState,
        prompt: str,
        server: WorkerServer | None = None,
    ) -> str | None:
        """Feed the prompt, pump the output into the transcript and watch the clock, the run
        row, the kill switch and the disk. Stdin stays open (RC-1): owner messages and late
        answers are written into the live session (``_push_live``), and each ``result`` is
        a turn's end where Resonant sends what the run hasn't seen yet or ends the session
        (``_turn_ended``). Always ends with the whole process tree gone."""
        assert proc.stdin and proc.stdout and proc.stderr
        stop: str | None = None
        live = LiveInput(proc.stdin)
        closed = asyncio.Event()  # Resonant closed stdin after a result: the session is ending
        turns: set[asyncio.Task[None]] = set()
        last_line = time.monotonic()
        last_kind: str | None = None  # the last turn line: assistant, user or result
        remote: asyncio.Task[None] | None = None

        async def feed() -> None:
            nonlocal remote
            ask = None
            async with live.lock:
                if self.cfg.remote_control == "on":  # first, as the CLI expects it
                    live.remote = True
                    ask = await live.control({"subtype": "remote_control", "enabled": True})
                if not await live.write_user(prompt):
                    return
            self._inputs[run.code] = live
            if ask is not None:
                remote = asyncio.create_task(self._remote_answer(run, live, ask))
            await self._push_live(run.code)  # anything that arrived since the prompt was built

        async def pump_out() -> None:
            nonlocal last_line, last_kind
            assert proc.stdout
            counted = False
            seen = self._side_effect_context(run, p)
            async for raw, truncated in read_lines(proc.stdout):
                last_line = time.monotonic()
                writer.write(raw, truncated)
                msg = state.feed(raw) if not truncated else None
                kind = msg.get("type") if msg is not None else None
                if kind in ("assistant", "user", "result"):
                    last_kind = kind
                if kind == "assistant" and msg is not None:
                    self._detect(run, msg, seen)
                elif kind == "control_response" and msg is not None:
                    live.feed(msg)
                elif kind == "result" and msg is not None:
                    turn = asyncio.create_task(
                        self._turn_ended(run, live, state, msg, closed, sent=live.sent)
                    )
                    turns.add(turn)
                    turn.add_done_callback(turns.discard)
                if state.session_id and not counted:
                    # Only a session Claude has written can be resumed.
                    counted = True
                    runs.mark_session(self.conn, run.code)

        async def pump_err() -> None:
            assert proc.stderr
            async for raw, truncated in read_lines(proc.stderr, 8192):
                writer.note("stderr", text=raw.decode(errors="replace"), truncated=truncated)

        def watch(path: Path, what: str, max_bytes: int, kind: str) -> asyncio.Task[None]:
            async def exceeded(limit: DiskLimit, value: int) -> None:
                nonlocal stop
                if stop is None:
                    stop = kind
                    self._disk_hit[run.code] = (limit, value)

            async def partial(size: DirSize) -> None:
                # Something in it can't be read (a test's chmod 0 dir, a too-deep tree): said
                # once, never texted, and only what was counted counts against the cap.
                log.warning(
                    "disk watchdog: a partial count",
                    extra={"run": run.code, "path": str(path), "counted": size.bytes},
                )
                writer.note("disk_partial", what=what, counted_bytes=size.bytes)
                with atomic(self.conn):
                    audit(
                        self.conn, "run.disk_partial", run=run.code, what=what, counted=size.bytes
                    )

            return asyncio.create_task(
                watch_disk(
                    path,
                    max_bytes,
                    exceeded,
                    interval_s=self.disk_interval_s,
                    min_free_bytes=self.min_free_bytes,
                    on_partial=partial,
                    free=self._disk_free,
                )
            )

        runs.update(self.conn, run.code, remote_url=None)  # an earlier session's URL is gone
        pumps = [asyncio.create_task(c) for c in (feed(), pump_out(), pump_err())]
        disk = watch(p.run_dir, "run folder", self.disk_limit_bytes, "disk")
        # The worktree or scratch dir, on the same cap.
        work_disk = watch(p.workspace, "worktree", self.disk_limit_bytes, "disk")
        cache = watch(self.cfg.cache_dir, "runner cache", self.cache_limit_bytes, "cache")
        exited = asyncio.create_task(proc.wait())
        began = time.monotonic()
        last_save = began
        bucket = _bucket(run.session_seconds)
        refresh_due = began + self.refresh_s
        refreshing: asyncio.Task[bool] | None = None
        closed_at: float | None = None
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
                if closed.is_set() and cast(str | None, last_kind) == "result":  # set by the pump
                    # Stdin is closed and the last turn is over: the CLI should exit. One that
                    # stays quiet that long without exiting is ended by the reaper below. (A
                    # turn still running after the close may be quiet for long: the time limit
                    # bounds that.)
                    closed_at = closed_at or time.monotonic()
                    if time.monotonic() - max(closed_at, last_line) >= self.end_grace_s:
                        log.warning("session didn't exit after its end", extra={"run": run.code})
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
            self._inputs.pop(run.code, None)
            if server is not None:
                await server.close()
            disk.cancel()
            work_disk.cancel()
            cache.cancel()
            if refreshing is not None:
                refreshing.cancel()
                await asyncio.wait([refreshing])
            hurry = stop == "cancelled" or sys.exc_info()[0] is not None
            # Resonant ends the session (RC-1): Remote Control off (best effort), stdin
            # closed; then the reaper's SIGTERM and SIGKILL for whatever still runs.
            if hurry or cast(str | None, stop) in ("disk", "cache"):  # set by the watchers
                live.close()  # /kill, /cancel, a crash or a runaway: no polite wait first
            elif proc.returncode is None and not live.closed:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        live.end(rc_timeout=self.rc_off_timeout_s), self.rc_off_timeout_s + 1
                    )
            live.close()
            for t in [*turns, *([remote] if remote is not None else [])]:
                t.cancel()
            await reaper.reap(target, grace_s=self.cancel_grace_s if hurry else self.kill_grace_s)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
            exited.cancel()
            done_pumps, pending = await asyncio.wait(pumps, timeout=5)
            for t in pending:
                t.cancel()
            await asyncio.wait([*done_pumps, *pending, disk, work_disk, cache, exited])
            late = [*turns, *([remote] if remote is not None else [])]  # feed may have made one
            for t in late:
                t.cancel()
            if late:
                await asyncio.wait(late)
            self._live.pop(run.code, None)
            with contextlib.suppress(Exception):  # the remote session ended with the process
                runs.update(self.conn, run.code, remote_url=None)
            writer.note("exit", code=proc.returncode, stop=stop)
            writer.close()
            # The time used counts even when the step is being cancelled, and the saved process
            # is forgotten only once it is really gone (else the next start reaps it as a crash).
            used = run.session_seconds + (time.monotonic() - began)
            runs.update(self.conn, run.code, session_seconds=used)
            if not await asyncio.to_thread(reaper.alive, target):
                runs.clear_process(self.conn, run.code)
        return stop

    async def _turn_ended(
        self,
        run: Run,
        live: LiveInput,
        state: StreamState,
        result: dict[str, Any],
        closed: asyncio.Event,
        *,
        sent: int = 0,
    ) -> None:
        """A ``result``: the session is idle (RC-1). What the run hasn't seen yet (answers,
        timeouts, owner messages) is its next turn, in the same process. Otherwise, or after
        an error result, Resonant ends the session and the step goes on as for a process that
        exited: a question still open parks the run, else the PR lookup. A turn Resonant
        didn't start (the owner typing over Remote Control) ends the same way. ``sent`` is how
        many user messages were written when the result was read: one written since starts
        another turn, whose own result decides. Never raises."""
        try:
            async with live.lock:
                if not live.open or live.sent != sent:
                    return  # ending already (the ender sets ``closed``), or a turn in flight
                if result.get("is_error") is not True:
                    self.desk.time_out_due(run.code)
                    found = self.desk.live_delivery(run.code, turn_end=True)
                    if found is not None:
                        text, mark = found
                        told = lambda: self._told_live(run.code, text, mark)  # noqa: E731
                        if await live.write_user(f"{text}\n\nCarry on with the task.", told):
                            return
                live.open = False
                state.ended = True
            await live.end(rc_timeout=self.rc_off_timeout_s)
        except Exception:
            log.exception("turn end failed", extra={"run": run.code})
            live.close()
        closed.set()

    async def _push_live(self, code: str) -> None:
        """An answer or owner message arrived while the run's session process is alive: it is
        written in now (RC-1). Once the session is ending it stays in the store for the next
        turn (``_after_exit`` resumes the session for it). Never raises."""
        live = self._inputs.get(code)
        if live is None:
            return
        try:
            async with live.lock:
                if not live.open:
                    return
                found = self.desk.live_delivery(code, turn_end=False)
                if found is not None:
                    await live.write_user(found[0], lambda: self._told_live(code, *found))
        except Exception:
            log.exception("live input failed", extra={"run": code})

    def _told_live(self, code: str, text: str, sent: Callable[[], None]) -> None:
        """What was written into the live session is delivered, and saved with the turn's
        prompt (``resume_prompt``), so a crash before the session ends resends it."""
        with atomic(self.conn):
            sent()
            run = runs.get(self.conn, code)
            saved = run.resume_prompt if run is not None else None
            runs.update(self.conn, code, resume_prompt=f"{saved}\n\n{text}" if saved else text)
            audit(self.conn, "run.live_input", run=code)

    async def _remote_answer(
        self, run: Run, live: LiveInput, ask: asyncio.Future[ControlReply]
    ) -> None:
        """The answer to the session's Remote Control request (RC-1). A URL is stored on the
        run (a resume's replaces the last); an error, or no answer within ``rc_timeout_s``,
        leaves the run going without it. Either is audited and kept in kv ``REMOTE_KV`` (the
        ``--check`` row), and the requester hears it once per run."""
        reply = await live.reply(ask, self.rc_timeout_s)
        url = session_url(reply) if reply is not None and reply.ok else None
        if reply is None:
            why = f"no answer in {self.rc_timeout_s:g}s"
        elif not reply.ok:
            why = reply.error or "an error with no reason"
        else:
            why = None if url is not None else "no session URL in the answer"
        if why is not None:
            why = self._run_redactors.get(run.code, Redactor()).text(why)
            live.remote = False  # nothing to turn off at the end
        try:
            with atomic(self.conn):
                if url is not None:
                    runs.update(self.conn, run.code, remote_url=url)
                audit(self.conn, "run.remote_control", run=run.code, ok=url is not None,
                      reason=why)  # fmt: skip
                kv_set(self.conn, REMOTE_KV, {"state": "ok" if why is None else "unavailable",
                       "at": iso(self.clock()), "run": run.code, "detail": why or ""})  # fmt: skip
        except Exception:
            log.exception("remote control not recorded", extra={"run": run.code})
        if why is not None:
            log.warning("remote control unavailable", extra={"run": run.code, "reason": why})
        text = (
            f"{run.code} Remote Control: {url}"
            if url is not None
            else f"{run.code}: Remote Control unavailable: {why}."
        )
        try:  # once per run, whichever comes first (a resume's new URL is in /runs)
            await self.channel.send(run.principal, text, dedupe_key=f"run:{run.code}:remote")
        except ChannelError as e:
            log.warning("remote control not texted", extra={"run": run.code, "reason": str(e)})

    async def _after_exit(
        self,
        w: _Work,
        run: Run,
        returncode: int | None,
        result: dict[str, Any] | None,
        *,
        ended: bool = False,
    ) -> StepOutcome | None:
        """Classify a session that ended by itself, or that Resonant ended after its last
        ``result`` (``ended``, RC-1: then the exit status doesn't count, the CLI may have
        needed the kill to go)."""
        keep = f"(work kept: /runs {run.code})"
        bad_exit = returncode != 0 and not ended
        failed = result is None or result.get("is_error") is True or bad_exit
        on_login = not (w.secrets.claude_token or w.secrets.anthropic_key)
        if failed and on_login and isinstance(self._login_check, LoginCheck):
            self._login_check.forget()  # it ran on the owner's login, which may have expired
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
        if result.get("is_error") is True or bad_exit:
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
        merges = await self._check_base_untouched(w, run, p)
        pr = await self.workspace.find_pr(run.repo, p.branch, w.secrets)
        if pr is None:
            if await self.workspace.pushed_commits(p, w.secrets) == 0:
                names = merged_names(detected_list(self.conn, run.code)) if merges else []
                if names:  # PER-268: its work was merging a PR (its own gh pr merge), and it did
                    set_merged(self.conn, run.code, names)
                    if not runs.finish(self.conn, run.code, "done", step="reply"):
                        raise _Cancelled
                    with atomic(self.conn):
                        audit(self.conn, "run.done_merged", run=run.code, merged=len(names))
                    return self._must_get(run.code)
                if unpushed := await self.workspace.local_commits(p):  # PER-271
                    raise RunnerError(
                        f"{COULDNT_PUSH}{_plural(unpushed, 'commit')} saved on {p.branch} "
                        f"(/runs {run.code})"
                    )
                raise NoChanges("the session pushed nothing")
            raise RunnerError(
                f"the session pushed {p.branch} but opened no PR (work kept: /runs {run.code})"
            )
        self._check_stop(run.code)
        target = self._pr_target(run, pr.number)
        if target is not None and pr.state in ("MERGED", "CLOSED"):
            approvals_store.obsolete_for_target(self.conn, target, self.clock())  # PER-269
        if (
            target is not None
            and pr.state == "MERGED"
            and (
                record := action_ids.merge_record(
                    self.conn, *_split_target(target), run_code=run.code
                )
            )
        ):
            # PER-269: Resonant merged it (merge_pr), or the owner approved the run's merge:
            # the run's work is done and merged, not a merge behind Resonant's back.
            set_merged(self.conn, run.code, [f"PR #{pr.number}"])
            set_merged_by(self.conn, run.code, MergedBy(record.by, record.code))
            if not runs.finish(self.conn, run.code, "done", step="reply",
                               pr_number=pr.number, pr_url=pr.url):  # fmt: skip
                raise _Cancelled
            with atomic(self.conn):
                audit(self.conn, "run.done_merged", run=run.code, merged=1, pr=pr.number,
                      by=record.by, approval=record.code)  # fmt: skip
            return self._must_get(run.code)
        if pr.state == "MERGED":  # merged with no record in Resonant: no check ever ran on it
            set_alert(
                self.conn,
                run.code,
                f"⚠ {run.code} merged its own PR #{pr.number}, so none of Resonant's checks "
                f"ran on it. Look at {self._slug(run.repo)}.",
            )
            raise RunnerError(f"it merged its own PR #{pr.number}, not Resonant")
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
            refusal = _clip(clean_external(w.redactor.text(str(e))), REASON_MAX)
        except GitError as e:
            why = f"checking the PR failed: {e.message[-120:]}"
            refusal = _clip(clean_external(w.redactor.text(why)), REASON_MAX)
        else:
            fields.update(files_changed=checked.files, flagged_paths=checked.flagged)
        if not runs.finish(self.conn, run.code, "ready", step="reply", failure=refusal, **fields):
            runs.update(self.conn, run.code, pr_number=pr.number, pr_url=pr.url)
            raise _Cancelled  # cancelled just now: the reply says its PR is open
        with atomic(self.conn):
            audit(self.conn, "run.ready", run=run.code, pr=pr.number)
        return self._must_get(run.code)

    async def _collect_local(self, w: _Work, run: Run, p: Prepared) -> Run:
        """After the session of a run that can't push (RX-1): its work is the commits on its
        local branch, in the owner's checkout. None is ``no_changes``; otherwise the run ends
        ``done`` saying why it wasn't pushed. ``head_sha`` stays unset, so the cleanup after
        ``keep_days`` removes the worktree but keeps the branch (it moved from the base)."""
        self._check_stop(run.code)
        if await self.workspace.local_commits(p) == 0:
            raise NoChanges("the session committed nothing")
        assert w.local is not None
        why = f"can't push yet: {NO_PUSH_LONG[w.local]}"
        if w.task.checkpoint.get("base_from") == "checkout":
            why += f" (it started from your checkout's origin/{p.base_branch}, not fetched)"
        if not runs.finish(self.conn, run.code, "done", step="reply", failure=why):
            raise _Cancelled
        with atomic(self.conn):
            audit(self.conn, "run.done_local", run=run.code, branch=p.branch, why=w.local)
        return self._must_get(run.code)

    async def _check_base_untouched(self, w: _Work, run: Run, p: Prepared) -> tuple[str, ...]:
        """A push straight onto the base branch skips every check a merge runs (the base is
        already live, and the checks only look at what the PR adds). Protection should stop
        it, but a ruleset the app can bypass would not, so the activity feed is always asked
        what the app's bot put on the base during the run, by the type GitHub reports
        (PER-268). A push, force-push or delete fails the run with a ⚠ line in its end text
        (said now), and it is never merged. A PR merge is not a direct push: its shas are
        returned (audited ``run.base_merge``), so a run asked to merge a PR, whose own ``gh pr
        merge`` was read from its commands, ends ``done`` saying so. A bot merge the run
        didn't make (Resonant merging another run's PR meanwhile) is only audited. If the
        feed can't be read the run fails: it can't be shown clean."""
        if self.app is None:
            return ()
        try:
            bot = await self.app.bot_login()
        except GitHubAppError as e:
            raise RunnerError(f"couldn't check {p.base_branch} for direct pushes: {e}") from None
        since = (run.started_at or run.created_at) - timedelta(minutes=1)
        found = await self.workspace.base_pushes(run.repo, w.secrets, actor=bot, since=since)
        if found.merges:
            short = ", ".join(sha[:7] for sha in found.merges[:5])
            with atomic(self.conn):
                audit(self.conn, "run.base_merge", run=run.code, base=p.base_branch, shas=short)
        if not found.pushes:
            return found.merges
        short = ", ".join(sha[:7] for sha in found.pushes[:5])
        with atomic(self.conn):
            audit(self.conn, "run.base_push", run=run.code, base=p.base_branch, shas=short)
        set_alert(
            self.conn,
            run.code,
            f"⚠ {run.code} pushed {short} straight to {p.base_branch}, skipping every check. "
            f"Look at {self._slug(run.repo)} now.",
        )
        raise RunnerError(f"it pushed directly to {p.base_branch} ({short}), so it won't be merged")

    # -- fork mode (docs/employee-plan.md D6) --

    def _repo_of(self, run: Run) -> repo_store.Repo:
        repo = repo_store.find(self.conn, run.repo)
        if repo is None or repo.name != run.repo:
            raise RunnerError(f"{run.repo} isn't a registered repo")
        return repo

    async def _fork_of(self, run: Run, secrets: RunnerSecrets) -> str:
        """The owner's fork for a fork-mode run, from GitHub (made the first time; asked
        again after a restart, since nothing a worker can write is trusted for it)."""
        if (fork := self._forks.get(run.code)) is None:
            fork = self._forks[run.code] = await self.workspace.ensure_fork(run.repo, secrets)
        return fork

    async def _collect_fork(self, w: _Work, run: Run, p: Prepared) -> Run:
        """After a fork run's session: the branch it pushed to the fork is fetched into the
        mirror and put through the pre-merge checks (tokens, objects, paths, the commit
        text that becomes the PR). Nothing pushed is ``no_changes``; a refusal fails the run,
        and nothing is opened upstream. Then the ``publish`` step asks the owner."""
        self._check_stop(run.code)
        fork = await self._fork_of(run, w.secrets)
        try:
            checked = await self.workspace.check_fork(
                run.repo, run.code, fork, p.branch, p.base_sha, w.secrets
            )
        except CheckFailed as e:
            raise RunnerError(
                f"the checks refuse it, so nothing was opened upstream: {e}"
            ) from None
        # The session is over: its token goes before the owner is asked, so nothing holding
        # it can move the branch between the ok and the PR.
        await self._deprovision(run.code, remove_dir=False)
        self._check_stop(run.code)
        runs.update(
            self.conn, run.code, step="publish", head_sha=checked.head_sha,
            files_changed=checked.result.files, flagged_paths=checked.result.flagged,
        )  # fmt: skip
        with atomic(self.conn):
            audit(self.conn, "run.fork_checked", run=run.code, fork=fork, head=checked.head_sha)
        return self._must_get(run.code)

    async def _publish(self, w: _Work, run: Run, p: Prepared) -> StepOutcome | None:
        """Ask before the upstream PR: an ``external_pr`` trust check on the original repo
        (``irreversible``: it goes out under the owner's name, so it asks every time unless a
        rule allows it). It goes through the worker desk exactly like a worker's
        ``resonant check``: a check row, the decision (``TrustChecks.request_check``) in the
        background, the run waiting without a slot (``waiting``, reason ``question``) and woken
        when it comes; ``ask_timeout`` and a restart close it the same way (after a restart it
        is asked again). On ok the branch is fetched and checked again, and the PR is opened
        with the ``public_repo`` token only if its head is still the one the owner was asked
        about. The run ends ``done`` either way. None means it ended."""
        repo = self._repo_of(run)
        if run.head_sha is None:
            raise RunnerError("the fork's branch wasn't checked")
        change = await self.workspace.fork_change(run.repo, run.code, p.base_sha)
        if change.head != run.head_sha:
            raise RunnerError(f"{p.branch} isn't at the commit that was checked")
        self._check_stop(run.code)
        check = Check(system=repo.slug, action=EXTERNAL_PR, what=change.what, irreversible=True)
        q = self.desk.find_check(run.code, check)
        if q is None or (q.status == "timed_out" and q.answer == RESTARTED):
            q = await self.desk.request_check(run, check)
        if q.status == "open":
            runs.update(self.conn, run.code, status="waiting", wait_reason="question",
                        waiting_since=self.clock())  # fmt: skip
            self._pulse_open(self._must_get(run.code), "question")
            until = self.desk.wait_until(run.code) or self.clock() + timedelta(minutes=1)
            return Wait("human_reply", dict(w.task.checkpoint), until)
        questions_store.mark_delivered(self.conn, q.id, at=self.clock())
        self._check_stop(run.code)
        if not (q.status == "answered" and q.answer == CHECK_OK):
            why = "no decision in time" if q.status == "timed_out" else "not approved"
            if not runs.finish(self.conn, run.code, "done", step="reply", failure=why):
                raise _Cancelled
            return None
        fork = await self._fork_of(run, w.secrets)
        checked = await self.workspace.check_fork(
            run.repo, run.code, fork, p.branch, p.base_sha, w.secrets
        )
        if checked.head_sha != run.head_sha:
            raise RunnerError(
                f"{p.branch} moved on {fork} after you were asked; nothing was opened upstream"
            )
        self._check_stop(run.code)
        pr = await self.workspace.open_upstream_pr(
            run.repo, fork, p.branch, run.head_sha, checked.change,
            self._upstream_body(repo, run, checked.change.body), w.secrets,
        )  # fmt: skip
        fields: dict[str, Any] = {"pr_number": pr.number, "pr_url": pr.url}
        if pr.files is not None:
            fields["files_changed"] = pr.files
        with atomic(self.conn):
            audit(self.conn, "run.upstream_pr", run=run.code, repo=repo.slug, pr=pr.number,
                  head=run.head_sha)  # fmt: skip
        if not runs.finish(self.conn, run.code, "done", step="reply", **fields):
            runs.update(self.conn, run.code, **fields)
            raise _Cancelled  # cancelled just now: the reply says its PR is open
        return None

    @staticmethod
    def _upstream_body(repo: repo_store.Repo, run: Run, body: str) -> str:
        """The upstream PR's description: the last commit's body. The owner's ask is quoted
        only when the repo's ``quote_ask`` is on (off by default for fork repos)."""
        if repo.quote_ask:
            asked = clean_external(run.ask)[:500]
            body = f"{body}\n\n> {asked}".strip()
        return body

    # -- auto-merge (``start_run(merge=True)``, merge per trust rules) --

    def _auto_merge_blocker(self, run: Run) -> str | None:
        """Why a ready run with merge intent isn't merged without the owner, or None."""
        repo = repo_store.find(self.conn, run.repo)
        if repo is None or repo.mode != "push":
            return "it isn't a repo I merge"
        if run.failure:
            return "the checks refuse it"
        if run.flagged_paths:
            return f"it changes {', '.join(run.flagged_paths)}"
        if repo.ci is False:
            return f"{repo.name} has no CI"
        if self._paused():
            return "Resonant is paused"
        if merge_decision(self.conn, repo.name) != "ok":
            return f"a rule says to ask before merging in {repo.name}"
        return None

    def _owner_merge_blocker(self, run: Run) -> str | None:
        """Why an owner's merge watch (``_watch_merge``) stops before merging, or None. The
        owner asked for the merge, so flagged paths, a repo with no CI and a rule that asks
        don't stop it; the checks and CI still do, in ``Workspace.merge``."""
        repo = repo_store.find(self.conn, run.repo)
        if repo is None or repo.mode != "push":
            return "it isn't a repo I merge"
        if self._paused():
            return "Resonant is paused"
        return None

    async def _auto_merge(self, task: Task, run: Run, *, first: bool) -> StepOutcome:
        """A ready run whose ask included merging: merge once CI is green on the head the run
        recorded, through the same path and checks as ``/merge``. Pending CI is looked at
        again every ``merge_poll_s`` (the task waits without a slot) for at most
        ``merge_wait_s`` after the run; red CI, a refusal, a moved head or a rule that asks
        stops it with a reply. ``first``: the run just became ready, so its reply hasn't gone
        out yet.

        The same wait serves an owner's ``/merge`` that found CI running (checkpoint ``by:
        owner``, from ``_watch_merge``): its blockers are ``_owner_merge_blocker``'s, its
        ``merge_wait_s`` counts from the ``/merge`` (``since``), and the merge is the owner's
        (flagged paths allowed). Its outcome is reported like an auto-merge's: the merged
        line by the ``merged`` level, a stop as a text."""
        done = Done({"run": run.code, "status": run.status})
        owner = task.checkpoint.get("by") == "owner"
        why = self._owner_merge_blocker(run) if owner else self._auto_merge_blocker(run)
        state: str | None = None
        secrets: RunnerSecrets | None = None
        if why is None:
            try:
                secrets = await load_runner_secrets(self.secrets, claude=False)
                state = await self.workspace.ci_state(run.repo, _pr_number(run), secrets)
            except (SecretError, RunnerError) as e:
                why = f"CI couldn't be read ({e})"
        if why is None and state in ("pending", "error", "none"):  # none: checks not registered yet
            start = _since(task.checkpoint) if owner else None
            start = start or run.ended_at or self.clock()
            if self.clock() - start < timedelta(seconds=self.merge_wait_s):
                if first:
                    told = await self._conclude(run, watching=True)
                    if isinstance(told, Fail):
                        return told
                checkpoint = {**task.checkpoint, "watch": True}
                return Wait("ci", checkpoint, self.clock() + timedelta(seconds=self.merge_poll_s))
            why = (
                "no CI checks reported"
                if state == "none"
                else f"CI didn't finish within {fmt_duration(self.merge_wait_s)}"
            )
        elif why is None and state == "red":
            why = "CI is red"
        if why is None and secrets is not None:
            merged = await self._merge(
                run.code, secrets, by="owner" if owner else "auto", allow_flagged=owner,
                allow_no_ci=owner, wait_ci=owner, replied=False,
            )  # fmt: skip
            if isinstance(merged, MergeQueued):  # CI restarted between the look and the merge
                checkpoint = {**task.checkpoint, "watch": True}
                return Wait("ci", checkpoint, self.clock() + timedelta(seconds=self.merge_poll_s))
            if isinstance(merged, Merged):
                if owner and (merged.no_ci or merged.flagged):
                    # the warnings the owner would have seen on a direct /merge
                    with contextlib.suppress(ChannelError):
                        await self.channel.send(
                            run.principal, merged.text,
                            dedupe_key=f"run:{run.code}:merge-watch-warn",
                        )  # fmt: skip
                if first:  # the merged line is the run's only word: never cleared silently
                    await self._deprovision(run.code)
                    await self._end_cli(run.code)
                # the merged line went out by the trust rules' urgency (``_report_merged``)
                return Done({"run": run.code, "status": "merged"})
            now = self._must_get(run.code)
            if now.status != "ready":  # merged (or ended) another way meanwhile: nothing to say
                return Done({"run": run.code, "status": now.status})
            assert isinstance(merged, Refused)
            why = merged.reason.removeprefix(f"{run.code} not merged: ")
        assert why is not None
        if owner:
            with contextlib.suppress(ChannelError):
                await self.channel.send(
                    run.principal, f"{run.code} not merged: {why}. /merge {run.code} to try again.",
                    dedupe_key=f"run:{run.code}:merge-watch:{task.checkpoint.get('since')}",
                )  # fmt: skip
            return done
        note = f"Not merged automatically: {why}. /merge {run.code} when it's ready."
        if first:
            return await self._conclude(run, note=note)
        with contextlib.suppress(ChannelError):
            await self.channel.send(
                run.principal, f"{run.code} {note[0].lower()}{note[1:]}",
                dedupe_key=f"run:{run.code}:auto-merge",
            )  # fmt: skip
        return done

    # -- the session's GitHub token --

    def _pr_target(self, run: Run, number: int) -> str | None:
        """``jhe/dori#2`` for a PR of the run's repo (``gate.actions``); None (never raises)
        when the repo isn't registered."""
        repo = repo_store.find(self.conn, run.repo) if run.repo else None
        return action_ids.pr_target(repo.slug, number) if repo is not None else None

    def _slug(self, repo_name: str) -> str:
        repo = repo_store.find(self.conn, repo_name)
        if repo is None:
            raise RunnerError(f"{repo_name} isn't a registered repo")
        return repo.slug

    def _redactor(self, w: _Work, code: str) -> Redactor:
        return Redactor(
            [*w.secrets.values(), *self._tokens.get(code, ()), *self._spent.get(code, ())]
        )

    async def _mint(self, run: Run, secrets: RunnerSecrets) -> str:
        """A new token for the run's repo, remembered so it is revoked at the end. A fork
        run's token is for the owner's fork (the app is installed on all of the owner's
        repos, which covers forks), never for the original: so the session can push its
        branch to the fork but can't open, or touch, anything upstream."""
        repo = repo_store.find(self.conn, run.repo)
        if repo is None:
            raise RunnerError(f"{run.repo} isn't a registered repo")
        if self.app is None:
            raise RunnerError("the runner isn't set up: runner.github_app isn't in the config")
        owner, name = None, repo.slug.partition("/")[2]
        if repo.mode == "fork":
            owner, _, name = (await self._fork_of(run, secrets)).partition("/")
        try:
            minted = await self.app.mint(name, workflows=repo.allow_github_changes, owner=owner)
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
        token = await self._mint(run, w.secrets)
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
            token = await self._mint(run, w.secrets)
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
        # cleaned where stored: a failure reason (git stderr, GitHub, the session) is texted
        reason = _clip(clean_external(red.text(reason)), REASON_MAX)
        if not runs.finish(self.conn, run.code, "failed", failure=reason, step="reply"):
            return await self._conclude(self._must_get(run.code))  # cancelled first
        with atomic(self.conn):
            audit(self.conn, "run.failed", run=run.code, reason=reason)
        return await self._conclude(self._must_get(run.code))

    async def _end(self, run: Run, status: runs.Status) -> StepOutcome:
        runs.finish(self.conn, run.code, status, step="reply")
        return await self._conclude(self._must_get(run.code))

    async def _conclude(
        self, run: Run, *, watching: bool = False, note: str | None = None
    ) -> StepOutcome:
        """Everything after a run's row has its final status: clear the pulse item, send the
        reply (once), and tell the loop how the task ended. ``watching`` and ``note`` are
        the auto-merge's (it will merge once CI is green; why it won't)."""
        self._forks.pop(run.code, None)
        if run.status == "cancelled":
            await self._finish_cancelled(run.code, send=False)
        else:
            await self._deprovision(run.code)
            await self._end_cli(run.code)
        try:
            await self._send_reply(run, watching=watching, note=note)
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
            secrets = await load_runner_secrets(self.secrets, claude=False)
            state = await self.workspace.ci_state(run.repo, run.pr_number, secrets)
        except Exception:  # a reply must go out whatever gh or the Keychain say
            log.warning("CI state unavailable for the reply", extra={"run": run.code})
            return None
        return None if state == "error" else state

    async def _send_reply(
        self, run: Run, *, watching: bool = False, note: str | None = None
    ) -> None:
        """Report an ended run by urgency (``runners/reports.py``): its digest line is
        written untold, then, when it is ``now``, the full reply is texted and the line
        marked told. A refused text leaves the line for the next digest. A cancel is always
        texted (the owner did it), and so is a token that wasn't revoked. ``watching`` and
        ``note`` are the auto-merge's; a fork run's reply names its upstream."""
        repo = repo_store.find(self.conn, run.repo) if run.repo else None
        no_ci = repo is not None and repo.mode == "push" and repo.ci is False
        upstream = repo.slug if repo is not None and repo.mode == "fork" else None
        ci = await self._ci_word(run)
        merged = merged_list(self.conn, run.code) if run.status == "done" else []
        merged_by = merged_by_of(self.conn, run.code) if merged else None
        parts = end_parts(run, ci, no_ci=no_ci, upstream=upstream, watching=watching,
                          merged=merged, merged_by=merged_by)  # fmt: skip
        event = end_event(run)
        title = end_title(run, ci, merged=merged)
        # PER-268: one text per outcome. The ⚠ lines (a direct push, a PR merged by the
        # session) come first, then the auto-merge's note, the headline's own lines and the
        # side effects; end_text keeps the headline and at most two more (⚠ always).
        warn = alerts(self.conn, run.code)
        warned = bool(warn)
        if (expires := self._unrevoked.get(run.code)) is not None:
            at = expires.astimezone(self.tz).strftime("%H:%M")
            warn.append(f"⚠ The run's GitHub token wasn't revoked; it expires at {at}.")
            title += f" · ⚠ token not revoked, expires {at}"
            warned = True
        text: str | None = None
        if parts is not None:
            head, lines, said = parts
            if run.status != "cancelled" and (still := self._still_working(run)):
                head += f" {still}"
            if note:  # the auto-merge's note says how to merge it: no second hint
                lines = [line for line in lines if not line.startswith(f"/merge {run.code} ")]
            also = self.reports.also_line(run, skip=merged)
            extra = [*warn, *([note] if note else []), *lines, *([also] if also else []), *said]
            text = end_text(head, extra)
        held = any(
            e.high and e.kind in HELD_KINDS for e in detected_list(self.conn, run.code)
        )  # a merge the end text says: the side effect's level applies to it
        if text is None or event is None:
            self.reports.clear_silently(run)
            if text is None:
                return
            item = None
        else:
            level = "now" if warned else self.reports.level(run, event)
            if (
                level == "digest"
                and held
                and self.reports.level(run, "side_effect", "merge") == "now"
            ):
                level = "now"
            self.reports.audit_report(run, event, level)
            item = self.reports.ended(run, title, urgent=event == "failed" or warned)
            if level == "digest":
                self._unrevoked.pop(run.code, None)  # the line carries the warning
                return
        try:
            await self.channel.send(run.principal, text, dedupe_key=f"run:{run.code}:{run.status}")
        except ChannelRefused:  # nobody took it: the digest carries the line (and warning)
            log.warning("run reply refused", extra={"run": run.code})
            self._unrevoked.pop(run.code, None)
            return
        self._unrevoked.pop(run.code, None)  # only now: a retried send says it again
        if item is not None:
            self.reports.told(item)
        if warn:
            clear_alerts(self.conn, run.code)
        # PER-268: what the owner was told, for the brains, without the worker's own words
        # (its summary, its did: notes). Still outside content as list_runs is (the ask may
        # be a tainted turn's, a failure quotes GitHub, flagged paths are the worker's): it
        # taints the turn that sees it, as the run tools do.
        bare = end_parts(replace(run, summary=None), ci, no_ci=no_ci, upstream=upstream,
                         watching=watching, merged=merged, merged_by=merged_by)  # fmt: skip
        if bare is not None:
            said = end_text(bare[0], [*warn, *([note] if note else []), *bare[1]])
            ref = f"run:{run.code}:{run.status}"
            system_texts.record(self.conn, run.principal, "run", said, ref=ref,
                                task_id=run.task_id, outside=True)  # fmt: skip

    def _still_working(self, run: Run) -> str:
        """``Still working on R5 (public dashboard view).``: the owner's other active runs,
        by name (at most two, then ``+N more``), or "" when nothing else is running."""
        try:
            others = [r for r in runs.active(self.conn) if r.code != run.code
                      and r.principal == run.principal]  # fmt: skip
        except Exception:  # the clause is a courtesy: the reply goes out without it
            return ""
        if not others:
            return ""
        named = [run_who(r) for r in others[:2]]
        if len(others) > 2:
            named.append(f"{len(others) - 2} more")
        return f"Still working on {and_list(named)}."

    async def _report_merged(self, run: Run, *, by_owner: bool, deps: Sequence[str] = ()) -> None:
        """A merged run's digest line (it lists the dependency files the PR changed, ``deps``).
        ``by_owner``: the merge answered the owner's own ``/merge`` (or a yes), which already
        got the reply, so the line is marked told. Otherwise (EM-7's merge per trust rules,
        or an owner's merge that waited for CI) it goes by the ``merged`` level."""
        item = self.reports.ended(run, end_title(run, deps=deps), urgent=False)
        if by_owner:
            self.reports.told(item)
            return
        level = self.reports.level(run, "merged")
        self.reports.audit_report(run, "merged", level)
        if level == "digest":
            return
        try:
            await self.channel.send(run.principal, item.title,  # with its did: list
                                    dedupe_key=f"run:{run.code}:merged")  # fmt: skip
        except ChannelError as e:
            log.warning("merge not texted", extra={"run": run.code, "reason": str(e)})
            return
        self.reports.told(item)

    async def _finish_cancelled(self, code: str, *, send: bool = True) -> None:
        """What a cancelled run still needs once its processes are gone: its dir deleted, its
        pulse item cleared, and (once) the reply. Safe to repeat."""
        async with self._cleanup_locks.setdefault(code, asyncio.Lock()):
            run = self._must_get(code)
            # The live session if there is one, and anything else of the run (a straggler
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
        self.reports.forget(code)
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
                             failure=_clip(clean_external(error or ""), REASON_MAX)
                             or "the task ended unexpectedly"):  # fmt: skip
                ended.append(run)
        cutoff = self.clock() - timedelta(minutes=2)
        for run in runs.active(self.conn):
            lost = run.task_id is None and run.created_at < cutoff
            if lost and runs.finish(
                self.conn, run.code, "failed", failure="lost before it started"
            ):
                ended.append(run)
        for run in ended:
            current = self._must_get(run.code)
            try:
                if current.status == "failed":  # no step will conclude it: report it here
                    with contextlib.suppress(ChannelError):
                        await self._send_reply(current)
                else:
                    self._pulse_clear(run)
            except Exception:  # one run's report must not stop the others'
                log.exception("ended run not reported", extra={"run": run.code})
            if current.status == "cancelled":
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
        """The ``normal`` pulse item while a run goes: mentioned by the next hourly pulse
        (``runners/reports.py``), with the worker's latest note."""
        self.reports.running(run, bucket or _bucket(run.session_seconds))

    def _pulse_clear(self, run: Run) -> None:
        """Clear the item silently: the owner got a direct reply (a cancel)."""
        self.reports.clear_silently(run)


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


def _mergeable(run: Run) -> bool:
    return (
        run.status == "ready"
        and run.pr_number is not None
        and run.head_sha is not None
        and run.branch is not None
    )


def _split_target(target: str) -> tuple[str, int]:
    """``("jhe/dori", 2)`` for ``jhe/dori#2`` (a ``gate.actions.pr_target``)."""
    repo, number = target.rsplit("#", 1)
    return repo, int(number)


def _pr_number(run: Run) -> int:
    assert run.pr_number is not None
    return run.pr_number


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
    """Binary units, as the caps are set: 5000 MiB is ``4.9 GiB``."""
    if n >= 1024**3:
        return f"{round(n / 1024**3, 1):g} GiB"
    return f"{round(n / 1024**2, 1):g} MiB"


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


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


def _since(checkpoint: dict[str, Any]) -> datetime | None:
    """An owner merge watch's start (``since``, code-written ISO with an offset), or None."""
    raw = checkpoint.get("since")
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return at if at.tzinfo is not None else None

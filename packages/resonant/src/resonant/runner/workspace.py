"""The runner's workspace: the owner's checkouts, each run's worktree or scratch dir, the
session's credentials, PR discovery and merge (docs/employee-plan.md D1, docs/repos-plan.md D10).

A run on a repo works in a git worktree of the owner's checkout, ``<code_dir>/<name>`` (cloned on
first use; a repo's ``path`` overrides where it is), at ``<code_dir>/.worktrees/<name>/R<n>`` on
branch ``resonant/r<n>-<slug>`` from ``origin/<base>``. The checkout's gitignored local config
(``.env`` files, ``.envrc``, tool versions, ``.mcp.json``) is symlinked into the worktree. A run
with no repo gets ``<code_dir>/.scratch/R<n>``. The Claude session pushes its own branch and
opens the PR with a per-run GitHub App token (``write_session_credentials``); this module finds
the PR afterwards and, before any merge, fetches its head into the daemon-only mirror and runs
the checks.

Trust rules this module keeps:

- The owner's checkout is only ever fetched into (``refs/remotes/origin/<base>``) and given
  worktrees and their ``resonant/…`` branches: its working tree, its current branch and its
  config are never touched. A first clone lands in a temp dir and is renamed into place. A
  checkout is never deleted; only a run's worktree or scratch dir is, and only at the exact
  path the run recorded under ``code_dir``.
- The session's git finds the run's credential helper through ``GIT_CONFIG_*`` in its
  environment (``session_git_env``), not through any config file, so nothing about the run's
  token reaches the checkout's config and the owner's own pushes are unchanged.
- The daemon's GitHub token is only ever given to the daemon's own git (the first clone of a
  checkout, the bare mirror's fetch) and to ``gh``, and never by argv or environment. It goes in a
  0600 file in a fresh 0700 dir under ``runner.secrets_dir``, created right before the call
  and removed right after, success or not. git reads it through a credential helper script;
  ``gh`` through a ``hosts.yml`` in its own ``GH_CONFIG_DIR``. The token is never in a remote
  URL.
- Every daemon git command runs with ``GIT_CONFIG_NOSYSTEM=1``, ``GIT_CONFIG_GLOBAL=/dev/null``,
  ``core.hooksPath=/dev/null``, no askpass, no inherited credential helper and an absolute git
  binary; in a checkout also under the guard, with fsmonitor, auto-gc and submodule recursion
  off. The committer comes from
  the environment, never from a config file.
- The pre-merge checks read the bare mirror (``runner.mirror_dir``), which only the daemon
  writes, never a checkout: a worker can write its checkout's ``.git`` (config, refs, objects),
  so nothing the merge decision rests on is read from there. The PR's head arrives in the
  mirror by ``git fetch`` with fsck.
- The session's own token (a one-repo App installation token, see ``github_app``) lives in a
  0700 ``creds`` dir inside the run dir: a credential helper script, a ``token`` file and a
  ``gh/hosts.yml``. It is never in env or argv. The daemon writes there only through
  directory fds with ``O_NOFOLLOW`` and ``rename``, so a link planted by the session can't
  redirect a write.
- A worker can write its checkout's ``.git`` (it must, to commit): its hooks, refs and config.
  So the daemon's token goes only to the mirror's fetch; the base sha, CI and tracked names are
  read from the mirror; and every git command in a checkout or worktree runs tokenless under
  the thin guard (``guard``, EM-3), so a filter, hook or URL rewrite planted there runs as the
  worker could, never as the daemon. ``worktree add`` is ``--no-checkout``; the checkout itself
  is a guarded ``reset --hard``. The owner's own git in the checkout still trusts it: that is
  the honor-system trust of docs/employee-plan.md D4.
- Branch hygiene: a run's ``resonant/…`` branch is deleted only by compare-and-delete against
  the sha the run left, and never while a worktree has it checked out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
import sqlite3
import stat
import tempfile
import weakref
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote

from resonant.config import RunnerConfig
from resonant.redact import Redactor
from resonant.runner.checks import (
    CheckFailed,
    CheckResult,
    GitError,
    GitRunner,
    OutputTooLarge,
    check_texts,
    ci_present,
    run_checks,
)
from resonant.runner.cleanup import RUN_CODE, rmtree_nofollow
from resonant.secrets import RunnerSecrets
from resonant.store import repos as repo_store
from resonant.store.db import atomic
from resonant.store.repos import Repo

FORK_NEEDS_PUBLIC_REPO = (
    "A fork run needs a classic GitHub token with only the public_repo scope. On the Mini: "
    "resonant secret set runner/github_public_token."
)
GITHUB_TOKEN_HOW = "resonant secret set runner/github_token"  # noqa: S105  # a command


def needs_github_token(what: str) -> str:
    """The sentence for something only the daemon's own GitHub token can do, when it isn't
    set: workers still run without it (docs/employee-plan.md, RX-1)."""
    return (
        f"{what} needs the daemon's GitHub token (runner/github_token), which isn't set yet. "
        f"On the Mini: {GITHUB_TOKEN_HOW}."
    )


FORK_READY_TRIES = 6  # GitHub makes a new fork in the background; the clone waits for it
FORK_READY_WAIT_S = 5.0  # the first wait, doubled each try
UPSTREAM_BODY_MAX = 4000
GIT_TIMEOUT_S = 300.0
GH_TIMEOUT_S = 120.0
COMMITTER_NAME = "Resonant runner"
COMMITTER_EMAIL = "resonant-runner@users.noreply.github.com"
_SAFE_PATH = "/usr/bin:/bin"
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
_SHA = re.compile(r"[0-9a-f]{40}")
# An anonymous fetch GitHub refused (a private repo), as opposed to the network failing.
_ANON_REFUSED = re.compile(
    r"not found|does not appear to be a git repository|could not read username|"
    r"authentication failed|terminal prompts disabled|\b40[13]\b",
    re.IGNORECASE,
)
_ERR_CLIP = 300
CREDS_DIR = "creds"
_PUSH_KINDS = ("push", "force_push", "branch_deletion", "pr_merge")
_PR_FIELDS = "state,headRefName,headRefOid,baseRefName,isCrossRepository,title,body"
WORKTREES = ".worktrees"  # <code_dir>/.worktrees/<name>/R<n>
SCRATCH = ".scratch"  # <code_dir>/.scratch/R<n>
EXCLUDE_FILE = "git-exclude"  # in the run dir: the session's core.excludesFile
# The checkout's local config a run sees through a symlink, when present and untracked.
LOCAL_CONFIG = (".env", ".envrc", ".tool-versions", ".python-version", ".node-version", ".mcp.json")
_TEMPLATES = (".example", ".sample", ".template")  # committed .env templates, never linked
# github.com over ssh becomes https, so the run's credential helper answers for it
_INSTEAD_OF = (
    ("url.https://github.com/.insteadOf", "git@github.com:"),
    ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
)
# daemon git in a checkout: nothing a worker could have configured there runs as the daemon
_CHECKOUT_CONFIG = (
    ("core.fsmonitor", "false"),
    ("core.sshCommand", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("fetch.recurseSubmodules", "false"),
    ("submodule.recurse", "false"),
    ("protocol.allow", "never"),
    ("protocol.https.allow", "always"),
    ("protocol.file.allow", "always"),
    ("core.askPass", ""),
    ("core.pager", "cat"),
    *_INSTEAD_OF,
)
_GITHUB_REMOTE = re.compile(
    r"(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|[^@/:]+@)github\.com[:/]"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
    re.IGNORECASE,
)


# Checkout locks, per event loop and repo, shared by every Workspace (the runner's and the
# daily cleanup job's) so their git in one checkout never overlaps.
_CHECKOUT_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)


class RunnerError(Exception):
    """A failure with a reason fit for ``R3 failed: <reason>``. Never carries a token."""


class NoChanges(RunnerError):
    """The session left no commits: the run ends ``no_changes``."""


class MergeRefused(RunnerError):
    pass


def upstream_merges(slug: str) -> str:
    """Why a fork-mode repo's PR is never merged by Resonant (docs/repos-plan.md D3)."""
    return f"{slug} is upstream; its maintainers merge."


# --- helpers -----------------------------------------------------------------------------


def committer_env() -> dict[str, str]:
    """Author and committer identity by environment (for git here and for the sandbox env)."""
    return {
        "GIT_AUTHOR_NAME": COMMITTER_NAME,
        "GIT_AUTHOR_EMAIL": COMMITTER_EMAIL,
        "GIT_COMMITTER_NAME": COMMITTER_NAME,
        "GIT_COMMITTER_EMAIL": COMMITTER_EMAIL,
    }


def slugify(text: str, *, limit: int = 30) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return slug or "run"


def branch_name(code: str, ask: str) -> str:
    return f"resonant/{code.lower()}-{slugify(ask)}"


def _config_env(config: Sequence[tuple[str, str]]) -> dict[str, str]:
    """``GIT_CONFIG_COUNT``/``KEY``/``VALUE``: config for every git started with this env,
    above any config file, written nowhere."""
    env = {"GIT_CONFIG_COUNT": str(len(config))}
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def session_git_env(helper: Path | None, exclude: Path | None = None) -> dict[str, str]:
    """The session's git config, by environment (docs/employee-plan.md D1): any credential
    helper from the checkout's config reset, then the run's ``helper`` (a script path, never a
    token), github.com over ssh rewritten to https, and ``exclude`` as ``core.excludesFile`` so
    the linked local config is never committed. Nothing is written to the checkout."""
    config = [("credential.helper", "")]
    if helper is not None:
        config.append(("credential.helper", f"!{shlex.quote(str(helper))}"))
    config += _INSTEAD_OF
    if exclude is not None:
        config.append(("core.excludesFile", str(exclude)))
    return _config_env(config)


def local_config_name(name: str) -> bool:
    """Whether a top-level checkout entry is local config a run's worktree links to."""
    if name in LOCAL_CONFIG:
        return True
    return name.startswith(".env.") and not name.lower().endswith(_TEMPLATES)


def link_local_config(checkout: Path, worktree: Path, tracked: Iterable[str]) -> list[str]:
    """Symlink the checkout's untracked local config (``.env``, ``.env.*`` but not templates,
    ``.envrc``, tool versions, ``.mcp.json``) into a fresh worktree. Only regular files or
    links, only names the checkout doesn't track and the worktree doesn't already have.
    Returns the names linked."""
    skip = set(tracked)
    linked: list[str] = []
    for name in sorted(os.listdir(checkout)):
        if not local_config_name(name) or name in skip:
            continue
        try:
            mode = os.lstat(checkout / name).st_mode
        except FileNotFoundError:
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)) or os.path.lexists(worktree / name):
            continue
        os.symlink(checkout / name, worktree / name)
        linked.append(name)
    return linked


def remote_slug(url: str) -> str | None:
    """``owner/name`` of a github.com remote URL (https, ssh or scp form), else None."""
    m = _GITHUB_REMOTE.fullmatch(url.strip())
    return m[1] if m else None


def git_env(helper: Path | None = None, extra: Sequence[tuple[str, str]] = ()) -> dict[str, str]:
    """A from-scratch git environment: no user or system config, no hooks, no prompts, no
    redirects, ``extra`` config, and a credential helper only when ``helper`` (a script path,
    never a token) is given."""
    config = [("core.hooksPath", "/dev/null"), ("http.followRedirects", "false"), *extra]
    config.append(("credential.helper", ""))  # reset any inherited helper
    if helper is not None:
        config.append(("credential.helper", f"!{shlex.quote(str(helper))}"))
    env = {
        "PATH": _SAFE_PATH,
        "HOME": "/var/empty",
        "LANG": "en_US.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",  # set but empty: no core.askPass program is ever run
        **_config_env(config),
        **committer_env(),
    }
    return env


CREDENTIAL_HELPER = """#!/bin/sh
# git credential helper: answers "get" for github.com only, from a 0600 file next to it.
[ "$1" = get ] || exit 0
host=
while IFS= read -r line && [ -n "$line" ]; do
  case "$line" in host=*) host=${line#host=} ;; esac
done
[ "$host" = github.com ] || exit 0
IFS= read -r token < "$(dirname "$0")/token"
printf 'username=x-access-token\\npassword=%s\\n' "$token"
"""


def _write_private(path: Path, text: str, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


@contextlib.contextmanager
def secret_dir(base: Path) -> Generator[Path]:
    """A fresh 0700 dir under ``base`` (itself 0700, daemon-only), removed on exit whether
    the body succeeded or not. File reads in it are protected by the sandbox's ``$HOME``
    read deny (``~/.resonant`` is denied), not by anything in here."""
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(base, 0o700)
    path = Path(tempfile.mkdtemp(prefix="call-", dir=base))  # mkdtemp is 0700
    try:
        yield path
    finally:
        rmtree_nofollow(path)


def write_git_credentials(dir_: Path, token: str) -> Path:
    """Put the token and the credential helper script in ``dir_``; returns the helper."""
    _write_private(dir_ / "token", token + "\n")
    helper = dir_ / "credential-helper"
    _write_private(helper, CREDENTIAL_HELPER, 0o700)
    return helper


def write_gh_config(dir_: Path, token: str) -> Path:
    """A ``GH_CONFIG_DIR`` with a ``hosts.yml`` holding the token; returns the dir."""
    config = dir_ / "gh"
    config.mkdir(mode=0o700)
    _write_private(
        config / "hosts.yml",
        f"github.com:\n    oauth_token: {token}\n    git_protocol: https\n",
    )
    return config


# --- the session's credentials -----------------------------------------------------------

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _hosts_yml(token: str) -> str:
    return f"github.com:\n    oauth_token: {token}\n    git_protocol: https\n"


def _plain_dir(parent_fd: int | None, name: str | Path, *, create: bool) -> int:
    """An fd for a directory of ours below ``parent_fd`` (or at an absolute path), never
    through a link, created 0700 when ``create``. The session can write the run dir, so each
    step is checked."""
    try:
        if parent_fd is not None and create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=parent_fd)
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as e:
        raise RunnerError("the session's credential dir isn't a plain directory") from e
    if os.fstat(fd).st_uid != os.getuid():
        os.close(fd)
        raise RunnerError("the session's credential dir isn't ours")
    if create:
        os.fchmod(fd, 0o700)
    return fd


def _replace_file(dir_fd: int, name: str, text: str, mode: int) -> None:
    """Atomically put ``text`` at ``name``: a new file beside it, then ``rename``. A planted
    symlink or hard link at ``name`` is replaced, never written through."""
    tmp = f".{name}.new"
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     mode, dir_fd=dir_fd)  # fmt: skip
        try:
            os.write(fd, text.encode())
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as e:  # a name the session planted (a dir, say) in the way
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise RunnerError("the session's credential files can't be updated") from e


def write_session_credentials(run_dir: Path, token: str) -> Path:
    """Put the run's token where its session finds it: ``<run>/creds/token`` and
    ``credential-helper`` (git) and ``creds/gh/hosts.yml`` (``gh``), 0700 dirs and 0600 files.
    Creates them the first time and replaces them in place after (a refresh). Returns the dir.
    ``run_dir`` is daemon-created; everything below it may have been touched by the session,
    so nothing is followed. (The session can still swap ``creds/`` for a dir of its own with
    the same owner, which the checks accept; that gives it nothing, since it can read the
    token it was given anyway, and it would only lose the refresh.)"""
    run_fd = _plain_dir(None, run_dir, create=False)
    try:
        creds_fd = _plain_dir(run_fd, CREDS_DIR, create=True)
        try:
            gh_fd = _plain_dir(creds_fd, "gh", create=True)
            try:
                _replace_file(creds_fd, "token", token + "\n", 0o600)
                _replace_file(creds_fd, "credential-helper", CREDENTIAL_HELPER, 0o700)
                _replace_file(gh_fd, "hosts.yml", _hosts_yml(token), 0o600)
            finally:
                os.close(gh_fd)
        finally:
            os.close(creds_fd)
    finally:
        os.close(run_fd)
    return run_dir / CREDS_DIR


def read_session_token(run_dir: Path) -> str | None:
    """The token last written for the run, or None (no file, not a plain file, too big). For
    revoking a token a crash left behind."""
    try:
        run_fd = _plain_dir(None, run_dir, create=False)
        try:
            creds_fd = _plain_dir(run_fd, CREDS_DIR, create=False)
            try:
                fd = os.open("token", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=creds_fd)  # fmt: skip
            finally:
                os.close(creds_fd)
        finally:
            os.close(run_fd)
    except (OSError, RunnerError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, 4096)
    except OSError:
        return None
    finally:
        os.close(fd)
    token = raw.decode(errors="replace").strip()
    return token if token and "\n" not in token and len(raw) < 4096 else None


async def _read_limited(stream: asyncio.StreamReader, limit: int | None) -> bytes:
    buf = bytearray()
    while chunk := await stream.read(1 << 16):
        buf += chunk
        if limit is not None and len(buf) > limit:
            raise OutputTooLarge
    return bytes(buf)


async def _drain(stream: asyncio.StreamReader) -> None:
    while await stream.read(1 << 16):
        pass


async def _feed(proc: asyncio.subprocess.Process, data: bytes) -> None:
    assert proc.stdin is not None
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.write(data)
        await proc.stdin.drain()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.close()


@dataclass(frozen=True)
class Exec:
    returncode: int
    stdout: bytes
    stderr: str  # redacted, clipped


async def _exec(
    argv: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path | None = None,
    stdin: bytes | None = None,
    timeout_s: float = GIT_TIMEOUT_S,
    redact: Iterable[str] = (),
    max_out: int | None = None,
) -> Exec:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr = proc.stdout, proc.stderr
    assert stdout is not None and stderr is not None

    async def run() -> tuple[bytes, bytes]:
        t_out = asyncio.create_task(_read_limited(stdout, max_out))
        t_err = asyncio.create_task(_read_limited(stderr, 1 << 20))
        t_in = asyncio.create_task(_feed(proc, stdin)) if stdin is not None else None
        tasks: list[asyncio.Future[Any]] = [t_out, t_err, *([t_in] if t_in else [])]
        try:
            out, err = await asyncio.gather(t_out, t_err)
            if t_in:
                await t_in
            await proc.wait()
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return out, err

    try:
        out, err = await asyncio.wait_for(run(), timeout_s)
    except BaseException:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        # A paused pipe never reaches EOF, and the exit isn't reported until it does.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(_drain(stdout), _drain(stderr)), 5)
        await proc.wait()
        raise
    text = Redactor(redact).text(err.decode(errors="replace")).strip()
    return Exec(proc.returncode if proc.returncode is not None else -1, out, text[-_ERR_CLIP:])


@dataclass(frozen=True)
class Prepared:
    """A run's places. ``run_dir`` (under ``runner.work_dir``) holds the session's home, tmp and
    credentials; ``workspace`` is where it works (its cwd): the worktree, or for a run with no
    repo (``repo_name == ""``) the scratch dir."""

    code: str
    repo_name: str
    run_dir: Path
    workspace: Path
    checkout: Path | None = None
    branch: str = ""
    base_branch: str = ""
    base_sha: str = ""
    # RX-1: the base came from the checkout's ``origin/<base>`` (no token, a private repo),
    # not a fresh fetch. Only ``prepare`` sets it; the runner keeps it in the task checkpoint.
    from_checkout: bool = False

    @property
    def scratch(self) -> bool:
        return not self.repo_name

    @property
    def creds_dir(self) -> Path:
        return self.run_dir / CREDS_DIR

    @property
    def exclude_file(self) -> Path:
        """The session's ``core.excludesFile``: the local config linked into the worktree."""
        return self.run_dir / EXCLUDE_FILE


@dataclass(frozen=True)
class PrInfo:
    """A PR the session opened, as ``gh pr list`` reports it."""

    number: int
    url: str
    state: str  # OPEN, CLOSED or MERGED
    base: str
    head_sha: str
    files: int | None


CiState = Literal["green", "red", "pending", "none", "error"]


def _https_url(repo: Repo) -> str:
    return f"https://{repo.remote}"


def _slug_url(slug: str) -> str:
    return f"https://github.com/{slug}"


@dataclass(frozen=True)
class ForkChange:
    """What a fork run's branch holds, read from the daemon-only mirror: the tip commit's
    subject and body (the upstream PR's title and description) and the diff's size."""

    title: str
    body: str
    files: int
    insertions: int
    deletions: int
    head: str  # the branch tip these were read at

    @property
    def stat(self) -> str:
        files = f"{self.files} file" + ("" if self.files == 1 else "s")
        return f"+{self.insertions}/-{self.deletions}, {files}"

    @property
    def what(self) -> str:
        """The external_pr check's text: the size and the head first, so a long title can't
        clip them, and an ok given for one head never stands for another."""
        return f"{self.stat} @ {self.head[:12]}: {self.title}"


@dataclass(frozen=True)
class ForkCheck:
    head_sha: str
    result: CheckResult
    change: ForkChange


# --- the workspace -----------------------------------------------------------------------


class Workspace:
    def __init__(
        self,
        cfg: RunnerConfig,
        conn: sqlite3.Connection,
        *,
        url_for: Callable[[Repo], str] | None = None,
        fork_url_for: Callable[[str], str] | None = None,
        fork_wait_s: float = FORK_READY_WAIT_S,
        guard: Callable[[Sequence[str]], Sequence[str]] | None = None,
    ) -> None:
        """``guard`` wraps every git command run in a checkout (whose ``.git`` a worker can
        write) so it runs under the thin guard (EM-3): by default ``sandbox.wrap``."""
        self.cfg = cfg
        self.conn = conn  # the repo registry (store.repos)
        # The git URL of a repo. Always https without credentials; tests point it at a path.
        self._url_for: Callable[[Repo], str] = url_for or _https_url
        # ... and of the owner's fork of a fork-mode repo, by its ``owner/name``.
        self._fork_url_for: Callable[[str], str] = fork_url_for or _slug_url
        self.fork_wait_s = fork_wait_s
        self._locks: dict[str, asyncio.Lock] = {}
        self._git_bin = str(cfg.git_path)
        self._guard = guard or self._sandbox_guard
        self._guard_prefix: list[str] | None = None

    def _sandbox_guard(self, argv: Sequence[str]) -> Sequence[str]:
        """``argv`` under the thin guard, with no run of its own: whatever a checkout's
        config makes git run (a filter, a hook, an ``ext::`` URL) runs as a worker would,
        never as the daemon. Fails closed when the sandbox isn't there."""
        from resonant.sandbox import SandboxParams, default_deny_exec, sandbox_exec_available
        from resonant.sandbox import wrap as sandbox_wrap

        if self._guard_prefix is None:
            if not sandbox_exec_available():
                raise RunnerError("the sandbox isn't available, so git can't run in a checkout")
            # the control-dir rules too, with no run's dir as its own: what a planted filter
            # runs here can't read, replace or reach any run's socket or token
            ctl = self.cfg.work_dir / ".ctl"
            params = SandboxParams(
                run_dir=self.cfg.work_dir / ".guard", cache_dir=self.cfg.cache_dir,
                secrets_dir=self.cfg.secrets_dir, work_dir=self.cfg.work_dir,
                deny_exec=default_deny_exec(), deny_read=tuple(self.cfg.deny_read),
                ctl_root=ctl, ctl_dir=ctl / "_none",
            )  # fmt: skip
            self._guard_prefix = sandbox_wrap(["/usr/bin/true"], params)[:-1]
        return [*self._guard_prefix, *argv]

    # -- locks and paths --

    def mirror_lock(self, repo_name: str) -> asyncio.Lock:
        """The per-repo mirror lock: every mirror fetch (refresh, a PR head, a fork branch)
        takes it. Not re-entrant, and not the checkout lock (``checkout_lock``)."""
        return self._locks.setdefault(repo_name, asyncio.Lock())

    def mirror_path(self, repo_name: str) -> Path:
        return self.cfg.mirror_dir / f"{repo_name}.git"

    def _repo(self, repo_name: str) -> Repo:
        repo = repo_store.find(self.conn, repo_name)
        # Only the registry name: it is the mirror's dir name, never a slug or alias.
        if repo is None or repo.name != repo_name:
            raise RunnerError(f"{repo_name} isn't a registered repo")
        return repo

    def _mirror_git(self, repo_name: str) -> GitRunner:
        mirror = self.mirror_path(repo_name)

        async def git(*args: str, stdin: bytes | None = None, max_out: int | None = None) -> bytes:
            res = await _exec(
                [self._git_bin, "-C", str(mirror), *args],
                env=git_env(),
                stdin=stdin,
                max_out=max_out,
            )
            if res.returncode != 0:
                raise GitError(args, res.returncode, res.stderr)
            return res.stdout

        return git

    async def _git(
        self,
        args: Sequence[str],
        *,
        token: str | None = None,
        cwd: Path | None = None,
        stdin: bytes | None = None,
        extra: Sequence[tuple[str, str]] = (),
        max_out: int | None = None,
    ) -> bytes:
        """Run git. With a ``token``, it's written to a private file for the length of this
        call and handed over by a credential helper, never by env or argv."""
        with contextlib.ExitStack() as stack:
            helper = None
            if token:
                tmp = stack.enter_context(secret_dir(self.cfg.secrets_dir))
                helper = write_git_credentials(tmp, token)
            res = await _exec(
                [self._git_bin, *args],
                env=git_env(helper, extra),
                cwd=cwd,
                stdin=stdin,
                redact=[token] if token else (),
                max_out=max_out,
            )
        if res.returncode != 0:
            raise GitError(args, res.returncode, res.stderr)
        return res.stdout

    async def _ensure_mirror(self, repo_name: str) -> Path:
        mirror = self.mirror_path(repo_name)
        if not (mirror / "HEAD").exists():
            mirror.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await self._git(["init", "--bare", "-q", str(mirror)])
        return mirror

    async def _refresh(self, repo_name: str, token: str | None) -> None:
        """Fetch the remote's branches into the mirror. Caller holds the mirror lock. With no
        ``token`` the fetch is anonymous: a public repo works, a private one fails.

        No fsckObjects here: the remote is the owner's own repo, and old histories often
        fail fsck. The untrusted input (a run's pushed PR head) is fetched with fsck."""
        repo = self._repo(repo_name)
        mirror = await self._ensure_mirror(repo_name)
        await self._git(
            [
                "-C",
                str(mirror),
                "fetch",
                "--prune",
                "--no-tags",
                "--no-write-fetch-head",
                self._url_for(repo),
                "+refs/heads/*:refs/heads/*",
            ],
            token=token,
        )

    async def refresh_mirror(self, repo_name: str, *, github_token: str) -> None:
        """Bring the mirror up to date."""
        async with self.mirror_lock(repo_name):
            await self._refresh(repo_name, github_token)

    # -- prepare --

    async def repo_has_ci(
        self, repo_name: str, *, github_token: str | None = None, refresh: bool = True
    ) -> bool:
        """Whether the repo's base branch has a workflow that runs on ``pull_request``
        (the runner only works in repos with CI). Fails closed. With ``refresh`` the mirror
        is brought up to date first; this is also what a ``--check`` row per repo can use."""
        repo = self._repo(repo_name)
        async with self.mirror_lock(repo_name):
            if refresh:
                if github_token is None:
                    raise ValueError("refresh needs the GitHub token")
                await self._refresh(repo_name, github_token)
            try:
                return await ci_present(self._mirror_git(repo_name), f"refs/heads/{repo.base}")
            except GitError:
                return False

    # -- checkouts, worktrees and scratch dirs --

    def checkout_lock(self, repo_name: str) -> asyncio.Lock:
        """The per-repo checkout lock: clone, fetch, worktree add and remove each take it (two
        runs on one repo each get a worktree, one after the other). Shared by every
        ``Workspace`` on the running loop. Not re-entrant."""
        locks = _CHECKOUT_LOCKS.setdefault(asyncio.get_running_loop(), {})
        return locks.setdefault(repo_name.lower(), asyncio.Lock())

    def checkout_path(self, repo: Repo) -> Path:
        """The owner's checkout: the repo's ``path``, else ``<code_dir>/<name>``."""
        return repo.path if repo.path is not None else self.cfg.code_dir / repo.name

    def worktree_path(self, repo_name: str, code: str) -> Path:
        return self.cfg.code_dir / WORKTREES / repo_name / code

    def scratch_path(self, code: str) -> Path:
        return self.cfg.code_dir / SCRATCH / code

    def _run_dir(self, code: str) -> Path:
        """``<work_dir>/<code>``, made afresh: ``home``, ``tmp`` and an empty ``creds``. A
        leftover (a crash before the session started) is daemon-owned and is rebuilt."""
        run_dir = self.cfg.work_dir / code
        run_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        rmtree_nofollow(run_dir)
        run_dir.mkdir(mode=0o700)
        for sub in ("home", "tmp", CREDS_DIR):
            (run_dir / sub).mkdir(mode=0o700)
        return run_dir

    async def _in_checkout(
        self,
        checkout: Path,
        *args: str,
        max_out: int | None = None,
        env: dict[str, str] | None = None,
    ) -> bytes:
        """Daemon git in a checkout (or one of its worktrees): under the guard, with no token
        and with the checkout's risky settings overridden. A worker can write the checkout's
        ``.git`` (config, hooks, attributes), so nothing it says there may run as the daemon
        (docs/employee-plan.md, EM-3 S2)."""
        argv = self._guard([self._git_bin, "-C", str(checkout), *args])
        res = await _exec(argv, env={**git_env(None, _CHECKOUT_CONFIG), **(env or {})},
                          max_out=max_out)  # fmt: skip
        if res.returncode != 0:
            raise GitError(args, res.returncode, res.stderr)
        return res.stdout

    async def ensure_checkout(
        self, repo: Repo, token: str | None, *, fork: str | None = None
    ) -> Path:
        """The owner's checkout of ``repo``, cloned (https, no credentials in the URL; the
        daemon's token by credential helper for this call only) when nothing is there. An
        existing one must be a git checkout whose ``origin`` is this repo. Caller holds the
        checkout lock.

        For a fork-mode repo, ``fork`` is the owner's fork (``owner/name``): the checkout's
        ``origin`` is the fork, and a fresh clone also gets an ``upstream`` remote for the
        original. Both are public, so no token is used."""
        path = self.checkout_path(repo)
        self._check_checkout_place(path)
        url = self._url_for(repo) if fork is None else self._fork_url_for(fork)
        want = repo.slug if fork is None else fork
        if not await asyncio.to_thread(os.path.lexists, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            # cloned beside the final place and renamed in, so a clone cut short (a cancel, a
            # kill) never leaves a half checkout in the way of the next run
            tmp = Path(tempfile.mkdtemp(prefix=f".{path.name}.clone-", dir=path.parent))
            try:
                await self._clone(url, tmp, token, fork=fork is not None)
                if fork is not None:  # ours, just made: the original as ``upstream``
                    await self._in_checkout(tmp, "remote", "add", "upstream", self._url_for(repo))
                os.chmod(tmp, 0o755)  # noqa: S103  # a normal checkout, not mkdtemp's 0700
                os.rename(tmp, path)
            except GitError as e:
                raise RunnerError(f"cloning {want} failed: {e.message[-120:]}") from None
            finally:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(rmtree_nofollow, tmp)
            return path
        not_ours = RunnerError(f"{path} is in the way: it isn't a git checkout of {want}")
        if not await asyncio.to_thread(path.is_dir):
            raise not_ours
        if not await asyncio.to_thread(os.path.lexists, path / ".git"):
            raise not_ours
        if not await asyncio.to_thread(os.path.isdir, path / ".git"):
            # a linked worktree or a submodule: its git dir lives elsewhere
            raise RunnerError(f"{path} isn't a plain checkout (its .git isn't a directory)")
        try:
            top = (await self._in_checkout(path, "rev-parse", "--show-toplevel")).decode().strip()
            origin = await self._in_checkout(path, "config", "--get", "remote.origin.url")
        except GitError:
            raise not_ours from None
        if not await asyncio.to_thread(os.path.samefile, top, path):
            raise not_ours
        origin_url = origin.decode().strip()
        slug = remote_slug(origin_url)
        if origin_url != url and (slug is None or slug.lower() != want.lower()):
            raise RunnerError(f"{path}'s origin isn't {want}")
        return path

    async def _clone(self, url: str, dest: Path, token: str | None, *, fork: bool) -> None:
        """``git clone`` into an empty ``dest``. A fork GitHub is still making isn't there
        yet: the clone is tried again a few times, waiting longer each time."""
        wait = self.fork_wait_s
        for attempt in range(FORK_READY_TRIES if fork else 1):
            try:
                await self._git(["clone", "-q", "--", url, str(dest)], token=token,
                                extra=_CHECKOUT_CONFIG)  # fmt: skip
                return
            except GitError as e:
                missing = re.search(r"not found|does not appear to be a git repository|empty",
                                    e.message, re.IGNORECASE)  # fmt: skip
                if attempt + 1 >= (FORK_READY_TRIES if fork else 1) or not missing:
                    raise
            await asyncio.to_thread(rmtree_nofollow, dest)  # a failed clone may leave files
            await asyncio.to_thread(dest.mkdir, mode=0o700)
            await asyncio.sleep(wait)
            wait *= 2

    def _check_checkout_place(self, path: Path) -> None:
        """A checkout (a repo's ``path``) may not sit in or over Resonant's own dirs: the run,
        mirror, secrets and cache dirs, or the worktree and scratch roots."""
        cfg = self.cfg
        own = (cfg.work_dir, cfg.mirror_dir, cfg.secrets_dir, cfg.cache_dir,
               cfg.code_dir / WORKTREES, cfg.code_dir / SCRATCH)  # fmt: skip
        if not path.is_absolute() or any(
            path.is_relative_to(d) or d.is_relative_to(path) for d in own
        ):
            raise RunnerError(f"{path} can't be a checkout: it overlaps Resonant's own dirs")

    async def prepare(
        self,
        code: str,
        repo_name: str,
        ask: str,
        *,
        github_token: str | None,
        fork: str | None = None,
    ) -> Prepared:
        """Make the run's places: the checkout (cloned on first use) fetched, a worktree on a
        new branch from ``origin/<base>`` with the checkout's local config linked in, and the
        run dir. Notes whether the base has CI. All of it before the session exists; the
        checkout's working tree and current branch are never touched.

        For a fork-mode repo, ``fork`` is the owner's fork (``ensure_fork``): the checkout is
        the fork's (cloned with no token: it is public), and the worktree starts from the
        original's base, which comes through the mirror like a push repo's, into
        ``refs/remotes/upstream/<base>``. CI isn't recorded (theirs runs on their side).

        With no ``github_token`` (RX-1: the daemon's token isn't set yet) the mirror and a
        first clone are fetched anonymously, which works for a public repo. When GitHub
        refuses that (a private repo: auth or not found; any other failure, like the network,
        still fails the run) and the owner already has a checkout, the run starts from the
        checkout's own ``origin/<base>`` as last fetched (read under the guard; never the
        local ``<base>``, which may hold unpushed work) and ``from_checkout`` says so: such a
        run only ever commits locally, nothing it makes is merged by Resonant, and CI isn't
        recorded.

        A leftover worktree or run dir (a crash before the session started) is removed and
        rebuilt."""
        if not RUN_CODE.fullmatch(code):
            raise RunnerError(f"bad run code {code!r}")
        repo = self._repo(repo_name)
        branch = branch_name(code, ask)
        worktree = self.worktree_path(repo.name, code)
        # The base comes through the daemon-only mirror: the token is only ever given to the
        # mirror's fetch (a checkout's http.* or credential config can't reach it), and the
        # base sha, CI and the tracked names are read there, not from a worker-writable .git.
        mirror_git = self._mirror_git(repo.name)
        from_mirror = True
        base_sha = ""
        tracked: list[str] = []
        async with self.mirror_lock(repo.name):
            try:
                await self._refresh(repo.name, github_token)
            except GitError as e:
                fetch_failed = RunnerError(
                    f"fetching {repo.base} of {repo.slug} failed: {e.message[-120:]}"
                )
                if github_token is not None or fork is not None:
                    raise fetch_failed from None
                if not _ANON_REFUSED.search(e.message):  # the network, not a private repo
                    raise fetch_failed from None
                if not await asyncio.to_thread(os.path.lexists, self.checkout_path(repo)):
                    raise RunnerError(self.no_clone_text(repo)) from None
                from_mirror = False
            if from_mirror:
                try:
                    out = await mirror_git("rev-parse", "--verify", "-q",
                                           f"refs/heads/{repo.base}^{{commit}}")  # fmt: skip
                except GitError:
                    raise RunnerError(f"{repo.base} isn't a branch of {repo_name}") from None
                base_sha = out.decode().strip()
                try:
                    has_ci = await ci_present(mirror_git, base_sha)
                except GitError:
                    has_ci = False
                top = await mirror_git("ls-tree", "-z", "--name-only", base_sha)
                tracked = [n for n in top.decode(errors="replace").split("\0") if n]
                # No CI is allowed (docs/repos-plan.md D5): the registry records it, the run's
                # reply says so, and merging is what's refused.
                if fork is None and (not has_ci or repo.ci is None):
                    with atomic(self.conn):
                        repo_store.set_ci(self.conn, repo.slug, has_ci)
        async with self.checkout_lock(repo.name):
            # a fork's checkout is cloned from the (public) fork with no token
            checkout = await self.ensure_checkout(
                repo, github_token if fork is None else None, fork=fork
            )
            if from_mirror:
                tracking = f"refs/remotes/{'origin' if fork is None else 'upstream'}/{repo.base}"
                try:  # tokenless, from the mirror, under the guard
                    await self._in_checkout(
                        checkout, "fetch", "-q", "--no-tags", "--no-write-fetch-head",
                        str(self.mirror_path(repo.name)), f"+refs/heads/{repo.base}:{tracking}",
                        env={"GIT_ALLOW_PROTOCOL": "file"},
                    )  # fmt: skip
                except GitError as e:
                    raise RunnerError(
                        f"fetching {repo.base} into {checkout} failed: {e.message[-120:]}"
                    ) from None
            else:
                base_sha, tracked = await self._checkout_base(checkout, repo)

            # a leftover from a crash (its branch is reset by ``-B`` below)
            await self._remove_worktree(code, worktree, checkout, None)
            run_dir = await asyncio.to_thread(self._run_dir, code)
            try:
                worktree.parent.mkdir(parents=True, exist_ok=True)
                # --no-checkout: no filter or attribute of the checkout's config runs here;
                # the checkout itself is the guarded reset below
                await self._in_checkout(
                    checkout, "worktree", "add", "-q", "--no-checkout", "--no-track", "-B",
                    branch, str(worktree), base_sha,
                )  # fmt: skip
                linked = await asyncio.to_thread(link_local_config, checkout, worktree, tracked)
                (run_dir / EXCLUDE_FILE).write_text("".join(f"/{n}\n" for n in linked))
                await self._in_checkout(worktree, "reset", "-q", "--hard", base_sha)
            except BaseException:
                with contextlib.suppress(Exception):  # never hide why prepare failed
                    await self._remove_worktree(code, worktree, checkout, branch, base_sha)
                    await asyncio.to_thread(rmtree_nofollow, run_dir)
                raise
        return Prepared(code, repo.name, run_dir, worktree, checkout, branch, repo.base, base_sha,
                        from_checkout=not from_mirror)  # fmt: skip

    async def _checkout_base(self, checkout: Path, repo: Repo) -> tuple[str, list[str]]:
        """The base commit and its top-level names from the owner's checkout (the daemon has
        no token to fetch a private repo with): ``origin/<base>`` as the checkout last fetched
        it. Under the guard, like every daemon git command in a checkout. The ref lives in a
        ``.git`` an earlier worker could write, which is acceptable only because such a run
        commits locally and nothing it makes is pushed or merged by Resonant."""
        ref = f"refs/remotes/origin/{repo.base}"
        try:
            out = await self._in_checkout(checkout, "rev-parse", "--verify", "-q",
                                          f"{ref}^{{commit}}")  # fmt: skip
        except GitError:
            out = b""
        sha = out.decode().strip()
        if not _SHA.fullmatch(sha):
            raise RunnerError(
                f"{checkout} has no origin/{repo.base} to start from, and fetching it needs "
                f"the daemon's GitHub token (runner/github_token)"
            )
        top = await self._in_checkout(checkout, "ls-tree", "-z", "--name-only", sha)
        return sha, [n for n in top.decode(errors="replace").split("\0") if n]

    def no_clone_text(self, repo: Repo) -> str:
        """Why a run on ``repo`` can't start without the daemon's token: there is no checkout
        and the repo can't be cloned anonymously (it is private, or not reachable)."""
        path = self.checkout_path(repo)
        return (
            f"I can't work on {repo.slug} yet: there's no checkout at {path}, and cloning it "
            f"needs the daemon's GitHub token (runner/github_token). On the Mini: "
            f"{GITHUB_TOKEN_HOW}, or clone it to {path} yourself."
        )

    async def can_start_tokenless(self, repo_name: str) -> bool:
        """Whether a run on the repo can be prepared without the daemon's token: the owner
        has a checkout, or the repo answers an anonymous ``git ls-remote`` (it is public)."""
        repo = self._repo(repo_name)
        if await asyncio.to_thread(os.path.lexists, self.checkout_path(repo)):
            return True
        try:
            res = await _exec(
                [self._git_bin, "ls-remote", "-q", "--exit-code", "--heads", "--",
                 self._url_for(repo), repo.base],
                env=git_env(), timeout_s=GH_TIMEOUT_S,
            )  # fmt: skip
        except (OSError, TimeoutError):
            return False
        return res.returncode == 0

    async def local_commits(self, p: Prepared) -> int:
        """How many commits the run's local branch has beyond its base, read in the owner's
        checkout under the guard (a run that can't push leaves its work there)."""
        if p.checkout is None:
            return 0
        try:
            out = await self._in_checkout(
                p.checkout, "rev-list", "--count", f"{p.base_sha}..refs/heads/{p.branch}"
            )
        except GitError:
            return 0
        return int(out.decode().strip() or 0)

    async def prepare_scratch(self, code: str) -> Prepared:
        """A run with no repo: the run dir and an empty ``<code_dir>/.scratch/<code>``."""
        if not RUN_CODE.fullmatch(code):
            raise RunnerError(f"bad run code {code!r}")
        scratch = self.scratch_path(code)
        run_dir = await asyncio.to_thread(self._run_dir, code)
        try:
            scratch.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await asyncio.to_thread(self._remove_checked, scratch)
            scratch.mkdir(mode=0o700)
        except BaseException:
            await asyncio.to_thread(rmtree_nofollow, run_dir)
            raise
        return Prepared(code, "", run_dir, scratch)

    def _workspace_kind(self, path: Path, code: str) -> str | None:
        """``worktree`` or ``scratch`` when ``path`` is exactly where this module puts the
        run's work (``<code_dir>/.worktrees/<name>/<code>``, ``<code_dir>/.scratch/<code>``),
        with no symlink on the way down from ``code_dir``; else None."""
        if not RUN_CODE.fullmatch(code) or not path.is_absolute() or path.name != code:
            return None
        root = self.cfg.code_dir
        if path.parent == root / SCRATCH:
            kind, between = "scratch", [root / SCRATCH]
        elif path.parent.parent == root / WORKTREES and repo_store.NAME.fullmatch(path.parent.name):
            kind, between = "worktree", [root / WORKTREES, path.parent]
        else:
            return None
        if any(p.is_symlink() for p in between):
            return None
        return kind

    def _remove_checked(self, path: Path) -> bool:
        """Delete a run's worktree or scratch dir found at a checked place, without following
        any link inside it (a linked ``.env`` is unlinked, its target kept)."""
        if self._workspace_kind(path, path.name) is None:
            raise RunnerError(f"refusing to delete {path}: not a run's worktree or scratch dir")
        return rmtree_nofollow(path)

    async def _remove_worktree(
        self,
        code: str,
        worktree: Path,
        checkout: Path | None,
        branch: str | None,
        expect: str | None = None,
    ) -> bool:
        """``git worktree remove --force`` in the checkout; whatever git couldn't remove is
        deleted (checked place, no links followed) along with the checkout's admin entry for
        that one worktree. Then the run's own ``resonant/<code>-…`` branch is deleted, but only
        while it still points at ``expect`` (what the run left there) and no worktree has it
        checked out: a branch the owner checked out or committed to is kept. Caller holds the
        checkout lock. The checkout itself is never deleted."""
        if self._workspace_kind(worktree, code) != "worktree":
            raise RunnerError(f"refusing to remove {worktree}: not a run's worktree")
        usable = checkout is not None and await asyncio.to_thread(os.path.isdir, checkout / ".git")
        removed = False
        # No ``git worktree remove``: it would read the worktree's status (filters,
        # fsmonitor) under the checkout's config. The dir goes (no links followed) and then
        # only its admin entry.
        if await asyncio.to_thread(os.path.lexists, worktree):
            removed = await asyncio.to_thread(self._remove_checked, worktree) or removed
        if usable and checkout is not None:
            await asyncio.to_thread(_drop_admin_entry, checkout, worktree)
        own = f"resonant/{code.lower()}-"
        if not (usable and checkout is not None and branch and expect):
            return removed
        if not (branch.startswith(own) and _SHA.fullmatch(expect)):
            return removed
        with contextlib.suppress(GitError):
            listed = await self._in_checkout(checkout, "worktree", "list", "--porcelain")
            if f"branch refs/heads/{branch}\n" not in listed.decode(errors="replace") + "\n":
                # compare-and-delete: refused when the branch moved since the run
                await self._in_checkout(checkout, "update-ref", "-d", f"refs/heads/{branch}",
                                        expect)  # fmt: skip
        return removed

    async def remove_workspace(
        self,
        code: str,
        *,
        workspace: str | Path | None,
        checkout: str | Path | None = None,
        branch: str | None = None,
        expect: str | None = None,
    ) -> bool:
        """Remove a run's worktree (and its branch) or scratch dir: on cancel, on merge and
        ``runner.keep_days`` after the run ended. Only the exact place the run recorded under
        ``code_dir`` is touched; anything else raises ``RunnerError``. The branch goes only
        while it still points at ``expect``. False if nothing was there."""
        if workspace is None:
            return False
        path = Path(workspace)
        kind = self._workspace_kind(path, code)
        if kind == "scratch":
            return await asyncio.to_thread(rmtree_nofollow, path)
        if kind != "worktree":
            raise RunnerError(f"refusing to remove {path}: not a run's worktree or scratch dir")
        async with self.checkout_lock(path.parent.name):
            return await self._remove_worktree(
                code, path, None if checkout is None else Path(checkout), branch, expect
            )

    # -- gh --

    async def gh(
        self, args: Sequence[str], secrets: RunnerSecrets, *, stdin: bytes | None = None
    ) -> Exec:
        """Run ``gh`` with the token in a throwaway config dir (for the repo resolver)."""
        return await self._gh(args, secrets, stdin=stdin)

    async def _gh(
        self,
        args: Sequence[str],
        secrets: RunnerSecrets,
        *,
        stdin: bytes | None = None,
        public: bool = False,
    ) -> Exec:
        """``gh`` with the daemon's token, or with ``public`` the ``public_repo`` token (fork
        mode only: making the fork, and opening or closing the upstream PR)."""
        token = secrets.github_token
        if token is None and not public:
            raise RunnerError(needs_github_token("Asking GitHub"))
        if public:
            if not secrets.github_public_token:
                raise RunnerError(FORK_NEEDS_PUBLIC_REPO)
            token = secrets.github_public_token
        # Everything gh reads or writes lives in one throwaway dir; the token is in its
        # hosts.yml (which gh prefers to the Keychain) and never in env or argv.
        if token is None:  # unreachable: both cases above raise
            raise RunnerError(needs_github_token("Asking GitHub"))
        with secret_dir(self.cfg.secrets_dir) as tmp:
            config = write_gh_config(tmp, token)
            env = {
                "PATH": _SAFE_PATH,
                "HOME": str(tmp),
                "GH_CONFIG_DIR": str(config),
                "XDG_CONFIG_HOME": str(tmp),
                "XDG_CACHE_HOME": str(tmp / "cache"),
                "XDG_STATE_HOME": str(tmp / "state"),
                "GH_PROMPT_DISABLED": "1",
                "GH_NO_UPDATE_NOTIFIER": "1",
                "NO_COLOR": "1",
            }
            return await _exec(
                [str(self.cfg.gh_path), *args],
                env=env,
                cwd=tmp,
                stdin=stdin,
                timeout_s=GH_TIMEOUT_S,
                redact=secrets.values(),
            )

    # -- the base branch --

    async def base_protected(self, repo_name: str, secrets: RunnerSecrets) -> bool | None:
        """Whether the base branch has protection (``GET /repos/{o}/{r}/branches/{base}``,
        daemon token). None when GitHub can't say."""
        repo = self._repo(repo_name)
        res = await self._gh(["api", f"repos/{repo.slug}/branches/{repo.base}"], secrets)
        if res.returncode != 0:
            return None
        try:
            value = json.loads(res.stdout).get("protected")
        except (ValueError, AttributeError):
            return None
        return value if isinstance(value, bool) else None

    async def base_pushes(
        self, repo_name: str, secrets: RunnerSecrets, *, actor: str, since: datetime
    ) -> list[str]:
        """The commits ``actor`` (the app's bot) put on the base branch since ``since``: the
        ``after`` sha of each push, force-push, branch deletion and PR merge in the repo's
        activity feed. One query per activity type, filtered by actor on GitHub's side, so a
        busy branch can't push the bot's event out of the first page. Raises ``RunnerError``
        when the feed can't be read or an event can't be understood: it can't be shown clean."""
        repo = self._repo(repo_name)
        found: list[str] = []
        for kind in _PUSH_KINDS:
            query = (
                f"ref=refs/heads/{repo.base}&actor={quote(actor)}&activity_type={kind}&per_page=100"
            )
            res = await self._gh(["api", f"repos/{repo.slug}/activity?{query}"], secrets)
            if res.returncode != 0:
                raise RunnerError(f"couldn't read {repo.base}'s activity: {res.stderr[-120:]}")
            try:
                events = json.loads(res.stdout)
            except ValueError:
                events = None
            if not isinstance(events, list):
                raise RunnerError(f"{repo.base}'s activity couldn't be read")
            for event in cast(list[Any], events):
                try:
                    login = str(event["actor"]["login"])
                    when = datetime.fromisoformat(str(event["timestamp"]))
                    event_kind, after = str(event["activity_type"]), str(event["after"])
                except (KeyError, TypeError, ValueError):
                    raise RunnerError(f"{repo.base}'s activity couldn't be read") from None
                if login.lower() == actor.lower() and when >= since and event_kind in _PUSH_KINDS:
                    found.append(after if _SHA.fullmatch(after) else "?")
        return list(dict.fromkeys(found))

    # -- what the session left --

    async def find_pr(self, repo_name: str, branch: str, secrets: RunnerSecrets) -> PrInfo | None:
        """The PR the session opened for ``branch`` (``gh pr list --head``), or None. A PR
        from a fork is ignored, and an open one wins over a closed one."""
        repo = self._repo(repo_name)
        listed = await self._gh(
            ["pr", "list", "--repo", repo.slug, "--head", branch, "--state", "all", "--json",
             "number,url,state,baseRefName,headRefOid,isCrossRepository,changedFiles"],
            secrets,
        )  # fmt: skip
        if listed.returncode != 0:
            raise RunnerError(f"gh pr list failed: {listed.stderr[-120:]}")
        try:
            items: list[Any] = json.loads(listed.stdout)
        except ValueError:
            raise RunnerError("gh pr list printed something unreadable") from None
        found: list[PrInfo] = []
        for item in items:
            try:
                url, head = str(item["url"]), str(item["headRefOid"])
                files = item.get("changedFiles")
                if item.get("isCrossRepository") or not _PR_URL.fullmatch(url):
                    continue
                if not _SHA.fullmatch(head):
                    continue
                found.append(
                    PrInfo(
                        int(item["number"]),
                        url,
                        str(item["state"]),
                        str(item["baseRefName"]),
                        head,
                        files if isinstance(files, int) else None,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        found.sort(key=lambda pr: (pr.state != "OPEN", -pr.number))
        return found[0] if found else None

    async def pushed_commits(self, p: Prepared, secrets: RunnerSecrets) -> int:
        """How many commits ``p.branch`` has on the remote beyond the run's base (0 if the
        branch was never pushed)."""
        async with self.mirror_lock(p.repo_name):
            await self._refresh(p.repo_name, secrets.github_token)
            try:
                out = await self._mirror_git(p.repo_name)(
                    "rev-list", "--count", f"{p.base_sha}..refs/heads/{p.branch}"
                )
            except GitError:
                return 0
        return int(out.decode().strip() or 0)

    async def ci_state(self, repo_name: str, pr_number: int, secrets: RunnerSecrets) -> CiState:
        """The PR's checks: ``gh pr checks`` exits 0 (green), 8 (pending) or 1 (red, or none
        reported)."""
        repo = self._repo(repo_name)
        checks = await self._gh(["pr", "checks", str(pr_number), "--repo", repo.slug], secrets)
        if checks.returncode == 0:
            return "green"
        if checks.returncode == 8:
            return "pending"
        if checks.returncode == 1:
            return "none" if "no checks reported" in checks.stderr else "red"
        return "error"

    async def check_pr(
        self,
        repo_name: str,
        pr_number: int,
        branch: str,
        secrets: RunnerSecrets,
        *,
        expect_head: str | None = None,
        allow_github: bool | None = None,
    ) -> CheckResult:
        """The checks of docs/repos-plan.md D10, on the PR as it is now: fetch its head branch
        into the mirror (with fsck: the session wrote it), then run ``run_checks`` over
        ``merge-base..head`` plus a token scan of the PR's title and body. Raises
        ``MergeRefused`` (the PR isn't mergeable as it stands) or ``CheckFailed`` (the checks
        refuse it). With ``expect_head`` the PR must still be at that commit.
        ``allow_github`` overrides the repo's ``allow_github_changes`` for these checks only
        (the add-CI merge: its one file is under ``.github/`` and is checked by hash)."""
        repo = self._repo(repo_name)
        policy = repo if allow_github is None else replace(repo, allow_github_changes=allow_github)
        if pr_number < 1 or not re.fullmatch(r"[\w./-]+", branch) or ".." in branch:
            raise MergeRefused("bad PR reference")
        viewed = await self._gh(
            ["pr", "view", str(pr_number), "--repo", repo.slug, "--json", _PR_FIELDS], secrets
        )
        if viewed.returncode != 0:
            raise MergeRefused(f"couldn't read the PR (gh pr view: {viewed.stderr[-120:]})")
        try:
            pr = json.loads(viewed.stdout)
            state, head_ref, head, base = (
                str(pr[k]) for k in ("state", "headRefName", "headRefOid", "baseRefName")
            )
            cross, title, body = bool(pr["isCrossRepository"]), str(pr["title"]), str(pr["body"])
        except (ValueError, KeyError, TypeError):
            raise MergeRefused("the PR couldn't be read") from None
        if state != "OPEN":
            raise MergeRefused(f"PR #{pr_number} is {state.lower()}, not open")
        if cross or head_ref != branch:
            raise MergeRefused(f"PR #{pr_number} isn't the run's branch")
        if base != repo.base:
            raise MergeRefused(f"PR #{pr_number} targets {base}, not {repo.base}")
        if not _SHA.fullmatch(head):
            raise MergeRefused("the PR's head commit couldn't be read")
        if expect_head is not None and head != expect_head:
            raise MergeRefused(
                f"PR #{pr_number} changed since the run finished (head {head[:7]}, "
                f"was {expect_head[:7]})"
            )
        ref = f"refs/runs/pr-{pr_number}"
        mirror = self.mirror_path(repo_name)
        async with self.mirror_lock(repo_name):
            await self._ensure_mirror(repo_name)
            try:
                await self._git(
                    ["-C", str(mirror), "-c", "transfer.fsckObjects=true",
                     "-c", "fetch.fsckObjects=true", "fetch", "-q", "--no-tags",
                     "--no-write-fetch-head", self._url_for(repo), f"+refs/heads/{branch}:{ref}"],
                    token=secrets.github_token,
                )  # fmt: skip
            except GitError as e:
                raise MergeRefused(f"fetching the PR's branch failed: {e.message[-120:]}") from None
            await self._refresh(repo_name, secrets.github_token)  # the base as it is now
            git = self._mirror_git(repo_name)
            try:
                fetched = (await git("rev-parse", "--verify", ref)).decode().strip()
                base_tip = (await git("rev-parse", "--verify", f"refs/heads/{repo.base}")).decode()
                merge_base = (await git("merge-base", base_tip.strip(), fetched)).decode().strip()
            except GitError:
                raise MergeRefused("the PR's branch shares no history with its base") from None
            if fetched != head:
                raise MergeRefused(f"PR #{pr_number} moved while it was being checked")
            result = await run_checks(
                git,
                base_sha=merge_base,
                head_sha=fetched,
                branch=branch,
                repo=policy,
                secrets=secrets.values(),
            )
        check_texts(secrets.values(), pr_title=title, pr_body=body)
        return result

    # -- fork mode (docs/employee-plan.md D6, docs/repos-plan.md D3) --

    async def ensure_fork(self, repo_name: str, secrets: RunnerSecrets) -> str:
        """The owner's fork of a fork-mode repo, made if it isn't there yet
        (``POST /repos/{upstream}/forks`` with the ``public_repo`` token; GitHub answers with
        the existing fork when there is one). Then read back and checked: a public fork whose
        parent is the repo. Returns its ``owner/name``. Raises ``RunnerError``."""
        repo = self._repo(repo_name)
        if repo.mode != "fork":
            raise RunnerError(f"{repo.slug} isn't a fork-mode repo")
        made = await self._gh(
            ["api", "-X", "POST", f"repos/{repo.slug}/forks", "-F", "default_branch_only=true"],
            secrets,
            public=True,
        )
        if made.returncode != 0:
            raise RunnerError(f"couldn't fork {repo.slug}: {made.stderr[-120:]}")
        try:
            slug = str(json.loads(made.stdout)["full_name"])
        except (ValueError, KeyError, TypeError):
            raise RunnerError(f"GitHub's answer to forking {repo.slug} wasn't readable") from None
        if not repo_store.SLUG.fullmatch(slug) or {".", ".."} & set(slug.split("/")):
            raise RunnerError(f"GitHub's answer to forking {repo.slug} wasn't readable")
        seen = await self._gh(["api", f"repos/{slug}"], secrets, public=True)
        try:
            info: Any = json.loads(seen.stdout) if seen.returncode == 0 else None
            parent = str(info["parent"]["full_name"])
            ok = (
                info["fork"] is True and info["private"] is False and str(info["full_name"]) == slug
            )
        except (ValueError, KeyError, TypeError):
            ok, parent = False, ""
        if not ok or parent.lower() != repo.slug.lower():
            raise RunnerError(f"{slug} isn't a public fork of {repo.slug}")
        return slug

    def _fork_ref(self, code: str) -> str:
        if not RUN_CODE.fullmatch(code):
            raise RunnerError(f"bad run code {code!r}")
        return f"refs/runs/fork-{code}"

    async def fork_change(self, repo_name: str, code: str, base_sha: str) -> ForkChange:
        """The upstream PR's title, description and size for a fork run's branch, as the
        last ``check_fork`` fetched it into the mirror. Reads only the mirror."""
        git = self._mirror_git(repo_name)
        ref = self._fork_ref(code)
        if not _SHA.fullmatch(base_sha):
            raise RunnerError("bad base commit")
        async with self.mirror_lock(repo_name):
            head = (await git("rev-parse", "--verify", f"{ref}^{{commit}}")).decode().strip()
            message = (await git("log", "-1", "--format=%s%x00%b", ref)).decode(errors="replace")
            stat = (await git("diff", "--shortstat", f"{base_sha}...{ref}")).decode()
        title, _, body = message.partition("\0")
        numbers = {
            unit: int(m[1]) if (m := re.search(rf"(\d+) {unit}", stat)) else 0
            for unit in ("file", "insertion", "deletion")
        }
        return ForkChange(
            title=title.strip()[:200] or "Changes from Resonant",
            body=body.strip()[:UPSTREAM_BODY_MAX],
            files=numbers["file"],
            insertions=numbers["insertion"],
            deletions=numbers["deletion"],
            head=head,
        )

    async def check_fork(
        self, repo_name: str, code: str, fork: str, branch: str, base_sha: str,
        secrets: RunnerSecrets,
    ) -> ForkCheck:  # fmt: skip
        """The pre-merge checks, for a fork run's branch before its upstream PR: the branch
        is fetched from the fork into the daemon-only mirror (with fsck; the session wrote
        it), then ``run_checks`` over ``merge-base..head`` and the token scan of the title
        and description. Raises ``NoChanges`` (nothing pushed beyond the base),
        ``CheckFailed`` or ``RunnerError``."""
        repo = self._repo(repo_name)
        if not re.fullmatch(r"[\w./-]+", branch) or ".." in branch or not _SHA.fullmatch(base_sha):
            raise RunnerError("bad branch reference")
        url, ref = self._fork_url_for(fork), self._fork_ref(code)
        listed = await self._git(["ls-remote", "--", url, f"refs/heads/{branch}"])
        if not listed.strip():
            raise NoChanges(f"nothing was pushed to {fork}")
        async with self.mirror_lock(repo_name):
            mirror = await self._ensure_mirror(repo_name)
            try:
                await self._git(
                    ["-C", str(mirror), "-c", "transfer.fsckObjects=true",
                     "-c", "fetch.fsckObjects=true", "fetch", "-q", "--no-tags",
                     "--no-write-fetch-head", url, f"+refs/heads/{branch}:{ref}"],
                )  # fmt: skip
            except GitError as e:
                raise RunnerError(
                    f"fetching {branch} from {fork} failed: {e.message[-120:]}"
                ) from None
            await self._refresh(repo_name, secrets.github_token)  # the upstream base as it is now
            git = self._mirror_git(repo_name)
            try:
                head = (await git("rev-parse", "--verify", f"{ref}^{{commit}}")).decode().strip()
                ahead = int((await git("rev-list", "--count", f"{base_sha}..{head}")).decode())
                base_tip = (await git("rev-parse", "--verify", f"refs/heads/{repo.base}")).decode()
                merge_base = (await git("merge-base", base_tip.strip(), head)).decode().strip()
            except GitError:
                raise RunnerError(f"{branch} shares no history with {repo.base}") from None
            if ahead == 0:
                raise NoChanges(f"nothing was pushed to {fork} beyond {repo.base}")
            result = await run_checks(
                git, base_sha=merge_base, head_sha=head, branch=branch, repo=repo,
                secrets=secrets.values(),
            )  # fmt: skip
        change = await self.fork_change(repo_name, code, base_sha)
        check_texts(secrets.values(), pr_title=change.title, pr_body=change.body)
        return ForkCheck(head, result, change)

    async def find_upstream_pr(
        self, repo_name: str, fork: str, branch: str, secrets: RunnerSecrets
    ) -> PrInfo | None:
        """An open PR on the repo from ``fork``'s ``branch`` (one Resonant opened before a
        crash), read with the daemon's token. None if there is none."""
        repo = self._repo(repo_name)
        owner = fork.partition("/")[0].lower()
        listed = await self._gh(
            ["pr", "list", "--repo", repo.slug, "--head", branch, "--state", "open", "--json",
             "number,url,state,baseRefName,headRefOid,isCrossRepository,headRepositoryOwner,"
             "changedFiles"],
            secrets,
        )  # fmt: skip
        if listed.returncode != 0:
            raise RunnerError(f"gh pr list failed: {listed.stderr[-120:]}")
        try:
            items: list[Any] = json.loads(listed.stdout)
        except ValueError:
            raise RunnerError("gh pr list printed something unreadable") from None
        for item in items:
            try:
                login = str(item["headRepositoryOwner"]["login"]).lower()
                url, head, files = (
                    str(item["url"]),
                    str(item["headRefOid"]),
                    item.get("changedFiles"),
                )
                if not item["isCrossRepository"] or login != owner or not _PR_URL.fullmatch(url):
                    continue
                return PrInfo(int(item["number"]), url, str(item["state"]),
                              str(item["baseRefName"]), head,
                              files if isinstance(files, int) else None)  # fmt: skip
            except (KeyError, TypeError, ValueError):
                continue
        return None

    async def open_upstream_pr(
        self, repo_name: str, fork: str, branch: str, head_sha: str, change: ForkChange,
        body: str, secrets: RunnerSecrets,
    ) -> PrInfo:  # fmt: skip
        """Open the PR on the original repo from ``fork:branch`` with the ``public_repo``
        token (``gh pr create --repo <upstream> --head <owner>:<branch>``). Only after the
        owner's ok on this exact head (the caller's). An open one from an earlier try is
        reused when it is at ``head_sha``. The new PR is read back; if its head isn't
        ``head_sha`` (the branch moved while it was being opened) it is closed at once and
        ``RunnerError`` is raised, so no PR stays open on content the owner didn't see."""
        repo = self._repo(repo_name)
        if not _SHA.fullmatch(head_sha):
            raise RunnerError("bad head commit")
        found = await self.find_upstream_pr(repo_name, fork, branch, secrets)
        if found is not None:
            if found.head_sha != head_sha:
                raise RunnerError(
                    f"PR #{found.number} on {repo.slug} is already open from {branch} at another "
                    f"commit ({found.head_sha[:7]})"
                )
            return found
        owner = fork.partition("/")[0]
        made = await self._gh(
            ["pr", "create", "--repo", repo.slug, "--head", f"{owner}:{branch}",
             "--base", repo.base, "--title", change.title, "--body-file", "-"],
            secrets, stdin=body.encode(), public=True,
        )  # fmt: skip
        m = _PR_URL.search(made.stdout.decode(errors="replace"))
        if made.returncode != 0 or m is None:
            raise RunnerError(f"opening the PR on {repo.slug} failed: {made.stderr[-120:]}")
        number = int(m[1])
        viewed = await self._gh(
            ["pr", "view", str(number), "--repo", repo.slug, "--json",
             "headRefOid,headRefName,isCrossRepository,changedFiles,state,baseRefName"],
            secrets,
        )  # fmt: skip
        try:
            pr: Any = json.loads(viewed.stdout) if viewed.returncode == 0 else {}
            head, files = str(pr["headRefOid"]), pr.get("changedFiles")
            same = head == head_sha and str(pr["headRefName"]) == branch
        except (ValueError, KeyError, TypeError):
            head, files, same = "", None, False
        if not same:
            closed = await self._gh(
                ["pr", "close", str(number), "--repo", repo.slug, "--comment",
                 "Closed by Resonant: the branch moved while this PR was being opened."],
                secrets, public=True,
            )  # fmt: skip
            gone = "closed it" if closed.returncode == 0 else "couldn't close it; close it by hand"
            raise RunnerError(
                f"PR #{number} on {repo.slug} isn't at the commit you approved ({gone})"
            )
        return PrInfo(number, m[0], "OPEN", repo.base, head,
                      files if isinstance(files, int) else None)  # fmt: skip

    # -- merge --

    async def _is_starter_ci_pr(
        self, repo: Repo, pr_number: int, head_sha: str, secrets: RunnerSecrets
    ) -> bool:
        """The add-CI PR (only the starter workflow, hash-checked on the PR head) may merge
        into a repo that has no CI yet."""
        from resonant.runner.create import adds_only_starter_ci

        async def run(args: Sequence[str]) -> tuple[int, bytes]:
            res = await self._gh(args, secrets)
            return res.returncode, res.stdout

        return await adds_only_starter_ci(run, repo.slug, pr_number, head_sha)

    async def _recheck_ci(self, repo: Repo, secrets: RunnerSecrets) -> bool:
        """Look again before refusing a merge for no CI: it may have been added since. Records
        what it finds. Fails closed."""
        try:
            has_ci = await self.repo_has_ci(repo.name, github_token=secrets.github_token)
        except GitError:
            return False
        with atomic(self.conn):
            repo_store.set_ci(self.conn, repo.slug, has_ci)
        return has_ci

    async def merge(
        self,
        repo_name: str,
        pr_number: int,
        head_sha: str,
        secrets: RunnerSecrets,
        *,
        branch: str,
        allow_flagged: bool = True,
        starter_ci: bool = False,
    ) -> CheckResult:
        """Squash-merge a PR whose CI is green, only if its head is still ``head_sha``.

        The only merge path, and it checks first: the PR's head is fetched into the mirror
        and put through ``check_pr`` (tokens in the history, objects, paths and the PR text;
        sensitive paths; ``.github`` changes refused unless the repo allows them), then CI,
        then ``--match-head-commit``. Returns the check result (its flagged paths are what
        the merge reply lists). Raises ``MergeRefused``.

        ``starter_ci``: the add-CI PR (``merge_ci_pr``), stricter, not looser: the branch must
        be an add-CI branch and the PR's whole diff the hash-checked starter workflow
        (``adds_only_starter_ci``), whether or not the repo has CI. The checks then allow
        ``.github/`` (whatever ``allow_github_changes`` says) but nothing may be flagged
        except that one file, so a branch that also touched anything else is refused."""
        from resonant.runner.create import CI_BRANCH_PREFIX, CI_PATH

        repo = self._repo(repo_name)
        if repo.mode == "fork":
            raise MergeRefused(upstream_merges(repo.slug))
        if not _SHA.fullmatch(head_sha) or pr_number < 1:
            raise MergeRefused("bad PR reference")
        if secrets.github_token is None:  # the mirror's checks and the merge itself use it
            raise MergeRefused(needs_github_token("Merging"))
        if starter_ci and (
            not branch.startswith(CI_BRANCH_PREFIX)
            or not await self._is_starter_ci_pr(repo, pr_number, head_sha, secrets)
        ):
            raise MergeRefused(f"PR #{pr_number} isn't only the starter CI")
        if (
            not starter_ci
            and repo.ci is False
            and not await self._recheck_ci(repo, secrets)
            and not await self._is_starter_ci_pr(repo, pr_number, head_sha, secrets)
        ):
            raise MergeRefused(
                f"{repo.slug} has no CI; add a pull_request workflow first (or ask me to add CI)."
            )
        try:
            result = await self.check_pr(
                repo_name, pr_number, branch, secrets, expect_head=head_sha,
                allow_github=True if starter_ci else None,
            )  # fmt: skip
        except CheckFailed as e:
            raise MergeRefused(f"the checks refuse it: {e}") from None
        except GitError as e:
            raise MergeRefused(f"checking the PR failed: {e.message[-120:]}") from None
        if starter_ci and set(result.flagged) - {CI_PATH}:
            raise MergeRefused(f"PR #{pr_number} isn't only the starter CI")
        if result.flagged and not allow_flagged:  # an auto-merge: flagged paths wait for /merge
            raise MergeRefused(f"it changes {', '.join(result.flagged)}")
        number = str(pr_number)
        match await self.ci_state(repo_name, pr_number, secrets):
            case "pending":
                raise MergeRefused("CI is still running")
            case "none":
                raise MergeRefused("no CI checks reported for this PR")
            case "red":
                raise MergeRefused("CI isn't green")
            case "error":
                raise MergeRefused("couldn't read the CI checks (gh pr checks errored)")
            case "green":
                pass
        merged = await self._gh(
            ["pr", "merge", number, "--repo", repo.slug, "--squash",
             "--match-head-commit", head_sha],
            secrets,
        )  # fmt: skip
        if merged.returncode != 0:
            raise MergeRefused(f"gh pr merge failed: {merged.stderr[-120:]}")
        return result


def _drop_admin_entry(checkout: Path, worktree: Path) -> None:
    """Remove the checkout's ``.git/worktrees/<id>`` whose ``gitdir`` is ``worktree``'s, once
    that worktree's dir is gone (``git worktree prune`` would also drop the owner's own
    worktrees whose dirs are only out of reach). Never raises."""
    admin = checkout / ".git" / "worktrees"
    with contextlib.suppress(OSError):
        if not stat.S_ISDIR(os.lstat(admin).st_mode):  # a link planted by a worker
            return
    want = {str(worktree / ".git"), os.path.join(os.path.realpath(worktree.parent),
                                                 worktree.name, ".git")}  # fmt: skip
    with contextlib.suppress(OSError):
        if os.path.lexists(worktree):
            return
        for entry in os.listdir(admin):
            gitdir = admin / entry / "gitdir"
            with contextlib.suppress(OSError):
                if gitdir.read_text().strip() in want:
                    rmtree_nofollow(admin / entry)

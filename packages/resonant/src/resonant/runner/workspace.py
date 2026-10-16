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
  config are never touched. A checkout is never deleted; only a run's worktree or scratch dir
  is, and only at the exact path the run recorded under ``code_dir``.
- The session's git finds the run's credential helper through ``GIT_CONFIG_*`` in its
  environment (``session_git_env``), not through any config file, so nothing about the run's
  token reaches the checkout's config and the owner's own pushes are unchanged.
- The daemon's GitHub token is only ever given to the daemon's own git (clone and fetch of a
  checkout, the bare mirror) and to ``gh``, and never by argv or environment. It goes in a
  0600 file in a fresh 0700 dir under ``runner.secrets_dir``, created right before the call
  and removed right after, success or not. git reads it through a credential helper script;
  ``gh`` through a ``hosts.yml`` in its own ``GH_CONFIG_DIR``. The token is never in a remote
  URL.
- Every daemon git command runs with ``GIT_CONFIG_NOSYSTEM=1``, ``GIT_CONFIG_GLOBAL=/dev/null``,
  ``core.hooksPath=/dev/null``, no inherited credential helper and an absolute git binary; in a
  checkout also with fsmonitor, auto-gc and submodule recursion off. The committer comes from
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
- Accepted risk: a worker can change its checkout's ``.git/config`` (a smudge filter, say), and
  the daemon's next worktree checkout in that repo would run it outside the sandbox. The owner's
  own git in that checkout would run it too; the thin guard (docs/employee-plan.md D2) does not
  cover checkouts.
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
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import dataclass
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

UPSTREAM_LATER = "Upstream PRs come in a later update."
GIT_TIMEOUT_S = 300.0
GH_TIMEOUT_S = 120.0
COMMITTER_NAME = "Resonant runner"
COMMITTER_EMAIL = "resonant-runner@users.noreply.github.com"
_SAFE_PATH = "/usr/bin:/bin"
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
_SHA = re.compile(r"[0-9a-f]{40}")
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
    *_INSTEAD_OF,
)
_GITHUB_REMOTE = re.compile(
    r"(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|[^@/:]+@)github\.com[:/]"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
    re.IGNORECASE,
)


class RunnerError(Exception):
    """A failure with a reason fit for ``R3 failed: <reason>``. Never carries a token."""


class NoChanges(RunnerError):
    """The session left no commits: the run ends ``no_changes``."""


class MergeRefused(RunnerError):
    pass


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


# --- the workspace -----------------------------------------------------------------------


class Workspace:
    def __init__(
        self,
        cfg: RunnerConfig,
        conn: sqlite3.Connection,
        *,
        url_for: Callable[[Repo], str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.conn = conn  # the repo registry (store.repos)
        # The git URL of a repo. Always https without credentials; tests point it at a path.
        self._url_for: Callable[[Repo], str] = url_for or _https_url
        self._locks: dict[str, asyncio.Lock] = {}
        self._checkout_locks: dict[str, asyncio.Lock] = {}
        self._git_bin = str(cfg.git_path)

    # -- locks and paths --

    def mirror_lock(self, repo_name: str) -> asyncio.Lock:
        """The per-repo mirror lock: refresh, harvest-fetch and push each take it. Not
        re-entrant, and not the runner's one-run-per-repo lock."""
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

    async def _refresh(self, repo_name: str, token: str) -> None:
        """Fetch the remote's branches into the mirror. Caller holds the mirror lock.

        No fsckObjects here: the remote is the owner's own repo, and old histories often
        fail fsck. The untrusted input (the session's bundle) is fetched with fsck."""
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
        """Bring the mirror up to date (also the go-live check's "mirror is fetchable")."""
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
        runs on one repo each get a worktree, one after the other). Not re-entrant."""
        return self._checkout_locks.setdefault(repo_name.lower(), asyncio.Lock())

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
        self, checkout: Path, *args: str, token: str | None = None, max_out: int | None = None
    ) -> bytes:
        """Daemon git in a checkout, with the checkout's risky settings overridden."""
        return await self._git(
            ["-C", str(checkout), *args], token=token, extra=_CHECKOUT_CONFIG, max_out=max_out
        )

    def _checkout_git(self, checkout: Path) -> GitRunner:
        async def git(*args: str, stdin: bytes | None = None, max_out: int | None = None) -> bytes:
            return await self._in_checkout(checkout, *args, max_out=max_out)

        return git

    async def ensure_checkout(self, repo: Repo, token: str) -> Path:
        """The owner's checkout of ``repo``, cloned (https, no credentials in the URL; the
        daemon's token by credential helper for this call only) when nothing is there. An
        existing one must be a git checkout whose ``origin`` is this repo. Caller holds the
        checkout lock."""
        path = self.checkout_path(repo)
        url = self._url_for(repo)
        if not await asyncio.to_thread(os.path.lexists, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                await self._git(["clone", "-q", "--", url, str(path)], token=token,
                                extra=_CHECKOUT_CONFIG)  # fmt: skip
            except GitError as e:
                raise RunnerError(f"cloning {repo.slug} failed: {e.message[-120:]}") from None
            return path
        not_ours = RunnerError(f"{path} is in the way: it isn't a git checkout of {repo.slug}")
        if not await asyncio.to_thread(path.is_dir):
            raise not_ours
        try:
            top = (await self._in_checkout(path, "rev-parse", "--show-toplevel")).decode().strip()
            origin = await self._in_checkout(path, "config", "--get", "remote.origin.url")
        except GitError:
            raise not_ours from None
        if not await asyncio.to_thread(os.path.samefile, top, path):
            raise not_ours
        origin_url = origin.decode().strip()
        slug = remote_slug(origin_url)
        if origin_url != url and (slug is None or slug.lower() != repo.slug.lower()):
            raise RunnerError(f"{path}'s origin isn't {repo.slug}")
        return path

    async def prepare(self, code: str, repo_name: str, ask: str, *, github_token: str) -> Prepared:
        """Make the run's places: the checkout (cloned on first use) fetched, a worktree on a
        new branch from ``origin/<base>`` with the checkout's local config linked in, and the
        run dir. Notes whether the base has CI. All of it before the session exists; the
        checkout's working tree and current branch are never touched.

        A leftover worktree or run dir (a crash before the session started) is removed and
        rebuilt."""
        if not RUN_CODE.fullmatch(code):
            raise RunnerError(f"bad run code {code!r}")
        repo = self._repo(repo_name)
        branch = branch_name(code, ask)
        worktree = self.worktree_path(repo.name, code)
        async with self.checkout_lock(repo.name):
            checkout = await self.ensure_checkout(repo, github_token)
            tracking = f"refs/remotes/origin/{repo.base}"
            try:
                await self._in_checkout(
                    checkout, "fetch", "-q", "--no-tags", "--no-write-fetch-head",
                    self._url_for(repo), f"+refs/heads/{repo.base}:{tracking}",
                    token=github_token,
                )  # fmt: skip
            except GitError as e:
                raise RunnerError(
                    f"fetching {repo.base} of {repo.slug} failed: {e.message[-120:]}"
                ) from None
            try:
                out = await self._in_checkout(checkout, "rev-parse", "--verify", "-q",
                                              f"{tracking}^{{commit}}")  # fmt: skip
            except GitError:
                raise RunnerError(f"{repo.base} isn't a branch of {repo_name}") from None
            base_sha = out.decode().strip()
            try:
                has_ci = await ci_present(self._checkout_git(checkout), base_sha)
            except GitError:
                has_ci = False
            # No CI is allowed (docs/repos-plan.md D5): the registry records it, the run's
            # reply says so, and merging is what's refused (after a look at the mirror).
            with atomic(self.conn):
                repo_store.set_ci(self.conn, repo.slug, has_ci)

            await self._remove_worktree(code, worktree, checkout, branch)
            run_dir = await asyncio.to_thread(self._run_dir, code)
            try:
                worktree.parent.mkdir(parents=True, exist_ok=True)
                await self._in_checkout(
                    checkout, "worktree", "add", "-q", "--no-track", "-B", branch,
                    str(worktree), base_sha,
                )  # fmt: skip
                listed = await self._in_checkout(checkout, "ls-files", "-z", "--", *LOCAL_CONFIG,
                                                 ".env.*")  # fmt: skip
                tracked = [n for n in listed.decode(errors="replace").split("\0") if n]
                linked = await asyncio.to_thread(link_local_config, checkout, worktree, tracked)
                (run_dir / EXCLUDE_FILE).write_text("".join(f"/{n}\n" for n in linked))
            except BaseException:
                await self._remove_worktree(code, worktree, checkout, branch)
                await asyncio.to_thread(rmtree_nofollow, run_dir)
                raise
        return Prepared(code, repo.name, run_dir, worktree, checkout, branch, repo.base, base_sha)

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
        self, code: str, worktree: Path, checkout: Path | None, branch: str | None
    ) -> bool:
        """``git worktree remove --force`` in the checkout; whatever git couldn't remove is
        deleted (checked place, no links followed) and the checkout's worktree list pruned.
        Then the run's own ``resonant/<code>-…`` branch is deleted from the checkout. Caller
        holds the checkout lock. The checkout itself is never deleted."""
        if self._workspace_kind(worktree, code) != "worktree":
            raise RunnerError(f"refusing to remove {worktree}: not a run's worktree")
        usable = checkout is not None and await asyncio.to_thread(os.path.exists, checkout / ".git")
        removed = False
        if usable and checkout is not None and await asyncio.to_thread(os.path.lexists, worktree):
            try:
                await self._in_checkout(checkout, "worktree", "remove", "--force", "--force",
                                        str(worktree))  # fmt: skip
                removed = True
            except GitError:
                pass
        if await asyncio.to_thread(os.path.lexists, worktree):
            removed = await asyncio.to_thread(self._remove_checked, worktree) or removed
            if usable and checkout is not None:
                with contextlib.suppress(GitError):
                    await self._in_checkout(checkout, "worktree", "prune")
        own = f"resonant/{code.lower()}-"
        if usable and checkout is not None and branch and branch.startswith(own):
            with contextlib.suppress(GitError):  # gone already, or checked out by the owner
                await self._in_checkout(checkout, "branch", "-q", "-D", branch)
        return removed

    async def remove_workspace(
        self,
        code: str,
        *,
        workspace: str | Path | None,
        checkout: str | Path | None = None,
        branch: str | None = None,
    ) -> bool:
        """Remove a run's worktree (and its branch) or scratch dir: on cancel, on merge and
        ``runner.keep_days`` after the run ended. Only the exact place the run recorded under
        ``code_dir`` is touched; anything else raises ``RunnerError``. False if nothing was
        there."""
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
                code, path, None if checkout is None else Path(checkout), branch
            )

    # -- gh --

    async def gh(
        self, args: Sequence[str], secrets: RunnerSecrets, *, stdin: bytes | None = None
    ) -> Exec:
        """Run ``gh`` with the token in a throwaway config dir (for the repo resolver)."""
        return await self._gh(args, secrets, stdin=stdin)

    async def _gh(
        self, args: Sequence[str], secrets: RunnerSecrets, *, stdin: bytes | None = None
    ) -> Exec:
        # Everything gh reads or writes lives in one throwaway dir; the token is in its
        # hosts.yml (which gh prefers to the Keychain) and never in env or argv.
        with secret_dir(self.cfg.secrets_dir) as tmp:
            config = write_gh_config(tmp, secrets.github_token)
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
    ) -> CheckResult:
        """The checks of docs/repos-plan.md D10, on the PR as it is now: fetch its head branch
        into the mirror (with fsck: the session wrote it), then run ``run_checks`` over
        ``merge-base..head`` plus a token scan of the PR's title and body. Raises
        ``MergeRefused`` (the PR isn't mergeable as it stands) or ``CheckFailed`` (the checks
        refuse it). With ``expect_head`` the PR must still be at that commit."""
        repo = self._repo(repo_name)
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
                repo=repo,
                secrets=secrets.values(),
            )
        check_texts(secrets.values(), pr_title=title, pr_body=body)
        return result

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
    ) -> CheckResult:
        """Squash-merge a PR whose CI is green, only if its head is still ``head_sha``.

        The only merge path, and it checks first: the PR's head is fetched into the mirror
        and put through ``check_pr`` (tokens in the history, objects, paths and the PR text;
        sensitive paths; ``.github`` changes refused unless the repo allows them), then CI,
        then ``--match-head-commit``. Returns the check result (its flagged paths are what
        the merge reply lists). Raises ``MergeRefused``."""
        repo = self._repo(repo_name)
        if not _SHA.fullmatch(head_sha) or pr_number < 1:
            raise MergeRefused("bad PR reference")
        if (
            repo.ci is False
            and not await self._recheck_ci(repo, secrets)
            and not await self._is_starter_ci_pr(repo, pr_number, head_sha, secrets)
        ):
            raise MergeRefused(
                f"{repo.slug} has no CI; add a pull_request workflow first (or ask me to add CI)."
            )
        try:
            result = await self.check_pr(
                repo_name, pr_number, branch, secrets, expect_head=head_sha
            )
        except CheckFailed as e:
            raise MergeRefused(f"the checks refuse it: {e}") from None
        except GitError as e:
            raise MergeRefused(f"checking the PR failed: {e.message[-120:]}") from None
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

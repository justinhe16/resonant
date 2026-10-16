"""The runner's workspace: mirror, clone, the session's credentials, PR discovery and merge
(docs/phase5a-plan.md D3, docs/repos-plan.md D10).

On a ``push`` repo the Claude session pushes its own branch and opens the PR with a per-run
GitHub App token (``write_session_credentials``); this module finds the PR afterwards and, before
any merge, fetches its head into the mirror and runs the checks. The bundle handback and the
daemon-driven push and PR (``harvest``, ``import_bundle``, ``check``, ``publish``) stay for the
fork mode, where the daemon still publishes.

Trust rules this module keeps:

- The GitHub token is only ever given to the daemon's own git (in the bare mirror) and to
  ``gh``, and never by argv or environment: a sandboxed process can read any same-user
  process's env and argv. It goes in a 0600 file in a fresh 0700 dir under
  ``runner.secrets_dir`` (``~/.resonant/run-secrets``, which the sandbox's ``$HOME`` read
  deny covers), created right before the call and removed right after, success or not.
  git reads it through a credential helper script; ``gh`` through a ``hosts.yml`` in its own
  ``GH_CONFIG_DIR`` (``gh`` prefers the config token and never reaches for the Keychain
  when it has one). Only the paths are in env. The token is never in a remote URL, and
  never goes into the sandbox.
- Every git command runs with ``GIT_CONFIG_NOSYSTEM=1``, ``GIT_CONFIG_GLOBAL=/dev/null``,
  ``core.hooksPath=/dev/null``, no credential helper and an absolute git binary. The
  committer comes from the environment, never from a config file.
- Once the session starts, the daemon runs git only in the mirror. In the clone it runs
  four commands, all in ``prepare``, before any sandboxed process exists. What the session
  leaves behind arrives in the mirror by ``git fetch`` with fsck (the PR's branch) or, for a
  bundle, by file descriptor and fsck.
- The session's own token (a one-repo App installation token, see ``github_app``) lives in a
  0700 ``creds`` dir inside the run dir, which the session can read: a credential helper
  script, a ``token`` file and a ``gh/hosts.yml``. It is never in env or argv. The daemon
  writes there only through directory fds with ``O_NOFOLLOW`` and ``rename``, so a link planted
  by the session can't redirect a write.
- Accepted risk: the package cache (``runner.cache_dir``) is shared between runs, so one run
  can poison it for the next; it sits inside the same sandbox and nothing else uses it.
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
from collections.abc import Awaitable, Callable, Generator, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

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
HARVEST_TIMEOUT_S = 300.0
MAX_ASK_QUOTE = 2000
MAX_SUMMARY_QUOTE = 2000
COMMITTER_NAME = "Resonant runner"
COMMITTER_EMAIL = "resonant-runner@users.noreply.github.com"
_SAFE_PATH = "/usr/bin:/bin"
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
_SHA = re.compile(r"[0-9a-f]{40}")
_ERR_CLIP = 300
CREDS_DIR = "creds"
_PUSH_KINDS = ("push", "force_push", "branch_deletion")
_PR_FIELDS = "state,headRefName,headRefOid,baseRefName,isCrossRepository,title,body"

# Run inside the sandbox: commit anything left over, then bundle base..branch.
# argv: repo, out.bundle, base sha, branch, git, commit message.
HARVEST_SCRIPT = """set -e
cd "$1"
bin="$5"
g() { "$bin" -c core.hooksPath=/dev/null -c gc.auto=0 -c core.fsmonitor=false "$@"; }
g symbolic-ref HEAD "refs/heads/$4"
g add -A
if ! g diff --cached --quiet; then
  g commit -q --no-verify -m "$6"
fi
[ "$(g rev-list --count "$3..refs/heads/$4")" = 0 ] && exit 0
g bundle create -q "$2" "$3..refs/heads/$4"
"""


class RunnerError(Exception):
    """A failure with a reason fit for ``R3 failed: <reason>``. Never carries a token."""


class BundleRefused(RunnerError):
    pass


class NoChanges(RunnerError):
    """The session left no commits: the run ends ``no_changes``."""


class PublishAborted(RunnerError):
    """The run was cancelled or Resonant paused before a publish step."""


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


def git_env(helper: Path | None = None) -> dict[str, str]:
    """A from-scratch git environment: no user or system config, no hooks, no prompts, no
    redirects, and a credential helper only when ``helper`` (a script path, never a token)
    is given."""
    config = [("core.hooksPath", "/dev/null"), ("http.followRedirects", "false")]
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
        "GIT_CONFIG_COUNT": str(len(config)),
        **committer_env(),
    }
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
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


def build_pr_body(code: str, repo: Repo, ask: str, summary: str, result: CheckResult) -> str:
    """Code-built PR body: the run, the ask (unless ``quote_ask`` is off), Claude's summary,
    the checks and the flagged paths."""
    lines = [f"Resonant run {code}."]  # untrusted text below has @ neutralized
    if repo.quote_ask:
        quoted = ask.strip()[:MAX_ASK_QUOTE].replace("\n", "\n> ")
        lines += ["", "Ask:", f"> {quoted}"]
    if summary.strip():
        lines += ["", "Summary from Claude:", summary.strip()[:MAX_SUMMARY_QUOTE]]
    lines += ["", f"Checks: {result.files} files, {result.changed_bytes} bytes changed."]
    if result.flagged:
        lines.append(result.flag_line)
    # A zero-width space after "@" so the PR can't ping users or teams.
    return "\n".join(lines).replace("@", "@\u200b") + "\n"


@dataclass(frozen=True)
class Prepared:
    code: str
    repo_name: str
    run_dir: Path
    repo_dir: Path
    branch: str
    base_branch: str
    base_sha: str

    @property
    def bundle_path(self) -> Path:
        return self.run_dir / "out.bundle"

    @property
    def creds_dir(self) -> Path:
        return self.run_dir / CREDS_DIR

    @property
    def run_ref(self) -> str:
        return f"refs/runs/{self.code.lower()}"


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str


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


StillOk = Callable[[], Awaitable[bool]]
"""Whether the run may still publish (not cancelled, Resonant not paused). Called right
before ``push`` and right before ``gh pr create``."""

Wrap = Callable[[Sequence[str]], Sequence[str]]
"""Wraps an argv so it runs inside the sandbox (P5A-1's ``wrap``, with the run's params
bound). Tests pass the identity."""


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
                env=git_env(helper),
                cwd=cwd,
                stdin=stdin,
                redact=[token] if token else (),
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

    async def prepare(self, code: str, repo_name: str, ask: str, *, github_token: str) -> Prepared:
        """Refresh the mirror, note whether the repo has CI, clone it into
        ``<work_dir>/<code>/repo`` and branch, with the credential helper set (and an empty
        ``creds`` dir for it). The only daemon-side git in the clone, and it happens before
        the session exists.

        Invariant for the sandbox (P5A-1 probes it): the only sandbox-writable path is
        ``<work_dir>/<code>`` (plus the cache), so ``work_dir`` and the mirror dir are never
        written by a session. A leftover ``<code>`` dir (a crash before the session started)
        is daemon-owned and is deleted and rebuilt.
        """
        if not RUN_CODE.fullmatch(code):
            raise RunnerError(f"bad run code {code!r}")
        repo = self._repo(repo_name)
        run_dir = self.cfg.work_dir / code
        repo_dir = run_dir / "repo"
        branch = branch_name(code, ask)
        url = self._url_for(repo)
        async with self.mirror_lock(repo_name):
            await self._refresh(repo_name, github_token)
            mirror = self.mirror_path(repo_name)
            git = self._mirror_git(repo_name)
            try:
                base_sha = (await git("rev-parse", "--verify", f"refs/heads/{repo.base}")).decode()
            except GitError:
                raise RunnerError(f"{repo.base} isn't a branch of {repo_name}") from None
            base_sha = base_sha.strip()
            try:
                has_ci = await ci_present(git, base_sha)
            except GitError:
                has_ci = False
            # No CI is allowed (docs/repos-plan.md D5): the registry records it, the run's
            # reply says so, and merging is what's refused.
            with atomic(self.conn):
                repo_store.set_ci(self.conn, repo.slug, has_ci)

            run_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await asyncio.to_thread(rmtree_nofollow, run_dir)
            run_dir.mkdir(mode=0o700)
            try:
                for sub in ("home", "tmp", CREDS_DIR):
                    (run_dir / sub).mkdir(mode=0o700)
                await self._git(
                    [
                        "clone",
                        "-q",
                        "--no-hardlinks",
                        "--no-tags",
                        "--branch",
                        repo.base,
                        "--",
                        str(mirror),
                        str(repo_dir),
                    ],
                    cwd=run_dir,
                )
                await self._git(["-C", str(repo_dir), "remote", "set-url", "origin", url])
                # The helper reads the token file next to it; the path is no secret. The file
                # is written when the session starts (``write_session_credentials``).
                helper = run_dir / CREDS_DIR / "credential-helper"
                await self._git(
                    [
                        "-C",
                        str(repo_dir),
                        "config",
                        "credential.helper",
                        f"!{shlex.quote(str(helper))}",
                    ]
                )
                await self._git(["-C", str(repo_dir), "checkout", "-q", "-b", branch])
            except BaseException:
                await asyncio.to_thread(rmtree_nofollow, run_dir)
                raise
        return Prepared(code, repo_name, run_dir, repo_dir, branch, repo.base, base_sha)

    # -- harvest --

    def harvest_argv(self, p: Prepared) -> list[str]:
        """The argv to run **inside the sandbox**: commit leftovers, then
        ``git bundle create out.bundle <base>..<branch>``. No bundle means no commits."""
        return [
            "/bin/sh",
            "-c",
            HARVEST_SCRIPT,
            "sh",
            str(p.repo_dir),
            str(p.bundle_path),
            p.base_sha,
            p.branch,
            self._git_bin,
            f"{p.code}: uncommitted work",
        ]

    async def harvest(self, p: Prepared, wrap: Wrap, env: dict[str, str]) -> None:
        """Run the harvest script through ``wrap`` (the sandbox). ``env`` is the sandbox env
        (it needs the committer identity: ``committer_env()``)."""
        res = await _exec(
            list(wrap(self.harvest_argv(p))),
            env=env,
            cwd=p.run_dir,
            timeout_s=HARVEST_TIMEOUT_S,
        )
        if res.returncode != 0:
            raise RunnerError("collecting the session's commits failed")

    async def import_bundle(self, p: Prepared) -> str:
        """Read ``out.bundle`` by fd, check it, and fetch the branch into the mirror as
        ``refs/runs/<code>``. Returns the head sha. Raises ``NoChanges`` (no bundle) or
        ``BundleRefused``."""
        cap = self.cfg.max_bundle_mb * 1024 * 1024
        incoming = self.cfg.mirror_dir / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True, mode=0o700)
        copy = incoming / f"{p.code}.bundle"
        await asyncio.to_thread(_copy_bundle, p.run_dir, p.bundle_path.name, copy, cap)
        try:
            try:
                heads = (
                    await self._git(
                        [
                            "-C",
                            str(self.mirror_path(p.repo_name)),
                            "bundle",
                            "list-heads",
                            str(copy),
                        ]
                    )
                ).decode()
            except GitError:
                raise BundleRefused("the bundle isn't a valid git bundle") from None
            refs = [line.split(" ", 1)[1].strip() for line in heads.splitlines() if " " in line]
            if refs != [f"refs/heads/{p.branch}"]:
                raise BundleRefused("the bundle holds refs other than the run's branch")
            async with self.mirror_lock(p.repo_name):
                try:
                    await self._git(
                        [
                            "-C",
                            str(self.mirror_path(p.repo_name)),
                            "-c",
                            "transfer.fsckObjects=true",
                            "-c",
                            "fetch.fsckObjects=true",
                            "fetch",
                            "-q",
                            "--no-tags",
                            "--no-write-fetch-head",
                            str(copy),
                            f"+refs/heads/{p.branch}:{p.run_ref}",
                        ]
                    )
                except GitError as e:
                    raise BundleRefused(
                        f"the bundle failed git's checks: {e.message[-120:]}"
                    ) from None
                head = await self._mirror_git(p.repo_name)("rev-parse", "--verify", p.run_ref)
        finally:
            with contextlib.suppress(FileNotFoundError):
                copy.unlink()
        return head.decode().strip()

    async def check(self, p: Prepared, head_sha: str, secrets: RunnerSecrets) -> CheckResult:
        """The refuse/flag checks on ``base..head`` in the mirror. Raises ``CheckFailed``."""
        return await run_checks(
            self._mirror_git(p.repo_name),
            base_sha=p.base_sha,
            head_sha=head_sha,
            branch=p.branch,
            repo=self._repo(p.repo_name),
            secrets=secrets.values(),
        )

    # -- publish --

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

    @staticmethod
    async def _must_still_be_ok(still_ok: StillOk) -> None:
        if not await still_ok():
            raise PublishAborted("the run was cancelled or Resonant is paused")

    async def publish(
        self,
        p: Prepared,
        result: CheckResult,
        secrets: RunnerSecrets,
        *,
        ask: str,
        summary: str,
        still_ok: StillOk,
    ) -> PullRequest:
        """Push the run's branch and open the PR. Idempotent: a branch already at the head
        is a no-op push, and an open PR for the branch is reused.

        ``still_ok`` is awaited before the push, again after it, and again right before
        ``gh pr create``; a falsy answer raises ``PublishAborted``.
        """
        repo = self._repo(p.repo_name)
        if repo.mode != "push":  # never push to a repo the token can't push to
            raise RunnerError(UPSTREAM_LATER)
        if not _SHA.fullmatch(result.head_sha):
            raise RunnerError("bad head commit")
        body = build_pr_body(p.code, repo, ask, summary, result)
        check_texts(secrets.values(), pr_body=body, pr_title=result.title, branch_name=p.branch)

        await self._must_still_be_ok(still_ok)
        async with self.mirror_lock(p.repo_name):
            try:
                await self._git(
                    [
                        "-C",
                        str(self.mirror_path(p.repo_name)),
                        "push",
                        "-q",
                        self._url_for(repo),
                        f"{result.head_sha}:refs/heads/{p.branch}",
                    ],
                    token=secrets.github_token,
                )
            except GitError as e:
                raise RunnerError(f"pushing the branch failed: {e.message[-120:]}") from None

        await self._must_still_be_ok(still_ok)
        listed = await self._gh(
            ["pr", "list", "--repo", repo.slug, "--head", p.branch, "--state", "all",
             "--json", "number,url"],
            secrets,
        )  # fmt: skip
        if listed.returncode == 0:
            with contextlib.suppress(ValueError, KeyError, TypeError):
                for item in json.loads(listed.stdout):
                    url = str(item["url"])
                    if _PR_URL.fullmatch(url):
                        return PullRequest(int(item["number"]), url)

        await self._must_still_be_ok(still_ok)
        created = await self._gh(
            ["pr", "create", "--repo", repo.slug, "--head", p.branch, "--base", p.base_branch,
             "--title", result.title.replace("@", "@\u200b"), "--body-file", "-"],
            secrets,
            stdin=body.encode(),
        )  # fmt: skip
        if created.returncode != 0:
            raise RunnerError(f"gh pr create failed: {created.stderr[-120:]}")
        lines = created.stdout.decode(errors="replace").split()
        match = _PR_URL.fullmatch(lines[-1]) if lines else None
        if match is None:
            raise RunnerError("gh pr create didn't print a PR URL")
        return PullRequest(int(match[1]), match[0])

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
        """The commits ``actor`` (the app's bot) pushed straight to the base branch since
        ``since``: the ``after`` sha of each push and force-push in the repo's activity feed.
        Raises ``RunnerError`` when the feed can't be read."""
        repo = self._repo(repo_name)
        res = await self._gh(
            ["api", f"repos/{repo.slug}/activity?ref=refs/heads/{repo.base}&per_page=100"], secrets
        )
        if res.returncode != 0:
            raise RunnerError(f"couldn't read {repo.base}'s activity: {res.stderr[-120:]}")
        try:
            events: list[Any] = json.loads(res.stdout)
        except ValueError:
            raise RunnerError(f"{repo.base}'s activity couldn't be read") from None
        found: list[str] = []
        for event in events:
            try:
                login = str(event["actor"]["login"])
                when = datetime.fromisoformat(str(event["timestamp"]))
                kind, after = str(event["activity_type"]), str(event["after"])
            except (KeyError, TypeError, ValueError):
                continue
            if login.lower() == actor.lower() and when >= since and kind in _PUSH_KINDS:
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


# --- the bundle read ---------------------------------------------------------------------


def _copy_bundle(run_dir: Path, name: str, dest: Path, cap: int) -> None:
    """Copy ``<run_dir>/<name>`` into ``dest`` through one fd: ``O_NOFOLLOW``, a regular
    file, at most ``cap`` bytes (read ``cap + 1`` from the fd, never from the path)."""
    try:
        dir_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise BundleRefused("the run dir isn't a plain directory") from e
    try:
        try:
            # O_NONBLOCK so a FIFO planted at the path can't hang the open.
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=dir_fd,
            )
        except FileNotFoundError:
            raise NoChanges("the session made no commits") from None
        except OSError:
            raise BundleRefused("the bundle isn't a regular file") from None
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BundleRefused("the bundle isn't a regular file")
        with contextlib.suppress(FileNotFoundError):
            dest.unlink()
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            remaining = cap + 1
            while remaining > 0:
                chunk = os.read(fd, min(1 << 20, remaining))
                if not chunk:
                    break
                os.write(out, chunk)
                remaining -= len(chunk)
            if remaining == 0:
                raise BundleRefused(f"the bundle is over {cap // (1024 * 1024)} MB")
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                dest.unlink()  # never leave a partial copy
            raise
        finally:
            os.close(out)
    finally:
        os.close(fd)

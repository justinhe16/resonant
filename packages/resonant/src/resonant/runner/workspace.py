"""The runner's workspace: mirror, clone, bundle handback, push, PR and merge
(docs/phase5a-plan.md D3).

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
  three commands, all in ``prepare``, before any sandboxed process exists. The bundle the
  session leaves behind is read by file descriptor and fetched into the mirror with fsck.
- The daemon writes nothing into a run dir after ``prepare``.
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
from pathlib import Path
from typing import Any

from resonant.config import RunnerConfig
from resonant.redact import Redactor
from resonant.runner.checks import (
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
# git credential helper: answers "get" from a 0600 file next to it.
[ "$1" = get ] || exit 0
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
    def run_ref(self) -> str:
        return f"refs/runs/{self.code.lower()}"


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str


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
        ``<work_dir>/<code>/repo`` and branch. The only daemon-side git in the clone, and it
        happens before the session exists.

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
                for sub in ("home", "tmp"):
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
        self, repo_name: str, pr_number: int, head_sha: str, secrets: RunnerSecrets
    ) -> None:
        """Squash-merge a PR whose CI is green, only if its head is still ``head_sha``."""
        repo = self._repo(repo_name)
        if not _SHA.fullmatch(head_sha) or pr_number < 1:
            raise MergeRefused("bad PR reference")
        number = str(pr_number)
        if (
            repo.ci is False
            and not await self._recheck_ci(repo, secrets)
            and not await self._is_starter_ci_pr(repo, pr_number, head_sha, secrets)
        ):
            raise MergeRefused(
                f"{repo.slug} has no CI; add a pull_request workflow first (or ask me to add CI)."
            )
        checks = await self._gh(["pr", "checks", number, "--repo", repo.slug], secrets)
        if checks.returncode == 8:
            raise MergeRefused("CI is still running")
        if checks.returncode == 1 and "no checks reported" in checks.stderr:
            raise MergeRefused("no CI checks reported for this PR")
        if checks.returncode == 1:
            raise MergeRefused("CI isn't green")
        if checks.returncode != 0:
            raise MergeRefused("couldn't read the CI checks (gh pr checks errored)")
        merged = await self._gh(
            ["pr", "merge", number, "--repo", repo.slug, "--squash",
             "--match-head-commit", head_sha],
            secrets,
        )  # fmt: skip
        if merged.returncode != 0:
            raise MergeRefused(f"gh pr merge failed: {merged.stderr[-120:]}")


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

"""The runner's workspace: mirror, clone, bundle handback, push, PR and merge
(docs/phase5a-plan.md D3).

Trust rules this module keeps:

- The GitHub token is only ever given to the daemon's own git (in the bare mirror) and to
  ``gh``, by environment (``GIT_CONFIG_COUNT`` / ``GH_TOKEN``). It is never on an argv, in a
  remote URL or in a file, and never goes into the sandbox.
- Every git command runs with ``GIT_CONFIG_NOSYSTEM=1``, ``GIT_CONFIG_GLOBAL=/dev/null``,
  ``core.hooksPath=/dev/null``, no credential helper and an absolute git binary. The
  committer comes from the environment, never from a config file.
- Once the session starts, the daemon runs git only in the mirror. In the clone it runs
  three commands, all in ``prepare``, before any sandboxed process exists. The bundle the
  session leaves behind is read by file descriptor and fetched into the mirror with fsck.
- The daemon writes nothing into a run dir after ``prepare``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import signal
import stat
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from resonant.config import RunnerConfig, RunnerRepo
from resonant.redact import Redactor
from resonant.runner.checks import (
    CheckResult,
    GitError,
    GitRunner,
    check_texts,
    run_checks,
)
from resonant.runner.cleanup import RUN_CODE, rmtree_nofollow
from resonant.secrets import RunnerSecrets

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
g="$5"
"$g" -c core.hooksPath=/dev/null add -A
if ! "$g" -c core.hooksPath=/dev/null diff --cached --quiet; then
  "$g" -c core.hooksPath=/dev/null commit -q --no-verify -m "$6"
fi
[ "$("$g" rev-list --count "$3..$4")" = 0 ] && exit 0
"$g" bundle create -q "$2" "$3..$4"
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


def git_env(token: str | None = None) -> dict[str, str]:
    """A from-scratch git environment: no user or system config, no hooks, no credential
    helper, no prompts. With a token, an Authorization header scoped to github.com."""
    config = [("core.hooksPath", "/dev/null"), ("credential.helper", "")]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        config.append(("http.https://github.com/.extraHeader", f"Authorization: Basic {basic}"))
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
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout_s)
    except BaseException:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    text = Redactor(redact).text(err.decode(errors="replace")).strip()
    return Exec(proc.returncode if proc.returncode is not None else -1, out, text[-_ERR_CLIP:])


def build_pr_body(code: str, repo: RunnerRepo, ask: str, summary: str, result: CheckResult) -> str:
    """Code-built PR body: the run, the ask (unless ``quote_ask`` is off), Claude's summary,
    the checks and the flagged paths."""
    lines = [f"Resonant run {code}."]
    if repo.quote_ask:
        quoted = ask.strip()[:MAX_ASK_QUOTE].replace("\n", "\n> ")
        lines += ["", "Ask:", f"> {quoted}"]
    if summary.strip():
        lines += ["", "Summary from Claude:", summary.strip()[:MAX_SUMMARY_QUOTE]]
    lines += ["", f"Checks: {result.files} files, {result.changed_bytes} bytes changed."]
    if result.flagged:
        lines.append(result.flag_line)
    return "\n".join(lines) + "\n"


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


def _https_url(repo: RunnerRepo) -> str:
    return f"https://{repo.remote}"


# --- the workspace -----------------------------------------------------------------------


class Workspace:
    def __init__(
        self,
        cfg: RunnerConfig,
        *,
        url_for: Callable[[RunnerRepo], str] | None = None,
    ) -> None:
        self.cfg = cfg
        # The git URL of a repo. Always https without credentials; tests point it at a path.
        self._url_for: Callable[[RunnerRepo], str] = url_for or _https_url
        self._locks: dict[str, asyncio.Lock] = {}
        self._git_bin = str(cfg.git_path)

    # -- locks and paths --

    def mirror_lock(self, repo_name: str) -> asyncio.Lock:
        """The per-repo mirror lock: refresh, harvest-fetch and push each take it. Not
        re-entrant, and not the runner's one-run-per-repo lock."""
        return self._locks.setdefault(repo_name, asyncio.Lock())

    def mirror_path(self, repo_name: str) -> Path:
        return self.cfg.mirror_dir / f"{repo_name}.git"

    def _repo(self, repo_name: str) -> RunnerRepo:
        try:
            return self.cfg.repos[repo_name]
        except KeyError:
            raise RunnerError(f"{repo_name} isn't in runner.repos") from None

    def _mirror_git(self, repo_name: str) -> GitRunner:
        mirror = self.mirror_path(repo_name)

        async def git(*args: str, stdin: bytes | None = None) -> bytes:
            res = await _exec([self._git_bin, "-C", str(mirror), *args], env=git_env(), stdin=stdin)
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
        res = await _exec(
            [self._git_bin, *args],
            env=git_env(token),
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
        """Fetch the remote's branches into the mirror. Caller holds the mirror lock."""
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

    async def prepare(self, code: str, repo_name: str, ask: str, *, github_token: str) -> Prepared:
        """Refresh the mirror, clone it into ``<work_dir>/<code>/repo`` and branch. The only
        daemon-side git in the clone, and it happens before the session exists."""
        if not RUN_CODE.fullmatch(code):
            raise RunnerError(f"bad run code {code!r}")
        repo = self._repo(repo_name)
        run_dir = self.cfg.work_dir / code
        run_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            raise RunnerError(f"run dir for {code} already exists") from None
        try:
            for sub in ("home", "tmp"):
                (run_dir / sub).mkdir(mode=0o700)
            repo_dir = run_dir / "repo"
            branch = branch_name(code, ask)
            url = self._url_for(repo)
            async with self.mirror_lock(repo_name):
                await self._refresh(repo_name, github_token)
                mirror = self.mirror_path(repo_name)
                try:
                    base_sha = (
                        (
                            await self._mirror_git(repo_name)(
                                "rev-parse", "--verify", f"refs/heads/{repo.base}"
                            )
                        )
                        .decode()
                        .strip()
                    )
                except GitError:
                    raise RunnerError(f"{repo.base} isn't a branch of {repo_name}") from None
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

    async def _gh(
        self, args: Sequence[str], secrets: RunnerSecrets, *, stdin: bytes | None = None
    ) -> Exec:
        home = self.cfg.mirror_dir.parent / "gh"
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {
            "PATH": _SAFE_PATH,
            "HOME": str(home),
            "GH_CONFIG_DIR": str(home),
            "GH_TOKEN": secrets.github_token,
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "NO_COLOR": "1",
        }
        return await _exec(
            [str(self.cfg.gh_path), *args],
            env=env,
            cwd=home,
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
                        f"{p.run_ref}:refs/heads/{p.branch}",
                    ],
                    token=secrets.github_token,
                )
            except GitError as e:
                raise RunnerError(f"pushing the branch failed: {e.message[-120:]}") from None

        await self._must_still_be_ok(still_ok)
        listed = await self._gh(
            ["pr", "list", "--repo", repo.slug, "--head", p.branch, "--state", "open",
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
             "--title", result.title, "--body-file", "-"],
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

    async def merge(
        self, repo_name: str, pr_number: int, head_sha: str, secrets: RunnerSecrets
    ) -> None:
        """Squash-merge a PR whose CI is green, only if its head is still ``head_sha``."""
        repo = self._repo(repo_name)
        if not _SHA.fullmatch(head_sha) or pr_number < 1:
            raise MergeRefused("bad PR reference")
        number = str(pr_number)
        checks = await self._gh(["pr", "checks", number, "--repo", repo.slug], secrets)
        if checks.returncode == 8:
            raise MergeRefused("CI is still running")
        if checks.returncode != 0:
            raise MergeRefused("CI isn't green")
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
        finally:
            os.close(out)
        if remaining == 0:
            dest.unlink()
            raise BundleRefused(f"the bundle is over {cap // (1024 * 1024)} MB")
    finally:
        os.close(fd)

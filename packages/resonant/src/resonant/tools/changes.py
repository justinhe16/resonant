"""``recent_changes`` (BR-9, L0): what changed lately in a repo, from its local checkout.

"Most recent commits/PRs?" had no tool: ``prs`` lists only open PRs and ``pr`` needs a number,
so the brain guessed ``pr{number: 1}`` and reported a ten-day-old PR as the latest. This reads
the newest commits of a checkout with ``git log`` (no fetch: nothing touches the network or
changes the checkout), with each squash-merge's PR number taken from its subject (``… (#104)``).

- ``recent_changes{repo?, limit?}``: ``repo`` is a registered repo (name, ``owner/name`` or
  alias: its checkout, as the briefing lists it), ``resonant`` (Resonant's own checkout,
  ``ops.repo_path``), or an absolute or ``~/`` path to a checkout. Without it: Resonant's own
  checkout, else the only registered repo. ``limit`` commits (default ``DEFAULT_LIMIT``, at
  most ``MAX_LIMIT``), newest first, each ``{sha, date, author, subject, pr?}``; plus the
  branch, how far it is ahead of or behind its upstream as of the last fetch, and when that
  fetch was (``fetched_at``). GitHub may have newer commits than the checkout: the github
  extension's ``recent_prs`` is GitHub's own view.

The path goes through the read tools' scope (``files.ReadScope``: the deny list, then the
read roots), so this reads nothing ``read_file`` couldn't. git runs with no system or global
config, ``HOME=/var/empty``, no hooks, pager, fsmonitor or signature checks (a repo's own
config can't make it run a program), a fixed ``PATH`` and a timeout. Commit text was written
by people and workers, so the spec is ``untrusted`` (it taints the brain's task, D6) and every
string is redacted and clipped like a file read. Owner-only unless a member has the
``builtin:files`` grant.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from resonant.config import Settings
from resonant.executor import ToolFailed, ToolHandler
from resonant.store import repos as repo_store
from resonant.tools.base import BuiltinContext
from resonant.tools.files import GRANT, ReadScope, redact_clip
from resonant_sdk import Effect, ToolSpec

DEFAULT_LIMIT = 10
MAX_LIMIT = 30
REPO_MAX = 200
SUBJECT_MAX = 160
AUTHOR_MAX = 60
GIT_TIMEOUT_S = 10.0
OUT_MAX_BYTES = 256 * 1024
SELF_NAMES = frozenset({"resonant", "resonant itself", "self"})
_SEP = "\x1f"
_FORMAT = _SEP.join(("%h", "%cI", "%an", "%s"))
# A squash merge's subject ends "(#104)"; a merge commit's says "Merge pull request #104".
_PR = re.compile(r"\(#(\d{1,7})\)\s*$|^Merge pull request #(\d{1,7})\b")
# Config a repo can't override (``-c`` beats the repo's own): nothing it sets runs a program.
_GIT_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.pager=cat",
    "log.showSignature=false",
    "color.ui=false",
)
_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/var/empty",
    "LANG": "en_US.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",  # a read never takes the index lock
    "GIT_NO_REPLACE_OBJECTS": "1",  # refs/replace can't rewrite what a commit says
    "GIT_ASKPASS": "",
}

RECENT_CHANGES = ToolSpec(
    name="recent_changes",
    description=(
        "The newest commits on a repo's local checkout (git log, newest first: date, author, "
        "subject, PR number when merged from one), plus how far it is behind GitHub as of "
        "the last fetch. Use it for 'what changed', 'latest commits', 'recent PRs' or 'status "
        "of the X repo'. `repo`: a repo name from the briefing, 'resonant' for Resonant "
        "itself, or a checkout path."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "minLength": 1, "maxLength": REPO_MAX},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
        },
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=GIT_TIMEOUT_S * 4 + 5.0,  # four git commands in a row
    untrusted=True,
    grant=GRANT,
)


def _self_checkout(settings: Settings) -> Path | None:
    return settings.ops.repo_path


def repo_path(name: str, path: Path | None, code_dir: Path) -> Path:
    """A registered repo's checkout, as the briefing lists it (``briefing.repo_path``)."""
    return path if path is not None else code_dir / name


def resolve_checkout(
    conn: sqlite3.Connection, settings: Settings, scope: ReadScope, raw: object
) -> tuple[str, Path]:
    """``(label, real path)`` of the checkout ``raw`` names (see the module doc), checked
    against ``scope``; ``ToolFailed`` saying why not."""
    tool = RECENT_CHANGES.name
    if raw is not None and (not isinstance(raw, str) or not raw.strip()):
        raise ToolFailed(f"{tool}: repo must be a repo name or a path")
    key = raw.strip() if isinstance(raw, str) else ""
    own = _self_checkout(settings)
    code_dir = settings.runner.code_dir
    if key.startswith(("/", "~")):
        label, given = key, key
    elif key:
        repo = repo_store.find(conn, key)
        if repo is not None:
            label, given = repo.slug, str(repo_path(repo.name, repo.path, code_dir))
        elif key.lower() in SELF_NAMES and own is not None:
            label, given = "resonant", str(own)
        else:
            names = sorted(r.name for r in repo_store.list_all(conn))
            known = ", ".join(names[:20]) or "none registered"
            raise ToolFailed(
                f"{tool}: no repo named {key[:60]!r} (repos: {known}; 'resonant' is Resonant "
                "itself; or pass a checkout path)"
            )
    elif own is not None:
        label, given = "resonant", str(own)
    else:
        repos = repo_store.list_all(conn)
        if len(repos) != 1:
            names = ", ".join(sorted(r.name for r in repos)[:20]) or "none registered"
            raise ToolFailed(f"{tool}: say which repo ({names})")
        only = repos[0]
        label, given = only.slug, str(repo_path(only.name, only.path, code_dir))
    real = scope.check(tool, given)
    check_git_dir(scope, real, given)
    return label, real


def check_git_dir(scope: ReadScope, real: Path, given: str) -> None:
    """The checkout's own ``.git`` must hold what git will read, inside the read roots: a real
    directory (not a ``gitdir:`` file or a symlink), no ``commondir`` (a linked worktree),
    ``objects`` resolving inside the roots, and every ``objects/info/alternates`` entry too.
    Otherwise a worker-written ``.git`` could make git show commits from a repo the read
    tools can't reach. git is then pinned to this dir (``GIT_DIR``, ``GIT_WORK_TREE``)."""
    tool = RECENT_CHANGES.name
    gitdir = real / ".git"
    if gitdir.is_symlink() or not gitdir.is_dir():
        if gitdir.exists() or gitdir.is_symlink():
            raise ToolFailed(f"{tool}: {given}'s .git isn't a plain directory (a linked worktree?)")
        raise ToolFailed(f"{tool}: {given} isn't a git checkout")
    if (gitdir / "commondir").exists():
        raise ToolFailed(f"{tool}: {given} is a linked worktree; name its main checkout")
    objects = gitdir / "objects"
    scope.check(tool, str(objects))
    alternates = objects / "info" / "alternates"
    if alternates.is_symlink():
        raise ToolFailed(f"{tool}: {given} has a symlinked alternates file")
    if alternates.exists():
        try:
            lines = alternates.read_text(errors="replace").splitlines()[:20]
        except OSError:
            raise ToolFailed(f"{tool}: {given}'s alternates can't be read") from None
        for line in (x.strip() for x in lines):
            if line and not line.startswith("#"):
                scope.check(tool, os.path.normpath(objects / line))


async def _git(git: Path, cwd: Path, *args: str) -> tuple[int, str]:
    """``(exit code, stdout)`` of one hardened, read-only git command."""
    argv = [str(git), "--no-pager"]
    for kv in _GIT_CONFIG:
        argv += ["-c", kv]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        *args,
        cwd=cwd,
        # Pinned: the checked .git and this checkout, never a core.worktree or discovery.
        env={**_ENV, "GIT_DIR": str(cwd / ".git"), "GIT_WORK_TREE": str(cwd)},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdout is not None
    try:
        async with asyncio.timeout(GIT_TIMEOUT_S):
            out = b""
            while len(out) < OUT_MAX_BYTES:  # read() returns what's there, not all of it
                chunk = await proc.stdout.read(OUT_MAX_BYTES - len(out))
                if not chunk:
                    break
                out += chunk
            if len(out) >= OUT_MAX_BYTES:  # enough: don't wait on a full pipe
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()
    except TimeoutError:
        raise ToolFailed(f"{RECENT_CHANGES.name}: git timed out") from None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def parse_log(text: str) -> list[dict[str, Any]]:
    """``git log --format=_FORMAT`` output as commit dicts (redacted, clipped)."""
    commits: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split(_SEP)
        if len(parts) != 4:
            continue
        sha, date, author, subject = parts
        item: dict[str, Any] = {
            "sha": sha[:12],
            "date": date,
            "author": redact_clip(author, AUTHOR_MAX),
            "subject": redact_clip(subject, SUBJECT_MAX),
        }
        if m := _PR.search(subject):
            item["pr"] = int(m.group(1) or m.group(2))
        commits.append(item)
    return commits


async def recent_changes(
    conn: sqlite3.Connection, settings: Settings, scope: ReadScope, args: Mapping[str, Any]
) -> dict[str, Any]:
    tool = RECENT_CHANGES.name
    if extra := set(args) - {"repo", "limit"}:
        raise ToolFailed(f"{tool}: unknown arguments {sorted(extra)}")
    limit = args.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ToolFailed(f"{tool}: limit must be 1 to {MAX_LIMIT}")
    label, path = resolve_checkout(conn, settings, scope, args.get("repo"))
    git = settings.runner.git_path
    if not os.access(git, os.X_OK):
        raise ToolFailed(f"{tool}: git isn't at {git}")
    code, out = await _git(git, path, "log", f"-n{limit}", f"--format={_FORMAT}", "HEAD", "--")
    commits = parse_log(out)
    if code != 0 and not commits:  # killed at the output cap still has what it printed
        raise ToolFailed(f"{tool}: git log failed in {path} (no commits yet?)")
    result: dict[str, Any] = {
        "repo": label,
        "path": str(path),
        "commits": commits,
        "untrusted": True,
    }
    _, branch = await _git(git, path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch.strip():
        result["branch"] = redact_clip(branch.strip(), 100)
    code, counts = await _git(
        git, path, "rev-list", "--left-right", "--count", "HEAD...@{upstream}"
    )
    ahead_behind = counts.split()
    if code == 0 and len(ahead_behind) == 2 and all(c.isdigit() for c in ahead_behind):
        result["ahead"], result["behind"] = int(ahead_behind[0]), int(ahead_behind[1])
    code, fetch_head = await _git(git, path, "rev-parse", "--git-path", "FETCH_HEAD")
    if code == 0 and fetch_head.strip():
        with contextlib.suppress(OSError):
            mtime = (path / fetch_head.strip()).stat().st_mtime
            result["fetched_at"] = datetime.fromtimestamp(int(mtime), UTC).isoformat()
    result["note"] = (
        "The local checkout, not fetched now: GitHub may have newer commits (the github "
        "extension's recent_prs shows GitHub's own list)."
    )
    return result


SPECS: tuple[ToolSpec, ...] = (RECENT_CHANGES,)


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    settings = ctx.state.settings
    scope = ReadScope.from_settings(settings)

    async def _recent_changes(args: dict[str, Any]) -> dict[str, Any]:
        return await recent_changes(ctx.conn, settings, scope, args)

    return {RECENT_CHANGES.name: _recent_changes}


__all__ = ["RECENT_CHANGES", "SPECS", "handlers", "parse_log", "recent_changes"]

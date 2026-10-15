"""Checks on a run's commits before anything is pushed (docs/phase5a-plan.md D3).

They run on the commits fetched into the daemon's own mirror, through a ``GitRunner`` bound
to the mirror: nothing here ever executes git in the run's clone.

- **Refused** (``CheckFailed``, the message is the reason shown to the owner): the head
  doesn't descend from the base; too many files or bytes; a change under ``.github/``
  (unless the repo allows it); a ``.gitmodules`` change or a submodule; a runner token (or
  anything shaped like one) in the diff, a commit message, the branch name or the PR body.
- **Flagged** (``CheckResult.flagged``): files a ``pull_request`` CI run or the owner's
  tooling executes. Not refused, but shown in the done and ``/merge`` replies.

Reasons never quote the matched text, so a refusal can't leak a token.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from resonant.config import RunnerRepo
from resonant.redact import Redactor

MAX_FILES = 200
MAX_CHANGED_BYTES = 20 * 1024 * 1024
MAX_TITLE_CHARS = 120
ZERO_SHA = "0" * 40

_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-ant-[A-Za-z0-9_-]{20,}"
)

_FLAGGED_NAMES = frozenset(
    {
        ".mcp.json",
        "makefile",
        "gnumakefile",
        "justfile",
        "conftest.py",
        "noxfile.py",
        "tox.ini",
        "package.json",
        "pyproject.toml",
        "setup.py",
        ".pre-commit-config.yaml",
        ".devcontainer.json",
    }
)


class CheckFailed(Exception):
    """A run the checks refuse. ``str(e)`` is the reason, fit for ``R3 failed: <reason>``."""


class GitError(Exception):
    """A git command that exited non-zero. ``message`` is redacted stderr."""

    def __init__(self, args_: Sequence[str], returncode: int, message: str) -> None:
        super().__init__(f"git {args_[0] if args_ else ''} failed ({returncode}): {message}")
        self.returncode = returncode
        self.message = message


class GitRunner(Protocol):
    async def __call__(self, *args: str, stdin: bytes | None = None) -> bytes:
        """Run git with these args in the mirror; stdout, or ``GitError``."""
        ...


@dataclass(frozen=True)
class CheckResult:
    files: int
    changed_bytes: int
    flagged: tuple[str, ...]  # sensitive paths, sorted
    title: str  # the first commit's subject
    head_sha: str

    @property
    def flag_line(self) -> str:
        """``⚠ changes Makefile, package.json`` or an empty string."""
        return f"⚠ changes {', '.join(self.flagged)}" if self.flagged else ""


def classify_path(path: str, *, allow_github: bool = False) -> tuple[str | None, bool]:
    """``(refusal, flagged)`` for a repo path. Compared case-insensitively, since macOS
    checkouts are."""
    parts = path.lower().split("/")
    name = parts[-1]
    if parts[0] == ".github" and not allow_github:
        return "it changes .github/ (workflows and anything else Actions reads)", False
    if name == ".gitmodules":
        return "it changes .gitmodules", False
    flagged = (
        ".claude" in parts
        or ".devcontainer" in parts
        or name in _FLAGGED_NAMES
        or name.startswith("dockerfile")
        or (len(parts) >= 2 and parts[-2] == ".vscode" and name in ("tasks.json", "settings.json"))
    )
    return None, flagged


def has_secret(values: Iterable[str], text: str) -> bool:
    """Whether any value (in any encoding the redactor knows) or a token-shaped string is in
    ``text``."""
    if _SHAPES.search(text):
        return True
    redactor = Redactor(values)
    return bool(redactor) and redactor.text(text) != text


def check_texts(values: Iterable[str], **named: str) -> None:
    """Refuse if a token is in any of the named texts (``branch=…``, ``pr_body=…``)."""
    vals = list(values)
    for where, text in named.items():
        if has_secret(vals, text):
            raise CheckFailed(f"a token appears in the {where.replace('_', ' ')}")


def _parse_raw(raw: bytes) -> list[tuple[str, str, str]]:
    """``(newmode, newsha, path)`` for each entry of ``git diff --raw -z`` (no renames)."""
    items = raw.split(b"\0")
    out: list[tuple[str, str, str]] = []
    i = 0
    while i + 1 < len(items) and items[i].startswith(b":"):
        meta = items[i][1:].decode().split(" ")
        out.append((meta[1], meta[3], items[i + 1].decode(errors="surrogateescape")))
        i += 2
    return out


def _added_lines(diff: str) -> str:
    return "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


async def run_checks(
    git: GitRunner,
    *,
    base_sha: str,
    head_sha: str,
    branch: str,
    repo: RunnerRepo,
    secrets: Iterable[str],
) -> CheckResult:
    """Check ``base_sha..head_sha`` in the mirror. Raises ``CheckFailed``."""
    values = [v for v in secrets if v]
    check_texts(values, branch_name=branch)

    try:
        await git("merge-base", "--is-ancestor", base_sha, head_sha)
    except GitError as e:
        if e.returncode != 1:
            raise
        raise CheckFailed("the branch doesn't descend from its base") from None

    raw = await git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", base_sha, head_sha)
    entries = _parse_raw(raw)
    if len(entries) > MAX_FILES:
        raise CheckFailed(f"it changes {len(entries)} files (the limit is {MAX_FILES})")

    flagged: set[str] = set()
    for mode, _sha, path in entries:
        if mode == "160000":
            raise CheckFailed("it adds or changes a submodule")
        refusal, is_flagged = classify_path(path, allow_github=repo.allow_github_changes)
        if refusal:
            raise CheckFailed(refusal)
        if is_flagged:
            flagged.add(path)

    blobs = [sha for _m, sha, _p in entries if sha != ZERO_SHA]
    changed = 0
    if blobs:
        sizes = await git(
            "cat-file", "--batch-check=%(objectsize)", stdin=("\n".join(blobs) + "\n").encode()
        )
        changed = sum(int(s) for s in sizes.split() if s.isdigit())
    if changed > MAX_CHANGED_BYTES:
        raise CheckFailed(f"it changes {changed // (1024 * 1024)} MB (the limit is 20 MB)")

    log = await git("log", "-z", "--reverse", "--format=%B", f"{base_sha}..{head_sha}")
    messages = [m.decode(errors="replace") for m in log.split(b"\0") if m.strip()]
    diff = (
        await git(
            "diff", "--text", "--no-color", "--no-ext-diff", "--no-textconv", base_sha, head_sha
        )
    ).decode(errors="replace")
    added = await asyncio.to_thread(_added_lines, diff)
    await asyncio.to_thread(
        check_texts,
        values,
        commit_message="\n".join(messages),
        diff=added,
    )

    first = messages[0].strip().splitlines()[0] if messages else ""
    title = first[:MAX_TITLE_CHARS] or "Resonant run"
    return CheckResult(len(entries), changed, tuple(sorted(flagged)), title, head_sha)

"""Checks on a run's commits before anything is pushed (docs/phase5a-plan.md D3).

They run on the commits fetched into the daemon's own mirror, through a ``GitRunner`` bound
to the mirror: nothing here ever executes git in the run's clone.

- **Refused** (``CheckFailed``, the message is the reason shown to the owner): a merge
  commit; a head that doesn't descend from the base; too many files, commits or bytes; a
  change under ``.github/`` (unless the repo allows it), a ``.gitmodules`` change or a
  submodule *in any commit of the history* (so adding then removing doesn't hide it); a runner
  token (or anything shaped like one) in any commit's patch, raw commit object (author,
  committer, message), the branch name or the PR body.
- **Flagged** (``CheckResult.flagged``): files a ``pull_request`` CI run or the owner's
  tooling executes, plus symlink and mode changes. Not refused, but shown in the done and
  ``/merge`` replies.

Anything git prints that this module can't parse fails closed. Reasons never quote the
matched text, so a refusal can't leak a token.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

import yaml

from resonant.redact import Redactor


class RepoPolicy(Protocol):
    """What the checks read of a repo (a registry ``Repo``)."""

    @property
    def allow_github_changes(self) -> bool: ...


MAX_FILES = 200
MAX_COMMITS = 500
MAX_CHANGED_BYTES = 20 * 1024 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024  # the whole history's patches
MAX_TITLE_CHARS = 120
ZERO_SHA = "0" * 40
_SHA = re.compile(r"[0-9a-f]{40}")

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
        ".envrc",
        ".npmrc",
        ".gitattributes",
        "jenkinsfile",
        ".gitlab-ci.yml",
    }
)
_FLAGGED_DIRS = frozenset({".claude", ".devcontainer", ".husky", ".circleci"})


class CheckFailed(Exception):
    """A run the checks refuse. ``str(e)`` is the reason, fit for ``R3 failed: <reason>``."""


class OutputTooLarge(Exception):
    """A git command printed more than its ``max_out`` (the process is killed)."""


class GitError(Exception):
    """A git command that exited non-zero. ``message`` is redacted stderr."""

    def __init__(self, args_: Sequence[str], returncode: int, message: str) -> None:
        super().__init__(f"git {_subcommand(args_)} failed ({returncode}): {message}")
        self.returncode = returncode
        self.message = message


def _subcommand(args: Sequence[str]) -> str:
    """The git subcommand in an arg list, skipping ``-C <dir>`` and ``-c <k=v>``."""
    it = iter(args)
    for arg in it:
        if arg in ("-C", "-c"):
            next(it, None)
        elif not arg.startswith("-"):
            return arg
    return ""


class GitRunner(Protocol):
    async def __call__(
        self, *args: str, stdin: bytes | None = None, max_out: int | None = None
    ) -> bytes:
        """Run git with these args in the mirror; stdout, or ``GitError``. More than
        ``max_out`` bytes of stdout raises ``OutputTooLarge``."""
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
        any(part in _FLAGGED_DIRS for part in parts)
        or name in _FLAGGED_NAMES
        or name.startswith(("dockerfile", ".yarnrc"))
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


@dataclass(frozen=True)
class RawEntry:
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str
    path: str


def parse_raw(raw: bytes) -> list[RawEntry]:
    """Entries of ``git diff --raw -z --no-renames`` (also ``log --raw``). Anything that
    doesn't parse raises ``CheckFailed``: the checks never guess."""
    items = raw.split(b"\0")
    if items and items[-1] == b"":
        items.pop()
    out: list[RawEntry] = []
    i = 0
    while i < len(items):
        meta = items[i].lstrip(b"\n").decode(errors="replace").split(" ")
        if (
            i + 1 >= len(items)
            or not meta[0].startswith(":")
            or len(meta) != 5
            or not _SHA.fullmatch(meta[2])
            or not _SHA.fullmatch(meta[3])
        ):
            raise CheckFailed("git's diff output couldn't be read")
        out.append(
            RawEntry(
                meta[0][1:],
                meta[1],
                meta[2],
                meta[3],
                items[i + 1].decode(errors="surrogateescape"),
            )
        )
        i += 2
    return out


def added_lines(patch: str) -> str:
    """Every line a patch adds, taken from inside hunks only (so a line that starts with
    ``++`` or ``+++`` is still scanned; file headers are not hunks)."""
    out: list[str] = []
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            out.append(line[1:])
    return "\n".join(out)


def _mode_flag(e: RawEntry) -> str | None:
    if e.new_mode == "120000":
        return f"{e.path} (symlink)"
    regular = ("100644", "100755")
    if e.old_mode in regular and e.new_mode in regular and e.old_mode != e.new_mode:
        return f"{e.path} (mode change)"
    if e.new_mode == "100755" and e.old_mode not in regular:
        return f"{e.path} (executable)"
    return None


async def run_checks(
    git: GitRunner,
    *,
    base_sha: str,
    head_sha: str,
    branch: str,
    repo: RepoPolicy,
    secrets: Iterable[str],
) -> CheckResult:
    """Check ``base_sha..head_sha`` in the mirror. Raises ``CheckFailed``."""
    values = [v for v in secrets if v]
    if not (_SHA.fullmatch(base_sha) and _SHA.fullmatch(head_sha)):
        raise CheckFailed("bad commit id")
    check_texts(values, branch_name=branch)
    span = f"{base_sha}..{head_sha}"

    try:
        await git("merge-base", "--is-ancestor", base_sha, head_sha)
    except GitError as e:
        if e.returncode != 1:
            raise
        raise CheckFailed("the branch doesn't descend from its base") from None

    commits = (await git("rev-list", "--parents", span)).decode().splitlines()
    if len(commits) > MAX_COMMITS:
        raise CheckFailed(f"it has {len(commits)} commits (the limit is {MAX_COMMITS})")
    if any(len(line.split()) != 2 for line in commits):
        raise CheckFailed("the branch has a merge commit")
    shas = [line.split()[0] for line in commits]

    # Paths from every commit, so a forbidden file added and removed again is still seen.
    try:
        history = parse_raw(
            await git("log", "--raw", "-z", "--no-renames", "--no-abbrev", "--format=", span)
        )
        net = parse_raw(
            await git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", base_sha, head_sha)
        )
    except OutputTooLarge:
        raise CheckFailed("the history is too large to check") from None
    if len(net) > MAX_FILES:
        raise CheckFailed(f"it changes {len(net)} files (the limit is {MAX_FILES})")

    flagged: set[str] = set()
    for e in history:
        if "160000" in (e.old_mode, e.new_mode):
            raise CheckFailed("it adds or changes a submodule")
        refusal, is_flagged = classify_path(e.path, allow_github=repo.allow_github_changes)
        if refusal:
            raise CheckFailed(refusal)
        if is_flagged:
            flagged.add(e.path)
    for e in net:
        if (mode_flag := _mode_flag(e)) is not None:
            flagged.add(mode_flag)

    # Size: new blobs and the old blobs they replace or delete.
    blobs = sorted({s for e in net for s in (e.old_sha, e.new_sha) if s != ZERO_SHA})
    changed = 0
    if blobs:
        out = await git(
            "cat-file", "--batch-check=%(objectsize)", stdin=("\n".join(blobs) + "\n").encode()
        )
        sizes = out.split()
        if len(sizes) != len(blobs) or not all(s.isdigit() for s in sizes):
            raise CheckFailed("object sizes couldn't be read")
        changed = sum(int(s) for s in sizes)
    if changed > MAX_CHANGED_BYTES:
        raise CheckFailed(f"it changes {changed // (1024 * 1024)} MB (the limit is 20 MB)")

    # Every commit object (author, committer, message) and every commit's patch.
    try:
        raw_commits = await git(
            "cat-file", "--batch", stdin=("\n".join(shas) + "\n").encode(), max_out=MAX_LOG_BYTES
        )
        patch = (
            await git(
                "log",
                "-p",
                "--text",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--format=%H",
                span,
                max_out=MAX_LOG_BYTES,
            )
        ).decode(errors="replace")
    except OutputTooLarge:
        raise CheckFailed("the history is too large to check") from None
    added = await asyncio.to_thread(added_lines, patch)
    await asyncio.to_thread(
        check_texts,
        values,
        file_paths="\n".join(e.path for e in history),
        commit_object=raw_commits.decode(errors="replace"),
        diff=added,
    )

    log = await git("log", "-z", "--reverse", "--format=%B", span)
    messages = [m.decode(errors="replace") for m in log.split(b"\0") if m.strip()]
    first = messages[0].strip().splitlines()[0] if messages else ""
    title = first[:MAX_TITLE_CHARS] or "Resonant run"
    return CheckResult(len(net), changed, tuple(sorted(flagged)), title, head_sha)


# --- CI presence -------------------------------------------------------------------------

_WORKFLOW = re.compile(r"\.github/workflows/[^/]+\.ya?ml")
MAX_WORKFLOW_BYTES = 1024 * 1024


def runs_on_pull_request(text: str) -> bool:
    """Whether a workflow file's ``on:`` includes ``pull_request``. Conservative: a file that
    doesn't parse, or has an unexpected shape, is not CI."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    if not isinstance(doc, dict):
        return False
    mapping = cast(dict[object, object], doc)
    # YAML 1.1 reads a bare `on` key as boolean True.
    on = mapping.get("on", mapping.get(True))
    if isinstance(on, str):
        return on == "pull_request"
    if isinstance(on, list):
        return "pull_request" in cast(list[object], on)
    if isinstance(on, dict):
        return "pull_request" in cast(dict[object, object], on)
    return False


async def ci_present(git: GitRunner, rev: str) -> bool:
    """Whether ``rev`` (a sha or ref in the mirror) has a ``.github/workflows/*.yml|yaml``
    that runs on ``pull_request``. Raises ``GitError`` if ``rev`` can't be read."""
    listing = await git("ls-tree", "-r", "-z", "--name-only", rev, "--", ".github/workflows")
    for name in listing.decode(errors="replace").split("\0"):
        if not _WORKFLOW.fullmatch(name):
            continue
        try:
            blob = await git("cat-file", "blob", f"{rev}:{name}", max_out=MAX_WORKFLOW_BYTES)
        except (GitError, OutputTooLarge):
            continue
        if await asyncio.to_thread(runs_on_pull_request, blob.decode(errors="replace")):
            return True
    return False

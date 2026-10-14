"""Extension sources, staging, and the tree digest (plan D1).

Only two sources exist in Phase 3, both first-party and local:
- ``repo:<name>``: ``extensions/<name>`` in the Resonant checkout, read from the **HEAD
  commit** with ``git archive``. Uncommitted edits in the working tree are never copied.
- ``path:<abs dir>``: a directory inside a git work tree. Its tracked files are read from
  the **index** (``git ls-files -s`` + ``git cat-file``), and only when the directory is
  clean, so what is staged is exactly what git says is there.

Either way the bytes are written to a staging directory, never read again from the source.
The digest that pins a tree lives in :mod:`resonant.extensions.pin`, shared with the host.

Git runs as an argv with a timeout, an environment without ``GIT_*`` overrides, no prompts,
no optional locks and no fsmonitor, so reading a source never writes to it or runs its hooks.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import subprocess
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from resonant.extensions.pin import TreeError
from resonant_sdk.manifest import SLUG

GIT_TIMEOUT_S = 60.0
MAX_TREE_BYTES = 64 * 1024 * 1024  # an extension tree bigger than this is refused
MAX_FILES = 5000

_SLUG = re.compile(SLUG)


class SourceError(TreeError):
    """The source can't be staged: unknown form, not git, dirty, or an unsafe tree."""


@dataclass(frozen=True)
class Source:
    kind: str  # "repo" | "path"
    text: str  # "repo:<name>" | "path:<abs dir>": stored on the row and in the audit

    @property
    def arg(self) -> str:
        return self.text.split(":", 1)[1]


def parse_source(text: str) -> Source:
    """``repo:<name>`` or ``path:<absolute dir>``. Anything else is refused (plan D13)."""
    kind, sep, arg = text.partition(":")
    if not sep or kind not in ("repo", "path"):
        raise SourceError(f"unknown source {text!r}: use repo:<name> or path:/abs/dir")
    if kind == "repo":
        if not _SLUG.match(arg):
            raise SourceError(f"repo:{arg!r}: the name must be a lowercase slug")
        return Source("repo", f"repo:{arg}")
    if not arg.isprintable() or not os.path.isabs(arg):
        raise SourceError(f"path:{arg!r}: give an absolute directory")
    norm = os.path.normpath(arg)
    return Source("path", f"path:{norm}")


@dataclass(frozen=True)
class StagedFiles:
    commit: str | None
    notes: tuple[str, ...]  # shown to the owner (e.g. uncommitted edits that were skipped)


def git_env() -> dict[str, str]:
    """The caller's environment without ``GIT_*`` overrides, plus no prompts or locks."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    return env


def git(
    cwd: Path, *args: str, stdin: bytes | None = None, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    """Run ``git -C cwd <args>``. Raises SourceError on failure (when ``check``)."""
    exe = shutil.which("git")
    if exe is None:
        raise SourceError("git is not installed")
    argv = [exe, "-c", "core.fsmonitor=false", "-C", str(cwd), *args]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            input=stdin,
            capture_output=True,
            env=git_env(),
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SourceError(f"git {args[0]} failed ({type(e).__name__})") from None
    if check and proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip().splitlines()
        raise SourceError(f"git {args[0]} failed: {err[-1] if err else proc.returncode}")
    return proc


def _head(cwd: Path) -> str | None:
    proc = git(cwd, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


def _safe_rel(rel: str) -> str:
    p = PurePosixPath(rel)
    if not rel or p.is_absolute() or any(part in ("", ".", "..", ".git") for part in p.parts):
        raise SourceError(f"unsafe path in the tree: {rel!r}")
    return p.as_posix()


class _Writer:
    """Writes entries into ``dest``, enforcing size and type limits."""

    def __init__(self, dest: Path) -> None:
        self.dest = dest
        self.total = 0
        self.count = 0

    def _count(self, size: int) -> None:
        self.total += size
        self.count += 1
        if self.total > MAX_TREE_BYTES or self.count > MAX_FILES:
            raise SourceError("the extension tree is too large")

    def file(self, rel: str, data: bytes, *, executable: bool) -> None:
        self._count(len(data))
        path = self.dest / _safe_rel(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as f:  # never follows or overwrites an existing entry
            f.write(data)
        path.chmod(0o755 if executable else 0o644)

    def symlink(self, rel: str, target: str) -> None:
        self._count(len(target))
        path = self.dest / _safe_rel(rel)
        if not target or "\0" in target or os.path.isabs(target):
            raise SourceError(f"{rel}: symlink must point inside the extension dir")
        # Lexical check here; the resolve-based check in checks.dir_errors runs on the result.
        joined = os.path.normpath(os.path.join(os.path.dirname(rel), target))
        if joined == ".." or joined.startswith("../"):
            raise SourceError(f"{rel}: symlink resolves outside the extension dir")
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, path)


def stage_repo(repo: Path, name: str, dest: Path) -> StagedFiles:
    """``extensions/<name>`` at the HEAD commit of ``repo``, via ``git archive``."""
    if not (repo / ".git").exists():
        raise SourceError(f"{repo} is not a git checkout")
    commit = _head(repo)
    if commit is None:
        raise SourceError(f"{repo} has no commits")
    prefix = f"extensions/{name}/"
    if not git(repo, "ls-tree", "-d", commit, "--", f"extensions/{name}").stdout:
        raise SourceError(f"extensions/{name} is not in {repo} at {commit[:8]}")
    tar = git(repo, "archive", "--format=tar", commit, "--", prefix).stdout
    writer = _Writer(dest)
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as archive:
        for member in archive:
            if member.name.rstrip("/") + "/" == prefix or member.isdir():
                continue
            if not member.name.startswith(prefix):
                raise SourceError(f"unexpected entry in the archive: {member.name!r}")
            rel = member.name[len(prefix) :]
            if member.issym():
                writer.symlink(rel, member.linkname)
            elif member.isreg():
                f = archive.extractfile(member)
                assert f is not None
                writer.file(rel, f.read(), executable=bool(member.mode & 0o100))
            else:
                raise SourceError(f"{rel}: unsupported entry type in the tree")
    if writer.count == 0:
        raise SourceError(f"extensions/{name} is empty at {commit[:8]}")
    notes: list[str] = []
    dirty = git(repo, "status", "--porcelain", "-z", "--", prefix).stdout
    if dirty:
        notes.append(f"uncommitted changes in {prefix} are not installed (HEAD {commit[:8]} is)")
    return StagedFiles(commit, tuple(notes))


def stage_path(src: Path, dest: Path) -> StagedFiles:
    """The tracked files under ``src`` (a git work tree), read from the index. Refused when
    any tracked file there is modified, staged-but-uncommitted, or conflicted."""
    if not src.is_dir():
        raise SourceError(f"{src} is not a directory")
    if git(src, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise SourceError(f"{src} is not inside a git work tree")
    commit = _head(src)
    if commit is None:  # no commits yet: only the working tree versus the index
        dirty = git(src, "diff", "--no-ext-diff", "--name-only", "-z", "--", ".").stdout
    else:
        dirty = git(src, "status", "--porcelain", "-z", "--untracked-files=no", "--", ".").stdout
    if dirty:
        names = [n for n in dirty.decode(errors="replace").split("\0") if n][:5]
        raise SourceError(f"{src} has uncommitted changes ({', '.join(names)}); commit them first")
    listing = git(src, "ls-files", "-s", "-z", "--", ".").stdout
    entries: list[tuple[str, str, str]] = []  # (mode, sha, rel)
    for record in listing.decode().split("\0"):
        if not record:
            continue
        meta, _, rel = record.partition("\t")
        mode, sha, stage_no = meta.split()
        if stage_no != "0":
            raise SourceError(f"{rel}: unresolved merge conflict")
        if mode not in ("100644", "100755", "120000"):
            raise SourceError(f"{rel}: unsupported entry type (mode {mode}); no submodules")
        entries.append((mode, sha, rel))
    if not entries:
        raise SourceError(f"{src} has no tracked files")
    blobs = _cat_blobs(src, [sha for _, sha, _ in entries])
    writer = _Writer(dest)
    for mode, sha, rel in entries:
        if mode == "120000":
            writer.symlink(rel, blobs[sha].decode(errors="surrogateescape"))
        else:
            writer.file(rel, blobs[sha], executable=mode == "100755")
    return StagedFiles(commit, ())


def _cat_blobs(cwd: Path, shas: Sequence[str]) -> dict[str, bytes]:
    unique = sorted(set(shas))
    out = git(cwd, "cat-file", "--batch", stdin="".join(f"{s}\n" for s in unique).encode()).stdout
    blobs: dict[str, bytes] = {}
    pos = 0
    for sha in unique:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode().split()
        if len(header) != 3 or header[0] != sha or header[1] != "blob":
            raise SourceError(f"git cat-file: unexpected object for {sha[:8]}")
        size = int(header[2])
        blobs[sha] = out[nl + 1 : nl + 1 + size]
        pos = nl + 1 + size + 1  # content, then a newline
    return blobs


def make_readonly(root: Path) -> None:
    """``chmod -R a-w`` without following symlinks."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        base = Path(dirpath)
        for name in [*filenames, *dirnames]:
            p = base / name
            if not p.is_symlink():
                p.chmod(stat.S_IMODE(p.lstat().st_mode) & ~0o222)
    root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)


def remove_tree(root: Path) -> None:
    """Delete a (possibly read-only) tree. Missing is fine."""
    if not root.exists() and not root.is_symlink():
        return
    if root.is_symlink() or not root.is_dir():
        root.unlink()
        return
    for dirpath, dirnames, _ in os.walk(root):
        Path(dirpath).chmod(0o700)
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
    shutil.rmtree(root)

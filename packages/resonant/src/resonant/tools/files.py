"""Read tools for the brain (docs/brain-plan.md D5, BR-2): ``read_file``, ``list_dir``,
``search``, ``tail_log`` and ``read_transcript``. All L0 reads.

Every path goes through ``ReadScope.check``, in this order:

1. A string, absolute or ``~/…``; a ``..`` component is refused outright.
2. The deny list on the path as given (``brain.read_deny`` on top of
   ``config.DEFAULT_READ_DENY``, plus Resonant's own secrets, which always apply).
3. ``realpath``; the deny list again on the real path.
4. The real path must be inside a read root (``brain.read_roots``, real paths too). A symlink
   in a root that leads out of every root is refused.

Comparisons are NFC and case-folded (APFS ignores both). A file is then opened with
``O_NOFOLLOW | O_NONBLOCK``, must be a regular file, and its path as the kernel sees it
(``F_GETPATH``) is checked once more, so a path swapped between the check and the open is
still caught. Only text is returned: a NUL or invalid UTF-8 in the first 8 KB is "binary".

Results are outside content: every string is scrubbed of credential-shaped text
(``redact_secrets``: private key blocks, known token prefixes, JWTs, bearer values, URL
credentials, ``password=…``-style assignments), clipped, and each result carries
``untrusted: true``. The specs are ``untrusted=True`` (the brain loop taints the task, D6)
and ``grant="files"``: owner-only unless a member's grants list ``"builtin:files":
[request]``.

- ``read_file{path, offset?, limit?}``: lines ``offset..offset+limit-1`` (1-based, at most
  ``READ_LINES_MAX``), each clipped to ``LINE_MAX`` chars, at most ``CONTENT_MAX`` chars in
  all, scanning at most ``SCAN_MAX_BYTES``.
- ``list_dir{path}``: entries by name (at most ``LIST_MAX``), symlinks shown, never followed;
  denied names left out.
- ``search{pattern, path?, glob?, regex?, limit?}``: ripgrep at ``brain.rg_path`` (no config,
  no symlinks, gitignore honoured, hidden files skipped) or, without it, a bounded Python walk
  (``WALK_MAX_FILES`` files, ``WALK_MAX_BYTES``, ``WALK_DEADLINE_S``). Every hit is
  checked against the scope again. Literal unless ``regex``; smart case.
- ``tail_log{name?, n?, contains?}``: the last ``n`` lines of a file directly in
  ``logs_dir`` (default ``resonant.jsonl``), formatted and scrubbed like ``recent_logs``.
- ``read_transcript{code, n?}``: a run's transcript tail as ``{kind, text}`` entries,
  described and cleaned like ``runners/activity.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import fnmatch
import os
import re
import stat
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from resonant.config import DEFAULT_READ_DENY, Settings
from resonant.executor import ToolFailed, ToolHandler
from resonant.redact import REDACTED
from resonant.runners import activity
from resonant.tools.base import BuiltinContext
from resonant.tools.ops import LOG_FILE, LOG_LINE_MAX, log_line
from resonant.tools.ops import scrub as scrub_handles
from resonant_sdk import Effect, ToolSpec

GRANT = "files"  # members: grants: {"builtin:files": [request]}

PATH_MAX = 1024
READ_LINES_DEFAULT = 200
READ_LINES_MAX = 500
LINE_MAX = 500
CONTENT_MAX = 12_000
SCAN_MAX_BYTES = 8 * 1024 * 1024
SNIFF_BYTES = 8192
LIST_MAX = 200
SEARCH_DEFAULT = 20
SEARCH_MAX = 50
PATTERN_MAX = 200
GLOB_MAX = 100
HIT_MAX = 200
SEARCH_TIMEOUT_S = 10.0
RG_OUT_MAX_BYTES = 1024 * 1024
FILE_MAX_BYTES = 2 * 1024 * 1024  # search skips bigger files
WALK_MAX_FILES = 20_000
WALK_MAX_BYTES = 64 * 1024 * 1024
WALK_DEADLINE_S = 5.0
WALK_SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", "dist", "build", "target"})
LOG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
LOGS_DEFAULT = 20
LOGS_MAX = 100
LOG_TAIL_BYTES = 1024 * 1024
CONTAINS_MAX = 100
TRANSCRIPT_DEFAULT = 20
TRANSCRIPT_MAX = 50
SAID_MAX = 400
RESULT_MAX = 200
RUN_CODE = re.compile(r"R([1-9][0-9]{0,8})", re.ASCII | re.IGNORECASE)

_SECRET_SCRUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----.*?"
                r"(?:-----END [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----|\Z)", re.DOTALL), REDACTED),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|sk-[A-Za-z0-9_-]{20,}|xox[abprse]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[0-9A-Z]{16}"
                r"|AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}"
                r"|lin_api_[A-Za-z0-9]{20,}|sb_secret_[A-Za-z0-9_-]{10,})"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{12,}"), rf"\1\2{REDACTED}"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@'\"]+@"), rf"\1{REDACTED}@"),
    # A credential-named key (``GITHUB_TOKEN``, ``"password"``, ``api_key``) with a literal
    # value: quoted, or a bare token with both a letter and a digit. Calls are left alone.
    (re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|secret[_-]?key|token"
                r"|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)[\"']?)"
                r"(\s*[:=]\s*)(\"[^\"\n]{4,}\"|'[^'\n]{4,}'"
                r"|(?=[A-Za-z0-9._~+/=-]*[0-9])(?=[A-Za-z0-9._~+/=-]*[A-Za-z])"
                r"[A-Za-z0-9._~+/=-]{8,}(?=[\s,;]|$))"), rf"\1\2{REDACTED}"),
)  # fmt: skip


def redact_secrets(text: str) -> str:
    """Credential-shaped text replaced by ``[redacted]``. Pattern-based: the read tools
    don't know the values (``redact.Redactor`` is for known ones)."""
    for pattern, repl in _SECRET_SCRUBS:
        text = pattern.sub(repl, text)
    return text


_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")
_KEY_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")
_KEY_BODY = re.compile(r"\s*[A-Za-z0-9+/=_-]{32,}\s*")  # a line of nothing but base64


class LineRedactor:
    """``redact_secrets`` line by line, plus the lines of a private key block (which no
    single line shows whole), so line numbers stay put."""

    def __init__(self) -> None:
        self.in_key = False

    def __call__(self, line: str) -> str:
        if self.in_key:
            self.in_key = not _KEY_END.search(line)
            return REDACTED
        if _KEY_BEGIN.search(line) and not _KEY_END.search(line):
            self.in_key = True
        return redact_secrets(line)


def redact_hit(line: str) -> str:
    """A search hit seen without its neighbours: a bare base64 line may be key material."""
    return REDACTED if _KEY_BODY.fullmatch(line) else redact_secrets(line)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- scope -----------------------------------------------------------------------------


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _parts(path: Path | str) -> tuple[str, ...]:
    return tuple(_fold(p) for p in Path(path).parts)


def transcript_dir(settings: Settings) -> Path:
    """``ClaudeRunner.transcript_dir``: ``<runner state>/transcripts``."""
    return settings.runner.mirror_dir.parent / "transcripts"


def default_roots(settings: Settings) -> list[Path]:
    roots = [settings.runner.code_dir, Path.home() / "resonant"]
    if settings.ops.repo_path is not None:
        roots.append(settings.ops.repo_path)
    roots += [settings.logs_dir, settings.home / "docs", transcript_dir(settings)]
    return roots


def resonant_secrets(settings: Settings) -> list[Path]:
    """Resonant's own state that no read tool ever returns, whatever the config says."""
    db = settings.db_path
    runner = settings.runner
    return [
        db,
        *(db.with_name(db.name + s) for s in ("-wal", "-shm", "-journal")),
        settings.principals_path,
        settings.ext_data_dir,
        settings.home / "run-secrets",
        runner.secrets_dir,
        runner.mirror_dir,
        runner.work_dir,
    ]


@dataclass(frozen=True)
class ReadScope:
    roots: tuple[Path, ...]  # real paths
    deny_paths: tuple[tuple[str, ...], ...]  # folded parts, as given and real
    deny_globs: tuple[tuple[str, ...], ...]  # folded component patterns

    @classmethod
    def build(
        cls, roots: Sequence[Path], deny: Sequence[str], always: Sequence[Path] = ()
    ) -> ReadScope:
        paths: list[tuple[str, ...]] = []
        globs: list[tuple[str, ...]] = []
        for entry in deny:
            if entry.startswith(("/", "~")):
                p = Path(entry).expanduser()
                paths += [_parts(p), _parts(os.path.realpath(p))]
            else:
                globs.append(tuple(_fold(c) for c in entry.strip("/").split("/") if c))
        for p in always:
            paths += [_parts(p), _parts(os.path.realpath(p))]
        real_roots = tuple(dict.fromkeys(Path(os.path.realpath(r.expanduser())) for r in roots))
        return cls(real_roots, tuple(dict.fromkeys(paths)), tuple(g for g in globs if g))

    @classmethod
    def from_settings(cls, settings: Settings) -> ReadScope:
        brain = settings.brain
        roots = brain.read_roots if brain.read_roots is not None else default_roots(settings)
        return cls.build(roots, [*DEFAULT_READ_DENY, *brain.read_deny], resonant_secrets(settings))

    def denied(self, path: Path | str) -> bool:
        parts = _parts(path)
        for d in self.deny_paths:
            if parts[: len(d)] == d:
                return True
        for g in self.deny_globs:
            n = len(g)
            for i in range(len(parts) - n + 1):
                if all(fnmatch.fnmatchcase(parts[i + j], g[j]) for j in range(n)):
                    return True
        return False

    def inside(self, real: Path | str) -> bool:
        parts = _parts(real)
        return any(parts[: len(r)] == r for r in (_parts(root) for root in self.roots))

    def top_roots(self) -> list[Path]:
        """The roots not inside another root (so a nested one isn't searched twice)."""
        return [
            r
            for r in self.roots
            if not any(o != r and _parts(r)[: len(_parts(o))] == _parts(o) for o in self.roots)
        ]

    def visible(self, path: Path | str) -> bool:
        """For a path found by walking a root (no symlinks followed): not denied, in a root."""
        return not self.denied(path) and self.inside(path)

    def check(self, tool: str, raw: object) -> Path:
        """The real path for ``raw``, or ``ToolFailed`` saying why not."""
        if not isinstance(raw, str) or not raw.strip() or len(raw) > PATH_MAX or "\0" in raw:
            raise ToolFailed(f"{tool}: path must be a string of 1 to {PATH_MAX} characters")
        raw = raw.strip()
        if raw == "~" or raw.startswith("~/"):
            raw = str(Path.home()) + raw[1:]
        if not raw.startswith("/"):
            raise ToolFailed(f"{tool}: path must be absolute or start with ~/")
        if ".." in raw.split("/"):
            raise ToolFailed(f"{tool}: '..' isn't allowed in a path")
        given = Path(os.path.normpath(raw))
        if self.denied(given):
            raise ToolFailed(f"{tool}: {given} is on the deny list")
        real = Path(os.path.realpath(given))
        if self.denied(real):
            raise ToolFailed(f"{tool}: {given} leads to a path on the deny list")
        if not self.inside(real):
            if self.inside(given):
                raise ToolFailed(f"{tool}: {given} is a symlink out of the read roots")
            raise ToolFailed(f"{tool}: {given} is outside the read roots")
        return real

    def recheck_fd(self, tool: str, fd: int, given: Path) -> None:
        """The opened file's path as the kernel sees it must pass too (macOS ``F_GETPATH``)."""
        getpath = getattr(fcntl, "F_GETPATH", None)
        if getpath is None:
            return
        try:
            raw = fcntl.fcntl(fd, getpath, b"\0" * 1024).split(b"\0", 1)[0]
        except OSError:
            raise ToolFailed(f"{tool}: can't confirm where {given} is") from None
        real = os.fsdecode(raw)
        if self.denied(real) or not self.inside(real):
            raise ToolFailed(f"{tool}: {given} changed while it was opened")


def _open(scope: ReadScope, tool: str, real: Path, *, directory: bool) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(real, flags)
    except FileNotFoundError:
        raise ToolFailed(f"{tool}: {real} doesn't exist") from None
    except NotADirectoryError:
        raise ToolFailed(f"{tool}: {real} isn't a directory") from None
    except OSError as e:
        raise ToolFailed(f"{tool}: can't open {real} ({type(e).__name__})") from None
    try:
        mode = os.fstat(fd).st_mode
        if directory and not stat.S_ISDIR(mode):
            raise ToolFailed(f"{tool}: {real} isn't a directory")
        if not directory and stat.S_ISDIR(mode):
            raise ToolFailed(f"{tool}: {real} is a directory (use list_dir)")
        if not directory and not stat.S_ISREG(mode):
            raise ToolFailed(f"{tool}: {real} isn't a regular file")
        scope.recheck_fd(tool, fd, real)
    except BaseException:
        os.close(fd)
        raise
    return fd


def is_binary(head: bytes) -> bool:
    """A NUL, or UTF-8 that breaks before the last few bytes (a cut character is fine)."""
    if b"\0" in head:
        return True
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        return e.start < len(head) - 4
    return False


# --- args -------------------------------------------------------------------------------


def _known(tool: str, args: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(args) - allowed
    if unknown:
        raise ToolFailed(f"{tool}: unknown arguments: {sorted(unknown)}")


def _int(tool: str, args: Mapping[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    v = args.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or v < lo:
        raise ToolFailed(f"{tool}: {key} must be a whole number of at least {lo}")
    return min(v, hi)


def _bool(tool: str, args: Mapping[str, Any], key: str) -> bool:
    v = args.get(key, False)
    if not isinstance(v, bool):
        raise ToolFailed(f"{tool}: {key} must be true or false")
    return v


def _text(
    tool: str, args: Mapping[str, Any], key: str, limit: int, *, required: bool
) -> str | None:
    v = args.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v.strip() or len(v) > limit or "\0" in v:
        raise ToolFailed(f"{tool}: {key} must be a string of 1 to {limit} characters")
    return v


# --- read_file and list_dir --------------------------------------------------------------


def _lines(f: Any, budget: int) -> Iterator[tuple[str, int]]:
    """``(line, bytes read)`` with long lines cut at ``LINE_MAX`` chars (the rest skipped)."""
    used = 0
    chunk = LINE_MAX * 4 + 4
    while used < budget:
        raw: bytes = f.readline(chunk)
        if not raw:
            return
        used += len(raw)
        cut = not raw.endswith(b"\n")
        while cut and used < budget:  # skip the rest of a long line
            more: bytes = f.readline(64 * 1024)
            if not more:
                break
            used += len(more)
            cut = not more.endswith(b"\n")
        yield _clip(raw.decode("utf-8", errors="replace").rstrip("\r\n"), LINE_MAX), used


def read_file(scope: ReadScope, args: Mapping[str, Any]) -> dict[str, Any]:
    tool = "read_file"
    _known(tool, args, {"path", "offset", "limit"})
    real = scope.check(tool, args.get("path"))
    offset = _int(tool, args, "offset", 1, 1, 10**9)
    limit = _int(tool, args, "limit", READ_LINES_DEFAULT, 1, READ_LINES_MAX)
    fd = _open(scope, tool, real, directory=False)
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if is_binary(f.read(SNIFF_BYTES)):
            raise ToolFailed(f"{tool}: {real} is a binary file")
        f.seek(0)
        out: list[str] = []
        chars = 0
        number = 0
        more = clipped = False
        redact = LineRedactor()
        for line, _used in _lines(f, SCAN_MAX_BYTES):
            number += 1
            text = redact(line)  # every line, so a key block before offset is still seen
            if number < offset:
                continue
            if len(out) == limit:
                more = True
                break
            entry = f"{number}: {text}"
            if chars + len(entry) + 1 > CONTENT_MAX:
                more = clipped = True
                break
            out.append(entry)
            chars += len(entry) + 1
        else:
            more = f.read(1) != b""  # bytes left: the scan budget ran out
    result: dict[str, Any] = {
        "path": str(real),
        "size_bytes": size,
        "start": offset,
        "end": offset + len(out) - 1 if out else None,
        "content": "\n".join(out),
        "more": more,
        "untrusted": True,
    }
    if clipped:
        result["note"] = f"cut at {CONTENT_MAX} characters; read on with offset"
    if not out and number < offset:
        result["note"] = (
            f"offset is past the first {SCAN_MAX_BYTES // (1024 * 1024)} MB I read"
            if more
            else f"the file has {number} lines"
        )
    return result


def _kind(entry: os.DirEntry[str]) -> str:
    if entry.is_symlink():
        return "link"
    if entry.is_dir(follow_symlinks=False):
        return "dir"
    if entry.is_file(follow_symlinks=False):
        return "file"
    return "other"


def list_dir(scope: ReadScope, args: Mapping[str, Any]) -> dict[str, Any]:
    tool = "list_dir"
    _known(tool, args, {"path"})
    real = scope.check(tool, args.get("path"))
    fd = _open(scope, tool, real, directory=True)
    try:
        entries: list[dict[str, Any]] = []
        total = 0
        with os.scandir(fd) as it:
            for e in sorted(it, key=lambda e: e.name):
                if scope.denied(real / e.name):
                    continue
                total += 1
                if len(entries) >= LIST_MAX:
                    continue
                kind = _kind(e)
                item: dict[str, Any] = {"name": redact_secrets(e.name), "type": kind}
                if kind == "file":
                    with contextlib.suppress(OSError):
                        item["size"] = e.stat(follow_symlinks=False).st_size
                entries.append(item)
    finally:
        os.close(fd)
    return {
        "path": str(real),
        "entries": entries,
        "count": total,
        "truncated": total > len(entries),
        "untrusted": True,
    }


# --- search -----------------------------------------------------------------------------


@dataclass
class _Hits:
    limit: int
    items: list[dict[str, Any]]
    partial: str | None = None

    @property
    def full(self) -> bool:
        return len(self.items) >= self.limit

    def add(self, path: str, line: int, text: str) -> None:
        if not self.full:
            text = redact_hit(text.rstrip("\r\n")).strip()
            self.items.append({"path": path, "line": line, "text": _clip(text, HIT_MAX)})


def _matcher(pattern: str, regex: bool) -> re.Pattern[str]:
    flags = 0 if any(c.isupper() for c in pattern) else re.IGNORECASE  # smart case
    try:
        return re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error:
        raise ToolFailed("search: pattern isn't a valid regular expression") from None


def _glob_ok(glob: str | None, rel: str, name: str) -> bool:
    if glob is None:
        return True
    return fnmatch.fnmatch(rel if "/" in glob else name, glob)


def _scan_file(path: Path, matcher: re.Pattern[str], hits: _Hits) -> int:
    """Search one file (no symlink, regular, at most ``FILE_MAX_BYTES``, text); the bytes read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > FILE_MAX_BYTES:
                return 0
            data = f.read(FILE_MAX_BYTES)
    except OSError:
        return 0
    if is_binary(data[:SNIFF_BYTES]):
        return len(data)
    for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        if matcher.search(line):
            hits.add(str(path), number, line)
            if hits.full:
                break
    return len(data)


def walk_search(
    scope: ReadScope,
    roots: Sequence[Path],
    matcher: re.Pattern[str],
    glob: str | None,
    hits: _Hits,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """The fallback: a bounded walk of ``roots`` (a root may be a file). Hidden names,
    ``WALK_SKIP_DIRS``, symlinks, denied paths, big and binary files are skipped."""
    deadline = clock() + WALK_DEADLINE_S
    files = 0
    read = 0
    for root in roots:
        if not root.is_dir():
            read += _scan_file(root, matcher, hits)
            continue
        stack = [root]
        while stack and not hits.full:
            if clock() > deadline or files >= WALK_MAX_FILES or read >= WALK_MAX_BYTES:
                hits.partial = "the search budget ran out"
                return
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            subdirs: list[Path] = []
            for e in entries:
                if hits.full:
                    return
                if e.name.startswith(".") or e.is_symlink():
                    continue
                path = Path(e.path)
                if not scope.visible(path):
                    continue
                if e.is_dir(follow_symlinks=False):
                    if e.name not in WALK_SKIP_DIRS:
                        subdirs.append(path)
                elif e.is_file(follow_symlinks=False) and _glob_ok(
                    glob, str(path.relative_to(root)), e.name
                ):
                    files += 1
                    read += _scan_file(path, matcher, hits)
            stack.extend(reversed(subdirs))


def rg_argv(
    rg: Path,
    pattern: str,
    roots: Sequence[Path],
    glob: str | None,
    regex: bool,
    deny_globs: Sequence[tuple[str, ...]],
) -> list[str]:
    argv = [
        str(rg),
        "--no-config",
        "--no-messages",
        "--null",
        "--line-number",
        "--with-filename",
        "--no-heading",
        "--color=never",
        "--smart-case",
        "--max-columns=500",
        "--max-columns-preview",
        f"--max-filesize={FILE_MAX_BYTES}",
        "--max-count=10",
    ]
    if not regex:
        argv.append("--fixed-strings")
    if glob is not None:
        argv.append(f"--glob={glob}")
    # After the caller's glob: the last matching glob wins. Hits are re-checked anyway.
    argv += [f"--glob=!{'/'.join(g)}" for g in deny_globs]
    argv += [f"--regexp={pattern}", "--", *map(str, roots)]
    return argv


async def rg_search(argv: list[str], scope: ReadScope, hits: _Hits) -> None:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin"},
        start_new_session=True,
    )
    assert proc.stdout is not None
    read = 0
    try:
        async with asyncio.timeout(SEARCH_TIMEOUT_S):
            while not hits.full and read < RG_OUT_MAX_BYTES:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                read += len(raw)
                path_b, sep, rest = raw.partition(b"\0")
                number, colon, text = rest.partition(b":")
                if not sep or not colon or not number.isdigit():
                    continue
                path = os.fsdecode(path_b)
                if scope.visible(path):
                    hits.add(path, int(number), text.decode("utf-8", errors="replace"))
    except TimeoutError:
        hits.partial = "the search timed out"
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        await proc.wait()
    if read >= RG_OUT_MAX_BYTES and not hits.full:
        hits.partial = "the search output was too large"
    elif proc.returncode == 2 and not hits.items and hits.partial is None:
        hits.partial = "ripgrep reported an error (a bad regex?)"


def usable_rg(rg: Path | None) -> Path | None:
    if rg is None:
        return None
    try:
        st = os.stat(rg)
    except OSError:
        return None
    return rg if stat.S_ISREG(st.st_mode) and os.access(rg, os.X_OK) else None


async def search(scope: ReadScope, rg: Path | None, args: Mapping[str, Any]) -> dict[str, Any]:
    tool = "search"
    _known(tool, args, {"pattern", "path", "glob", "regex", "limit"})
    pattern = _text(tool, args, "pattern", PATTERN_MAX, required=True)
    assert pattern is not None
    glob = _text(tool, args, "glob", GLOB_MAX, required=False)
    regex = _bool(tool, args, "regex")
    limit = _int(tool, args, "limit", SEARCH_DEFAULT, 1, SEARCH_MAX)
    if args.get("path") is not None:
        roots = [scope.check(tool, args["path"])]
    else:
        roots = [r for r in scope.top_roots() if not scope.denied(r)]
    roots = [r for r in roots if r.exists()]
    hits = _Hits(limit, [])
    engine = "none"
    binary = usable_rg(rg)
    if roots and binary is not None:
        engine = "rg"
        argv = rg_argv(binary, pattern, roots, glob, regex, scope.deny_globs)
        try:
            await rg_search(argv, scope, hits)
        except OSError:
            engine = "python"
    if roots and engine != "rg":
        engine = "python"
        matcher = _matcher(pattern, regex)
        await asyncio.to_thread(walk_search, scope, roots, matcher, glob, hits)
    out: dict[str, Any] = {
        "pattern": pattern,
        "roots": [str(r) for r in roots],
        "hits": hits.items,
        "count": len(hits.items),
        "more": hits.full,
        "engine": engine,
        "untrusted": True,
    }
    if hits.partial is not None:
        out["partial"] = hits.partial
    return out


# --- tail_log and read_transcript ---------------------------------------------------------


def _tail_bytes(fd: int, limit: int) -> tuple[bytes, bool]:
    size = os.fstat(fd).st_size
    start = max(size - limit, 0)
    os.lseek(fd, start, os.SEEK_SET)
    data = b""
    while len(data) < size - start:
        chunk = os.read(fd, size - start - len(data))
        if not chunk:
            break
        data += chunk
    return data, start > 0


def _log_names(logs_dir: Path) -> list[str]:
    try:
        with os.scandir(logs_dir) as it:
            return sorted(
                e.name
                for e in it
                if e.is_file(follow_symlinks=False) and LOG_NAME.fullmatch(e.name)
            )[:50]
    except OSError:
        return []


def tail_log(logs_dir: Path, args: Mapping[str, Any]) -> dict[str, Any]:
    tool = "tail_log"
    _known(tool, args, {"name", "n", "contains"})
    name = _text(tool, args, "name", 64, required=False) or LOG_FILE
    if not LOG_NAME.fullmatch(name):
        raise ToolFailed(f"{tool}: name must be a file name in Resonant's logs dir")
    n = _int(tool, args, "n", LOGS_DEFAULT, 1, LOGS_MAX)
    contains = _text(tool, args, "contains", CONTAINS_MAX, required=False)
    try:
        fd = os.open(logs_dir / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return {"found": False, "name": name, "logs": _log_names(logs_dir), "untrusted": True}
    except OSError as e:
        raise ToolFailed(f"{tool}: can't open {name} ({type(e).__name__})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ToolFailed(f"{tool}: {name} isn't a regular file")
        data, cut = _tail_bytes(fd, LOG_TAIL_BYTES)
    finally:
        os.close(fd)
    raw_lines = data.decode(errors="replace").splitlines()
    if cut and raw_lines:
        raw_lines = raw_lines[1:]  # the first line is probably cut
    needle = _fold(contains) if contains is not None else None
    lines: list[str] = []
    for raw in reversed(raw_lines):
        if not raw.strip():
            continue
        line = (
            log_line(raw, clean=redact_secrets)
            if name.endswith(".jsonl")
            else _clip(scrub_handles(redact_secrets(raw)), LOG_LINE_MAX)
        )
        if needle is not None and needle not in _fold(line):
            continue
        lines.append(line)
        if len(lines) == n:
            break
    lines.reverse()
    return {
        "found": True,
        "name": name,
        "lines": lines,
        "count": len(lines),
        "n": n,
        "contains": contains,
        "untrusted": True,
    }


def _blocks(msg: Mapping[str, Any]) -> list[dict[str, Any]]:
    inner: object = msg.get("message")
    content: object = (
        cast(dict[str, object], inner).get("content") if isinstance(inner, dict) else None
    )
    if not isinstance(content, list):
        return []
    return [cast(dict[str, Any], b) for b in cast(list[object], content) if isinstance(b, dict)]


def _said(text: str, limit: int) -> str:
    return activity.clip(redact_secrets(text), limit)


def _result_text(block: Mapping[str, Any]) -> str:
    content: object = block.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for c in cast(list[object], content):
        text = cast(dict[str, object], c).get("text") if isinstance(c, dict) else None
        if isinstance(text, str):
            texts.append(text)
    return " ".join(texts)


def transcript_entries(events: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Transcript records as ``{kind, text}``: ``said``, ``call``, ``result``,
    ``turn ended``, ``session ended``. Cleaned and clipped like ``run_activity``."""
    out: list[dict[str, str]] = []
    for msg in events:
        kind = msg.get("type")
        if kind == "assistant":
            for b in _blocks(msg):
                if b.get("type") == "tool_use" and isinstance(b.get("name"), str):
                    args = b.get("input")
                    desc = activity.describe_call(
                        str(b["name"]), cast(dict[str, Any], args) if isinstance(args, dict) else {}
                    )
                    out.append({"kind": "call", "text": _said(desc, activity.CALL_MAX)})
                elif (
                    b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip()
                ):
                    out.append({"kind": "said", "text": _said(str(b["text"]), SAID_MAX)})
        elif kind == "user":
            for b in _blocks(msg):
                if b.get("type") == "tool_result":
                    text = _result_text(b)
                    out.append({"kind": "result", "text": _said(text, RESULT_MAX) if text else ""})
        elif kind == "result":
            text = msg.get("result")
            said = _said(text, SAID_MAX) if isinstance(text, str) and text.strip() else ""
            out.append({"kind": "turn ended", "text": said})
        elif kind == "resonant.exit":
            out.append({"kind": "session ended", "text": ""})
    return out


def read_transcript(directory: Path, args: Mapping[str, Any]) -> dict[str, Any]:
    tool = "read_transcript"
    _known(tool, args, {"code", "n"})
    raw = args.get("code")
    m = RUN_CODE.fullmatch(raw.strip()) if isinstance(raw, str) else None
    if m is None:
        raise ToolFailed(f"{tool}: code must be a run code like R3")
    code = f"R{int(m.group(1))}"
    n = _int(tool, args, "n", TRANSCRIPT_DEFAULT, 1, TRANSCRIPT_MAX)
    path = directory / f"{code}.jsonl"
    events = activity.read_tail(path)  # O_NOFOLLOW; [] without a file
    if not events:
        return {
            "found": False,
            "code": code,
            "text": f"No transcript for {code}.",
            "untrusted": True,
        }
    entries = transcript_entries(events)
    return {
        "found": True,
        "code": code,
        "entries": entries[-n:],
        "count": min(len(entries), n),
        "more": len(entries) > n,
        "untrusted": True,
    }


# --- specs and registration ----------------------------------------------------------------

_PATH = {"type": "string", "description": "absolute, or starting with ~/"}

READ_FILE = ToolSpec(
    name="read_file",
    description=(
        "Read a text file on the Mac mini (in ~/code, ~/resonant, Resonant's logs, docs and "
        f"run transcripts), {READ_LINES_DEFAULT} lines from `offset` (1-based) by default. "
        "Secrets are redacted; some paths (.env, keys, ~/Documents, ~/Library) are refused."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "path": _PATH,
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": READ_LINES_MAX},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=10.0,
    untrusted=True,
    grant=GRANT,
)
LIST_DIR = ToolSpec(
    name="list_dir",
    description=(
        f"List a directory on the Mac mini (where read_file reads), at most {LIST_MAX} entries."
    ),
    args_schema={
        "type": "object",
        "properties": {"path": _PATH},
        "required": ["path"],
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=10.0,
    untrusted=True,
    grant=GRANT,
)
SEARCH = ToolSpec(
    name="search",
    description=(
        "Search file contents for text (ripgrep) under `path`, or under every readable place "
        "without it. `glob` narrows the files (*.py); `regex` makes the pattern a regular "
        f"expression. At most {SEARCH_MAX} hits: path, line, text."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "minLength": 1, "maxLength": PATTERN_MAX},
            "path": _PATH,
            "glob": {"type": "string", "maxLength": GLOB_MAX},
            "regex": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_MAX},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=SEARCH_TIMEOUT_S + 10.0,
    untrusted=True,
    grant=GRANT,
)
TAIL_LOG = ToolSpec(
    name="tail_log",
    description=(
        f"The last lines (at most {LOGS_MAX}) of one of Resonant's logs ({LOG_FILE} by "
        "default), optionally only those containing `contains`. URLs and handles removed."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 64},
            "n": {"type": "integer", "minimum": 1, "maximum": LOGS_MAX},
            "contains": {"type": "string", "maxLength": CONTAINS_MAX},
        },
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=10.0,
    untrusted=True,
    grant=GRANT,
)
READ_TRANSCRIPT = ToolSpec(
    name="read_transcript",
    description=(
        "The tail of a Claude run's transcript (R3): what it said, the tools it called and "
        f"their results, cleaned and clipped, at most {TRANSCRIPT_MAX} entries."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "pattern": "^[Rr][0-9]{1,9}$"},
            "n": {"type": "integer", "minimum": 1, "maximum": TRANSCRIPT_MAX},
        },
        "required": ["code"],
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=10.0,
    untrusted=True,
    grant=GRANT,
)

SPECS: tuple[ToolSpec, ...] = (READ_FILE, LIST_DIR, SEARCH, TAIL_LOG, READ_TRANSCRIPT)


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    settings = ctx.state.settings
    scope = ReadScope.from_settings(settings)
    rg = settings.brain.rg_path
    logs_dir = settings.logs_dir
    transcripts = transcript_dir(settings)

    async def _read_file(args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(read_file, scope, args)

    async def _list_dir(args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(list_dir, scope, args)

    async def _search(args: dict[str, Any]) -> dict[str, Any]:
        return await search(scope, rg, args)

    async def _tail_log(args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(tail_log, logs_dir, args)

    async def _read_transcript(args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(read_transcript, transcripts, args)

    return {
        READ_FILE.name: _read_file,
        LIST_DIR.name: _list_dir,
        SEARCH.name: _search,
        TAIL_LOG.name: _tail_log,
        READ_TRANSCRIPT.name: _read_transcript,
    }


__all__ = [
    "GRANT",
    "SPECS",
    "ReadScope",
    "handlers",
    "list_dir",
    "read_file",
    "read_transcript",
    "redact_secrets",
    "search",
    "tail_log",
    "transcript_entries",
]

"""Run-dir cleanup, the disk watchdog and the cache prune (docs/employee-plan.md D1).

A run dir is written by the sandboxed session, so everything the daemon does to one treats it
as hostile: no path is ever followed through a symlink. Deletes and sizes walk with directory
file descriptors (``dir_fd`` all the way down, ``O_NOFOLLOW`` on every open, ``lstat``
semantics), so a symlink planted in the tree is unlinked as a link and never entered.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NamedTuple

RUN_CODE = re.compile(r"R[1-9][0-9]*")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
DAY_S = 86400.0


def _open_dir(name: str, dir_fd: int | None) -> int:
    """Open a directory without following a symlink at the last component."""
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def _entries(fd: int) -> list[tuple[str, os.stat_result]]:
    """``(name, lstat)`` for the entries of an open directory."""
    out: list[tuple[str, os.stat_result]] = []
    with os.scandir(fd) as it:
        names = [e.name for e in it]
    for name in names:
        with contextlib.suppress(FileNotFoundError):
            out.append((name, os.stat(name, dir_fd=fd, follow_symlinks=False)))
    return out


MAX_DEPTH = 256  # deeper trees are refused by deletes and counted only down to it by sizes


def _clear_flags(dir_fd: int, name: str) -> None:
    """Clear immutable flags (``uchg``) on an entry the session set them on."""
    # Python has no fchflags, so this goes by the directory's real path (F_GETPATH) and
    # doesn't follow a link at the last component. The only effect of losing a race here
    # is clearing a flag elsewhere; nothing is read, written or deleted through it.
    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is None or not hasattr(os, "chflags"):
        return
    with contextlib.suppress(OSError):
        real = fcntl.fcntl(dir_fd, getpath, b"\0" * 1024).split(b"\0", 1)[0].decode()
        os.chflags(os.path.join(real, name), 0, follow_symlinks=False)  # pyright: ignore[reportAttributeAccessIssue]


def _retry_flags(fn: Callable[[], None], dir_fd: int, name: str) -> None:
    try:
        fn()
    except PermissionError:
        _clear_flags(dir_fd, name)
        fn()


@dataclass
class _Frame:
    fd: int
    name: str  # in the parent
    parent_fd: int
    pending: list[tuple[str, os.stat_result]]


def _open_for_delete(name: str, parent_fd: int) -> _Frame:
    try:
        fd = _open_dir(name, parent_fd)
    except PermissionError:
        os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
        fd = _open_dir(name, parent_fd)
    try:
        os.fchmod(fd, 0o700)  # a directory the session made unreadable or read-only
        return _Frame(fd, name, parent_fd, _entries(fd))
    except BaseException:
        os.close(fd)
        raise


def _delete_tree(parent_fd: int, name: str) -> None:
    """Iterative delete of directory ``name`` in ``parent_fd``: one open fd per level (at
    most ``MAX_DEPTH``), no recursion, symlinks unlinked as links."""
    stack = [_open_for_delete(name, parent_fd)]
    try:
        while stack:
            top = stack[-1]
            if top.pending:
                child, st = top.pending.pop()
                if stat.S_ISDIR(st.st_mode):
                    if len(stack) >= MAX_DEPTH:
                        raise OSError(errno.ELOOP, "directory tree too deep to delete")
                    stack.append(_open_for_delete(child, top.fd))
                else:
                    _retry_flags(lambda c=child, f=top.fd: os.unlink(c, dir_fd=f), top.fd, child)
            else:
                stack.pop()
                os.close(top.fd)
                _retry_flags(
                    lambda f=top.parent_fd, n=top.name: os.rmdir(n, dir_fd=f),
                    top.parent_fd,
                    top.name,
                )
    finally:
        for frame in stack:
            os.close(frame.fd)


def rmtree_nofollow(path: Path) -> bool:
    """Delete a directory tree without following any symlink. False if it's already gone.

    ``path`` must be a real directory: a symlink there is unlinked itself (its target is left
    alone), and anything else raises ``NotADirectoryError``.
    """
    parent_fd = _open_dir(str(path.parent), None)
    try:
        try:
            st = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(st.st_mode):
            os.unlink(path.name, dir_fd=parent_fd)
            return True
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(str(path))
        _delete_tree(parent_fd, path.name)
        return True
    finally:
        os.close(parent_fd)


class DirSize(NamedTuple):
    """What ``dir_size`` counted. ``partial``: some of the tree couldn't be read (a directory
    that won't open, or one nested deeper than ``MAX_DEPTH``), so the real size is larger."""

    bytes: int
    partial: bool


def dir_size(path: Path) -> DirSize:
    """Bytes in use under ``path`` (allocated blocks), not following symlinks. 0 if missing.

    Counts what it can read: a directory that won't open is skipped, a tree deeper than
    ``MAX_DEPTH`` is counted down to that depth, and a directory that disappears mid-walk is
    skipped (it's gone, so the count stays whole). Workers are unguarded, so the watchdog is a
    net for a runaway fill, not a wall: a partial count is never treated as over a limit.
    """
    try:
        root = _open_dir(str(path), None)
    except (FileNotFoundError, NotADirectoryError):
        return DirSize(0, False)
    except OSError:
        return DirSize(0, True)
    total = 0
    partial = False
    stack: list[tuple[int, list[tuple[str, os.stat_result]]]] = []
    try:
        try:
            stack.append((root, _entries(root)))
        except OSError:
            os.close(root)
            return DirSize(0, True)
        while stack:
            fd, pending = stack[-1]
            if not pending:
                stack.pop()
                os.close(fd)
                continue
            name, st = pending.pop()
            total += st.st_blocks * 512
            if not stat.S_ISDIR(st.st_mode):
                continue
            if len(stack) >= MAX_DEPTH:
                partial = True
                continue
            try:
                child = _open_dir(name, fd)
            except FileNotFoundError:
                continue
            except OSError:
                partial = True
                continue
            try:
                stack.append((child, _entries(child)))
            except FileNotFoundError:
                os.close(child)
            except OSError:
                os.close(child)
                partial = True
        return DirSize(total, partial)
    finally:
        for fd, _ in stack:
            os.close(fd)


def free_bytes(path: Path) -> int | None:
    """Free space for an unprivileged writer on ``path``'s volume. None if it can't be read."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


DiskLimit = Literal["cap", "free"]  # which one a watcher hit: its own cap or the volume's floor
PARTIAL_CHECKS = 3  # partial counts in a row before ``on_partial`` is told (once)


async def watch_disk(
    path: Path,
    max_bytes: int,
    on_exceeded: Callable[[DiskLimit, int], Awaitable[None]],
    *,
    interval_s: float = 60.0,
    min_free_bytes: int = 0,
    on_partial: Callable[[DirSize], Awaitable[None]] | None = None,
    free: Callable[[Path], int | None] = free_bytes,
) -> None:
    """Check ``path``'s size every ``interval_s`` until cancelled. Run it as a task next to
    the session and cancel it when the session ends.

    Two limits, and on the first hit ``on_exceeded(limit, value)`` is awaited once and the
    watch returns (the caller kills the run):

    - ``"cap"``: the counted bytes are over ``max_bytes`` (``value`` is the count). A partial
      count is never over on its own account, only by what it did count.
    - ``"free"``: the volume has under ``min_free_bytes`` free and the count grew since the
      last check (``value`` is the free bytes). A tree that isn't growing isn't what's
      filling the disk, so it's left alone.

    A count that stays partial for ``PARTIAL_CHECKS`` checks in a row is passed to
    ``on_partial`` once."""
    last = (await asyncio.to_thread(dir_size, path)).bytes
    partial_runs = 0
    while True:
        await asyncio.sleep(interval_s)
        size = await asyncio.to_thread(dir_size, path)
        if size.bytes > max_bytes:
            await on_exceeded("cap", size.bytes)
            return
        grew, last = size.bytes > last, size.bytes
        if grew and min_free_bytes > 0:
            left = await asyncio.to_thread(free, path)
            if left is not None and left < min_free_bytes:
                await on_exceeded("free", left)
                return
        partial_runs = partial_runs + 1 if size.partial else 0
        if partial_runs == PARTIAL_CHECKS and on_partial is not None:
            await on_partial(size)


def delete_run_dir(work_dir: Path, code: str) -> bool:
    """Delete ``<work_dir>/<code>`` (on merge or cancel). False if there was none."""
    if not RUN_CODE.fullmatch(code):
        raise ValueError(f"bad run code {code!r}")
    return rmtree_nofollow(work_dir / code)


# --- the cache ---------------------------------------------------------------------------


def _files(root: int) -> list[tuple[float, int, tuple[str, ...]]]:
    """Every non-directory under an open dir, iteratively. Directories nested deeper than
    ``MAX_DEPTH`` or that won't open are skipped (they can't be pruned)."""
    out: list[tuple[float, int, tuple[str, ...]]] = []
    stack: list[tuple[int, tuple[str, ...], list[tuple[str, os.stat_result]]]] = [
        (os.dup(root), (), _entries(root))
    ]
    try:
        while stack:
            fd, parts, pending = stack[-1]
            if not pending:
                stack.pop()
                os.close(fd)
                continue
            name, st = pending.pop()
            if not stat.S_ISDIR(st.st_mode):
                out.append((max(st.st_atime, st.st_mtime), st.st_blocks * 512, (*parts, name)))
            elif len(stack) < MAX_DEPTH:
                try:
                    child = _open_dir(name, fd)
                except OSError:
                    continue
                stack.append((child, (*parts, name), _entries(child)))
        return out
    finally:
        for fd, _, _ in stack:
            os.close(fd)


def _unlink_at(root_fd: int, parts: tuple[str, ...]) -> None:
    """Unlink a file by walking its directories with ``O_NOFOLLOW``, so a directory swapped
    for a symlink since the scan makes this fail instead of deleting outside the cache."""
    fds: list[int] = []
    try:
        fd = root_fd
        for part in parts[:-1]:
            fd = _open_dir(part, fd)
            fds.append(fd)
        os.unlink(parts[-1], dir_fd=fd)
    finally:
        for f in fds:
            os.close(f)


def prune_cache(cache_dir: Path, max_bytes: int) -> int:
    """Delete the least recently used files until the cache is under ``max_bytes``. Returns
    the bytes freed. A cache that doesn't exist is fine."""
    try:
        root = _open_dir(str(cache_dir), None)
    except (FileNotFoundError, NotADirectoryError):
        return 0
    try:
        files = _files(root)
        total = sum(size for _, size, _ in files)
        freed = 0
        for _, size, parts in sorted(files):
            if total - freed <= max_bytes:
                break
            try:
                _unlink_at(root, parts)
            except OSError:
                continue
            freed += size
        return freed
    finally:
        os.close(root)


# --- the daily job -----------------------------------------------------------------------


@dataclass
class CleanupResult:
    removed_runs: list[str] = field(default_factory=list[str])
    cache_freed_bytes: int = 0
    errors: list[str] = field(default_factory=list[str])


def wipe_secret_dirs(secrets_dir: Path) -> int:
    """Delete every leftover per-call secret dir (a crash between writing a token file and
    removing it). Never raises. The daemon calls this at startup (before any run), and
    ``cleanup`` calls it daily. Returns how many entries were removed."""
    removed = 0
    try:
        names = os.listdir(secrets_dir)
    except OSError:
        return 0
    for name in names:
        with contextlib.suppress(Exception):
            try:
                rmtree_nofollow(secrets_dir / name)
            except NotADirectoryError:
                (secrets_dir / name).unlink()
            removed += 1
    return removed


def cleanup(
    work_dir: Path,
    cache_dir: Path,
    *,
    secrets_dir: Path | None = None,
    ended_at: Mapping[str, float],
    keep_days: int,
    max_cache_bytes: int,
    now: float | None = None,
) -> CleanupResult:
    """The daily core job. ``ended_at`` maps a run code to the epoch seconds when its run
    ended (from the ``runs`` table; running runs are left out and so are never touched).
    Deletes run dirs that ended more than ``keep_days`` ago, then prunes the cache."""
    now = time.time() if now is None else now
    result = CleanupResult()
    for code, ended in sorted(ended_at.items()):
        if not RUN_CODE.fullmatch(code) or now - ended < keep_days * DAY_S:
            continue
        try:
            if delete_run_dir(work_dir, code):
                result.removed_runs.append(code)
        except Exception as e:  # one bad run dir must not stop the rest
            result.errors.append(f"{code}: {getattr(e, 'strerror', None) or type(e).__name__}")
    try:
        result.cache_freed_bytes = prune_cache(cache_dir, max_cache_bytes)
    except Exception as e:
        result.errors.append(f"cache: {getattr(e, 'strerror', None) or type(e).__name__}")
    if secrets_dir is not None:
        wipe_secret_dirs(secrets_dir)
    return result

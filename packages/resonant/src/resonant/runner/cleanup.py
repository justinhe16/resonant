"""Run-dir cleanup, the disk watchdog and the cache prune (docs/phase5a-plan.md D3).

A run dir is written by the sandboxed session, so everything the daemon does to one treats it
as hostile: no path is ever followed through a symlink. Deletes and sizes walk with directory
file descriptors (``dir_fd`` all the way down, ``O_NOFOLLOW`` on every open, ``lstat``
semantics), so a symlink planted in the tree is unlinked as a link and never entered.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

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


def _remove_children(fd: int) -> None:
    os.fchmod(fd, 0o700)  # a directory the session made unreadable or read-only
    for name, st in _entries(fd):
        if stat.S_ISDIR(st.st_mode):
            try:
                child = _open_dir(name, fd)
            except PermissionError:
                os.chmod(name, 0o700, dir_fd=fd, follow_symlinks=False)
                child = _open_dir(name, fd)
            try:
                _remove_children(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)  # a symlink is removed as a link


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
        fd = _open_dir(path.name, parent_fd)
        try:
            _remove_children(fd)
        finally:
            os.close(fd)
        os.rmdir(path.name, dir_fd=parent_fd)
        return True
    finally:
        os.close(parent_fd)


def dir_size(path: Path) -> int:
    """Bytes in use under ``path`` (allocated blocks), not following symlinks. 0 if missing."""
    try:
        fd = _open_dir(str(path), None)
    except (FileNotFoundError, NotADirectoryError):
        return 0
    try:
        return _size(fd)
    finally:
        os.close(fd)


def _size(fd: int) -> int:
    total = 0
    for name, st in _entries(fd):
        total += st.st_blocks * 512
        if stat.S_ISDIR(st.st_mode):
            try:
                child = _open_dir(name, fd)
            except OSError:
                continue
            try:
                total += _size(child)
            finally:
                os.close(child)
    return total


async def watch_disk(
    run_dir: Path,
    max_bytes: int,
    on_exceeded: Callable[[int], Awaitable[None]],
    *,
    interval_s: float = 60.0,
) -> None:
    """Check the run dir's size every ``interval_s`` until cancelled. When it's over
    ``max_bytes``, await ``on_exceeded(size)`` once and return (the caller kills the run).
    Run it as a task next to the session and cancel it when the session ends."""
    while True:
        await asyncio.sleep(interval_s)
        size = await asyncio.to_thread(dir_size, run_dir)
        if size > max_bytes:
            await on_exceeded(size)
            return


def delete_run_dir(work_dir: Path, code: str) -> bool:
    """Delete ``<work_dir>/<code>`` (on merge or cancel). False if there was none."""
    if not RUN_CODE.fullmatch(code):
        raise ValueError(f"bad run code {code!r}")
    return rmtree_nofollow(work_dir / code)


# --- the cache ---------------------------------------------------------------------------


def _files(fd: int, parts: tuple[str, ...]) -> list[tuple[float, int, tuple[str, ...]]]:
    out: list[tuple[float, int, tuple[str, ...]]] = []
    for name, st in _entries(fd):
        if stat.S_ISDIR(st.st_mode):
            try:
                child = _open_dir(name, fd)
            except OSError:
                continue
            try:
                out.extend(_files(child, (*parts, name)))
            finally:
                os.close(child)
        else:
            out.append((max(st.st_atime, st.st_mtime), st.st_blocks * 512, (*parts, name)))
    return out


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
        files = _files(root, ())
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


def cleanup(
    work_dir: Path,
    cache_dir: Path,
    *,
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
        except OSError as e:
            result.errors.append(f"{code}: {e.strerror or type(e).__name__}")
    result.cache_freed_bytes = prune_cache(cache_dir, max_cache_bytes)
    return result

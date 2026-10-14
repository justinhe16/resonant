"""Extension pins: the tree digest of an installed copy, and its re-verification.

Pure functions over a directory. The manager computes the digest on the staged source and
again on the copy before it writes the row; the host re-verifies every enabled row at load.

``tree_digest(root)`` is a sha256 over every file and symlink under ``root`` (directories
only through their contents, like git), sorted by POSIX relative path. Each entry is
``<mode>\\0<path>\\0<sha256 hex>\\0``, where ``mode`` is git's: ``100644``, ``100755`` (any
exec bit) or ``120000`` (a symlink, hashed over its target string, never followed). Write
bits are not part of it, so the read-only copy has the same digest as its source.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Protocol

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_CHUNK = 1024 * 1024


class Pinned(Protocol):
    """What ``verify`` reads from an ``ExtensionRecord``."""

    @property
    def commit(self) -> str | None: ...

    @property
    def tree_digest(self) -> str: ...


def _entries(root: Path) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):  # never follows symlinked dirs
        base = Path(dirpath)
        names = list(filenames)
        # A symlink to a directory shows up in dirnames; hash it as a link, don't descend.
        for d in list(dirnames):
            if (base / d).is_symlink():
                dirnames.remove(d)
                names.append(d)
        for name in names:
            p = base / name
            rel = p.relative_to(root).as_posix()
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(p)
                out.append(("120000", rel, hashlib.sha256(target.encode()).hexdigest()))
            elif stat.S_ISREG(st.st_mode):
                mode = "100755" if st.st_mode & 0o111 else "100644"
                out.append((mode, rel, _file_sha(p)))
            else:
                raise ValueError(f"{rel}: not a regular file or symlink")
    return sorted(out, key=lambda e: e[1])


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(root: Path) -> str:
    """The sha256 hex digest of the tree under ``root`` (see the module docstring).

    Raises ``FileNotFoundError`` if ``root`` isn't a directory and ``ValueError`` for a
    special file (fifo, socket, device).
    """
    if not root.is_dir() or root.is_symlink():
        raise FileNotFoundError(f"{root} is not a directory")
    h = hashlib.sha256()
    for mode, rel, digest in _entries(root):
        h.update(f"{mode}\0{rel}\0{digest}\0".encode())
    return h.hexdigest()


def escaping_symlinks(root: Path) -> list[str]:
    """Relative paths of symlinks under ``root`` that resolve outside it."""
    real = root.resolve()
    out: list[str] = []
    for mode, rel, _ in _entries(root):
        if mode == "120000" and not (root / rel).resolve().is_relative_to(real):
            out.append(rel)
    return out


def verify(record: Pinned, root: Path) -> str | None:
    """None if the copy at ``root`` still matches the record's pins, else the reason.

    Checks: ``commit`` is None or a full 40-hex sha; the copy exists; no symlink escapes
    it; and its ``tree_digest`` equals the pinned one. The reason never includes file
    contents.
    """
    if record.commit is not None and not _COMMIT.match(record.commit):
        return "commit pin is not a full sha"
    try:
        if escaping_symlinks(root):
            return "a symlink resolves outside the extension dir"
        digest = tree_digest(root)
    except FileNotFoundError:
        return "the installed copy is missing"
    except (OSError, ValueError) as e:
        return f"the installed copy can't be read ({type(e).__name__})"
    if digest != record.tree_digest:
        return "tree digest changed"
    return None

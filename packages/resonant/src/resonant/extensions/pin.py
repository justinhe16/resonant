"""Pins: the tree digest of an extension copy, and verifying a copy against its row (D1).

Shared by the manager (install, ``ext list``) and the host (every load). Pure: nothing here
writes, runs a process, or reads anything outside the given root.

``tree_digest(root)`` is sha256 over the sorted ``path\\0kind\\0sha256(content)\\0`` of every
entry under ``root``, where kind is ``f`` (file), ``x`` (owner-executable file) or ``l``
(symlink; its content is the link target, never followed). Permissions other than the exec
bit don't count, so the read-only copy has the same digest as the staging dir it came from.
Special files (fifos, devices, sockets) raise ``TreeError``.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from resonant.store.extensions import ExtensionRecord

Kind = str  # "f" | "x" | "l"
TreeEntries = Mapping[str, tuple[Kind, str]]  # rel posix path -> (kind, sha256 hex)
CopyState = Literal["ok", "changed", "missing"]


class TreeError(Exception):
    """A tree can't be pinned (an unsupported entry, or an unsafe one)."""


def tree_entries(root: Path) -> dict[str, tuple[Kind, str]]:
    """Every entry under ``root`` as ``rel -> (kind, sha256)``. Symlinks are never followed."""
    out: dict[str, tuple[Kind, str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in [*dirnames, *filenames]:
            path = base / name
            rel = path.relative_to(root).as_posix()
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(path).encode(errors="surrogateescape")
                out[rel] = ("l", hashlib.sha256(target).hexdigest())
            elif stat.S_ISREG(st.st_mode):
                kind = "x" if st.st_mode & stat.S_IXUSR else "f"
                out[rel] = (kind, _file_sha(path))
            elif not stat.S_ISDIR(st.st_mode):
                raise TreeError(f"{rel}: unsupported file type")
        dirnames[:] = [d for d in dirnames if not (base / d).is_symlink()]
    return out


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_of(entries: TreeEntries) -> str:
    h = hashlib.sha256()
    for rel in sorted(entries):
        kind, sha = entries[rel]
        h.update(f"{rel}\0{kind}\0{sha}\0".encode(errors="surrogateescape"))
    return h.hexdigest()


def tree_digest(root: Path) -> str:
    """The pin for the tree at ``root``."""
    return digest_of(tree_entries(root))


def verify_copy(record: ExtensionRecord, root: Path) -> CopyState:
    """The copy at ``root`` against ``record.tree_digest``: ``ok``, ``changed`` (any
    difference, an unreadable entry or a special file) or ``missing`` (no such dir).

    The commit is part of the approval, not of the bytes; the digest covers every byte
    (``resonant.yaml`` included), so a matching digest means the approved tree."""
    if root.is_symlink() or not root.is_dir():
        return "missing"
    try:
        return "ok" if tree_digest(root) == record.tree_digest else "changed"
    except (OSError, TreeError):
        return "changed"

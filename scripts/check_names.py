#!/usr/bin/env python3
"""Fail when a known real person's name appears in a tracked file (the repo is public).

Tests, fixtures and doc examples use neutral names (alice, bob, carol). This guard keeps it
that way without publishing the names it guards against:

- ``scripts/names_denylist.json`` holds a salt and the salted SHA-256 of each denied name
  (lowercase), never the names. It keeps the names out of a plain grep or code search; a
  determined reader could still try a dictionary of first names against the hashes.
- ``NAME_DENYLIST`` (env, e.g. a GitHub secret): optional extra names, comma- or
  whitespace-separated, checked in plain text and never printed.

Every tracked text file is split into letter runs, and each run also into its camelCase parts
(``ownerId`` -> ``owner``, ``id``). A run or part whose lowercase form is denied is reported as
``path:line: denied name x***`` (masked) and the exit code is 1.

    uv run python scripts/check_names.py              # check the repo
    uv run python scripts/check_names.py --hash NAME  # print the hash line to add a name
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DENYLIST = ROOT / "scripts" / "names_denylist.json"
ENV = "NAME_DENYLIST"
# Generated or vendored: not hand-written, and random letter runs in them would be noise.
SKIP_PREFIXES = ("dashboard/dist/",)
SKIP_NAMES = frozenset({"uv.lock", "package-lock.json", "names_denylist.json"})

_RUN = re.compile(r"[A-Za-z]+")
_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")


def digest(salt: str, token: str) -> str:
    return hashlib.sha256(f"{salt}:{token.lower()}".encode()).hexdigest()


class Denylist:
    def __init__(self, salt: str, hashes: set[str], plain: set[str]) -> None:
        self.salt = salt
        self.hashes = hashes
        self.plain = plain
        self._seen: dict[str, bool] = {}

    @classmethod
    def load(cls, path: Path, env: str | None) -> Denylist:
        data = json.loads(path.read_text())
        plain = {t.lower() for t in re.split(r"[\s,]+", env or "") if t}
        return cls(str(data["salt"]), {str(h) for h in data["sha256"]}, plain)

    def denied(self, token: str) -> bool:
        low = token.lower()
        hit = self._seen.get(low)
        if hit is None:
            hit = low in self.plain or digest(self.salt, low) in self.hashes
            self._seen[low] = hit
        return hit


def tokens(line: str) -> set[str]:
    out: set[str] = set()
    for run in _RUN.findall(line):
        out.add(run)
        out.update(_PART.findall(run))
    return out


def tracked_files(root: Path) -> list[str]:
    res = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607 - git from PATH, as in CI
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    )
    return [p for p in res.stdout.split("\0") if p]


def skipped(rel: str) -> bool:
    return rel.startswith(SKIP_PREFIXES) or Path(rel).name in SKIP_NAMES


def mask(token: str) -> str:
    return token[0] + "*" * (len(token) - 1)


def scan(root: Path, deny: Denylist) -> list[str]:
    found: list[str] = []
    for rel in tracked_files(root):
        path = root / rel
        if skipped(rel) or not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue  # binary
        text = raw.decode("utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            for tok in sorted({t.lower() for t in tokens(line)}):
                if deny.denied(tok):
                    found.append(f"{rel}:{n}: denied name {mask(tok)}")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--root", type=Path, default=ROOT, help="repo to check (default: this one)")
    ap.add_argument("--denylist", type=Path, default=DENYLIST, help="salted hash file")
    ap.add_argument("--hash", metavar="NAME", help="print NAME's salted hash and exit")
    args = ap.parse_args(argv)
    deny = Denylist.load(args.denylist, os.environ.get(ENV))
    if args.hash:
        print(digest(deny.salt, args.hash))
        return 0
    if not deny.plain:
        print(f"note: {ENV} is not set; checking the {len(deny.hashes)} hashed names only")
    found = scan(args.root, deny)
    for line in found:
        print(line)
    if found:
        print(
            f"{len(found)} real name(s) found; use neutral names (alice, bob, carol) instead.",
            file=sys.stderr,
        )
        return 1
    print("names: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

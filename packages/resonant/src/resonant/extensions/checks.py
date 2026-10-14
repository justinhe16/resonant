"""Checks that relate a manifest to its directory and to this installation.

The SDK's ``Manifest`` validates the file on its own. These need more context:
- ``approver_errors``: approvers against ``principals.yaml``.
- ``name_errors``: the extension name against builtin command words.
- ``dir_errors``: the extension directory. Stdlib only (no dependency files, so no
  build-time code ever runs), every command's argv[0] on the allowlist, and no symlink that
  resolves outside the root.

``ext validate``, ``ext add`` and the extension host's load call them (wired in later).
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from resonant.principals import Principals
from resonant.router.commands import BY_NAME
from resonant_sdk import Manifest

# argv[0] words allowed besides a file inside the extension dir. python3 means the daemon's
# own interpreter; gh and git are looked up on the extension PATH and pinned at install.
ALLOWED_COMMANDS = frozenset({"python3", "gh", "git"})
EXT_PATH = "/opt/homebrew/bin:/usr/bin:/bin"
# Commands that fetch and run code at call time. Refused by name, with a clearer message.
FETCH_RUNNERS = frozenset(
    {"npx", "bunx", "pnpx", "pipx", "uvx", "uv", "docker", "podman", "curl", "wget"}
)
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "env"})
RESERVED_WORDS = frozenset({"ext"})  # builtin commands that land in a later ticket

DEPENDENCY_FILES = frozenset({"uv.lock", "package.json", "setup.py", "setup.cfg", "Pipfile"})
_SKIP_DIRS = frozenset({".git", "__pycache__"})


def approver_errors(manifest: Manifest, principals: Principals) -> list[str]:
    """Each declared approver must exist and hold ``approve`` for this extension."""
    errors: list[str] = []
    for name in manifest.approvers:
        if name not in principals.principals:
            errors.append(f"approver {name!r} is not a known principal")
        elif not principals.can(name, "approve", manifest.name):
            errors.append(f"approver {name!r} lacks the approve grant for {manifest.name!r}")
    return errors


def name_errors(manifest: Manifest) -> list[str]:
    """The extension name becomes ``/<name>``, so it may not shadow a builtin command."""
    builtin = {c.lstrip("/") for c in BY_NAME} | RESERVED_WORDS
    if manifest.name in builtin:
        return [f"extension name {manifest.name!r} is a builtin command"]
    return []


def commands(manifest: Manifest) -> list[tuple[str, str]]:
    """Every command the manifest can run, as ``(where, run)``."""
    out = [(f"tool {name}", t.run) for name, t in manifest.tools.items()]
    out += [(f"job {j.id}", j.run) for j in manifest.jobs]
    if manifest.health:
        out.append(("health", manifest.health.check))
    if manifest.mcp:
        out.append(("mcp", shlex.join(manifest.mcp.command)))
    return out


def dir_errors(root: Path, manifest: Manifest) -> list[str]:
    """Stdlib only, the argv[0] allowlist, and no escaping symlinks under ``root``."""
    root = root.resolve()
    errors = _dependency_errors(root) + _symlink_errors(root)
    for where, run in commands(manifest):
        if err := argv0_error(root, run):
            errors.append(f"{where}: {err}")
    return errors


def argv0_error(root: Path, run: str) -> str | None:
    """Why ``run``'s argv[0] isn't allowed, or None. ``root`` must be resolved.

    Allowed: a file inside ``root`` (``./cli.py``), ``python3 <.py file inside root>``,
    ``gh`` or ``git``. Everything else is refused; fetch-runners and shells by name.
    """
    try:
        argv = shlex.split(run)
    except ValueError as e:
        return f"cannot parse {run!r}: {e}"
    if not argv:
        return "empty command"
    word = argv[0]
    if "{" in word:
        return f"argv[0] {word!r} may not be a placeholder"
    name = Path(word).name
    if name in FETCH_RUNNERS:
        return f"{word!r} fetches and runs code at call time; refused"
    if name in SHELLS:
        return f"{word!r}: commands never run through a shell; refused"
    if "/" in word:
        return _inside_error(root, word)
    if word not in ALLOWED_COMMANDS:
        allowed = ", ".join(sorted(ALLOWED_COMMANDS))
        return f"{word!r} is not allowed; use a file in the extension dir or one of {allowed}"
    if word == "python3":
        script = argv[1] if len(argv) > 1 else ""
        if not script.endswith(".py") or script.startswith("-") or "{" in script:
            return "python3 must be followed by a .py script in the extension dir"
        return _inside_error(root, script)
    return None


def resolve_argv0(root: Path, run: str, path: str = EXT_PATH) -> str:
    """The absolute path ``run``'s argv[0] runs, for pinning at install. Raises ValueError.

    ``python3`` is the daemon's own interpreter. ``gh``/``git`` are looked up on ``path``.
    """
    root = root.resolve()
    if err := argv0_error(root, run):
        raise ValueError(err)
    word = shlex.split(run)[0]
    if word == "python3":
        return sys.executable
    if "/" in word:
        return str((root / word).resolve())
    found = shutil.which(word, path=path)
    if found is None:
        raise ValueError(f"{word!r} not found on {path}")
    return str(Path(found).resolve())


def _inside_error(root: Path, rel: str) -> str | None:
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        return f"{rel!r} resolves outside the extension dir"
    if not target.is_file():
        return f"{rel!r} is not a file in the extension dir"
    return None


def _walk(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):  # never descends into symlinked dirs
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        base = Path(dirpath)
        yield from (base / d for d in dirnames)
        yield from (base / f for f in filenames)


def _dependency_errors(root: Path) -> list[str]:
    errors: list[str] = []
    for p in _walk(root):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(root)
        if p.name in DEPENDENCY_FILES or (
            p.name.startswith("requirements") and p.name.endswith(".txt")
        ):
            errors.append(f"{rel}: extensions are stdlib only; dependency files are refused")
        elif p.name == "pyproject.toml" and (why := _pyproject_deps(p)):
            errors.append(f"{rel}: extensions are stdlib only; {why}")
    return errors


def _pyproject_deps(path: Path) -> str | None:
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return f"unreadable pyproject.toml ({e})"
    keys = (
        "project.dependencies",
        "project.optional-dependencies",
        "dependency-groups",
        "tool.poetry.dependencies",
    )
    found = [k for k in keys if _dig(data, *k.split("."))]
    return f"it declares {', '.join(found)}" if found else None


def _dig(data: Any, *path: str) -> Any:
    for key in path:
        try:
            data = data[key]
        except (KeyError, TypeError, IndexError):
            return None
    return data


def _symlink_errors(root: Path) -> list[str]:
    errors: list[str] = []
    for p in _walk(root):
        if p.is_symlink() and not p.resolve().is_relative_to(root):
            errors.append(f"{p.relative_to(root)}: symlink resolves outside the extension dir")
    return errors

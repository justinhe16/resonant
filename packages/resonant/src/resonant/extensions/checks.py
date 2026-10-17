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
from urllib.parse import urlsplit

from resonant.principals import Principals
from resonant.router.commands import BY_NAME
from resonant.sandbox import guarded_tool
from resonant_sdk import Manifest
from resonant_sdk.manifest import McpHttpServer, mcp_url_problem

# argv[0] words allowed besides a file inside the extension dir. python3 means the daemon's
# own interpreter; gh and git are looked up on EXT_PATH and pinned at install.
ALLOWED_COMMANDS = frozenset({"python3", "gh", "git"})
# Binaries pinned by absolute path at install: as argv[0], or a manifest's ``bins`` (which an
# extension's own code runs, given as RESONANT_BIN_<NAME>). Only the install-time lookup
# searches /opt/homebrew/bin; a process's PATH (extensions.process.path) never needs it.
PINNED_BINS = frozenset({"gh", "git"})
EXT_PATH = "/opt/homebrew/bin:/usr/bin:/bin"
# Commands that fetch and run code at call time. Refused by name, with a clearer message.
FETCH_RUNNERS = frozenset(
    {"npx", "bunx", "pnpx", "pipx", "uvx", "uv", "docker", "podman", "curl", "wget"}
)
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "env"})
MCP_SHELLS = SHELLS | {"ksh", "csh", "tcsh"}
# Interpreters and package managers, refused as an MCP server command (any ``python*`` too):
# their arguments name code the install pin doesn't cover.
INTERPRETERS = frozenset(
    {
        "node", "nodejs", "deno", "bun", "ruby", "perl", "php", "java", "lua", "osascript",
        "npm", "pnpm", "yarn", "pip", "pip3", "brew", "go", "cargo", "poetry", "pdm",
        "swift", "dotnet", "tsx", "ts-node", "corepack", "nix", "busybox", "awk", "xargs",
        "nohup", "sudo", "su", "doas", "timeout", "nice", "caffeinate", "open",
    }
)  # fmt: skip
RESERVED_WORDS: frozenset[str] = frozenset()  # command words reserved beyond BY_NAME

DEPENDENCY_FILES = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "pdm.lock",
        "Pipfile",
        "Pipfile.lock",
        "package.json",
        "setup.py",
        "setup.cfg",
        "environment.yml",
    }
)
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
    return out


def dir_errors(root: Path, manifest: Manifest) -> list[str]:
    """Stdlib only, the argv[0] allowlist, and no escaping symlinks under ``root``."""
    root = root.resolve()
    errors = _dependency_errors(root) + _symlink_errors(root)
    for where, run in commands(manifest):
        if err := argv0_error(root, run):
            errors.append(f"{where}: {err}")
    if manifest.mcp:
        for sid, server in manifest.mcp.servers.items():
            if isinstance(server, McpHttpServer):
                err = mcp_url_error(server.url)
            else:
                err = mcp_command_error(root, server.command)
            if err:
                errors.append(f"mcp server {sid}: {err}")
    return errors


# Plain http to a loopback host is for tests only (their fake server); never set in the
# daemon. Production MCP-over-HTTP is https only, so the bearer token never crosses a
# socket another local process could squat on.
ALLOW_LOOPBACK_HTTP = False


def mcp_url_error(url: str) -> str | None:
    """Why an MCP http server's ``url`` isn't allowed, or None: https only (see
    ``ALLOW_LOOPBACK_HTTP``)."""
    if problem := mcp_url_problem(url):
        return problem
    if urlsplit(url).scheme != "https" and not ALLOW_LOOPBACK_HTTP:
        return "url must be https"
    return None


def mcp_key(server_id: str) -> str:
    """The ``ExtensionRecord.argv0_paths`` key an MCP server's command is pinned under."""
    return f"mcp:{server_id}"


def mcp_command_error(root: Path, command: str) -> str | None:
    """Why an MCP server ``command`` isn't allowed, or None. ``root`` must be resolved.

    Allowed: a ``.py`` file in the extension dir (``./server.py``, run under the daemon's
    interpreter with ``-I -S``), or a server binary by name (looked up at install) or by
    absolute path, pinned by :func:`resolve_mcp_command`. Fetch-runners, shells and
    interpreters are refused by name: they would run code the pin doesn't cover.
    """
    if not command or any(c.isspace() for c in command):
        return "command must be one word; put arguments in args"
    name = Path(command).name
    if name in FETCH_RUNNERS:
        return f"{command!r} fetches and runs code at call time; refused"
    if name in MCP_SHELLS:
        return f"{command!r}: commands never run through a shell; refused"
    if name in INTERPRETERS or name.startswith("python"):
        return (
            f"{command!r} is an interpreter; use a .py file in the extension dir or a "
            "pinned server binary"
        )
    if command.startswith("/"):
        return None
    if "/" in command:
        if err := _inside_error(root, command):
            return err
        if not command.endswith(".py"):
            return f"{command!r}: files in the extension dir run as Python; name it *.py"
    return None


def resolve_mcp_command(root: Path, command: str, path: str = EXT_PATH) -> str:
    """The absolute path an MCP server ``command`` runs, for pinning at install. Raises
    ValueError.

    An in-dir ``.py`` resolves inside ``root``. A binary (by name on ``path``, or absolute)
    must be an executable file no Claude worker can replace: root-owned all the way up, or a
    Homebrew keg the sandbox guard protects (as for ``bins``).
    """
    root = root.resolve()
    if err := mcp_command_error(root, command):
        raise ValueError(err)
    if "/" in command and not command.startswith("/"):
        return str((root / command).resolve())
    found = command if command.startswith("/") else shutil.which(command, path=path)
    if found is None or not os.path.isfile(found) or not os.access(found, os.X_OK):
        raise ValueError(f"{command!r} is not an executable on {path}")
    real = Path(os.path.realpath(found))
    if err := mcp_command_error(root, str(real)):  # a link named "server" to uvx or node
        raise ValueError(f"{command!r} resolves to {real}: {err}")
    if not (_unwritable(real) or guarded_tool(Path(found))):
        raise ValueError(
            f"{command!r} resolves to {real}, which Claude workers can write; install the "
            "server root-owned (for example in /usr/local/bin with sudo)"
        )
    return str(real)


def argv0_error(root: Path, run: str) -> str | None:
    """Why ``run``'s argv[0] isn't allowed, or None. ``root`` must be resolved.

    Allowed: a ``.py`` file inside ``root`` (``./cli.py``), ``python3 <.py file inside root>``,
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
        if err := _inside_error(root, word):
            return err
        # The process runner runs in-dir files only as Python, under the daemon interpreter.
        if not word.endswith(".py"):
            return f"{word!r}: files in the extension dir run as Python; name it *.py"
        return None
    if word not in ALLOWED_COMMANDS:
        allowed = ", ".join(sorted(ALLOWED_COMMANDS))
        return f"{word!r} is not allowed; use a file in the extension dir or one of {allowed}"
    if word == "python3":
        script = argv[1] if len(argv) > 1 else ""
        if not script.endswith(".py") or script.startswith("-") or "{" in script:
            return "python3 must be followed by a .py script in the extension dir"
        return _inside_error(root, script)
    return None


def bin_key(name: str) -> str:
    """The ``ExtensionRecord.argv0_paths`` key a manifest ``bins`` entry is pinned under (not
    a spec name: those never hold a colon)."""
    return f"bin:{name}"


def bin_env(name: str) -> str:
    """The variable a pinned ``bins`` entry reaches the extension's process in."""
    return f"RESONANT_BIN_{name.upper()}"


def _unwritable(path: Path) -> bool:
    """Neither ``path`` nor any directory above it is writable by this user (root-owned or
    SIP-protected), so no worker running as this user can replace it."""
    return not any(os.access(p, os.W_OK) for p in (path, *path.parents))


def resolve_bin(name: str, path: str = EXT_PATH) -> str:
    """The real file ``name`` (``gh``/``git``) runs, looked up on ``path``, for pinning at
    install. Raises ValueError when it is missing, or when it sits where a Claude worker
    could replace it: it must be root-owned all the way up, or a Homebrew keg the sandbox
    guard write-protects (``sandbox.guarded_tool``)."""
    if name not in PINNED_BINS:
        raise ValueError(f"{name!r} can't be pinned; only {', '.join(sorted(PINNED_BINS))}")
    found = shutil.which(name, path=path)
    if found is None:
        raise ValueError(f"{name!r} not found on {path}")
    real = Path(os.path.realpath(found))
    if not (_unwritable(real) or guarded_tool(Path(found))):
        raise ValueError(
            f"{name!r} resolves to {real}, which Claude workers can write and the guard doesn't"
            f" protect; install it with Homebrew (brew install {name})"
        )
    return str(real)


def resolve_argv0(root: Path, run: str, path: str = EXT_PATH) -> str:
    """The absolute path ``run``'s argv[0] runs, for pinning at install. Raises ValueError.

    ``python3`` is the daemon's own interpreter. ``gh``/``git`` are :func:`resolve_bin`.
    """
    root = root.resolve()
    if err := argv0_error(root, run):
        raise ValueError(err)
    word = shlex.split(run)[0]
    if word == "python3":
        return sys.executable
    if "/" in word:
        return str((root / word).resolve())
    return resolve_bin(word, path)


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
        "project.dynamic",
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

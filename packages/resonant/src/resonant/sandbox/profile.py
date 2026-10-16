"""The Seatbelt sandbox for Claude workers: the thin guard's template, parameters and ``wrap``.

``sandbox-exec -p <profile> -D KEY=VALUE ... argv``. The profile is ``runner.sb`` with a few
placeholder lines filled in by name only: every path reaches SBPL as a ``-D`` parameter,
``os.path.realpath``-ed first (``/tmp``, ``/var`` and ``/etc`` are symlinks into ``/private``
and SBPL matches real paths) and refused if it is not absolute or holds a quote, a backslash,
a closing paren or a control character. See docs/employee-plan.md (D2).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
API_PORT = 7777
GIT_NAME = "Resonant runner"
GIT_EMAIL = "resonant-runner@users.noreply.github.com"

# Toolchain dirs whose ``bin`` subdirs go on a session's PATH (runners/claude.py). Reads are
# allowed everywhere, so these no longer widen what a session can see. Entries that don't
# exist are dropped.
DEFAULT_READ_PATHS = (
    "~/.local/share/claude",
    "~/.local/share/uv/python",
    "~/.cargo/bin",
    "~/.rustup/toolchains",
    "~/.nvm/versions",
    "~/.bun/bin",
)
# Binaries the daemon itself runs outside the guard, by absolute path, with credentials:
# gh (runner.gh_path, the run's token; an extension's pinned argv0), uv (ops.uv_path,
# /update), ollama (its LaunchAgent) and git (runner.git_path when configured elsewhere; an
# extension's pinned argv0). A worker that could replace one would run code outside the
# sandbox, so every copy in the install dirs and the configured paths, as the link, its real
# file and its Homebrew keg, is write-protected. (The default git is /usr/bin/git under SIP;
# extensions' python3 is the daemon's venv.)
DAEMON_TOOLS = ("gh", "uv", "ollama", "git")
# Install dirs searched for those tools and for the tools linked into a session, after
# ~/.local/bin (host_tool_path).
HOST_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
# Tools resolve_tool returns only as the system copy, never from a dir a worker can write:
# git is also what the daemon itself runs (runner.git_path), so keep the two the same binary.
SYSTEM_ONLY_TOOLS = frozenset({"git"})
# Binaries that may be linked into the run-local bin/.
NEVER_LINKED = frozenset({"resonant"})

_PLACEHOLDERS = {
    "protect": ";; @PROTECT@",
    "deny_read": ";; @DENY_READ@",
    "ancestors": ";; @ANCESTORS@",
    "exec": ";; @DENY_EXEC@",
}


class SandboxError(Exception):
    """A path or setting the sandbox refuses, or a sandbox that can't be built."""


def checked_path(path: str | os.PathLike[str], *, follow: bool = True) -> str:
    """The real, absolute form of ``path``, or SandboxError if it can't go into a profile.
    With ``follow=False`` only the parent is resolved, so a symlink stays itself (to protect
    the link, not only its target)."""
    text = os.fspath(path)
    if not os.path.isabs(text):
        raise SandboxError(f"not an absolute path: {text!r}")
    if "\0" in text:
        real = text
    elif follow:
        real = os.path.realpath(text)
    else:
        head, tail = os.path.split(os.path.normpath(text))
        real = os.path.join(os.path.realpath(head), tail) if tail else os.path.realpath(head)
    for ch in real:
        if ch in '"\\)' or ord(ch) < 32 or ord(ch) == 127:
            raise SandboxError(f"path has a character the sandbox refuses: {real!r}")
    return real


@dataclass(frozen=True)
class SandboxParams:
    """One session's guard. Paths are normalised by ``resolved()`` before use.

    ``read_paths`` and ``work_dir`` are accepted for the callers' sake and no longer limit
    anything: reads are allowed everywhere and runs no longer hide from each other."""

    run_dir: Path
    cache_dir: Path
    secrets_dir: Path  # runner.secrets_dir: required, never readable or writable
    home: Path = field(default_factory=Path.home)
    read_paths: tuple[Path, ...] = ()
    deny_exec: tuple[Path, ...] = ()
    deny_read: tuple[Path, ...] = ()  # runner.deny_read: extra paths, read and write denied
    work_dir: Path | None = None
    # write-protected paths: the installed daemon (None: ``default_protected()``)
    protect: tuple[Path, ...] | None = None
    # the run's worktree and its checkout's .git, or its scratch dir (EM-2). Writes are allowed
    # everywhere, so this grants nothing; resolved() checks no guard covers them.
    write_paths: tuple[Path, ...] = ()

    def resolved(self) -> SandboxParams:
        home = Path(checked_path(self.home))
        run = Path(checked_path(self.run_dir))
        cache = Path(checked_path(self.cache_dir))
        secrets = Path(checked_path(self.secrets_dir))
        deny_read = tuple(Path(checked_path(p)) for p in self.deny_read)
        raw_protect = default_protected() if self.protect is None else self.protect
        protect: dict[Path, None] = {}
        for p in raw_protect:
            protect.setdefault(Path(checked_path(p, follow=False)), None)
            protect.setdefault(Path(checked_path(p)), None)
        # runner.sb names $HOME-relative paths as strings, and SBPL matches real paths: a
        # symlinked ~/.resonant or ~/.zshrc (a dotfiles repo) would leave its target open.
        # Each one's real target is denied too, write-only or read and write like the rule.
        for g in persistence_paths(home)[1:]:
            if (real := Path(checked_path(g))) != g:
                protect.setdefault(real, None)
        denied: dict[Path, None] = dict.fromkeys(deny_read)
        for g in (home / "Library" / "Messages", home / "Library" / "Keychains"):
            if (real := Path(checked_path(g))) != g:
                denied.setdefault(real, None)
        deny_read = tuple(denied)
        guarded = [
            *(g for g in persistence_paths(home) if g != home),
            *protect,
            secrets,
            *deny_read,
        ]
        writes = tuple(Path(checked_path(p)) for p in self.write_paths)
        dirs = (("run dir", run), ("cache dir", cache), *(("work path", w) for w in writes))
        for what, d in dirs:
            for g in guarded:
                # a session must be able to write its own dirs
                if d.is_relative_to(g):
                    raise SandboxError(f"{what} {d} is inside {g}, which a session can't write")
            if secrets.is_relative_to(d):
                raise SandboxError(f"secrets_dir {secrets} is inside the {what} {d}")
        reads = tuple(Path(checked_path(p)) for p in self.read_paths if Path(p).exists())
        work = Path(checked_path(self.work_dir)) if self.work_dir is not None else None
        return SandboxParams(
            run_dir=run,
            cache_dir=cache,
            work_dir=work,
            home=home,
            read_paths=reads,
            deny_exec=tuple(Path(checked_path(p)) for p in self.deny_exec),
            deny_read=deny_read,
            secrets_dir=secrets,
            protect=tuple(protect),
            write_paths=writes,
        )


def persistence_paths(home: Path) -> tuple[Path, ...]:
    """Where a write would outlive the run: $HOME itself, ~/.resonant, login items and
    LaunchAgents (~/Library), shell startup files, Claude and ssh config, git config.
    runner.sb denies writes to each (plus the system LaunchAgents and the cron spools)."""
    names = (
        "Library",
        ".claude",
        ".ssh",
        ".config",
        ".resonant",
        ".zshrc",
        ".zshenv",
        ".zprofile",
        ".zlogin",
        ".zlogout",
        ".bashrc",
        ".bash_profile",
        ".bash_login",
        ".profile",
        ".gitconfig",
    )
    return (home, *(home / n for n in names))


def _package_checkout() -> Path | None:
    """The git checkout this package runs from (the daemon's code), if any."""
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").is_file():
            return parent
    return None


def _keg(real: Path) -> Path | None:
    """The Homebrew keg (``.../Cellar/<formula>/<version>``) a binary lives in, if any: the
    whole keg, since a tool loads its libs and helpers from there."""
    parts = real.parts
    if "Cellar" in parts and len(parts) > parts.index("Cellar") + 2:
        return Path(*parts[: parts.index("Cellar") + 3])
    return None


def host_tool_path() -> str:
    """Where Resonant looks for the tools it hands a session or runs itself, by name, as a
    PATH string: the usual install dirs, never this process's PATH (the daemon's is system
    dirs only, see launchd._path)."""
    dirs = (Path("~/.local/bin").expanduser(), *(Path(d) for d in HOST_TOOL_DIRS))
    return os.pathsep.join(str(d) for d in dirs)


def _configured_tools() -> list[Path]:
    """The tool paths the daemon's config pins (runner.git_path, runner.gh_path,
    ops.uv_path), which may sit outside the install dirs. Empty if the config can't load."""
    try:
        from resonant.config import load_settings

        settings = load_settings()
    except Exception:
        return []
    tools = [settings.runner.git_path, settings.runner.gh_path, settings.ops.uv_path]
    return [p for p in tools if p is not None]


def default_protected() -> tuple[Path, ...]:
    """The installed daemon, write-protected from sessions. Call it in the daemon: it reads
    the daemon's own interpreter and environment.

    - its venv, the interpreter install the venv runs on, and the checkout its code runs from;
    - ``$RESONANT_HOME`` when it is set (the default ``~/.resonant`` is in runner.sb);
    - the ``resonant`` entry point, every copy of ``DAEMON_TOOLS`` in the install dirs and
      the tool paths its config pins (the link, its target and its Homebrew keg), which the
      daemon runs outside the guard by absolute path.

    The dirs on PATH (``~/.local/bin``, ``/opt/homebrew/bin``) stay writable, so a worker can
    ``brew install`` and ``uv tool install``: the daemon runs with a system-only PATH and
    calls every tool by absolute path, so nothing put there can shadow one."""
    found: list[Path] = [Path(sys.prefix), Path(sys.base_prefix)]
    if (checkout := _package_checkout()) is not None:
        found.append(checkout)
    if state := os.environ.get("RESONANT_HOME"):
        found.append(Path(state).expanduser())
    dirs = [Path(sys.executable).parent, *map(Path, host_tool_path().split(os.pathsep))]
    candidates = [d / name for name in ("resonant", *DAEMON_TOOLS) for d in dirs]
    candidates += _configured_tools()
    for path in candidates:
        if not path.is_file():  # also false for a dangling link
            continue
        found.append(path)
        if (keg := _keg(Path(os.path.realpath(path)))) is not None:
            found.append(keg)
    return tuple(found)


def default_deny_exec() -> tuple[Path, ...]:
    """The daemon's venv and the `resonant` entry point: sessions must not start either."""
    found = [Path(sys.prefix)]
    entry = shutil.which("resonant", path=host_tool_path())
    if entry is not None:
        found.append(Path(entry))
    return tuple(found)


def default_read_paths() -> tuple[Path, ...]:
    return tuple(Path(p).expanduser() for p in DEFAULT_READ_PATHS)


def _ancestors(paths: Iterable[Path]) -> list[str]:
    seen: dict[str, None] = {}
    for p in paths:
        for parent in p.parents:
            seen.setdefault(str(parent), None)
    return list(seen)


# Fixed paths in runner.sb whose ancestors also need the rename guard.
_FIXED_GUARDED = (
    "/Library/LaunchAgents",
    "/Library/LaunchDaemons",
    "/Library/StartupItems",
    "/Library/Keychains",
    "/private/var/at",
    "/private/var/db/com.apple.backgroundtaskmanagement",
)


def guarded_paths(params: SandboxParams) -> tuple[Path, ...]:
    """Every path the profile protects by subpath or literal (``params`` resolved)."""
    home = params.home
    return (
        *persistence_paths(home),
        home / "Library" / "Messages",
        home / "Library" / "Keychains",
        *(params.protect or ()),
        params.secrets_dir,
        *params.deny_read,
        *(Path(p) for p in _FIXED_GUARDED),
    )


def template_text() -> str:
    return resources.files("resonant.sandbox").joinpath("runner.sb").read_text(encoding="utf-8")


def render(params: SandboxParams) -> tuple[str, dict[str, str]]:
    """The profile text and its ``-D`` definitions for ``params``."""
    p = params.resolved()
    defs: dict[str, str] = {"HOME": str(p.home), "SECRETS": str(p.secrets_dir)}
    lines: dict[str, list[str]] = {k: [] for k in _PLACEHOLDERS}
    for key, prefix, paths, rule in (
        ("protect", "PROTECT", p.protect or (), "subpath"),
        ("deny_read", "DENY_READ", p.deny_read, "subpath"),
        ("exec", "DENY_EXEC", p.deny_exec, "subpath"),
        ("ancestors", "ANCESTOR", [Path(a) for a in _ancestors(guarded_paths(p))], "literal"),
    ):
        for i, path in enumerate(paths):
            defs[f"{prefix}_{i}"] = str(path)
            lines[key].append(f'  ({rule} (param "{prefix}_{i}"))')
    text = template_text()
    for key, placeholder in _PLACEHOLDERS.items():
        if placeholder not in text:
            raise SandboxError(f"runner.sb lost its {placeholder} placeholder")
        text = text.replace(placeholder, "\n".join(lines[key]))
    return text, defs


def wrap(argv: Sequence[str], params: SandboxParams) -> list[str]:
    """``argv`` prefixed so that it, and every child it starts, runs inside the sandbox."""
    if not argv:
        raise SandboxError("nothing to run")
    text, defs = render(params)
    out = [SANDBOX_EXEC, "-p", text]
    for key, value in defs.items():
        out += ["-D", f"{key}={value}"]
    return [*out, *argv]


def profile_hash(params: SandboxParams) -> str:
    """Identifies the template and the parameter shape, for the self-probe cache key."""
    text, defs = render(params)
    blob = text + "\0" + "\0".join(f"{k}={defs[k]}" for k in sorted(defs))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def macos_build() -> str:
    try:
        proc = subprocess.run(
            ["/usr/bin/sw_vers", "-buildVersion"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return proc.stdout.strip() or "unknown"


def sandbox_exec_available() -> bool:
    return os.access(SANDBOX_EXEC, os.X_OK)


# --- run directory, run-local bin and environment --------------------------------------------


def prepare_dirs(params: SandboxParams) -> None:
    """Create ``repo/ home/ tmp/ bin/`` in the run dir and the cache subdirs. Daemon-side,
    before the session starts."""
    p = params.resolved()
    for sub in ("repo", "home", "tmp", "bin"):
        (p.run_dir / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    for sub in ("uv", "npm", "pip"):
        (p.cache_dir / sub).mkdir(mode=0o700, parents=True, exist_ok=True)


def resolve_tool(name: str) -> Path | None:
    """The real binary behind ``name`` in ``host_tool_path()``; for ``SYSTEM_ONLY_TOOLS``
    (``git``, which the daemon also runs itself) only the system copy. Apple's /usr/bin shims
    are resolved through ``xcrun`` (into the Command Line Tools or Xcode, both root-owned) so
    the session never runs the shim, which wants to write a cache under /var/folders."""
    search = "/usr/bin" if name in SYSTEM_ONLY_TOOLS else host_tool_path()
    found = shutil.which(name, path=search)
    if found is None:
        return None
    real = Path(os.path.realpath(found))
    if real.parent == Path("/usr/bin") and name in {"git"} and shutil.which("xcrun"):
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv
                ["/usr/bin/xcrun", "--find", name],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return real
        path = proc.stdout.strip()
        if proc.returncode == 0 and path:
            return Path(os.path.realpath(path))
    return real


def link_tools(params: SandboxParams, tools: Mapping[str, str | os.PathLike[str]]) -> None:
    """Symlink the few binaries a session needs into the run-local ``bin/``. ``resonant``
    is refused by name and by target."""
    p = params.resolved()
    bin_dir = p.run_dir / "bin"
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, target in tools.items():
        real = Path(checked_path(target))
        if name in NEVER_LINKED or real.name in NEVER_LINKED:
            raise SandboxError(f"{name} must not be reachable from a session")
        if any(real.is_relative_to(d) for d in p.deny_exec):
            raise SandboxError(f"{real} is in a denied exec path")
        link = bin_dir / name
        link.unlink(missing_ok=True)
        link.symlink_to(real)


def scratch_env(
    params: SandboxParams,
    *,
    path_dirs: Sequence[str | os.PathLike[str]] = (),
    claude_token: str | None = None,
    anthropic_key: str | None = None,
) -> dict[str, str]:
    """The session's whole environment, built from nothing: no ``os.environ``, no
    ``SSH_AUTH_SOCK``. ``HOME`` and every temp and cache dir point into the run."""
    p = params.resolved()
    run, cache = p.run_dir, p.cache_dir
    path = [str(run / "bin"), *SYSTEM_PATH, *(checked_path(d) for d in path_dirs)]
    env = {
        "PATH": ":".join(path),
        "HOME": str(run / "home"),
        "TMPDIR": str(run / "tmp"),
        "CLAUDE_CONFIG_DIR": str(run / "home" / ".claude"),
        # Claude Code hard-codes /tmp/claude-<uid> unless told otherwise; keep it in the run.
        "CLAUDE_CODE_TMPDIR": str(run / "tmp"),
        "LANG": "en_US.UTF-8",
        "UV_CACHE_DIR": str(cache / "uv"),
        "npm_config_cache": str(cache / "npm"),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "DISABLE_AUTOUPDATER": "1",
        "GIT_AUTHOR_NAME": GIT_NAME,
        "GIT_AUTHOR_EMAIL": GIT_EMAIL,
        "GIT_COMMITTER_NAME": GIT_NAME,
        "GIT_COMMITTER_EMAIL": GIT_EMAIL,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if claude_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = claude_token
    if anthropic_key:
        env["ANTHROPIC_API_KEY"] = anthropic_key
    return env

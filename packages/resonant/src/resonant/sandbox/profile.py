"""The Seatbelt sandbox for runner sessions: profile template, parameters and ``wrap``.

``sandbox-exec -p <profile> -D KEY=VALUE ... argv``. The profile is ``runner.sb`` with a few
placeholder lines filled in by name only: every path reaches SBPL as a ``-D`` parameter,
``os.path.realpath``-ed first (``/tmp``, ``/var`` and ``/etc`` are symlinks into ``/private``
and SBPL matches real paths) and refused if it is not absolute or holds a quote, a backslash,
a closing paren or a control character. See docs/phase5a-plan.md (D1).
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
OLLAMA_PORT = 11434
GIT_NAME = "Resonant runner"
GIT_EMAIL = "resonant-runner@users.noreply.github.com"

# Toolchain subpaths a session may read. Whole tool homes like ~/.cargo are not here: they
# hold credentials. Entries that don't exist are dropped when the profile is rendered.
DEFAULT_READ_PATHS = (
    "~/.local/share/claude",
    "~/.local/share/uv/python",
    "~/.cargo/bin",
    "~/.rustup/toolchains",
    "~/.nvm/versions",
    "~/.bun/bin",
)
# Under $HOME and never readable, even when a read path above sits inside one of them.
ALWAYS_DENIED = (
    ".resonant",
    ".ssh",
    ".aws",
    ".config/gh",
    ".claude",
    "Library",
    ".local/bin",
)
# Credential dirs and files under $HOME, denied for read and write. runner.sb gets one
# subpath line each (the @HOME_SECRETS@ placeholder), so this list is the whole rule.
HOME_SECRET_DIRS = (
    ".ssh",
    ".aws",
    ".config/gh",
    ".config/gcloud",
    ".azure",
    ".kube",
    ".gnupg",
)
HOME_SECRET_FILES = (
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".git-credentials",
    ".docker/config.json",
)
HOME_SECRETS = (*HOME_SECRET_DIRS, *HOME_SECRET_FILES)
# Binaries that may be linked into the run-local bin/.
NEVER_LINKED = frozenset({"resonant"})

_PLACEHOLDERS = {
    "read": ";; @READ_ALLOW@",
    "meta": ";; @META_ALLOW@",
    "exec": ";; @DENY_EXEC@",
    "exempt": ";; @SECRET_EXEMPT@",
    "deny_read": ";; @DENY_READ@",
    "home_secrets": ";; @HOME_SECRETS@",
    "work_exempt": ";; @WORK_EXEMPT@",
    "write": ";; @WRITE_ALLOW@",
}


class SandboxError(Exception):
    """A path or setting the sandbox refuses, or a sandbox that can't be built."""


def checked_path(path: str | os.PathLike[str]) -> str:
    """The real, absolute form of ``path``, or SandboxError if it can't go into a profile."""
    text = os.fspath(path)
    if not os.path.isabs(text):
        raise SandboxError(f"not an absolute path: {text!r}")
    real = os.path.realpath(text) if "\0" not in text else text
    for ch in real:
        if ch in '"\\)' or ord(ch) < 32 or ord(ch) == 127:
            raise SandboxError(f"path has a character the sandbox refuses: {real!r}")
    return real


@dataclass(frozen=True)
class SandboxParams:
    """What one session may touch. Paths are normalised by ``resolved()`` before use."""

    run_dir: Path
    cache_dir: Path
    secrets_dir: Path  # runner.secrets_dir: required, never readable or writable
    home: Path = field(default_factory=Path.home)
    read_paths: tuple[Path, ...] = ()
    deny_exec: tuple[Path, ...] = ()
    deny_read: tuple[Path, ...] = ()  # runner.deny_read: extra paths, read and write denied
    # runner.work_dir, the parent of every run dir: all of it is denied except this run's dir,
    # the cache and the read paths, wherever it sits (default: the run dir's parent)
    work_dir: Path | None = None
    # the run's worktree and its checkout, or its scratch dir (EM-2): read and written like the
    # run dir. The thin guard (EM-1) replaces this profile; until then this keeps runs working.
    write_paths: tuple[Path, ...] = ()

    def resolved(self) -> SandboxParams:
        home = Path(checked_path(self.home))
        run = Path(checked_path(self.run_dir))
        for name in (*ALWAYS_DENIED, *HOME_SECRETS):
            if run.is_relative_to(home / name):
                raise SandboxError(
                    f"run dir {run} is inside {home / name}, which is never readable"
                )
        cache = Path(checked_path(self.cache_dir))
        for what, d in (("run dir", run), ("cache dir", cache)):
            for guarded in persistence_paths(home):
                # the dir may not be, hold or sit inside a persistence path ($HOME itself
                # may only be held: runs live under it)
                inside = d.is_relative_to(guarded) and guarded != home
                if inside or guarded.is_relative_to(d):
                    raise SandboxError(f"{what} {d} is, holds or is inside {guarded}")
        reads = tuple(Path(checked_path(p)) for p in self.read_paths if Path(p).exists())
        writes = tuple(Path(checked_path(p)) for p in self.write_paths)
        secrets = Path(checked_path(self.secrets_dir))
        for what, path in (
            ("run dir", run),
            ("cache dir", cache),
            *(("read path", r) for r in reads),
            *(("work path", w) for w in writes),
        ):
            if path.is_relative_to(secrets) or secrets.is_relative_to(path):
                raise SandboxError(
                    f"secrets_dir {secrets} overlaps the sandbox-readable {what} {path}"
                )
        work = Path(checked_path(self.work_dir)) if self.work_dir is not None else run.parent
        if work == Path("/") or not run.is_relative_to(work) or run == work:
            raise SandboxError(f"run dir {run} must be inside the work dir {work}")
        return SandboxParams(
            run_dir=run,
            cache_dir=cache,
            work_dir=work,
            home=home,
            read_paths=reads,
            deny_exec=tuple(Path(checked_path(p)) for p in self.deny_exec),
            deny_read=tuple(Path(checked_path(p)) for p in self.deny_read),
            secrets_dir=secrets,
            write_paths=writes,
        )


def persistence_paths(home: Path) -> tuple[Path, ...]:
    """Where a write would outlive the run: $HOME itself, login items, shell startup files,
    Claude and ssh config, git config."""
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
        ".bashrc",
        ".bash_profile",
        ".profile",
        ".gitconfig",
    )
    return (home, *(home / n for n in names))


def default_deny_exec() -> tuple[Path, ...]:
    """The daemon's venv and the `resonant` entry point: sessions must not start either."""
    found = [Path(sys.prefix)]
    entry = shutil.which("resonant")
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


def template_text() -> str:
    return resources.files("resonant.sandbox").joinpath("runner.sb").read_text(encoding="utf-8")


def render(params: SandboxParams) -> tuple[str, dict[str, str]]:
    """The profile text and its ``-D`` definitions for ``params``."""
    p = params.resolved()
    defs: dict[str, str] = {"RUN": str(p.run_dir), "CACHE": str(p.cache_dir), "HOME": str(p.home)}
    reads = [p.run_dir, p.cache_dir, *p.read_paths, *p.write_paths]
    read_lines: list[str] = []
    for i, path in enumerate(p.read_paths):
        defs[f"READ_{i}"] = str(path)
        read_lines.append(f'  (subpath (param "READ_{i}"))')
    write_lines: list[str] = []
    for i, path in enumerate(p.write_paths):
        defs[f"WRITE_{i}"] = str(path)
        write_lines.append(f'  (subpath (param "WRITE_{i}"))')
    read_lines += write_lines
    meta_lines: list[str] = []
    for i, path in enumerate(_ancestors(reads)):
        defs[f"META_{i}"] = path
        meta_lines.append(f'  (literal (param "META_{i}"))')
    exec_lines: list[str] = []
    for i, path in enumerate(p.deny_exec):
        defs[f"DENY_EXEC_{i}"] = str(path)
        exec_lines.append(f'  (subpath (param "DENY_EXEC_{i}"))')
    # the secret-file name denies spare the run's own files and the toolchain paths (a
    # package's bundled cacert.pem); the system roots are spared in runner.sb itself
    exempt_lines = [f'  (subpath (param "{k}"))' for k in ("RUN", "CACHE")]
    exempt_lines += [f'  (subpath (param "READ_{i}"))' for i in range(len(p.read_paths))]
    exempt_lines += write_lines
    deny_read_lines: list[str] = []
    for i, path in enumerate(p.deny_read):
        defs[f"DENY_READ_{i}"] = str(path)
        deny_read_lines.append(f'  (subpath (param "DENY_READ_{i}"))')
    home_secret_lines = [
        f'  (subpath (string-append (param "HOME") "/{rel}"))' for rel in HOME_SECRETS
    ]
    defs["SECRETS"] = str(p.secrets_dir)
    work = p.work_dir or p.run_dir.parent
    defs["WORK"] = str(work)
    # other runs' dirs (and their credentials) stay unreadable even when the work dir sits
    # where the HOME denies don't reach; the allowed paths and their ancestors are spared
    work_lines = [f'  (subpath (param "{k}"))' for k in ("RUN", "CACHE")]
    work_lines += [f'  (subpath (param "READ_{i}"))' for i in range(len(p.read_paths))]
    work_lines += [f'  (literal (param "META_{i}"))' for i in range(len(meta_lines))]
    work_lines += write_lines
    text = template_text()
    for key, lines in (
        ("read", read_lines),
        ("meta", meta_lines),
        ("exec", exec_lines),
        ("exempt", exempt_lines),
        ("deny_read", deny_read_lines),
        ("home_secrets", home_secret_lines),
        ("work_exempt", work_lines),
        ("write", write_lines),
    ):
        if _PLACEHOLDERS[key] not in text:
            raise SandboxError(f"runner.sb lost its {_PLACEHOLDERS[key]} placeholder")
        text = text.replace(_PLACEHOLDERS[key], "\n".join(lines))
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
    """The real binary behind ``name`` on the daemon's PATH. Apple's /usr/bin shims
    (``git``) are resolved through ``xcrun`` so the session never runs the shim, which wants
    to write a cache under /var/folders."""
    found = shutil.which(name)
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
    is refused by name and by target; ``~/.local/bin`` itself is not readable."""
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
        # Claude Code hard-codes /tmp/claude-<uid> unless told otherwise; /tmp is not readable.
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

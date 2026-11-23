"""``resonant relay deploy`` (PER-316): deploy the Fly relay with a Keychain deploy token.

A long-lived Fly deploy token, scoped to the relay app, lives in personal Resonant's
Keychain as ``relay/fly_deploy_token``. The deploy reads it and runs the same ``fly deploy``
as go-live step 15, with ``FLY_API_TOKEN`` in the child's env only, so the owner (or Claude,
deploys are pre-authorized) can deploy without a live ``fly auth login``.

The token never goes in argv, a log line, stdout, an error or the audit. ``fly``'s own
output is streamed to the terminal line by line (with the token masked, should it ever
echo it), and only a short tail is kept, to spot an auth failure and count machines.

Every subprocess goes through an injectable :class:`DeployRunner`, so tests use a fake and
never run a real ``fly`` or ``git``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, cast

from resonant.ops import ops_env
from resonant.secrets import secret_ref

RELAY_EXT: Final = "relay"
FLY_DEPLOY_TOKEN_REF: Final = secret_ref(RELAY_EXT, "fly_deploy_token")
FLY_CONFIG: Final = "relay/fly.toml"
FLY_DOCKERFILE: Final = "relay/Dockerfile"
# The image copies the built dashboard (relay/Dockerfile), so the build context is the repo
# root and dashboard/dist must exist.
DASHBOARD_DIST: Final = "dashboard/dist"
HOMEBREW_FLY: Final = Path("/opt/homebrew/bin/fly")
GIT: Final = "/usr/bin/git"
DEPLOY_TIMEOUT_S: Final = 30 * 60.0
STATUS_TIMEOUT_S: Final = 60.0
GIT_TIMEOUT_S: Final = 10.0
TOKEN_ENV: Final = "FLY_API_TOKEN"  # noqa: S105 - the env var name, not a value
# fly reads either; an inherited one must never reach the child (ours is set explicitly).
INHERITED_TOKENS: Final = ("FLY_API_TOKEN", "FLY_ACCESS_TOKEN")
TAIL_LINES: Final = 200
EXIT_TIMEOUT: Final = 124
EXIT_NO_START: Final = 127
# Lower-cased phrases fly prints when it refuses a token; matched only on a non-zero exit.
AUTH_PHRASES: Final = (
    "unauthorized",
    "not authorized",
    "token has expired",
    "token validation",
    "token is expired",
    "authentication required",
    "no access token available",
)
MASK: Final = "[redacted]"


def setup_lines(app: str | None) -> list[str]:
    """How to create and store the token (the owner runs both; Claude never does)."""
    name = app or "<app>"
    return [
        f"  fly tokens create deploy -a {name} --expiry 8760h",
        f"  resonant secret set {FLY_DEPLOY_TOKEN_REF} --expires-in 365d",
    ]


def renew_line(app: str) -> str:
    return (
        f"Fly rejected the token: renew with fly tokens create deploy -a {app} --expiry 8760h, "
        f"then resonant secret set {FLY_DEPLOY_TOKEN_REF} --expires-in 365d"
    )


def deploy_argv(fly: Path, app: str) -> list[str]:
    """The go-live step 15 deploy. ``--ha=false``: two machines would alert twice."""
    return [
        str(fly),
        "deploy",
        "--app",
        app,
        "--config",
        FLY_CONFIG,
        "--dockerfile",
        FLY_DOCKERFILE,
        "--ha=false",
        ".",
    ]


def status_argv(fly: Path, app: str) -> list[str]:
    return [str(fly), "status", "--app", app, "--json"]


def head_argv() -> list[str]:
    return [GIT, "rev-parse", "HEAD"]


def find_fly(
    configured: Path | None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    homebrew: Path = HOMEBREW_FLY,
) -> Path | None:
    """``relay.fly_path`` if set, else ``fly`` or ``flyctl`` on the caller's PATH, else
    Homebrew's. An ops env's PATH is system dirs only, so the child gets an absolute path.
    None when none is an executable file."""
    candidates: list[Path] = []
    if configured is not None:
        candidates.append(configured)
    else:
        candidates += [Path(p) for name in ("fly", "flyctl") if (p := which(name))]
        candidates.append(homebrew)
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path.absolute()
    return None


def child_env(
    fly: Path, token: str | None, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """``ops_env`` plus ``fly``'s dir first on PATH and ``FLY_API_TOKEN`` (when given).
    Nothing else of the caller's env: an inherited Fly token is dropped."""
    base = os.environ if base is None else base
    clean = {k: v for k, v in base.items() if k not in INHERITED_TOKENS}
    env = ops_env(clean)
    env["PATH"] = f"{fly.parent}:{env['PATH']}"
    for key in INHERITED_TOKENS:
        env.pop(key, None)
    if token is not None:
        env[TOKEN_ENV] = token
    return env


@dataclass(frozen=True)
class RunResult:
    returncode: int
    # The last ``TAIL_LINES`` lines of stdout and stderr together, the token masked.
    tail: str
    timed_out: bool = False


class DeployRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        echo: bool,
        mask: Sequence[str] = (),
    ) -> RunResult:
        """Run argv to completion. ``echo`` streams each output line to the terminal as it
        comes. Every value in ``mask`` is replaced in what is echoed or kept. Past
        ``timeout_s`` the process group is killed (``timed_out``). Never raises for a
        command that can't start: that is exit 127."""
        ...


def masked(text: str, values: Sequence[str]) -> str:
    for value in values:
        if value:
            text = text.replace(value, MASK)
    return text


class SubprocessDeployRunner:
    """The real runner: ``subprocess.Popen``, stderr into stdout, read line by line."""

    def __init__(self, out: Callable[[str], None] | None = None) -> None:
        self.out = out or _write_stdout

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        echo: bool,
        mask: Sequence[str] = (),
    ) -> RunResult:
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                start_new_session=True,  # its own process group, so a kill reaches children
            )
        except OSError:
            return RunResult(EXIT_NO_START, "")
        timed_out = threading.Event()

        def kill() -> None:
            timed_out.set()
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

        timer = threading.Timer(timeout_s, kill)
        timer.daemon = True
        timer.start()
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = masked(line, mask)
                tail.append(line)
                if echo:
                    self.out(line)
            proc.wait()
        finally:
            timer.cancel()
        return RunResult(proc.returncode, "".join(tail), timed_out.is_set())


def _write_stdout(line: str) -> None:
    sys.stdout.write(line)
    sys.stdout.flush()


def auth_rejected(result: RunResult) -> bool:
    """A non-zero exit with a phrase fly prints when it refuses a token."""
    if result.returncode == 0 or result.timed_out:
        return False
    low = result.tail.lower()
    return any(p in low for p in AUTH_PHRASES)


def machine_count(status_json: str) -> int | None:
    """The number of machines in ``fly status --json``, or None if it doesn't parse."""
    start = status_json.find("{")
    if start < 0:
        return None
    try:
        data: Any = json.loads(status_json[start:])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    machines = cast(dict[str, Any], data).get("Machines")
    if not isinstance(machines, list):
        return None
    return len(cast(list[Any], machines))


def repo_problem(repo: Path | None) -> str | None:
    """Why ``repo`` can't be deployed from, or None."""
    if repo is None:
        return "no repo: set ops.repo_path or pass --repo"
    if not repo.is_dir():
        return f"repo {repo} is not a directory"
    for rel in (FLY_CONFIG, FLY_DOCKERFILE):
        if not (repo / rel).is_file():
            return f"{repo / rel} is missing: is {repo} the resonant checkout?"
    if not (repo / DASHBOARD_DIST).is_dir():
        return (
            f"{repo / DASHBOARD_DIST} is missing: build the dashboard first "
            "(cd dashboard && npm ci && npm run build)"
        )
    return None

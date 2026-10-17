"""How a Claude worker authenticates (docs/employee-plan.md, GD-1).

Three sources, in this order:

1. ``runner/claude_token`` (``CLAUDE_CODE_OAUTH_TOKEN``, a ``claude setup-token`` value: the
   subscription);
2. ``runner/anthropic_key`` (``ANTHROPIC_API_KEY``: API billing, with ``--max-budget-usd``);
3. with ``runner.guard: off`` only, the owner's own ``claude`` login on this Mac: the session
   has the real ``HOME`` and no sandbox, so ``claude`` finds its stored login as it does in
   the owner's terminal. Neither variable is set then.

``ANTHROPIC_API_KEY`` is put in a session's environment only when the API key is the chosen
source: set beside a subscription login it would win and bill the API.

Whether the login works is asked of ``claude auth status --json`` (exit 0 and, when the
output is JSON with ``loggedIn``, that being true), with a minimal environment: the real
``HOME``, a system-and-install-dirs ``PATH``, no token or key. Its output is never logged or
returned (it can name the account). The answer is cached (:class:`LoginCheck`), so
``setup_gaps``, the dashboard and ``/runs`` don't start a process each time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from resonant.secrets import RunnerSecrets

log = logging.getLogger(__name__)

AuthSource = Literal["token", "api_key", "login"]

STATUS_TIMEOUT_S = 20.0
OK_TTL_S = 600.0  # a working login is asked again after this
BAD_TTL_S = 60.0  # ... a missing one sooner, so a fresh `claude` login shows up quickly
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

LOGIN_HOW = "log in with `claude` in the Mini's terminal"


def auth_source(secrets: RunnerSecrets, *, login_ok: bool) -> AuthSource | None:
    """The credential a session uses, or None when there is none."""
    if secrets.claude_token:
        return "token"
    if secrets.anthropic_key:
        return "api_key"
    return "login" if login_ok else None


def status_env(home: Path) -> dict[str, str]:
    """The environment of the login check: built from nothing, never a token or key."""
    from resonant.sandbox.profile import host_tool_path

    env = {
        "HOME": str(home),
        "PATH": f"{host_tool_path()}:{SYSTEM_PATH}",
        "LANG": "en_US.UTF-8",
        "DISABLE_AUTOUPDATER": "1",
    }
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = None
    if user:
        env["USER"] = env["LOGNAME"] = user
    return env


def parse_status(returncode: int, stdout: str) -> bool:
    """Logged in: exit 0, and ``loggedIn`` true when the output is JSON that has it."""
    if returncode != 0:
        return False
    try:
        data: Any = json.loads(stdout)
    except ValueError:
        return True
    if isinstance(data, dict) and "loggedIn" in data:
        return data["loggedIn"] is True
    return True


def login_status(
    claude: Sequence[str],
    home: Path,
    *,
    timeout_s: float = STATUS_TIMEOUT_S,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Whether ``claude`` (an argv head) is logged in for ``home``. Never raises; prints and
    logs nothing of the output."""
    try:
        proc = run(
            [*claude, "auth", "status", "--json"],
            env=status_env(home),
            cwd=str(home) if home.is_dir() else "/",
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return parse_status(proc.returncode, proc.stdout or "")


class LoginCheck:
    """``await check()``: whether the owner's ``claude`` login works, cached (``ok_ttl_s`` for
    yes, ``bad_ttl_s`` for no). ``claude`` gives the argv head each time (None: not
    installed, so no). ``probe`` is the seam for tests (by default :func:`login_status` in a
    thread)."""

    def __init__(
        self,
        claude: Callable[[], Sequence[str] | None],
        home: Path,
        *,
        probe: Callable[[Sequence[str], Path], bool] | None = None,
        ok_ttl_s: float = OK_TTL_S,
        bad_ttl_s: float = BAD_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._claude = claude
        self.home = home
        self._probe = probe
        self.ok_ttl_s = ok_ttl_s
        self.bad_ttl_s = bad_ttl_s
        self._clock = clock
        self._cached: tuple[float, bool] | None = None
        self._lock = asyncio.Lock()

    def forget(self) -> None:
        """Ask again next time (a run failed to authenticate)."""
        self._cached = None

    async def __call__(self) -> bool:
        async with self._lock:
            now = self._clock()
            if self._cached is not None:
                at, ok = self._cached
                if now - at < (self.ok_ttl_s if ok else self.bad_ttl_s):
                    return ok
            argv = self._claude()
            ok = False
            if argv:
                ok = await asyncio.to_thread(self._probe or login_status, argv, self.home)
            log.info("claude login check", extra={"logged_in": ok})
            self._cached = (self._clock(), ok)
            return ok

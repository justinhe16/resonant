"""LaunchAgent generation and install.

Resonant runs as a per-user LaunchAgent (gui/<uid>), not a system LaunchDaemon. It needs
the logged-in GUI session for Messages.app, computer use, and the login Keychain. Plists
are built with plistlib, so no templating or escaping is involved.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from resonant.config import Settings

DAEMON_LABEL = "com.resonant.daemon"
OLLAMA_LABEL = "com.resonant.ollama"
_SYSTEM_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _path() -> str:
    # ~/.local/bin is where uv tools and the `claude` CLI usually live.
    return f"{Path('~/.local/bin').expanduser()}:{_SYSTEM_PATH}"


def agents_dir() -> Path:
    return Path("~/Library/LaunchAgents").expanduser()


def plist_path(label: str) -> Path:
    return agents_dir() / f"{label}.plist"


def daemon_plist(settings: Settings, python: str | None = None) -> dict[str, Any]:
    logs = settings.logs_dir
    return {
        "Label": DAEMON_LABEL,
        "ProgramArguments": [python or sys.executable, "-m", "resonant", "daemon", "run"],
        "EnvironmentVariables": {"RESONANT_HOME": str(settings.home), "PATH": _path()},
        "WorkingDirectory": str(settings.home),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "daemon.out.log"),
        "StandardErrorPath": str(logs / "daemon.err.log"),
    }


def ollama_plist(settings: Settings, ollama: str) -> dict[str, Any]:
    url = urlparse(settings.model.base_url)
    logs = settings.logs_dir
    return {
        "Label": OLLAMA_LABEL,
        "ProgramArguments": [ollama, "serve"],
        "EnvironmentVariables": {
            "OLLAMA_HOST": f"{url.hostname or '127.0.0.1'}:{url.port or 11434}",
            "OLLAMA_KEEP_ALIVE": "-1",
            "OLLAMA_CONTEXT_LENGTH": str(settings.model.context_tokens),
            "PATH": _path(),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "ollama.out.log"),
        "StandardErrorPath": str(logs / "ollama.err.log"),
    }


def find_ollama() -> str | None:
    return shutil.which("ollama")


class LaunchctlError(RuntimeError):
    pass


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(  # noqa: S603 - fixed binary, no shell
        ["/bin/launchctl", *args], check=False, capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise LaunchctlError(
            f"launchctl {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


def install(plist: dict[str, Any]) -> Path:
    """Write the plist and (re)load it into the user's GUI domain."""
    label: str = plist["Label"]
    path = plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    for key in ("StandardOutPath", "StandardErrorPath"):
        if key in plist:
            Path(plist[key]).parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{label}", check=False)
    # bootout is asynchronous. bootstrap fails with EIO (5) until the old instance is gone.
    for attempt in range(10):
        try:
            _launchctl("bootstrap", domain, str(path))
            break
        except LaunchctlError:
            if attempt == 9:
                raise
            time.sleep(0.5)
    _launchctl("enable", f"{domain}/{label}")
    return path


def uninstall(label: str) -> bool:
    path = plist_path(label)
    _launchctl("bootout", f"gui/{os.getuid()}/{label}", check=False)
    if path.exists():
        path.unlink()
        return True
    return False


def is_loaded(label: str) -> bool:
    return _launchctl("print", f"gui/{os.getuid()}/{label}", check=False).returncode == 0

"""Instances: more than one Resonant daemon on one macOS user (PER-297, Phase 8).

One checkout can run several daemons as the same user, each an *instance* with its own name.
Everything that would collide between two of them on one user is derived here from the name,
in one place, so every caller agrees:

==================  ===============================  ======================================
what                ``resonant`` (the default)        any other name, e.g. ``cori``
==================  ===============================  ======================================
home                ``~/.resonant``                   ``~/.resonant-cori``
launchd label       ``com.resonant.daemon``           ``com.resonant.cori.daemon``
Keychain service    ``resonant``                      ``resonant-cori``
runner.work_dir     ``~/resonant-runs``               ``~/cori/runs``
runner.code_dir     ``~/code``                        ``~/cori/code``
runner.cache_dir    ``~/resonant-runs/.cache``        ``~/cori/runs/.cache``
==================  ===============================  ======================================

The default instance's names are exactly what Resonant used before instances existed, so a
personal install with no ``instance:`` block and no ``RESONANT_INSTANCE`` doesn't move.

Picking an instance (the CLI and the daemon both): ``RESONANT_HOME`` (or ``home=`` in tests)
names the home outright; otherwise ``RESONANT_INSTANCE`` (or the CLI's ``--instance``) maps a
name to its home; otherwise it is ``~/.resonant``. A home's ``config.yaml`` may state
``instance.name``; when it disagrees with the name asked for, loading refuses
(``InstanceMismatch``) instead of running one instance's config under another's name.

The ``api.port`` is not derived: each instance's config sets it (``config/cori.example.yaml``
uses 7778), and ``resonant daemon install`` refuses a port that is already bound
(``port_in_use``).

**Siblings** (PER-290): the other instances installed on this user, found from their
LaunchAgent plists (``com.resonant[.<name>].daemon.plist`` in ``~/Library/LaunchAgents``) and
the ``RESONANT_HOME`` each one runs with (``siblings``). One checkout runs them all, so a code
deploy restarts every sibling, and personal Resonant's kill switch pauses them too.

This module imports nothing from the rest of Resonant, so ``config``, ``launchd``,
``secrets`` and ``ops`` can all use it.
"""

from __future__ import annotations

import errno
import logging
import os
import plistlib
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

log = logging.getLogger(__name__)

DEFAULT_INSTANCE = "resonant"
DEFAULT_DISPLAY_NAME = "Resonant"
# The env var that picks an instance (the CLI's --instance sets it for itself and its
# children). Not a settings field: config.Settings drops it from its env source.
INSTANCE_ENV = "RESONANT_INSTANCE"
HOME_ENV = "RESONANT_HOME"

# A name lands in a launchd label, a Keychain service, a directory name and a dashboard
# field, so it is a plain lowercase slug: no dots (labels are dot-separated), no slashes.
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
# A Keychain service goes into ``security`` argv and onto a ``security -i`` command line that
# is split on spaces: printable, no spaces or quotes.
SERVICE_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")

_DAEMON_LABEL = "com.resonant.daemon"
_LABEL_RE = re.compile(rf"com\.resonant\.(?P<name>{NAME_RE.pattern})\.daemon")


class InstanceError(ValueError):
    """A bad instance name, or a request that names two different instances."""


class InstanceMismatch(InstanceError):
    """The home asked for says it belongs to another instance."""


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise InstanceError(
            f"instance name {name!r} must be a slug: [a-z][a-z0-9-]{{0,31}} (like cori)"
        )
    return name


def is_default(name: str) -> bool:
    return name == DEFAULT_INSTANCE


def home_for(name: str) -> Path:
    """The instance's home, expanded: ``~/.resonant`` or ``~/.resonant-<name>``."""
    validate_name(name)
    raw = "~/.resonant" if is_default(name) else f"~/.resonant-{name}"
    return Path(raw).expanduser()


def home_owner(home: Path) -> str | None:
    """The instance whose default home ``home`` is (``~/.resonant`` is ``resonant``'s,
    ``~/.resonant-cori`` is ``cori``'s), or None for any other path."""
    user = Path("~").expanduser()
    if home.parent != user:
        return None
    if home.name == ".resonant":
        return DEFAULT_INSTANCE
    rest = home.name.removeprefix(".resonant-")
    if rest != home.name and NAME_RE.fullmatch(rest) and not is_default(rest):
        return rest
    return None


def daemon_label(name: str) -> str:
    """``com.resonant.daemon`` or ``com.resonant.<name>.daemon``."""
    validate_name(name)
    return _DAEMON_LABEL if is_default(name) else f"com.resonant.{name}.daemon"


def is_daemon_label(label: str) -> bool:
    """Whether ``label`` is some instance's daemon label (the ops allow-list uses it)."""
    m = _LABEL_RE.fullmatch(label)
    return label == _DAEMON_LABEL or (m is not None and not is_default(m["name"]))


def keychain_service(name: str) -> str:
    """The derived Keychain service: ``resonant`` or ``resonant-<name>``."""
    validate_name(name)
    return DEFAULT_INSTANCE if is_default(name) else f"{DEFAULT_INSTANCE}-{name}"


def service_owner(service: str) -> str | None:
    """The instance whose *derived* service ``service`` is, or None when it is nobody's."""
    if service == DEFAULT_INSTANCE:
        return DEFAULT_INSTANCE
    prefix = f"{DEFAULT_INSTANCE}-"
    if service.startswith(prefix) and NAME_RE.fullmatch(rest := service.removeprefix(prefix)):
        return rest
    return None


@dataclass(frozen=True)
class RunDirs:
    """The runner's worker-visible dirs (unexpanded, like the config defaults)."""

    work_dir: Path
    code_dir: Path
    cache_dir: Path

    def items(self) -> tuple[tuple[str, Path], ...]:
        return (("work_dir", self.work_dir), ("code_dir", self.code_dir),
                ("cache_dir", self.cache_dir))  # fmt: skip


def run_dirs(name: str) -> RunDirs:
    """The default ``runner.work_dir``, ``code_dir`` and ``cache_dir`` for ``name``. Two
    different names' dirs never overlap, except for names whose ``~/<name>`` is inside the
    default instance's dirs (``code``, ``resonant-runs``), which config validation refuses."""
    validate_name(name)
    if is_default(name):
        return RunDirs(Path("~/resonant-runs"), Path("~/code"), Path("~/resonant-runs/.cache"))
    root = Path("~") / name
    return RunDirs(root / "runs", root / "code", root / "runs" / ".cache")


def overlaps(a: Path, b: Path) -> bool:
    return a.is_relative_to(b) or b.is_relative_to(a)


# --- picking the instance -----------------------------------------------------------------


def requested(env: Mapping[str, str] | None = None) -> str | None:
    """The instance ``RESONANT_INSTANCE`` asks for (validated), or None when unset."""
    raw = (os.environ if env is None else env).get(INSTANCE_ENV)
    if raw is None or raw == "":
        return None
    return validate_name(raw)


def resolve_home(env: Mapping[str, str] | None = None) -> Path:
    """``RESONANT_HOME`` when set, else the home of ``RESONANT_INSTANCE``, else
    ``~/.resonant``."""
    e = os.environ if env is None else env
    if explicit := e.get(HOME_ENV):
        return Path(explicit).expanduser()
    return home_for(requested(e) or DEFAULT_INSTANCE)


def stated_name(config_yaml: Path) -> str | None:
    """``instance.name`` as ``config_yaml`` states it, or None (no file, no block, no name, or
    a file that doesn't parse: full validation reports those)."""
    try:
        data: object = yaml.safe_load(config_yaml.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    block = cast(dict[str, object], data).get("instance")
    if not isinstance(block, dict):
        return None
    name = cast(dict[str, object], block).get("name")
    return name if isinstance(name, str) else None


def check_home(home: Path, want: str | None) -> None:
    """Refuse a home whose ``config.yaml`` states another instance than ``want``."""
    if want is None:
        return
    stated = stated_name(home / "config.yaml")
    if stated is not None and stated != want:
        raise InstanceMismatch(f"this home ({home}) is instance {stated}, not {want}")


def select(flag: str | None, env: Mapping[str, str] | None = None) -> str | None:
    """The instance the CLI's ``--instance`` and ``RESONANT_INSTANCE`` ask for together:
    either alone, or both when they agree. None when neither is given."""
    from_env = requested(env)
    if flag is None:
        return from_env
    validate_name(flag)
    if from_env is not None and from_env != flag:
        raise InstanceError(f"--instance {flag} disagrees with {INSTANCE_ENV}={from_env}")
    return flag


# --- siblings (PER-290) ------------------------------------------------------------------


@dataclass(frozen=True)
class Sibling:
    """Another instance installed on this user: its name, daemon label, home (from its
    plist's ``RESONANT_HOME``) and plist."""

    name: str
    label: str
    home: Path
    plist: Path

    @property
    def display_name(self) -> str:
        """How replies name it: ``Resonant`` for the default instance, else ``Cori``."""
        return DEFAULT_DISPLAY_NAME if is_default(self.name) else self.name.capitalize()


def label_owner(label: str) -> str | None:
    """The instance whose daemon label ``label`` is, or None."""
    if label == _DAEMON_LABEL:
        return DEFAULT_INSTANCE
    m = _LABEL_RE.fullmatch(label)
    return m["name"] if m is not None and not is_default(m["name"]) else None


def agents_dir() -> Path:
    return Path("~/Library/LaunchAgents").expanduser()


def _plist_home(path: Path, name: str) -> Path | None:
    """The ``RESONANT_HOME`` a daemon plist runs with (else its instance's default home), or
    None when the plist can't be read."""
    try:
        with path.open("rb") as f:
            data: object = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    env = cast(dict[str, object], data).get("EnvironmentVariables")
    raw = cast(dict[str, object], env).get(HOME_ENV) if isinstance(env, dict) else None
    return Path(raw).expanduser() if isinstance(raw, str) and raw else home_for(name)


def siblings(me: str, directory: Path | None = None) -> list[Sibling]:
    """Every *other* instance with an installed daemon plist, by name. A plist that can't be
    read, or whose home is missing, is skipped with a warning (a deploy goes on without it)."""
    found: list[Sibling] = []
    where = agents_dir() if directory is None else directory
    try:
        entries = sorted(where.glob("com.resonant*.daemon.plist"))
    except OSError:
        return []
    for path in entries:
        name = label_owner(path.name.removesuffix(".plist"))
        if name is None or name == me:
            continue
        home = _plist_home(path, name)
        if home is None:
            log.warning("sibling %s: its plist can't be read; skipped", name)
            continue
        if not home.is_dir():
            log.warning("sibling %s: its home %s is missing; skipped", name, home)
            continue
        found.append(Sibling(name, daemon_label(name), home, path))
    return found


# --- the API port -------------------------------------------------------------------------


def port_in_use(host: str, port: int) -> bool:
    """Whether something already listens on ``host:port`` (bind test, nothing is sent)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        # uvicorn sets SO_REUSEADDR too, so a port in TIME_WAIT counts as free; a listener on
        # the same address still makes bind fail.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            if e.errno in (errno.EADDRINUSE, errno.EACCES):
                return True
            raise
    return False

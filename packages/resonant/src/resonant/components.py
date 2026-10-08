"""Daemon components: the tool registry, principals, gate and executor, built once at start.

``build_components`` is the single place the daemon wires these together. Later phases add
fields here (runners, channels, ...); give new fields defaults so existing callers and tests
keep working.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from resonant.config import Settings
from resonant.executor import Executor, HandlerKey, ToolHandler
from resonant.gate import DryRunGate
from resonant.principals import Principal, Principals, load_principals
from resonant.tools import ToolRegistry
from resonant.tools.builtin import register_builtins

if TYPE_CHECKING:
    from resonant.api import DaemonState

log = logging.getLogger(__name__)

# Stand-in owner when principals.yaml is missing. It has no channel identities, so no
# channel sender resolves to any principal and every channel request is denied.
FALLBACK_OWNER = "owner"


@dataclass
class Components:
    registry: ToolRegistry
    principals: Principals
    gate: DryRunGate
    executor: Executor
    handlers: dict[HandlerKey, ToolHandler]
    principals_loaded: bool = True  # False: principals.yaml missing, channels deny all


def deny_all_principals() -> Principals:
    """Principals with a lone identity-less owner: no channel identity resolves."""
    return Principals(principals={FALLBACK_OWNER: Principal(role="owner")})


def build_components(
    settings: Settings, conn: sqlite3.Connection, state: DaemonState
) -> Components:
    """Build the registry (with builtins), principals, ``DryRunGate`` and ``Executor``.

    ``state`` is the daemon's live state; ``daemon_status`` reads uptime and loop progress
    from it. A missing principals.yaml is a warning (channels deny everyone); an invalid
    one is an error.
    """
    registry = ToolRegistry()
    handlers = register_builtins(registry, conn, state)
    path = settings.principals_path
    if path.exists():
        principals, loaded = load_principals(path), True
    else:
        log.warning("%s missing: denying all channel principals", path)
        principals, loaded = deny_all_principals(), False
    gate = DryRunGate(conn, principals, registry)
    executor = Executor(conn, gate, handlers)
    return Components(
        registry=registry,
        principals=principals,
        gate=gate,
        executor=executor,
        handlers=handlers,
        principals_loaded=loaded,
    )

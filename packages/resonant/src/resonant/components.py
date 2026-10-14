"""Daemon components: the tool registry, principals, gate and executor, built once at start.

The gate is the Phase 2 ``PolicyGate`` (risk x origin, approvals, kill switch), built with
``settings.dry_run`` and ``settings.gate.approval_ttl_s``. ``DryRunGate`` stays as a test
double.

``build_components`` is the single place the daemon wires these together. Later phases add
fields here (runners, channels, ...); give new fields defaults so existing callers and tests
keep working.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from resonant.config import Settings
from resonant.executor import Executor, HandlerKey, ToolHandler
from resonant.gate import Gate, Intent, PolicyGate
from resonant.gate.policy import SummaryAction
from resonant.gateway.channel import Channel
from resonant.loop.types import Runner
from resonant.models import ModelClient
from resonant.principals import Principal, Principals, load_principals
from resonant.runners.local import LocalRunner
from resonant.tools import ToolRegistry
from resonant.tools import notes as notes_tools
from resonant.tools.builtin import register_builtins
from resonant.tools.registry import ToolKey

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
    gate: Gate
    executor: Executor
    handlers: dict[HandlerKey, ToolHandler]
    principals_loaded: bool = True  # False: principals.yaml missing, channels deny all
    runners: dict[str, Runner] = field(default_factory=dict[str, Runner])  # name -> runner

    def register_local_runner(
        self, conn: sqlite3.Connection, *, model: ModelClient, channel: Channel
    ) -> LocalRunner:
        """Build ``runner="local"`` from these components and add it to ``runners``.

        The model client and channel are built by the daemon (the channel needs
        ``principals`` from here), so the runner is registered after ``build_components``.
        """
        runner = LocalRunner(
            conn,
            model=model,
            channel=channel,
            gate=self.gate,
            executor=self.executor,
            registry=self.registry,
        )
        self.runners[runner.name] = runner
        return runner


def deny_all_principals() -> Principals:
    """Principals with a lone identity-less owner: no channel identity resolves."""
    return Principals(principals={FALLBACK_OWNER: Principal(role="owner")})


def summary_actions(conn: sqlite3.Connection) -> dict[ToolKey, SummaryAction]:
    """Code-built approval headings per builtin tool (``delete all 4 notes``)."""

    def clear_notes(intent: Intent) -> str | None:
        return notes_tools.summary_action(conn, intent)

    return {(None, notes_tools.CLEAR_NOTES.name): clear_notes}


def build_components(
    settings: Settings, conn: sqlite3.Connection, state: DaemonState
) -> Components:
    """Build the registry (with builtins), principals, ``PolicyGate`` and ``Executor``.

    ``state`` is the daemon's live state; ``daemon_status`` reads uptime and loop progress
    from it. A missing principals.yaml is a warning (channels deny everyone); an invalid
    one is an error.
    """
    path = settings.principals_path
    if path.exists():
        principals, loaded = load_principals(path), True
    else:
        log.warning("%s missing: denying all channel principals", path)
        principals, loaded = deny_all_principals(), False
    registry = ToolRegistry()
    handlers = register_builtins(registry, conn, state, principals=principals)
    gate = PolicyGate(
        conn,
        principals,
        registry,
        dry_run=settings.dry_run,
        approval_ttl_s=settings.gate.approval_ttl_s,
        actions=summary_actions(conn),
    )
    executor = Executor(conn, gate, handlers)
    return Components(
        registry=registry,
        principals=principals,
        gate=gate,
        executor=executor,
        handlers=handlers,
        principals_loaded=loaded,
    )

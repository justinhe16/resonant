"""Daemon components: the tool registry, principals, gate and executor, built once at start.

The gate is the Phase 2 ``PolicyGate`` (risk x origin, approvals, kill switch), built with
``settings.dry_run`` and ``settings.gate.approval_ttl_s``. ``DryRunGate`` stays as a test
double.

Extensions (Phase 3). The ``ExtensionHost`` loads every enabled extension once, before the
gate and executor exist: it registers their tool and job specs, and its handlers are merged
into the executor's map here (the executor copies its map at build, so nothing is added
later). The gate gets the host's ``approvers_for``; the executor gets the ``SecretStore``
and ``extensions.result_max_bytes``.

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
from resonant.executor import Executor, Handler, HandlerKey, ToolHandler
from resonant.extensions.host import ExtensionHost
from resonant.extensions.process import ExtRunner
from resonant.gate import Gate, Intent, PolicyGate
from resonant.gate.policy import LevelFor, SummaryAction
from resonant.gateway.channel import Channel
from resonant.loop.types import Runner
from resonant.models import ModelClient
from resonant.principals import Principal, Principals, load_principals
from resonant.runners.local import MODEL_RESULT_MAX_CHARS, LocalRunner
from resonant.secrets import SecretStore
from resonant.tools import ToolRegistry
from resonant.tools import notes as notes_tools
from resonant.tools import ops as ops_tools
from resonant.tools import repos as repos_tools
from resonant.tools import runs as runs_tools
from resonant.tools import trust as trust_tools
from resonant.tools.builtin import register_builtins
from resonant.tools.registry import ToolKey
from resonant.trust.tools import StandingAllows
from resonant_sdk import Level, ToolSpec

if TYPE_CHECKING:
    from resonant.api import DaemonState
    from resonant.ops import OpsRunner

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
    handlers: dict[HandlerKey, ToolHandler]  # builtins (the fast path's reads)
    principals_loaded: bool = True  # False: principals.yaml missing, channels deny all
    runners: dict[str, Runner] = field(default_factory=dict[str, Runner])  # name -> runner
    extensions: ExtensionHost | None = None  # None only in tests that build by hand
    ops_runner: OpsRunner | None = None  # the ops tools' subprocesses (None: the real one)

    def chat_specs(self) -> list[ToolSpec]:
        """Every registered spec a chat step can call: builtins and extension tools, never
        extension job specs."""
        host = self.extensions
        return [s for s in self.registry.specs() if host is None or not host.hidden(s)]

    def register_local_runner(
        self,
        conn: sqlite3.Connection,
        *,
        model: ModelClient,
        channel: Channel,
        timezone: str = "UTC",
        model_result_max_chars: int = MODEL_RESULT_MAX_CHARS,
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
            timezone=timezone,
            extensions=self.extensions,
            model_result_max_chars=model_result_max_chars,
        )
        self.runners[runner.name] = runner
        return runner


def deny_all_principals() -> Principals:
    """Principals with a lone identity-less owner: no channel identity resolves."""
    return Principals(principals={FALLBACK_OWNER: Principal(role="owner")})


def summary_actions(conn: sqlite3.Connection) -> dict[ToolKey, SummaryAction]:
    """Code-built approval and dry-run headings per builtin tool (``delete all 4 notes``,
    ``restart Ollama``)."""

    def clear_notes(intent: Intent) -> str | None:
        return notes_tools.summary_action(conn, intent)

    actions: dict[ToolKey, SummaryAction] = {(None, notes_tools.CLEAR_NOTES.name): clear_notes}
    for name in ops_tools.SUMMARY_ACTIONS:
        actions[(None, name)] = ops_tools.summary_action

    def merge_run(intent: Intent) -> str | None:
        return runs_tools.summary_action(conn, intent)

    actions[(None, runs_tools.MERGE_RUN.name)] = merge_run

    def trust_rule(intent: Intent) -> str | None:
        return trust_tools.summary_action(conn, intent)

    for spec in (trust_tools.ALLOW_TRUST_RULE, trust_tools.REMOVE_TRUST_RULE):
        actions[(None, spec.name)] = trust_rule
    actions[(None, repos_tools.MERGE_CI_PR.name)] = repos_tools.summary_action
    return actions


def level_overrides(conn: sqlite3.Connection) -> dict[ToolKey, LevelFor]:
    """Builtin tools whose level depends on the call: ``merge_run`` follows the trust rules
    (docs/employee-plan.md D4)."""

    def merge_run(intent: Intent) -> Level | None:
        return runs_tools.merge_level(conn, intent)

    return {(None, runs_tools.MERGE_RUN.name): merge_run}


def build_components(
    settings: Settings,
    conn: sqlite3.Connection,
    state: DaemonState,
    *,
    ops_runner: OpsRunner | None = None,
    secrets: SecretStore | None = None,
    ext_runner: ExtRunner | None = None,
) -> Components:
    """Build the registry (with builtins and loaded extensions), principals,
    ``PolicyGate`` and ``Executor``.

    ``state`` is the daemon's live state; ``daemon_status`` reads uptime and loop progress
    from it. ``ops_runner`` runs the ops tools' subprocesses (default: the real one; tests
    pass a fake). ``secrets`` defaults to the real Keychain store and ``ext_runner`` to the
    real process runner; tests pass ``SecretStore(FakeKeychain())`` and fakes. A missing
    principals.yaml is a warning (channels deny everyone); an invalid one is an error.
    """
    path = settings.principals_path
    if path.exists():
        principals, loaded = load_principals(path), True
    else:
        log.warning("%s missing: denying all channel principals", path)
        principals, loaded = deny_all_principals(), False
    registry = ToolRegistry()
    handlers = register_builtins(
        registry, conn, state, principals=principals, ops_runner=ops_runner
    )
    secret_store = secrets if secrets is not None else SecretStore()
    host = ExtensionHost.from_settings(settings, conn, registry, runner=ext_runner)
    host.secrets = secret_store
    host.load()
    gate = PolicyGate(
        conn,
        principals,
        registry,
        dry_run=settings.dry_run,
        approval_ttl_s=settings.gate.approval_ttl_s,
        actions=summary_actions(conn),
        approvers_for=host.approvers_for,
        levels=level_overrides(conn),
        standing=StandingAllows(conn),
    )
    merged: dict[HandlerKey, Handler] = {**handlers, **host.handlers, **host.mcp_handlers}
    executor = Executor(
        conn,
        gate,
        merged,
        secrets=secret_store,
        result_max_bytes=settings.extensions.result_max_bytes,
    )
    return Components(
        registry=registry,
        principals=principals,
        gate=gate,
        executor=executor,
        handlers=handlers,
        principals_loaded=loaded,
        ops_runner=ops_runner,
        extensions=host,
    )

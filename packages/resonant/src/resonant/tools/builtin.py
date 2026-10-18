"""Built-in tools: questions about Resonant and its machine, plus the owner's notes.

Each tool area is a module (see ``resonant.tools.base``); ``BUILTIN_MODULES`` lists them and
``register_builtins`` iterates it. A new area is one module plus one line here.

Every builtin has ``extension=None`` (so global scope, owner-only by default). Handlers are
async ``(args) -> dict``, return JSON-serializable dicts, and are fast: database reads are
indexed and capped. "No data" is an empty result, never an exception. Bad arguments raise
``ToolFailed`` (a definite failure the caller can report back).

No tool returns message bodies, task checkpoints/results, or file contents, except the notes and
reminder tools, which return the owner's own text, and ``recent_logs``, which returns a few
scrubbed lines of Resonant's own log.
Output shapes are documented in ``docs/interfaces.md`` ("Built-in tools").
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from resonant.executor import HandlerKey, ToolHandler
from resonant.principals import Principals
from resonant.store.db import utcnow
from resonant.tools import (
    ask,
    commands,
    ext,
    notes,
    ops,
    projects,
    reminders,
    repos,
    runs,
    system,
    tasks,
    trust,
    updates,
)
from resonant.tools.base import BuiltinContext, BuiltinModule
from resonant.tools.projects import PROJECTS
from resonant.tools.registry import ToolRegistry
from resonant.tools.system import DAEMON_STATUS, MODEL_STATUS, SYSTEM_STATUS
from resonant.tools.tasks import LIST_TASKS, LIST_TASKS_DEFAULT, LIST_TASKS_MAX, TASK_COUNTS
from resonant_sdk import ToolSpec

if TYPE_CHECKING:
    from resonant.api import DaemonState
    from resonant.ops import OpsRunner

__all__ = [
    "BUILTIN_MODULES",
    "BUILTIN_SPECS",
    "DAEMON_STATUS",
    "LIST_TASKS",
    "LIST_TASKS_DEFAULT",
    "LIST_TASKS_MAX",
    "MODEL_STATUS",
    "PROJECTS",
    "SYSTEM_STATUS",
    "TASK_COUNTS",
    "register_builtins",
]

BUILTIN_MODULES: tuple[BuiltinModule, ...] = (
    tasks,
    system,
    commands,
    notes,
    reminders,
    projects,
    ops,
    ext,
    updates,
    runs,
    repos,
    trust,
    ask,
)
BUILTIN_SPECS: tuple[ToolSpec, ...] = tuple(s for m in BUILTIN_MODULES for s in m.SPECS)


def register_builtins(
    registry: ToolRegistry,
    conn: sqlite3.Connection,
    state: DaemonState,
    *,
    principals: Principals,
    clock: Callable[[], datetime] = utcnow,
    ops_runner: OpsRunner | None = None,
) -> dict[HandlerKey, ToolHandler]:
    """Register every module's specs and return their handlers, keyed ``(None, name)``.

    ``ops_runner`` runs the ops tools' subprocesses (default: the real one).
    """
    ctx = BuiltinContext(
        conn=conn,
        state=state,
        registry=registry,
        principals=principals,
        clock=clock,
        ops_runner=ops_runner,
    )
    out: dict[HandlerKey, ToolHandler] = {}
    for module in BUILTIN_MODULES:
        handlers = module.handlers(ctx)
        # A module may refine its specs from the settings (``runs``: the repo names).
        specs_for: Callable[[BuiltinContext], tuple[ToolSpec, ...]] | None = getattr(
            module, "specs_for", None
        )
        specs = specs_for(ctx) if specs_for is not None else module.SPECS
        if set(handlers) != {s.name for s in specs}:
            raise ValueError(f"{module!r}: handlers don't match its specs")
        for spec in specs:
            registry.register(spec)
            out[(None, spec.name)] = handlers[spec.name]
    return out

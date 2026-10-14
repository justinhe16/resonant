"""Built-in tools: questions about Resonant itself and the machine it runs on.

Each tool area is a module (see ``resonant.tools.base``); ``BUILTIN_MODULES`` lists them and
``register_builtins`` iterates it. A new area is one module plus one line here.

Every builtin has ``extension=None`` (so global scope, owner-only by default). Handlers are
async ``(args) -> dict``, return JSON-serializable dicts, and are fast: database reads are
indexed and capped. "No data" is an empty result, never an exception. Bad arguments raise
``ToolFailed`` (a definite failure the caller can report back).

No tool returns message bodies, task checkpoints/results, or file contents.
Output shapes are documented in ``docs/interfaces.md`` ("Built-in tools").
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from resonant.executor import HandlerKey, ToolHandler
from resonant.principals import Principals
from resonant.tools import commands, system, tasks
from resonant.tools.base import BuiltinContext, BuiltinModule
from resonant.tools.registry import ToolRegistry
from resonant.tools.system import DAEMON_STATUS, MODEL_STATUS, SYSTEM_STATUS
from resonant.tools.tasks import LIST_TASKS, LIST_TASKS_DEFAULT, LIST_TASKS_MAX, TASK_COUNTS
from resonant_sdk import ToolSpec

if TYPE_CHECKING:
    from resonant.api import DaemonState

__all__ = [
    "BUILTIN_MODULES",
    "BUILTIN_SPECS",
    "DAEMON_STATUS",
    "LIST_TASKS",
    "LIST_TASKS_DEFAULT",
    "LIST_TASKS_MAX",
    "MODEL_STATUS",
    "SYSTEM_STATUS",
    "TASK_COUNTS",
    "register_builtins",
]

BUILTIN_MODULES: tuple[BuiltinModule, ...] = (tasks, system, commands)
BUILTIN_SPECS: tuple[ToolSpec, ...] = tuple(s for m in BUILTIN_MODULES for s in m.SPECS)


def register_builtins(
    registry: ToolRegistry,
    conn: sqlite3.Connection,
    state: DaemonState,
    *,
    principals: Principals,
) -> dict[HandlerKey, ToolHandler]:
    """Register every module's specs and return their handlers, keyed ``(None, name)``."""
    ctx = BuiltinContext(conn=conn, state=state, registry=registry, principals=principals)
    out: dict[HandlerKey, ToolHandler] = {}
    for module in BUILTIN_MODULES:
        handlers = module.handlers(ctx)
        if set(handlers) != {s.name for s in module.SPECS}:
            raise ValueError(f"{module!r}: handlers don't match its specs")
        for spec in module.SPECS:
            registry.register(spec)
            out[(None, spec.name)] = handlers[spec.name]
    return out

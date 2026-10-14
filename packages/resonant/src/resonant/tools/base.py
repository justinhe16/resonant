"""What a builtin tool module looks like, and what it gets at registration.

Each module under ``resonant.tools`` (``tasks``, ``system``, ``commands``, ...) exports:

- ``SPECS``: its ``ToolSpec``s (``extension=None``, ``source="builtin"``).
- ``handlers(ctx) -> {name: handler}``: one async ``(args) -> dict`` handler per spec.

``tools.builtin.BUILTIN_MODULES`` lists the modules and ``register_builtins`` iterates
them, so a new tool area is one new module plus one line there. ``BuiltinContext`` is what
a module may use; later phases add fields with defaults so existing modules keep working.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from resonant.executor import ToolFailed, ToolHandler
from resonant.principals import Principals
from resonant.tools.registry import ToolRegistry
from resonant_sdk import ToolSpec

if TYPE_CHECKING:
    from resonant.api import DaemonState

NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


@dataclass(frozen=True)
class BuiltinContext:
    conn: sqlite3.Connection
    state: DaemonState
    registry: ToolRegistry  # the full registry, read at call time (extensions included)
    principals: Principals


class BuiltinModule(Protocol):
    SPECS: tuple[ToolSpec, ...]

    def handlers(self, ctx: BuiltinContext) -> dict[str, ToolHandler]: ...


def no_args(name: str, args: dict[str, Any]) -> None:
    if args:
        raise ToolFailed(f"{name} takes no arguments (got {sorted(args)})")

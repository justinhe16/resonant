"""TC-1: nothing outside ``resonant/toolcore/`` calls the gate or the executor directly.

That is the "can't skip the gate" guarantee: every tool call goes through ``ToolCore`` (the
registry spec, the schema check, the origin, ``gate.evaluate``, the pin, the executor's
idempotency key). The scan is over the AST of every module in the package: a call of
``evaluate`` or ``consume`` on something named like a gate (``gate``, ``self.gate``,
``core.gate``), or of ``execute`` or ``recorded`` on something named like an executor; any
access counts, so a bound method or one passed to ``to_thread`` is caught too.
"""

from __future__ import annotations

import ast
from pathlib import Path

import resonant

PACKAGE = Path(resonant.__file__).resolve().parent

# The gate and the executor themselves, and the tool core built over them.
OWNERS = ("toolcore/", "gate/", "executor.py")

# Known exceptions, each with why. Keep it empty: an entry that no longer calls anything
# fails the test so it is removed. (The scheduler's inline L0 jobs were the last one: PER-247
# moved them onto ToolCore.)
ALLOWED: dict[str, str] = {}

GATE_CALLS = {"evaluate", "consume"}
EXECUTOR_CALLS = {"execute", "recorded"}


def _receiver_name(node: ast.expr) -> str | None:
    """The last name in a receiver: ``self.core.gate`` -> ``gate``; a call (``super()``) -> None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def direct_calls(source: str) -> list[str]:
    """``line: receiver.method`` for each direct gate or executor call in ``source``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        # Any access, not only a call: a bound method (``ev = gate.evaluate``) or one handed
        # on (``asyncio.to_thread(executor.execute, …)``) counts too.
        if not isinstance(node, ast.Attribute):
            continue
        method = node.attr
        name = (_receiver_name(node.value) or "").lower()
        if (method in GATE_CALLS and "gate" in name) or (
            method in EXECUTOR_CALLS and "executor" in name
        ):
            found.append(f"{node.lineno}: {name}.{method}")
    return found


def _modules() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if not rel.startswith(OWNERS):
            out[rel] = path.read_text(encoding="utf-8")
    return out


def test_no_module_outside_toolcore_calls_the_gate_or_executor() -> None:
    offenders = {
        rel: calls
        for rel, source in _modules().items()
        if rel not in ALLOWED and (calls := direct_calls(source))
    }
    assert offenders == {}, (
        "call tools through resonant.toolcore.ToolCore, not the gate or executor directly"
    )


def test_every_allowed_exception_is_still_needed() -> None:
    modules = _modules()
    stale = [rel for rel in ALLOWED if not direct_calls(modules.get(rel, ""))]
    assert stale == []


def test_the_scan_catches_direct_calls() -> None:
    source = """
def f(self, gate, intent, executor):
    gate.evaluate(intent)
    self.core.gate.consume("t", "h")
    self.executor.execute(intent, None, step_no=0)
    executor.recorded("k")
    asyncio.to_thread(self.executor.execute, intent)
    self.conn.execute("SELECT 1")  # not an executor
    super().execute(intent)  # an Executor subclass calling its own base
"""
    assert direct_calls(source) == [
        "3: gate.evaluate",
        "4: gate.consume",
        "5: executor.execute",
        "6: executor.recorded",
        "7: executor.execute",
    ]

"""Router intent labels and the extension catalog.

PER-256 retired the labeler, the intent set and the per-intent toolsets: the local model
only routes now (``brain/triage.py``) and Claude chooses the tools. What is left is the
label a fast-path result and a task's audit rows carry (``IntentLabel``: a builtin name such
as ``system``, or ``extension:<name>`` for a loaded extension's slash command) and the
``ExtensionCatalog`` protocol the router and the runners need from the extension host.
"""

from __future__ import annotations

from typing import Protocol

from resonant_sdk import Manifest, ToolSpec

# A fast-path result's label: a builtin name (``system``, ``job_control``, ...) or
# ``extension:<name>``. It names the command for audit rows and ``/projects``; no model
# reads it.
IntentLabel = str

EXTENSION_INTENT_PREFIX = "extension:"


def extension_intent(name: str) -> IntentLabel:
    return f"{EXTENSION_INTENT_PREFIX}{name}"


class ExtensionCatalog(Protocol):
    """What the router and the runners need from the ``ExtensionHost``."""

    def tool_specs(self, extension: str | None = None) -> list[ToolSpec]: ...

    def is_job(self, spec: ToolSpec) -> bool: ...

    def manifest(self, name: str) -> Manifest | None: ...

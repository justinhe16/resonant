"""Router intents: the one place they are defined.

Phase 1 intents and the builtin tools each one may use. ``extension:<name>`` is reserved for
Phase 3 extension intents and is never produced here. The labeler's JSON schema, the prompt's
intent definitions and the toolset mapping are all derived from ``INTENTS``.
"""

from __future__ import annotations

from typing import Literal, get_args

IntentLabel = Literal["question", "job_control", "system", "run_project", "unknown"]
INTENTS: tuple[IntentLabel, ...] = get_args(IntentLabel)
UNKNOWN: IntentLabel = "unknown"
EXTENSION_INTENT_PREFIX = "extension:"  # reserved for Phase 3

# Shown to the labeler, in this order. Keep them short: they are part of the stable prefix.
INTENT_DEFINITIONS: dict[IntentLabel, str] = {
    "question": "a general question or conversation that needs no information about Resonant",
    "job_control": "asks about Resonant's tasks or jobs: what is running, queued, waiting, done",
    "system": "asks about the health of the Mac mini, the Resonant daemon or the local model",
    "run_project": "asks Resonant to do work on a project: write code, open a PR, run a job",
    "unknown": "anything else, or unclear",
}

# Builtin tools (extension None) per intent. Filtered by principal before the model sees them.
INTENT_TOOLS: dict[IntentLabel, tuple[str, ...]] = {
    "question": (),
    "job_control": ("list_tasks", "task_counts"),
    "system": ("daemon_status", "model_status", "system_status"),
    "run_project": (),
    "unknown": (),
}


def parse_intent(value: object) -> IntentLabel | None:
    """``value`` as a Phase 1 intent, or None if it isn't one."""
    for intent in INTENTS:
        if value == intent:
            return intent
    return None

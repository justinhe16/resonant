"""Router intents: the one place they are defined.

The intents and the builtin tools each one may use. Phase 2 added ``notes``, ``reminders``
and ``ops``: plain chat reaches the same tools as the slash commands, through the model.
``extension:<name>`` is reserved for Phase 3 extension intents and is never produced here.
The labeler's JSON schema, the prompt's intent definitions and the toolset mapping are all
derived from ``INTENTS``.
"""

from __future__ import annotations

from typing import Literal, get_args

IntentLabel = Literal[
    "question", "job_control", "system", "notes", "reminders", "ops", "run_project", "unknown"
]
INTENTS: tuple[IntentLabel, ...] = get_args(IntentLabel)
UNKNOWN: IntentLabel = "unknown"
EXTENSION_INTENT_PREFIX = "extension:"  # reserved for Phase 3

# Shown to the labeler, in this order. Keep them short: they are part of the stable prefix.
INTENT_DEFINITIONS: dict[IntentLabel, str] = {
    "question": "a general question, chit-chat or thanks that needs nothing from Resonant",
    "job_control": "asks about Resonant's tasks or jobs: what is running, queued, waiting, done",
    "system": (
        "asks about the health of the Mac mini (CPU, memory, disk), the Resonant daemon or "
        "the local model, or what Resonant can do"
    ),
    "notes": "asks to save, list or delete the owner's notes",
    "reminders": "asks to set a reminder, or to list or cancel pending reminders",
    "ops": "asks Resonant to restart Ollama or itself, update itself, or show its recent logs",
    "run_project": "asks Resonant to do work on a project: write code, open a PR, run a job",
    "unknown": "anything else, or unclear",
}

# Builtin tools (extension None) per intent. Filtered by principal before the model sees them.
# A model-chosen call is always ``origin="inferred"``: the gate runs L3 and reports, asks a
# plain yes for L2 (restart_resonant, update_resonant) and a code for L1 (clear_notes).
INTENT_TOOLS: dict[IntentLabel, tuple[str, ...]] = {
    "question": (),
    "job_control": ("list_tasks", "projects", "task_counts"),
    "system": ("daemon_status", "list_commands", "model_status", "system_status"),
    "notes": ("add_note", "clear_notes", "delete_note", "list_notes"),
    "reminders": ("add_reminder", "cancel_reminder", "list_reminders"),
    "ops": ("disk", "recent_logs", "restart_ollama", "restart_resonant", "update_resonant"),
    "run_project": (),
    "unknown": (),
}


def parse_intent(value: object) -> IntentLabel | None:
    """``value`` as an intent, or None if it isn't one."""
    for intent in INTENTS:
        if value == intent:
            return intent
    return None

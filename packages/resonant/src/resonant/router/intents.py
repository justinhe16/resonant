"""Router intents: the one place they are defined.

The builtin intents and the builtin tools each one may use. Phase 2 added ``notes``,
``reminders`` and ``ops``: plain chat reaches the same tools as the slash commands, through
the model.

Phase 3 adds one ``extension:<name>`` intent per loaded extension that has a manifest
``description`` and at least one chat tool. ``IntentSet`` holds them; it is built **once at
start** from the loaded extensions (``intent_set``), so it changes only with an install,
update, remove or restart. Builtins come first in their fixed order, then the extensions
sorted by name. The labeler's JSON schema, the prompt's intent definitions and the toolset
mapping are all derived from the set.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Protocol, get_args

from resonant.gate.codes import clean_external
from resonant_sdk import Manifest, ToolSpec

BuiltinIntent = Literal[
    "question",
    "job_control",
    "system",
    "updates",
    "notes",
    "reminders",
    "ops",
    "run_project",
    "rules",
    "unknown",
]
# A builtin intent, or ``extension:<name>`` for a loaded extension. Always validated against
# the ``IntentSet`` built at start (``IntentSet.parse``), never taken from the model as is.
IntentLabel = str

INTENTS: tuple[BuiltinIntent, ...] = get_args(BuiltinIntent)
UNKNOWN: BuiltinIntent = "unknown"
EXTENSION_INTENT_PREFIX = "extension:"
DEFINITION_MAX = 120  # the manifest's own limits, kept after cleaning
EXAMPLE_MAX = 80
EXAMPLES_MAX = 3

# Shown to the labeler, in this order. Keep them short: they are part of the stable prefix.
INTENT_DEFINITIONS: dict[BuiltinIntent, str] = {
    "question": "a general question, chit-chat or thanks that needs nothing from Resonant",
    "job_control": "asks about Resonant's tasks or jobs: what is running, queued, waiting, done",
    "system": (
        "asks about the health of the Mac mini (CPU, memory, disk), the Resonant daemon or "
        "the local model, or what Resonant can do"
    ),
    "updates": "asks what's new, for an update, a summary, the briefing, or what's open",
    "notes": "asks to save, list or delete the owner's notes",
    "reminders": "asks to set a reminder, or to list or cancel pending reminders",
    "ops": "asks Resonant to restart Ollama or itself, update itself, or show its recent logs",
    "run_project": (
        "asks for a Claude worker to do something (code in a repo, a PR, ops, research, a data "
        "fix); about Claude runs (R3): what is going, cancel or merge one, answer or tell one "
        "something; or about the repos Claude works in: list, add, forget, create, add CI; "
        "not general updates or status"
    ),
    "rules": (
        "asks to set, list or forget trust rules (not repos): when Claude runs should or "
        "shouldn't ask first, or when to text right away"
    ),
    "unknown": "anything else, or unclear",
}

# Builtin tools (extension None) per intent. Filtered by principal before the model sees them.
# A model-chosen call is always ``origin="inferred"``: the gate runs L3 and reports, asks a
# plain yes for L2 (restart_resonant, update_resonant, merge_run, allow_trust_rule,
# remove_trust_rule, create_repo, add_ci, merge_ci_pr) and a code for L1 (clear_notes).
# start_run is L3: the sandbox is the wall, so the owner's request is the go-ahead.
# answer_worker and message_worker are L3: it's the owner talking to their own worker.
# add_repo and forget_repo are L3: they only change Resonant's own list (nothing on GitHub);
# list_repos is L0. add_trust_rule is L3: it only tightens trust or sets urgency; anything
# that widens trust (allow, forget) asks.
INTENT_TOOLS: dict[BuiltinIntent, tuple[str, ...]] = {
    "question": (),
    "job_control": ("list_tasks", "projects", "task_counts"),
    "system": (
        "daemon_status",
        "list_commands",
        "list_extensions",
        "model_status",
        "system_status",
    ),
    "updates": ("briefing_preview", "open_items", "pulse_preview"),
    "notes": ("add_note", "clear_notes", "delete_note", "list_notes"),
    "reminders": ("add_reminder", "cancel_reminder", "list_reminders"),
    "ops": ("disk", "recent_logs", "restart_ollama", "restart_resonant", "update_resonant"),
    "run_project": (
        "add_ci",
        "add_repo",
        "answer_worker",
        "cancel_run",
        "create_repo",
        "forget_repo",
        "list_repos",
        "list_runs",
        "merge_ci_pr",
        "merge_run",
        "message_worker",
        "run_activity",
        "start_run",
    ),
    "rules": (
        "add_trust_rule",
        "allow_tools",
        "allow_trust_rule",
        "list_trust_rules",
        "remove_trust_rule",
    ),
    "unknown": (),
}


def extension_intent(name: str) -> IntentLabel:
    return f"{EXTENSION_INTENT_PREFIX}{name}"


def extension_of(intent: IntentLabel) -> str | None:
    """``"github"`` for ``"extension:github"``; None for a builtin intent."""
    if intent.startswith(EXTENSION_INTENT_PREFIX):
        return intent.removeprefix(EXTENSION_INTENT_PREFIX) or None
    return None


def builtin_intent(value: object) -> BuiltinIntent | None:
    """``value`` as a builtin intent, or None."""
    for intent in INTENTS:
        if value == intent:
            return intent
    return None


@dataclass(frozen=True)
class ExtensionIntent:
    """One extension's intent: its cleaned manifest description and few-shot examples."""

    name: str
    definition: str
    examples: tuple[str, ...]

    @property
    def label(self) -> IntentLabel:
        return extension_intent(self.name)


def _clean(text: str, limit: int) -> str:
    return clean_external(text)[:limit].strip()


@dataclass(frozen=True)
class IntentSet:
    """The intents the router may produce: builtins, then extensions sorted by name."""

    extensions: tuple[ExtensionIntent, ...] = ()

    @classmethod
    def from_manifests(cls, manifests: Iterable[Manifest]) -> IntentSet:
        """One intent per manifest with a description and at least one tool. Strings are
        cleaned (``clean_external``: printable ASCII, no brackets or code-like tokens) and
        clipped, so they are safe in the labeler prompt. The result depends only on the
        manifests, never on their order."""
        out: dict[str, ExtensionIntent] = {}
        for m in manifests:
            definition = _clean(m.description, DEFINITION_MAX)
            if not definition or not (m.tools or m.mcp):
                continue
            cleaned = (_clean(x, EXAMPLE_MAX) for x in m.examples[:EXAMPLES_MAX])
            out[m.name] = ExtensionIntent(m.name, definition, tuple(e for e in cleaned if e))
        return cls(tuple(out[name] for name in sorted(out)))

    @property
    def labels(self) -> tuple[IntentLabel, ...]:
        return (*INTENTS, *(e.label for e in self.extensions))

    def parse(self, value: object) -> IntentLabel | None:
        """``value`` as one of these intents, or None if it isn't one."""
        builtin = builtin_intent(value)
        if builtin is not None:
            return builtin
        ext = self.extension(value)
        return None if ext is None else ext.label

    def extension(self, intent: object) -> ExtensionIntent | None:
        return next((e for e in self.extensions if e.label == intent), None)

    def definition(self, intent: IntentLabel) -> str:
        builtin = builtin_intent(intent)
        if builtin is not None:
            return INTENT_DEFINITIONS[builtin]
        ext = self.extension(intent)
        return INTENT_DEFINITIONS[UNKNOWN] if ext is None else ext.definition

    def with_manifest(self, manifest: Manifest) -> IntentSet:
        """This set with ``manifest``'s intent added or replaced (the advisory eval)."""
        kept = [e for e in self.extensions if e.name != manifest.name]
        added = IntentSet.from_manifests([manifest]).extensions
        return IntentSet(tuple(sorted([*kept, *added], key=lambda e: e.name)))


BUILTIN_INTENTS = IntentSet()


def parse_intent(value: object, intents: IntentSet = BUILTIN_INTENTS) -> IntentLabel | None:
    """``value`` as one of ``intents`` (builtins only by default), or None."""
    return intents.parse(value)


class ExtensionCatalog(Protocol):
    """What the router and the local runner need from the ``ExtensionHost``."""

    def tool_specs(self, extension: str | None = None) -> list[ToolSpec]: ...

    def is_job(self, spec: ToolSpec) -> bool: ...

    def manifest(self, name: str) -> Manifest | None: ...


def intent_set(catalog: ExtensionCatalog | None) -> IntentSet:
    """The intents for the loaded extensions (builtins only without a host)."""
    if catalog is None:
        return BUILTIN_INTENTS
    names = sorted({s.extension for s in catalog.tool_specs() if s.extension is not None})
    manifests = [m for m in (catalog.manifest(n) for n in names) if m is not None]
    return IntentSet.from_manifests(manifests)

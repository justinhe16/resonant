"""The brain (docs/brain-plan.md): context and memory for the general agent loop.

- ``context``: the prompt in cache order (``build_context``, ``BrainContext``,
  ``stable_prefix``, ``prefix_hash``, ``CatalogEntry``).
- ``briefing``: the environment briefing and "since we last talked" notes.
- ``history``: recent exchanges as turns and the running summary (``fold``).
- ``facts``: the long-term facts store and its prompt index.

Nothing here is wired into a runner yet; the brain loop (BR-1) uses it.
"""

from resonant.brain.context import (
    BRAIN_SYSTEM,
    TURN_NUDGE,
    BrainContext,
    CatalogEntry,
    build_context,
    catalog_entries,
    current_turn,
    prefix_hash,
    stable_prefix,
)
from resonant.brain.facts import Fact, FactError, FactStore, facts_dir, render_index
from resonant.brain.history import FoldResult, fold, fold_due, history_messages

__all__ = [
    "BRAIN_SYSTEM",
    "TURN_NUDGE",
    "BrainContext",
    "CatalogEntry",
    "Fact",
    "FactError",
    "FactStore",
    "FoldResult",
    "build_context",
    "catalog_entries",
    "current_turn",
    "facts_dir",
    "fold",
    "fold_due",
    "history_messages",
    "prefix_hash",
    "render_index",
    "stable_prefix",
]

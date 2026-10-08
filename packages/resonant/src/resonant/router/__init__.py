"""Router: deterministic fast path, then one LLM intent label. See ``resonant.router.router``."""

from resonant.router.fast_path import FastPathResult, match_fast_path
from resonant.router.intents import INTENT_TOOLS, INTENTS, IntentLabel
from resonant.router.labeler import Label, Labeler
from resonant.router.router import Classification, Router, classify, toolset_for

__all__ = [
    "INTENTS",
    "INTENT_TOOLS",
    "Classification",
    "FastPathResult",
    "IntentLabel",
    "Label",
    "Labeler",
    "Router",
    "classify",
    "match_fast_path",
    "toolset_for",
]

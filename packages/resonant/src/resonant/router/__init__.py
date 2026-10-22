"""Router: deterministic fast path, approval replies, then the brain. See
``resonant.router.router``."""

from resonant.router.fast_path import FastPathResult, match_fast_path
from resonant.router.intents import IntentLabel
from resonant.router.router import Router

__all__ = [
    "FastPathResult",
    "IntentLabel",
    "Router",
    "match_fast_path",
]

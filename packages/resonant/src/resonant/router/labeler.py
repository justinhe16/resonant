"""LLM intent labeler: one small ``chat_json`` call per message.

The prompt is the stable prefix from ``prefixes.py`` with the untrusted text last. Thinking
is off (the model client guarantees it), ``max_tokens`` is tiny and temperature is 0.

Failure handling (never raises for model problems):
- ``ModelUnavailableError`` (Ollama down, timeout, missing model): intent ``unknown`` with
  ``fallback=True``. The local runner then sends ``FALLBACK_REPLY``.
- ``ModelOutputError`` (the model answered, but not valid JSON): intent ``unknown`` with
  ``fallback=False``.

The ``router.label`` span records intent, confidence, latency and the prefix hash. Never the
message text.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from resonant.models import ModelClient, ModelOutputError, ModelUnavailableError, SpanFactory
from resonant.models.client import null_spans
from resonant.router.intents import UNKNOWN, IntentLabel, parse_intent
from resonant.router.prefixes import LABEL_PREFIX_HASH, LABEL_SCHEMA, label_messages

log = logging.getLogger(__name__)

LABEL_MAX_TOKENS = 20
LABEL_TEMPERATURE = 0.0


@dataclass(frozen=True)
class Label:
    intent: IntentLabel
    confidence: float
    fallback: bool  # True: the model was unavailable
    latency_ms: int
    prefix_hash: str = LABEL_PREFIX_HASH


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return max(0.0, min(1.0, float(value)))


class Labeler:
    def __init__(self, model: ModelClient) -> None:
        self.model = model

    async def label(self, text: str, *, spans: SpanFactory | None = None) -> Label:
        spans = spans or null_spans
        start = time.monotonic()
        with spans("router.label", prefix_hash=LABEL_PREFIX_HASH) as s:
            intent: IntentLabel = UNKNOWN
            confidence = 0.0
            fallback = False
            try:
                reply = await self.model.chat_json(
                    label_messages(text),
                    schema=LABEL_SCHEMA,
                    max_tokens=LABEL_MAX_TOKENS,
                    temperature=LABEL_TEMPERATURE,
                    spans=spans,
                )
            except ModelUnavailableError as e:
                log.warning("labeler: model unavailable (%s)", type(e).__name__)
                fallback = True
            except ModelOutputError:
                log.warning("labeler: model returned invalid output")
            else:
                data = reply.json or {}
                parsed = parse_intent(data.get("intent"))
                if parsed is not None:
                    intent, confidence = parsed, _confidence(data.get("confidence"))
            latency_ms = int((time.monotonic() - start) * 1000)
            s.set(intent=intent, confidence=confidence, fallback=fallback, latency_ms=latency_ms)
        return Label(intent, confidence, fallback, latency_ms)

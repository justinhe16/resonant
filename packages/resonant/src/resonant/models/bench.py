"""Latency benchmark: label-style JSON calls and single-tool-call prompts, p50/p95.

Warms the model up first (one call of each kind, untimed) so a cold load after a reboot
doesn't skew the numbers. Prompts are synthetic: no real message bodies.
"""

from __future__ import annotations

import contextlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from resonant.models.client import ChatMessage, ModelClient, ModelError
from resonant_sdk import Effect, ToolSpec

LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["chat", "question", "task", "ignore"]}},
    "required": ["label"],
}
LABEL_MESSAGES: list[ChatMessage] = [
    {
        "role": "system",
        "content": "Classify the user's message. Reply with JSON: "
        '{"label": "chat" | "question" | "task" | "ignore"}.',
    },
    {"role": "user", "content": "hey what time is it right now?"},
]

BENCH_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="current_time",
        description="The current local date and time.",
        effect=Effect.READ,
    ),
    ToolSpec(
        name="weather",
        description="Today's weather forecast for a city.",
        args_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        effect=Effect.READ,
    ),
]
TOOL_MESSAGES: list[ChatMessage] = [
    {"role": "system", "content": "Call exactly one tool to answer the user."},
    {"role": "user", "content": "what's the weather in Oakland today?"},
]


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 0..100). Raises on an empty list."""
    if not values:
        raise ValueError("no samples")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass
class Series:
    kind: str
    latencies_ms: list[float] = field(default_factory=list[float])
    failures: int = 0

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "n": len(self.latencies_ms)}
        out["failures"] = self.failures
        if self.latencies_ms:
            out["p50_ms"] = round(percentile(self.latencies_ms, 50))
            out["p95_ms"] = round(percentile(self.latencies_ms, 95))
        return out


async def run_bench(
    client: ModelClient,
    n: int = 20,
    *,
    warmup: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> list[Series]:
    """Run ``n`` label calls then ``n`` tool calls. Failed calls are counted, not timed."""

    async def label() -> None:
        await client.chat_json(LABEL_MESSAGES, schema=LABEL_SCHEMA, max_tokens=32)

    async def tool() -> None:
        reply = await client.chat_tools(TOOL_MESSAGES, tools=BENCH_TOOLS, max_tokens=128)
        if not reply.tool_calls:
            raise ModelError("no valid tool call")

    out: list[Series] = []
    for kind, call in (("label", label), ("tool", tool)):
        series = Series(kind)
        if warmup:
            with contextlib.suppress(ModelError):  # the timed runs will show it
                await call()
        for _ in range(n):
            start = clock()
            try:
                await call()
            except ModelError:
                series.failures += 1
                continue
            series.latencies_ms.append((clock() - start) * 1000)
        out.append(series)
    return out

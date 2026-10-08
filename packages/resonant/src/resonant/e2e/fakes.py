"""Fakes for the end-to-end harness: a scripted ``ModelClient`` and a recording ``OsaRunner``.

``ScriptedModel`` behaves like a well-behaved local model, deterministically:

- Label calls (``schema == LABEL_SCHEMA``): the intent comes from keyword rules over the
  user's message (``LABEL_RULES``, first match wins, default ``question``), confidence 0.9.
- ``chat_tools``: the first call asks for one tool (``task_counts`` if it is in the toolset,
  else the first tool), with no args; later calls ask for nothing.
- Answer calls (``schema == REPLY_SCHEMA``): ``{"reply": final_reply}``.
- ``probe()``: always ok.

Each call can sleep ``delay_s`` first, to stand in for model latency.

``RecordingOsaRunner`` records every osascript argv (handle, text, monotonic time) instead of
running it, and lets a caller wait for the n-th send.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from resonant.gateway.imessage.sender import OSASCRIPT, OsaResult
from resonant.models import ChatMessage, ModelReply, ProbeResult, SpanFactory, ToolCall
from resonant.router.prefixes import LABEL_SCHEMA
from resonant.runners.local import REPLY_SCHEMA
from resonant_sdk import ToolSpec

LABEL_RULES: tuple[tuple[str, str], ...] = (
    ("queue", "job_control"),
    ("task", "job_control"),
    ("job", "job_control"),
    ("mini", "system"),
    ("cpu", "system"),
    ("memory", "system"),
    ("model", "system"),
    ("refactor", "run_project"),
    ("open a pr", "run_project"),
)
DEFAULT_FINAL_REPLY = "Here's the rundown from the task list: all good."


class ScriptedModel:
    """A deterministic ``ModelClient``. ``calls`` counts every chat call by kind."""

    def __init__(
        self,
        *,
        final_reply: str = DEFAULT_FINAL_REPLY,
        delay_s: float = 0.0,
        rules: Sequence[tuple[str, str]] = LABEL_RULES,
    ) -> None:
        self.final_reply = final_reply
        self.delay_s = delay_s
        self.rules = tuple(rules)
        self.calls: dict[str, int] = {"label": 0, "tools": 0, "answer": 0, "probe": 0}

    async def _wait(self) -> None:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)

    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        max_tokens: int = 512,
        temperature: float = 0.0,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        await self._wait()
        if schema == LABEL_SCHEMA:
            self.calls["label"] += 1
            intent = self.label_for(messages[-1]["content"])
            data: dict[str, Any] = {"intent": intent, "confidence": 0.9}
        elif schema == REPLY_SCHEMA:
            self.calls["answer"] += 1
            data = {"reply": self.final_reply}
        else:
            raise ValueError("ScriptedModel: unexpected schema")
        return ModelReply(json.dumps(data), [], data, 1, 10, 5)

    async def chat_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        max_tokens: int = 512,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        await self._wait()
        self.calls["tools"] += 1
        already = any(
            m["role"] == "assistant" and m["content"].startswith('{"tool_call"') for m in messages
        )
        if already or not tools:
            return ModelReply(None, [], None, 1, 10, 5)
        names = [t.name for t in tools]
        name = "task_counts" if "task_counts" in names else names[0]
        return ModelReply(None, [ToolCall(name, {})], None, 1, 10, 5)

    async def probe(self) -> ProbeResult:
        self.calls["probe"] += 1
        return ProbeResult(True, True, True, True, 1, "scripted model")

    def label_for(self, wrapped: str) -> str:
        text = wrapped.lower()
        for keyword, intent in self.rules:
            if keyword in text:
                return intent
        return "question"


@dataclass(frozen=True)
class SentMessage:
    handle: str
    text: str
    at: float  # the runner's clock when osascript would have run


class RecordingOsaRunner:
    """An ``OsaRunner`` that records sends instead of running osascript. Always succeeds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.sent: list[SentMessage] = []
        self._changed = asyncio.Event()

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> OsaResult:
        if len(argv) != 5 or argv[0] != OSASCRIPT:
            raise ValueError("RecordingOsaRunner: unexpected argv shape")
        self.sent.append(SentMessage(handle=argv[3], text=argv[4], at=self.clock()))
        self._changed.set()
        return OsaResult(0, "", "")

    async def wait_for(self, count: int, timeout_s: float) -> SentMessage:
        """Wait until at least ``count`` sends were recorded; return the ``count``-th."""
        async with asyncio.timeout(timeout_s):
            while len(self.sent) < count:
                self._changed.clear()
                await self._changed.wait()
        return self.sent[count - 1]


async def wait_until(
    predicate: Callable[[], bool], timeout_s: float, *, poll_s: float = 0.01
) -> None:
    """Poll ``predicate`` until it is true. Raises ``TimeoutError`` after ``timeout_s``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() >= deadline:
            raise TimeoutError("condition not met in time")
        await asyncio.sleep(poll_s)

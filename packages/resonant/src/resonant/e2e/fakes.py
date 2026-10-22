"""Fakes for the end-to-end harness: a scripted ``ModelClient`` and a recording ``OsaRunner``.

PER-256: the local model only routes, so ``ScriptedModel`` answers the triage call and the
history fold, nothing else.

- **Triage** (a ``chat_json`` call with the triage schema, ``brain.triage.SCHEMA``): answers
  the next item of ``triages`` (a dict, or an exception to raise), else ``triage`` (default
  ``DEFAULT_TRIAGE``: a ``claude`` handler and the type ``other``, so the turn is Claude's).
  Each request is kept in ``triage_requests``.
- A history fold (``{summary}``) gets a fixed summary.
- ``chat_tools`` is never called by the runners any more; calling it is a test bug.
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
from typing import Any, cast

from resonant.brain.triage import SCHEMA as TRIAGE_SCHEMA
from resonant.gateway.imessage.sender import OSASCRIPT, OsaResult
from resonant.models import ChatMessage, ModelReply, ProbeResult, SpanFactory
from resonant_sdk import ToolSpec

DEFAULT_TRIAGE: dict[str, Any] = {
    "handler": "claude",
    "kind": "general",
    "message_type": "other",
    "say": "",
}


class ScriptedModel:
    """A deterministic ``ModelClient``. ``calls`` counts every chat call by kind."""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls: dict[str, int] = {"triage": 0, "fold": 0, "probe": 0}
        self.triage: dict[str, Any] = dict(DEFAULT_TRIAGE)
        self.triages: list[dict[str, Any] | BaseException] = []
        self.triage_requests: list[ToolsRequest] = []

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
        tools: list[ToolSpec] | None = None,
    ) -> ModelReply:
        await self._wait()
        if schema == TRIAGE_SCHEMA:
            self.calls["triage"] += 1
            self.triage_requests.append(ToolsRequest(list(messages), [t.name for t in tools or []]))
            item = self.triages.pop(0) if self.triages else self.triage
            if isinstance(item, BaseException):
                raise item
            data: dict[str, Any] = dict(item)
            return ModelReply(json.dumps(data), [], data, 1, 10, 5)
        if _props(schema) == {"summary"}:  # the brain's history fold
            self.calls["fold"] += 1
            data = {"summary": "Earlier: the owner and Resonant talked."}
            return ModelReply(json.dumps(data), [], data, 1, 10, 5)
        raise ValueError("ScriptedModel: unexpected schema")

    async def chat_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        max_tokens: int = 512,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        raise AssertionError("ScriptedModel: the local model no longer calls tools (PER-256)")

    async def probe(self) -> ProbeResult:
        self.calls["probe"] += 1
        return ProbeResult(True, True, True, True, 1, "scripted model")


def _props(schema: dict[str, Any]) -> set[str]:
    return set(cast(dict[str, Any], schema.get("properties") or {}))


@dataclass(frozen=True)
class ToolsRequest:
    """One model call as the scripted model saw it."""

    messages: list[ChatMessage]
    tools: list[str]  # names, in the order sent


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

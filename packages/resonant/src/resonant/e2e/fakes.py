"""Fakes for the end-to-end harness: a scripted ``ModelClient`` and a recording ``OsaRunner``.

``ScriptedModel`` behaves like a well-behaved local model, deterministically:

- Label calls (a schema with an ``intent`` property: ``LABEL_SCHEMA`` or the schema for a
  set with extension intents): the intent comes from keyword rules over the user's message
  (``LABEL_RULES``, first match wins, default ``question``), confidence 0.9. A rule's intent
  outside the schema's enum (an extension that isn't loaded) is skipped.
- ``chat_tools``: the first call asks for one tool, later calls ask for nothing. The tool is
  the first ``TOOL_RULES`` match over the current message whose tool is in the toolset, else
  ``task_counts`` if it is in the toolset, else the first tool. Args are none, except
  ``add_reminder``: "remind me in N minutes to X" becomes ``{at: <Current time> + N min, text:
  X}``, using the ``Current time:`` line the runner puts before the message.
- Answer calls (``schema == REPLY_SCHEMA``): ``{"reply": final_reply}``. A history fold
  (``{summary}``) gets a fixed summary.
- ``probe()``: always ok.

**Scripted mode** (the brain loop, BR-1): ``ScriptedModel(script=[...])``, or ``push(...)``
per test. Each ``chat_tools`` call takes the next item: a list of ``ToolCall`` (or
``(name, args)`` pairs) is one round of tool calls, a string is the final reply (``text``,
no calls), an exception is raised. An empty script answers ``final_reply``. Label and answer
calls work as above, so a turn that falls back to the intents path still labels. Every
``chat_tools`` request is kept in ``requests`` (messages and tool names), for asserting on
the prompt and the toolset.

Each call can sleep ``delay_s`` first, to stand in for model latency.

``RecordingOsaRunner`` records every osascript argv (handle, text, monotonic time) instead of
running it, and lets a caller wait for the n-th send.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from resonant.gateway.imessage.sender import OSASCRIPT, OsaResult
from resonant.models import ChatMessage, ModelReply, ProbeResult, SpanFactory, ToolCall
from resonant.router.prefixes import LABEL_SCHEMA
from resonant.runners.local import REPLY_SCHEMA
from resonant_sdk import ToolSpec

LABEL_RULES: tuple[tuple[str, str], ...] = (
    ("my prs", "extension:github"),  # only when github is loaded (in the schema's enum)
    ("time is it in", "extension:example"),
    ("my linear", "extension:linear"),  # only when linear is loaded
    ("linear ticket", "extension:linear"),
    ("remind me in", "reminders"),
    ("restart", "ops"),
    ("what can you do", "system"),
    ("clear my notes", "notes"),
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
# (keyword in the current message, tool). First match whose tool is in the toolset wins.
TOOL_RULES: tuple[tuple[str, str], ...] = (
    ("my prs", "prs"),
    ("my linear", "list_issues"),
    ("linear ticket", "save_issue"),
    ("remind me in", "add_reminder"),
    ("restart resonant", "restart_resonant"),
    ("restart ollama", "restart_ollama"),
    ("what can you do", "list_commands"),
    ("clear my notes", "clear_notes"),
)
Scripted = Sequence[ToolCall | tuple[str, dict[str, Any]]] | str | BaseException

_REMIND = re.compile(r"remind me in (\d+) minutes? to (.+?)</message>", re.IGNORECASE | re.DOTALL)
_NOW = re.compile(r"^Current time: (\S+) ", re.MULTILINE)
DEFAULT_FINAL_REPLY = "Here's the rundown from the task list: all good."


class ScriptedModel:
    """A deterministic ``ModelClient``. ``calls`` counts every chat call by kind."""

    def __init__(
        self,
        *,
        final_reply: str = DEFAULT_FINAL_REPLY,
        delay_s: float = 0.0,
        rules: Sequence[tuple[str, str]] = LABEL_RULES,
        script: Sequence[Scripted] | None = None,
    ) -> None:
        self.final_reply = final_reply
        self.delay_s = delay_s
        self.rules = tuple(rules)
        self.calls: dict[str, int] = {"label": 0, "tools": 0, "answer": 0, "probe": 0}
        self.script: list[Scripted] | None = None if script is None else list(script)
        self.requests: list[ToolsRequest] = []

    def push(self, *items: Scripted) -> None:
        """Append to the script (and switch to scripted mode)."""
        if self.script is None:
            self.script = []
        self.script.extend(items)

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
        if _is_label_schema(schema):
            self.calls["label"] += 1
            intent = self.label_for(messages[-1]["content"], _enum(schema))
            data: dict[str, Any] = {"intent": intent, "confidence": 0.9}
        elif schema == REPLY_SCHEMA:
            self.calls["answer"] += 1
            data = {"reply": self.final_reply}
        elif _props(schema) == {"summary"}:  # the brain's history fold
            self.calls["fold"] = self.calls.get("fold", 0) + 1
            data = {"summary": "Earlier: the owner and Resonant talked."}
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
        self.requests.append(ToolsRequest(list(messages), [t.name for t in tools]))
        if self.script is not None:
            return self._scripted()
        already = any(
            m["role"] == "assistant" and m["content"].startswith('{"tool_call"') for m in messages
        )
        if already or not tools:
            return ModelReply(None, [], None, 1, 10, 5)
        names = [t.name for t in tools]
        current = next(
            (m["content"] for m in reversed(messages) if "<message>" in m["content"]), ""
        )
        lowered = current.lower()
        name = next(
            (t for k, t in TOOL_RULES if k in lowered and t in names),
            "task_counts" if "task_counts" in names else names[0],
        )
        args = reminder_args(current) if name == "add_reminder" else {}
        return ModelReply(None, [ToolCall(name, args)], None, 1, 10, 5)

    def _scripted(self) -> ModelReply:
        assert self.script is not None
        if not self.script:
            return ModelReply(self.final_reply, [], None, 1, 10, 5)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            return ModelReply(item, [], None, 1, 10, 5)
        calls = [c if isinstance(c, ToolCall) else ToolCall(c[0], dict(c[1])) for c in item]
        return ModelReply(None, calls, None, 1, 10, 5)

    async def probe(self) -> ProbeResult:
        self.calls["probe"] += 1
        return ProbeResult(True, True, True, True, 1, "scripted model")

    def label_for(self, wrapped: str, allowed: Sequence[str] | None = None) -> str:
        text = wrapped.lower()
        for keyword, intent in self.rules:
            if keyword in text and (allowed is None or intent in allowed):
                return intent
        return "question"


def _enum(schema: dict[str, Any]) -> list[str]:
    props = cast(dict[str, Any], schema.get("properties") or {})
    intent = cast(dict[str, Any], props.get("intent") or {})
    return [str(v) for v in cast(list[Any], intent.get("enum") or [])]


def _props(schema: dict[str, Any]) -> set[str]:
    return set(cast(dict[str, Any], schema.get("properties") or {}))


def _is_label_schema(schema: dict[str, Any]) -> bool:
    return schema == LABEL_SCHEMA or bool(_enum(schema))


def reminder_args(turn: str) -> dict[str, Any]:
    """``add_reminder`` args for "remind me in N minutes to X", from the turn's current time."""
    now, ask = _NOW.search(turn), _REMIND.search(turn)
    if now is None or ask is None:
        return {}
    at = datetime.fromisoformat(now.group(1)) + timedelta(minutes=int(ask.group(1)))
    return {"at": at.isoformat(), "text": ask.group(2).strip()}


@dataclass(frozen=True)
class ToolsRequest:
    """One ``chat_tools`` call as the scripted model saw it."""

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

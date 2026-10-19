"""Seeding helpers for the brain tests: messages that started a task (with our reply)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from resonant.loop import NewTask
from resonant.models import ChatMessage, ModelOutputError, ModelReply, ProbeResult, SpanFactory
from resonant.store import tasks as repo
from resonant.store.db import transaction
from resonant_sdk import Event, ToolSpec

OWNER = "justin"
PRIORITY = 10


def message(
    db: sqlite3.Connection,
    text: str,
    *,
    reply: str | None = None,
    principal: str = OWNER,
    at: datetime | None = None,
    task: bool = True,
    source: str = "imessage",
) -> str:
    """Store one message event (and, with ``task``, the task it started, whose checkpoint
    holds ``reply``). Returns the event id."""
    extra: dict[str, Any] = {} if at is None else {"occurred_at": at}
    event = Event(
        source=source, type="message", principal=principal, payload={"text": text}, **extra
    )
    with transaction(db):
        repo.insert_event(db, event)
        if task:
            cp: dict[str, Any] = {} if reply is None else {"reply": reply}
            task_id = repo.insert_task(
                db,
                NewTask(
                    kind="chat",
                    runner="local",
                    priority=PRIORITY,
                    principal=principal,
                    checkpoint=cp,
                    origin_event_id=event.id,
                    trace_id=event.trace_id,
                ),
            )
            repo.attach_event(db, event.id, task_id)
    return event.id


class SummaryModel:
    """A ModelClient whose ``chat_json`` answers ``{"summary": ...}`` (or raises)."""

    def __init__(self, summary: str | None = "folded", error: Exception | None = None) -> None:
        self.summary = summary
        self.error = error
        self.calls: list[list[ChatMessage]] = []

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
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        if self.summary is None:
            raise ModelOutputError("bad")
        return ModelReply(
            text="",
            tool_calls=[],
            json={"summary": self.summary},
            latency_ms=1,
            prompt_tokens=None,
            completion_tokens=None,
        )

    async def chat_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        max_tokens: int = 512,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        raise AssertionError("not used")

    async def probe(self) -> ProbeResult:
        raise AssertionError("not used")

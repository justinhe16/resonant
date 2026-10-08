"""Event: the unit of input to Resonant.

Every inbound message, schedule firing, extension emission, and runner signal is an Event.
Events are appended to the store and are never mutated (only marked consumed).

``principal`` is always set by core from an authenticated channel identity. Any value an
extension supplies is overwritten.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from resonant_sdk.ids import new_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=new_id)
    source: str  # "imessage" | "slack" | "webhook" | "scheduler" | "ext:<name>" | "claude" | ...
    type: str  # "message" | "job.due" | "approval.reply" | "ext.<name>.<event>" | ...
    payload: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    principal: str | None = None
    # Duplicates (same dedupe_key) are dropped on ingest.
    dedupe_key: str | None = None
    occurred_at: datetime = Field(default_factory=_utcnow)
    trace_id: str = Field(default_factory=new_id)

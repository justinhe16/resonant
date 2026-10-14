"""ReminderRunner (``runner="reminder"``): sends a due reminder. No model, no tools.

The loop dispatches a reminder task once its ``wake_at`` (the due time) has passed. One
step sends ``⏰ <text>`` to the task's principal with ``dedupe_key=f"reminder:{task.id}"``
and returns ``Done``. The send is the deferred effect of an ``add_reminder`` call the gate
already allowed, so it doesn't go through the gate again.

- **Late.** More than ``LATE_AFTER`` past the due time (the daemon was down, or the send
  was retried), the text ends with ``(late, due 3:00pm)``.
- **Failures.** ``ChannelRefused`` (no identity for the principal) fails for good. Any other
  ``ChannelError`` is a retryable ``Fail``; the dedupe key prevents a double send.
- A malformed checkpoint fails for good.

Spans carry the task id and lateness, never the reminder text.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from resonant.gateway.channel import Channel, ChannelError, ChannelRefused
from resonant.loop.types import Done, Fail, StepContext, StepOutcome, Task
from resonant.observability.spans import span
from resonant.store.db import utcnow
from resonant.tools.when import format_due
from resonant_sdk import Event

NAME = "reminder"
LATE_AFTER = timedelta(seconds=120)
PREFIX = "⏰"


def reminder_text(text: str, at: datetime, *, now: datetime, tz: str) -> str:
    """``⏰ stretch``, or ``⏰ stretch (late, due 3:00pm)`` when more than 2 min late."""
    if now - at > LATE_AFTER:
        return f"{PREFIX} {text} (late, due {format_due(at, now=now, tz=tz)})"
    return f"{PREFIX} {text}"


class ReminderRunner:
    """``runner="reminder"``. ``timezone`` is ``settings.timezone`` (for the late marker)."""

    name = NAME
    uses_claude_slot = False

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        channel: Channel,
        timezone: str,
        clock: Callable[[], datetime] = utcnow,
        max_step_s: float | None = 30.0,
    ) -> None:
        self.conn = conn
        self.channel = channel
        self.timezone = timezone
        self.clock = clock
        self.max_step_s = max_step_s

    async def step(self, task: Task, events: list[Event], ctx: StepContext) -> StepOutcome:
        principal = task.principal
        if principal is None:
            return Fail("reminder task has no principal")
        parsed = _parse(task.checkpoint)
        if parsed is None:
            return Fail("reminder checkpoint is malformed")
        text, at = parsed
        now = self.clock()
        late = now - at > LATE_AFTER
        body = reminder_text(text, at, now=now, tz=self.timezone)
        with span(
            self.conn, "runner.reminder.send", trace_id=task.trace_id, task_id=task.id, late=late
        ) as s:
            try:
                await self.channel.send(principal, body, dedupe_key=f"reminder:{task.id}")
            except ChannelRefused as e:
                s.set(ok=False, error=type(e).__name__)
                return Fail(f"reminder refused: {e}")
            except ChannelError as e:
                s.set(ok=False, error=type(e).__name__)
                return Fail(f"reminder send failed: {e}", retryable=True)
            s.set(ok=True)
        return Done({"late": late, "late_s": max(0, round((now - at).total_seconds()))})


def _parse(cp: dict[str, Any]) -> tuple[str, datetime] | None:
    text, at = cp.get("text"), cp.get("at")
    if not isinstance(text, str) or not isinstance(at, str):
        return None
    try:
        due = datetime.fromisoformat(at)
    except ValueError:
        return None
    return (text, due) if due.utcoffset() is not None else None

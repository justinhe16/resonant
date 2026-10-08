"""EchoRunner: a trivial runner for tests and smoke checks. Echoes event payloads."""

from __future__ import annotations

from resonant_sdk import Event

from resonant.loop.types import Done, StepOutcome, Task


class EchoRunner:
    name = "echo"
    uses_claude_slot = False

    async def step(self, task: Task, events: list[Event]) -> StepOutcome:
        return Done(result={"echo": [e.payload for e in events]})

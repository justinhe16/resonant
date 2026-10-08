"""EchoRunner: a trivial runner for tests and smoke checks. Echoes event payloads."""

from __future__ import annotations

from resonant.loop.types import Done, StepContext, StepOutcome, Task
from resonant_sdk import Event


class EchoRunner:
    name = "echo"
    uses_claude_slot = False
    max_step_s: float | None = None

    async def step(self, task: Task, events: list[Event], ctx: StepContext) -> StepOutcome:
        return Done(result={"echo": [e.payload for e in events]})

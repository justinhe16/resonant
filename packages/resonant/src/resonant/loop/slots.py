"""Claude slot pool: caps concurrent Claude sessions and reserves capacity for on-call.

Priority decides who gets the next free slot (the loop dispatches in priority order).
Running sessions are never preempted. ``reserved_oncall`` slots can only be taken by
on-call tasks, so an incident never waits behind long background work.
"""

from __future__ import annotations


class SlotPool:
    def __init__(self, capacity: int, reserved_oncall: int = 0) -> None:
        if not 0 <= reserved_oncall < capacity:
            raise ValueError("need 0 <= reserved_oncall < capacity")
        self.capacity = capacity
        self.reserved_oncall = reserved_oncall
        self._holders: dict[str, bool] = {}  # task_id -> is on-call

    @property
    def in_use(self) -> int:
        return len(self._holders)

    def try_acquire(self, task_id: str, *, oncall: bool) -> bool:
        if task_id in self._holders:
            return True
        if self.in_use >= self.capacity:
            return False
        if not oncall:
            # Non-on-call work may use only the unreserved slots, whatever on-call holds.
            others = sum(1 for is_oncall in self._holders.values() if not is_oncall)
            if others >= self.capacity - self.reserved_oncall:
                return False
        self._holders[task_id] = oncall
        return True

    def release(self, task_id: str) -> None:
        self._holders.pop(task_id, None)

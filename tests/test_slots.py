from __future__ import annotations

import pytest
from resonant.loop import SlotPool


def test_non_oncall_limited_to_unreserved() -> None:
    pool = SlotPool(capacity=3, reserved_oncall=1)
    assert pool.try_acquire("a", oncall=False)
    assert pool.try_acquire("b", oncall=False)
    assert not pool.try_acquire("c", oncall=False)
    assert pool.try_acquire("incident", oncall=True)
    assert not pool.try_acquire("incident2", oncall=True)  # at capacity


def test_oncall_does_not_eat_unreserved_share() -> None:
    pool = SlotPool(capacity=2, reserved_oncall=1)
    assert pool.try_acquire("incident", oncall=True)
    assert pool.try_acquire("a", oncall=False)
    assert not pool.try_acquire("b", oncall=False)


def test_oncall_may_use_unreserved_slots_too() -> None:
    pool = SlotPool(capacity=3, reserved_oncall=1)
    assert all(pool.try_acquire(f"i{n}", oncall=True) for n in range(3))


def test_reacquire_and_release() -> None:
    pool = SlotPool(capacity=1)
    assert pool.try_acquire("a", oncall=False)
    assert pool.try_acquire("a", oncall=False)
    assert pool.in_use == 1
    pool.release("a")
    pool.release("a")
    assert pool.in_use == 0


@pytest.mark.parametrize("capacity,reserved", [(1, 1), (2, 3), (2, -1)])
def test_invalid(capacity: int, reserved: int) -> None:
    with pytest.raises(ValueError):
        SlotPool(capacity, reserved)

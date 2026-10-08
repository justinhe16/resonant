from __future__ import annotations

import pytest
from pydantic import ValidationError

from resonant_sdk import Event, new_id


def test_ids_unique_and_time_sortable() -> None:
    ids = [new_id() for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert all(len(i) == 26 for i in ids)
    # Same-ms ids can share a prefix; the timestamp part never goes backwards.
    assert [i[:10] for i in ids] == sorted(i[:10] for i in ids)


def test_event_defaults_and_strictness() -> None:
    e = Event(source="imessage", type="message")
    assert e.id and e.trace_id and e.payload == {}
    with pytest.raises(ValidationError):
        Event(source="x", type="y", bogus=1)  # pyright: ignore[reportCallIssue]

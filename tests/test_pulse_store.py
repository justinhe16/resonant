from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from resonant.store import pulse
from resonant.store.db import open_db

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    return open_db(tmp_path / "resonant.db")


def report(
    db: sqlite3.Connection,
    title: str = "Dori failing",
    *,
    state: object | None = None,
    urgency: pulse.Urgency = "urgent",
    clear: bool = False,
    ttl_s: int = pulse.DEFAULT_TTL_S,
    key: str = "dori/incident/1",
    source: str = "dori",
    now: datetime = NOW,
) -> pulse.Change:
    return pulse.upsert(db, source, key, title, urgency, state, clear, ttl_s, now)[0]


def told(db: sqlite3.Connection, at: datetime = NOW) -> list[pulse.Item]:
    items = pulse.pending(db, pulse.URGENCIES, at)
    pulse.mark_told(db, items, at)
    return items


def test_fingerprint_prefers_state_and_is_canonical() -> None:
    assert pulse.fingerprint("a", {"x": 1, "y": 2}) == pulse.fingerprint("b", {"y": 2, "x": 1})
    assert pulse.fingerprint("a", {"x": 1}) != pulse.fingerprint("a", {"x": 2})
    assert pulse.fingerprint("a") == pulse.fingerprint("a", None)
    assert pulse.fingerprint("a") != pulse.fingerprint("b")
    # A state that equals the title text is still a different thing.
    assert pulse.fingerprint("a", "a") != pulse.fingerprint("a")
    # Urgency and the clear flag are part of what the owner heard.
    assert pulse.fingerprint("a", urgency="urgent") != pulse.fingerprint("a")
    assert pulse.fingerprint("a", clear=True) != pulse.fingerprint("a")


def test_fingerprint_rejects_unusable_state() -> None:
    with pytest.raises(ValueError, match="JSON"):
        pulse.fingerprint("a", {"x": object()})
    with pytest.raises(ValueError, match="JSON"):
        pulse.fingerprint("a", {"x": float("nan")})
    with pytest.raises(ValueError, match="bytes"):
        pulse.fingerprint("a", {"x": "y" * pulse.MAX_STATE_BYTES})
    pulse.fingerprint("a", {"x": "y" * (pulse.MAX_STATE_BYTES - 10)})


def test_new_then_same_once_told(db: sqlite3.Connection) -> None:
    assert report(db) == "new"
    assert report(db) == "new"  # never told: still news
    assert [i.key for i in told(db)] == ["dori/incident/1"]
    assert report(db) == "same"
    assert pulse.pending(db, pulse.URGENCIES, NOW) == []


def test_a_b_a_is_a_change_each_time(db: sqlite3.Connection) -> None:
    assert report(db, state={"s": "A"}) == "new"
    told(db)
    assert report(db, state={"s": "B"}) == "changed"
    told(db)
    assert report(db, state={"s": "A"}) == "changed"  # B was told, so A is news again
    assert [i.fingerprint for i in pulse.pending(db, pulse.URGENCIES, NOW)] == [
        pulse.fingerprint("Dori failing", {"s": "A"}, urgency="urgent")
    ]


def test_untold_change_stays_pending_and_a_b_a_before_telling_is_same(
    db: sqlite3.Connection,
) -> None:
    report(db, state={"s": "A"})
    told(db)
    assert report(db, state={"s": "B"}) == "changed"
    # The owner never heard B (paused, muted); going back to A is what they last heard.
    assert report(db, state={"s": "A"}) == "same"
    assert pulse.pending(db, pulse.URGENCIES, NOW) == []
    assert report(db, state={"s": "B"}) == "changed"
    assert len(pulse.pending(db, pulse.URGENCIES, NOW)) == 1  # untold stays pending


def test_title_is_the_fingerprint_without_state(db: sqlite3.Connection) -> None:
    report(db, "PnL -1.8k")
    told(db)
    assert report(db, "PnL -1.8k") == "same"
    assert report(db, "PnL -2.4k") == "changed"


def test_mark_told_records_the_version_that_was_sent(db: sqlite3.Connection) -> None:
    report(db, state={"s": 1})
    listed = pulse.pending(db, pulse.URGENCIES, NOW)
    report(db, state={"s": 2})  # lands between listing and sending
    pulse.mark_told(db, listed, NOW)
    (still,) = pulse.pending(db, pulse.URGENCIES, NOW)
    assert still.fingerprint == pulse.fingerprint("Dori failing", {"s": 2}, urgency="urgent")
    assert still.told_at is None  # the stale list changed nothing


def test_pending_filters_urgency_source_and_orders(db: sqlite3.Connection) -> None:
    report(db, "low one", urgency="low", key="a", source="x")
    report(db, "normal one", urgency="normal", key="b", source="y", now=NOW + HOUR / 2)
    report(db, "urgent one", urgency="urgent", key="c", source="x", now=NOW + HOUR)
    later = NOW + HOUR
    assert [i.key for i in pulse.pending(db, pulse.URGENCIES, later)] == ["c", "b", "a"]
    assert [i.key for i in pulse.pending(db, ["normal", "urgent"], later)] == ["c", "b"]
    assert [i.key for i in pulse.pending(db, pulse.URGENCIES, later, sources_excluded=["x"])] == [
        "b"
    ]
    assert pulse.pending(db, [], later) == []


def test_cleared_is_pending_once_then_gone(db: sqlite3.Connection) -> None:
    report(db, "Dori failing", state={"s": "failing"})
    told(db)
    assert report(db, "Dori recovered", state={"s": "resolved"}, clear=True) == "changed"
    (item,) = pulse.pending(db, pulse.URGENCIES, NOW)
    assert item.cleared and item.title == "Dori recovered"
    assert pulse.open_items(db, NOW) == []  # cleared items aren't "open"
    pulse.mark_told(db, [item], NOW)
    assert pulse.pending(db, pulse.URGENCIES, NOW) == []
    assert pulse.open_items(db, NOW) == []
    assert report(db, "Dori recovered", state={"s": "resolved"}, clear=True) == "same"


def test_a_resolve_with_the_told_text_is_still_news(db: sqlite3.Connection) -> None:
    report(db, "PnL ok")
    told(db)
    assert report(db, "PnL ok", clear=True) == "changed"
    (item,) = pulse.pending(db, pulse.URGENCIES, NOW)
    assert item.cleared
    pulse.mark_told(db, [item], NOW)
    assert pulse.pending(db, pulse.URGENCIES, NOW) == []
    assert pulse.open_items(db, NOW) == []
    assert report(db, "PnL ok") == "changed"  # reopening after a told clear is news too


def test_escalation_is_news(db: sqlite3.Connection) -> None:
    report(db, state={"s": 1}, urgency="normal")
    told(db)
    assert report(db, state={"s": 1}, urgency="normal") == "same"
    assert report(db, state={"s": 1}, urgency="urgent") == "changed"


def test_upsert_returns_the_stored_item_and_get_finds_it(db: sqlite3.Connection) -> None:
    change, item = pulse.upsert(db, "dori", "k", " t ", "urgent", {"s": 1}, False, 3600, NOW)
    assert change == "new" and item.title == "t" and item.urgency == "urgent"
    assert item.fingerprint == pulse.fingerprint("t", {"s": 1}, urgency="urgent")
    assert pulse.get(db, "dori", "k") == item
    assert pulse.get(db, "dori", "nope") is None
    pulse.mark_told(db, [item], NOW)
    change, again = pulse.upsert(db, "dori", "k", "t", "urgent", {"s": 1}, False, 3600, NOW)
    assert change == "same" and again.told_fingerprint == again.fingerprint


def test_an_untold_clear_survives_expiry_until_told(db: sqlite3.Connection) -> None:
    report(db, "failing", state={"s": 1})
    told(db)
    report(db, "recovered", state={"s": 2}, clear=True, ttl_s=3600)  # held over a mute
    later = NOW + timedelta(days=2)
    (item,) = pulse.pending(db, pulse.URGENCIES, later)
    assert item.cleared
    pulse.mark_told(db, [item], later)
    assert pulse.pending(db, pulse.URGENCIES, later) == []
    # Told clears are pruned after a week; an expired untold open item is not pending.
    assert pulse.prune(db, later + timedelta(days=8)) == 1


def test_untold_clear_is_pruned_only_after_30_days(db: sqlite3.Connection) -> None:
    report(db, clear=True)
    assert pulse.prune(db, NOW + timedelta(days=29)) == 0
    assert pulse.prune(db, NOW + timedelta(days=31)) == 1


def test_mark_told_is_compare_and_set(db: sqlite3.Connection) -> None:
    report(db, state={"s": 1})
    stale = pulse.pending(db, pulse.URGENCIES, NOW)
    report(db, state={"s": 2})
    fresh = pulse.pending(db, pulse.URGENCIES, NOW)
    pulse.mark_told(db, fresh, NOW)
    pulse.mark_told(db, stale, NOW + HOUR)  # a stale list must not regress the told state
    item = pulse.get(db, "dori", "dori/incident/1")
    assert item is not None and item.told_at == NOW and not item.changed


@pytest.mark.parametrize(
    "kwargs",
    [
        {"now": datetime(2026, 10, 14, 15, 0)},  # naive
        {"title": "   "},
        {"title": ""},
        {"state": {"x": object()}},
        {"state": {"x": "y" * 3000}},
    ],
)
def test_upsert_refuses_unusable_input(db: sqlite3.Connection, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        report(db, **kwargs)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    assert pulse.pending(db, pulse.URGENCIES, NOW) == []


def test_a_new_report_reopens_a_cleared_item(db: sqlite3.Connection) -> None:
    report(db, state={"s": "failing"}, clear=True)
    told(db)
    assert report(db, state={"s": "failing again"}) == "changed"
    assert [i.key for i in pulse.open_items(db, NOW)] == ["dori/incident/1"]


def test_open_items_lists_changed_first(db: sqlite3.Connection) -> None:
    report(db, "told, urgent", key="a", urgency="urgent")
    told(db)
    report(db, "fresh, low", key="b", urgency="low", now=NOW + HOUR / 2)
    report(db, "fresh, normal", key="c", urgency="normal", now=NOW + HOUR / 2)
    items = pulse.open_items(db, NOW + HOUR / 2)
    assert [i.key for i in items] == ["c", "b", "a"]
    assert [i.changed for i in items] == [True, True, False]


def test_expiry_hides_items_and_a_report_extends_it(db: sqlite3.Connection) -> None:
    report(db, ttl_s=3600)
    assert len(pulse.pending(db, pulse.URGENCIES, NOW + HOUR / 2)) == 1
    assert pulse.pending(db, pulse.URGENCIES, NOW + HOUR) == []
    assert pulse.open_items(db, NOW + HOUR) == []
    report(db, ttl_s=3600, now=NOW + HOUR / 2)  # still happening
    assert len(pulse.open_items(db, NOW + HOUR)) == 1


def test_prune_removes_old_cleared_and_expired_only(db: sqlite3.Connection) -> None:
    report(db, key="expired", ttl_s=3600)
    report(db, key="cleared", clear=True)
    told(db)  # a told clear goes after a week
    report(db, key="live", ttl_s=pulse.MAX_TTL_S)
    report(db, key="fresh-clear", clear=True, now=NOW + timedelta(days=7))
    assert pulse.prune(db, NOW + timedelta(days=6)) == 0
    # expired + 7d; cleared rows are old after 7d regardless of ttl.
    assert pulse.prune(db, NOW + timedelta(days=7, hours=1, seconds=1)) == 2
    keys = {r["key"] for r in db.execute("SELECT key FROM pulse_items")}
    assert keys == {"live", "fresh-clear"}


@pytest.mark.parametrize(
    "key", ["", "A", "has space", "x" * 121, "emoji✓", "semi;colon", "a\nb", "[a7]"]
)
def test_key_validation(db: sqlite3.Connection, key: str) -> None:
    assert not pulse.valid_key(key)
    with pytest.raises(ValueError, match="key"):
        report(db, key=key)


def test_key_accepts_the_documented_alphabet(db: sqlite3.Connection) -> None:
    for key in ("a", "dori/incident/INC-412".lower(), "a.b_c:d-e/f0", "x" * 120):
        assert report(db, key=key) == "new"


@pytest.mark.parametrize("ttl_s", [0, -1, pulse.MAX_TTL_S + 1])
def test_ttl_bounds(db: sqlite3.Connection, ttl_s: int) -> None:
    with pytest.raises(ValueError, match="ttl_s"):
        report(db, ttl_s=ttl_s)
    assert report(db, ttl_s=pulse.MAX_TTL_S) == "new"


def test_bad_urgency_is_refused(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="urgency"):
        pulse.upsert(db, "x", "k", "t", "loud", None, False, 60, NOW)  # pyright: ignore[reportArgumentType]


def test_title_is_clipped(db: sqlite3.Connection) -> None:
    report(db, "  " + "x" * 500)
    (item,) = pulse.pending(db, pulse.URGENCIES, NOW)
    assert item.title == "x" * pulse.TITLE_MAX


def test_same_key_in_two_sources_is_two_items(db: sqlite3.Connection) -> None:
    report(db, source="a")
    report(db, source="b")
    assert {i.source for i in pulse.pending(db, pulse.URGENCIES, NOW)} == {"a", "b"}

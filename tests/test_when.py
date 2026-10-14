from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from resonant.tools.when import (
    AMBIGUOUS,
    DST_GAP,
    NO_TEXT,
    NO_WHEN,
    PASSED,
    TOO_FAR,
    WhenError,
    check_at,
    clock_time,
    format_due,
    parse_when,
    split_when,
)

TZ = "America/Los_Angeles"
LA = ZoneInfo(TZ)


def local(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=LA)


# Tuesday 2026-10-13, 10:00 PDT.
NOW = local(2026, 10, 13, 10, 0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("now", NOW),
        ("NOW", NOW),
        ("in 20 min", NOW + timedelta(minutes=20)),
        ("in 20min", NOW + timedelta(minutes=20)),
        ("in 1 minute", NOW + timedelta(minutes=1)),
        ("in 5 m", NOW + timedelta(minutes=5)),
        ("in 2 h", NOW + timedelta(hours=2)),
        ("in 2hrs", NOW + timedelta(hours=2)),
        ("in 3 hours", NOW + timedelta(hours=3)),
        ("3pm", local(2026, 10, 13, 15, 0)),
        ("3 PM", local(2026, 10, 13, 15, 0)),
        ("3:45pm", local(2026, 10, 13, 15, 45)),
        ("11:59 pm", local(2026, 10, 13, 23, 59)),
        ("12pm", local(2026, 10, 13, 12, 0)),
        ("at 3pm", local(2026, 10, 13, 15, 0)),
        ("15:00", local(2026, 10, 13, 15, 0)),
        ("today 15:30", local(2026, 10, 13, 15, 30)),
        ("tomorrow 9am", local(2026, 10, 14, 9, 0)),
        ("tomorrow at 09:30", local(2026, 10, 14, 9, 30)),
        ("tomorrow 12am", local(2026, 10, 14, 0, 0)),
        ("tomorrow 00:15", local(2026, 10, 14, 0, 15)),
        ("friday 9am", local(2026, 10, 16, 9, 0)),
        ("fri 9am", local(2026, 10, 16, 9, 0)),
        ("Monday 8:00am", local(2026, 10, 19, 8, 0)),
        ("tues 11am", local(2026, 10, 13, 11, 0)),  # today, still ahead
        ("tuesday 9am", local(2026, 10, 20, 9, 0)),  # today has passed: next week
        ("  in   20   min ", NOW + timedelta(minutes=20)),
    ],
)
def test_parse_table(text: str, expected: datetime) -> None:
    got = parse_when(text, now=NOW, tz=TZ)
    assert got == expected
    assert got.utcoffset() is not None


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("3", AMBIGUOUS),
        ("9:30", AMBIGUOUS),
        ("12:00", AMBIGUOUS),
        ("tomorrow 3", AMBIGUOUS),
        ("13pm", AMBIGUOUS),
        ("3:75pm", AMBIGUOUS),
        ("25:00", AMBIGUOUS),
        ("9am", PASSED),
        ("09:59", PASSED),
        ("yesterday 9am", PASSED),
        ("yesterday", PASSED),
        ("in 366 days", NO_WHEN),
        ("in 9000 h", TOO_FAR),
        ("next week", NO_WHEN),
        ("", NO_WHEN),
    ],
)
def test_parse_rejects(text: str, hint: str) -> None:
    with pytest.raises(WhenError) as e:
        parse_when(text, now=NOW, tz=TZ)
    assert str(e.value) == hint


def test_bare_unit_and_day_need_more() -> None:
    with pytest.raises(WhenError, match="in 20 min"):
        parse_when("in 20", now=NOW, tz=TZ)
    with pytest.raises(WhenError, match="tomorrow 9am"):
        parse_when("tomorrow", now=NOW, tz=TZ)


def test_tomorrow_at_2359_is_the_next_day() -> None:
    now = local(2026, 10, 13, 23, 59)
    assert parse_when("tomorrow 12:30am", now=now, tz=TZ) == local(2026, 10, 14, 0, 30)
    assert parse_when("in 2 min", now=now, tz=TZ) == local(2026, 10, 14, 0, 1)
    # "wednesday" is tomorrow; "tuesday" (today) has passed, so it is a week out.
    assert parse_when("wed 8am", now=now, tz=TZ) == local(2026, 10, 14, 8, 0)
    assert parse_when("tue 8am", now=now, tz=TZ) == local(2026, 10, 20, 8, 0)


def test_weekday_rollover_from_sunday() -> None:
    now = local(2026, 10, 18, 20, 0)  # Sunday
    assert parse_when("mon 9am", now=now, tz=TZ) == local(2026, 10, 19, 9, 0)
    assert parse_when("sat 9am", now=now, tz=TZ) == local(2026, 10, 24, 9, 0)
    assert parse_when("sun 9am", now=now, tz=TZ) == local(2026, 10, 25, 9, 0)


def test_dst_gap_is_rejected() -> None:
    now = local(2026, 3, 7, 12, 0)  # the day before clocks spring forward at 2am
    with pytest.raises(WhenError) as e:
        parse_when("tomorrow 2:30am", now=now, tz=TZ)
    assert str(e.value) == DST_GAP
    with pytest.raises(WhenError, match="doesn't exist"):
        parse_when("sunday 02:30", now=now, tz=TZ)
    # Either side of the gap is fine.
    assert parse_when("tomorrow 1:59am", now=now, tz=TZ).utcoffset() == timedelta(hours=-8)
    assert parse_when("tomorrow 3am", now=now, tz=TZ).utcoffset() == timedelta(hours=-7)


def test_dst_fold_takes_the_first() -> None:
    now = local(2026, 10, 31, 12, 0)  # the day before clocks fall back at 2am
    at = parse_when("tomorrow 1:30am", now=now, tz=TZ)
    assert at.utcoffset() == timedelta(hours=-7)  # PDT, the first 1:30am
    assert at.astimezone(UTC) == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    # "in N" is absolute time, whatever the wall clock does.
    before = local(2026, 11, 1, 0, 30)
    assert parse_when("in 3 h", now=before, tz=TZ) - before == timedelta(hours=3)


def test_zone_matters() -> None:
    now = datetime(2026, 10, 13, 17, 0, tzinfo=UTC)  # 10am LA, 1pm New York
    la = parse_when("3pm", now=now, tz=TZ)
    ny = parse_when("3pm", now=now, tz="America/New_York")
    assert la.astimezone(UTC).hour == 22 and ny.astimezone(UTC).hour == 19
    assert parse_when("now", now=now, tz=TZ) == now


@pytest.mark.parametrize(
    ("text", "at", "rest"),
    [
        ("in 20 min stretch", NOW + timedelta(minutes=20), "stretch"),
        ("3pm call Mom", local(2026, 10, 13, 15, 0), "call Mom"),
        ("3 pm call Mom", local(2026, 10, 13, 15, 0), "call Mom"),
        ("tomorrow at 9:30am Take   meds", local(2026, 10, 14, 9, 30), "Take meds"),
        ("fri 9am 3 things", local(2026, 10, 16, 9, 0), "3 things"),
        ("now ping", NOW, "ping"),
    ],
)
def test_split_when(text: str, at: datetime, rest: str) -> None:
    assert split_when(text, now=NOW, tz=TZ) == (at, rest)


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("3 call mom", AMBIGUOUS),
        ("tomorrow 3 call mom", AMBIGUOUS),
        ("yesterday 9am x", PASSED),
        ("9am x", PASSED),
        ("3pm", NO_TEXT),
        ("call mom", NO_WHEN),
        ("", NO_WHEN),
    ],
)
def test_split_when_rejects(text: str, hint: str) -> None:
    with pytest.raises(WhenError) as e:
        split_when(text, now=NOW, tz=TZ)
    assert str(e.value) == hint


def test_split_when_dst_gap() -> None:
    with pytest.raises(WhenError) as e:
        split_when("2:30am x", now=local(2026, 3, 8, 0, 30), tz=TZ)
    assert str(e.value) == DST_GAP


def test_check_at_model_path() -> None:
    check_at(NOW + timedelta(minutes=1), now=NOW)
    check_at(NOW - timedelta(seconds=30), now=NOW)  # within the grace
    with pytest.raises(WhenError, match="passed"):
        check_at(NOW - timedelta(minutes=5), now=NOW)
    with pytest.raises(WhenError, match="year"):
        check_at(NOW + timedelta(days=366), now=NOW)
    with pytest.raises(WhenError, match="offset"):
        check_at(datetime(2026, 10, 14, 9, 0), now=NOW)


def test_format_due() -> None:
    assert clock_time(local(2026, 1, 1, 0, 5)) == "12:05am"
    assert clock_time(local(2026, 1, 1, 12, 0)) == "12:00pm"
    assert format_due(local(2026, 10, 13, 15, 0), now=NOW, tz=TZ) == "3:00pm"
    assert format_due(local(2026, 10, 14, 9, 30), now=NOW, tz=TZ) == "tomorrow 9:30am"
    assert format_due(local(2026, 10, 16, 9, 0), now=NOW, tz=TZ) == "Fri 9:00am"
    assert format_due(local(2026, 10, 12, 9, 0), now=NOW, tz=TZ) == "Mon 9:00am"
    assert format_due(local(2026, 10, 20, 9, 0), now=NOW, tz=TZ) == "Oct 20 9:00am"
    # Shown in the configured zone, whatever zone ``at`` carries.
    assert format_due(datetime(2026, 10, 13, 22, 0, tzinfo=UTC), now=NOW, tz=TZ) == "3:00pm"

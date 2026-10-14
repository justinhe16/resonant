from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from resonant.extensions.cron import (
    LOOKBACK,
    MAX_LISTED,
    CronError,
    Decision,
    Downtime,
    Outcome,
    Schedule,
    decide,
    parse_schedule,
)

TZ = "America/Los_Angeles"
LA = ZoneInfo(TZ)


def local(year: int, month: int, day: int, hour: int, minute: int = 0, fold: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=LA, fold=fold)


def walls(schedule: Schedule, after: datetime, until: datetime) -> list[str]:
    """Due times as local wall clock plus zone abbreviation, e.g. ``11-01 01:30 PDT``."""
    return [d.astimezone(LA).strftime("%m-%d %H:%M %Z") for d in schedule.due_between(after, until)]


# 2026 in Los Angeles: spring forward Sun 03-08 02:00 -> 03:00, fall back Sun 11-01 02:00 -> 01:00.
SPRING = (local(2026, 3, 7, 12), local(2026, 3, 9, 12))
FALL = (local(2026, 10, 31, 12), local(2026, 11, 2, 12))


@pytest.mark.parametrize(
    ("expr", "window", "expected"),
    [
        # Fixed times: a gap time runs once right after the jump; a fold time runs once.
        ("30 2 * * *", SPRING, ["03-08 03:00 PDT", "03-09 02:30 PDT"]),
        ("0 2 * * *", SPRING, ["03-08 03:00 PDT", "03-09 02:00 PDT"]),
        ("0 3 * * *", SPRING, ["03-08 03:00 PDT", "03-09 03:00 PDT"]),
        ("30 1 * * *", SPRING, ["03-08 01:30 PST", "03-09 01:30 PDT"]),
        ("30 1 * * *", FALL, ["11-01 01:30 PDT", "11-02 01:30 PST"]),
        ("0 9 * * *", FALL, ["11-01 09:00 PST", "11-02 09:00 PST"]),
        # `*` hour or minute follows wall time: the gap hour is skipped, the fold hour twice.
        (
            "*/20 * * * *",
            (local(2026, 3, 8, 1), local(2026, 3, 8, 3, 30)),
            ["03-08 01:20 PST", "03-08 01:40 PST", "03-08 03:00 PDT", "03-08 03:20 PDT"],
        ),
        (
            "0 * * * *",
            (local(2026, 11, 1, 0, 30), local(2026, 11, 1, 2, 30)),
            ["11-01 01:00 PDT", "11-01 01:00 PST", "11-01 02:00 PST"],
        ),
    ],
)
def test_la_dst_table(expr: str, window: tuple[datetime, datetime], expected: list[str]) -> None:
    assert walls(parse_schedule(expr, TZ), *window) == expected


def test_fold_after_second_occurrence_does_not_repeat() -> None:
    # Seeded inside the repeated hour's second pass (01:40 PST), 01:45 has already happened
    # once; cronsim's wall-clock walk would offer 01:45 PDT, which is in the past.
    schedule = parse_schedule("45 1 * * *", LA)
    after = local(2026, 11, 1, 1, 40, fold=1)
    assert walls(schedule, after, local(2026, 11, 3, 0)) == ["11-02 01:45 PST"]


def test_every_ten_minutes() -> None:
    schedule = parse_schedule("*/10 * * * *", TZ)
    due = schedule.due_between(local(2026, 10, 14, 9, 5), local(2026, 10, 14, 9, 40))
    assert [d.astimezone(LA).minute for d in due] == [10, 20, 30, 40]
    assert all(d.tzinfo is UTC for d in due)
    # Strictly after: a due time equal to `after` is not repeated.
    assert schedule.next_after(local(2026, 10, 14, 9, 10)) == local(2026, 10, 14, 9, 20)


def test_weekdays_at_nine() -> None:
    schedule = parse_schedule("0 9 * * 1-5", TZ)
    # Fri 10-16 10:00 -> next is Mon 10-19 09:00.
    assert schedule.next_after(local(2026, 10, 16, 10)) == local(2026, 10, 19, 9)
    week = walls(schedule, local(2026, 10, 18, 0), local(2026, 10, 25, 0))
    assert week == [f"10-{d} 09:00 PDT" for d in (19, 20, 21, 22, 23)]


@pytest.mark.parametrize(
    ("expr", "after", "until", "expected"),
    [
        # Day 31 runs only in 31-day months.
        (
            "0 9 31 * *",
            local(2026, 1, 1, 0),
            local(2026, 8, 1, 0),
            ["01-31 09:00 PST", "03-31 09:00 PDT", "05-31 09:00 PDT", "07-31 09:00 PDT"],
        ),
        # Feb 29 runs only in leap years.
        ("0 9 29 2 *", local(2026, 1, 1, 0), local(2028, 12, 31, 0), ["02-29 09:00 PST"]),
        # The 1st of each month.
        (
            "0 0 1 * *",
            local(2026, 10, 14, 0),
            local(2027, 1, 2, 0),
            ["11-01 00:00 PDT", "12-01 00:00 PST", "01-01 00:00 PST"],
        ),
        # Day-of-month and day-of-week both restricted match either (the 15th or Mondays).
        (
            "0 9 15 * 1",
            local(2026, 10, 13, 0),
            local(2026, 10, 27, 0),
            ["10-15 09:00 PDT", "10-19 09:00 PDT", "10-26 09:00 PDT"],
        ),
    ],
)
def test_monthly_edges(expr: str, after: datetime, until: datetime, expected: list[str]) -> None:
    assert walls(parse_schedule(expr, LA), after, until) == expected


@pytest.mark.parametrize(
    ("expr", "reason"),
    [
        ("", "empty cron expression"),
        ("   ", "empty cron expression"),
        ("* * * *", "want 5 fields"),
        ("0 0 * * * *", "want 5 fields"),
        ("@daily", "write five fields"),
        ("60 * * * *", "bad minute"),
        ("0 24 * * *", "bad hour"),
        ("0 0 32 * *", "bad day-of-month"),
        ("0 0 30 2 *", "bad day-of-month"),
        ("0 0 * 13 *", "bad month"),
        ("0 0 * * 8", "bad day-of-week"),
        ("*/0 * * * *", "bad minute"),
        ("a b c d e", "bad minute"),
    ],
)
def test_invalid_expressions_rejected(expr: str, reason: str) -> None:
    with pytest.raises(CronError, match=reason):
        parse_schedule(expr, TZ)


def test_expression_is_normalized() -> None:
    assert parse_schedule("  0   9 * *  1-5 ", TZ).expr == "0 9 * * 1-5"


def test_naive_datetimes_rejected() -> None:
    schedule = parse_schedule("0 9 * * *", TZ)
    with pytest.raises(ValueError, match="after must be timezone-aware"):
        schedule.next_after(datetime(2026, 10, 14, 9))
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        decide(
            schedule,
            now=datetime(2026, 10, 14, 9),
            last_due=None,
            paused=False,
            catchup=None,
            downtime=None,
            busy=False,
        )


# --- decide ---------------------------------------------------------------------------

DAILY_9 = parse_schedule("0 9 * * *", TZ)
EVERY_10 = parse_schedule("*/10 * * * *", TZ)
NINE = local(2026, 10, 14, 9)


def run(
    schedule: Schedule = DAILY_9,
    *,
    now: datetime,
    last_due: datetime | None,
    paused: bool = False,
    catchup: timedelta | None = None,
    downtime: Downtime | None = None,
    busy: bool = False,
) -> Decision:
    return decide(
        schedule,
        now=now,
        last_due=last_due,
        paused=paused,
        catchup=catchup,
        downtime=downtime,
        busy=busy,
    )


def test_idle_before_due() -> None:
    d = run(now=NINE - timedelta(seconds=1), last_due=NINE - timedelta(days=1))
    assert d == Decision(Outcome.IDLE)


def test_on_time_runs() -> None:
    d = run(now=NINE + timedelta(seconds=5), last_due=NINE - timedelta(days=1))
    assert d.outcome is Outcome.RUN
    assert d.due == NINE and not d.late
    assert d.last_due == NINE and d.missed == ()


def test_already_claimed_is_idle() -> None:
    # A second tick in the same minute: last_due already holds 09:00.
    d = run(now=NINE + timedelta(seconds=30), last_due=NINE)
    assert d.outcome is Outcome.IDLE and d.last_due is None


def test_never_claimed_only_on_time() -> None:
    assert run(now=NINE + timedelta(seconds=10), last_due=None).due == NINE
    # Two minutes late with no history: neither run nor missed.
    assert run(now=NINE + timedelta(minutes=2), last_due=None).outcome is Outcome.IDLE


def test_coalesces_to_latest_and_misses_the_rest() -> None:
    # Down 09:05..09:42 with a 1h window: only 09:40 runs (late); 09:10..09:30 are missed.
    down = Downtime(since=local(2026, 10, 14, 9, 5), until=local(2026, 10, 14, 9, 42))
    d = run(
        EVERY_10,
        now=local(2026, 10, 14, 9, 42),
        last_due=local(2026, 10, 14, 9, 0),
        catchup=timedelta(hours=1),
        downtime=down,
    )
    assert d.outcome is Outcome.RUN and d.late
    assert d.due == local(2026, 10, 14, 9, 40) == d.last_due
    assert [m.astimezone(LA).minute for m in d.missed] == [10, 20, 30]


def test_catchup_inside_window_while_down() -> None:
    down = Downtime(since=NINE - timedelta(hours=1), until=NINE + timedelta(hours=2))
    d = run(
        now=NINE + timedelta(hours=2),
        last_due=NINE - timedelta(days=1),
        catchup=timedelta(hours=3),
        downtime=down,
    )
    assert d.outcome is Outcome.RUN and d.late and d.due == NINE


def test_catchup_outside_window_is_missed() -> None:
    down = Downtime(since=NINE - timedelta(hours=1), until=NINE + timedelta(hours=4))
    d = run(
        now=NINE + timedelta(hours=4),
        last_due=NINE - timedelta(days=1),
        catchup=timedelta(hours=3),
        downtime=down,
    )
    assert d.outcome is Outcome.MISSED
    assert d.due is None and d.missed == (NINE,) and d.last_due == NINE


def test_no_catchup_window_is_missed() -> None:
    down = Downtime(since=NINE - timedelta(hours=1), until=NINE + timedelta(minutes=5))
    d = run(now=NINE + timedelta(minutes=5), last_due=NINE - timedelta(days=1), downtime=down)
    assert d.outcome is Outcome.MISSED and d.missed == (NINE,)


def test_catchup_needs_the_daemon_down() -> None:
    # Within the window, but the daemon was up at 09:00 (say the loop stalled): missed.
    now, last = NINE + timedelta(minutes=30), NINE - timedelta(days=1)
    window = timedelta(hours=1)
    assert run(now=now, last_due=last, catchup=window).outcome is Outcome.MISSED
    earlier = Downtime(since=NINE - timedelta(hours=5), until=NINE - timedelta(hours=4))
    d = run(now=now, last_due=last, catchup=window, downtime=earlier)
    assert d.outcome is Outcome.MISSED


def test_paused_period_never_catches_up() -> None:
    last = NINE - timedelta(days=1)
    paused = run(now=NINE + timedelta(seconds=5), last_due=last, paused=True)
    assert paused.outcome is Outcome.PAUSED
    assert paused.skipped == (NINE,) and paused.last_due == NINE and paused.due is None
    # After /resume the scheduler has stored last_due = 09:00: nothing to catch up, even
    # with a window and a downtime that covers it.
    resumed = run(
        now=NINE + timedelta(minutes=10),
        last_due=paused.last_due,
        catchup=timedelta(hours=1),
        downtime=Downtime(since=NINE - timedelta(hours=1), until=NINE + timedelta(minutes=10)),
    )
    assert resumed.outcome is Outcome.IDLE


def test_pause_beats_catchup_and_overlap() -> None:
    down = Downtime(since=NINE - timedelta(hours=1), until=NINE + timedelta(minutes=30))
    d = run(
        EVERY_10,
        now=NINE + timedelta(minutes=30),
        last_due=NINE - timedelta(minutes=10),
        paused=True,
        catchup=timedelta(hours=1),
        downtime=down,
        busy=True,
    )
    assert d.outcome is Outcome.PAUSED
    assert [s.astimezone(LA).minute for s in d.skipped] == [0, 10, 20, 30]
    assert d.missed == () and d.last_due == NINE + timedelta(minutes=30)


def test_overlap_skips_and_consumes() -> None:
    d = run(now=NINE + timedelta(seconds=5), last_due=NINE - timedelta(days=1), busy=True)
    assert d.outcome is Outcome.OVERLAP
    assert d.due == NINE == d.last_due and not d.late


def test_overlap_on_a_late_catchup() -> None:
    down = Downtime(since=NINE - timedelta(hours=1), until=NINE + timedelta(minutes=20))
    d = run(
        now=NINE + timedelta(minutes=20),
        last_due=NINE - timedelta(days=1),
        catchup=timedelta(hours=1),
        downtime=down,
        busy=True,
    )
    assert d.outcome is Outcome.OVERLAP and d.late


def test_busy_does_not_matter_when_nothing_runs() -> None:
    d = run(now=NINE + timedelta(minutes=5), last_due=NINE - timedelta(days=1), busy=True)
    assert d.outcome is Outcome.MISSED


def test_missed_list_is_capped() -> None:
    every_5 = parse_schedule("*/5 * * * *", TZ)
    d = run(every_5, now=NINE + timedelta(seconds=1), last_due=NINE - timedelta(days=1))
    assert d.outcome is Outcome.RUN and d.due == NINE
    assert len(d.missed) == MAX_LISTED and d.truncated
    assert d.missed[-1] == NINE - timedelta(minutes=5)


def test_lookback_bounds_the_scan() -> None:
    last = NINE - LOOKBACK - timedelta(days=10)
    d = run(now=NINE + timedelta(seconds=1), last_due=last)
    assert d.outcome is Outcome.RUN and d.truncated
    # 09:00 on each of the 30 days after now - LOOKBACK; the 10 days before are dropped.
    assert len(d.missed) == LOOKBACK.days - 1


def test_lookback_without_older_due_is_not_truncated() -> None:
    yearly = parse_schedule("0 9 14 10 *", TZ)
    d = run(yearly, now=NINE + timedelta(seconds=1), last_due=NINE - timedelta(days=365))
    assert d.outcome is Outcome.RUN and not d.truncated and d.missed == ()


def test_fall_back_fold_runs_once_through_decide() -> None:
    schedule = parse_schedule("30 1 * * *", TZ)
    # Compare in UTC: Python never calls an ambiguous wall time equal across zones.
    first = local(2026, 11, 1, 1, 30).astimezone(UTC)  # 01:30 PDT
    d = run(schedule, now=first + timedelta(seconds=5), last_due=first - timedelta(days=1))
    assert d.outcome is Outcome.RUN and d.due == first
    # An hour later the wall clock reads 01:30 again (PST): nothing new is due.
    again = local(2026, 11, 1, 1, 30, fold=1) + timedelta(seconds=5)
    assert run(schedule, now=again, last_due=d.last_due).outcome is Outcome.IDLE


def test_spring_gap_runs_once_after_jump() -> None:
    schedule = parse_schedule("30 2 * * *", TZ)
    jump = local(2026, 3, 8, 3, 0)
    d = run(schedule, now=jump + timedelta(seconds=5), last_due=local(2026, 3, 7, 2, 30))
    assert d.outcome is Outcome.RUN and d.due == jump and not d.late
    assert run(schedule, now=jump + timedelta(minutes=45), last_due=d.last_due).outcome is (
        Outcome.IDLE
    )


def test_catchup_bounds() -> None:
    with pytest.raises(ValueError, match="catchup"):
        run(now=NINE, last_due=None, catchup=timedelta(hours=25))
    with pytest.raises(ValueError, match="catchup"):
        run(now=NINE, last_due=None, catchup=timedelta(seconds=-1))


def test_mixed_zones_compare_in_utc() -> None:
    # `now` in UTC and `last_due` in LA give the same answer as both in LA.
    d = run(now=(NINE + timedelta(seconds=5)).astimezone(UTC), last_due=NINE - timedelta(days=1))
    assert d.outcome is Outcome.RUN and d.due == NINE

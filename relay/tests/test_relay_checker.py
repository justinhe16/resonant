"""The dead-man checker: fresh, stale, failing, recovered, no duplicates, read errors."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from resonant_relay import http
from resonant_relay.alerts import Alert
from resonant_relay.checker import Checker
from resonant_relay.source import ReadError, Status

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


class FakeSource:
    def __init__(self) -> None:
        self.result: Status | Exception | None = None

    def latest(self) -> Status | None:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeNotifier:
    def __init__(self, name: str = "ntfy") -> None:
        self.name = name
        self.sent: list[Alert] = []
        self.broken = False

    def send(self, alert: Alert) -> None:
        if self.broken:
            raise http.HttpError("HTTP 500")
        self.sent.append(alert)

    def events(self) -> list[tuple[str, str]]:
        return [(a.kind, a.event) for a in self.sent]


@pytest.fixture
def rig() -> tuple[Checker, FakeSource, FakeNotifier, Clock]:
    clock, source, notifier = Clock(), FakeSource(), FakeNotifier()
    checker = Checker(source, [notifier], stale_after=300, read_fail_after=300, clock=clock)
    return checker, source, notifier, clock


def snap(clock: Clock, *failing: str, age_min: float = 0) -> Status:
    return Status(clock.now - timedelta(minutes=age_min), tuple(failing))


def test_fresh_snapshot_sends_nothing(rig: tuple[Checker, FakeSource, FakeNotifier, Clock]):
    checker, source, notifier, clock = rig
    for _ in range(5):
        source.result = snap(clock)
        checker.check()
        clock.advance(1)
    assert notifier.sent == []
    assert checker.last_check is not None


def test_stale_alerts_once_then_recovers(rig: tuple[Checker, FakeSource, FakeNotifier, Clock]):
    checker, source, notifier, clock = rig
    source.result = snap(clock)  # the last snapshot, taken at T0
    checker.check()
    clock.advance(4)
    checker.check()
    assert notifier.sent == []  # 4 minutes is not stale yet
    clock.advance(2)
    checker.check()
    assert notifier.events() == [("stale", "open")]
    assert "6m" in notifier.sent[0].message
    for _ in range(10):  # still stale: no duplicates
        clock.advance(1)
        checker.check()
    assert notifier.events() == [("stale", "open")]
    source.result = snap(clock)
    checker.check()
    assert notifier.events() == [("stale", "open"), ("stale", "recovered")]
    checker.check()
    assert len(notifier.sent) == 2


def test_no_snapshots_at_all_is_stale(rig: tuple[Checker, FakeSource, FakeNotifier, Clock]):
    checker, source, notifier, _ = rig
    source.result = None
    checker.check()
    checker.check()
    assert notifier.events() == [("stale", "open")]
    assert "no snapshots" in notifier.sent[0].message


def test_failing_health_alerts_once_then_recovers(
    rig: tuple[Checker, FakeSource, FakeNotifier, Clock],
):
    checker, source, notifier, clock = rig
    source.result = snap(clock, "brain")
    checker.check()
    assert notifier.events() == [("failing", "open")]
    assert "brain" in notifier.sent[0].message
    # Still failing (even with another row joining): one alert per incident.
    for _ in range(3):
        clock.advance(1)
        source.result = snap(clock, "brain", "imessage")
        checker.check()
    assert notifier.events() == [("failing", "open")]
    clock.advance(1)
    source.result = snap(clock)
    checker.check()
    assert notifier.events() == [("failing", "open"), ("failing", "recovered")]


def test_degraded_and_unknown_do_not_alert(rig: tuple[Checker, FakeSource, FakeNotifier, Clock]):
    checker, source, notifier, clock = rig
    source.result = Status(clock.now, ())  # parse_status only lists `failing` rows
    checker.check()
    assert notifier.sent == []


def test_a_new_incident_after_recovery_alerts_again(
    rig: tuple[Checker, FakeSource, FakeNotifier, Clock],
):
    checker, source, notifier, clock = rig
    source.result = snap(clock, "brain")
    checker.check()
    source.result = snap(clock)
    checker.check()
    source.result = snap(clock, "brain")
    checker.check()
    assert notifier.events() == [
        ("failing", "open"),
        ("failing", "recovered"),
        ("failing", "open"),
    ]


def test_stale_and_failing_are_separate_incidents(
    rig: tuple[Checker, FakeSource, FakeNotifier, Clock],
):
    checker, source, notifier, clock = rig
    source.result = snap(clock, "ollama", age_min=10)
    checker.check()
    assert sorted(notifier.events()) == [("failing", "open"), ("stale", "open")]


def test_read_error_alerts_only_when_persistent_and_distinctly(
    rig: tuple[Checker, FakeSource, FakeNotifier, Clock],
):
    checker, source, notifier, clock = rig
    source.result = snap(clock)
    checker.check()
    source.result = ReadError("Supabase read failed: HTTP 503")
    checker.check()
    clock.advance(2)
    checker.check()
    assert notifier.sent == []  # a blip is not an incident
    clock.advance(3)
    checker.check()
    assert notifier.events() == [("read_error", "open")]
    alert = notifier.sent[0]
    assert "can't read" in alert.title and "HTTP 503" in alert.message
    # Reads failing for longer than STALE_AFTER don't also claim the Mini is stale:
    # the relay can't tell, and says so in its own alert.
    for _ in range(5):
        clock.advance(1)
        checker.check()
    assert notifier.events() == [("read_error", "open")]
    source.result = snap(clock)
    checker.check()
    assert notifier.events() == [("read_error", "open"), ("read_error", "recovered")]


def test_read_blip_resets_the_read_error_timer(
    rig: tuple[Checker, FakeSource, FakeNotifier, Clock],
):
    checker, source, notifier, clock = rig
    for _ in range(4):
        source.result = ReadError("Supabase read failed: HTTP 503")
        checker.check()
        clock.advance(4)
        source.result = snap(clock)
        checker.check()
    assert notifier.sent == []


def test_read_errors_keep_an_open_stale_incident_open(
    rig: tuple[Checker, FakeSource, FakeNotifier, Clock],
):
    checker, source, notifier, clock = rig
    source.result = snap(clock, age_min=10)
    checker.check()
    source.result = ReadError("Supabase read failed: timed out")
    for _ in range(10):
        clock.advance(1)
        checker.check()
    assert notifier.events() == [("stale", "open"), ("read_error", "open")]
    source.result = snap(clock)
    checker.check()
    assert sorted(notifier.events()[2:]) == [("read_error", "recovered"), ("stale", "recovered")]


def test_unexpected_source_exception_counts_as_a_read_error(
    rig: tuple[Checker, FakeSource, FakeNotifier, Clock],
):
    checker, source, notifier, clock = rig
    source.result = RuntimeError("bug")
    checker.check()  # never raises
    clock.advance(6)
    checker.check()
    assert notifier.events() == [("read_error", "open")]
    assert "RuntimeError" in notifier.sent[0].message


def test_failed_send_is_retried_per_channel_without_duplicates():
    clock, source = Clock(), FakeSource()
    ntfy, hook = FakeNotifier("ntfy"), FakeNotifier("webhook")
    checker = Checker(source, [ntfy, hook], clock=clock)
    hook.broken = True
    source.result = snap(clock, "brain")
    checker.check()
    checker.check()
    assert ntfy.events() == [("failing", "open")]
    assert hook.sent == []
    hook.broken = False
    checker.check()
    checker.check()
    assert ntfy.events() == [("failing", "open")]
    assert hook.events() == [("failing", "open")]


def test_recovery_before_a_successful_open_send_sends_nothing():
    clock, source, n = Clock(), FakeSource(), FakeNotifier()
    checker = Checker(source, [n], clock=clock)
    n.broken = True
    source.result = snap(clock, "brain")
    checker.check()
    n.broken = False
    source.result = snap(clock)
    checker.check()
    assert n.sent == []  # the owner never heard of it, so no "recovered" either


def test_run_loop_checks_until_stopped(rig: tuple[Checker, FakeSource, FakeNotifier, Clock]):
    checker, source, _, clock = rig
    source.result = snap(clock)
    stop = threading.Event()
    t = threading.Thread(target=checker.run, args=(0.01, stop))
    t.start()
    for _ in range(400):
        if checker.last_check is not None:
            break
        time.sleep(0.005)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()

"""The dead-man checker: fresh, stale, failing, recovered, no duplicates, read errors."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from resonant_relay import http
from resonant_relay.alerts import Alert
from resonant_relay.checker import Checker
from resonant_relay.source import ReadError, Status, Statuses

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


class FakeSource:
    """`result` is personal's status (None: no snapshots at all), an exception to raise, or
    a whole per-instance map."""

    def __init__(self) -> None:
        self.result: Status | Exception | Statuses | None = None

    def latest(self) -> Statuses:
        if isinstance(self.result, Exception):
            raise self.result
        if isinstance(self.result, dict):
            return dict(self.result)
        return {} if self.result is None else {"resonant": self.result}


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


# --- golden: one instance (RELAY_INSTANCES unset) keeps today's exact text ------------------


def test_golden_personal_alert_text_is_unchanged():
    clock, source, n = Clock(), FakeSource(), FakeNotifier()
    checker = Checker(source, [n], clock=clock)
    source.result = snap(clock, "brain", age_min=6)
    checker.check()
    source.result = snap(clock)
    checker.check()
    source.result = None
    checker.check()
    source.result = ReadError("Supabase read failed: HTTP 503")
    checker.check()
    clock.advance(6)
    checker.check()
    source.result = snap(clock)
    checker.check()
    got = [(a.kind, a.event, a.title, a.message) for a in n.sent]
    assert got == [
        (
            "stale",
            "open",
            "Resonant: no snapshots",
            "Snapshots stopped: none for 6m (last 11:54 UTC).",
        ),
        ("failing", "open", "Resonant: health failing", "Failing: brain."),
        (
            "stale",
            "recovered",
            "Resonant: snapshots back",
            "Snapshots are back (latest 12:00 UTC).",
        ),
        ("failing", "recovered", "Resonant: health ok", "No health row is failing now."),
        ("stale", "open", "Resonant: no snapshots", "Snapshots stopped: no snapshots at all."),
        (
            "read_error",
            "open",
            "Resonant relay: can't read Supabase",
            "The relay can't read snapshots for 6m: Supabase read failed: HTTP 503. "
            "It can't tell whether Resonant is up.",
        ),
        (
            "stale",
            "recovered",
            "Resonant: snapshots back",
            "Snapshots are back (latest 12:06 UTC).",
        ),
        (
            "read_error",
            "recovered",
            "Resonant relay: reading again",
            "The relay reads Supabase again.",
        ),
    ]


# --- per instance (PER-305) ------------------------------------------------------------------


class Multi:
    """resonant -> ntfy; cori -> ntfy and her Slack; read_error -> ntfy only."""

    def __init__(self, instances: tuple[str, ...] = ("resonant", "cori")) -> None:
        self.clock, self.source = Clock(), FakeSource()
        self.ntfy, self.slack = FakeNotifier("ntfy"), FakeNotifier("slack:cori")
        self.checker = Checker(
            self.source,
            [self.ntfy, self.slack],
            clock=self.clock,
            instances=instances,
            routes={"resonant": ["ntfy"], "cori": ["ntfy", "slack:cori"]},
            read_error_to=["ntfy"],
        )

    def push(self, personal_age: float | None, cori_age: float | None, **failing: str) -> None:
        def one(name: str, age: float | None) -> Status | None:
            if age is None:
                return None
            fail = tuple(f for f in failing.get(name, "").split(",") if f)
            return snap(self.clock, *fail, age_min=age)

        self.source.result = {
            "resonant": one("resonant", personal_age),
            "cori": one("cori", cori_age),
        }
        self.checker.check()

    @staticmethod
    def seen(n: FakeNotifier) -> list[tuple[str | None, str, str]]:
        return [(a.instance, a.kind, a.event) for a in n.sent]


def test_cori_stale_while_personal_fresh_alerts_only_cori():
    m = Multi()
    m.push(0, 0)
    m.clock.advance(7)
    m.push(0, 7)
    m.push(0, 7)  # still stale: no duplicates
    assert m.seen(m.ntfy) == [("cori", "stale", "open")]
    assert m.seen(m.slack) == [("cori", "stale", "open")]
    alert = m.ntfy.sent[0]
    assert alert.title == "Cori: no snapshots"
    assert m.slack.sent[0].summary == "Cori: no snapshot for 7m (last 12:00 UTC)"
    m.clock.advance(1)
    m.push(0, 20 / 60)
    assert m.seen(m.ntfy)[1:] == [("cori", "stale", "recovered")]
    assert m.seen(m.slack)[1:] == [("cori", "stale", "recovered")]
    assert m.slack.sent[1].summary == "Cori: back (snapshot 20s old)"


def test_both_stale_is_two_alerts_and_two_recoveries():
    m = Multi()
    m.push(10, 10)
    assert sorted(m.seen(m.ntfy)) == [("cori", "stale", "open"), ("resonant", "stale", "open")]
    assert [a.title for a in m.ntfy.sent if a.instance == "resonant"] == ["Resonant: no snapshots"]
    assert m.seen(m.slack) == [("cori", "stale", "open")]  # personal never goes to Slack
    m.push(0, 0)
    assert sorted(m.seen(m.ntfy)[2:]) == [
        ("cori", "stale", "recovered"),
        ("resonant", "stale", "recovered"),
    ]


def test_cori_failing_text_on_slack():
    m = Multi()
    m.push(0, 0, cori="channel:slack,model")
    assert m.seen(m.ntfy) == [("cori", "failing", "open")]
    assert m.slack.sent[0].summary == "Cori: failing — channel:slack, model"
    assert m.ntfy.sent[0].title == "Cori: health failing"
    m.push(0, 0)
    assert m.seen(m.slack) == [("cori", "failing", "open"), ("cori", "failing", "recovered")]


def test_cori_never_pushed_is_stale():
    m = Multi()
    m.source.result = {"resonant": snap(m.clock)}  # no `cori` entry at all
    m.checker.check()
    assert m.seen(m.ntfy) == [("cori", "stale", "open")]
    assert m.slack.sent[0].summary == "Cori: no snapshots at all"


def test_an_unlisted_instance_is_ignored():
    clock, source, n = Clock(), FakeSource(), FakeNotifier()
    checker = Checker(source, [n], clock=clock)  # RELAY_INSTANCES unset: resonant only
    source.result = {"resonant": snap(clock), "cori": snap(clock, "brain", age_min=60)}
    checker.check()
    assert n.sent == []
    assert all(inst == "resonant" or inst == "" for inst, _ in checker.active)


def test_failed_slack_send_is_retried_without_repeating_ntfy():
    m = Multi()
    m.slack.broken = True
    m.push(0, 10)
    m.push(0, 10)
    assert m.seen(m.ntfy) == [("cori", "stale", "open")]
    assert m.slack.sent == []
    m.slack.broken = False
    m.push(0, 11)
    m.push(0, 12)
    assert m.seen(m.ntfy) == [("cori", "stale", "open")]
    assert m.seen(m.slack) == [("cori", "stale", "open")]


def test_read_error_goes_to_ntfy_only_and_keeps_instance_states():
    m = Multi()
    m.push(0, 10)  # cori stale
    m.source.result = ReadError("Supabase read failed: HTTP 503")
    for _ in range(7):
        m.clock.advance(1)
        m.checker.check()
    assert m.seen(m.ntfy) == [("cori", "stale", "open"), (None, "read_error", "open")]
    assert m.seen(m.slack) == [("cori", "stale", "open")]  # read_error isn't about Cori
    m.push(0, 0)
    assert sorted(m.seen(m.ntfy)[2:], key=str) == sorted(
        [("cori", "stale", "recovered"), (None, "read_error", "recovered")], key=str
    )
    assert m.seen(m.slack)[1:] == [("cori", "stale", "recovered")]

"""The dead-man checker (plan §1 D6).

Every CHECK_EVERY it reads the newest snapshot's status and decides three conditions:

- `stale`: no snapshot newer than STALE_AFTER (or none at all);
- `failing`: the newest snapshot has a health row in state `failing`;
- `read_error`: Supabase couldn't be read for READ_FAIL_AFTER in a row. While reads fail,
  `stale` and `failing` keep their last known value (it can't tell), so a read outage
  doesn't recover or re-open them; it is reported on its own, distinctly.

Each channel remembers what it last told the owner per condition and is only told about a
change: one alert when an incident opens, one when it recovers. A failed send is retried
on the next check, per channel, so a broken webhook never makes ntfy repeat itself.
State is in memory: after a restart an incident still open is announced once more.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from resonant_relay import http
from resonant_relay.alerts import Alert, Kind, Notifier
from resonant_relay.source import ReadError, Status, StatusSource

log = logging.getLogger("resonant_relay.checker")

KINDS: tuple[Kind, ...] = ("stale", "failing", "read_error")


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _hhmm(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%H:%M UTC")


class Checker:
    def __init__(
        self,
        source: StatusSource,
        notifiers: Sequence[Notifier],
        stale_after: float = 300.0,
        read_fail_after: float = 300.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._source = source
        self._notifiers = list(notifiers)
        self._stale_after = stale_after
        self._read_fail_after = read_fail_after
        self._clock = clock
        # What is true now, as far as the checker can tell.
        self.active: dict[Kind, bool] = dict.fromkeys(KINDS, False)
        # What each channel was last told, per condition.
        self.told: dict[str, dict[Kind, bool]] = {
            n.name: dict.fromkeys(KINDS, False) for n in self._notifiers
        }
        self._detail: dict[Kind, str] = dict.fromkeys(KINDS, "")
        self._last: Status | None = None
        self._failing_since: datetime | None = None
        self.last_check: float | None = None  # monotonic time of the last finished check

    def check(self) -> None:
        """One pass: read, decide, notify. Never raises."""
        now = self._clock()
        try:
            status = self._source.latest()
        except ReadError as e:
            self._on_read_error(now, str(e))
        except Exception as e:  # a bug in a source must not stop the dead-man switch
            self._on_read_error(now, f"unexpected {type(e).__name__}")
        else:
            self._on_status(now, status)
        self._notify(now)
        self.last_check = time.monotonic()

    def _on_read_error(self, now: datetime, error: str) -> None:
        if self._failing_since is None:
            self._failing_since = now
            log.warning("cannot read Supabase: %s", error)
        down = (now - self._failing_since).total_seconds()
        if down >= self._read_fail_after:
            self.active["read_error"] = True
            self._detail["read_error"] = f"for {_ago(down)}: {error}"

    def _on_status(self, now: datetime, status: Status | None) -> None:
        if self._failing_since is not None:
            log.info("reading Supabase again")
        self._failing_since = None
        self.active["read_error"] = False
        self._last = status
        if status is None:
            self.active["stale"] = True
            self._detail["stale"] = "no snapshots at all"
            self.active["failing"] = False
            return
        age = (now - status.taken_at).total_seconds()
        self.active["stale"] = age > self._stale_after
        self._detail["stale"] = f"none for {_ago(age)} (last {_hhmm(status.taken_at)})"
        self.active["failing"] = bool(status.failing)
        if status.failing:
            self._detail["failing"] = ", ".join(status.failing)

    def _alert(self, kind: Kind, opened: bool, now: datetime) -> Alert:
        d = self._detail[kind]
        if kind == "stale":
            if opened:
                return Alert(
                    kind, "open", "Resonant: no snapshots", f"Snapshots stopped: {d}.", now
                )
            latest = f" (latest {_hhmm(self._last.taken_at)})" if self._last else ""
            return Alert(
                kind, "recovered", "Resonant: snapshots back", f"Snapshots are back{latest}.", now
            )
        if kind == "failing":
            if opened:
                return Alert(kind, "open", "Resonant: health failing", f"Failing: {d}.", now)
            return Alert(
                kind, "recovered", "Resonant: health ok", "No health row is failing now.", now
            )
        if opened:
            return Alert(
                kind,
                "open",
                "Resonant relay: can't read Supabase",
                f"The relay can't read snapshots {d}. It can't tell whether Resonant is up.",
                now,
            )
        return Alert(
            kind,
            "recovered",
            "Resonant relay: reading again",
            "The relay reads Supabase again.",
            now,
        )

    def _notify(self, now: datetime) -> None:
        for n in self._notifiers:
            told = self.told[n.name]
            for kind in KINDS:
                want = self.active[kind]
                if told[kind] == want:
                    continue
                alert = self._alert(kind, want, now)
                try:
                    n.send(alert)
                except http.HttpError as e:
                    log.warning("%s alert (%s %s) not sent: %s", n.name, kind, alert.event, e)
                    continue
                except Exception as e:
                    log.warning(
                        "%s alert (%s %s) not sent: %s", n.name, kind, alert.event, type(e).__name__
                    )
                    continue
                told[kind] = want
                log.info("%s alert sent: %s %s", n.name, kind, alert.event)

    def run(self, every: float, stop: threading.Event) -> None:
        """Checks now and then every `every` seconds until `stop` is set."""
        while not stop.is_set():
            started = time.monotonic()
            self.check()
            stop.wait(max(0.0, every - (time.monotonic() - started)))

"""The dead-man checker (plan §1 D6; per instance since PER-305).

Every CHECK_EVERY it reads each watched instance's newest snapshot status (RELAY_INSTANCES,
default `resonant`) and decides, per instance, two conditions, plus one for the relay:

- `stale`: no snapshot newer than STALE_AFTER (or none at all, or the instance never pushed);
- `failing`: the newest snapshot has a health row in state `failing`;
- `read_error` (global): Supabase couldn't be read for READ_FAIL_AFTER in a row. While reads
  fail, every instance's `stale` and `failing` keep their last known value (it can't tell),
  so a read outage doesn't recover or re-open them; it is reported on its own, distinctly.

Each channel remembers what it last told the owner per condition it hears about (`routes`:
instance -> channel names; `read_error_to` for the relay's own) and is only told about a
change: one alert when an incident opens, one when it recovers. A failed send is retried
on the next check, per channel, so a broken Slack webhook never makes ntfy repeat itself.
Rows of an instance that isn't watched are ignored. Alerts name the instance by its display
name; `resonant`'s text is exactly what it was before instances.
State is in memory: after a restart an incident still open is announced once more.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime

from resonant_relay import http
from resonant_relay.alerts import Alert, Kind, Notifier
from resonant_relay.source import DEFAULT_INSTANCE, ReadError, Status, Statuses, StatusSource

log = logging.getLogger("resonant_relay.checker")

KINDS: tuple[Kind, ...] = ("stale", "failing", "read_error")
# A condition: (instance, kind). read_error is the relay's own, under instance "".
Key = tuple[str, Kind]
RELAY_KEY: Key = ("", "read_error")


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _hhmm(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%H:%M UTC")


def _instance_keys(inst: str) -> tuple[Key, Key]:
    return (inst, "stale"), (inst, "failing")


def display_name(instance: str) -> str:
    """The label in alerts: the daemon's default `instance.display_name` (the name,
    capitalized), so `resonant` keeps today's "Resonant" and `cori` reads "Cori"."""
    return instance.capitalize()


class Checker:
    def __init__(
        self,
        source: StatusSource,
        notifiers: Sequence[Notifier],
        stale_after: float = 300.0,
        read_fail_after: float = 300.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        instances: Sequence[str] = (DEFAULT_INSTANCE,),
        routes: Mapping[str, Collection[str]] | None = None,
        read_error_to: Collection[str] | None = None,
    ):
        self._source = source
        self._notifiers = list(notifiers)
        self._stale_after = stale_after
        self._read_fail_after = read_fail_after
        self._clock = clock
        self._instances = tuple(dict.fromkeys(instances)) or (DEFAULT_INSTANCE,)
        names = [n.name for n in self._notifiers]
        # Conditions: (instance, kind) for stale and failing; read_error is about the relay.
        self._keys: list[Key] = [k for i in self._instances for k in _instance_keys(i)]
        self._keys.append(RELAY_KEY)
        # Which channels hear about which conditions. Default: every channel, everything.
        hears: dict[str, list[Key]] = {name: [] for name in names}
        for i in self._instances:
            to = names if routes is None else routes.get(i, names)
            for name in to:
                if name in hears:
                    hears[name] += _instance_keys(i)
        for name in names if read_error_to is None else read_error_to:
            if name in hears:
                hears[name].append(RELAY_KEY)
        # What is true now, as far as the checker can tell.
        self.active: dict[Key, bool] = dict.fromkeys(self._keys, False)
        # What each channel was last told, per condition it hears about.
        self.told: dict[str, dict[Key, bool]] = {
            name: dict.fromkeys([k for k in self._keys if k in keys], False)
            for name, keys in hears.items()
        }
        self._detail: dict[Key, str] = dict.fromkeys(self._keys, "")
        self._last: dict[str, Status | None] = dict.fromkeys(self._instances)
        self._failing_since: datetime | None = None
        self.last_check: float | None = None  # monotonic time of the last finished check

    def check(self) -> None:
        """One pass: read, decide, notify. Never raises."""
        now = self._clock()
        try:
            statuses = self._source.latest()
        except ReadError as e:
            self._on_read_error(now, str(e))
        except Exception as e:  # a bug in a source must not stop the dead-man switch
            self._on_read_error(now, f"unexpected {type(e).__name__}")
        else:
            self._on_statuses(now, statuses)
        self._notify(now)
        self.last_check = time.monotonic()

    def _on_read_error(self, now: datetime, error: str) -> None:
        if self._failing_since is None:
            self._failing_since = now
            log.warning("cannot read Supabase: %s", error)
        down = (now - self._failing_since).total_seconds()
        if down >= self._read_fail_after:
            self.active[RELAY_KEY] = True
            self._detail[RELAY_KEY] = f"for {_ago(down)}: {error}"

    def _on_statuses(self, now: datetime, statuses: Statuses) -> None:
        if self._failing_since is not None:
            log.info("reading Supabase again")
        self._failing_since = None
        self.active[RELAY_KEY] = False
        # An instance that isn't watched is ignored; a watched one that is missing never
        # pushed (or has no ingest token yet), which is "no snapshots".
        for inst in self._instances:
            self._on_status(now, inst, statuses.get(inst))

    def _on_status(self, now: datetime, inst: str, status: Status | None) -> None:
        stale, failing = _instance_keys(inst)
        self._last[inst] = status
        if status is None:
            self.active[stale] = True
            self._detail[stale] = "no snapshots at all"
            self.active[failing] = False
            return
        age = (now - status.taken_at).total_seconds()
        self.active[stale] = age > self._stale_after
        self._detail[stale] = f"none for {_ago(age)} (last {_hhmm(status.taken_at)})"
        self.active[failing] = bool(status.failing)
        if status.failing:
            self._detail[failing] = ", ".join(status.failing)

    def _alert(self, key: Key, opened: bool, now: datetime) -> Alert:
        inst, kind = key
        d = self._detail[key]
        if kind == "read_error":
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
        who = display_name(inst)
        last = self._last.get(inst)
        if kind == "stale":
            if opened:
                if last is None:
                    line = f"{who}: no snapshots at all"
                else:
                    age = (now - last.taken_at).total_seconds()
                    line = f"{who}: no snapshot for {_ago(age)} (last {_hhmm(last.taken_at)})"
                return Alert(
                    kind,
                    "open",
                    f"{who}: no snapshots",
                    f"Snapshots stopped: {d}.",
                    now,
                    inst,
                    line,
                )
            latest = f" (latest {_hhmm(last.taken_at)})" if last else ""
            back = (
                f"{who}: back (snapshot {_ago((now - last.taken_at).total_seconds())} old)"
                if last
                else f"{who}: back"
            )
            return Alert(
                kind,
                "recovered",
                f"{who}: snapshots back",
                f"Snapshots are back{latest}.",
                now,
                inst,
                back,
            )
        if opened:
            return Alert(
                kind,
                "open",
                f"{who}: health failing",
                f"Failing: {d}.",
                now,
                inst,
                f"{who}: failing — {d}",
            )
        return Alert(
            kind,
            "recovered",
            f"{who}: health ok",
            "No health row is failing now.",
            now,
            inst,
            f"{who}: health ok, nothing failing",
        )

    def _notify(self, now: datetime) -> None:
        for n in self._notifiers:
            told = self.told[n.name]
            for key in told:
                want = self.active[key]
                if told[key] == want:
                    continue
                alert = self._alert(key, want, now)
                what = f"{key[0]} {key[1]}" if key[0] else key[1]
                try:
                    n.send(alert)
                except http.HttpError as e:
                    log.warning("%s alert (%s %s) not sent: %s", n.name, what, alert.event, e)
                    continue
                except Exception as e:
                    log.warning(
                        "%s alert (%s %s) not sent: %s", n.name, what, alert.event, type(e).__name__
                    )
                    continue
                told[key] = want
                log.info("%s alert sent: %s %s", n.name, what, alert.event)

    def run(self, every: float, stop: threading.Event) -> None:
        """Checks now and then every `every` seconds until `stop` is set."""
        while not stop.is_set():
            started = time.monotonic()
            self.check()
            stop.wait(max(0.0, every - (time.monotonic() - started)))

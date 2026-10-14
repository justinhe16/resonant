"""Reminder times: a small, deterministic parser for "when", in the configured timezone.

Pure functions over an explicit ``now`` (aware) and a zone (``settings.timezone``); stdlib
``zoneinfo`` only. Accepted forms (case-insensitive, extra spaces ignored):

- ``now``
- ``in N (m|min|mins|minute|minutes|h|hr|hrs|hour|hours)``, e.g. ``in 20 min``, ``in 2h``
- a time today: ``H(:MM)?(am|pm)`` (``3pm``, ``9:30 am``), or 24-hour ``HH:MM`` when the
  hour is 13 or more or zero-padded (``15:00``, ``09:30``); an optional ``at`` before it
- ``today <time>``, ``tomorrow <time>``, ``<weekday> <time>`` (the next one; today if the
  time hasn't passed yet). Weekdays may be abbreviated (``mon``, ``tues``, ``thu``).

Rejected with a ``WhenError`` whose message is a short hint for the usage reply:

- an ambiguous time (``3``, ``9:30``, ``12:00``): ``say 3pm or 15:00``
- a time that has passed (including ``yesterday …``), more than 365 days out, or a local
  time that doesn't exist because clocks spring forward (DST gap).

A repeated local time (DST fold, clocks fall back) resolves to the first one.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MAX_AHEAD = timedelta(days=365)
# The model path (ISO datetimes) allows this much in the past: a "now" can age a little
# between parsing it and running the tool.
PAST_GRACE = timedelta(seconds=60)
MAX_WHEN_TOKENS = 5  # "tomorrow at 3:30 pm" is the longest form

AMBIGUOUS = "say 3pm or 15:00"
INVALID = "that's not a time of day"
PASSED = "that time has passed"
TOO_FAR = "that's more than a year away"
DST_GAP = "that time doesn't exist that day (clocks change)"
NO_WHEN = "say when, like 3pm, in 20 min or tomorrow 9am"
NO_TEXT = "say what to remind you about"

_WEEKDAYS: dict[str, int] = {}
for _i, _names in enumerate(
    (
        ("monday", "mon"),
        ("tuesday", "tue", "tues"),
        ("wednesday", "wed"),
        ("thursday", "thu", "thur", "thurs"),
        ("friday", "fri"),
        ("saturday", "sat"),
        ("sunday", "sun"),
    )
):
    for _n in _names:
        _WEEKDAYS[_n] = _i

_IN_RE = re.compile(r"in (\d{1,6}) ?(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)")
_IN_BARE_RE = re.compile(r"in \d+")
_AMPM_RE = re.compile(r"(\d{1,2})(?::(\d{2}))? ?(am|pm)")
_H24_RE = re.compile(r"(\d{1,2}):(\d{2})")
_BARE_RE = re.compile(r"\d{1,2}(:\d{2})?")
_DAY_RE = re.compile(r"(\w+) (?:at )?(.+)")


class WhenError(ValueError):
    """A time we understood but can't use. ``str(e)`` is the hint for the usage reply."""


class _Unrecognized(ValueError):
    """Not a time at all (the caller may try a shorter prefix)."""


def zone(tz: ZoneInfo | str) -> ZoneInfo:
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _time_of_day(text: str) -> time:
    """``3pm``, ``9:30 am``, ``15:00``, ``09:30``. Raises ``_Unrecognized`` or ``WhenError``."""
    if m := _AMPM_RE.fullmatch(text):
        hour, minute = int(m[1]), int(m[2] or 0)
        if not 1 <= hour <= 12 or minute > 59:
            raise WhenError(INVALID)
        return time(hour % 12 + (12 if m[3] == "pm" else 0), minute)
    if (m := _H24_RE.fullmatch(text)) and (int(m[1]) >= 13 or m[1].startswith("0")):
        hour, minute = int(m[1]), int(m[2])
        if hour > 23 or minute > 59 or len(m[1]) != 2:
            raise WhenError(INVALID)
        return time(hour, minute)
    if _BARE_RE.fullmatch(text):
        raise WhenError(AMBIGUOUS)
    raise _Unrecognized(text)


def _local(day: date, at: time, tz: ZoneInfo) -> datetime:
    """``day`` at ``at`` in ``tz``. A DST-gap time raises; a repeated one is the first."""
    dt = datetime.combine(day, at, tzinfo=tz)  # fold=0: the earlier of a repeated time
    if dt.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != dt.replace(tzinfo=None):
        raise WhenError(DST_GAP)
    return dt


def _resolve(text: str, now: datetime, tz: ZoneInfo) -> datetime:
    today = now.astimezone(tz).date()
    if text == "now":
        return now.astimezone(tz)
    if m := _IN_RE.fullmatch(text):
        n = int(m[1])
        delta = timedelta(hours=n) if m[2].startswith("h") else timedelta(minutes=n)
        return (now + delta).astimezone(tz)
    if _IN_BARE_RE.fullmatch(text):
        raise WhenError("say in 20 min or in 2 h")
    if text == "yesterday":
        raise WhenError(PASSED)
    if text in ("today", "tomorrow") or text in _WEEKDAYS:
        raise WhenError(f"say a time, like {text} 9am")
    if m := _DAY_RE.fullmatch(text):
        word, rest = m[1], m[2]
        if word == "at":
            return _local(today, _time_of_day(rest), tz)
        if word in ("today", "tomorrow", "yesterday"):
            offset = {"today": 0, "tomorrow": 1, "yesterday": -1}[word]
            return _local(today + timedelta(days=offset), _time_of_day(rest), tz)
        if word in _WEEKDAYS:
            at = _time_of_day(rest)
            ahead = (_WEEKDAYS[word] - today.weekday()) % 7
            dt = _local(today + timedelta(days=ahead), at, tz)
            if dt < now:  # that weekday is today and the time has passed: next week
                dt = _local(today + timedelta(days=ahead + 7), at, tz)
            return dt
    return _local(today, _time_of_day(text), tz)


def parse_when(text: str, *, now: datetime, tz: ZoneInfo | str) -> datetime:
    """The aware datetime (in ``tz``) that ``text`` means. Raises ``WhenError``."""
    try:
        return _parse(text, now=now, tz=zone(tz))
    except _Unrecognized:
        raise WhenError(NO_WHEN) from None


def _parse(text: str, *, now: datetime, tz: ZoneInfo) -> datetime:
    norm = _norm(text)
    at = _resolve(norm, now, tz)
    if norm != "now":
        check_at(at, now=now, grace=timedelta(0))
    return at


def split_when(text: str, *, now: datetime, tz: ZoneInfo | str) -> tuple[datetime, str]:
    """``/remind <when> <text>``: the longest leading words that parse as a time, and the
    rest (original case, stripped). Raises ``WhenError`` with the most specific hint."""
    words = text.split()
    tzi = zone(tz)
    for n in range(min(len(words), MAX_WHEN_TOKENS), 0, -1):
        try:
            at = _parse(" ".join(words[:n]), now=now, tz=tzi)
        except _Unrecognized:
            continue
        rest = " ".join(words[n:])
        if not rest:
            raise WhenError(NO_TEXT)
        return at, rest
    raise WhenError(NO_WHEN)


def check_at(at: datetime, *, now: datetime, grace: timedelta = PAST_GRACE) -> None:
    """``at`` must be aware, not in the past (beyond ``grace``), and at most 365 days out."""
    if at.utcoffset() is None:
        raise WhenError("the time needs a UTC offset")
    if at < now - grace:
        raise WhenError(PASSED)
    if at - now > MAX_AHEAD:
        raise WhenError(TOO_FAR)


def clock_time(dt: datetime) -> str:
    """``3:00pm``."""
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"


def format_due(at: datetime, *, now: datetime, tz: ZoneInfo | str) -> str:
    """How to say ``at`` to the owner: ``3:00pm`` (today), ``tomorrow 3:00pm``, ``Tue
    3:00pm`` (within a week either way), else ``Oct 20 3:00pm``."""
    tzi = zone(tz)
    local = at.astimezone(tzi)
    days = (local.date() - now.astimezone(tzi).date()).days
    t = clock_time(local)
    if days == 0:
        return t
    if days == 1:
        return f"tomorrow {t}"
    if -6 <= days <= 6:
        return f"{local:%a} {t}"
    return f"{local:%b} {local.day} {t}"

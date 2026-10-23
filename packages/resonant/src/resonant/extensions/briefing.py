"""The core daily briefing, ``core/briefing`` (D12): one short message to the owner a day.

A core job (``scheduler.CORE_JOBS``): L0, no model, run inline by the scheduler at
``briefing.at`` (default 08:00) in ``settings.timezone``, daily. ``briefing.enabled: false``,
no iMessage channel, or no ``principals.yaml`` owner: not scheduled. Catch-up is 3h: a
daemon that was down at 08:00 and comes back by 11:00 sends it once.

The text is built by code from builtin reads, one line per section, and a section with
nothing to say is left out. When all are, it still arrives, as one short line
(:func:`nothing_line`: ``Morning. Nothing for today.`` or one of a few variants, picked by
the date):

- ``⏰ 2 reminders: 9:30am stretch, 3:00pm call mom``: the owner's pending reminders due
  today (``list_reminders``), in local time.
- ``▶ 1 running, 2 waiting (approval)``: open work from the ``projects`` query (states and
  wait reasons only, never titles or message bodies). Reminders are left out (above).
- ``✋ 1 approval pending (A7)``: ``approvals.pending_for(owner)``.
- ``⚠ model, imessage degraded``: degraded checks in ``health_snapshot``.
- ``Workers:`` then the progress rollup (EM-6, GH-2, ``extensions/rollup.py``): the news
  the owner hasn't heard (the 08:00 pulse is skipped, so the briefing says it: ended and
  merged runs, merged PRs, stalls, review requests) and what is still going, marked told
  after the send like the Updates.
- ``Updates:`` then one line per open pulse item (``store.pulse.open_items``: every
  extension, and the ``core`` brain notices not yet told, such as ``W3 iPhone 17 in stock
  ended without firing``; every urgency), changed-since-told first, at most 8 lines and
  ``+N more``. The items listed are marked told after the send (a manual run too), so the
  next pulse doesn't repeat them. github's morning summary is one of these
  (``[github] 3 open PRs, 1 failing CI, 2 awaiting your review``, reported at low urgency).
  An item with nothing to say (github's all-zero summary) is reported ``quiet`` and never
  gets here (BF-1, ``extensions/pulse.py``).

For example::

    ⏰ 2 reminders: 9:30am stretch, 3:00pm call mom
    ▶ 1 running, 2 waiting (approval)
    ✋ 1 approval pending (A7)
    ⚠ imessage degraded
    Updates:
    [github] 3 open PRs, 1 failing CI, 2 awaiting your review
    [example] PnL -$1.8k today

The whole message is at most 1,000 characters (the Updates lines are dropped, with
``+N more``, to fit), well inside one iMessage chunk. It is sent with
``dedupe_key=briefing:<local date>``, so a restart, a catch-up or an on-demand run never
sends a second one that day. Skipped while paused.

In Claude's voice (PER-273, ``brain.voice``): when the briefing has reminders, open work or
workers' news, Claude writes its prose (``Morning. You've got ...``) from the same data as
facts, and the text is the prose, then the lines code keeps verbatim (⚠ health, the Updates
items, the workers' side-effect lines, ``+N more``), then a code-built closing line
(``Needs you: approve A7.`` or ``No action needed.``: approval codes are never shown to
Claude). The empty briefing, no Claude, or a text the guard stops is the code-built one above,
exactly.

On demand (P3-14's ``/ext run core/briefing``): ``run_briefing_now(deps)``. It is keyed
``briefing:manual:<local date>:<HHMM>``, so it sends at most once a minute and never uses up
the scheduled one.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from resonant import killswitch
from resonant.executor import CallContext
from resonant.extensions import notify, rollup
from resonant.extensions.pulse_job import (
    deliver,
    fit_entries,
    fit_lines,
    is_worker_item,
    item_line,
    prune_inflight,
    refresh_rollup,
    running_workers,
    worker_entries,
)
from resonant.monitor import health_snapshot
from resonant.store import pulse as pulse_store
from resonant.store.approvals import answerable as pending_for  # BA-1: codes that bind
from resonant.store.db import kv_get, utcnow
from resonant.tools.projects import clip, projects
from resonant.tools.reminders import list_reminders
from resonant.tools.when import clock_time

if TYPE_CHECKING:
    from resonant.brain.voice import Facts, Voice
    from resonant.config import BriefingConfig, Settings
    from resonant.extensions.scheduler import CoreDeps, CoreJob
    from resonant.gateway.channel import Channel

log = logging.getLogger(__name__)
# ``brain.notices.KEY_PREFIX`` (importing it here is a cycle through the router)
NOTICE_PREFIX = "notice/"

JOB_ID = "briefing"
CATCHUP = timedelta(hours=3)
TIMEOUT_S = 60.0
MAX_CHARS = 1000
UPDATES_SHOWN = 8
UPDATES_HEADER = "Updates:"
# The whole briefing when every section is empty: terse, like a coworker's "morning".
NOTHING_LINES = (
    "Morning. Nothing for today.",
    "Morning. Clear day, nothing on.",
    "Morning. Nothing on the list today.",
    "Morning. All quiet today.",
)
NOTHING = NOTHING_LINES[0]  # the line when no date is known (a Briefing built without one)
REMINDERS_SHOWN = 3
REMINDER_TEXT_MAX = 30
CODES_SHOWN = 5
OPEN_STATES = ("running", "waiting", "queued")


def schedule(cfg: BriefingConfig) -> str:
    """The briefing's daily cron, in ``settings.timezone``."""
    at = cfg.local_time
    return f"{at.minute} {at.hour} * * *"


def dedupe_key(day: date) -> str:
    return f"briefing:{day.isoformat()}"


def manual_dedupe_key(local_now: datetime) -> str:
    """An on-demand run's key: at most one per local minute, separate from the day's."""
    return f"briefing:manual:{local_now.date().isoformat()}:{local_now:%H%M}"


# --- the content: pure data, then a pure render -----------------------------------------


@dataclass(frozen=True)
class Briefing:
    reminders: tuple[tuple[str, str], ...] = ()  # (local clock time, text), soonest first
    running: int = 0
    waiting: tuple[str, ...] = ()  # one wait reason per waiting task
    queued: int = 0
    approvals: tuple[str, ...] = ()  # one code per pending approval ("" when it has none)
    degraded: tuple[str, ...] = ()  # health check names
    updates: tuple[pulse_store.Item, ...] = ()  # open pulse items, most changed first
    workers: tuple[pulse_store.Item, ...] = ()  # running workers, and finished ones untold
    roll: rollup.Rollup = field(default_factory=rollup.Rollup)  # the Workers rollup's names
    day: date | None = None  # the local date (picks the empty briefing's line)


def nothing_line(day: date | None) -> str:
    """The empty briefing: one of ``NOTHING_LINES``, by date (the same all day, so a rerun
    of the day's key says the same thing)."""
    return NOTHING if day is None else NOTHING_LINES[day.toordinal() % len(NOTHING_LINES)]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _reminders_line(b: Briefing) -> str | None:
    if not b.reminders:
        return None
    shown = [f"{t} {clip(text, REMINDER_TEXT_MAX)}" for t, text in b.reminders[:REMINDERS_SHOWN]]
    more = len(b.reminders) - REMINDERS_SHOWN
    if more > 0:
        shown.append(f"+{more} more")
    return f"⏰ {_plural(len(b.reminders), 'reminder')}: {', '.join(shown)}"


def _work_line(b: Briefing) -> str | None:
    parts: list[str] = []
    if b.running:
        parts.append(f"{b.running} running")
    if b.waiting:
        reasons = ", ".join(sorted(set(b.waiting)))
        parts.append(f"{len(b.waiting)} waiting ({reasons})")
    if b.queued:
        parts.append(f"{b.queued} queued")
    return "▶ " + ", ".join(parts) if parts else None


def approvals_line(b: Briefing) -> str | None:
    if not b.approvals:
        return None
    codes = [c for c in b.approvals if c]
    line = f"✋ {_plural(len(b.approvals), 'approval')} pending"
    if codes:
        shown = ", ".join(codes[:CODES_SHOWN]) + (", …" if len(codes) > CODES_SHOWN else "")
        line += f" ({shown})"
    return line


def _health_line(b: Briefing) -> str | None:
    return f"⚠ {', '.join(b.degraded)} degraded" if b.degraded else None


def _updates_lines(b: Briefing, room: int) -> tuple[list[str], list[pulse_store.Item]]:
    """The Updates section (header, lines, ``+N more``) in ``room`` characters, and the
    items it shows."""
    if not b.updates:
        return [], []
    candidates = b.updates[:UPDATES_SHOWN]
    lines, _ = fit_lines([item_line(i) for i in candidates], room - len(UPDATES_HEADER) - 1)
    shown = list(candidates[: len(lines)])
    if not shown:
        return [], []
    more = len(b.updates) - len(shown)
    return [UPDATES_HEADER, *lines, *([f"+{more} more"] if more else [])], shown


def render_full(b: Briefing) -> tuple[str, list[pulse_store.Item]]:
    """The message (one line per non-empty section, at most ``MAX_CHARS``) and the Updates
    items it lists."""
    lines = [
        line
        for line in (
            _reminders_line(b),
            _work_line(b),
            approvals_line(b),
            _health_line(b),
        )
        if line
    ]
    base = "\n".join(lines)
    room = MAX_CHARS - len(base) - (1 if base else 0)
    news = [i for i in b.workers if i.changed]
    going = [i for i in b.workers if not i.changed]
    workers, listed, more = fit_entries(worker_entries(news, context=going, roll=b.roll), room)
    if more:
        workers.append(f"+{more} more")
    lines += workers
    used = len("\n".join(workers)) + (1 if workers else 0)
    extra, shown = _updates_lines(b, room - used)
    text = "\n".join([*lines, *extra]) or nothing_line(b.day)
    text = text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1].rstrip() + "…"
    return text, [*listed, *shown]


def render(b: Briefing) -> str:
    return render_full(b)[0]


def sections(b: Briefing) -> list[str]:
    """Names of the non-empty sections (the run's result; no content). Workers counts only
    when the rollup has a line to show."""
    news = [i for i in b.workers if i.changed]
    going = [i for i in b.workers if not i.changed]
    named = {
        "reminders": _reminders_line(b),
        "work": _work_line(b),
        "approvals": approvals_line(b),
        "health": _health_line(b),
        "workers": "x" if worker_entries(news, context=going, roll=b.roll) else None,
        "updates": "x" if b.updates else None,
    }
    return [name for name, line in named.items() if line]


# --- reads ---------------------------------------------------------------------------------


def _reminders(
    conn: sqlite3.Connection, owner: str, now: datetime, day: date, tz: ZoneInfo
) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for r in list_reminders(conn, owner=owner, now=now, tz=tz.key)["reminders"]:
        item = cast(dict[str, Any], r)
        at = datetime.fromisoformat(str(item["at"])).astimezone(tz)
        if at.date() == day:
            out.append((clock_time(at), str(item["text"])))
    return tuple(out)


def _work(conn: sqlite3.Connection) -> tuple[int, tuple[str, ...], int]:
    """Open work from the ``projects`` query: states and wait reasons only."""
    items = cast(list[dict[str, Any]], projects(conn, {"all": True})["items"])
    running = queued = 0
    waiting: list[str] = []
    for item in items:
        if item["kind"] == "reminder" or item["state"] not in OPEN_STATES:
            continue
        if item["state"] == "running":
            running += 1
        elif item["state"] == "waiting":
            waiting.append(str(item["wait_reason"] or "?"))
        else:
            queued += 1
    return running, tuple(waiting), queued


def _degraded(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(name for name, s in health_snapshot(conn).items() if s["state"] == "degraded")


def _updates(conn: sqlite3.Connection, now: datetime) -> tuple[pulse_store.Item, ...]:
    """Open pulse items for the Updates section: extension items (the briefing has its own
    approvals and health lines) and brain notices the owner hasn't heard (``core`` /
    ``notice/``: a watch that ended without firing, one held by the daily cap), not from
    muted extensions (they are neither listed nor marked told)."""
    items = [
        i
        for i in pulse_store.open_items(conn, now)
        if i.source != "core" or (i.key.startswith(NOTICE_PREFIX) and i.changed)
    ]
    muted = {s for s in {i.source for i in items} if notify.muted_until(conn, s, now)}
    return tuple(i for i in items if i.source not in muted)


def _workers(conn: sqlite3.Connection, now: datetime) -> tuple[pulse_store.Item, ...]:
    """The Workers section: runs still going, and the rollup's news the owner hasn't heard
    of (EM-6, GH-2; the 08:00 pulse is skipped, so the briefing says what it would have)."""
    pending = pulse_store.pending(conn, ("urgent", "normal", "low"), now)
    finished = rollup.held_while_stale(conn, now, [i for i in pending if is_worker_item(i)])
    keys = {i.key for i in finished}
    told = [*running_workers(conn, now), *rollup.quiet_items(conn, now)]
    return (*finished, *(i for i in told if i.key not in keys))


def gather(
    conn: sqlite3.Connection,
    *,
    owner: str,
    now: datetime,
    day: date,
    tz: ZoneInfo,
    stall_after: timedelta = rollup.STALL_AFTER,
) -> Briefing:
    """Every section's data for local ``day``."""
    running, waiting, queued = _work(conn)
    workers = _workers(conn, now)
    return Briefing(
        reminders=_reminders(conn, owner, now, day, tz),
        running=running,
        waiting=waiting,
        queued=queued,
        approvals=tuple(a.code or "" for a in pending_for(conn, owner, now)),
        degraded=_degraded(conn),
        updates=_updates(conn, now),
        workers=workers,
        roll=rollup.gather(conn, now, workers, stall_after=stall_after),
        day=day,
    )


# --- Claude's voice ------------------------------------------------------------------------

_MORE = re.compile(r"\+\d+ more")


def briefing_facts(
    conn: sqlite3.Connection, b: Briefing, listed: list[pulse_store.Item], text: str
) -> Facts | None:
    """The briefing's facts for Claude (PER-273): today's reminders and open work, and the
    workers' news as items. The health line, the Updates items, side-effect lines and ``+N
    more`` are the tail, kept code-built; approval codes are the closing line's only. None
    when there is nothing to say (the empty briefing) or an item can't be said."""
    # late: ``resonant.brain`` imports the router, which imports this module
    from resonant.brain import voice as voice_mod
    from resonant.brain.voice import Day, Facts
    from resonant.extensions import voice_facts

    if b.day is None:
        return None
    going = [i for i in b.workers if not i.changed]
    found = voice_facts.worker_facts(conn, listed, going, b.roll)
    if found is None:
        return None
    reminders = tuple(
        (t, voice_mod.fact_text(r, voice_mod.REMINDER_MAX) or "reminder")
        for t, r in b.reminders[:REMINDERS_SHOWN]
    )
    if not (found.items or reminders or b.running or b.waiting or b.queued):
        return None
    tail = [*found.tail]
    if (health := _health_line(b)) is not None:
        tail.append(health)
    tail += [item_line(i) for i in listed if not is_worker_item(i)]
    if (more := len(b.reminders) - REMINDERS_SHOWN) > 0:
        tail.append(f"+{more} more reminders")
    last = text.rsplit("\n", 1)[-1]
    if _MORE.fullmatch(last):
        tail.append(last)
    return Facts(
        "briefing",
        found.items,
        going=found.going,
        day=Day(b.day.strftime("%A"), reminders, b.running, len(b.waiting), b.queued),
        approvals=b.approvals,
        tail=tuple(tail),
    )


# --- sending -------------------------------------------------------------------------------


def _paused(conn: sqlite3.Connection) -> bool:
    try:
        return killswitch.is_paused(conn)
    except killswitch.PausedStateUnknownError:
        return True  # fail closed, like the scheduler


async def send_briefing(
    conn: sqlite3.Connection,
    channel: Channel,
    owner: str,
    *,
    tz: str,
    now: datetime,
    due: datetime | None = None,
    manual: bool = False,
    settings: Settings | None = None,
    voice: Voice | None = None,
) -> dict[str, Any]:
    """Build and send the briefing for the local date of ``due`` (else ``now``), deduped
    per date (``briefing:<date>``). ``manual`` (on demand): keyed
    ``briefing:manual:<date>:<HHMM>`` instead, so it never uses up the scheduled one and
    sends at most once a minute. Returns ``{sent, date, sections, chars}``; never the text.
    ``sent`` means handed to the channel: a repeat key is dropped there. Paused: not sent.
    ``settings``: refresh the Workers rollup's derived items first (``pulse.stall_after``).
    ``voice`` (PER-273): Claude writes the prose (:func:`briefing_facts`); the code-built text
    is the fallback."""
    zone = ZoneInfo(tz)
    local_now = (due or now).astimezone(zone)
    day = local_now.date()
    if _paused(conn):
        return {"sent": False, "date": day.isoformat(), "skipped": "paused"}
    stall_after = rollup.STALL_AFTER
    if settings is not None:
        refresh_rollup(conn, settings, now)
        stall_after = settings.pulse.stall
    b = gather(conn, owner=owner, now=now, day=day, tz=zone, stall_after=stall_after)
    key = manual_dedupe_key(local_now) if manual else dedupe_key(day)
    prune_inflight(conn, now)
    built: dict[str, Any] = {}

    def build() -> tuple[str, list[pulse_store.Item]]:
        text, listed = render_full(b)
        built["sections"] = sections(b)
        return text, listed

    # Stored intent first: a rerun of this key resends the same text and marks only the
    # items it listed (a new item stays pending).
    async def rewrite(text: str, listed: list[pulse_store.Item]) -> str:
        assert voice is not None
        facts = briefing_facts(conn, b, listed, text)
        return text if facts is None else (await voice.write(facts, text)).text

    await deliver(conn, channel, owner, key, now, build, rewrite=rewrite if voice else None)
    rec: Any = kv_get(conn, "pulse.inflight:" + key)
    text = str(cast(dict[str, Any], rec)["text"]) if isinstance(rec, dict) else ""
    return {
        "sent": True,
        "date": day.isoformat(),
        "sections": built.get("sections", []),
        "chars": len(text),
    }


async def run_briefing_now(deps: CoreDeps, *, now: datetime | None = None) -> dict[str, Any]:
    """Send the briefing on demand (P3-14's ``/ext run core/briefing``), keyed per minute
    (``manual``). Sent even when ``briefing.enabled`` is false; nothing while paused, and
    ``{sent: False, skipped: "unreachable"}`` with no channel or owner."""
    if deps.channel is None or deps.owner is None:
        return {"sent": False, "skipped": "unreachable"}
    return await send_briefing(
        deps.conn,
        deps.channel,
        deps.owner,
        tz=deps.settings.timezone,
        now=now or utcnow(),
        manual=True,
        settings=deps.settings,
        voice=deps.components.voice,
    )


def _due(args: dict[str, Any]) -> datetime | None:
    value = args.get("due_at")
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.utcoffset() is not None else None


def briefing_job(deps: CoreDeps, *, clock: Callable[[], datetime] = utcnow) -> CoreJob | None:
    """The ``CORE_JOBS`` factory. None when disabled, with no channel, or with no owner."""
    from resonant.extensions.scheduler import CoreJob

    cfg = deps.settings.briefing
    channel, owner = deps.channel, deps.owner
    if not cfg.enabled:
        return None
    if channel is None or owner is None:
        log.info("briefing not scheduled", extra={"channel": channel is not None})
        return None
    tz = deps.settings.timezone
    conn, settings = deps.conn, deps.settings

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        return await send_briefing(
            conn,
            channel,
            owner,
            tz=tz,
            now=clock(),
            due=_due(args),
            settings=settings,
            voice=deps.components.voice,
        )

    return CoreJob(
        id=JOB_ID,
        schedule=schedule(cfg),
        run=run,
        catchup=CATCHUP,
        timeout_s=TIMEOUT_S,
    )


__all__ = [
    "CATCHUP",
    "JOB_ID",
    "MAX_CHARS",
    "NOTHING",
    "NOTHING_LINES",
    "Briefing",
    "briefing_facts",
    "briefing_job",
    "dedupe_key",
    "gather",
    "manual_dedupe_key",
    "nothing_line",
    "render",
    "render_full",
    "run_briefing_now",
    "sections",
    "send_briefing",
]

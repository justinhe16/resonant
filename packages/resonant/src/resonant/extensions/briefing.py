"""The core daily briefing, ``core/briefing`` (D12): one short message to the owner a day.

A core job (``scheduler.CORE_JOBS``): L0, no model, run inline by the scheduler at
``briefing.at`` (default 08:00) in ``settings.timezone``, daily. ``briefing.enabled: false``,
no iMessage channel, or no ``principals.yaml`` owner: not scheduled. Catch-up is 3h: a
daemon that was down at 08:00 and comes back by 11:00 sends it once.

The text is built by code from builtin reads, one line per section, and a section with
nothing to say is left out (``Nothing on today.`` when all are):

- ``⏰ 2 reminders: 9:30am stretch, 3:00pm call mom``: the owner's pending reminders due
  today (``list_reminders``), in local time.
- ``▶ 1 running, 2 waiting (approval)``: open work from the ``projects`` query (states and
  wait reasons only, never titles or message bodies). Reminders are left out (above).
- ``✋ 1 approval pending (A7)``: ``approvals.pending_for(owner)``.
- ``⚠ model, imessage degraded``: degraded checks in ``health_snapshot``.
- ``github: 3 open PRs, ...``: the text of today's latest ``ext.github.digest`` event
  (P3-9 schedules ``digest`` at 07:50), sanitized and at most 200 chars. Left out when there
  is none today or ``github`` isn't loaded.

The whole message is at most 600 characters, well inside one iMessage chunk. It is sent with
``dedupe_key=briefing:<local date>``, so a restart, a catch-up or an on-demand run never
sends a second one that day. Skipped while paused.

On demand (P3-14's ``/ext run core/briefing``): call the core job's ``run`` (it is the
``ScheduledJob.call`` of the scheduler's ``core/briefing`` job) with ``{}`` (now) or
``{"due_at": iso}``, or ``send_briefing`` directly.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from resonant import killswitch
from resonant.executor import CallContext
from resonant.gate.codes import clean_external
from resonant.monitor import health_snapshot
from resonant.store.approvals import pending_for
from resonant.store.db import iso, utcnow
from resonant.tools.projects import clip, projects
from resonant.tools.reminders import list_reminders
from resonant.tools.when import clock_time

if TYPE_CHECKING:
    from resonant.extensions.host import ExtensionHost
    from resonant.extensions.scheduler import CoreDeps, CoreJob
    from resonant.gateway.channel import Channel

log = logging.getLogger(__name__)

JOB_ID = "briefing"
CATCHUP = timedelta(hours=3)
TIMEOUT_S = 60.0
MAX_CHARS = 600
NOTHING = "Nothing on today."
GITHUB = "github"
DIGEST_TYPE = "ext.github.digest"
DIGEST_SOURCE = "ext:github"
DIGEST_MAX = 200
REMINDERS_SHOWN = 3
REMINDER_TEXT_MAX = 30
CODES_SHOWN = 5
OPEN_STATES = ("running", "waiting", "queued")


def dedupe_key(day: date) -> str:
    return f"briefing:{day.isoformat()}"


# --- the content: pure data, then a pure render -----------------------------------------


@dataclass(frozen=True)
class Briefing:
    reminders: tuple[tuple[str, str], ...] = ()  # (local clock time, text), soonest first
    running: int = 0
    waiting: tuple[str, ...] = ()  # one wait reason per waiting task
    queued: int = 0
    approvals: tuple[str, ...] = ()  # one code per pending approval ("" when it has none)
    degraded: tuple[str, ...] = ()  # health check names
    github: str | None = None  # the sanitized digest line


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


def _approvals_line(b: Briefing) -> str | None:
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


def render(b: Briefing) -> str:
    """The message: one line per non-empty section, at most ``MAX_CHARS``."""
    lines = [
        line
        for line in (
            _reminders_line(b),
            _work_line(b),
            _approvals_line(b),
            _health_line(b),
            b.github,
        )
        if line
    ]
    text = "\n".join(lines) or NOTHING
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1].rstrip() + "…"


def sections(b: Briefing) -> list[str]:
    """Names of the non-empty sections (the run's result; no content)."""
    named = {
        "reminders": _reminders_line(b),
        "work": _work_line(b),
        "approvals": _approvals_line(b),
        "health": _health_line(b),
        "github": b.github,
    }
    return [name for name, line in named.items() if line]


# --- reads ---------------------------------------------------------------------------------


def _day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
    return start, end


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


def github_line(conn: sqlite3.Connection, day: date, tz: ZoneInfo) -> str | None:
    """The text of the latest ``ext.github.digest`` event that occurred on ``day`` (local),
    sanitized and at most ``DIGEST_MAX`` chars; None if there is none."""
    start, end = _day_bounds(day, tz)
    row = conn.execute(
        """SELECT payload FROM events WHERE type = ? AND source = ?
               AND occurred_at >= ? AND occurred_at < ?
           ORDER BY occurred_at DESC, received_at DESC LIMIT 1""",
        (DIGEST_TYPE, DIGEST_SOURCE, iso(start), iso(end)),
    ).fetchone()
    if row is None:
        return None
    try:
        payload: object = json.loads(row["payload"])
    except ValueError:
        return None
    text = cast(dict[str, Any], payload).get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        return None
    line = clean_external(text.replace("✗", "x"))
    if not line:
        return None
    if not line.startswith(f"{GITHUB}:"):
        line = f"{GITHUB}: {line}"
    return line if len(line) <= DIGEST_MAX else line[: DIGEST_MAX - 3].rstrip() + "..."


def gather(
    conn: sqlite3.Connection,
    *,
    owner: str,
    now: datetime,
    day: date,
    tz: ZoneInfo,
    github: bool,
) -> Briefing:
    """Every section's data for local ``day``. ``github``: the extension is loaded."""
    running, waiting, queued = _work(conn)
    return Briefing(
        reminders=_reminders(conn, owner, now, day, tz),
        running=running,
        waiting=waiting,
        queued=queued,
        approvals=tuple(a.code or "" for a in pending_for(conn, owner, now)),
        degraded=_degraded(conn),
        github=github_line(conn, day, tz) if github else None,
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
    github: bool = False,
) -> dict[str, Any]:
    """Build and send the briefing for the local date of ``due`` (else ``now``), deduped
    per date. Returns ``{sent, date, sections, chars}``; never the text. Paused: not sent."""
    zone = ZoneInfo(tz)
    day = (due or now).astimezone(zone).date()
    if _paused(conn):
        return {"sent": False, "date": day.isoformat(), "skipped": "paused"}
    b = gather(conn, owner=owner, now=now, day=day, tz=zone, github=github)
    text = render(b)
    await channel.send(owner, text, dedupe_key=dedupe_key(day))
    return {"sent": True, "date": day.isoformat(), "sections": sections(b), "chars": len(text)}


def _github_loaded(host: ExtensionHost | None) -> bool:
    return host is not None and GITHUB in host.loaded


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
    conn, host = deps.conn, deps.components.extensions
    at = cfg.local_time

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        return await send_briefing(
            conn,
            channel,
            owner,
            tz=tz,
            now=clock(),
            due=_due(args),
            github=_github_loaded(host),
        )

    return CoreJob(
        id=JOB_ID,
        schedule=f"{at.minute} {at.hour} * * *",
        run=run,
        catchup=CATCHUP,
        timeout_s=TIMEOUT_S,
    )


__all__ = [
    "CATCHUP",
    "JOB_ID",
    "MAX_CHARS",
    "NOTHING",
    "Briefing",
    "briefing_job",
    "dedupe_key",
    "gather",
    "github_line",
    "render",
    "sections",
    "send_briefing",
]

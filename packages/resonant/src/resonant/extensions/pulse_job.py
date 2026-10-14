"""The hourly pulse, ``core/pulse`` (docs/pulse-plan.md D4): one "btw" message an hour at most.

A core job (``scheduler.CORE_JOBS``): L0, no model, run inline at ``0 * * * *`` in
``settings.timezone`` with a 50 minute catch-up. ``pulse.enabled: false``, no iMessage channel
or no ``principals.yaml`` owner: not scheduled. Each run:

1. prunes old items and refreshes the **core items** (source ``core``) from the store:
   ``health/<check>`` (a degraded check, ``⚠ imessage degraded``, then ``✓ imessage recovered``
   once, unless nobody heard it degrade) and ``approval/<code>`` (a pending approval older
   than ``pulse.remention_after``: ``✋ A7 still waiting: <label>``, said once; it is cleared
   without a message when it is resolved, the owner has seen that);
2. unless it is the briefing's hour (the briefing covers it), inside the quiet hours, or
   paused: lists the untold ``urgent`` and ``normal`` items of sources that aren't muted
   (urgent ones only get here when they were held), newest first, urgent first, and sends
   ONE message to the owner, ``dedupe_key=pulse:<local date>:<HH>``; then marks those items
   told. Nothing pending: nothing sent.

The message is at most ``MAX_CHARS`` (1,000). Items that don't fit are not marked told and ride
the next pulse; the message ends ``+N more``. Extension items are rendered as
``notify.prefix(source)`` (the code-built ``[name]``, anti-spoofing) + mark + the stored,
already cleaned title; core items have no prefix. The decisions use the job's due time (a
catch-up run at 06:20 for 05:00 is still quiet), the reads use the clock.

On demand: ``run_pulse_now(deps)`` (P35-5's ``/pulse now``, ``/ext run core/pulse``) is keyed
``pulse:manual:<date>:<HHMM>`` and ignores the briefing hour and the quiet hours, but not
pause. ``preview(conn, now)`` is the pure text of what the next pulse would say.
Results carry counts and reasons, never text.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from resonant import killswitch
from resonant.executor import CallContext
from resonant.extensions import notify
from resonant.monitor import health_snapshot
from resonant.store import pulse as store
from resonant.store.approvals import pending_for
from resonant.store.db import atomic, kv_get, kv_set, utcnow
from resonant.tools.projects import clip

if TYPE_CHECKING:
    from resonant.config import Settings
    from resonant.extensions.scheduler import CoreDeps, CoreJob
    from resonant.gateway.channel import Channel

log = logging.getLogger(__name__)

JOB_ID = "pulse"
SCHEDULE = "0 * * * *"
CATCHUP = timedelta(minutes=50)
TIMEOUT_S = 60.0
MAX_CHARS = 1000
LINE_MAX = 200
HEADER = "btw:"
CORE = "core"
LAST_KEY = "pulse.last_key"  # kv: the dedupe key of the last pulse that was sent and told
ITEM_TTL_S = 24 * 3600
LABEL_MAX = 60
_BAD_KEY_CHARS = re.compile(r"[^a-z0-9/._:-]")


def schedule() -> str:
    """The pulse's cron, in ``settings.timezone``: on the hour."""
    return SCHEDULE


def dedupe_key(local: datetime) -> str:
    return f"pulse:{local.date().isoformat()}:{local:%H}"


def manual_dedupe_key(local: datetime) -> str:
    return f"pulse:manual:{local.date().isoformat()}:{local:%H%M}"


# --- rendering: pure -------------------------------------------------------------------------


def item_line(item: store.Item, max_chars: int = LINE_MAX) -> str:
    """One item as a line: ``[dori] ✓ Dori recovered``, ``[trader] 🔴 PnL ...``; a core item
    has no ``[name]`` prefix (its title carries its own symbol)."""
    if item.source == CORE:
        return clip(item.title, max_chars)
    mark = "✓ " if item.cleared else "🔴 " if item.urgency == "urgent" else ""
    # Stored titles are already cleaned (clean_text is not idempotent): never re-clean.
    head = notify.prefix(item.source) + " "
    return head + clip(mark + item.title, max(1, max_chars - len(head)))


def _order(items: Sequence[store.Item]) -> list[store.Item]:
    """Urgent first, then normal, then low; newest first within each."""
    rank = {"urgent": 0, "normal": 1, "low": 2}
    by_age = sorted(items, key=lambda i: (i.source, i.key))
    by_age.sort(key=lambda i: i.updated_at, reverse=True)
    by_age.sort(key=lambda i: rank[i.urgency])
    return by_age


def fit_lines(lines: Sequence[str], room: int) -> tuple[list[str], int]:
    """The leading ``lines`` that fit in ``room`` characters (each on its own line, after a
    newline) with room left for a ``+N more`` line if some are dropped. Returns the lines
    shown and how many are left out."""
    shown: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        left = len(lines) - i - 1
        reserve = len(f"\n+{left} more") if left else 0
        if used + 1 + len(line) + reserve > room:
            break
        shown.append(line)
        used += 1 + len(line)
    return shown, len(lines) - len(shown)


def compose(items: Sequence[store.Item]) -> tuple[str, list[store.Item]] | None:
    """The pulse text and the items it shows (the rest wait); None with no items."""
    ordered = _order(items)
    if not ordered:
        return None
    lines = [item_line(i) for i in ordered]
    shown, more = fit_lines(lines, MAX_CHARS - len(HEADER))
    if not shown:  # one line is at most LINE_MAX, so this is only a tiny-cap safety net
        shown, more = lines[:1], len(lines) - 1
    parts = [HEADER, *shown]
    if more:
        parts.append(f"+{more} more")
    return "\n".join(parts), ordered[: len(shown)]


def _pending(conn: sqlite3.Connection, now: datetime) -> list[store.Item]:
    """Untold urgent and normal items of sources that aren't muted."""
    items = store.pending(conn, ("urgent", "normal"), now)
    muted = {s for s in {i.source for i in items} if s != CORE and notify.muted_until(conn, s, now)}
    return [i for i in items if i.source not in muted]


def preview(conn: sqlite3.Connection, now: datetime | None = None) -> str | None:
    """What the next pulse would say, or None if it has nothing. Read-only (core items are
    only refreshed by a run)."""
    composed = compose(_pending(conn, now or utcnow()))
    return None if composed is None else composed[0]


# --- core items ------------------------------------------------------------------------------


def _key(prefix: str, name: str) -> str:
    return prefix + "/" + _BAD_KEY_CHARS.sub("-", name.lower())[: store.KEY_MAX - len(prefix) - 1]


def _sync(
    conn: sqlite3.Connection,
    now: datetime,
    prefix: str,
    live: dict[str, str],
    gone: Callable[[store.Item], str | None],
) -> None:
    """Make the open core items under ``prefix/`` match ``live`` (key -> title).

    A live key is reported (normal, constant state: said once). An open item no longer live
    is cleared with the title ``gone(item)`` returns, or silently (cleared and marked told,
    no message) when that is None or the owner never heard of the item.
    """
    for key, title in live.items():
        store.upsert(conn, CORE, key, title, "normal", {"open": True}, False, ITEM_TTL_S, now)
    for item in store.open_items(conn, now):
        if item.source != CORE or not item.key.startswith(prefix + "/") or item.key in live:
            continue
        title = gone(item)
        _change, cleared = store.upsert(
            conn,
            CORE,
            item.key,
            title or item.title,
            "normal",
            {"open": False},
            True,
            ITEM_TTL_S,
            now,
        )
        if title is None or item.changed:
            store.mark_told(conn, [cleared], now)


def refresh_core_items(
    conn: sqlite3.Connection, owner: str, settings: Settings, now: datetime
) -> None:
    """Refresh ``core/health/<check>`` and ``core/approval/<code>`` from the store."""
    degraded = {
        name: f"⚠ {notify.clean_text(name)} degraded"
        for name, s in health_snapshot(conn).items()
        if s["state"] == "degraded"
    }
    _sync(
        conn,
        now,
        "health",
        {_key("health", n): t for n, t in degraded.items()},
        lambda item: "✓ " + item.title.removeprefix("⚠ ").removesuffix(" degraded") + " recovered",
    )
    waiting: dict[str, str] = {}
    for a in pending_for(conn, owner, now):
        if now - a.created_at < settings.pulse.remention:
            continue
        code = notify.clean_text(a.code or a.id[:8])
        label = clip(notify.clean_text(a.summary), LABEL_MAX)
        waiting[_key("approval", a.code or a.id)] = f"✋ {code} still waiting: {label}"
    _sync(conn, now, "approval", waiting, lambda item: None)


# --- sending ---------------------------------------------------------------------------------


def _paused(conn: sqlite3.Connection) -> bool:
    try:
        return killswitch.is_paused(conn)
    except killswitch.PausedStateUnknownError:
        return True  # fail closed, like the scheduler


async def send_pulse(
    conn: sqlite3.Connection,
    channel: Channel,
    owner: str,
    *,
    settings: Settings,
    now: datetime,
    due: datetime | None = None,
    manual: bool = False,
) -> dict[str, Any]:
    """Prune, refresh the core items and, if it is time, send the pulse.

    Returns ``{sent, count, skipped?}`` (``skipped``: ``paused``, ``briefing``, ``quiet``,
    ``nothing``, ``duplicate``); never the text. ``manual`` ignores the briefing hour and the
    quiet hours (not pause) and uses the per-minute manual key.
    """
    local = (due or now).astimezone(ZoneInfo(settings.timezone))
    store.prune(conn, now)
    refresh_core_items(conn, owner, settings, now)
    if _paused(conn):
        return {"sent": False, "count": 0, "skipped": "paused"}
    if not manual:
        if settings.briefing.enabled and local.hour == settings.briefing.local_time.hour:
            return {"sent": False, "count": 0, "skipped": "briefing"}
        if settings.pulse.in_quiet(local.time()):
            return {"sent": False, "count": 0, "skipped": "quiet"}
    key = manual_dedupe_key(local) if manual else dedupe_key(local)
    if kv_get(conn, LAST_KEY) == key:
        # Already sent and told this hour (a restart or a catch-up): what is pending now
        # arrived since, and rides the next pulse.
        return {"sent": False, "count": 0, "skipped": "duplicate"}
    composed = compose(_pending(conn, now))
    if composed is None:
        return {"sent": False, "count": 0, "skipped": "nothing"}
    text, shown = composed
    await channel.send(owner, text, dedupe_key=key)
    with atomic(conn):
        store.mark_told(conn, shown, now)
        kv_set(conn, LAST_KEY, key)
    return {"sent": True, "count": len(shown)}


async def run_pulse_now(deps: CoreDeps, *, now: datetime | None = None) -> dict[str, Any]:
    """Send the pulse on demand (``/pulse now``, ``/ext run core/pulse``), keyed per minute.
    Sent even when ``pulse.enabled`` is false and in the quiet hours; nothing while paused;
    ``{sent: False, skipped: "unreachable"}`` with no channel or owner."""
    if deps.channel is None or deps.owner is None:
        return {"sent": False, "count": 0, "skipped": "unreachable"}
    return await send_pulse(
        deps.conn,
        deps.channel,
        deps.owner,
        settings=deps.settings,
        now=now or utcnow(),
        manual=True,
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


def pulse_job(deps: CoreDeps, *, clock: Callable[[], datetime] = utcnow) -> CoreJob | None:
    """The ``CORE_JOBS`` factory. None when disabled, with no channel, or with no owner."""
    from resonant.extensions.scheduler import CoreJob

    channel, owner = deps.channel, deps.owner
    if not deps.settings.pulse.enabled:
        return None
    if channel is None or owner is None:
        log.info("pulse not scheduled", extra={"channel": channel is not None})
        return None
    conn, settings = deps.conn, deps.settings

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        return await send_pulse(
            conn, channel, owner, settings=settings, now=clock(), due=_due(args)
        )

    return CoreJob(id=JOB_ID, schedule=SCHEDULE, run=run, catchup=CATCHUP, timeout_s=TIMEOUT_S)


__all__ = [
    "CATCHUP",
    "JOB_ID",
    "MAX_CHARS",
    "compose",
    "dedupe_key",
    "fit_lines",
    "item_line",
    "manual_dedupe_key",
    "preview",
    "pulse_job",
    "refresh_core_items",
    "run_pulse_now",
    "schedule",
    "send_pulse",
]

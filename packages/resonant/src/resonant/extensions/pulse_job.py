"""The hourly pulse, ``core/pulse`` (docs/pulse-plan.md D4): one "btw" message an hour at most.

A core job (``scheduler.CORE_JOBS``): L0, no model, run inline at ``0 * * * *`` in
``settings.timezone`` with a 50 minute catch-up. ``pulse.enabled: false``, no iMessage channel
or no ``principals.yaml`` owner: not scheduled. Each run:

1. prunes old items and refreshes the **core items** (source ``core``) from the store:
   ``approval/<code>`` (a pending approval older than ``pulse.remention_after``:
   ``✋ A7 still waiting: <label>``, said once; it is cleared without a message when it is
   resolved, the owner has seen that). Health stays with the HealthMonitor, which already
   alerts on degrade and recovery;
2. unless it is the briefing's hour (the briefing covers it), inside the quiet hours, or
   paused: lists the untold ``urgent`` and ``normal`` items of sources that aren't muted
   (urgent ones only get here when they were held), oldest first, urgent first, and sends
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

Exactly once (``deliver``, shared with the briefing): before ``channel.send`` the exact text and
the (source, key, fingerprint) of the items in it are stored in kv ``pulse.inflight:<dedupe
key>``. A rerun of the same key (a crash between send and mark, a catch-up, a restart) never
recomposes: it resends the stored text (the channel drops it if it went out) and marks exactly
those items told, compare-and-set, so an item that arrived since stays pending. A record
that is done makes the rerun a ``duplicate``. A new key first finishes any other unsent record
the same way (a run that raised after its send is not rerun: the scheduler consumes its due
time). Records are pruned after 2 days.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from resonant import killswitch
from resonant.executor import CallContext
from resonant.extensions import notify
from resonant.extensions.pulse import render_item
from resonant.store import pulse as store
from resonant.store.approvals import pending_for
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow
from resonant.tools.projects import clip
from resonant.tools.when import format_due

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
INFLIGHT = "pulse.inflight:"  # kv: one record per dedupe key, see deliver()
INFLIGHT_KEEP = timedelta(days=2)
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
    mark = "✓" if item.cleared and item.source != CORE else "🔴" if item.urgency == "urgent" else ""
    return render_item(item, mark, max_chars)


def _order(items: Sequence[store.Item]) -> list[store.Item]:
    """Urgent first, then normal, then low; oldest first within each, so an overflow can't
    starve old items."""
    rank = {"urgent": 0, "normal": 1, "low": 2}
    by_age = sorted(items, key=lambda i: (i.source, i.key))
    by_age.sort(key=lambda i: i.updated_at)
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
        assert len(shown[0]) <= MAX_CHARS - len(HEADER)
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


def open_summary(conn: sqlite3.Connection, now: datetime, tz: str) -> str | None:
    """``Open: github 1, example 2 (muted until 3:00pm)``: open (uncleared, unexpired) items
    per source, core first; None with none. Read-only."""
    counts: dict[str, int] = {}
    for item in store.open_items(conn, now):
        counts[item.source] = counts.get(item.source, 0) + 1
    if not counts:
        return None
    parts: list[str] = []
    for source in sorted(counts, key=lambda s: (s != CORE, s)):
        part = f"{source} {counts[source]}"
        until = None if source == CORE else notify.muted_until(conn, source, now)
        if until is not None:
            part += f" (muted until {format_due(until, now=now, tz=tz)})"
        parts.append(part)
    return "Open: " + ", ".join(parts)


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
    """Refresh ``core/approval/<code>`` from the store."""
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


def prune_inflight(conn: sqlite3.Connection, now: datetime) -> None:
    """Drop delivery records (done, or never finished) older than ``INFLIGHT_KEEP``."""
    rows = conn.execute("SELECT key FROM kv WHERE key LIKE ?", (INFLIGHT + "%",)).fetchall()
    with atomic(conn):
        for (k,) in rows:
            rec: Any = kv_get(conn, k)
            if not isinstance(rec, dict):
                continue
            at = str(cast(dict[str, Any], rec).get("at", ""))
            if at and datetime.fromisoformat(at) < now - INFLIGHT_KEEP:
                conn.execute("DELETE FROM kv WHERE key = ?", (k,))


async def deliver(
    conn: sqlite3.Connection,
    channel: Channel,
    owner: str,
    key: str,
    now: datetime,
    build: Callable[[], tuple[str, list[store.Item]] | None],
) -> tuple[str, int]:
    """Send one message to ``owner`` exactly once for dedupe key ``key``.

    ``build`` returns the text and the items it lists (None: nothing to say). The intent is
    stored before the send; a rerun resends the stored text and marks exactly the stored
    items told (see the module docstring). Returns ``("sent", n)``, ``("nothing", 0)`` or
    ``("duplicate", 0)``.
    """
    rkey = INFLIGHT + key
    raw: Any = kv_get(conn, rkey)
    rec = cast(dict[str, Any], raw) if isinstance(raw, dict) else None
    if rec is None:
        await _finish_unsent(conn, channel, owner, now, except_key=rkey)
        built = build()
        if built is None:
            return "nothing", 0
        text, listed = built
        rec = {
            "text": text,
            "items": [[i.source, i.key, i.fingerprint] for i in listed],
            "sent": False,
            "at": iso(now),
        }
        with atomic(conn):
            kv_set(conn, rkey, rec)
    elif rec.get("sent"):
        return "duplicate", 0
    return "sent", await _send_recorded(conn, channel, owner, key, rec, now)


async def _send_recorded(
    conn: sqlite3.Connection,
    channel: Channel,
    owner: str,
    key: str,
    rec: dict[str, Any],
    now: datetime,
) -> int:
    """Send the stored text under its own dedupe key and mark exactly its items told."""
    recorded = cast(list[list[str]], rec["items"])
    await channel.send(owner, str(rec["text"]), dedupe_key=key)
    with atomic(conn):
        for source, ikey, fp in recorded:
            item = store.get(conn, source, ikey)
            if item is not None:
                store.mark_told(conn, [replace(item, fingerprint=fp)], now)
        kv_set(conn, INFLIGHT + key, {**rec, "sent": True})
    return len(recorded)


async def _finish_unsent(
    conn: sqlite3.Connection, channel: Channel, owner: str, now: datetime, *, except_key: str
) -> None:
    """Finish the delivery records of other keys that never got marked: a run that raised
    after its send (the scheduler consumes that due time, so no catch-up reruns it). Without
    this the next message would compose the same untold items again under a new key. The
    channel drops the resend if it went out. Best effort: a failure here is logged."""
    rows = conn.execute("SELECT key FROM kv WHERE key LIKE ?", (INFLIGHT + "%",)).fetchall()
    for (k,) in rows:
        if k == except_key:
            continue
        raw: Any = kv_get(conn, k)
        if not isinstance(raw, dict) or cast(dict[str, Any], raw).get("sent"):
            continue
        rec = cast(dict[str, Any], raw)
        try:
            await _send_recorded(conn, channel, owner, k.removeprefix(INFLIGHT), rec, now)
        except Exception:
            log.warning("pulse: could not finish an earlier delivery", extra={"key": k})


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
    prune_inflight(conn, now)
    refresh_core_items(conn, owner, settings, now)
    if _paused(conn):
        return {"sent": False, "count": 0, "skipped": "paused"}
    if not manual:
        if settings.briefing.enabled and local.hour == settings.briefing.local_time.hour:
            return {"sent": False, "count": 0, "skipped": "briefing"}
        if settings.pulse.in_quiet(local.time()):
            return {"sent": False, "count": 0, "skipped": "quiet"}
    key = manual_dedupe_key(local) if manual else dedupe_key(local)
    status, count = await deliver(
        conn, channel, owner, key, now, lambda: compose(_pending(conn, now))
    )
    if status == "sent":
        return {"sent": True, "count": count}
    return {"sent": False, "count": 0, "skipped": status}


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
    "deliver",
    "fit_lines",
    "item_line",
    "manual_dedupe_key",
    "open_summary",
    "preview",
    "pulse_job",
    "refresh_core_items",
    "run_pulse_now",
    "schedule",
    "send_pulse",
]

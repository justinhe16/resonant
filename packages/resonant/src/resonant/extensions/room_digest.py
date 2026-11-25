"""The room digest (PER-302): the daily briefing for a team, posted to a room.

``briefing.audience: room:<key>`` turns ``core/briefing`` into this: once a day (``briefing.at``
on ``briefing.days``, in ``settings.timezone``), ONE code-built post to
``channels.slack.rooms.<key>`` through ``ChannelHub.post_room``, instead of the owner's
briefing. ``audience: owner`` (the default) is the personal briefing, unchanged.

The digest carries no owner-private sections (no reminders, notes, facts, approval codes or
message bodies). In order, each left out when it has nothing to say:

- each open report of the extensions listed in ``briefing.reports`` (an allowlist: an
  unlisted extension never reaches the room): its title, then up to ``REPORT_LINES`` of its
  ``state.digest`` lines (strings the extension renders itself; cleaned like titles, at most
  ``LINE_MAX`` characters each), then ``+N more``;
- ``Runs: 2 failed (R12, R14) · 1 waiting (R15: approval) · 1 running (R16)``: worker runs that
  failed in the 24 hours before the post, and the ones still waiting or running (R-codes and
  wait reasons only);
- ``✋ 2 approvals waiting (oldest 40m)``: open gated approvals, counted (codes stay with
  the approvers).

Nothing at all: ``Quiet day.``. Code-built, no voice.

**Blocks** (PER-332). A report may also carry ``state.blocks``: Slack Block Kit blocks the
extension builds itself (a header, sections with fields, a table, ...). When at least one
listed report has valid ones (``slack.blocks.valid_blocks``: a list of 1..50 objects with a
string ``type``, no ``<!channel>``-style mention), the post also carries blocks
(:func:`render_blocks`): each report's own blocks, or a plain-text section of its title and
lines when it has none, then a section with the runs and approvals lines, a divider between
parts. Over 50 blocks in all: no blocks. The text above is always posted with them as the
notification fallback, and alone when Slack rejects the blocks (the adapter's fallback).

**Once a day**: keyed ``digest:<local date>`` through ``pulse_job.deliver`` with its own
records (``digest.inflight:``), so a restart, a catch-up or a rerun never posts twice; recorded
as a ``briefing`` system text of ``room:<key>``. It marks nothing told: the owner's pulse items
are untouched. Skipped while paused (unknown counts as paused).

**The room is gone** (not configured, the bot removed: ``not_in_channel``, Slack down): the
post is logged (no body) and the job's result is ``skipped: failed``; the Slack adapter notes
the room's last error in its health ``detail`` (PER-303); and the owner gets ONE DM per
outage (kv ``digest.room_down``), cleared by the next good post.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from resonant import killswitch
from resonant.extensions.notify import clean_text
from resonant.extensions.pulse_job import deliver, prune_inflight
from resonant.gateway.channel import ChannelError
from resonant.gateway.slack.blocks import MAX_BLOCKS, SECTION_MAX, valid_blocks
from resonant.store import approvals as approvals_store
from resonant.store import pulse as pulse_store
from resonant.store import runs as runs_store
from resonant.store.db import atomic, iso, kv_get, kv_set
from resonant.tools.projects import clip

if TYPE_CHECKING:
    from resonant.config import Settings
    from resonant.gateway.channel import Channel
    from resonant.gateway.hub import RoomPosting

log = logging.getLogger(__name__)

INFLIGHT = "digest.inflight:"  # kv: the digest's delivery records (pulse_job.deliver)
DOWN_KEY = "digest.room_down"  # kv: {room, since, reason, told} while posts fail
QUIET = "Quiet day."
MAX_CHARS = 3000
REPORT_LINES = 12
LINE_MAX = 200
CODES_SHOWN = 8
WINDOW = timedelta(hours=24)


def dedupe_key(day: date) -> str:
    return f"digest:{day.isoformat()}"


def manual_dedupe_key(local_now: datetime) -> str:
    """An on-demand digest's key: at most one per local minute, separate from the day's."""
    return f"digest:manual:{local_now.date().isoformat()}:{local_now:%H%M}"


def room_principal(room: str) -> str:
    """Whose system text the digest is: the room's (as the heartbeat's)."""
    return f"room:{room}"


@dataclass(frozen=True)
class Report:
    source: str
    title: str
    lines: tuple[str, ...] = ()
    more: int = 0
    blocks: tuple[dict[str, Any], ...] = ()  # the report's own ``state.blocks`` (PER-332)


@dataclass(frozen=True)
class RoomDigest:
    reports: tuple[Report, ...] = ()
    failed: tuple[str, ...] = ()  # run codes, oldest first
    waiting: tuple[tuple[str, str], ...] = ()  # (code, wait reason)
    running: tuple[str, ...] = ()
    approvals: int = 0
    oldest: timedelta | None = None  # the oldest open approval's age

    @property
    def empty(self) -> bool:
        return not (self.reports or self.failed or self.waiting or self.running or self.approvals)


# --- reads ----------------------------------------------------------------------------------


def digest_lines(state: Any) -> tuple[list[str], int]:
    """A report's ``state.digest`` lines (cleaned, clipped, at most ``REPORT_LINES``) and how
    many more there were. Anything that isn't a list of strings is no lines."""
    if not isinstance(state, dict):
        return [], 0
    raw: object = cast(dict[str, Any], state).get("digest")
    if not isinstance(raw, list):
        return [], 0
    lines = [clip(clean_text(x), LINE_MAX) for x in cast(list[Any], raw) if isinstance(x, str)]
    lines = [x for x in lines if x]
    return lines[:REPORT_LINES], max(0, len(lines) - REPORT_LINES)


def report_blocks(state: Any) -> list[dict[str, Any]]:
    """A report's ``state.blocks`` when valid (``valid_blocks``), else none (logged when the
    key is there but unusable)."""
    if not isinstance(state, dict):
        return []
    raw: object = cast(dict[str, Any], state).get("blocks")
    if raw is None:
        return []
    checked = valid_blocks(raw)
    if checked is None:
        log.warning("digest: a report's blocks are invalid; using its text")
        return []
    return checked


def _reports(conn: sqlite3.Connection, sources: list[str], now: datetime) -> tuple[Report, ...]:
    if not sources:
        return ()
    rank = {s: i for i, s in enumerate(sources) if s != "core"}  # core: the owner's own
    items = [i for i in pulse_store.open_items(conn, now) if i.source in rank]
    items.sort(key=lambda i: (rank[i.source], i.key))
    out: list[Report] = []
    for item in items:
        lines, more = digest_lines(item.state)
        blocks = tuple(report_blocks(item.state))
        out.append(Report(item.source, item.title, tuple(lines), more, blocks))
    return tuple(out)


def gather(conn: sqlite3.Connection, settings: Settings, now: datetime) -> RoomDigest:
    """Every section's data, as of ``now``."""
    active = runs_store.active(conn)
    waiting = tuple((r.code, r.wait_reason or "?") for r in active if r.status == "waiting")
    running = tuple(r.code for r in active if r.status != "waiting")
    failed = tuple(r.code for r in runs_store.ended_since(conn, "failed", now - WINDOW))
    pending = approvals_store.pending_all(conn, now)
    oldest = now - min(a.created_at for a in pending) if pending else None
    return RoomDigest(
        reports=_reports(conn, settings.briefing.reports, now),
        failed=failed,
        waiting=waiting,
        running=running,
        approvals=len(pending),
        oldest=oldest,
    )


# --- the text (pure) --------------------------------------------------------------------------


def _codes(codes: list[str]) -> str:
    shown = ", ".join(codes[:CODES_SHOWN])
    extra = len(codes) - CODES_SHOWN
    return shown + (f", +{extra}" if extra > 0 else "")


def _age(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def runs_line(d: RoomDigest) -> str | None:
    parts: list[str] = []
    if d.failed:
        parts.append(f"{len(d.failed)} failed ({_codes(list(d.failed))})")
    if d.waiting:
        parts.append(f"{len(d.waiting)} waiting ({_codes([f'{c}: {w}' for c, w in d.waiting])})")
    if d.running:
        parts.append(f"{len(d.running)} running ({_codes(list(d.running))})")
    return "Runs: " + " · ".join(parts) if parts else None


def approvals_line(d: RoomDigest) -> str | None:
    if not d.approvals:
        return None
    word = "approval" if d.approvals == 1 else "approvals"
    age = f" (oldest {_age(d.oldest)})" if d.oldest is not None else ""
    return f"✋ {d.approvals} {word} waiting{age}"


def render(d: RoomDigest) -> str:
    """The post: the reports, then runs and approvals; ``Quiet day.`` when all are empty. At
    most ``MAX_CHARS``."""
    if d.empty:
        return QUIET
    lines: list[str] = []
    for r in d.reports:
        lines.append(r.title)
        lines += [f"• {x}" for x in r.lines]
        if r.more:
            lines.append(f"+{r.more} more")
    lines += [x for x in (runs_line(d), approvals_line(d)) if x]
    text = "\n".join(lines)
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1].rstrip() + "…"


def _plain(text: str) -> dict[str, Any]:
    shown = text if len(text) <= SECTION_MAX else text[: SECTION_MAX - 1] + "…"
    return {"type": "section", "text": {"type": "plain_text", "text": shown, "emoji": True}}


def render_blocks(d: RoomDigest) -> list[dict[str, Any]] | None:
    """The post's Block Kit blocks (see "Blocks"), or None: no report has blocks, or they
    come to more than ``MAX_BLOCKS``."""
    if not any(r.blocks for r in d.reports):
        return None
    parts: list[list[dict[str, Any]]] = []
    for r in d.reports:
        if r.blocks:
            parts.append(list(r.blocks))
        else:
            lines = [r.title, *(f"• {x}" for x in r.lines)]
            lines += [f"+{r.more} more"] if r.more else []
            parts.append([_plain("\n".join(lines))])
    core = [x for x in (runs_line(d), approvals_line(d)) if x]
    if core:
        parts.append([_plain("\n".join(core))])
    out: list[dict[str, Any]] = []
    for part in parts:
        if out:
            out.append({"type": "divider"})
        out += part
    if len(out) > MAX_BLOCKS:
        log.warning("digest: %d blocks is over %d; posting the text alone", len(out), MAX_BLOCKS)
        return None
    return out


def sections(d: RoomDigest) -> list[str]:
    """Names of the non-empty sections (the run's result; no content)."""
    named = {
        "reports": bool(d.reports),
        "runs": runs_line(d) is not None,
        "approvals": bool(d.approvals),
    }
    return [k for k, v in named.items() if v]


# --- sending ----------------------------------------------------------------------------------


def _paused(conn: sqlite3.Connection) -> bool:
    try:
        return killswitch.is_paused(conn)
    except killswitch.PausedStateUnknownError:
        return True


async def _room_down(
    conn: sqlite3.Connection,
    channel: Any,
    owner: str | None,
    room: str,
    reason: str,
    now: datetime,
) -> None:
    """Tell the owner once per outage that the digest couldn't be posted."""
    raw: Any = kv_get(conn, DOWN_KEY)
    rec = cast(dict[str, Any], raw) if isinstance(raw, dict) else None
    if rec is None or rec.get("room") != room:
        rec = {"room": room, "since": iso(now), "reason": reason[:200], "told": False}
        with atomic(conn):
            kv_set(conn, DOWN_KEY, rec)
    if rec.get("told") or owner is None:
        return
    text = (
        f"I couldn't post the daily digest to the {room} room ({rec['reason']}). If the room "
        "is gone or I was removed from it, invite me back or change briefing.audience."
    )
    try:
        await channel.send(owner, text, dedupe_key=f"digest-down:{room}:{rec['since']}")
    except ChannelError as e:
        log.warning("digest: owner not told (%s)", type(e).__name__)
        return
    with atomic(conn):
        kv_set(conn, DOWN_KEY, {**rec, "told": True})


def _drop_unsent(conn: sqlite3.Connection, keep: str) -> None:
    """Abandon every unsent digest record but ``keep``'s: a digest is only worth its own day,
    so one that failed is never posted late next to a newer one."""
    rows = conn.execute("SELECT key FROM kv WHERE key LIKE ?", (INFLIGHT + "%",)).fetchall()
    rows = [r for r in rows if not str(r[0]).startswith(INFLIGHT + "digest:manual:")]
    stale: list[str] = []
    for (k,) in rows:
        rec: Any = kv_get(conn, k)
        if k != keep and not (isinstance(rec, dict) and cast(dict[str, Any], rec).get("sent")):
            stale.append(k)
    if stale:
        with atomic(conn):
            for k in stale:
                conn.execute("DELETE FROM kv WHERE key = ?", (k,))


def _room_up(conn: sqlite3.Connection) -> None:
    if kv_get(conn, DOWN_KEY) is not None:
        with atomic(conn):
            conn.execute("DELETE FROM kv WHERE key = ?", (DOWN_KEY,))


async def send_room_digest(
    conn: sqlite3.Connection,
    channel: RoomPosting,
    owner: str | None,
    *,
    settings: Settings,
    now: datetime,
    due: datetime | None = None,
    manual: bool = False,
) -> dict[str, Any]:
    """Post the digest for the local date of ``due`` (else ``now``) to ``briefing.audience``'s
    room, once (``digest:<date>``; ``manual``: ``digest:manual:<date>:<HHMM>``). Returns
    ``{posted, date, sections?, chars?, blocks?, skipped?}`` (``blocks``: how many went with
    it; ``skipped``: ``paused``, ``duplicate``, ``failed``); never the text. ``owner`` gets
    one DM per outage of the room."""
    room = settings.briefing.room
    assert room is not None, "briefing.audience is the owner"
    local = (due or now).astimezone(ZoneInfo(settings.timezone))
    day = local.date().isoformat()
    if _paused(conn):
        return {"posted": False, "date": day, "skipped": "paused"}
    prune_inflight(conn, now, inflight=INFLIGHT)
    key = manual_dedupe_key(local) if manual else dedupe_key(local.date())
    if not manual:  # an on-demand post never touches the day's scheduled record
        _drop_unsent(conn, keep=INFLIGHT + key)
    d = gather(conn, settings, now)
    text = render(d)
    rich = render_blocks(d)

    async def post(body: str, dedupe: str) -> object:
        # blocks go only with this run's own text (a stale record's resend is text alone)
        extra = rich if dedupe == key and body == text else None
        return await channel.post_room(room, body, dedupe_key=dedupe, blocks=extra)

    try:
        status, _ = await deliver(
            conn,
            cast("Channel", channel),  # unused: ``post`` sends
            room_principal(room),
            key,
            now,
            lambda: (text, []),
            outside=lambda _items: bool(d.reports),  # extension text in the reports
            post=post,
            inflight=INFLIGHT,
        )
    except ChannelError as e:
        log.warning("digest: not posted to room %s (%s)", room, type(e).__name__)
        await _room_down(conn, channel, owner, room, str(e), now)
        return {"posted": False, "date": day, "skipped": "failed"}
    _room_up(conn)
    if status == "duplicate":
        return {"posted": False, "date": day, "skipped": "duplicate"}
    out = {"posted": True, "date": day, "sections": sections(d), "chars": len(text)}
    return out | ({"blocks": len(rich)} if rich else {})


__all__ = [
    "DOWN_KEY",
    "INFLIGHT",
    "QUIET",
    "Report",
    "RoomDigest",
    "approvals_line",
    "dedupe_key",
    "digest_lines",
    "gather",
    "manual_dedupe_key",
    "render",
    "render_blocks",
    "report_blocks",
    "room_principal",
    "runs_line",
    "sections",
    "send_room_digest",
]

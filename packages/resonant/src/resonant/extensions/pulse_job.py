"""The hourly pulse, ``core/pulse`` (docs/pulse-plan.md D4): one "btw" message an hour at most.

A core job (``scheduler.CORE_JOBS``): L0, no model, run inline at ``0 * * * *`` in
``settings.timezone`` with a 50 minute catch-up. ``pulse.enabled: false``, no iMessage channel
or no ``principals.yaml`` owner: not scheduled. Each run:

1. prunes old items and refreshes the **core items** (source ``core``) from the store: an
   open worker question is news again after :func:`remention_delay`. Pending approvals are
   not pulse items (PER-261): ``core/remind`` texts each one reminder at half its TTL
   (``runners/approve.py``), and any ``core/approval/*`` item left from before is cleared
   without a message. Health stays with the HealthMonitor, which already alerts on degrade
   and recovery;
2. unless it is the briefing's hour (the briefing covers it), inside the quiet hours, or
   paused: lists the untold ``urgent`` and ``normal`` items of sources that aren't muted
   (urgent ones only get here when they were held), oldest first, urgent first, and sends
   ONE message to the owner, ``dedupe_key=pulse:<local date>:<HH>``; then marks those items
   told. Nothing pending: nothing sent.

Claude workers' items (``core`` / ``run/<code>``, EM-6) and the rollup's derived items
(``stall/``, ``merged/``, ``review/``, GH-2) go in a ``Workers:`` section after the rest, as
the progress rollup's lines (``extensions/rollup.py``: ``Merged: …``, ``Looks like … got
dropped``, ``Waiting on you to merge: …``, ``Still working on: …``, ``Quiet: …``); when
there is news, the workers still running (already told), the github PRs still being worked on
and the stalls already told are listed with them, for status (they never make a pulse send
on their own). Each run first refreshes the derived items
(:func:`refresh_rollup`, ``pulse.stall_after``).

The message is at most ``MAX_CHARS`` (1,000). Items that don't fit are not marked told and ride
the next pulse; the message ends ``+N more``. Extension items are rendered as
``notify.prefix(source)`` (the code-built ``[name]``, anti-spoofing) + mark + the stored,
already cleaned title; core items have no prefix. The decisions use the job's due time (a
catch-up run at 06:20 for 05:00 is still quiet), the reads use the clock.

On demand: ``run_pulse_now(deps)`` (P35-5's ``/pulse now``, ``/ext run core/pulse``) is keyed
``pulse:manual:<date>:<HHMM>`` and ignores the briefing hour and the quiet hours, but not
pause. ``preview(conn, now)`` is the pure text of what the next pulse would say.
Results carry counts and reasons, never text.

In Claude's voice (PER-273, ``brain.voice``, ``brain/voice.py``): when the pulse has workers'
news, Claude writes its prose from the same items as facts and the text is the prose, then
the lines code keeps verbatim (a ⚠ line, a run's side effects, extension items and worker
questions, ``+N more``), then a code-built closing line (``No action needed.`` or ``Needs
you: ...``). No ``btw:`` header or section labels. A pulse of only extension items or
questions, no Claude (down, at its usage limit, over ``brain.voice.daily_cap``, paused) or a
text the guard stops is the code-built pulse above, exactly. ``/pulse`` and ``pulse_preview``
are always the code-built preview.

Exactly once (``deliver``, shared with the briefing): before ``channel.send`` the exact text and
the (source, key, fingerprint) of the items in it are stored in kv ``pulse.inflight:<dedupe
key>``. A rerun of the same key (a crash between send and mark, a catch-up, a restart) never
recomposes: it resends the stored text (the channel drops it if it went out) and marks exactly
those items told, compare-and-set, so an item that arrived since stays pending. A record
that is done makes the rerun a ``duplicate``. A new key first finishes any other unsent record
(under 2 hours old, items unchanged; else it is abandoned) the same way (a run that raised
after its send is not rerun: the scheduler consumes its due time). Records are pruned after 2 days.

What was sent is a system text (PER-274, ``store.system_texts``, kind ``pulse`` or ``briefing``
from the key): recorded in the same transaction as the items' told marks, ``ref`` = the dedupe
key (a rerun or resend records nothing new), the text as sent (Claude's or the code-built one,
without a bare ``No action needed.``), and ``now`` as its time, which is the items' ``told_at``,
so the brains' "Already told" can leave out what a text shown in the transcript said.
``outside`` (decided when the text is built and kept in the record; a record without it
counts as outside): the text lists an item that isn't a code-built core notice (a worker's
item, the rollup's PR lines, a worker question, an extension's item), or a briefing names a
worker; the turn that sees it is tainted, as after a run's outcome.

The heartbeat (PER-303, ``pulse.heartbeat: {room, quiet_hours}``; ``room: null``, the default,
is off and changes nothing): after the owner step, whether or not it sent anything, the same
job posts one code-built line to the room (``ChannelHub.post_room``, Cori's
``channels.slack.rooms.pulse``, ``#voyager-pulse``): ``✓ Cori up · healthy · running R12, R14 ·
next job J3 at 14:00`` (``idle``; the next job only once PER-298's ``registered_jobs`` exist),
``⏸ Cori paused · …`` while paused, then ``⚠ <check>: degraded since <time>`` lines (at most 5,
``+N more``) and ``Waiting: 2 approvals (oldest 40m)``; under 500 characters. It needs only the
room (no owner, no iMessage), posts in the briefing hour, and inside the quiet hours unless
``quiet_hours: true``. With it on the job ``runs_while_paused`` and skips the owner step while
paused. Exactly once through :func:`deliver` (records ``heartbeat.inflight:<key>``, key
``heartbeat:<date>:<HH>``; an unsent record of an earlier hour is abandoned), recorded as a
``heartbeat`` system text of ``room:<key>``. A failed post is logged and never fails the job.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from resonant import killswitch
from resonant.executor import CallContext
from resonant.extensions import cron, notify, rollup
from resonant.extensions.pulse import render_item, urgent_mark
from resonant.gateway.hub import RoomPosting
from resonant.store import approvals as approvals_store
from resonant.store import pulse as store
from resonant.store import runs as runs_store
from resonant.store import system_texts
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow
from resonant.tools.when import format_due

if TYPE_CHECKING:
    from resonant.brain.voice import Facts, Voice
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
# A send that isn't ``channel.send(owner, …)``: ``(text, dedupe_key)`` (the heartbeat's room).
Post = Callable[[str, str], Awaitable[object]]
INFLIGHT_KEEP = timedelta(days=2)
UNSENT_MAX_AGE = timedelta(hours=2)  # an unsent record older than this is abandoned
ITEM_TTL_S = 24 * 3600
WORKER_PREFIX = "run/"
WORKERS_HEADER = "Workers:"
NOTICE_PREFIX = "notice/"  # a brain notice (``brain/notices.py``): code-built, not outside


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
    mark = ""
    if item.cleared and item.source != CORE:
        mark = "✓"
    elif item.urgency == "urgent":
        mark = urgent_mark(item)
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


def is_worker_item(item: store.Item) -> bool:
    """A line of the ``Workers:`` rollup (GH-2): a Claude worker's item (``core`` /
    ``run/<code>``, ``runners/reports.py``) or one the rollup derives (``stall/``,
    ``merged/``, ``review/``, ``extensions/rollup.py``)."""
    return rollup.is_rollup_item(item)


def worker_line(item: store.Item, max_chars: int = LINE_MAX) -> str:
    """One worker as a line under ``Workers:`` (its title is code-built around cleaned
    parts): ``🔴`` for one that should have been texted (a failure nobody took)."""
    return render_item(item, urgent_mark(item) if item.urgency == "urgent" else "", max_chars)


def order_workers(items: Sequence[store.Item]) -> list[store.Item]:
    """Finished first (urgent first, oldest first: they are news), then the running ones
    oldest first (status, dropped first when the message is full)."""
    finished = _order([i for i in items if i.cleared])
    going = sorted((i for i in items if not i.cleared), key=lambda i: (i.updated_at, i.key))
    return [*finished, *going]


Entries = Sequence[tuple[str, tuple[store.Item, ...] | None]]


def fit_entries(entries: Entries, room: int) -> tuple[list[str], list[store.Item], int]:
    """Like :func:`fit_lines` for lines that each say some items (none: a status line) or
    are a section header (None). A header is never left last, and ``+N more`` counts items
    only. Returns the lines shown, their items and how many items are left out."""
    total = sum(len(items or ()) for _line, items in entries)
    shown: list[str] = []
    listed: list[store.Item] = []
    used = 0
    for line, items in entries:
        left = total - len(listed) - len(items or ())
        reserve = len(f"\n+{left} more") if left else 0
        if used + 1 + len(line) + reserve > room:
            break
        shown.append(line)
        used += 1 + len(line)
        listed += items or ()
    while shown and entries[len(shown) - 1][1] is None:  # a header with nothing under it
        shown.pop()
    return shown, listed, total - len(listed)


def worker_entries(
    workers: Sequence[store.Item],
    header: str = WORKERS_HEADER,
    *,
    context: Sequence[store.Item] = (),
    roll: rollup.Rollup | None = None,
) -> list[tuple[str, tuple[store.Item, ...] | None]]:
    """The ``Workers:`` section as entries for :func:`fit_entries`: the header and the
    rollup's lines (``rollup.entries``: ``workers`` are the news, ``context`` the runs still
    going). None without a line."""
    lines = rollup.entries(workers, context, roll)
    if not lines:
        return []
    return [(header, None), *lines]


def compose(
    items: Sequence[store.Item],
    context: Sequence[store.Item] = (),
    roll: rollup.Rollup | None = None,
) -> tuple[str, list[store.Item]] | None:
    """The pulse text and the items it shows (the rest wait); None with no items.

    Workers' and the rollup's items go in a ``Workers:`` section after the rest (before it
    when one is urgent: a failure the owner wasn't texted), as the rollup's lines
    (``extensions/rollup.py``). ``context``: workers already told that are still running,
    listed under ``Still working on`` when there is news (never a reason to send, and not
    counted in ``+N more``); ``roll`` names them and adds the PRs still being worked on."""
    if not items:
        return None
    others = _order([i for i in items if not is_worker_item(i)])
    keys = {(i.source, i.key) for i in items}
    workers = [i for i in items if is_worker_item(i)]
    going = [i for i in context if (i.source, i.key) not in keys and is_worker_item(i)]
    entries: list[tuple[str, tuple[store.Item, ...] | None]] = [
        (item_line(i), (i,)) for i in others
    ]
    section = worker_entries(workers, context=going, roll=roll)
    if any(i.urgency == "urgent" for i in workers):  # a failure nobody took goes first
        entries = [*section, *entries]
    else:
        entries += section
    shown, listed, _left = fit_entries(entries, MAX_CHARS - len(HEADER))
    shown_keys = {(i.source, i.key) for i in listed}
    more = sum(1 for k in keys if k not in shown_keys)  # news left out, not status lines
    if not listed:  # one line is at most LINE_MAX, so this is only a tiny-cap safety net
        first = next(i for i in items)
        shown, listed = [item_line(first)], [first]
        more = sum(1 for k in keys if k != (first.source, first.key))
        assert len(shown[0]) <= MAX_CHARS - len(HEADER)
    parts = [HEADER, *shown]
    if more:
        parts.append(f"+{more} more")
    return "\n".join(parts), listed


def running_workers(conn: sqlite3.Connection, now: datetime) -> list[store.Item]:
    """Open (uncleared, unexpired) worker items: the runs still going."""
    return [
        i
        for i in store.open_items(conn, now)
        if i.source == CORE and i.key.startswith(WORKER_PREFIX) and not i.cleared
    ]


def is_approval_item(item: store.Item) -> bool:
    """A core re-mention of a pending approval (its title carries the approval code)."""
    return item.source == CORE and item.key.startswith("approval/")


def _pending(
    conn: sqlite3.Connection, now: datetime, *, hide_approvals: bool = False
) -> list[store.Item]:
    """Untold urgent and normal items of sources that aren't muted (``hide_approvals``: and
    not the approval re-mentions, whose titles carry codes)."""
    items = store.pending(conn, ("urgent", "normal"), now)
    if hide_approvals:
        items = [i for i in items if not is_approval_item(i)]
    muted = {s for s in {i.source for i in items} if s != CORE and notify.muted_until(conn, s, now)}
    return [i for i in items if i.source not in muted]


def preview(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    *,
    hide_approvals: bool = False,
    stall_after: timedelta = rollup.STALL_AFTER,
) -> str | None:
    """What the next pulse would say, or None if it has nothing. Read-only (core items are
    only refreshed by a run)."""
    at = now or utcnow()
    composed = _compose_now(conn, at, stall_after, hide_approvals=hide_approvals)
    return None if composed is None else composed.text


@dataclass(frozen=True)
class Composed:
    """A composed pulse: the code-built ``text``, the items it ``listed``, and what the voice
    needs to say it again (PER-273): the workers still ``going`` and the rollup's names."""

    text: str
    listed: list[store.Item]
    going: list[store.Item]
    roll: rollup.Rollup


def _compose_now(
    conn: sqlite3.Connection, now: datetime, stall_after: timedelta, *, hide_approvals: bool
) -> Composed | None:
    """:func:`compose` over the store: the pending items, the running workers and the
    rollup's names for them. Read-only."""
    items = rollup.held_while_stale(conn, now, _pending(conn, now, hide_approvals=hide_approvals))
    going = [*running_workers(conn, now), *rollup.quiet_items(conn, now)]
    roll = rollup.gather(conn, now, [*items, *going], stall_after=stall_after)
    built = compose(items, going, roll)
    return None if built is None else Composed(built[0], built[1], going, roll)


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


def preview_text(
    conn: sqlite3.Connection, now: datetime, tz: str, *, stall_after: timedelta = rollup.STALL_AFTER
) -> str:
    """``/pulse`` and the ``pulse_preview`` tool: what the next pulse would say, then the open
    items per source. Read-only (nothing is marked told; core re-mentions appear once a pulse
    run computes them). Approval re-mentions are left out: ``/pulse`` is also the chat tool,
    and approval codes never reach the model (``/approvals`` shows them)."""
    text = (
        preview(conn, now, hide_approvals=True, stall_after=stall_after)
        or "Nothing new since the last update."
    )
    summary = open_summary(conn, now, tz)
    return f"{text}\n{summary}" if summary else text


# --- core items ------------------------------------------------------------------------------


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


def remention_delay(settings: Settings) -> timedelta:
    """``pulse.remention_after``, capped at half the approval TTL so the approval is still
    open when it is mentioned again."""
    return min(settings.pulse.remention, timedelta(seconds=settings.gate.approval_ttl_s / 2))


def refresh_core_items(
    conn: sqlite3.Connection, owner: str, settings: Settings, now: datetime
) -> None:
    """Refresh the core items from the store: worker questions are re-mentioned. Approvals
    are never pulse items (PER-261, ``core/remind`` reminds instead); an open
    ``core/approval/*`` item from before is cleared silently."""
    del owner
    _sync(conn, now, "approval", {}, lambda item: None)
    remention_questions(conn, now, remention_delay(settings))


QUESTION_PREFIX = "question/"  # worker_desk.question_item_key
STILL_WAITING = "still waiting: "


def remention_questions(conn: sqlite3.Connection, now: datetime, delay: timedelta) -> None:
    """An open worker question (``core/question/<run>-<id>``, written and cleared by the
    worker desk with the question's redacted text) is news again once the owner was last
    told about it ``delay`` ago: each hourly pulse repeats it until it is answered, times
    out or its run ends (EM-5)."""
    for item in store.open_items(conn, now):
        if item.source != CORE or not item.key.startswith(QUESTION_PREFIX) or item.changed:
            continue
        if item.told_at is None or now - item.told_at < delay:
            continue
        title = STILL_WAITING + item.title.removeprefix(STILL_WAITING)
        state = {"open": True, "reminded": now.isoformat(timespec="minutes")}
        store.upsert(conn, CORE, item.key, title, "normal", state, False, ITEM_TTL_S, now)


def refresh_rollup(conn: sqlite3.Connection, settings: Settings, now: datetime) -> None:
    """The rollup's derived items (stalls, the github watch's merges and review requests,
    ``extensions/rollup.py``). Never raises: the pulse goes out without them."""
    try:
        rollup.refresh(
            conn,
            now,
            stall_after=settings.pulse.stall,
            transcripts=rollup.transcripts_dir(settings.runner.mirror_dir),
        )
    except Exception:
        log.exception("rollup refresh failed")


# --- sending ---------------------------------------------------------------------------------


def _paused(conn: sqlite3.Connection) -> bool:
    try:
        return killswitch.is_paused(conn)
    except killswitch.PausedStateUnknownError:
        return True  # fail closed, like the scheduler


def _rec_at(rec: Any) -> datetime | None:
    """The record's ``at``, or None when it is missing or corrupt (treated as abandoned)."""
    try:
        at = datetime.fromisoformat(str(cast(dict[str, Any], rec)["at"]))
    except (KeyError, ValueError, TypeError):
        return None
    return at if at.utcoffset() is not None else None


def is_outside(items: Sequence[store.Item]) -> bool:
    """Whether a text listing ``items`` carries outside content (PER-274): any item but a
    core notice (a worker's item or question, the rollup's PR lines, an extension's item)."""
    return any(i.source != CORE or not i.key.startswith(NOTICE_PREFIX) for i in items)


def text_kind(key: str) -> system_texts.Kind:
    """The system text kind of a delivery's dedupe key: ``briefing:…``, ``heartbeat:…`` or
    the pulse's."""
    if key.startswith("briefing:"):
        return "briefing"
    return "heartbeat" if key.startswith("heartbeat:") else "pulse"


def prune_inflight(conn: sqlite3.Connection, now: datetime, *, inflight: str = INFLIGHT) -> None:
    """Drop delivery records (done, or never finished) older than ``INFLIGHT_KEEP``, and
    any whose ``at`` is corrupt (``inflight``: the records' kv prefix)."""
    rows = conn.execute("SELECT key FROM kv WHERE key LIKE ?", (inflight + "%",)).fetchall()
    with atomic(conn):
        for (k,) in rows:
            rec: Any = kv_get(conn, k)
            if not isinstance(rec, dict):
                continue
            at = _rec_at(rec)
            if at is None or at < now - INFLIGHT_KEEP:
                conn.execute("DELETE FROM kv WHERE key = ?", (k,))


async def deliver(
    conn: sqlite3.Connection,
    channel: Channel,
    owner: str,
    key: str,
    now: datetime,
    build: Callable[[], tuple[str, list[store.Item]] | None],
    *,
    rewrite: Callable[[str, list[store.Item]], Awaitable[str]] | None = None,
    outside: Callable[[list[store.Item]], bool] = is_outside,
    post: Post | None = None,
    inflight: str = INFLIGHT,
) -> tuple[str, int]:
    """Send one message to ``owner`` exactly once for dedupe key ``key``.

    ``build`` returns the text and the items it lists (None: nothing to say). The intent is
    stored before the send; a rerun resends the stored text and marks exactly the stored
    items told (see the module docstring). Returns ``("sent", n)``, ``("nothing", 0)`` or
    ``("duplicate", 0)``.

    ``rewrite`` (PER-273) may say the built text another way (Claude's voice); it runs once,
    before the record is stored, only for a new key, and gets the code-built text and the
    items. It may return the text unchanged; if it raises, the code-built text is sent. A
    cancel during it leaves no record and sends nothing.

    ``outside`` says from the listed items whether the text carries outside content (kept in
    the record for the system text, PER-274; default :func:`is_outside`).

    ``post`` (PER-303, the heartbeat) sends ``(text, dedupe_key)`` somewhere else than
    ``channel.send(owner, …)`` (a room); ``owner`` is then only the system text's principal.
    ``inflight`` is the kv prefix of the records, so a heartbeat's (``heartbeat.inflight:``)
    and the owner's never finish each other's.
    """
    rkey = inflight + key
    raw: Any = kv_get(conn, rkey)
    rec = cast(dict[str, Any], raw) if isinstance(raw, dict) else None
    if rec is None:
        await _finish_unsent(
            conn, channel, owner, now, except_key=rkey, post=post, inflight=inflight
        )
        built = build()
        if built is None:
            return "nothing", 0
        text, listed = built
        voiced = False
        if rewrite is not None:
            try:
                new = await rewrite(text, listed)
                voiced, text = new != text, new
            except Exception:
                log.exception("pulse: voice failed; sending the code-built text")
        rec = {
            "text": text,
            "items": [[i.source, i.key, i.fingerprint] for i in listed],
            "sent": False,
            "at": iso(now),
            "voice": voiced,
            "outside": outside(listed),
        }
        with atomic(conn):
            kv_set(conn, rkey, rec)
    elif rec.get("sent"):
        return "duplicate", 0
    return "sent", await _send_recorded(
        conn, channel, owner, key, rec, now, post=post, inflight=inflight
    )


async def _send_recorded(
    conn: sqlite3.Connection,
    channel: Channel,
    owner: str,
    key: str,
    rec: dict[str, Any],
    now: datetime,
    *,
    post: Post | None = None,
    inflight: str = INFLIGHT,
) -> int:
    """Send the stored text under its own dedupe key, mark exactly its items told and record
    it as a system text (PER-274, once per key)."""
    # late: ``resonant.brain`` imports the router, which imports this module
    from resonant.brain.voice import record_text

    recorded = cast(list[list[str]], rec["items"])
    text = str(rec["text"])
    if post is None:
        await channel.send(owner, text, dedupe_key=key)
    else:
        await post(text, key)
    with atomic(conn):
        for source, ikey, fp in recorded:
            item = store.get(conn, source, ikey)
            if item is not None:
                store.mark_told(conn, [replace(item, fingerprint=fp)], now)
        system_texts.record(conn, owner, text_kind(key), record_text(text), ref=key, now=now,
                            outside=rec.get("outside") is not False)  # fmt: skip
        kv_set(conn, inflight + key, {**rec, "sent": True})
    return len(recorded)


async def _finish_unsent(
    conn: sqlite3.Connection,
    channel: Channel,
    owner: str,
    now: datetime,
    *,
    except_key: str,
    post: Post | None = None,
    inflight: str = INFLIGHT,
) -> None:
    """Finish the delivery records of other keys that never got marked: a run that raised
    after its send (the scheduler consumes that due time, so no catch-up reruns it). Without
    this the next message would compose the same untold items again under a new key. The
    channel drops the resend if it went out.

    Only a record younger than ``UNSENT_MAX_AGE`` whose items still have the recorded
    fingerprints is resent (oldest first). Anything else is abandoned (the record is
    deleted; its items stay pending and are recomposed fresh, never as stale text). A resend
    that fails is logged and the record is tried again on the next delivery, until it ages
    out."""
    pending: list[tuple[datetime, str, dict[str, Any]]] = []
    abandoned: list[str] = []
    for (k,) in conn.execute("SELECT key FROM kv WHERE key LIKE ?", (inflight + "%",)).fetchall():
        raw: Any = kv_get(conn, k)
        if k == except_key or not isinstance(raw, dict) or cast(dict[str, Any], raw).get("sent"):
            continue
        rec = cast(dict[str, Any], raw)
        at = _rec_at(rec)
        if at is None or now - at > UNSENT_MAX_AGE or not _still_current(conn, rec):
            abandoned.append(k)
        else:
            pending.append((at, k, rec))
    if abandoned:
        with atomic(conn):
            for k in abandoned:
                conn.execute("DELETE FROM kv WHERE key = ?", (k,))
    for _at, k, rec in sorted(pending, key=lambda p: (p[0], p[1])):
        try:
            await _send_recorded(
                conn, channel, owner, k.removeprefix(inflight), rec, now,
                post=post, inflight=inflight,
            )  # fmt: skip
        except Exception:
            log.warning("pulse: could not finish an earlier delivery", extra={"key": k})


def _still_current(conn: sqlite3.Connection, rec: dict[str, Any]) -> bool:
    """Every item in the record still has the fingerprint the text was composed from."""
    try:
        for source, ikey, fp in cast(list[list[str]], rec["items"]):
            item = store.get(conn, source, ikey)
            if item is None or item.fingerprint != fp:
                return False
    except (KeyError, ValueError, TypeError):
        return False
    return True


_MORE = re.compile(r"\+\d+ more")


def pulse_facts(composed: Composed, conn: sqlite3.Connection) -> Facts | None:
    """The pulse's facts (PER-273): the workers' news as items; the extension items and
    worker questions, the ⚠ and side-effect lines and ``+N more`` as the tail, which stays
    code-built. None when the pulse has no workers' news (nothing for Claude to say)."""
    # late: ``resonant.brain`` imports the router, which imports this module
    from resonant.brain.voice import Facts
    from resonant.extensions import voice_facts

    found = voice_facts.worker_facts(conn, composed.listed, composed.going, composed.roll)
    if found is None or not found.items:
        return None
    others = _order([i for i in composed.listed if not is_worker_item(i)])
    tail = [*found.tail, *(item_line(i) for i in others)]
    last = composed.text.rsplit("\n", 1)[-1]
    if _MORE.fullmatch(last):
        tail.append(last)
    return Facts("pulse", found.items, going=found.going, tail=tuple(tail))


async def voiced_pulse(
    voice: Voice, conn: sqlite3.Connection, composed: Composed, text: str, *, ref: str | None = None
) -> str:
    """``text`` (the code-built pulse) said in Claude's voice, or ``text`` itself: with no
    workers' news, or whenever the voice falls back."""
    facts = pulse_facts(composed, conn)
    if facts is None:
        return text
    return (await voice.write(facts, text, ref=ref)).text


async def send_pulse(
    conn: sqlite3.Connection,
    channel: Channel,
    owner: str,
    *,
    settings: Settings,
    now: datetime,
    due: datetime | None = None,
    manual: bool = False,
    voice: Voice | None = None,
) -> dict[str, Any]:
    """Prune, refresh the core items and, if it is time, send the pulse.

    Returns ``{sent, count, skipped?}`` (``skipped``: ``paused``, ``briefing``, ``quiet``,
    ``nothing``, ``duplicate``); never the text. ``voice`` (PER-273): Claude writes the prose
    (:func:`voiced_pulse`). ``manual`` ignores the briefing hour and the
    quiet hours (not pause) and uses the per-minute manual key.
    """
    local = (due or now).astimezone(ZoneInfo(settings.timezone))
    store.prune(conn, now)
    prune_inflight(conn, now)
    refresh_core_items(conn, owner, settings, now)
    refresh_rollup(conn, settings, now)
    if _paused(conn):
        return {"sent": False, "count": 0, "skipped": "paused"}
    if not manual:
        if settings.briefing.enabled and local.hour == settings.briefing.local_time.hour:
            return {"sent": False, "count": 0, "skipped": "briefing"}
        if settings.pulse.in_quiet(local.time()):
            return {"sent": False, "count": 0, "skipped": "quiet"}
    key = manual_dedupe_key(local) if manual else dedupe_key(local)
    composed: list[Composed] = []

    def build() -> tuple[str, list[store.Item]] | None:
        built = _compose_now(conn, now, settings.pulse.stall, hide_approvals=False)
        if built is None:
            return None
        composed.append(built)
        return built.text, built.listed

    async def rewrite(text: str, _listed: list[store.Item]) -> str:
        assert voice is not None
        return await voiced_pulse(voice, conn, composed[-1], text, ref=key)

    status, count = await deliver(
        conn, channel, owner, key, now, build, rewrite=rewrite if voice is not None else None
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


# --- the heartbeat (PER-303) -----------------------------------------------------------------

HEARTBEAT_INFLIGHT = "heartbeat.inflight:"  # kv: the heartbeat's delivery records (deliver())
HEARTBEAT_MAX_CHARS = 500
HEARTBEAT_HEALTH_LINES = 5
HEARTBEAT_CODES = 8  # running R-codes named on the first line, then ``+N``


def heartbeat_key(local: datetime) -> str:
    return f"heartbeat:{local.date().isoformat()}:{local:%H}"


def heartbeat_principal(room: str) -> str:
    """Whose system text a room's heartbeat is: the room's (``room:pulse``), not a person's,
    so an hourly line never crowds anyone's transcript."""
    return f"room:{room}"


def _at(dt: datetime, now: datetime, zone: ZoneInfo) -> str:
    """``14:00`` today (local), else ``Mon 14:00`` (a week either way at most matters)."""
    local, today = dt.astimezone(zone), now.astimezone(zone)
    return f"{local:%H:%M}" if local.date() == today.date() else f"{local:%a %H:%M}"


def _age(delta: timedelta) -> str:
    """``40m``, ``3h``, ``2d``."""
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def next_registered_job(conn: sqlite3.Connection, now: datetime) -> tuple[str, datetime] | None:
    """The next due registered job (PER-298's ``registered_jobs``: ``J<n>``, ``active``,
    ``schedule`` in ``tz``) as ``(id, due)``. A soft dependency: None while the table doesn't
    exist (or doesn't read), and a row whose cron doesn't parse is left out. Read-only."""
    try:
        rows = conn.execute(
            "SELECT * FROM registered_jobs WHERE state = 'active' ORDER BY id"
        ).fetchall()
    except sqlite3.Error:
        return None
    best: tuple[str, datetime] | None = None
    for r in rows:
        try:
            keys = r.keys()
            if "kind" in keys and r["kind"] not in (None, "schedule"):
                continue
            due = cron.parse_schedule(str(r["schedule"]), str(r["tz"])).next_after(now)
            job_id = str(r["id"])
        except (cron.CronError, IndexError, KeyError, TypeError, ValueError):
            continue
        if due is not None and (best is None or due < best[1]):
            best = (job_id, due)
    return best


def heartbeat_text(
    conn: sqlite3.Connection, settings: Settings, now: datetime, *, paused: bool
) -> str:
    """The heartbeat, code-built from the store (no voice), at most ``HEARTBEAT_MAX_CHARS``.

    ``✓ Cori up · healthy · running R12, R14 · next job J3 at 14:00`` (``idle`` with no run;
    the next job only when PER-298's registered jobs exist), ``⏸ Cori paused · …`` while
    paused. Then a ``⚠ <check>: degraded since <time>`` line per degraded health row (at most
    5, then ``+N more``; Slack's own ``slack`` row among them) and ``Waiting: 2 approvals
    (oldest 40m)``. Names only: R-codes, check names and counts, never a run's ask or a
    detail."""
    from resonant.monitor import health_snapshot  # late: the monitor imports the gateway

    zone = ZoneInfo(settings.timezone)
    name = settings.instance.display_name
    degraded = [(c, row) for c, row in health_snapshot(conn).items() if row["state"] == "degraded"]
    parts = [f"⏸ {name} paused" if paused else f"✓ {name} up"]
    if degraded:
        parts.append(f"{len(degraded)} check{'s' if len(degraded) != 1 else ''} degraded")
    else:
        parts.append("healthy")
    codes = [r.code for r in runs_store.active(conn)]
    if codes:
        shown = ", ".join(codes[:HEARTBEAT_CODES])
        extra = len(codes) - HEARTBEAT_CODES
        parts.append(f"running {shown}" + (f" +{extra}" if extra > 0 else ""))
    else:
        parts.append("idle")
    job = next_registered_job(conn, now)
    if job is not None:
        parts.append(f"next job {job[0]} at {_at(job[1], now, zone)}")
    lines = [" · ".join(parts)]
    for check, row in degraded[:HEARTBEAT_HEALTH_LINES]:
        since = row.get("since")
        line = f"⚠ {check}: {row['state']}"
        if isinstance(since, str):
            with contextlib.suppress(ValueError):
                line += f" since {_at(datetime.fromisoformat(since), now, zone)}"
        lines.append(line)
    if len(degraded) > HEARTBEAT_HEALTH_LINES:
        lines.append(f"+{len(degraded) - HEARTBEAT_HEALTH_LINES} more")
    waiting = approvals_store.pending_all(conn, now)
    if waiting:
        oldest = min(a.created_at for a in waiting)
        n = len(waiting)
        lines.append(f"Waiting: {n} approval{'s' if n != 1 else ''} (oldest {_age(now - oldest)})")
    text = "\n".join(lines)
    if len(text) > HEARTBEAT_MAX_CHARS:
        text = text[: HEARTBEAT_MAX_CHARS - 1].rstrip() + "…"
    return text


async def send_heartbeat(
    conn: sqlite3.Connection,
    channel: RoomPosting,
    *,
    settings: Settings,
    now: datetime,
    due: datetime | None = None,
) -> dict[str, Any]:
    """Post the heartbeat to ``pulse.heartbeat.room`` once for the due hour.

    Returns ``{posted, skipped?}`` (``skipped``: ``off``, ``quiet``, ``duplicate``,
    ``failed``); never the text. Posts while paused (as ``⏸``) and, unless
    ``pulse.heartbeat.quiet_hours``, inside the quiet hours; the briefing hour too. Exactly
    once through :func:`deliver` (its own records, ``heartbeat.inflight:``), keyed
    ``heartbeat:<local date>:<HH>``, recorded as a ``heartbeat`` system text of
    ``room:<key>``. Never raises: a refused or failed post (the room isn't configured, the
    bot was removed from it, Slack is down) is logged and ``failed`` (the Slack adapter notes
    the room's error in its health detail, so the ``slack`` check shows it); a rerun in the
    same hour tries again under the same key, and a later hour abandons it (never sent late).
    """
    hb = settings.pulse.heartbeat
    room = hb.room
    if room is None:
        return {"posted": False, "skipped": "off"}
    local = (due or now).astimezone(ZoneInfo(settings.timezone))
    try:
        prune_inflight(conn, now, inflight=HEARTBEAT_INFLIGHT)
        if hb.quiet_hours and settings.pulse.in_quiet(local.time()):
            return {"posted": False, "skipped": "quiet"}
        key = heartbeat_key(local)
        _drop_other_heartbeats(conn, HEARTBEAT_INFLIGHT + key)
        paused = _paused(conn)
        text = heartbeat_text(conn, settings, now, paused=paused)

        async def post(body: str, key: str) -> object:
            return await channel.post_room(room, body, dedupe_key=key)

        status, _count = await deliver(
            conn,
            cast("Channel", channel),  # unused: ``post`` sends
            heartbeat_principal(room),
            key,
            now,
            lambda: (text, []),
            outside=lambda _items: False,  # code-built names and counts only
            post=post,
            inflight=HEARTBEAT_INFLIGHT,
        )
    except Exception as e:
        log.warning("heartbeat: not posted to room %s (%s)", room, type(e).__name__)
        return {"posted": False, "skipped": "failed"}
    if status == "duplicate":
        return {"posted": False, "skipped": "duplicate"}
    return {"posted": True}


def _drop_other_heartbeats(conn: sqlite3.Connection, keep: str) -> None:
    """Abandon every unsent heartbeat record but ``keep``'s: a heartbeat is only worth its
    own hour, so a post that failed is never sent late next to the new one."""
    rows = conn.execute(
        "SELECT key FROM kv WHERE key LIKE ?", (HEARTBEAT_INFLIGHT + "%",)
    ).fetchall()
    stale: list[str] = []
    for (k,) in rows:
        rec: Any = kv_get(conn, k)
        if k != keep and not (isinstance(rec, dict) and cast(dict[str, Any], rec).get("sent")):
            stale.append(k)
    if stale:
        with atomic(conn):
            for k in stale:
                conn.execute("DELETE FROM kv WHERE key = ?", (k,))


def _room_poster(channel: Channel | None) -> RoomPosting | None:
    return channel if isinstance(channel, RoomPosting) else None


def pulse_job(deps: CoreDeps, *, clock: Callable[[], datetime] = utcnow) -> CoreJob | None:
    """The ``CORE_JOBS`` factory.

    The owner pulse needs ``pulse.enabled``, a channel and an owner; the heartbeat (PER-303)
    only ``pulse.heartbeat.room`` and a channel that posts to rooms (the hub). None when
    neither can run. With the heartbeat on, the job runs while paused too
    (``runs_while_paused``): the owner step is skipped then (``paused``, as the scheduler
    would have), and the heartbeat posts ``⏸``. The heartbeat runs after the owner step,
    whether or not it sent anything (or raised), and never fails the job."""
    from resonant.extensions.scheduler import CoreJob

    channel, owner = deps.channel, deps.owner
    settings = deps.settings
    owner_pulse = settings.pulse.enabled and channel is not None and owner is not None
    rooms = _room_poster(channel) if settings.pulse.heartbeat.room is not None else None
    if not owner_pulse and rooms is None:
        if settings.pulse.enabled:
            log.info("pulse not scheduled", extra={"channel": channel is not None})
        return None
    conn = deps.conn

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        now, due = clock(), _due(args)
        beat: dict[str, Any] | None = None
        try:
            if not owner_pulse:
                out: dict[str, Any] = {"sent": False, "count": 0, "skipped": "unreachable"}
            elif rooms is not None and _paused(conn):
                out = {"sent": False, "count": 0, "skipped": "paused"}
            else:
                assert channel is not None and owner is not None
                out = await send_pulse(
                    conn, channel, owner, settings=settings, now=now, due=due,
                    voice=deps.components.voice,
                )  # fmt: skip
        finally:
            if rooms is not None:
                beat = await send_heartbeat(conn, rooms, settings=settings, now=now, due=due)
        if beat is not None:
            out["heartbeat"] = beat
        return out

    return CoreJob(
        id=JOB_ID,
        schedule=SCHEDULE,
        run=run,
        catchup=CATCHUP,
        timeout_s=TIMEOUT_S,
        runs_while_paused=rooms is not None,
    )


__all__ = [
    "CATCHUP",
    "JOB_ID",
    "MAX_CHARS",
    "NOTICE_PREFIX",
    "compose",
    "dedupe_key",
    "deliver",
    "fit_lines",
    "heartbeat_key",
    "heartbeat_text",
    "is_outside",
    "item_line",
    "manual_dedupe_key",
    "open_summary",
    "preview",
    "pulse_facts",
    "pulse_job",
    "refresh_core_items",
    "run_pulse_now",
    "schedule",
    "send_heartbeat",
    "send_pulse",
    "voiced_pulse",
]

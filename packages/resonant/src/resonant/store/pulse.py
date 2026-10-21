"""Pulse items repository (migration 005). Plain functions over a connection.

An item is the latest report of one thing worth mentioning, keyed ``(source, key)``: an
incident, a PnL figure, a degraded health check. ``source`` is ``core`` or an extension name.
Nothing is said twice because each row remembers the fingerprint the owner last **heard**
(``told_fingerprint``), not the last one reported:

- :func:`upsert` compares the new fingerprint with ``told_fingerprint``. So A -> B -> A after
  B was told is a change again, and a report the owner never heard (paused, muted, held for
  the next pulse) is still pending however often it is repeated. It returns ``new`` on every
  repeat while the row is untold, so ``Change`` is "is there news", not "send now": exactly
  once comes from the event-id dedupe of the send plus the told state, never from ``Change``.
- :func:`pending` lists items whose fingerprint differs from what was told (urgency picks the
  channel: urgent now, normal at the hourly pulse, low at the briefing).
- :func:`open_items` lists everything still open for the briefing, changed-since-told first.
- :func:`mark_acked` records that the owner wrote back after a told version (``acked_at``;
  any later message counts, so it is deterministic and needs no model), and
  :func:`known_items` lists what was told recently, for the brain's "Already told" list.
  Acking never re-opens an item; a new told version resets it.
- :func:`mark_told` records what was said, compare-and-set: it only updates a row whose
  current fingerprint is the one of the *item that was sent*, so a report that lands between
  listing and sending stays pending, and a stale list never overwrites newer told state.

A ``clear=True`` report is an item's last word ("Dori recovered"). It is pending until it is
told (it never expires out of :func:`pending` first, so a clear held over quiet hours or a
mute is not lost), and after it is told it drops out of every listing. :func:`prune` deletes
told cleared rows, and expired rows, a week after the fact.

The fingerprint hashes ``{"state": ...}`` when the extension gave a ``state``, else
``{"title": ...}``, plus the urgency and the ``clear`` flag: a resolve with the same text is
still news, and so is an escalation from normal to urgent. The daemon never interprets
``state`` (at most ``MAX_STATE_BYTES`` of canonical JSON); the extension decides what counts
as a real change by what it puts there (bucket the PnL, leave timestamps out).

Callers own transactions and audit rows. Titles must already be sanitized (the pulse runs
``notify.clean_text`` on them); this module only clips them to ``TITLE_MAX``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from resonant.store.db import atomic, iso

Urgency = Literal["urgent", "normal", "low"]
Change = Literal["new", "changed", "same"]

URGENCIES: tuple[Urgency, ...] = ("urgent", "normal", "low")
KEY_MAX = 120
TITLE_MAX = 200
DEFAULT_TTL_S = 24 * 3600
MAX_TTL_S = 7 * 24 * 3600
MAX_STATE_BYTES = 2000
PRUNE_AFTER = timedelta(days=7)
PRUNE_UNTOLD_CLEAR_AFTER = timedelta(days=30)  # a clear nobody ever heard is kept longer

_KEY = re.compile(r"^[a-z0-9/._:-]{1,120}$")


@dataclass(frozen=True)
class Item:
    source: str
    key: str
    title: str
    urgency: Urgency
    fingerprint: str
    cleared: bool
    updated_at: datetime
    expires_at: datetime
    told_fingerprint: str | None
    told_at: datetime | None
    acked_at: datetime | None = None

    @property
    def changed(self) -> bool:
        """True when the owner hasn't heard this version (never told, or told another)."""
        return self.told_fingerprint != self.fingerprint

    @property
    def known(self) -> bool:
        """True when the owner was told exactly this version."""
        return self.told_fingerprint == self.fingerprint


def fingerprint(
    title: str, state: Any | None = None, *, urgency: str = "normal", clear: bool = False
) -> str:
    """sha256 of the canonical JSON of ``state`` (else ``title``), ``urgency`` and ``clear``.

    Raises ``ValueError`` for a ``state`` that isn't JSON or is over ``MAX_STATE_BYTES``.
    """
    payload: dict[str, Any] = {"urgency": urgency, "clear": clear}
    if state is None:
        payload["title"] = title
    else:
        try:
            state_json = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as e:
            raise ValueError(f"state must be JSON-serializable: {e}") from e
        if len(state_json.encode()) > MAX_STATE_BYTES:
            raise ValueError(f"state must be at most {MAX_STATE_BYTES} bytes of JSON")
        payload["state"] = state
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_key(code: str) -> str:
    """The ``core`` item key of a run (``R8`` -> ``run/r8``)."""
    return f"run/{code.lower()}"


def valid_key(key: str) -> bool:
    return _KEY.fullmatch(key) is not None


def _row_to_item(r: sqlite3.Row) -> Item:
    told_at: str | None = r["told_at"]
    acked_at: str | None = r["acked_at"]
    return Item(
        source=r["source"],
        key=r["key"],
        title=r["title"],
        urgency=r["urgency"],
        fingerprint=r["fingerprint"],
        cleared=bool(r["cleared"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
        expires_at=datetime.fromisoformat(r["expires_at"]),
        told_fingerprint=r["told_fingerprint"],
        told_at=None if told_at is None else datetime.fromisoformat(told_at),
        acked_at=None if acked_at is None else datetime.fromisoformat(acked_at),
    )


def upsert(
    conn: sqlite3.Connection,
    source: str,
    key: str,
    title: str,
    urgency: Urgency,
    state: Any | None,
    clear: bool,
    ttl_s: int,
    now: datetime,
) -> tuple[Change, Item]:
    """Record a report; returns the change and the stored item (for :func:`mark_told`).

    ``new``: nothing told yet (repeated while it stays untold); ``changed``: differs from
    what was told. ``same`` means the owner already heard exactly this, so there is nothing
    to say. The row is rewritten either way, which also extends its expiry and reopens a
    cleared item. ``ValueError`` for a bad key, urgency, ttl, an empty title, a naive
    ``now`` or an unusable ``state``.
    """
    if not valid_key(key):
        raise ValueError(f"key must be 1..{KEY_MAX} characters of [a-z0-9/._:-]: {key!r}")
    if urgency not in URGENCIES:
        raise ValueError(f"urgency must be one of {URGENCIES}: {urgency!r}")
    if not 1 <= ttl_s <= MAX_TTL_S:
        raise ValueError(f"ttl_s must be 1..{MAX_TTL_S} seconds")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    title = title.strip()[:TITLE_MAX].rstrip()
    if not title:
        raise ValueError("title must not be empty")
    fp = fingerprint(title, state, urgency=urgency, clear=clear)
    with atomic(conn):
        row = conn.execute(
            "SELECT told_fingerprint FROM pulse_items WHERE source = ? AND key = ?", (source, key)
        ).fetchone()
        told: str | None = None if row is None else row["told_fingerprint"]
        conn.execute(
            "INSERT INTO pulse_items"
            " (source, key, title, urgency, fingerprint, cleared, updated_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (source, key) DO UPDATE SET title = excluded.title,"
            " urgency = excluded.urgency, fingerprint = excluded.fingerprint,"
            " cleared = excluded.cleared, updated_at = excluded.updated_at,"
            " expires_at = excluded.expires_at",
            (
                source,
                key,
                title,
                urgency,
                fp,
                int(clear),
                iso(now),
                iso(now + timedelta(seconds=ttl_s)),
            ),
        )
    item = get(conn, source, key)
    assert item is not None  # just written
    change: Change = "new" if told is None else "same" if told == fp else "changed"
    return change, item


def get(conn: sqlite3.Connection, source: str, key: str) -> Item | None:
    row = conn.execute(
        "SELECT * FROM pulse_items WHERE source = ? AND key = ?", (source, key)
    ).fetchone()
    return None if row is None else _row_to_item(row)


_URGENCY_ORDER = "CASE urgency WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"


def pending(
    conn: sqlite3.Connection,
    urgencies: Collection[Urgency],
    now: datetime,
    sources_excluded: Collection[str] = (),
) -> list[Item]:
    """Items at ``urgencies`` whose fingerprint isn't what the owner last heard.

    Expired items are left out, except cleared ones: their last word is owed until it is
    told, however long it was held.
    """
    wanted = list(urgencies)
    skipped = list(sources_excluded)
    if not wanted:
        return []
    # Only placeholders and module constants are interpolated; values are bound.
    sql = (
        "SELECT * FROM pulse_items"  # noqa: S608
        f" WHERE urgency IN ({','.join('?' * len(wanted))})"
        " AND (expires_at > ? OR cleared = 1)"
        " AND (told_fingerprint IS NULL OR told_fingerprint != fingerprint)"
    )
    params: list[str] = [*wanted, iso(now)]
    if skipped:
        sql += f" AND source NOT IN ({','.join('?' * len(skipped))})"
        params += skipped
    sql += f" ORDER BY {_URGENCY_ORDER}, updated_at, source, key"
    return [_row_to_item(r) for r in conn.execute(sql, params)]


def open_items(conn: sqlite3.Connection, now: datetime) -> list[Item]:
    """Uncleared, unexpired items at any urgency, for the briefing.

    Changed-since-told first, then by urgency and age.
    """
    sql = (
        "SELECT * FROM pulse_items WHERE cleared = 0 AND expires_at > ?"  # noqa: S608 - constant
        " ORDER BY (told_fingerprint IS NOT NULL AND told_fingerprint = fingerprint),"
        f" {_URGENCY_ORDER}, updated_at, source, key"
    )
    return [_row_to_item(r) for r in conn.execute(sql, (iso(now),))]


def mark_told(conn: sqlite3.Connection, items: Iterable[Item], at: datetime) -> None:
    """Record that the owner heard these versions (the fingerprints in ``items``).

    Compare-and-set: a row that was re-reported since the item was read is left pending.
    Telling a version the owner hadn't heard resets ``acked_at``; telling the same one again
    keeps it.
    """
    with atomic(conn):
        for item in items:
            conn.execute(
                "UPDATE pulse_items SET"
                " acked_at = CASE WHEN told_fingerprint IS ? THEN acked_at END,"
                " told_fingerprint = ?, told_at = ?"
                " WHERE source = ? AND key = ? AND fingerprint = ?",
                (
                    item.fingerprint,
                    item.fingerprint,
                    iso(at),
                    item.source,
                    item.key,
                    item.fingerprint,
                ),
            )


def mark_acked(conn: sqlite3.Connection, before: datetime, at: datetime) -> int:
    """The owner wrote at ``before``: ack every item told (at its current version) earlier.

    Returns how many were acked. Items told after ``before`` wait for the next message.
    """
    with atomic(conn):
        cur = conn.execute(
            "UPDATE pulse_items SET acked_at = ? WHERE acked_at IS NULL"
            " AND told_fingerprint = fingerprint AND told_at < ?",
            (iso(at), iso(before)),
        )
    return cur.rowcount


def known_items(conn: sqlite3.Connection, since: datetime, limit: int) -> list[Item]:
    """Items told (at their current version) since ``since``, newest told first.

    Cleared ones are included: "R8 failed" is still something the owner knows.
    """
    rows = conn.execute(
        "SELECT * FROM pulse_items WHERE told_fingerprint = fingerprint AND told_at >= ?"
        " ORDER BY told_at DESC, source, key LIMIT ?",
        (iso(since), limit),
    )
    return [_row_to_item(r) for r in rows]


def prune(conn: sqlite3.Connection, now: datetime) -> int:
    """Delete items cleared or expired for more than 7 days. Returns how many.

    A cleared item nobody has heard yet is kept for 30 days, so a long mute can't lose it.
    """
    cutoff = iso(now - PRUNE_AFTER)
    untold_cutoff = iso(now - PRUNE_UNTOLD_CLEAR_AFTER)
    with atomic(conn):
        cur = conn.execute(
            "DELETE FROM pulse_items WHERE"
            " (cleared = 1 AND told_fingerprint = fingerprint AND updated_at < ?)"
            " OR (cleared = 1 AND updated_at < ?)"
            " OR (cleared = 0 AND expires_at < ?)",
            (cutoff, untold_cutoff, cutoff),
        )
    return cur.rowcount

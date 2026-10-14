"""Pulse items repository (migration 005). Plain functions over a connection.

An item is the latest report of one thing worth mentioning, keyed ``(source, key)``: an
incident, a PnL figure, a degraded health check. ``source`` is ``core`` or an extension name.
Nothing is said twice because each row remembers the fingerprint the owner last **heard**
(``told_fingerprint``), not the last one reported:

- :func:`upsert` compares the new fingerprint with ``told_fingerprint``. So A -> B -> A after
  B was told is a change again, and a report the owner never heard (paused, muted, held for
  the next pulse) is still pending however often it is repeated.
- :func:`pending` lists items whose fingerprint differs from what was told (urgency picks the
  channel: urgent now, normal at the hourly pulse, low at the briefing).
- :func:`open_items` lists everything still open for the briefing, changed-since-told first.
- :func:`mark_told` records what was said. It stores the fingerprint of the *item that was
  sent*, so a report that lands between listing and sending stays pending.

A ``clear=True`` report is an item's last word ("Dori recovered"). It is pending once, if its
fingerprint differs from what was told, and after it is told it drops out of every listing.
:func:`prune` deletes rows a week after they were cleared or expired.

The fingerprint hashes ``{"state": ...}`` when the extension gave a ``state``, else
``{"title": ...}``. The daemon never interprets ``state``; the extension decides what counts
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
PRUNE_AFTER = timedelta(days=7)

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

    @property
    def changed(self) -> bool:
        """True when the owner hasn't heard this version (never told, or told another)."""
        return self.told_fingerprint != self.fingerprint


def fingerprint(title: str, state: Any | None = None) -> str:
    """sha256 of the canonical JSON of ``state`` when given, else of ``title``."""
    payload: dict[str, Any] = {"title": title} if state is None else {"state": state}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def valid_key(key: str) -> bool:
    return _KEY.match(key) is not None


def _row_to_item(r: sqlite3.Row) -> Item:
    told_at: str | None = r["told_at"]
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
) -> Change:
    """Record a report. ``new``: nothing told yet; ``changed``: differs from what was told.

    ``same`` means the owner already heard exactly this, so there is nothing to say. The row
    is rewritten either way, which also extends its expiry and reopens a cleared item.
    """
    if not valid_key(key):
        raise ValueError(f"key must be 1..{KEY_MAX} characters of [a-z0-9/._:-]: {key!r}")
    if urgency not in URGENCIES:
        raise ValueError(f"urgency must be one of {URGENCIES}: {urgency!r}")
    if not 1 <= ttl_s <= MAX_TTL_S:
        raise ValueError(f"ttl_s must be 1..{MAX_TTL_S} seconds")
    title = title.strip()[:TITLE_MAX]
    fp = fingerprint(title, state)
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
    if told is None:
        return "new"
    return "same" if told == fp else "changed"


_URGENCY_ORDER = "CASE urgency WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"


def pending(
    conn: sqlite3.Connection,
    urgencies: Collection[Urgency],
    now: datetime,
    sources_excluded: Collection[str] = (),
) -> list[Item]:
    """Unexpired items at ``urgencies`` whose fingerprint isn't what the owner last heard.

    Cleared items are included (their last word is still owed) until they are told.
    """
    wanted = list(urgencies)
    skipped = list(sources_excluded)
    if not wanted:
        return []
    # Only placeholders and module constants are interpolated; values are bound.
    sql = (
        "SELECT * FROM pulse_items"  # noqa: S608
        f" WHERE urgency IN ({','.join('?' * len(wanted))})"
        " AND expires_at > ? AND (told_fingerprint IS NULL OR told_fingerprint != fingerprint)"
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
    """Record that the owner heard these versions (the fingerprints in ``items``)."""
    with atomic(conn):
        for item in items:
            conn.execute(
                "UPDATE pulse_items SET told_fingerprint = ?, told_at = ?"
                " WHERE source = ? AND key = ?",
                (item.fingerprint, iso(at), item.source, item.key),
            )


def prune(conn: sqlite3.Connection, now: datetime) -> int:
    """Delete items cleared or expired for more than 7 days. Returns how many."""
    cutoff = iso(now - PRUNE_AFTER)
    with atomic(conn):
        cur = conn.execute(
            "DELETE FROM pulse_items WHERE expires_at < ? OR (cleared = 1 AND updated_at < ?)",
            (cutoff, cutoff),
        )
    return cur.rowcount

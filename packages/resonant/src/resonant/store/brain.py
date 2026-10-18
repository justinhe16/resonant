"""The brain's conversation store (migration 015). Plain functions over a connection.

- ``brain_summaries``: one running summary per principal and the last message event folded
  into it (``through_event_id``). ``save_summary`` is a compare-and-set on that cursor, so two
  folds that race can't fold the same exchanges twice.
- ``thread_after``: a principal's message events after the cursor (and before the current
  message), each with the checkpoint of the task it started, like ``tasks.principal_thread``.
- ``expired_since``: approvals that expired in a time window, for "since we last talked".

Events are ordered by rowid, as in ``tasks.principal_thread``. Callers own transactions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from resonant.store.db import iso
from resonant.store.tasks import _json, _row_to_event  # pyright: ignore[reportPrivateUsage]
from resonant_sdk import Event

SummarySource = Literal["model", "fallback"]


@dataclass(frozen=True)
class Summary:
    principal: str
    summary: str
    through_event_id: str
    folded: int  # exchanges folded in, in all
    source: SummarySource
    updated_at: datetime


def get_summary(conn: sqlite3.Connection, principal: str) -> Summary | None:
    r = conn.execute("SELECT * FROM brain_summaries WHERE principal = ?", (principal,)).fetchone()
    if r is None:
        return None
    return Summary(
        principal=r["principal"],
        summary=r["summary"],
        through_event_id=r["through_event_id"],
        folded=r["folded"],
        source=r["source"],
        updated_at=datetime.fromisoformat(r["updated_at"]),
    )


def save_summary(
    conn: sqlite3.Connection,
    principal: str,
    *,
    summary: str,
    through_event_id: str,
    added: int,
    source: SummarySource,
    expect_through: str | None,
    now: datetime,
) -> bool:
    """Store the new summary if the cursor is still ``expect_through`` (None: no row yet).
    False when another fold got there first (nothing written)."""
    if expect_through is None:
        cur = conn.execute(
            """INSERT INTO brain_summaries
                 (principal, summary, through_event_id, folded, source, updated_at)
               VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (principal) DO NOTHING""",
            (principal, summary, through_event_id, added, source, iso(now)),
        )
    else:
        cur = conn.execute(
            """UPDATE brain_summaries
               SET summary = ?, through_event_id = ?, folded = folded + ?, source = ?,
                   updated_at = ?
               WHERE principal = ? AND through_event_id = ?""",
            (summary, through_event_id, added, source, iso(now), principal, expect_through),
        )
    return cur.rowcount == 1


def thread_after(
    conn: sqlite3.Connection,
    principal: str,
    *,
    source: str,
    event_type: str,
    after_event_id: str | None,
    before_event_id: str | None,
    limit: int,
    newest: bool,
) -> list[tuple[Event, dict[str, Any] | None]]:
    """``principal``'s events of one source and type strictly after ``after_event_id`` (None:
    from the start; an id no longer stored also counts from the start) and strictly before
    ``before_event_id`` (None: up to the latest). At most ``limit`` of them: the newest when
    ``newest``, else the oldest. Always oldest first. Each with the checkpoint of the task it
    started (None if it started none)."""
    if limit <= 0:
        return []
    order = "DESC" if newest else "ASC"
    rows = conn.execute(
        f"""SELECT e.*, t.checkpoint AS origin_checkpoint FROM events e
            LEFT JOIN tasks t ON t.origin_event_id = e.id
            WHERE e.principal = ? AND e.source = ? AND e.type = ?
              AND e.rowid > COALESCE((SELECT rowid FROM events WHERE id = ?), 0)
              AND (? IS NULL OR e.rowid < (SELECT rowid FROM events WHERE id = ?))
            ORDER BY e.rowid {order} LIMIT ?""",  # noqa: S608 - order is a fixed keyword
        (principal, source, event_type, after_event_id, before_event_id, before_event_id, limit),
    ).fetchall()
    if newest:
        rows = list(reversed(rows))
    out: list[tuple[Event, dict[str, Any] | None]] = []
    for r in rows:
        cp: Any = _json(r["origin_checkpoint"])
        out.append((_row_to_event(r), cast(dict[str, Any], cp) if isinstance(cp, dict) else None))
    return out


@dataclass(frozen=True)
class Expired:
    summary: str  # code-built (gate/summary.py), never model text
    requested_by: str | None
    approvers: tuple[str, ...]
    expired_at: datetime


def expired_since(conn: sqlite3.Connection, since: datetime, until: datetime) -> list[Expired]:
    """Approvals that expired in ``(since, until]``, oldest first."""
    rows = conn.execute(
        """SELECT summary, requested_by, approvers, decided_at FROM approvals
           WHERE status = 'expired' AND decided_at > ? AND decided_at <= ?
           ORDER BY decided_at""",
        (iso(since), iso(until)),
    ).fetchall()
    out: list[Expired] = []
    for r in rows:
        approvers: Any = json.loads(r["approvers"] or "[]")
        out.append(
            Expired(
                summary=r["summary"],
                requested_by=r["requested_by"],
                approvers=tuple(str(a) for a in cast(list[Any], approvers))
                if isinstance(approvers, list)
                else (),
                expired_at=datetime.fromisoformat(r["decided_at"]),
            )
        )
    return out

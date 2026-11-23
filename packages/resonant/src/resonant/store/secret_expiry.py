"""Secret expiry rows (migration 035, PER-313). Plain functions over a connection.

One row per Keychain ref: when it expires, where that date came from (``owner``,
``assumed`` or ``probe``), when the row was written and an optional note. Metadata only: a
secret's value is never read for this and never stored here. Callers own transactions; every
function here is one statement.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from resonant.secrets import validate_ref
from resonant.store.db import iso

Source = Literal["owner", "assumed", "probe"]
SOURCES: tuple[Source, ...] = ("owner", "assumed", "probe")


@dataclass(frozen=True)
class ExpiryRow:
    ref: str
    expires_at: datetime  # UTC
    source: Source
    set_at: datetime  # UTC
    note: str | None = None


def _row(r: sqlite3.Row) -> ExpiryRow:
    return ExpiryRow(
        ref=r["ref"],
        expires_at=datetime.fromisoformat(r["expires_at"]),
        source=cast(Source, r["source"]),
        set_at=datetime.fromisoformat(r["set_at"]),
        note=r["note"],
    )


def get(conn: sqlite3.Connection, ref: str) -> ExpiryRow | None:
    r = conn.execute("SELECT * FROM secret_expiry WHERE ref = ?", (ref,)).fetchone()
    return None if r is None else _row(r)


def all(conn: sqlite3.Connection) -> list[ExpiryRow]:
    """Every row, by ref."""
    return [_row(r) for r in conn.execute("SELECT * FROM secret_expiry ORDER BY ref")]


def upsert(
    conn: sqlite3.Connection,
    ref: str,
    *,
    expires_at: datetime,
    source: Source,
    set_at: datetime,
    note: str | None = None,
) -> ExpiryRow:
    """Write (or replace) ``ref``'s row. Both times must be timezone-aware."""
    if expires_at.utcoffset() is None or set_at.utcoffset() is None:
        raise ValueError("secret expiry times must be timezone-aware")
    if source not in SOURCES:
        raise ValueError(f"bad expiry source {source!r}")
    validate_ref(ref)
    conn.execute(
        "INSERT INTO secret_expiry (ref, expires_at, source, set_at, note) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (ref) DO UPDATE SET expires_at = excluded.expires_at, "
        "source = excluded.source, set_at = excluded.set_at, note = excluded.note",
        (ref, iso(expires_at), source, iso(set_at), note),
    )
    row = get(conn, ref)
    assert row is not None
    return row


def clear(conn: sqlite3.Connection, ref: str) -> bool:
    """Remove ``ref``'s row. False if there was none."""
    return conn.execute("DELETE FROM secret_expiry WHERE ref = ?", (ref,)).rowcount > 0

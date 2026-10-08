"""SQLite access: connection setup, migrations, and small shared helpers.

Migrations are numbered ``NNN_name.sql`` files applied in order and tracked with
``PRAGMA user_version``. There is no ORM: repositories are plain functions over a
``sqlite3.Connection``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def now_iso() -> str:
    return iso(utcnow())


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with WAL, FK enforcement, and a busy timeout.

    ``isolation_level=None`` puts the connection in autocommit mode. Callers group writes
    with :func:`transaction`.
    """
    if isinstance(path, Path):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class transaction:
    """``with transaction(conn): ...`` runs the block in BEGIN IMMEDIATE / COMMIT."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")


def _migrations() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for entry in resources.files("resonant.store.migrations").iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.read_text()))
    found.sort()
    versions = [v for v, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migrations must be numbered 001..N without gaps, got {versions}")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Idempotent. Returns the resulting schema version."""
    current = schema_version(conn)
    for version, sql in _migrations():
        if version <= current:
            continue
        # executescript commits any open transaction, so each migration carries its own.
        conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        current = version
    return current


def open_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


# --- kv -------------------------------------------------------------------------------


def kv_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value"])


def kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value), now_iso()),
    )

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
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class transaction:
    """``with transaction(conn): ...`` runs the block in BEGIN IMMEDIATE / COMMIT.

    Not reentrant: nesting raises instead of silently joining the outer transaction.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        if self.conn.in_transaction:
            raise RuntimeError("transaction() is not reentrant")
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        if exc_type is not None:
            self.conn.execute("ROLLBACK")
            return
        try:
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise


class atomic:
    """Like :class:`transaction`, but joins an enclosing transaction via a SAVEPOINT.

    For small helpers (audit, spans, gate and executor bookkeeping) that may be called
    both standalone and from inside a caller's transaction.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._nested = False

    def __enter__(self) -> sqlite3.Connection:
        self._nested = self.conn.in_transaction
        self.conn.execute("SAVEPOINT resonant_atomic" if self._nested else "BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        if self._nested:
            if exc_type is not None:
                self.conn.execute("ROLLBACK TO resonant_atomic")
            self.conn.execute("RELEASE resonant_atomic")
            return
        if exc_type is not None:
            self.conn.execute("ROLLBACK")
            return
        try:
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise


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


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into complete statements (handles ``;`` inside strings)."""
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement in migration: {buf.strip()[:80]!r}")
    return [s for s in statements if s]


def migrate(conn: sqlite3.Connection, migrations: list[tuple[int, str]] | None = None) -> int:
    """Apply pending migrations atomically. Idempotent and safe across processes.

    Each migration runs in its own BEGIN IMMEDIATE transaction. ``user_version`` is
    re-read *after* the write lock is held, so two processes starting together never
    apply the same migration twice. Any failure rolls back that migration completely.
    """
    for version, sql in migrations if migrations is not None else _migrations():
        if version <= schema_version(conn):
            continue
        statements = _split_statements(sql)
        with transaction(conn):
            if version <= schema_version(conn):
                continue  # another process applied it while we waited for the lock
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {version:d}")
    return schema_version(conn)


def open_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


def latest_version() -> int:
    return max((v for v, _ in _migrations()), default=0)


class StoreUnavailableError(RuntimeError):
    pass


def open_existing(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open an existing, fully migrated database without creating or migrating anything.

    For tools that must fail closed (job-guard) or must not mutate state (status).
    Raises StoreUnavailableError if the file is missing or its schema is behind.
    """
    if not path.is_file():
        raise StoreUnavailableError(f"{path} does not exist")
    mode = "ro" if readonly else "rw"
    try:
        conn = sqlite3.connect(
            f"file:{path}?mode={mode}", uri=True, isolation_level=None, check_same_thread=False
        )
    except sqlite3.Error as e:
        raise StoreUnavailableError(f"cannot open {path}: {e}") from e
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if not readonly:
        conn.execute("PRAGMA foreign_keys=ON")
    if (version := schema_version(conn)) < latest_version():
        conn.close()
        raise StoreUnavailableError(f"{path} schema v{version} < v{latest_version()}")
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

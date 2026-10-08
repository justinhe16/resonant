"""Test helper: build a minimal Messages ``chat.db`` with the real table and column names.

Only the tables and columns the reader touches (plus a few neighbours, so the shape looks
like the real thing) are created. Every test of the reader uses this instead of the real
``~/Library/Messages/chat.db``::

    chat = FakeChatDB(tmp_path / "chat.db")
    chat.add_message(handle="+15551234567", text="hi")
    chat.add_message(handle="me@icloud.com", attributed_body=blob)   # text NULL
    chat.add_message(handle="+15551234567", text="hi all", group=True)

Conventions mirrored from the real database:
- ``message.date`` is nanoseconds since 2001-01-01 UTC (the Apple epoch).
- ``chat.style`` is 45 for a 1:1 chat and 43 for a group chat. A 1:1 chat's guid is
  ``iMessage;-;<handle>``; a group's is ``iMessage;+;chat<digits>``.
- ``associated_message_type`` is 0 for ordinary messages and 2000+ for tapbacks.
- The database is in WAL mode, like the real one.

``omit`` drops columns to simulate an unexpected schema from a future macOS.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from resonant_sdk import new_id

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
STYLE_DIRECT = 45
STYLE_GROUP = 43
TAPBACK_LOVE = 2000

_SCHEMA: dict[str, list[tuple[str, str]]] = {
    "handle": [
        ("ROWID", "INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE"),
        ("id", "TEXT NOT NULL"),
        ("country", "TEXT"),
        ("service", "TEXT NOT NULL"),
        ("uncanonicalized_id", "TEXT"),
    ],
    "chat": [
        ("ROWID", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("guid", "TEXT UNIQUE NOT NULL"),
        ("style", "INTEGER"),
        ("state", "INTEGER"),
        ("chat_identifier", "TEXT"),
        ("service_name", "TEXT"),
        ("display_name", "TEXT"),
    ],
    "message": [
        ("ROWID", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("guid", "TEXT UNIQUE NOT NULL"),
        ("text", "TEXT"),
        ("handle_id", "INTEGER DEFAULT 0"),
        ("subject", "TEXT"),
        ("attributedBody", "BLOB"),
        ("type", "INTEGER DEFAULT 0"),
        ("service", "TEXT"),
        ("account", "TEXT"),
        ("date", "INTEGER"),
        ("date_read", "INTEGER"),
        ("date_delivered", "INTEGER"),
        ("is_from_me", "INTEGER DEFAULT 0"),
        ("cache_has_attachments", "INTEGER DEFAULT 0"),
        ("associated_message_guid", "TEXT DEFAULT NULL"),
        ("associated_message_type", "INTEGER DEFAULT 0"),
    ],
    "chat_message_join": [
        ("chat_id", "INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE"),
        ("message_id", "INTEGER REFERENCES message (ROWID) ON DELETE CASCADE"),
        ("message_date", "INTEGER DEFAULT 0"),
    ],
    "chat_handle_join": [
        ("chat_id", "INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE"),
        ("handle_id", "INTEGER REFERENCES handle (ROWID) ON DELETE CASCADE"),
    ],
}


def to_apple_ns(dt: datetime) -> int:
    """Convert an aware datetime to ``message.date`` (ns since 2001-01-01 UTC)."""
    delta = dt - APPLE_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


class FakeChatDB:
    """A writable stand-in for ``chat.db``. The reader under test opens it read-only."""

    def __init__(self, path: Path, *, omit: Mapping[str, set[str]] | None = None) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        omit = omit or {}
        for table, columns in _SCHEMA.items():
            cols = ", ".join(f"{c} {d}" for c, d in columns if c not in omit.get(table, set()))
            self.conn.execute(f"CREATE TABLE {table} ({cols})")
        self._handles: dict[tuple[str, str], int] = {}
        self._chats: dict[str, int] = {}
        self._group_seq = 0

    def close(self) -> None:
        self.conn.close()

    def handle(self, handle: str, service: str = "iMessage") -> int:
        key = (handle, service)
        if key not in self._handles:
            cur = self.conn.execute(
                "INSERT INTO handle (id, country, service, uncanonicalized_id) "
                "VALUES (?, 'us', ?, ?)",
                (handle, service, handle),
            )
            self._handles[key] = int(cur.lastrowid or 0)
        return self._handles[key]

    def chat(self, guid: str, *, style: int, handles: list[int], service: str = "iMessage") -> int:
        if guid not in self._chats:
            cur = self.conn.execute(
                "INSERT INTO chat (guid, style, state, chat_identifier, service_name) "
                "VALUES (?, ?, 3, ?, ?)",
                (guid, style, guid.rsplit(";", 1)[-1], service),
            )
            chat_id = int(cur.lastrowid or 0)
            self.conn.executemany(
                "INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
                [(chat_id, h) for h in handles],
            )
            self._chats[guid] = chat_id
        return self._chats[guid]

    def add_message(
        self,
        *,
        handle: str,
        text: str | None = None,
        attributed_body: bytes | None = None,
        is_from_me: bool = False,
        service: str = "iMessage",
        associated_message_type: int = 0,
        group: bool = False,
        in_chat: bool = True,
        at: datetime | None = None,
        guid: str | None = None,
    ) -> int:
        """Insert one message (and its handle/chat as needed). Returns its ROWID."""
        handle_id = self.handle(handle, service)
        date = to_apple_ns(at or datetime.now(UTC))
        cur = self.conn.execute(
            "INSERT INTO message (guid, text, handle_id, attributedBody, service, account, date, "
            "is_from_me, associated_message_type, associated_message_guid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guid or new_id(),
                text,
                handle_id,
                attributed_body,
                service,
                "e:resonant@example.com",
                date,
                int(is_from_me),
                associated_message_type,
                "p:0/OTHER" if associated_message_type else None,
            ),
        )
        rowid = int(cur.lastrowid or 0)
        if in_chat:
            if group:
                self._group_seq += 1
                chat_id = self.chat(
                    f"{service};+;chat{self._group_seq:012d}",
                    style=STYLE_GROUP,
                    handles=[handle_id, self.handle("+15550000000", service)],
                    service=service,
                )
            else:
                prefix = "iMessage" if service == "iMessage" else "SMS"
                chat_id = self.chat(
                    f"{prefix};-;{handle}", style=STYLE_DIRECT, handles=[handle_id], service=service
                )
            self.conn.execute(
                "INSERT INTO chat_message_join (chat_id, message_id, message_date) "
                "VALUES (?, ?, ?)",
                (chat_id, rowid, date),
            )
        return rowid

    def max_rowid(self) -> int:
        return int(self.conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0])

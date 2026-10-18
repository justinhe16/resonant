"""Attachments on an owner's iMessage (BR-7, docs/brain-plan.md D10).

chat.db keeps each attachment in ``attachment`` (``filename`` is the file under
``~/Library/Messages/Attachments``, written ``~/...``; ``mime_type`` and ``uti`` may be
NULL) and links it to its message through ``message_attachment_join``. The reader calls
:func:`read_attachments` for a message from the owner and puts the result in the event's
``payload.attachments``; nothing here opens or copies a file (the files stay where Messages
put them, read-only; ``runners/media.py`` and the ask runner check and copy them later).

Only what Claude or whisper can use is kept, at most ``MAX_ATTACHMENTS`` per message:

- ``image``: JPEG, PNG, GIF, WebP, HEIC/HEIF (HEIC is converted to JPEG for Claude),
- ``pdf``,
- ``voice``: Messages' audio messages (``.caf``) and other audio (``.m4a``, ``.mp3``, ...).

Anything else (link previews, stickers, contact cards, videos) and anything over
``MAX_BYTES`` is skipped and counted in ``payload.attachments_skipped``. Outgoing attachments
never count (the reader only looks at incoming rows anyway). The tables are optional: a
chat.db without them (an older or future macOS) just yields no attachments.

PII: an attachment's path and name are in the stored event (as the text is), never in logs
or audit rows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

Kind = Literal["image", "pdf", "voice"]
MAX_ATTACHMENTS = 4  # runners.ask.ATTACH_MAX
MAX_BYTES = 20 * 1024 * 1024  # runners.ask.ATTACH_BYTES
NAME_MAX = 120

ATTACHMENT_COLUMNS: dict[str, tuple[str, ...]] = {
    "attachment": ("filename", "mime_type", "transfer_name", "total_bytes"),
    "message_attachment_join": ("message_id", "attachment_id"),
}

_QUERY = """
SELECT a.ROWID AS rowid, a.filename AS filename, a.mime_type AS mime, a.uti AS uti,
       a.transfer_name AS name, a.total_bytes AS bytes
FROM message_attachment_join maj
JOIN attachment a ON a.ROWID = maj.attachment_id
WHERE maj.message_id = ?
ORDER BY a.ROWID
"""

_BY_SUFFIX: dict[str, Kind] = {
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".gif": "image",
    ".webp": "image",
    ".heic": "image",
    ".heif": "image",
    ".pdf": "pdf",
    ".caf": "voice",
    ".m4a": "voice",
    ".mp3": "voice",
    ".aac": "voice",
    ".amr": "voice",
    ".wav": "voice",
}
_BY_MIME: dict[str, Kind] = {
    "image/jpeg": "image",
    "image/png": "image",
    "image/gif": "image",
    "image/webp": "image",
    "image/heic": "image",
    "image/heif": "image",
    "application/pdf": "pdf",
    "audio/x-caf": "voice",
    "audio/mp4": "voice",
    "audio/x-m4a": "voice",
    "audio/m4a": "voice",
    "audio/mpeg": "voice",
    "audio/aac": "voice",
    "audio/amr": "voice",
    "audio/wav": "voice",
    "audio/x-wav": "voice",
}


@dataclass(frozen=True)
class Attachment:
    path: str  # absolute (``~`` expanded)
    kind: Kind
    name: str  # the sender's file name, cleaned and clipped
    bytes: int | None

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "name": self.name, "bytes": self.bytes}


def classify(filename: str, mime: str | None) -> Kind | None:
    """The kind of a file by its MIME type, else its suffix; None for anything we can't use."""
    if mime:
        found = _BY_MIME.get(mime.lower().split(";", 1)[0].strip())
        if found is not None:
            return found
    return _BY_SUFFIX.get(Path(filename).suffix.lower())


def _clean_name(raw: object, fallback: str) -> str:
    name = raw if isinstance(raw, str) and raw.strip() else fallback
    kept = "".join(c if c.isprintable() and c not in "/\\" else " " for c in name)
    return " ".join(kept.split())[:NAME_MAX] or "attachment"


def has_tables(chat: sqlite3.Connection) -> bool:
    """Whether chat.db has the attachment tables and columns this module reads."""
    for table, columns in ATTACHMENT_COLUMNS.items():
        have = {str(r[1]) for r in chat.execute(f"PRAGMA table_info({table})")}
        if not set(columns) <= have:
            return False
    return True


def read_attachments(
    chat: sqlite3.Connection, message_rowid: int, *, home: Path | None = None
) -> tuple[list[Attachment], int]:
    """``(usable attachments, how many were skipped)`` for one message."""
    kept: list[Attachment] = []
    skipped = 0
    for row in chat.execute(_QUERY, (message_rowid,)).fetchall():
        filename: object = row["filename"]
        if not isinstance(filename, str) or not filename.strip():
            skipped += 1
            continue
        if filename.startswith("~/") and home is not None:
            path = home / filename[2:]
        else:
            path = Path(filename).expanduser()
        kind = classify(filename, row["mime"])
        size: object = row["bytes"]
        size_n = size if isinstance(size, int) and size >= 0 else None
        if (
            kind is None
            or not path.is_absolute()
            or (size_n is not None and size_n > MAX_BYTES)
            or len(kept) >= MAX_ATTACHMENTS
        ):
            skipped += 1
            continue
        kept.append(Attachment(str(path), kind, _clean_name(row["name"], path.name), size_n))
    return kept, skipped


def from_payload(payload: dict[str, Any]) -> list[Attachment]:
    """The attachments an event carries (as :meth:`Attachment.to_json` wrote them); bad
    entries are dropped."""
    raw: object = payload.get("attachments")
    out: list[Attachment] = []
    if not isinstance(raw, list):
        return out
    for entry in cast(list[Any], raw)[:MAX_ATTACHMENTS]:
        if not isinstance(entry, dict):
            continue
        d = cast(dict[str, Any], entry)
        path, kind, name, size = d.get("path"), d.get("kind"), d.get("name"), d.get("bytes")
        if not isinstance(path, str) or not Path(path).is_absolute():
            continue
        if kind not in ("image", "pdf", "voice"):
            continue
        out.append(
            Attachment(
                path,
                kind,
                name if isinstance(name, str) else Path(path).name,
                size if isinstance(size, int) else None,
            )
        )
    return out


__all__ = [
    "MAX_ATTACHMENTS",
    "MAX_BYTES",
    "Attachment",
    "Kind",
    "classify",
    "from_payload",
    "has_tables",
    "read_attachments",
]

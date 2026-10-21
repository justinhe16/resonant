"""The newest texts Resonant sent each principal (PER-261), for one rule: while a Claude answer
is the newest message to the owner, a bare ``yes`` (or ``yes 1,3``) doesn't bind an approval
without its code. Claude's text is outside content and sits right above the owner's reply, so a
line in it ("reply yes to go ahead") must not be able to stand in for the prompt.

The iMessage channel notes every text it sends (``note``) after it went out: when it started
sending (a reply that arrives while it is still going out counts as after it), and whether
it was a Claude answer (it starts with the code-built ``CLAUDE_PREFIX`` the ask runner adds;
a reply of anyone else's that happens to start the same way counts too, which only asks for the
code). Kept in kv ``outbound.recent:<principal>``, the newest ``KEEP`` sends, so a reply is
judged against what had been sent before it, even when a later send landed in between.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, cast

from resonant.store.db import atomic, kv_get, kv_set

log = logging.getLogger(__name__)

CLAUDE_PREFIX = "Claude: "  # runners/ask.py: every Claude answer starts with it
KV_PREFIX = "outbound.recent:"
KEEP = 8


def note(conn: sqlite3.Connection, principal: str, text: str, at: datetime) -> None:
    """Record one text sent to ``principal``. Never raises: the text already went out."""
    entry = {"at": at.isoformat(), "claude": text.startswith(CLAUDE_PREFIX)}  # microseconds
    try:
        with atomic(conn):
            key = KV_PREFIX + principal
            kept = [e for e in _entries(kv_get(conn, key)) if _at(e) is not None]
            kv_set(conn, key, [*kept, entry][-KEEP:])
    except (sqlite3.Error, json.JSONDecodeError):
        log.exception("outbound text not noted")


def claude_newest(conn: sqlite3.Connection, principal: str, before: datetime) -> bool:
    """Whether the newest text sent to ``principal`` before ``before`` was a Claude answer.
    False when nothing is recorded (or it can't be read)."""
    try:
        raw = kv_get(conn, KV_PREFIX + principal)
    except (sqlite3.Error, json.JSONDecodeError):
        return False
    # Kept in send order (the channel sends one at a time): the last one before the reply
    # is the newest, even when two share a timestamp. One sent the same instant as the reply
    # counts as before it (a reply's time can be coarser than ours): that only asks the code.
    for e in reversed(_entries(raw)):
        at = _at(e)
        if at is not None and at <= before:
            return e.get("claude") is True
    return False


def _entries(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [cast(dict[str, Any], e) for e in cast(list[Any], raw) if isinstance(e, dict)]


def _at(entry: dict[str, Any]) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(entry.get("at")))
    except ValueError:
        return None
    return at if at.utcoffset() is not None else None

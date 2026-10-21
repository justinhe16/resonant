"""The newest texts Resonant sent each principal (PER-261), for one rule: a bare ``yes`` (or
``yes 1,3``) doesn't bind an approval without its code when a Claude answer went out after
that approval's prompt (or its reminder). Claude's text is outside content and sits right above
the owner's reply, so a line in it ("reply yes to go ahead") must not stand in for the prompt.

The iMessage channel notes every text it sends (``note``) after it went out, in send order (it
sends one at a time): when it started, its dedupe key (which names a prompt or a reminder), and
whether it was a Claude answer (it starts with the code-built ``CLAUDE_PREFIX`` the ask runner
adds; anything else that starts the same way counts too, which only asks for the code). Kept in
kv ``outbound.recent:<principal>``, the newest ``KEEP`` sends. Order decides, not the clock: two
sends a millisecond apart, or an ack that lands after the answer, can't flip the rule. The
last Claude answer is also kept apart (``CLAUDE_KV``): a prompt that fell out of a full window
counts as before it (fails closed: the code is asked). The channel notes a text before its
first chunk goes out, so a delivered answer is never missing from the record.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any, cast

from resonant.store.db import atomic, kv_get, kv_set

log = logging.getLogger(__name__)

CLAUDE_PREFIX = "Claude: "  # runners/ask.py: every Claude answer starts with it
KV_PREFIX = "outbound.recent:"
KEEP = 32
CLAUDE_KV = "outbound.claude:"  # the last Claude answer's seq and time, kept apart from the window
SKEW = timedelta(seconds=1)  # a reply's time vs ours (chat.db's clock, milliseconds)


def reminder_key(approval_id: str, principal: str) -> str:
    """The send dedupe key of one approval's expiry reminder to one approver
    (``runners.approve.remind_due``)."""
    return f"approval:{approval_id}:remind:{principal}"


def note(
    conn: sqlite3.Connection, principal: str, text: str, at: datetime, key: str | None = None
) -> None:
    """Record one text sent to ``principal`` (``key``: its dedupe key). Never raises: the
    text already went out."""
    claude = text.startswith(CLAUDE_PREFIX)
    try:
        with atomic(conn):
            window = KV_PREFIX + principal
            kept = [e for e in _entries(kv_get(conn, window)) if _at(e) is not None]
            seq = max((_seq(e) for e in kept), default=0) + 1
            entry = {"seq": seq, "at": at.isoformat(), "claude": claude, "key": key}
            kv_set(conn, window, [*kept, entry][-KEEP:])
            if claude:
                kv_set(conn, CLAUDE_KV + principal, {"seq": seq, "at": at.isoformat()})
    except (sqlite3.Error, json.JSONDecodeError):
        log.exception("outbound text not noted")


def claude_since(
    conn: sqlite3.Connection, principal: str, prompts: Collection[str], before: datetime
) -> bool:
    """Whether a Claude answer went out to ``principal`` after the last of the ``prompts``
    (dedupe keys: an approval's prompt and its reminder) and before ``before`` (a reply's time,
    from chat.db's own clock and coarser than ours: a send up to ``SKEW`` after it counts as
    before it, which only asks for the code). A prompt
    older than every kept send counts as before them all. False when nothing is recorded or it
    can't be read."""
    try:
        raw = kv_get(conn, KV_PREFIX + principal)
    except (sqlite3.Error, json.JSONDecodeError):
        return False
    entries = _entries(raw)
    last = max((i for i, e in enumerate(entries) if e.get("key") in prompts), default=-1)
    for e in entries[last + 1 :]:
        at = _at(e)
        if e.get("claude") is True and at is not None and at <= before + SKEW:
            return True
    if last >= 0 or len(entries) < KEEP:
        return False
    # The prompt fell out of the window (or was never noted): fail closed when a Claude
    # answer went out before the reply at all (``CLAUDE_KV`` outlives the window).
    try:
        mark: Any = kv_get(conn, CLAUDE_KV + principal)
    except (sqlite3.Error, json.JSONDecodeError):
        return True
    at = _at(cast(dict[str, Any], mark)) if isinstance(mark, dict) else None
    return at is not None and at <= before + SKEW


def _seq(entry: dict[str, Any]) -> int:
    raw = entry.get("seq")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


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

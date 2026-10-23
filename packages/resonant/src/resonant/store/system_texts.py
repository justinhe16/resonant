"""What Resonant itself texted a principal (PER-268, migration 019; 024 adds the pulse).

Code-built texts the owner got outside a reply to their message (an approval prompt, the
result of an approved call, a run's end text; since PER-274 the hourly pulse and the
briefing, ``pulse`` and ``briefing``) are recorded here after they are sent, so the
brains can see them: qwen's history shows them between the exchanges as system turns
(``brain.history.replayed``) and the Claude brain gets the new ones at the top of its next
turn (``runners.ask``). ``ref`` is the send's dedupe key, so a resend records nothing new.
The text is code-built from cleaned parts and is cleaned again here (``notify.clean_text``)
and clipped (a pulse or briefing is kept whole, ``LONG_MAX``: the brains' "Already told"
leaves out what one shown in the transcript said); it doesn't taint a turn, unless it came
from a tainted task or outside content (``outside``: an approval prompt shows the args a
tainted turn chose; a run's outcome, a pulse or briefing naming workers), which taints as
that task's reply would. Rows older than ``KEEP`` are pruned on write.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from resonant.extensions.notify import clean_text
from resonant.store.db import atomic, iso, utcnow

log = logging.getLogger(__name__)

Kind = Literal["approval", "result", "run", "pulse", "briefing"]
TEXT_MAX = 400
LONG_KINDS: frozenset[Kind] = frozenset({"pulse", "briefing"})
LONG_MAX = 1200  # a pulse or briefing (1,000 chars, ``pulse_job.MAX_CHARS``) with its `` / ``s
KEEP = timedelta(days=7)


@dataclass(frozen=True)
class SystemText:
    id: int
    principal: str
    kind: Kind
    text: str
    task_id: str | None
    sent_at: datetime
    outside: bool = False  # from a tainted task: a turn that sees it is tainted


def tainted(checkpoint: dict[str, Any]) -> bool:
    """Whether a task's texts came after outside content: the task is tainted, or (BA-1) an
    item of its batch was pinned tainted."""
    if checkpoint.get("tainted") is True:
        return True
    batch: Any = checkpoint.get("batch")
    items = cast(list[Any], batch) if isinstance(batch, list) else []
    return any(isinstance(i, dict) and cast(dict[str, Any], i).get("tainted") is True
               for i in items)  # fmt: skip


def flat(text: str, limit: int = TEXT_MAX) -> str:
    """One line per text line, each cleaned, joined with `` / `` and clipped to ``limit``."""
    lines = [clean_text(line) for line in text.splitlines()]
    out = " / ".join(line for line in lines if line)
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def max_chars(kind: str | None) -> int:
    """How much of a ``kind`` text is kept: a pulse or briefing whole, else ``TEXT_MAX``."""
    return LONG_MAX if kind in LONG_KINDS else TEXT_MAX


def record(
    conn: sqlite3.Connection,
    principal: str,
    kind: Kind,
    text: str,
    *,
    ref: str,
    task_id: str | None = None,
    now: datetime | None = None,
    outside: bool = False,
) -> None:
    """Record a text Resonant sent ``principal`` (once per ``ref``). ``outside``: it came
    from a tainted task (its args were chosen after outside content), so the turn that sees
    it is tainted as after that task's reply (BR-10b). Never raises: the text already went
    out, and the brains' history is best effort."""
    text = flat(text, max_chars(kind))
    if not text:
        return
    at = now or utcnow()
    try:
        with atomic(conn):
            conn.execute(
                """INSERT INTO system_texts (principal, kind, text, ref, task_id, outside, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (ref) DO NOTHING""",
                (principal, kind, text, ref, task_id, int(outside), iso(at)),
            )
            conn.execute("DELETE FROM system_texts WHERE sent_at < ?", (iso(at - KEEP),))
    except sqlite3.Error:
        log.exception("system text not recorded")


def _row(r: sqlite3.Row) -> SystemText:
    return SystemText(r["id"], r["principal"], r["kind"], r["text"], r["task_id"],
                      datetime.fromisoformat(r["sent_at"]), bool(r["outside"]))  # fmt: skip


def between(
    conn: sqlite3.Connection,
    principal: str,
    *,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[SystemText]:
    """``principal``'s texts sent in ``(after, before)`` (None: open), the newest ``limit``,
    oldest first."""
    rows = conn.execute(
        """SELECT * FROM system_texts WHERE principal = ?
             AND (? IS NULL OR sent_at > ?) AND (? IS NULL OR sent_at < ?)
           ORDER BY id DESC LIMIT ?""",
        (principal, *([None] * 2 if after is None else [iso(after)] * 2),
         *([None] * 2 if before is None else [iso(before)] * 2), limit),
    ).fetchall()  # fmt: skip
    return [_row(r) for r in reversed(rows)]


def after_id(
    conn: sqlite3.Connection, principal: str, last_id: int, *, since: datetime, limit: int
) -> list[SystemText]:
    """``principal``'s texts with an id over ``last_id`` sent after ``since``, the newest
    ``limit``, oldest first."""
    rows = conn.execute(
        """SELECT * FROM system_texts WHERE principal = ? AND id > ? AND sent_at > ?
           ORDER BY id DESC LIMIT ?""",
        (principal, last_id, iso(since), limit),
    ).fetchall()
    return [_row(r) for r in reversed(rows)]


def last_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) FROM system_texts").fetchone()
    return int(row[0] or 0)

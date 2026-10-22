"""The news check, built from code (PER-256): "anything new?", "what do you need from me?".

The local model only routes now, so it never writes an answer about Resonant's state. When
the owner asks for news with no topic, this module answers from the same records the old
briefing read (PER-270's told state): what changed since the owner's previous message and
they weren't texted already (``briefing.since_notes``: runs that ended, approvals that
expired, worker questions; a run whose end was texted is left out), then what waits on them
now (open approvals by their code-built summary, open worker questions). Nothing to say is
``NEWS_NOTHING``. A store that can't be read is None: the turn goes to Claude instead, never
to a guess.

Pure apart from the reads. Never contains an approval code.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from resonant.brain.briefing import last_talked, run_name, since_notes
from resonant.brain.text import flat
from resonant.config import Settings
from resonant.gate.policy import change_line
from resonant.store import approvals as ap
from resonant.store import questions, runs

NEWS_NOTHING = "Nothing new, and nothing waits on you."
QUESTION_CHARS = 160
WAITING_MAX = 5


def _waiting(conn: sqlite3.Connection, principal: str, now: datetime, tz: ZoneInfo) -> list[str]:
    """What waits on ``principal`` now: open approvals, then open worker questions."""
    lines: list[str] = []
    for a in ap.pending_for(conn, principal, now)[:WAITING_MAX]:
        lines.append(f"{change_line(a)} (expires {a.expires_at.astimezone(tz):%H:%M})")
    for q in questions.open_asks(conn)[:WAITING_MAX]:
        run = runs.get(conn, q.run_code)
        name = run_name(q.run_code, run.repo if run is not None else None)
        lines.append(f'{name} asks: "{flat(q.question, QUESTION_CHARS)}"')
    return lines


def _in_notes(waiting: str, notes: list[str]) -> bool:
    """``waiting`` is a worker question whose run already has a "... asked:" note."""
    if " asks: " not in waiting:
        return False
    marker = waiting.split(" asks: ", 1)[0] + " asked:"
    return any(marker in n for n in notes)


def news_reply(
    conn: sqlite3.Connection,
    settings: Settings,
    principal: str,
    *,
    event_id: str | None,
    now: datetime,
) -> str | None:
    """The owner's news, or None when the records can't be read (the caller hands the text
    to Claude). ``event_id``: the message asking; the news is what happened since the one
    before it."""
    tz = ZoneInfo(settings.timezone)
    try:
        since = (
            last_talked(conn, principal, before_event_id=event_id) if event_id is not None else None
        )
        notes = (
            since_notes(conn, owner=True, since=since, now=now, tz=settings.timezone)
            if since is not None
            else []
        )
        waiting = _waiting(conn, principal, now, tz)
    except sqlite3.Error:
        return None
    # A worker question asked since the last message is in the notes already ("R2 (repo)
    # asked: ..."): don't list it twice under "asks".
    waiting = [w for w in waiting if not _in_notes(w, notes)]
    if not notes and not waiting:
        return NEWS_NOTHING
    lines: list[str] = []
    if notes:
        lines += ["New since we last talked:", *(f"- {n}" for n in notes)]
    else:
        lines.append("Nothing new.")
    if waiting:
        lines += ["Waiting on you:", *(f"- {w}" for w in waiting)]
    return "\n".join(lines)

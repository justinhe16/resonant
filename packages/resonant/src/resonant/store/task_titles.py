"""Task titles (PER-279): one short human line per task, e.g. ``Look up net worth``.

Stored on ``tasks.title`` when the task is created (``store.tasks.insert_task``, migration
025) and shown on the dashboard's task rows. ``NewTask.title`` wins when the creator knows
better; otherwise :func:`derive` builds one from what the task is, in code (no model call):

- an owner's text (``chat``, ``media``): the cleaned first words of the message, from its
  origin event (``from_text``); a media message with no text is ``Photo or file``;
- a scheduled job: ``<ext>/<job>``;
- a watch check: ``Watch W3: <what it watches>``;
- a run: its run title, the cleaned first words of the ask (``Merge R4: …`` for a merge
  watch);
- an ``ask_claude`` turn: the owner's own words when the brain kept them, else the question;
- a reminder: ``Reminder: <text>``;
- a fast reply or a command: its code-built title (``status``, ``approval reply``, …).

Every title goes through the redaction pass (``redact.clip_text``: credential-shaped text,
then the cap) before it is stored, and the snapshot runs it through its own pass again.
``title_outside`` is set when the words came from outside text (a tainted task's question or
ask), so the dashboard shows the ``outside`` badge on it.

Never an approval code: an approval reply's title says what the reply was about, from the
inbox's outcome, never the text the owner typed.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from resonant.gate.codes import CODE_DIGITS, CODE_LETTERS
from resonant.redact import clip_text

log = logging.getLogger(__name__)

TITLE_MAX = 80  # characters, after redaction
TEXT_WORDS = 6  # an owner's text
RUN_WORDS = 8  # a run's ask

# Userinfo (``user:pass@``) is dropped: only the host is kept.
_URL = re.compile(r"(?:https?://|www\.)(?:[^\s/?#@]*@)?([^\s/?#@]+)\S*", re.IGNORECASE)
# Openers that say nothing about the ask: greetings, fillers, "can you", "please".
_OPENER = re.compile(
    r"^(?:(?:hey|hi|hello|yo|ok|okay|so|um|uh|please|pls|plz|resonant|claude)\b(?!\.\w)[\s,!.:;-]*"
    r"|(?:can|could|would|will) (?:you|u)\b\s*)+",
    re.IGNORECASE,
)
_SPACE = re.compile(r"\s+")
_CODE = re.compile(rf"(?<![\w-])[{CODE_LETTERS}][{CODE_DIGITS}](?![\w-])", re.IGNORECASE)
CODE_MASK = "[code]"
_TRIM = ".,;:!?-\u2013\u2014\"'`([{<"  # stripped from the end of a cut title (dashes too)


@dataclass(frozen=True)
class Title:
    text: str
    outside: bool = False


def clean(title: str | None) -> str | None:
    """``title`` as one redacted line of at most ``TITLE_MAX`` characters, or None."""
    if title is None:
        return None
    line = _SPACE.sub(" ", title).strip()
    if not line:
        return None
    return clip_text(line, TITLE_MAX)


def from_text(text: str | None, words: int = TEXT_WORDS) -> str | None:
    """The cleaned first ``words`` words of ``text``'s first non-empty line: a link becomes
    its host, a greeting or "can you" opener is dropped, the first letter is capitalised and
    ``…`` marks a cut; an approval-code-shaped token is masked. None when nothing is left."""
    if not text:
        return None
    line = next((ln for ln in text.splitlines() if ln.strip()), "")
    line = _URL.sub(lambda m: m.group(1).removeprefix("www."), line)
    # A token shaped like an approval code (``D7``, ``gate/codes.py``) is masked: codes stay
    # on the Mini, and a title reaches the remote page. Code-built titles (``Watch W3``) never
    # come through here.
    line = _CODE.sub(CODE_MASK, line)
    line = _SPACE.sub(" ", line).strip()
    stripped = _OPENER.sub("", line).strip()
    line = stripped or line
    parts = line.split(" ")
    cut = len(parts) > words
    out = " ".join(parts[:words])
    out = out.rstrip(_TRIM if cut else ".!?;:,").rstrip()
    if not out:
        return None
    if "." not in out.split(" ", 1)[0]:  # not a host or a file name
        out = out[0].upper() + out[1:]
    return out + "…" if cut else out


def approval_reply(reason: str | None) -> str:
    """An approval (or batch) reply that was refused: what it was about, from the inbox's
    reason (``expired``, ``unknown_code``…), never the typed code."""
    return f"Approval reply: {reason.replace('_', ' ')}" if reason else "Approval reply"


def worker_reply(run_code: str | None) -> str:
    """The owner's reply to a worker's question."""
    return f"Reply to {run_code}" if run_code else "Worker reply"


def _tainted(checkpoint: Mapping[str, Any]) -> bool:
    from resonant.store.system_texts import tainted  # a cycle at import time

    return tainted(dict(checkpoint))


def _event_text(conn: sqlite3.Connection, event_id: str | None) -> tuple[str | None, bool]:
    """The origin message's text and whether it had attachments."""
    if event_id is None:
        return None, False
    row = conn.execute("SELECT payload FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None, False
    try:
        payload: Any = json.loads(row[0])
    except (TypeError, ValueError):
        return None, False
    if not isinstance(payload, dict):
        return None, False
    p = cast(dict[str, Any], payload)
    text = p.get("text")
    return (text if isinstance(text, str) else None), bool(p.get("attachments"))


def _one(conn: sqlite3.Connection, sql: str, arg: object) -> Any:
    try:
        row = conn.execute(sql, (arg,)).fetchone()
    except sqlite3.OperationalError:  # a table this database doesn't have yet
        return None
    return None if row is None else row[0]


def derive(
    conn: sqlite3.Connection,
    *,
    kind: str,
    checkpoint: Mapping[str, Any],
    origin_event_id: str | None = None,
) -> Title | None:
    """The title a task of ``kind`` gets (see the module docstring), or None."""
    cp = checkpoint
    code_title = cp.get("title") if isinstance(cp.get("title"), str) else None
    if kind == "job":
        ext, job = cp.get("ext"), cp.get("job")
        if isinstance(ext, str) and isinstance(job, str):
            return Title(f"{ext}/{job}")
    elif kind == "watch":
        wid = cp.get("watch")
        if isinstance(wid, int) and not isinstance(wid, bool):
            what = _one(conn, "SELECT what FROM brain_watches WHERE id = ?", wid)
            words = from_text(what if isinstance(what, str) else None)
            return Title(f"Watch W{wid}: {words}" if words else f"Watch W{wid}")
    elif kind == "run":
        code = cp.get("run")
        if isinstance(code, str):
            ask = _one(conn, "SELECT ask FROM runs WHERE code = ?", code)
            words = from_text(ask if isinstance(ask, str) else None, RUN_WORDS)
            outside = words is not None and _tainted(cp)
            if cp.get("watch") is True:
                return Title(f"Merge {code}: {words}" if words else f"Merge {code}", outside)
            if words:
                return Title(words, outside)
            return Title(f"Run {code}")
    elif kind == "ask":
        if cp.get("watch") is not None:
            return Title(f"Watch W{cp['watch']} check")
        owner = cp.get("owner_text")
        if isinstance(owner, str) and (words := from_text(owner)):
            return Title(words)
        question = cp.get("question")
        if isinstance(question, str) and (words := from_text(question)):
            return Title(words, _tainted(cp))
    elif kind == "reminder":
        words = from_text(cp.get("text") if isinstance(cp.get("text"), str) else None)
        return Title(f"Reminder: {words}" if words else "Reminder")
    elif kind in ("chat", "media"):
        text, attached = _event_text(conn, origin_event_id)
        if words := from_text(text):
            return Title(words)
        if kind == "media" or attached:
            return Title("Photo or file")
    if code_title and code_title.strip():
        title = code_title.strip()
        return Title(title[0].upper() + title[1:] if title[0].isalpha() else title)
    return None


def backfill(conn: sqlite3.Connection, *, batch: int = 500) -> int:
    """Migration 025's best-effort backfill: a title for every task that has none, from its
    checkpoint and its origin message. Runs inside the migration's transaction. Returns how
    many tasks got one. A no-op on a database without ``tasks.title``."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "title" not in cols:
        return 0
    done = 0
    last = ""
    while True:
        rows = conn.execute(
            "SELECT id, kind, checkpoint, origin_event_id FROM tasks"
            " WHERE title IS NULL AND id > ? ORDER BY id LIMIT ?",
            (last, batch),
        ).fetchall()
        if not rows:
            return done
        for r in rows:
            last = r[0]
            try:
                raw: Any = json.loads(r[2]) if r[2] else {}
            except ValueError:
                raw = {}
            cp = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
            try:
                found = derive(conn, kind=r[1], checkpoint=cp, origin_event_id=r[3])
                text = clean(found.text) if found else None
            except Exception:  # best-effort: one odd row never fails the migration
                log.warning("task title backfill: skipped task %s", r[0], exc_info=True)
                continue
            if text is None or found is None:
                continue
            conn.execute(
                "UPDATE tasks SET title = ?, title_outside = ? WHERE id = ?",
                (text, int(found.outside), r[0]),
            )
            done += 1

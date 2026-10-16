"""Worker questions and owner messages (migration 010, docs/employee-plan.md D3, EM-3).

A question is one ``resonant ask`` (kind ``ask``) or a ``resonant check`` that is waiting for
the owner (kind ``check``). An owner message is text for a running worker
(``ClaudeRunner.deliver_message``). Plain functions over a connection; every state change is
one conditional UPDATE, so of two racing paths (the CLI giving up and an answer arriving, a
CLI reply and a resume prompt) exactly one wins.

Question states: ``open`` -> ``answered`` | ``timed_out`` | ``cancelled``. ``deferred`` marks
an open question the CLI stopped waiting for: the run waits for it when its turn ends.
``delivered_at`` is set once, when the answer (or the timeout) reaches the session.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from resonant.store.db import atomic, iso, now_iso

Kind = Literal["ask", "check"]
QStatus = Literal["open", "answered", "timed_out", "cancelled"]


@dataclass(frozen=True)
class Question:
    id: int
    run_code: str
    kind: Kind
    question: str
    options: tuple[str, ...]
    urgent: bool
    status: QStatus
    deferred: bool
    answer: str | None
    asked_at: datetime
    answered_at: datetime | None
    delivered_at: datetime | None


@dataclass(frozen=True)
class Message:
    id: int
    run_code: str
    text: str
    created_at: datetime
    delivered_at: datetime | None


def _dt(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def _question(r: sqlite3.Row) -> Question:
    asked = _dt(r["asked_at"])
    assert asked is not None
    return Question(
        id=r["id"],
        run_code=r["run_code"],
        kind=cast(Kind, r["kind"]),
        question=r["question"],
        options=tuple(cast(list[str], json.loads(r["options"]))),
        urgent=bool(r["urgent"]),
        status=cast(QStatus, r["status"]),
        deferred=bool(r["deferred"]),
        answer=r["answer"],
        asked_at=asked,
        answered_at=_dt(r["answered_at"]),
        delivered_at=_dt(r["delivered_at"]),
    )


def _message(r: sqlite3.Row) -> Message:
    created = _dt(r["created_at"])
    assert created is not None
    return Message(r["id"], r["run_code"], r["text"], created, _dt(r["delivered_at"]))


# --- questions --------------------------------------------------------------------------------


def ask(
    conn: sqlite3.Connection,
    run_code: str,
    kind: Kind,
    question: str,
    *,
    options: Sequence[str] = (),
    urgent: bool = False,
    at: datetime,
) -> Question:
    cur = conn.execute(
        "INSERT INTO run_questions (run_code, kind, question, options, urgent, asked_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (run_code, kind, question, json.dumps(list(options)), int(urgent), iso(at)),
    )
    q = get(conn, int(cur.lastrowid or 0))
    assert q is not None
    return q


def get(conn: sqlite3.Connection, qid: int) -> Question | None:
    r = conn.execute("SELECT * FROM run_questions WHERE id = ?", (qid,)).fetchone()
    return None if r is None else _question(r)


def for_run(conn: sqlite3.Connection, run_code: str) -> list[Question]:
    rows = conn.execute(
        "SELECT * FROM run_questions WHERE run_code = ? ORDER BY id", (run_code,)
    ).fetchall()
    return [_question(r) for r in rows]


def open_for_run(
    conn: sqlite3.Connection, run_code: str, kind: Kind | None = None
) -> list[Question]:
    sql = "SELECT * FROM run_questions WHERE run_code = ? AND status = 'open'"
    params: list[object] = [run_code]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    return [_question(r) for r in conn.execute(sql + " ORDER BY id", params)]


def open_asks(conn: sqlite3.Connection) -> list[Question]:
    """Every open ``ask`` of an active run, oldest first."""
    rows = conn.execute(
        "SELECT q.* FROM run_questions q JOIN runs r ON r.code = q.run_code"
        " WHERE q.status = 'open' AND q.kind = 'ask'"
        " AND r.status IN ('queued', 'running', 'waiting') ORDER BY q.id"
    ).fetchall()
    return [_question(r) for r in rows]


def answer(conn: sqlite3.Connection, qid: int, text: str, *, at: datetime) -> bool:
    """open -> answered. False when it was no longer open (answered, timed out, cancelled)."""
    cur = conn.execute(
        "UPDATE run_questions SET status = 'answered', answer = ?, answered_at = ?"
        " WHERE id = ? AND status = 'open'",
        (text, iso(at), qid),
    )
    return cur.rowcount == 1


def defer(conn: sqlite3.Connection, qid: int) -> bool:
    """The CLI stopped waiting: the run waits for this one once its turn ends. False when it
    was no longer open (an answer won the race: deliver that instead)."""
    cur = conn.execute(
        "UPDATE run_questions SET deferred = 1 WHERE id = ? AND status = 'open'", (qid,)
    )
    return cur.rowcount == 1


def defer_all(conn: sqlite3.Connection) -> int:
    """Daemon start: no CLI call survived the restart, so every open ask is deferred."""
    return conn.execute(
        "UPDATE run_questions SET deferred = 1 WHERE status = 'open' AND kind = 'ask'"
    ).rowcount


def time_out(conn: sqlite3.Connection, qid: int, text: str, *, at: datetime) -> bool:
    """open -> timed_out, with the text the session will get instead of an answer."""
    cur = conn.execute(
        "UPDATE run_questions SET status = 'timed_out', answer = ?, answered_at = ?"
        " WHERE id = ? AND status = 'open'",
        (text, iso(at), qid),
    )
    return cur.rowcount == 1


def mark_delivered(conn: sqlite3.Connection, qid: int, *, at: datetime | None = None) -> bool:
    """Once only: True for the one caller that delivers the answer."""
    cur = conn.execute(
        "UPDATE run_questions SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL"
        " AND status IN ('answered', 'timed_out')",
        (iso(at) if at is not None else now_iso(), qid),
    )
    return cur.rowcount == 1


def undelivered(conn: sqlite3.Connection, run_code: str) -> list[Question]:
    """Answers (and timeouts) the session hasn't seen yet, oldest first."""
    rows = conn.execute(
        "SELECT * FROM run_questions WHERE run_code = ? AND status IN ('answered', 'timed_out')"
        " AND delivered_at IS NULL ORDER BY id",
        (run_code,),
    ).fetchall()
    return [_question(r) for r in rows]


def cancel_open(conn: sqlite3.Connection, run_code: str) -> int:
    """The run ended: its open questions are closed (and never delivered)."""
    return conn.execute(
        "UPDATE run_questions SET status = 'cancelled' WHERE run_code = ? AND status = 'open'",
        (run_code,),
    ).rowcount


# --- owner messages -----------------------------------------------------------------------------


def add_message(conn: sqlite3.Connection, run_code: str, text: str, *, at: datetime) -> Message:
    cur = conn.execute(
        "INSERT INTO run_messages (run_code, text, created_at) VALUES (?, ?, ?)",
        (run_code, text, iso(at)),
    )
    r = conn.execute("SELECT * FROM run_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _message(r)


def pending_messages(conn: sqlite3.Connection, run_code: str) -> list[Message]:
    rows = conn.execute(
        "SELECT * FROM run_messages WHERE run_code = ? AND delivered_at IS NULL ORDER BY id",
        (run_code,),
    ).fetchall()
    return [_message(r) for r in rows]


def mark_messages_delivered(conn: sqlite3.Connection, ids: Sequence[int], *, at: datetime) -> None:
    for mid in ids:
        conn.execute(
            "UPDATE run_messages SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
            (iso(at), mid),
        )


def take_messages(conn: sqlite3.Connection, run_code: str, *, at: datetime) -> list[Message]:
    """The run's undelivered messages, marked delivered in the same transaction: each one is
    handed to exactly one caller."""
    with atomic(conn):
        rows = conn.execute(
            "UPDATE run_messages SET delivered_at = ? WHERE run_code = ? AND delivered_at IS NULL"
            " RETURNING *",
            (iso(at), run_code),
        ).fetchall()
    return sorted((_message(r) for r in rows), key=lambda m: m.id)


def take_answers(conn: sqlite3.Connection, run_code: str, *, at: datetime) -> list[Question]:
    """The run's undelivered answers and timeouts, marked delivered in one statement."""
    with atomic(conn):
        rows = conn.execute(
            "UPDATE run_questions SET delivered_at = ? WHERE run_code = ?"
            " AND status IN ('answered', 'timed_out') AND delivered_at IS NULL RETURNING *",
            (iso(at), run_code),
        ).fetchall()
    return sorted((_question(r) for r in rows), key=lambda q: q.id)

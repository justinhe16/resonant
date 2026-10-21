"""Watches (BR-7, docs/brain-plan.md D10): "watch X and tell me when Y".

A watch is one ``brain_watches`` row (migration 016): what to check, how often, until when,
and what counts as Y. Two kinds of check:

- ``tool``: one call of a read tool the owner may use (L0, ``Effect.READ``: a builtin read or
  an extension's read tool), through the gate and executor with ``origin="scheduled"``.
  The condition is decided in code over the result's JSON: ``contains`` / ``missing`` a text
  (case-insensitive), or ``changed`` (its fingerprint differs from the first check's).
- ``ask``: a yes/no question for ``ask_claude`` (the web, a page); Claude starts its answer
  with ``YES`` or ``NO`` (``op="yes"``). Each check is one ask, counted against
  ``brain.ask_claude.daily_cap``, so ask watches run at most every
  ``brain.watches.ask_min_interval_minutes``.

On a hit the watch sends **one** notice (``brain/notices.py``, through the pulse, capped per
day) and ends (``then="stop"``, the default); ``then="continue"`` keeps it and fires again
only after the condition was false in between (or, for ``changed``, on the next change).
A watch that expires without ever firing leaves a ``low`` core item for the next briefing,
``W3 iPhone 17 in stock ended without firing`` (``notice/watch/<id>/ended``, no text of its
own), which expires with its 24h TTL.

Limits (``brain.watches``): ``max_active`` active watches, ``min_interval_minutes`` (15)
between checks, ``default_days`` (7) to expiry, ``max_days`` (30) at most, ``max_fails``
failed checks in a row end it with a notice. Watches are the owner's: the tools are global
builtins, so only the owner (or a principal granted builtins) may create them.

This module is the store and the decisions; ``tools/watches.py`` the tools and
``runners/watch.py`` the checks and the ``core/watches`` job that queues them.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from resonant.store.db import atomic, iso

CheckKind = Literal["tool", "ask"]
Op = Literal["contains", "missing", "changed", "yes"]
Then = Literal["stop", "continue"]
State = Literal["active", "fired", "cancelled", "expired", "failed"]
OPS: tuple[Op, ...] = ("contains", "missing", "changed", "yes")
TOOL_OPS: tuple[Op, ...] = ("contains", "missing", "changed")
WHAT_MAX = 100
VALUE_MAX = 200
QUESTION_MAX = 1000
DETAIL_MAX = 140
TERMINAL_TASK = ("done", "failed", "cancelled")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_YES = re.compile(r"^\W*(yes|no)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Watch:
    id: int
    principal: str
    what: str
    check_kind: CheckKind
    extension: str | None
    tool: str | None
    args: dict[str, Any]
    question: str | None
    op: Op
    value: str | None
    then: Then
    interval_s: int
    state: State
    created_at: datetime
    expires_at: datetime
    next_at: datetime
    checks: int
    fails: int
    fired: int
    last_met: bool
    baseline: str | None
    last_checked_at: datetime | None
    last_error: str | None
    last_ask_task: str | None
    open_task_id: str | None

    @property
    def code(self) -> str:
        """``W3``: how the owner names it."""
        return f"W{self.id}"


def _dt(raw: object) -> datetime | None:
    return datetime.fromisoformat(raw) if isinstance(raw, str) else None


def _row(r: sqlite3.Row) -> Watch:
    created, expires, next_at = _dt(r["created_at"]), _dt(r["expires_at"]), _dt(r["next_at"])
    assert created is not None and expires is not None and next_at is not None
    args: Any = json.loads(r["args"] or "{}")
    return Watch(
        id=int(r["id"]),
        principal=r["principal"],
        what=r["what"],
        check_kind=r["check_kind"],
        extension=r["extension"],
        tool=r["tool"],
        args=cast(dict[str, Any], args) if isinstance(args, dict) else {},
        question=r["question"],
        op=r["op"],
        value=r["value"],
        then=r["then_action"],
        interval_s=int(r["interval_s"]),
        state=r["state"],
        created_at=created,
        expires_at=expires,
        next_at=next_at,
        checks=int(r["checks"]),
        fails=int(r["fails"]),
        fired=int(r["fired"]),
        last_met=bool(r["last_met"]),
        baseline=r["baseline"],
        last_checked_at=_dt(r["last_checked_at"]),
        last_error=r["last_error"],
        last_ask_task=r["last_ask_task"],
        open_task_id=r["open_task_id"],
    )


# --- the store ---------------------------------------------------------------------------


def create(
    conn: sqlite3.Connection,
    *,
    principal: str,
    what: str,
    check_kind: CheckKind,
    op: Op,
    interval_s: int,
    now: datetime,
    expires_at: datetime,
    extension: str | None = None,
    tool: str | None = None,
    args: dict[str, Any] | None = None,
    question: str | None = None,
    value: str | None = None,
    then: Then = "stop",
    created_by_task: str | None = None,
) -> Watch:
    """Insert an active watch whose first check is due now. The caller validated it."""
    with atomic(conn):
        cur = conn.execute(
            """INSERT INTO brain_watches (principal, what, check_kind, extension, tool, args,
                   question, op, value, then_action, interval_s, state, created_at,
                   expires_at, next_at, created_by_task)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (
                principal,
                what,
                check_kind,
                extension,
                tool,
                json.dumps(args or {}, sort_keys=True),
                question,
                op,
                value,
                then,
                interval_s,
                iso(now),
                iso(expires_at),
                iso(now),
                created_by_task,
            ),
        )
    found = get(conn, int(cur.lastrowid or 0))
    assert found is not None
    return found


def get(conn: sqlite3.Connection, watch_id: int) -> Watch | None:
    r = conn.execute("SELECT * FROM brain_watches WHERE id = ?", (watch_id,)).fetchone()
    return None if r is None else _row(r)


def active_count(conn: sqlite3.Connection, principal: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM brain_watches WHERE principal = ? AND state = 'active'",
        (principal,),
    ).fetchone()
    return int(row[0])


def list_for(
    conn: sqlite3.Connection, principal: str, *, ended_since: datetime | None = None
) -> list[Watch]:
    """The principal's active watches, then (with ``ended_since``) those that ended since."""
    rows = conn.execute(
        """SELECT * FROM brain_watches WHERE principal = ?
             AND (state = 'active' OR (ended_at IS NOT NULL AND ended_at >= ?))
           ORDER BY state != 'active', id""",
        (principal, iso(ended_since) if ended_since is not None else "9999"),
    ).fetchall()
    return [_row(r) for r in rows]


def due(conn: sqlite3.Connection, now: datetime) -> list[Watch]:
    """Active watches due a check whose last check task (if any) has ended."""
    rows = conn.execute(
        """SELECT w.* FROM brain_watches w LEFT JOIN tasks t ON t.id = w.open_task_id
           WHERE w.state = 'active' AND w.next_at <= ?
             AND (w.open_task_id IS NULL OR t.id IS NULL OR t.status IN (?, ?, ?))
           ORDER BY w.next_at, w.id""",
        (iso(now), *TERMINAL_TASK),
    ).fetchall()
    return [_row(r) for r in rows]


def claim(conn: sqlite3.Connection, w: Watch, task_id: str, now: datetime) -> bool:
    """Record ``task_id`` as the watch's check and move ``next_at`` one interval on,
    compare-and-set on the previous ``open_task_id``. Call inside the caller's transaction
    (with the task insert)."""
    cur = conn.execute(
        """UPDATE brain_watches SET open_task_id = ?, next_at = ?
           WHERE id = ? AND state = 'active' AND open_task_id IS ?""",
        (task_id, iso(now + timedelta(seconds=w.interval_s)), w.id, w.open_task_id),
    )
    return cur.rowcount == 1


def end(
    conn: sqlite3.Connection, watch_id: int, state: State, now: datetime, error: str | None = None
) -> bool:
    """Active → ``state`` (compare-and-set). True when this call ended it."""
    with atomic(conn):
        cur = conn.execute(
            """UPDATE brain_watches SET state = ?, ended_at = ?, open_task_id = NULL,
                   last_error = COALESCE(?, last_error)
               WHERE id = ? AND state = 'active'""",
            (state, iso(now), error, watch_id),
        )
    return cur.rowcount == 1


def record(
    conn: sqlite3.Connection,
    w: Watch,
    *,
    now: datetime,
    ok: bool,
    met: bool = False,
    baseline: str | None = None,
    fired: bool = False,
    error: str | None = None,
    ask_task: str | None = None,
    end_state: State | None = None,
) -> bool:
    """One check's outcome: counts, the edge state, the baseline, the failure streak, and
    (``end_state``) the end of the watch, all in one transaction: a crash can't leave a
    fired watch active with its hit already recorded. True when ``end_state`` ended it."""
    with atomic(conn):
        conn.execute(
            """UPDATE brain_watches SET checks = checks + 1, last_checked_at = ?,
                   fails = CASE WHEN ? THEN 0 ELSE fails + 1 END,
                   last_error = CASE WHEN ? THEN last_error ELSE ? END,
                   last_met = CASE WHEN ? THEN ? ELSE last_met END,
                   baseline = COALESCE(?, baseline),
                   fired = fired + ?,
                   last_ask_task = COALESCE(?, last_ask_task)
               WHERE id = ?""",
            (iso(now), ok, ok, error, ok, met, baseline, int(fired), ask_task, w.id),
        )
        if end_state is None:
            return False
        cur = conn.execute(
            """UPDATE brain_watches SET state = ?, ended_at = ?, open_task_id = NULL
               WHERE id = ? AND state = 'active'""",
            (end_state, iso(now), w.id),
        )
        return cur.rowcount == 1


def release(conn: sqlite3.Connection, w: Watch, task_id: str) -> None:
    """The check task is done with the watch."""
    with atomic(conn):
        conn.execute(
            "UPDATE brain_watches SET open_task_id = NULL WHERE id = ? AND open_task_id = ?",
            (w.id, task_id),
        )


# --- decisions ---------------------------------------------------------------------------


def result_text(data: object) -> str:
    """A tool result as the text the condition looks at: its canonical JSON."""
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(data: object) -> str:
    return hashlib.sha256(result_text(data).encode()).hexdigest()


@dataclass(frozen=True)
class Verdict:
    met: bool  # the condition holds now
    fire: bool  # tell the owner (an edge, or the first hit)
    baseline: str | None = None  # a new baseline to store (``changed``)
    detail: str = ""  # code-built, for the notice


def judge_tool(w: Watch, data: object) -> Verdict:
    """The condition over one tool result (module docstring)."""
    if w.op == "changed":
        fp = fingerprint(data)
        if w.baseline is None:
            return Verdict(False, False, fp)  # the first look sets what "changed" means
        met = fp != w.baseline
        return Verdict(met, met, fp if met else None, "it changed")
    needle = (w.value or "").casefold()
    found = needle in result_text(data).casefold()
    met = found if w.op == "contains" else not found
    detail = f'saw "{w.value}"' if w.op == "contains" else f'"{w.value}" is gone'
    return Verdict(met, met and not w.last_met, None, detail)


def parse_yes(answer: str) -> bool | None:
    """``YES`` / ``NO`` at the start of Claude's answer (after the ``Claude:`` prefix), or
    None when it said neither."""
    m = _YES.match(answer.removeprefix("Claude:").strip())
    return None if m is None else m.group(1).lower() == "yes"


def judge_ask(w: Watch, answer: str) -> Verdict:
    said = parse_yes(answer)
    met = said is True
    body = answer.removeprefix("Claude:").strip()
    body = _YES.sub("", body, count=1).strip(" .,:;-\n")
    body = _URL.sub("(link)", body)  # page text chose these words: no links in a text to the owner
    detail = ("Claude: " + " ".join(body.split()))[:DETAIL_MAX] if body else ""
    return Verdict(met, met and not w.last_met, None, detail)


def notice_title(w: Watch, detail: str) -> str:
    """``👀 W3 iPhone 17 in stock: saw "Add to bag"``."""
    head = f"\U0001f440 {w.code} {w.what}"
    return f"{head}: {detail}" if detail else head


def ended_title(w: Watch) -> str:
    """``W3 iPhone 17 in stock ended without firing``: the briefing's line for a watch that
    expired with no hit (PER-266)."""
    return f"{w.code} {w.what} ended without firing"


def failed_title(w: Watch, error: str | None) -> str:
    why = f" ({error[:80]})" if error else ""
    return f"⚠ Stopped watch {w.code} {w.what}: its check failed {w.fails + 1} times{why}"


def describe(w: Watch) -> dict[str, Any]:
    """A watch as ``list_watches`` shows it (no tool output, no answers)."""
    check: dict[str, Any] = (
        {"tool": w.tool, "extension": w.extension, "when": w.op, "text": w.value}
        if w.check_kind == "tool"
        else {"ask": w.question}
    )
    return {
        "id": w.code,
        "what": w.what,
        "state": w.state,
        "check": check,
        "every_minutes": w.interval_s // 60,
        "then": w.then,
        "expires_at": iso(w.expires_at),
        "next_at": iso(w.next_at) if w.state == "active" else None,
        "checks": w.checks,
        "fired": w.fired,
        "last_error": w.last_error,
    }


__all__ = [
    "OPS",
    "TOOL_OPS",
    "Verdict",
    "Watch",
    "active_count",
    "claim",
    "create",
    "describe",
    "due",
    "end",
    "ended_title",
    "failed_title",
    "get",
    "judge_ask",
    "judge_tool",
    "list_for",
    "notice_title",
    "parse_yes",
    "record",
    "release",
]

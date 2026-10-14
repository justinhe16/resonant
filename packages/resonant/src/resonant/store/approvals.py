"""Approvals repository. Plain functions over a sqlite3 connection.

The gate owns the policy (who may approve, binding, codes). This module owns the rows:
- ``resolve`` is one conditional UPDATE, so two resolvers (CLI and daemon, or two replies
  in one poll) can't both win, and nothing changes between check and write.
- ``expire_due`` expires pending approvals past ``expires_at``. Expiry is lazy: the gate
  calls it before every entry point.
- An approval is single use: ``mark_used`` sets ``used_at`` once, in the same transaction
  as the executor's ``started`` row.
- ``wake_resolved`` tells the waiting task, in the resolver's transaction.

Orphans (pending approvals of a task that became terminal) are expired by ``store.tasks``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from resonant.gate.types import ResolvedApproval, ResolveResult
from resonant.store import tasks as repo
from resonant.store.audit import audit
from resonant.store.db import atomic, iso
from resonant_sdk import Effect, Event, Level, new_id

ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]

# Why ``resolve`` refused. "task_not_waiting": the task is no longer waiting or running.
ResolveRefusal = Literal["not_found", "already_resolved", "expired", "task_not_waiting"]


@dataclass(frozen=True)
class ChannelRef:
    """One approval prompt as sent: the channel's message ref, when, and to whom."""

    ref: str
    at: datetime
    principal: str


@dataclass(frozen=True)
class NewApproval:
    task_id: str
    intent_hash: str
    tool: str
    effect: Effect
    level: Level
    summary: str
    approvers: tuple[str, ...]
    code: str
    expires_at: datetime
    require_code: bool = False
    extension: str | None = None
    requested_by: str | None = None


@dataclass(frozen=True)
class Approval:
    id: str
    task_id: str | None
    intent_hash: str
    extension: str | None
    tool: str
    effect: Effect
    level: Level
    summary: str
    requested_by: str | None
    approvers: tuple[str, ...]
    status: ApprovalStatus
    decided_by: str | None
    decided_at: datetime | None
    expires_at: datetime
    channel_refs: tuple[ChannelRef, ...]
    created_at: datetime
    require_code: bool
    code: str | None
    used_at: datetime | None
    resolved_event_id: str | None

    def sent_at(self, principal: str) -> datetime | None:
        """When the prompt was first sent to ``principal`` (None if it never was)."""
        times = [r.at for r in self.channel_refs if r.principal == principal]
        return min(times, default=None)

    def resolved(self) -> ResolvedApproval:
        """This row as a gate ``ResolvedApproval``. Only meaningful once decided."""
        assert self.task_id is not None
        return ResolvedApproval(self.id, self.task_id, self.intent_hash, self.status == "approved")


def _dt(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def _row_to_approval(r: sqlite3.Row) -> Approval:
    expires = _dt(r["expires_at"])
    created = _dt(r["created_at"])
    assert expires is not None and created is not None
    return Approval(
        id=r["id"],
        task_id=r["task_id"],
        intent_hash=r["intent_hash"],
        extension=r["extension"],
        tool=r["tool"],
        effect=Effect(r["effect"]),
        level=Level(r["level"]),
        summary=r["summary"],
        requested_by=r["requested_by"],
        approvers=tuple(json.loads(r["approvers"])),
        status=cast(ApprovalStatus, r["status"]),
        decided_by=r["decided_by"],
        decided_at=_dt(r["decided_at"]),
        expires_at=expires,
        channel_refs=tuple(
            ChannelRef(c["ref"], datetime.fromisoformat(c["at"]), c["principal"])
            for c in json.loads(r["channel_refs"])
        ),
        created_at=created,
        require_code=bool(r["require_code"]),
        code=r["code"],
        used_at=_dt(r["used_at"]),
        resolved_event_id=r["resolved_event_id"],
    )


def insert_approval(conn: sqlite3.Connection, spec: NewApproval, now: datetime) -> str:
    """Insert a pending approval and return its id.

    Raises sqlite3.IntegrityError if one is already pending for (task, intent hash) or the
    code is taken. Check ``pending_for_intent`` and ``pending_codes`` first.
    """
    approval_id = new_id()
    conn.execute(
        """INSERT INTO approvals (id, task_id, intent_hash, extension, tool, effect, level,
               summary, requested_by, approvers, expires_at, require_code, code, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            approval_id,
            spec.task_id,
            spec.intent_hash,
            spec.extension,
            spec.tool,
            spec.effect.value,
            spec.level.value,
            spec.summary,
            spec.requested_by,
            json.dumps(list(spec.approvers)),
            iso(spec.expires_at),
            spec.require_code,
            spec.code,
            iso(now),
        ),
    )
    return approval_id


def get_approval(conn: sqlite3.Connection, approval_id: str) -> Approval | None:
    r = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return None if r is None else _row_to_approval(r)


def pending_for_intent(
    conn: sqlite3.Connection, task_id: str, intent_hash: str, now: datetime
) -> Approval | None:
    """The unexpired pending approval for (task, intent hash), to reuse on re-evaluate."""
    r = conn.execute(
        """SELECT * FROM approvals WHERE task_id = ? AND intent_hash = ?
               AND status = 'pending' AND expires_at > ?""",
        (task_id, intent_hash, iso(now)),
    ).fetchone()
    return None if r is None else _row_to_approval(r)


def approved_unused(conn: sqlite3.Connection, task_id: str, intent_hash: str) -> Approval | None:
    """An approved approval for (task, intent hash) that no execution has used yet.

    No expiry check: once approved, an approval is valid for the life of its task.
    """
    r = conn.execute(
        """SELECT * FROM approvals WHERE task_id = ? AND intent_hash = ?
               AND status = 'approved' AND used_at IS NULL
           ORDER BY decided_at, id LIMIT 1""",
        (task_id, intent_hash),
    ).fetchone()
    return None if r is None else _row_to_approval(r)


def pending_for(conn: sqlite3.Connection, principal: str, now: datetime) -> list[Approval]:
    """Unexpired pending approvals that ``principal`` is an approver for, oldest first."""
    rows = conn.execute(
        """SELECT * FROM approvals WHERE status = 'pending' AND expires_at > ?
               AND EXISTS (SELECT 1 FROM json_each(approvers) WHERE value = ?)
           ORDER BY created_at, id""",
        (iso(now), principal),
    ).fetchall()
    return [_row_to_approval(r) for r in rows]


def pending_by_code(conn: sqlite3.Connection, code: str, now: datetime) -> Approval | None:
    """The unexpired pending approval with this code. Codes are unique among pending."""
    r = conn.execute(
        "SELECT * FROM approvals WHERE code = ? AND status = 'pending' AND expires_at > ?",
        (code, iso(now)),
    ).fetchone()
    return None if r is None else _row_to_approval(r)


def pending_codes(conn: sqlite3.Connection) -> set[str]:
    """Codes in use by pending rows, expired or not (the unique index covers them all)."""
    rows = conn.execute(
        "SELECT code FROM approvals WHERE status = 'pending' AND code IS NOT NULL"
    ).fetchall()
    return {r["code"] for r in rows}


def by_resolved_event(conn: sqlite3.Connection, event_id: str) -> list[Approval]:
    """Approvals that this reply event resolved. Empty if it resolved none."""
    rows = conn.execute(
        "SELECT * FROM approvals WHERE resolved_event_id = ? ORDER BY decided_at, id",
        (event_id,),
    ).fetchall()
    return [_row_to_approval(r) for r in rows]


def add_channel_ref(
    conn: sqlite3.Connection, approval_id: str, *, ref: str, at: datetime, principal: str
) -> bool:
    """Record that the prompt was sent. Returns False if this (ref, principal) is known.

    A re-prompt that the channel deduped returns the same ref, and the first send time is
    the one that counts for the sent-before rule, so it is kept.
    """
    entry = json.dumps({"ref": ref, "at": iso(at), "principal": principal})
    cur = conn.execute(
        """UPDATE approvals SET channel_refs = json_insert(channel_refs, '$[#]', json(?))
           WHERE id = ? AND NOT EXISTS (
               SELECT 1 FROM json_each(channel_refs)
               WHERE json_extract(value, '$.ref') = ?
                 AND json_extract(value, '$.principal') = ?)""",
        (entry, approval_id, ref, principal),
    )
    return cur.rowcount == 1


def resolve(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    approved: bool,
    decided_by: str,
    now: datetime,
    event_id: str | None = None,
) -> ResolveRefusal | None:
    """Approve or reject a pending approval. Returns None on success, else why not.

    One conditional UPDATE: the row must still be pending and unexpired, and its task must
    be waiting or running. The refusal reason is read afterwards and is informational only.
    """
    cur = conn.execute(
        """UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?,
               resolved_event_id = ?
           WHERE id = ? AND status = 'pending' AND expires_at > ?
             AND EXISTS (SELECT 1 FROM tasks WHERE tasks.id = approvals.task_id
                         AND tasks.status IN ('waiting', 'running'))""",
        (
            "approved" if approved else "rejected",
            decided_by,
            iso(now),
            event_id,
            approval_id,
            iso(now),
        ),
    )
    if cur.rowcount == 1:
        return None
    current = get_approval(conn, approval_id)
    if current is None:
        return "not_found"
    if current.status != "pending":
        return "expired" if current.status == "expired" else "already_resolved"
    if current.expires_at <= now:
        return "expired"
    return "task_not_waiting"


def mark_used(conn: sqlite3.Connection, approval_id: str, now: datetime) -> bool:
    """Spend an approved approval. True exactly once; call with the executor's 'started'."""
    cur = conn.execute(
        """UPDATE approvals SET used_at = ?
           WHERE id = ? AND status = 'approved' AND used_at IS NULL""",
        (iso(now), approval_id),
    )
    return cur.rowcount == 1


def expire_due(conn: sqlite3.Connection, now: datetime) -> int:
    """pending -> expired for approvals past ``expires_at``. Audits each one."""
    with atomic(conn):
        rows = conn.execute(
            """UPDATE approvals SET status = 'expired', decided_at = ?
               WHERE status = 'pending' AND expires_at <= ?
               RETURNING id, task_id, tool, extension, intent_hash""",
            (iso(now), iso(now)),
        ).fetchall()
        for r in rows:
            audit(
                conn,
                "approval.expired",
                task_id=r["task_id"],
                tool=r["tool"],
                extension=r["extension"],
                intent_hash=r["intent_hash"],
                approval_id=r["id"],
            )
    return len(rows)


def wake_resolved(
    conn: sqlite3.Connection,
    result: ResolveResult,
    now: datetime,
    *,
    principal: str | None = None,
) -> int:
    """Tell each resolved approval's task. Call in the resolver's transaction.

    Inserts one ``Event(source="gate", type="approval.reply")`` per approval, deduped by
    approval id, and requeues the task if it is waiting. A task that is still running sees
    the event when its step ends (a ``Wait`` with pending events requeues). Returns the
    number of tasks requeued.
    """
    woken = 0
    with atomic(conn):
        for r in result.resolved:
            row = conn.execute("SELECT trace_id FROM tasks WHERE id = ?", (r.task_id,)).fetchone()
            if row is None:
                continue
            event = Event(
                source="gate",
                type="approval.reply",
                task_id=r.task_id,
                principal=principal,
                payload={"approval_id": r.approval_id, "approved": r.approved},
                dedupe_key=f"approval:{r.approval_id}:resolved",
                occurred_at=now,
                trace_id=row["trace_id"],
            )
            if repo.insert_event(conn, event) and repo.set_status(
                conn, r.task_id, "queued", expect=("waiting",)
            ):
                woken += 1
    return woken


def resolve_result(approvals: Sequence[Approval]) -> ResolveResult:
    """The ``ResolveResult`` for approvals already resolved (e.g. ``by_resolved_event``).

    Check for an empty list first: it means the event resolved nothing, not that it is done.
    """
    return ResolveResult(ok=True, resolved=tuple(a.resolved() for a in approvals))

"""Daily store prune (PER-259): ``core/prune``.

Nothing used to delete ``events``, finished ``tasks`` or ``audit_log`` rows, so the store only
grew. This job deletes what is past ``retention.events_days`` / ``tasks_days`` / ``audit_days``
and nothing anything still points at. Deleting is the one thing here that cannot be taken
back, so the rules lean toward keeping a row.

Never deleted, however old:

- An **event** that is unconsumed; belongs to an open task (status not done/failed/cancelled);
  is an open task's origin, or its id appears anywhere (any depth) among the string values of
  an open task's checkpoint (open tasks point at others through keys like ``requested_by`` and
  ``asking_task``); is an approval's ``resolved_event_id``; or is a ``brain_summaries``
  ``through_event_id`` (the brain's history counts from the start when its cursor event is
  gone, which would replay old messages).
- A **task** that is open or named in an open task's checkpoint; has any approval (approvals
  are not pruned); still has an event (the foreign key); is an ``ext_jobs`` or
  ``brain_watches`` task; has a run (``runs`` are never pruned, and a run's delivery inserts
  events with its ``task_id`` whatever its age) or a ``system_texts`` row. A pruned task's
  ``executions`` (its idempotency keys, meaningless without it) go with it, before it.
- An **audit** row of an open task (the local runner reads them).

Order: events, then executions and tasks, then audit; a task only becomes deletable once its
events are gone. Rows come out in chunks (``CHUNK`` per transaction) on the job's own
connection, so the daemon's writers interleave. ``audit_log`` is append-only by trigger
(migration 020 lets a DELETE through only for rows older than kv ``store.prune_audit_before``);
the prune sets that key, deletes and clears it in one transaction, so no other connection ever
sees it set.

Schedule: ``backup.daily_at`` plus 30 minutes, ``core/prune``. A deletion always follows a
verified copy: when backups are enabled and the last good one (kv ``backup.last_ok``) is older
than 24 hours, the run is skipped. With ``dry_run`` on it counts and deletes nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from resonant.backup import status_of as backup_status
from resonant.loop.types import TERMINAL
from resonant.store.audit import audit
from resonant.store.db import atomic, connect, iso, kv_set, transaction, utcnow

if TYPE_CHECKING:
    from resonant.config import RetentionConfig, Settings
    from resonant.executor import CallContext
    from resonant.extensions.scheduler import CoreDeps, CoreJob

log = logging.getLogger(__name__)

JOB_ID = "prune"
OFFSET = timedelta(minutes=30)  # after the backup, so the copy it needs exists
CATCHUP = timedelta(hours=20)
TIMEOUT_S = 1800.0
DEADLINE_MARGIN_S = 60.0  # stop between chunks this long before the job's timeout
CHUNK = 2000
BACKUP_MAX_AGE = timedelta(hours=24)
KV_AUDIT_BEFORE = "store.prune_audit_before"  # read by the audit_log_no_delete trigger

# loop.types.TERMINAL as SQL literals (constants of ours, never user input)
_TERMINAL_SQL = ", ".join(repr(s) for s in sorted(TERMINAL))


@dataclass(frozen=True)
class PruneResult:
    events: int = 0
    tasks: int = 0
    executions: int = 0
    audit: int = 0

    @property
    def total(self) -> int:
        return self.events + self.tasks + self.executions + self.audit

    def as_dict(self) -> dict[str, int]:
        return {
            "events": self.events,
            "tasks": self.tasks,
            "executions": self.executions,
            "audit": self.audit,
        }

    def plus(self, other: PruneResult) -> PruneResult:
        return PruneResult(
            self.events + other.events,
            self.tasks + other.tasks,
            self.executions + other.executions,
            self.audit + other.audit,
        )


# --- what is still referenced ----------------------------------------------------------


def _strings(value: object) -> set[str]:
    """Every string anywhere in a decoded JSON value (keys and values, any depth)."""
    if isinstance(value, str):
        return {value}
    found: set[str] = set()
    if isinstance(value, dict):
        for k, v in cast(dict[object, object], value).items():
            found |= _strings(k) | _strings(v)
    elif isinstance(value, list):
        for v in cast(list[object], value):
            found |= _strings(v)
    return found


def open_references(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """``(open task ids, ids they point at)``: the open tasks' origin events and every string in
    their checkpoints (other tasks, run codes, events). Open tasks are few, so this is Python."""
    open_ids: set[str] = set()
    refs: set[str] = set()
    for r in conn.execute(
        "SELECT id, origin_event_id, checkpoint FROM tasks"  # noqa: S608 - constants of ours
        f" WHERE status NOT IN ({_TERMINAL_SQL})"
    ):
        open_ids.add(r["id"])
        if r["origin_event_id"]:
            refs.add(r["origin_event_id"])
        try:
            refs |= _strings(json.loads(r["checkpoint"]))
        except ValueError:
            continue  # not JSON: there is nothing in it to follow
    return open_ids, refs


# --- the rules, as SQL -----------------------------------------------------------------
# ``:open`` / ``:keep`` are JSON arrays of ids (open task ids; ids an open task points at).

# An event nothing needs, past retention (``e`` is its alias).
_EVENT_DELETABLE = """
    e.received_at < :events_cut AND e.consumed_at IS NOT NULL
    AND (e.task_id IS NULL OR e.task_id NOT IN (SELECT value FROM json_each(:open)))
    AND e.id NOT IN (SELECT value FROM json_each(:keep))
    AND e.id NOT IN (SELECT resolved_event_id FROM approvals WHERE resolved_event_id IS NOT NULL)
    AND e.id NOT IN (SELECT through_event_id FROM brain_summaries)
    AND NOT EXISTS (SELECT 1 FROM brain_summaries b JOIN events c ON c.id = b.through_event_id
                    WHERE b.principal = e.principal AND e.rowid > c.rowid)
"""

# A finished task nothing needs, past retention (``t``): its events are only the ones the event
# prune keeps, so this holds both before the events are deleted (dry run) and after.
_TASK_DELETABLE = f"""
    t.status IN ({_TERMINAL_SQL}) AND t.updated_at < :tasks_cut
    AND t.id NOT IN (SELECT value FROM json_each(:keep))
    AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.task_id = t.id)
    AND NOT EXISTS (SELECT 1 FROM events e WHERE e.task_id = t.id AND NOT ({_EVENT_DELETABLE}))
    AND NOT EXISTS (SELECT 1 FROM ext_jobs j WHERE j.open_task_id = t.id)
    AND NOT EXISTS (SELECT 1 FROM brain_watches w WHERE w.open_task_id = t.id)
    AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.task_id = t.id)
    AND NOT EXISTS (SELECT 1 FROM system_texts s WHERE s.task_id = t.id)
"""  # noqa: S608 - only constants of ours are interpolated

_AUDIT_DELETABLE = """
    l.at < :audit_cut
    AND (l.task_id IS NULL OR l.task_id NOT IN (SELECT value FROM json_each(:open)))
"""


_COUNT_EVENTS = f"SELECT COUNT(*) FROM events e WHERE {_EVENT_DELETABLE}"  # noqa: S608
_COUNT_TASKS = f"SELECT COUNT(*) FROM tasks t WHERE {_TASK_DELETABLE}"  # noqa: S608
_COUNT_EXECUTIONS = (  # the executions of the tasks that would go
    "SELECT COUNT(*) FROM executions x WHERE x.task_id IN "  # noqa: S608
    f"(SELECT t.id FROM tasks t WHERE {_TASK_DELETABLE})"
)
_COUNT_AUDIT = f"SELECT COUNT(*) FROM audit_log l WHERE {_AUDIT_DELETABLE}"  # noqa: S608
_DELETE_EVENTS = (
    "DELETE FROM events WHERE id IN (SELECT e.id FROM events e"  # noqa: S608
    f" WHERE {_EVENT_DELETABLE} LIMIT :n)"
)
_PICK_TASKS = f"SELECT t.id FROM tasks t WHERE {_TASK_DELETABLE} LIMIT :n"  # noqa: S608
_DELETE_AUDIT = (
    "DELETE FROM audit_log WHERE id IN (SELECT l.id FROM audit_log l"  # noqa: S608
    f" WHERE {_AUDIT_DELETABLE} LIMIT :n)"
)


def _params(
    now: datetime, cfg: RetentionConfig, open_ids: set[str], refs: set[str]
) -> dict[str, str]:
    return {
        "events_cut": iso(now - timedelta(days=cfg.events_days)),
        "tasks_cut": iso(now - timedelta(days=cfg.tasks_days)),
        "audit_cut": iso(now - timedelta(days=cfg.audit_days)),
        "open": json.dumps(sorted(open_ids)),
        "keep": json.dumps(sorted(refs | open_ids)),
    }


# --- pruning ---------------------------------------------------------------------------


def prune(
    conn: sqlite3.Connection,
    now: datetime,
    cfg: RetentionConfig,
    *,
    dry_run: bool = False,
    chunk: int = CHUNK,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> PruneResult:
    """Delete what is past retention and unreferenced; returns the counts (what *would* go,
    with ``dry_run``). ``conn`` must have foreign keys on and no transaction open.

    Each chunk is one write transaction that re-reads the open tasks' references and picks its
    rows under the write lock, so a reference a writer added between chunks is always seen.
    ``deadline`` (``time.monotonic()``): stop between chunks once it passes; the rest goes on
    the next run."""
    if dry_run:
        return _count_deletable(conn, _params(now, cfg, *open_references(conn)))
    result = PruneResult(0, 0, 0, 0)
    for step in (_events_chunk, _tasks_chunk, _audit_chunk):
        while True:
            if deadline is not None and monotonic() >= deadline:
                log.info("prune stopped at its deadline; the rest goes next run")
                return result
            with transaction(conn):
                params: dict[str, Any] = {**_params(now, cfg, *open_references(conn)), "n": chunk}
                got, rows = step(conn, params)
            result = result.plus(got)
            if rows < chunk:
                break
    return result


def _count_deletable(conn: sqlite3.Connection, params: dict[str, str]) -> PruneResult:
    def count(sql: str) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    return PruneResult(
        events=count(_COUNT_EVENTS),
        tasks=count(_COUNT_TASKS),
        executions=count(_COUNT_EXECUTIONS),
        audit=count(_COUNT_AUDIT),
    )


def _events_chunk(conn: sqlite3.Connection, params: dict[str, Any]) -> tuple[PruneResult, int]:
    n = conn.execute(_DELETE_EVENTS, params).rowcount
    return PruneResult(n, 0, 0, 0), n


def _tasks_chunk(conn: sqlite3.Connection, params: dict[str, Any]) -> tuple[PruneResult, int]:
    """Picks and deletes in the same transaction: the ids deleted are exactly those checked."""
    ids = [r[0] for r in conn.execute(_PICK_TASKS, params)]
    if not ids:
        return PruneResult(0, 0, 0, 0), 0
    batch = json.dumps(ids)
    executions = conn.execute(
        "DELETE FROM executions WHERE task_id IN (SELECT value FROM json_each(?))", (batch,)
    ).rowcount
    tasks = conn.execute(
        "DELETE FROM tasks WHERE id IN (SELECT value FROM json_each(?))", (batch,)
    ).rowcount
    return PruneResult(0, tasks, executions, 0), len(ids)


def _audit_chunk(conn: sqlite3.Connection, params: dict[str, Any]) -> tuple[PruneResult, int]:
    """Opens the trigger's window, deletes and closes it, inside the chunk's transaction."""
    kv_set(conn, KV_AUDIT_BEFORE, params["audit_cut"])
    n = conn.execute(_DELETE_AUDIT, params).rowcount
    conn.execute("DELETE FROM kv WHERE key = ?", (KV_AUDIT_BEFORE,))
    return PruneResult(0, 0, 0, n), n


# --- the job ---------------------------------------------------------------------------


def schedule(daily_at: str) -> str:
    """Cron for ``backup.daily_at`` plus 30 minutes, wrapping past midnight."""
    hour, minute = daily_at.split(":")
    at = (int(hour) * 60 + int(minute) + int(OFFSET.total_seconds() // 60)) % (24 * 60)
    return f"{at % 60} {at // 60} * * *"


def _good_backup_age(conn: sqlite3.Connection, now: datetime) -> timedelta | None:
    last = backup_status(conn, now)["last_ok_at"]
    return None if last is None else now - datetime.fromisoformat(last)


def run_prune(settings: Settings, now: datetime) -> dict[str, Any]:
    """The work (the job calls it in a thread): the prune on a connection of its own, then
    the ``store.pruned`` audit row (only when something went)."""
    conn = connect(settings.db_path)
    try:
        result = prune(
            conn,
            now,
            settings.retention,
            dry_run=settings.dry_run,
            deadline=time.monotonic() + TIMEOUT_S - DEADLINE_MARGIN_S,
        )
        if settings.dry_run:
            log.info("prune dry run: would delete %s", result.as_dict())
            return {**result.as_dict(), "dry_run": True}
        if result.total:
            with atomic(conn):
                audit(
                    conn,
                    "store.pruned",
                    events=result.events,
                    tasks=result.tasks,
                    executions=result.executions,
                    audit=result.audit,
                )
        log.info("pruned %s", result.as_dict())
        return result.as_dict()
    finally:
        conn.close()


def store_upkeep_job(deps: CoreDeps, *, clock: Callable[[], datetime] = utcnow) -> CoreJob | None:
    """The ``CORE_JOBS`` factory. None when ``retention.enabled`` is false."""
    from resonant.extensions.scheduler import CoreJob

    settings = deps.settings
    if not settings.retention.enabled:
        return None

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        now = clock()
        if settings.backup.enabled:  # read on the loop's connection, before any thread
            age = _good_backup_age(deps.conn, now)
            if age is None or age > BACKUP_MAX_AGE:
                log.warning("prune skipped: no good backup in the last 24h")
                return {"skipped": "no backup in the last 24h"}
        return await asyncio.to_thread(run_prune, settings, now)

    return CoreJob(
        id=JOB_ID,
        schedule=schedule(settings.backup.daily_at),
        run=run,
        catchup=CATCHUP,
        timeout_s=TIMEOUT_S,
    )

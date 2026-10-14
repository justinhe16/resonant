"""Extension tools behind ``/ext`` (owner-only commands, global builtins).

- ``list_extensions`` (L0): every installed extension, read only. Per extension: status
  (``enabled``, ``disabled``, ``disabled_tampered``, or ``refused`` when an enabled row
  didn't load), the pin (commit and tree digest, 8 chars each), tool and job counts, its
  mute, and its ``ext:<name>`` health when that check has run. Per job: its schedule,
  level, next due time (from the manifest's cron, in the job's zone or ``settings.timezone``:
  an ``ext_jobs`` row exists only after the first due time), and the last run's outcome and
  fail streak from ``ext_jobs``. Manifest strings never appear, only names.
- ``mute_extension`` (L3): ``{extension, until}`` holds that extension's notifications
  until ``until`` (ISO with an offset, in the future, at most 7 days ahead); ``{extension,
  off: true}`` lifts it. kv ``ext.<name>.muted_until`` (ISO, UTC), audited
  ``ext.notify_muted`` / ``ext.notify_unmuted``. The notifier (P3-12) reads the same key.

Handlers don't see the requester; the gate limits callers to principals who may request
global tools (the owner).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from resonant.executor import ToolFailed, ToolHandler
from resonant.extensions.cron import CronError, parse_schedule
from resonant.monitor import HEALTH_KV_PREFIX, CheckState
from resonant.store import extensions as store
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set
from resonant.tools.base import BuiltinContext, no_args
from resonant.tools.when import format_due
from resonant_sdk import Effect, Level, Manifest, ToolSpec

if TYPE_CHECKING:
    from resonant.extensions.host import ExtensionHost

log = logging.getLogger(__name__)

MUTE_MAX = timedelta(days=7)
MUTE_SLACK = timedelta(minutes=5)  # parsed at route time; the task may run a little later
SHORT = 8

LIST_EXTENSIONS = ToolSpec(
    name="list_extensions",
    description=(
        "List installed extensions: status, pin, tools, jobs with next due time and last "
        "run, mute and health."
    ),
    effect=Effect.READ,
    timeout_s=5.0,
)
MUTE_EXTENSION = ToolSpec(
    name="mute_extension",
    description="Hold an extension's notifications until a time, or lift the mute (off).",
    args_schema={
        "type": "object",
        "properties": {
            "extension": {"type": "string", "maxLength": 48},
            "until": {"type": "string", "maxLength": 40},
            "off": {"type": "boolean"},
        },
        "required": ["extension"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=5.0,
)

SPECS: tuple[ToolSpec, ...] = (LIST_EXTENSIONS, MUTE_EXTENSION)


def muted_key(ext: str) -> str:
    return f"ext.{ext}.muted_until"


def muted_until(conn: sqlite3.Connection, ext: str, now: datetime) -> datetime | None:
    """When ``ext``'s mute ends, or None (never set, unreadable or already past)."""
    raw: object = kv_get(conn, muted_key(ext))
    if not isinstance(raw, str):
        return None
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return until if until.utcoffset() is not None and until > now else None


def _health(conn: sqlite3.Connection, ext: str) -> str | None:
    """The ``ext:<name>`` health check's state, or None if it has never run."""
    state = CheckState.from_kv(kv_get(conn, f"{HEALTH_KV_PREFIX}ext:{ext}")).state
    return None if state == "unknown" else state


def _loaded(ctx: BuiltinContext, host: ExtensionHost | None, name: str) -> bool:
    if host is not None:
        return name in host.loaded
    return bool(ctx.registry.specs(name))


def _when(at: datetime | None, now: datetime, tz: str) -> str | None:
    return None if at is None else format_due(at, now=now, tz=tz)


def list_extensions(ctx: BuiltinContext) -> dict[str, Any]:
    conn, now = ctx.conn, ctx.clock()
    tz = ctx.state.settings.timezone
    components = ctx.state.components
    host = components.extensions if components is not None else None
    out: list[dict[str, Any]] = []
    for record in store.list_extensions(conn):
        status: str = record.status
        reason: str | None = None
        if status == "enabled":
            if host is not None and record.name in host.refused:
                status, reason = "refused", host.refused[record.name]
            elif not _loaded(ctx, host, record.name):
                status = "refused"
        item: dict[str, Any] = {
            "name": record.name,
            "status": status,
            "reason": reason,
            "commit": record.commit[:SHORT] if record.commit else None,
            "digest": record.tree_digest[:SHORT],
            "tools": 0,
            "jobs": [],
            "muted_until": None,
            "muted_until_text": None,
            "health": _health(conn, record.name),
        }
        if (until := muted_until(conn, record.name, now)) is not None:
            item["muted_until"], item["muted_until_text"] = iso(until), _when(until, now, tz)
        try:
            manifest = Manifest.model_validate(record.manifest)
        except ValidationError:
            item["status"], item["reason"] = "refused", "manifest no longer validates"
            out.append(item)
            continue
        item["tools"] = len(manifest.tools)
        states = {j.job_id: j for j in store.list_jobs(conn, record.name)}
        for job in sorted(manifest.jobs, key=lambda j: j.id):
            try:
                next_due = parse_schedule(job.schedule, job.tz or tz).next_after(now)
            except (CronError, ValueError):
                next_due = None
            state = states.get(job.id)
            last_at = state.last_run_at if state is not None else None
            item["jobs"].append(
                {
                    "id": job.id,
                    "schedule": job.schedule,
                    "level": job.spec(record.name).effective_level.value,
                    "next_due": None if next_due is None else iso(next_due),
                    "next_due_text": _when(next_due, now, tz),
                    "last_run_at": None if last_at is None else iso(last_at),
                    "last_run_text": _when(last_at, now, tz),
                    "last_outcome": state.last_outcome if state is not None else None,
                    "fail_streak": state.fail_streak if state is not None else 0,
                }
            )
        out.append(item)
    return {"extensions": out, "count": len(out)}


def mute_extension(ctx: BuiltinContext, args: dict[str, Any]) -> dict[str, Any]:
    unknown = set(args) - {"extension", "until", "off"}
    if unknown:
        raise ToolFailed(f"unknown arguments: {sorted(unknown)}")
    name, until_raw, off = args.get("extension"), args.get("until"), args.get("off")
    if not isinstance(name, str) or store.get_extension(ctx.conn, name) is None:
        raise ToolFailed("no such extension")
    owner = ctx.principals.owner
    now = ctx.clock()
    if off is True and until_raw is None:
        with atomic(ctx.conn):
            was = muted_until(ctx.conn, name, now) is not None
            ctx.conn.execute("DELETE FROM kv WHERE key = ?", (muted_key(name),))
            audit(ctx.conn, "ext.notify_unmuted", extension=name, requested_by=owner, was_muted=was)
        return {"extension": name, "muted_until": None, "was_muted": was}
    if off is not None or not isinstance(until_raw, str):
        raise ToolFailed("give either until or off")
    try:
        until = datetime.fromisoformat(until_raw)
    except ValueError:
        raise ToolFailed("until must be an ISO time") from None
    if until.utcoffset() is None:
        raise ToolFailed("until needs a UTC offset")
    if until <= now or until > now + MUTE_MAX + MUTE_SLACK:
        raise ToolFailed("until must be in the future and at most 7 days ahead")
    with atomic(ctx.conn):
        kv_set(ctx.conn, muted_key(name), iso(until))
        audit(ctx.conn, "ext.notify_muted", extension=name, requested_by=owner, until=iso(until))
    tz = ctx.state.settings.timezone
    return {
        "extension": name,
        "muted_until": iso(until),
        "until_text": format_due(until, now=now, tz=tz),
    }


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    async def _list(args: dict[str, Any]) -> dict[str, Any]:
        no_args(LIST_EXTENSIONS.name, args)
        return list_extensions(ctx)

    async def _mute(args: dict[str, Any]) -> dict[str, Any]:
        return mute_extension(ctx, args)

    return {LIST_EXTENSIONS.name: _list, MUTE_EXTENSION.name: _mute}

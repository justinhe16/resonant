"""Extension tools behind ``/ext`` (owner-only commands, global builtins).

- ``list_extensions`` (L0): every installed extension, read only. Per extension: status
  (``enabled``, ``disabled``, ``disabled_tampered``, or ``refused`` when an enabled row
  didn't load), the pin (commit and tree digest, 8 chars each), tool and job counts, its
  mute, and its ``ext:<name>`` health when that check has run. Per job: its schedule,
  level, next due time (from the manifest's cron, in the job's zone or ``settings.timezone``:
  an ``ext_jobs`` row exists only after the first due time), and the last run's outcome and
  fail streak from ``ext_jobs``. Manifest strings never appear, only names. ``core`` lists
  the core jobs the same way (``core/briefing`` while ``briefing.enabled``, ``core/pulse``
  while ``pulse.enabled``).
- ``mute_extension`` (L3): ``{extension, until}`` holds that extension's notifications
  until ``until`` (ISO with an offset, in the future, at most 7 days ahead); ``{extension,
  off: true}`` lifts it, through the notifier's API (``notify.mute`` / ``notify.unmute``:
  kv ``ext.<name>.muted_until``, audited ``ext.notify_muted`` / ``ext.notify_unmuted``).

Handlers don't see the requester; the gate limits callers to principals who may request
global tools (the owner).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from resonant.executor import ToolFailed, ToolHandler
from resonant.extensions import briefing, notify, pulse_job
from resonant.extensions.cron import CronError, parse_schedule
from resonant.monitor import health_snapshot
from resonant.store import extensions as store
from resonant.store.db import iso
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


def _health(snapshot: dict[str, dict[str, Any]], ext: str) -> str | None:
    """The ``ext:<name>`` health check's state, or None if it has never run."""
    state: object = snapshot.get(f"ext:{ext}", {}).get("state")
    return state if isinstance(state, str) and state != "unknown" else None


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
    health = health_snapshot(conn)
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
            "health": _health(health, record.name),
        }
        if (until := notify.muted_until(conn, record.name, now)) is not None:
            item["muted_until"], item["muted_until_text"] = iso(until), _when(until, now, tz)
        try:
            manifest = Manifest.model_validate(record.manifest)
        except ValidationError:
            item["status"], item["reason"] = "refused", "manifest no longer validates"
            out.append(item)
            continue
        item["tools"] = len(manifest.tools)
        states = {j.job_id: j for j in store.list_jobs(conn, record.name)}
        item["jobs"] = [
            _job(
                job.id,
                job.schedule,
                job.tz or tz,
                job.spec(record.name).effective_level.value,
                states.get(job.id),
                now,
                tz,
            )
            for job in sorted(manifest.jobs, key=lambda j: j.id)
        ]
        out.append(item)
    return {"extensions": out, "count": len(out), "core": _core_jobs(ctx, now)}


def _job(
    job_id: str,
    schedule: str,
    zone: str,
    level: str,
    state: store.JobState | None,
    now: datetime,
    tz: str,
) -> dict[str, Any]:
    """One job's line: next due from its cron (an ``ext_jobs`` row exists only after the
    first due time), last run from ``ext_jobs``."""
    try:
        next_due = parse_schedule(schedule, zone).next_after(now)
    except (CronError, ValueError):
        next_due = None
    last_at = state.last_run_at if state is not None else None
    return {
        "id": job_id,
        "schedule": schedule,
        "level": level,
        "next_due": None if next_due is None else iso(next_due),
        "next_due_text": _when(next_due, now, tz),
        "last_run_at": None if last_at is None else iso(last_at),
        "last_run_text": _when(last_at, now, tz),
        "last_outcome": state.last_outcome if state is not None else None,
        "fail_streak": state.fail_streak if state is not None else 0,
    }


def _core_jobs(ctx: BuiltinContext, now: datetime) -> list[dict[str, Any]]:
    """Core jobs (``ext="core"``): the briefing while ``briefing.enabled`` and the pulse
    while ``pulse.enabled``, L0 in ``settings.timezone``."""
    settings = ctx.state.settings
    tz = settings.timezone
    out: list[dict[str, Any]] = []
    if settings.briefing.enabled:
        state = store.get_job(ctx.conn, store.CORE_EXT, briefing.JOB_ID)
        out.append(
            _job(briefing.JOB_ID, briefing.schedule(settings.briefing), tz, "L0", state, now, tz)
        )
    if settings.pulse.enabled:
        state = store.get_job(ctx.conn, store.CORE_EXT, pulse_job.JOB_ID)
        out.append(_job(pulse_job.JOB_ID, pulse_job.schedule(), tz, "L0", state, now, tz))
    return out


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
        was = notify.unmute(ctx.conn, name, by=owner, now=now)
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
    notify.mute(ctx.conn, name, until, by=owner)
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

"""``core/mcp`` (PER-275): the owner-facing half of MCP tool drift.

The runtime (``extensions/mcp.py``) decides what a changed tool is; the host
(``extensions/host.py``) saves a re-pin and, for a write or pay tool whose change isn't
benign, records the question as a task-less approval (``store.approvals.REPIN_TOOL``) and
queues the removal heads-up (``NOTES_KEY``). This module holds the code-built texts and the
job that finishes the work, once a minute (like ``core/remind``):

1. **Settle** each decided question (``unsettled_repins``): a yes re-pins the tool
   (``accept_pending``) and texts ``Re-pinned <tool>.``; a no turns it off for the life of
   the install (``declined``; ``resonant ext update`` clears it) and texts that. A question
   from before the last install, or about a server that has since moved on, is spent with
   no text. The row is spent (``approvals.settle``) only after its text went out or was
   refused.
2. **Expire** a pending question whose tool went back to what was approved (``reverted``) or
   whose extension is gone (``removed``).
3. **Prompt** each pending question the owner hasn't been sent (``[A7] Linear changed
   save_issue (+required field team_id). Accept? Reply yes / no``). A question that can't
   be sent just expires (24 h, with the usual half-way reminder); the tool is unaffected.
4. **Notes.** Send each queued heads-up as a non-urgent notice (``brain.notices``: pause,
   quiet hours and caps apply; a held one rides the pulse).

Up to a minute passes between a reply and its effect; the tool keeps working at its approved
level, and the gate still governs every call. A no also bites on the next call (the runtime
asks ``host.declined``). Texts are code-built; a server's property name is shown only if it
looks like an identifier. Nothing here touches a manifest, a tool's effect or level, a URL or
a secret.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from resonant.brain.notices import send_notice
from resonant.config import Settings
from resonant.extensions.mcp import SchemaChange
from resonant.extensions.notify import clean_text
from resonant.gateway.channel import Channel, ChannelError, ChannelRefused
from resonant.store import approvals as ap
from resonant.store import extensions as store
from resonant.store import system_texts
from resonant.store.audit import audit
from resonant.store.db import atomic, kv_get, kv_set, utcnow

if TYPE_CHECKING:
    from resonant.executor import CallContext
    from resonant.extensions.host import ExtensionHost
    from resonant.extensions.mcp import McpServerRuntime
    from resonant.extensions.scheduler import CoreDeps, CoreJob

log = logging.getLogger(__name__)

JOB_ID = "mcp"
SCHEDULE = "* * * * *"
CATCHUP = timedelta(minutes=2)
NOTES_KEY = "ext.mcp_notes"  # kv: {"<ext>/<tool>": {"key": notice key, "title": text}}
REPIN_TTL_S = 24 * 3600  # an unprompted question: the gate's 30 minutes is too short
_IDENT = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_NAMES_MAX = 3


def _names(names: Sequence[str]) -> str:
    """Server-supplied property names: identifiers only, at most three, then ``+N more``."""
    shown = [n if _IDENT.match(n) else "a field" for n in names[:_NAMES_MAX]]
    more = len(names) - _NAMES_MAX
    return ", ".join(shown) + (f" +{more} more" if more > 0 else "")


def repin_summary(ext: str, tool: str, change: SchemaChange) -> str:
    """``Linear changed save_issue (+required field team_id). Accept`` (the prompt adds the
    ``?``). Code-built from the diff of the schemas, not from any server text."""
    parts: list[str] = []
    if change.added_required:
        parts.append(f"+required field {_names(change.added_required)}")
    if change.removed:
        parts.append(f"removed field {_names(change.removed)}")
    if change.changed:
        parts.append(f"changed field {_names(change.changed)}")
    if change.unrequired:
        parts.append(f"no longer required: {_names(change.unrequired)}")
    if change.other:
        parts.append("other input rules changed")
    what = "; ".join(parts) or "what it takes changed"
    return clean_text(f"{ext.capitalize()} changed {tool} ({what}). Accept")


def auto_title(ext: str, tool: str, effect: str, change: SchemaChange) -> str:
    """The pulse line for a re-pin the daemon accepted on its own."""
    if effect == "read":
        why = "read-only"
    elif change.added_optional:
        why = "optional field added"
    else:
        why = "no change to what it takes"
    return clean_text(f"{ext.capitalize()} updated {tool}; I accepted it ({why})")


def removed_title(ext: str, tool: str) -> str:
    return clean_text(
        f"{ext.capitalize()}: the server no longer offers {tool}; I turned it off. "
        f"resonant ext update {ext} to review"
    )


def pending_notes(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    value: object = kv_get(conn, NOTES_KEY)
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for k, v in cast(Mapping[object, object], value).items():
        if isinstance(k, str) and isinstance(v, dict):
            entry = cast(Mapping[object, object], v)
            key, title = entry.get("key"), entry.get("title")
            if isinstance(key, str) and isinstance(title, str):
                out[k] = {"key": key, "title": title}
    return out


def queue_note(conn: sqlite3.Connection, name: str, key: str, title: str) -> None:
    """Queue one heads-up (caller holds the transaction)."""
    notes = pending_notes(conn)
    notes[name] = {"key": key, "title": title}
    kv_set(conn, NOTES_KEY, notes)


def _split(row: ap.Approval) -> tuple[str, str] | None:
    """``(extension, tool)`` of a re-pin question."""
    ext, _, tool = (row.action_target or "").partition("/")
    return (ext, tool) if ext and tool else None


def _runtime(host: ExtensionHost, ext: str, tool: str) -> McpServerRuntime | None:
    loaded = host.loaded.get(ext)
    if loaded is None:
        return None
    return next((r for r in loaded.mcp.values() if tool in r.approved), None)


async def _say(
    conn: sqlite3.Connection, channel: Channel, owner: str, text: str, key: str, now: datetime
) -> bool:
    """Send a result text; False after a transient failure (try again next run)."""
    try:
        await channel.send(owner, text, dedupe_key=key)
    except ChannelRefused:
        return True  # unreachable for good: nothing more to do
    except ChannelError as e:
        log.warning("mcp re-pin text failed (will retry): %s", e)
        return False
    system_texts.record(conn, owner, "result", text, ref=key, now=now)
    return True


async def _settle(
    conn: sqlite3.Connection,
    host: ExtensionHost,
    channel: Channel,
    owner: str,
    now: datetime,
) -> int:
    done = 0
    for row in ap.unsettled_repins(conn):
        target = _split(row)
        if target is None:
            continue
        ext, tool = target
        record = store.get_extension(conn, ext)
        runtime = _runtime(host, ext, tool)
        if record is None or row.created_at < record.installed_at or runtime is None:
            _spend(conn, row, now, "ext.mcp_repin_stale")
            done += 1
            continue
        if row.status == "rejected":
            runtime.disable(tool, "declined")
            text = f"Okay: {tool} is off until you run resonant ext update {ext}."
            event = "ext.mcp_repin_declined"
        elif runtime.approved.get(tool) == row.intent_hash or runtime.accept_pending(
            tool, row.intent_hash
        ):
            text, event = f"Re-pinned {tool}.", ""  # the save audited itself (ext.mcp_repinned)
        elif runtime.verified_once:
            _spend(conn, row, now, "ext.mcp_repin_stale")  # moved on or changed again
            done += 1
            continue
        else:
            continue  # not verified yet this process: the first check will accept it
        if not await _say(conn, channel, owner, text, f"mcp-repin:{row.id}:decided", now):
            continue
        _spend(conn, row, now, event)
        done += 1
    return done


def _spend(conn: sqlite3.Connection, row: ap.Approval, now: datetime, event: str) -> None:
    with atomic(conn):
        if ap.settle(conn, row.id, now) and event:
            audit(
                conn,
                event,
                extension=row.extension,
                tool=row.action_target,
                approval_id=row.id,
                decision=row.status,
            )


def _expire(conn: sqlite3.Connection, host: ExtensionHost, now: datetime) -> int:
    n = 0
    for row in ap.pending_repins(conn):
        target = _split(row)
        if target is None:
            continue
        ext, tool = target
        runtime = _runtime(host, ext, tool)
        record = store.get_extension(conn, ext)
        reason: str | None = None
        if record is None or row.created_at < record.installed_at:
            reason = "removed"
        elif runtime is not None and runtime.verified_once and tool not in runtime.pending:
            reason = "reverted"
        if reason is not None and ap.expire_one(conn, row.id, now, reason=reason):
            n += 1
    return n


async def _prompt(conn: sqlite3.Connection, channel: Channel, owner: str, now: datetime) -> int:
    from resonant.runners.approve import prompt_row

    sent = 0
    for row in ap.pending_repins(conn):
        if row.expires_at <= now or owner not in row.approvers or row.sent_at(owner) is not None:
            continue
        result = await prompt_row(conn, channel, row, now)
        sent += len(result.reached)
    return sent


async def _drain(
    conn: sqlite3.Connection,
    channel: Channel,
    owner: str,
    settings: Settings,
    now: datetime,
) -> int:
    sent = 0
    for name, note in sorted(pending_notes(conn).items()):
        try:
            await send_notice(
                conn, channel, owner, settings, key=note["key"], title=note["title"], now=now
            )
        except ChannelError as e:
            log.warning("mcp heads-up failed (will retry): %s", e)
            continue
        sent += 1
        with atomic(conn):
            notes = pending_notes(conn)
            if notes.get(name) == note:
                del notes[name]
            kv_set(conn, NOTES_KEY, notes)
    return sent


async def run_once(
    conn: sqlite3.Connection,
    host: ExtensionHost,
    channel: Channel,
    owner: str,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    with atomic(conn):
        ap.expire_due(conn, now)
    settled = await _settle(conn, host, channel, owner, now)
    expired = _expire(conn, host, now)
    prompted = await _prompt(conn, channel, owner, now)
    notes = await _drain(conn, channel, owner, settings, now)
    return {"settled": settled, "expired": expired, "prompted": prompted, "notes": notes}


def mcp_drift_job(deps: CoreDeps, *, clock: Callable[[], datetime] = utcnow) -> CoreJob | None:
    """The ``CORE_JOBS`` factory for ``core/mcp``. None with no channel, owner or host."""
    from resonant.extensions.scheduler import CoreJob

    channel, owner, host = deps.channel, deps.owner, deps.components.extensions
    if channel is None or owner is None or host is None:
        return None
    conn, settings = deps.conn, deps.settings

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        del args, ctx
        return await run_once(conn, host, channel, owner, settings, clock())

    return CoreJob(id=JOB_ID, schedule=SCHEDULE, run=run, catchup=CATCHUP, timeout_s=30.0)

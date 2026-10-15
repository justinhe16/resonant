"""Update reads: ``pulse_preview``, ``open_items`` and ``briefing_preview``.

What the owner is told and what is still open, readable from chat (the ``updates`` intent)
as well as from ``/pulse``. All three are L0 reads and global builtins, so the gate lets
only principals who can request global tools (the owner) call them. None of them sends a
message or marks anything told; a send stays ``/pulse now`` and ``/ext run core/briefing``,
which are slash-only.

- ``pulse_preview``: ``{text}``, what the next pulse would say plus the open items per
  source (the same text as ``/pulse``: ``pulse_job.preview_text``).
- ``open_items``: open (uncleared, unexpired) pulse items, optionally one source's, rendered
  with ``pulse.render_item`` (titles were cleaned on ingest), at most ``OPEN_ITEMS_MAX``
  clipped to ``ITEM_MAX_CHARS``. Muted extensions' items are left out, like the pulse and the
  briefing, and so are the core approval re-mentions. An extension that reports through the
  pulse is answerable by chat with no extension code.
- ``briefing_preview``: ``{text}``, the briefing as it would be sent now (today's date in
  ``settings.timezone``), except that the approvals line is a count: approval codes never
  reach the model (``/approvals`` shows them).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from zoneinfo import ZoneInfo

from resonant.executor import ToolFailed, ToolHandler
from resonant.extensions import briefing, notify, pulse, pulse_job
from resonant.store import pulse as store
from resonant.tools.base import NO_ARGS, BuiltinContext, no_args
from resonant_sdk import Effect, ToolSpec

OPEN_ITEMS_MAX = 10
ITEM_MAX_CHARS = 120  # titles are untrusted text going to the model: keep them short
SOURCE_MAX = 48

PULSE_PREVIEW = ToolSpec(
    name="pulse_preview",
    description=(
        "What the next update (pulse) would say and how many items are open per source. "
        "Read only: nothing is sent or marked told."
    ),
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)
OPEN_ITEMS = ToolSpec(
    name="open_items",
    description=(
        "Open update items reported by Resonant and its extensions (for example open PRs "
        "or incidents), optionally only one source's. Read only."
    ),
    args_schema={
        "type": "object",
        "properties": {"source": {"type": "string", "maxLength": SOURCE_MAX}},
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=5.0,
)
BRIEFING_PREVIEW = ToolSpec(
    name="briefing_preview",
    description=(
        "The daily briefing as it would be sent now: reminders, work, approvals, health "
        "and updates. Read only: nothing is sent or marked told."
    ),
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)

SPECS: tuple[ToolSpec, ...] = (PULSE_PREVIEW, OPEN_ITEMS, BRIEFING_PREVIEW)


def pulse_preview(ctx: BuiltinContext) -> dict[str, Any]:
    return {"text": pulse_job.preview_text(ctx.conn, ctx.clock(), ctx.state.settings.timezone)}


def open_items(ctx: BuiltinContext, args: dict[str, Any]) -> dict[str, Any]:
    unknown = set(args) - {"source"}
    if unknown:
        raise ToolFailed(f"unknown arguments: {sorted(unknown)}")
    source = args.get("source")
    if source is not None and (not isinstance(source, str) or len(source) > SOURCE_MAX):
        raise ToolFailed(f"source must be a string of at most {SOURCE_MAX} chars")
    now = ctx.clock()
    wanted = source.strip().lower() if isinstance(source, str) and source.strip() else None
    found = [i for i in store.open_items(ctx.conn, now) if not pulse_job.is_approval_item(i)]
    muted = {s for s in {i.source for i in found} if notify.muted_until(ctx.conn, s, now)}
    found = [i for i in found if i.source not in muted and wanted in (None, i.source)]
    shown = [
        {
            "source": i.source,
            "text": pulse.render_item(i, max_chars=ITEM_MAX_CHARS),
            "urgency": i.urgency,
            "new": i.changed,
        }
        for i in found[:OPEN_ITEMS_MAX]
    ]
    return {
        "items": shown,
        "count": len(found),
        "truncated": len(found) > len(shown),
        "source": wanted,
    }


def briefing_preview(ctx: BuiltinContext) -> dict[str, Any]:
    now = ctx.clock()
    tz = ZoneInfo(ctx.state.settings.timezone)
    b = briefing.gather(
        ctx.conn, owner=ctx.principals.owner, now=now, day=now.astimezone(tz).date(), tz=tz
    )
    # Codes never reach the model: the approvals line becomes a count.
    counted = replace(b, approvals=tuple("" for _ in b.approvals))
    text = briefing.render(counted)
    line = briefing.approvals_line(counted)
    if line is not None:
        n = len(b.approvals)
        text = text.replace(line, f"{n} approval{'' if n == 1 else 's'} waiting, see /approvals", 1)
    return {"text": text}


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    async def _pulse_preview(args: dict[str, Any]) -> dict[str, Any]:
        no_args(PULSE_PREVIEW.name, args)
        return pulse_preview(ctx)

    async def _open_items(args: dict[str, Any]) -> dict[str, Any]:
        return open_items(ctx, args)

    async def _briefing_preview(args: dict[str, Any]) -> dict[str, Any]:
        no_args(BRIEFING_PREVIEW.name, args)
        return briefing_preview(ctx)

    return {
        PULSE_PREVIEW.name: _pulse_preview,
        OPEN_ITEMS.name: _open_items,
        BRIEFING_PREVIEW.name: _briefing_preview,
    }

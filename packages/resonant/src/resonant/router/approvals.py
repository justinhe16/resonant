"""The router's approval inbox: text replies to approval prompts, and ``/approvals``.

``ApprovalInbox.handle(event, text, principal)`` runs after slash commands (so ``/kill`` is
never read as a reply) and before keywords and the labeler:

1. ``parse_reply(text, truncated=payload.truncated)``. Text that doesn't parse returns None
   and routes normally; it is never swallowed.
2. The sender's identity (``imessage:<handle>``) must map to the event's principal: the
   gate trusts ``reply.actor``.
3. ``gate.resolve_reply`` binds, resolves and wakes in one transaction, idempotent per
   event id. On success (or a replay of the same event) nothing is sent here: the woken
   runner sends the templated confirmation.
4. A refusal gets a short, code-built reply (``refusal_reply``). A non-approver always gets
   ``NOTHING_WAITING``, so pending approvals are never revealed.

Spans ``router.approval`` carry the path, outcome and reason, never the text.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from resonant.gate.codes import pick_example
from resonant.gate.replies import ParsedReply, parse_reply
from resonant.gate.types import ApprovalReply, Gate, PendingApproval
from resonant.models import SpanFactory
from resonant.models.client import null_spans
from resonant.principals import Principals
from resonant.router.render import MAX_REPLY_CHARS, duration
from resonant.store import approvals as ap
from resonant_sdk import Event

NOTHING_WAITING = "Nothing is waiting for approval."
ALWAYS_UNSUPPORTED = "Not done: yes always isn't offered for that one."
CHANNEL = "imessage"
MAX_ARGS_CHARS = 1500

Outcome = Literal["resolved", "replay", "refused", "unauthenticated"]


@dataclass(frozen=True)
class InboxResult:
    """A message the inbox consumed. ``reply`` is None when nothing should be sent."""

    reply: str | None
    outcome: Outcome
    reason: str | None = None


def ask_codes(pending: Sequence[PendingApproval]) -> str:
    """The re-ask for an ambiguous plain reply: each code with its summary."""
    if not pending:
        return NOTHING_WAITING
    items = ", ".join(f"{p.code} ({p.summary})" for p in pending)
    return f"Which one? {items}. Reply yes <code> or no <code>."


def refusal_reply(
    reason: str | None,
    code: str | None,
    sent: Sequence[PendingApproval],
    picks: Sequence[int] | None = None,
) -> str:
    """The reply to a refused approval reply. ``sent``: the actor's pending approvals whose
    prompts they have already seen; ``picks``: the reply's ``yes 1,3`` numbers, if any.
    Unknown reasons get the generic reply."""
    if reason == "ambiguous":
        return ask_codes(sent)
    if reason == "code_required":
        # The one that needs its code; else (PER-261: a bare yes right after a Claude answer)
        # the only one the reply could mean.
        coded = [p for p in sent if p.require_code] or [
            p for p in sent if code is None or p.code == code
        ]
        target = coded[0].code if len(coded) == 1 else "<code>"
        if picks:
            return f"Reply yes {','.join(map(str, picks))} {target} to confirm."
        return f"Reply yes {target} to confirm."
    if reason == "not_a_batch":
        return "That one is a single change. Reply yes or no."
    if reason == "unknown_pick":
        batches = [p for p in sent if p.changes > 1 and (code is None or p.code == code)]
        if len(batches) != 1:
            return (
                "Not done: those numbers aren't on the list. Reply yes 1,3 <code> with its numbers."
            )
        b = batches[0]
        return (
            f"Not done: {b.code} has {b.changes} changes. "
            f"Reply yes {pick_example(b.changes)} {b.code} with numbers from its list."
        )
    if reason == "unknown_code":
        return f"Nothing is waiting with code {code}."
    if reason == "expired":
        return "That approval expired. Not done."
    if reason == "already_resolved":
        return "That was already answered."
    if reason == "task_not_waiting":
        return "That task isn't waiting anymore. Not done."
    if reason == "not_sent_before":
        return "That reply came before the question. Reply again to answer it."
    if reason == "always_unsupported":
        target = [p for p in sent if p.code == code] if code else list(sent)
        if len(target) == 1 and target[0].require_code:
            return f"{ALWAYS_UNSUPPORTED} Reply yes {target[0].code} to confirm."
        return f"{ALWAYS_UNSUPPORTED} Reply yes or no."
    return NOTHING_WAITING  # nothing_pending, unsupported, not_found, anything else


class ApprovalInbox:
    def __init__(self, conn: sqlite3.Connection, *, principals: Principals, gate: Gate) -> None:
        self.conn = conn
        self.principals = principals
        self.gate = gate

    def handle(
        self, event: Event, text: str, principal: str, *, spans: SpanFactory | None = None
    ) -> InboxResult | None:
        """Bind ``text`` as an approval reply, or None if it isn't one."""
        parsed = parse_reply(text, truncated=event.payload.get("truncated") is True)
        if parsed is None:
            return None
        with (spans or null_spans)("router.approval", path="parser") as s:
            result = self._handle(event, text, principal, parsed)
            s.set(outcome=result.outcome, reason=result.reason, decision=parsed.decision)
        return result

    def _handle(self, event: Event, text: str, principal: str, parsed: ParsedReply) -> InboxResult:
        handle: object = event.payload.get("handle")
        identity = f"{CHANNEL}:{handle}"
        if not isinstance(handle, str) or self.principals.resolve(identity) != principal:
            return InboxResult(NOTHING_WAITING, "unauthenticated", "identity_mismatch")
        replay = bool(ap.by_resolved_event(self.conn, event.id))
        result = self.gate.resolve_reply(
            ApprovalReply(
                actor=principal,
                actor_identity=identity,
                channel=CHANNEL,
                raw_text=text,
                decision=parsed.decision,
                path="parser",
                event_id=event.id,
                occurred_at=event.occurred_at,
                code=parsed.code,
                comments=parsed.comments,
                always=parsed.always,
                picks=parsed.picks,
            )
        )
        if result.ok:
            return InboxResult(None, "replay" if replay else "resolved")
        sent = self.sent_pending(principal)
        return InboxResult(
            refusal_reply(result.reason, parsed.code, sent, parsed.picks), "refused", result.reason
        )

    def sent_pending(self, principal: str) -> list[PendingApproval]:
        """The principal's pending approvals whose prompts were already sent to them."""
        out: list[PendingApproval] = []
        for p in self.gate.pending_for(principal):
            row = ap.get_approval(self.conn, p.approval_id)
            if row is not None and row.sent_at(principal) is not None:
                out.append(p)
        return out


# --- /approvals --------------------------------------------------------------------------


def render_pending(pending: Sequence[PendingApproval], now: datetime) -> str:
    """``/approvals``: one line per pending approval, oldest first."""
    if not pending:
        return NOTHING_WAITING
    lines = [f"Waiting for approval ({len(pending)}):"]
    for p in pending:
        lines.append(f"- {p.code} · {p.summary} · expires in {_left(p.expires_at, now)}")
    lines.append("Reply yes <code> or no <code>. /approvals <code> shows the details.")
    return _clip("\n".join(lines))


def render_detail(a: ap.Approval, args: dict[str, Any] | None, now: datetime) -> str:
    """``/approvals <code>``: the summary plus the intent's full args (owner only)."""
    tool = f"{a.extension}: {a.tool}" if a.extension else a.tool
    shown = "(not recorded)" if args is None else json.dumps(args, ensure_ascii=False, indent=1)
    if len(shown) > MAX_ARGS_CHARS:
        shown = shown[:MAX_ARGS_CHARS] + "… (truncated)"
    answer = f"yes {a.code}" if a.require_code else "yes"
    return _clip(
        "\n".join(
            [
                f"[{a.code}] {a.summary}",
                f"Tool: {tool} ({a.level.value}, {a.effect.value})",
                f"Args: {shown}",
                f"Expires in {_left(a.expires_at, now)}. Reply {answer} / no {a.code}",
            ]
        )
    )


def approvals_reply(
    conn: sqlite3.Connection,
    pending: Sequence[PendingApproval],
    code: str | None,
    *,
    is_owner: bool,
    now: datetime,
) -> str | None:
    """The ``/approvals [code]`` reply over the asker's ``pending`` approvals (the gate's
    ``pending_for``), or None when a code lookup isn't allowed (owner only)."""
    if code is None:
        return render_pending(pending, now)
    if not is_owner:
        return None
    match = next((p for p in pending if p.code == code), None)
    row = ap.get_approval(conn, match.approval_id) if match is not None else None
    if row is None:
        return f"Nothing is waiting with code {code}."
    batch = ap.batches_of(conn, [row.id]).get(row.id)
    if batch is not None and batch.used_at is None:
        members = [m for m in (ap.get_approval(conn, i) for i in batch.approval_ids) if m]
        return render_batch_detail(conn, members, now)
    return render_detail(row, ap.intent_args(conn, row), now)


BATCH_ARGS_CHARS = 200  # each change's args in /approvals <code> for a batch


def render_batch_detail(
    conn: sqlite3.Connection, members: Sequence[ap.Approval], now: datetime
) -> str:
    """``/approvals <code>`` for a batch (BA-1): each change with its tool and full args
    (clipped), then the answer: for all of them, or (PER-255) for some by number."""
    head = members[0]
    lines = [f"[{head.code}] {len(members)} changes:"]
    for i, a in enumerate(members, 1):
        tool = f"{a.extension}: {a.tool}" if a.extension else a.tool
        args = ap.intent_args(conn, a)
        shown = "(not recorded)" if args is None else json.dumps(args, ensure_ascii=False)
        if len(shown) > BATCH_ARGS_CHARS:
            shown = shown[:BATCH_ARGS_CHARS] + "… (truncated)"
        lines.append(f"{i}. {tool} ({a.level.value}): {shown}")
    answer = f"yes {head.code}" if any(a.require_code for a in members) else "yes"
    left = _left(min(a.expires_at for a in members), now)
    some = f"yes {pick_example(len(members))} {head.code}"
    lines.append(f"Expires in {left}. Reply {answer} / no {head.code}, or {some} for just those.")
    return _clip("\n".join(lines))


def _left(expires_at: datetime, now: datetime) -> str:
    return duration((expires_at - now).total_seconds())


def _clip(text: str) -> str:
    return text if len(text) <= MAX_REPLY_CHARS else text[: MAX_REPLY_CHARS - 1] + "…"

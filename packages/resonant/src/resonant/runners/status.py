"""Status lines in the conversation that asked (PER-319): the thread keeps up.

Three short, code-built lines, each sent at most once per task and state:

- ``On it: starting R5 (fix the README typo).`` when a run starts work the asking reply
  didn't already announce (``start_line``; a run started by the Claude brain, not ``/run``,
  whose reply is the run's own ``On it: R5 · …``).
- ``Waiting on an approval from <Owner> or <Approver>.`` when a task waits on an approval whose
  prompt isn't in the asking conversation (``waiting_line``; a member's ask in a Slack DM).
  Nothing extra when the prompt is there (PER-320's one copy in a shared thread, or the
  asker's own prompt).
- ``Couldn't finish that: <plain reason>.`` when it fails or is cancelled
  (``failed_line``): a conversational task's terminal ``Fail`` (the loop's ``on_failed``
  hook: an approval prompt nobody got, a reply that couldn't go out, a step that crashed), a
  denial or an expiry (threaded conversations: iMessage's ``Not done …`` reply already says
  it), and a run that failed or was cancelled (threaded conversations: the run's end text is
  iMessage's line).

The reason is :func:`plain_reason`: the first line of the error, secrets redacted
(``redact_secrets``), cleaned (``clean_external``), clipped, with known channel errors said in
words (``messages_tab_disabled`` is ``Slack: Messages tab off``).

Who hears them. Every line goes to the task's principal in the task's thread
(``thread_of``), through the channel like any reply (the adapter chunks at 3,000 chars).
``quiet`` conversations (no thread: the owner's iMessage) get the failure line only where a
failure was silent before; the start and waiting lines are for threaded conversations, so
iMessage is no chattier than it was. The start and waiting lines are skipped while paused;
the failure line goes out as replies do. Deduped per task and state by the channel
(``status_key``: ``status:<task id>:<state>``), so retries and restarts never repeat one. A
failed send is logged (type only) and never fails the task.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from resonant import killswitch
from resonant.gate.codes import clean_external
from resonant.gateway.channel import Channel, ChannelError, MessageRef, thread_of
from resonant.redact import redact_secrets

if TYPE_CHECKING:
    from resonant.loop.types import Task

log = logging.getLogger(__name__)

STARTED = "started"
WAITING = "waiting"
FAILED = "failed"
REASON_MAX = 160
TITLE_MAX = 80
# The runners whose tasks answer a message: their terminal failure is said in the
# conversation. Runs (``claude``) say theirs in ``ClaudeRunner``; jobs, reminders and watches
# have no asking conversation.
CONVERSATIONAL = frozenset({"local", "brain", "ask", "media"})

# Channel errors a person can act on, in words (Slack's error codes).
_KNOWN: tuple[tuple[str, str], ...] = (
    ("messages_tab_disabled", "Slack: Messages tab off"),
    ("not_in_channel", "Slack: not in that channel"),
    ("channel_not_found", "Slack: channel not found"),
    ("is_archived", "Slack: channel archived"),
    ("user_not_found", "Slack: user not found"),
    ("account_inactive", "Slack: account inactive"),
    ("cannot_dm_bot", "Slack: can't DM a bot"),
    ("ratelimited", "Slack: rate limited"),
    ("invalid_auth", "Slack: not signed in"),
    ("not_authed", "Slack: not signed in"),
    ("token_revoked", "Slack: token revoked"),
    ("slack: not connected", "Slack: not connected"),
)
# A runner's own prefixes, said in plain words (the rest of the error follows, cleaned).
_PREFIXES: tuple[tuple[str, str], ...] = (
    ("approval prompt failed:", "I couldn't send the approval"),
    ("approval prompt refused:", "I couldn't send the approval"),
    ("reply send failed:", "I couldn't send the reply"),
    ("reply refused:", "I couldn't send the reply"),
    ("ask reply failed:", "I couldn't send the answer"),
    ("ask reply refused:", "I couldn't send the answer"),
    ("reply failed:", "I couldn't send the reply"),
)
_EXCEPTION = re.compile(r"^([A-Z][A-Za-z]*(?:Error|Exception|Failed|Timeout))\b:?\s*")


def status_key(task_id: str, state: str) -> str:
    """The send dedupe key of one task's status line in one state."""
    return f"status:{task_id}:{state}"


def start_line(code: str, title: str | None = None) -> str:
    """``On it: starting R5 (fix the README typo).``"""
    what = _clip(clean_external(title or ""), TITLE_MAX).rstrip(".")
    return f"On it: starting {code} ({what})." if what else f"On it: starting {code}."


def names_text(names: Sequence[str]) -> str:
    """``A``, ``A or B``, ``A, B or C``; ``an approver`` for none."""
    if not names:
        return "an approver"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


def waiting_line(names: Sequence[str]) -> str:
    """``Waiting on an approval from <Owner> or <Approver>.``"""
    return f"Waiting on an approval from {names_text(names)}."


def failed_line(reason: str) -> str:
    """``Couldn't finish that: <reason>.``"""
    reason = reason.strip().rstrip(".") or "something went wrong"
    return f"Couldn't finish that: {reason}."


def plain_reason(error: str | None) -> str:
    """A short, cleaned summary of a failure for the conversation: the first line only (no
    stack trace), secrets redacted, brackets and code look-alikes dropped, known channel
    errors in words, clipped to ``REASON_MAX``."""
    first = (error or "").strip().splitlines()[0] if (error or "").strip() else ""
    text = clean_external(redact_secrets(first))
    if not text:
        return "something went wrong"
    lead = ""
    low = text.lower()
    for prefix, said in _PREFIXES:
        if low.startswith(prefix):
            lead, text = said, text[len(prefix) :].strip()
            low = text.lower()
            break
    known = next((words for code, words in _KNOWN if code in low), None)
    if known is not None:
        text = known
    elif low.startswith("no approver is reachable"):
        text = "no approver could be reached"
    elif low.startswith("step exceeded its time limit"):
        text = "it took too long"
    elif (m := _EXCEPTION.match(text)) is not None and not lead:
        text = f"something went wrong ({m.group(1)})"
    if lead:
        text = f"{lead} ({text})" if text else lead
    return _clip(text, REASON_MAX).rstrip(".") or "something went wrong"


def quiet(task: Task) -> bool:
    """A conversation with no thread (the owner's iMessage): only the failure line, and only
    where a failure was silent before."""
    return thread_of(task, to=task.principal) is None


def paused(conn: sqlite3.Connection) -> bool:
    try:
        return killswitch.is_paused(conn)
    except (killswitch.PausedStateUnknownError, sqlite3.Error):
        return True  # unknown counts as paused


async def post(
    channel: Channel,
    task: Task,
    state: str,
    text: str,
    *,
    principal: str | None = None,
    thread: MessageRef | None = None,
) -> bool:
    """Send ``text`` as the task's ``state`` line, once (``status_key``), to ``principal``
    (default: the task's) in ``thread`` (default: the task's thread). True when it went out
    (or had already); a channel error is logged and False."""
    to = principal or task.principal
    if to is None:
        return False
    where = thread if thread is not None else thread_of(task, to=to)
    try:
        await channel.send(to, text, thread=where, dedupe_key=status_key(task.id, state))
    except ChannelError as e:
        log.warning("status line not sent", extra={"task": task.id, "state": state,
                                                   "error": type(e).__name__})  # fmt: skip
        return False
    return True


def display(name: str) -> str:
    """A principal's name as a line says it (``Principals.display``: ``lead`` -> ``Lead``)."""
    return name[:1].upper() + name[1:]


async def say_waiting(
    conn: sqlite3.Connection,
    channel: Channel,
    task: Task | None,
    approval_id: str,
    approvers: Sequence[str],
    *,
    here: bool,
) -> bool:
    """After an approval prompt went out: ``Waiting on an approval from …`` in the asking
    conversation when the prompt isn't there (``here`` False) and it is threaded, once per
    approval. ``approvers``: those who got it (an unlisted ``slack:U…`` isn't named). False
    when nothing was said."""
    if task is None or here or task.principal is None or quiet(task) or paused(conn):
        return False
    names = [display(a) for a in approvers if ":" not in a]
    return await post(channel, task, f"{WAITING}:{approval_id}", waiting_line(names))


def not_approved(conn: sqlite3.Connection, task_id: str, now: datetime) -> str | None:
    """Why a task's newest approval didn't go through, in words, or None (approved, used, or
    no approval): ``<Name> said no`` (``the approver`` for an unlisted one), or ``the approval
    expired before anyone answered``."""
    row = conn.execute(
        "SELECT status, decided_by, expires_at FROM approvals WHERE task_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    if row["status"] == "rejected":
        who = row["decided_by"]
        name = display(who) if isinstance(who, str) and who and ":" not in who else None
        return f"{name or 'the approver'} said no"
    expired = row["status"] == "expired" or (
        row["status"] == "pending" and datetime.fromisoformat(row["expires_at"]) <= now
    )
    return "the approval expired before anyone answered" if expired else None


class StatusLines:
    """The loop's ``on_failed`` hook (wired by ``wiring.build_stack``): a conversational
    task's terminal failure as one ``Couldn't finish that: …`` line in its conversation."""

    def __init__(self, conn: sqlite3.Connection, channel: Channel) -> None:
        self.conn, self.channel = conn, channel

    async def task_failed(self, task: Task, error: str) -> None:
        if task.runner not in CONVERSATIONAL or task.principal is None:
            return
        if task.checkpoint.get("watch") is not None:
            return  # a watch's ask: the watch decides whether anyone hears about it
        await post(self.channel, task, FAILED, failed_line(plain_reason(error)))


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

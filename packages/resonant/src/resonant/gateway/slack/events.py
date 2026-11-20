"""Pure parsing of Socket Mode envelopes (PER-295). No I/O: ``SlackChannel`` does the rest.

:func:`parse_message` reads an ``events_api`` payload into a :class:`Message`, or a
:class:`Dropped` with a short reason (never text):

- ``other_team``: the payload's ``team_id`` is missing or isn't ``channels.slack.team_id``, or
  the sender's ``user_team``/``team`` (when present) isn't. Only same-team senders are vouched
  for, and the check fails closed.
- ``self``: the bot's own message (``user`` is the bot, or any ``bot_id``).
- ``subtype``: an edit, delete, join, bot or any other subtype (only plain messages,
  ``thread_broadcast`` and ``file_share`` pass; files themselves are ignored in v1).
- ``untagged``: a channel message that neither tags the bot nor is a thread reply. The
  channel then keeps a thread reply only if the thread is followed.
- ``malformed``: a required field is missing or the wrong type (a truncated payload is never
  replied from).
- ``unsupported``: any other event type.

:func:`parse_click` reads an ``interactive`` ``block_actions`` payload into a
:class:`Click` on one of our approval buttons (``action_id`` ``resonant_approve`` or
``resonant_deny``; ``value`` is ``{"a": approval_id, "c": code}``), or None.

:func:`clean_text` strips the bot's own ``<@BOT>`` and unescapes Slack's ``&lt; &gt; &amp;``;
:func:`mentions` lists the other ``<@U…>`` users, which the channel renders as names.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

APPROVE_ACTION = "resonant_approve"
DENY_ACTION = "resonant_deny"
ACTIONS = (APPROVE_ACTION, DENY_ACTION)

_PASS_SUBTYPES = frozenset({"thread_broadcast", "file_share"})
_TS = re.compile(r"^\d{1,12}\.\d{1,9}$")
_ID = re.compile(r"^[A-Z0-9]{2,32}$")
_MENTION = re.compile(r"<@([UW][A-Z0-9]{2,31})(?:\|[^>]*)?>")

Kind = Literal["mention", "im", "reply"]


@dataclass(frozen=True)
class Message:
    """A message that may route. ``kind``: it tags the bot (``mention``), it's a DM
    (``im``), or it's an untagged reply in a thread (``reply``: routes only if followed).
    ``thread_ts`` is the thread's root (the message's own ``ts`` when it starts one)."""

    kind: Kind
    user: str
    channel: str
    ts: str
    thread_ts: str
    text: str  # raw Slack text (escaped, with <@U…> mentions)

    @property
    def thread(self) -> str:
        return f"{self.channel}:{self.thread_ts}"

    @property
    def ref(self) -> str:
        return f"{self.channel}:{self.ts}"


@dataclass(frozen=True)
class Dropped:
    reason: str


@dataclass(frozen=True)
class Click:
    """A click on an approval button. ``channel``/``ts``: the message clicked."""

    user: str
    channel: str
    ts: str
    thread_ts: str | None
    approval_id: str
    code: str
    decision: Literal["approve", "deny"]
    action_ts: str


def _s(d: dict[str, Any], key: str) -> str | None:
    v = d.get(key)
    return v if isinstance(v, str) and v else None


def _dict(v: object) -> dict[str, Any] | None:
    return cast(dict[str, Any], v) if isinstance(v, dict) else None


def parse_message(payload: dict[str, Any], *, team_id: str, bot_user_id: str) -> Message | Dropped:
    """An ``events_api`` envelope's payload as a :class:`Message`, or why it's dropped."""
    event = _dict(payload.get("event"))
    if event is None:
        return Dropped("malformed")
    if _s(payload, "team_id") != team_id:  # fails closed: the vouch rests on it
        return Dropped("other_team")
    for key in ("user_team", "team"):
        if _s(event, key) not in (None, team_id):
            return Dropped("other_team")
    etype = _s(event, "type")
    if etype not in ("app_mention", "message"):
        return Dropped("unsupported")
    if event.get("bot_id") is not None or _s(event, "user") == bot_user_id:
        return Dropped("self")
    subtype = event.get("subtype")
    if subtype is not None and subtype not in _PASS_SUBTYPES:
        return Dropped("subtype")
    user, channel, ts = _s(event, "user"), _s(event, "channel"), _s(event, "ts")
    text = event.get("text")
    thread_ts = event.get("thread_ts")
    if (
        user is None
        or channel is None
        or ts is None
        or not _ID.match(user)
        or not _ID.match(channel)
        or not _TS.match(ts)
        or not isinstance(text, str)
        or (thread_ts is not None and not (isinstance(thread_ts, str) and _TS.match(thread_ts)))
    ):
        return Dropped("malformed")
    root = thread_ts if isinstance(thread_ts, str) else ts
    if etype == "app_mention":
        kind: Kind = "mention"
    elif _s(event, "channel_type") == "im":
        kind = "im"
    elif f"<@{bot_user_id}" in text:
        kind = "mention"  # the message twin of an app_mention (same dedupe key)
    elif isinstance(thread_ts, str) and thread_ts != ts:
        kind = "reply"
    else:
        return Dropped("untagged")
    return Message(kind=kind, user=user, channel=channel, ts=ts, thread_ts=root, text=text)


def parse_click(payload: dict[str, Any], *, team_id: str) -> Click | Dropped:
    """A ``block_actions`` payload as a :class:`Click` on our approval button, or why not."""
    if payload.get("type") != "block_actions":
        return Dropped("unsupported")
    team = _dict(payload.get("team"))
    user = _dict(payload.get("user"))
    if team is None or user is None:
        return Dropped("malformed")
    if _s(team, "id") != team_id or _s(user, "team_id") not in (None, team_id):
        return Dropped("other_team")
    container = _dict(payload.get("container")) or {}
    channel = _s(container, "channel_id") or _s(_dict(payload.get("channel")) or {}, "id")
    message = _dict(payload.get("message")) or {}
    ts = _s(container, "message_ts") or _s(message, "ts")
    actions: object = payload.get("actions")
    uid = _s(user, "id")
    if (
        uid is None
        or channel is None
        or ts is None
        or not _ID.match(uid)
        or not isinstance(actions, list)
        or not actions
    ):
        return Dropped("malformed")
    action = _dict(cast(list[object], actions)[0])
    if action is None or _s(action, "action_id") not in ACTIONS:
        return Dropped("unsupported")
    try:
        value: object = json.loads(_s(action, "value") or "")
    except ValueError:
        return Dropped("malformed")
    v = _dict(value)
    approval_id = _s(v, "a") if v is not None else None
    code = _s(v, "c") if v is not None else None
    action_ts = _s(action, "action_ts") or ts
    if approval_id is None or code is None:
        return Dropped("malformed")
    return Click(
        user=uid,
        channel=channel,
        ts=ts,
        thread_ts=_s(message, "thread_ts"),
        approval_id=approval_id,
        code=code,
        decision="approve" if _s(action, "action_id") == APPROVE_ACTION else "deny",
        action_ts=action_ts,
    )


def clean_text(text: str, bot_user_id: str) -> str:
    """Strip the bot's ``<@BOT>`` and unescape ``&lt; &gt; &amp;``; whitespace trimmed."""
    out = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", "", text)
    return out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def mentions(text: str) -> list[str]:
    """The ``<@U…>`` users ``text`` mentions, in order, once each."""
    seen: list[str] = []
    for m in _MENTION.finditer(text):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def render_mentions(text: str, names: dict[str, str]) -> str:
    """``<@U123>`` -> ``@Name`` for every user in ``names``; others become ``@U123``."""
    return _MENTION.sub(lambda m: "@" + names.get(m.group(1), m.group(1)), text)


def ts_time(ts: str) -> datetime | None:
    """A Slack ``ts`` (``"1700000000.000100"``) as an aware datetime, or None."""
    try:
        return datetime.fromtimestamp(float(ts), UTC)
    except (ValueError, OverflowError, OSError):
        return None

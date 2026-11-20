"""Test doubles for the Slack transport (PER-295): a fake Web API and a fake socket.

Nothing here touches the network. ``FakeSlackWeb`` answers the methods ``SlackChannel``
calls and records every call; ``FakeSlackSocket`` hands envelopes to the channel's handler
and records acks. ``FakeSlackTransport`` builds both and remembers the tokens it was given
(so a test can check they never reach a log or an audit row). The envelope builders make
the Socket Mode shapes Slack sends: ``mention``, ``message``, ``click``.

Usage::

    web, socket = FakeSlackWeb(team_id="T1", bot_user_id="UBOT"), FakeSlackSocket()
    slack = SlackChannel(cfg, principals, conn, secrets=store,
                         transport=FakeSlackTransport(web, socket))
    await slack.start(submit)
    await socket.deliver(mention("U2", "<@UBOT> what's on staging?", team="T1"))
    assert web.posts[-1].thread_ts == "1700000000.000100"
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from resonant.gateway.slack.blocks import button_value
from resonant.gateway.slack.client import (
    Envelope,
    EnvelopeHandler,
    SlackCallError,
    SlackSocket,
    SlackWeb,
)
from resonant.gateway.slack.events import APPROVE_ACTION, DENY_ACTION


@dataclass(frozen=True)
class Post:
    """A ``chat.postMessage`` the fake answered."""

    channel: str
    text: str
    thread_ts: str | None
    ts: str
    blocks: list[dict[str, Any]] | None

    @property
    def buttons(self) -> bool:
        return any(b.get("type") == "actions" for b in self.blocks or [])


@dataclass
class FakeSlackWeb:
    team_id: str = "T0TEAM"
    bot_user_id: str = "U0BOT"
    names: dict[str, str] = field(default_factory=dict[str, str])  # users.info display names
    # conversations.info / conversations.list (PER-299): id -> {"name", "is_member", ...}
    channels: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )
    posts: list[Post] = field(default_factory=list[Post])
    updates: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    ephemerals: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    reactions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    # method -> errors raised by the next calls to it, in order (one per call)
    errors: dict[str, list[Exception]] = field(default_factory=dict[str, list[Exception]])
    auth_team: str | None = None  # auth.test answers this team (default: team_id)
    _ts: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def fail(self, method: str, *errors: Exception) -> None:
        self.errors.setdefault(method, []).extend(errors)

    def method_calls(self, method: str) -> list[dict[str, Any]]:
        return [a for m, a in self.calls if m == method]

    async def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, dict(args)))
        queued = self.errors.get(method)
        if queued:
            raise queued.pop(0)
        match method:
            case "auth.test":
                return {
                    "ok": True,
                    "team_id": self.auth_team or self.team_id,
                    "user_id": self.bot_user_id,
                }
            case "conversations.open":
                return {"ok": True, "channel": {"id": "D" + str(args["users"])[1:]}}
            case "chat.postMessage":
                ts = f"1800000000.{next(self._ts):06d}"
                self.posts.append(
                    Post(
                        args["channel"], args["text"], args.get("thread_ts"), ts, args.get("blocks")
                    )
                )
                return {"ok": True, "channel": args["channel"], "ts": ts}
            case "chat.update":
                self.updates.append(dict(args))
                return {"ok": True}
            case "chat.postEphemeral":
                self.ephemerals.append(dict(args))
                return {"ok": True}
            case "reactions.add":
                self.reactions.append(dict(args))
                return {"ok": True}
            case "conversations.info":
                cid = str(args["channel"])
                if cid not in self.channels:
                    raise SlackCallError(method, "channel_not_found")
                return {"ok": True, "channel": {"id": cid, **self.channels[cid]}}
            case "conversations.list":
                listed = [{"id": k, **v} for k, v in self.channels.items()]
                return {"ok": True, "channels": listed, "response_metadata": {"next_cursor": ""}}
            case "users.info":
                uid = str(args["user"])
                if uid not in self.names:
                    raise SlackCallError(method, "user_not_found")
                return {
                    "ok": True,
                    "user": {"id": uid, "profile": {"display_name": self.names[uid]}},
                }
            case _:
                raise SlackCallError(method, "unknown_method")


@dataclass
class FakeSlackSocket:
    handler: EnvelopeHandler | None = None
    acks: list[str] = field(default_factory=list[str])
    is_up: bool = False
    connects: int = 0
    closed: bool = False
    fail_connect: Exception | None = None

    async def connect(self, on_envelope: EnvelopeHandler) -> None:
        if self.fail_connect is not None:
            raise self.fail_connect
        self.handler = on_envelope
        self.is_up = True
        self.connects += 1

    async def ack(self, envelope_id: str) -> None:
        self.acks.append(envelope_id)

    async def connected(self) -> bool:
        return self.is_up

    async def close(self) -> None:
        self.is_up = False
        self.closed = True

    def drop(self) -> None:
        """The websocket dropped (the SDK reconnects on its own)."""
        self.is_up = False

    def reconnect(self) -> None:
        self.is_up = True
        self.connects += 1

    async def deliver(self, envelope: Envelope) -> None:
        assert self.handler is not None, "the channel isn't connected"
        await self.handler(envelope)


@dataclass
class FakeSlackTransport:
    """``Transport``: hands out ``web`` and ``socket``; remembers the tokens it got."""

    web: FakeSlackWeb
    socket: FakeSlackSocket
    tokens: list[str] = field(default_factory=list[str])

    def __call__(self, bot_token: str, app_token: str) -> tuple[SlackWeb, SlackSocket]:
        self.tokens += [bot_token, app_token]
        return self.web, self.socket


# --- envelopes --------------------------------------------------------------------------------

_envelopes = itertools.count(1)


def _envelope(
    kind: str, payload: dict[str, Any], *, envelope_id: str | None = None, retry_attempt: int = 0
) -> Envelope:
    return {
        "envelope_id": envelope_id or f"env-{next(_envelopes)}",
        "type": kind,
        "payload": payload,
        "retry_attempt": retry_attempt,
    }


def message(
    user: str,
    text: str,
    *,
    team: str = "T0TEAM",
    channel: str = "C0GENERAL",
    ts: str = "1700000000.000100",
    thread_ts: str | None = None,
    channel_type: str = "channel",
    event_type: str = "message",
    subtype: str | None = None,
    bot_id: str | None = None,
    user_team: str | None = None,
    envelope_id: str | None = None,
) -> Envelope:
    """An ``events_api`` envelope for one message event."""
    event: dict[str, Any] = {
        "type": event_type,
        "user": user,
        "text": text,
        "channel": channel,
        "ts": ts,
        "channel_type": channel_type,
        "team": user_team or team,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    if subtype is not None:
        event["subtype"] = subtype
    if bot_id is not None:
        event["bot_id"] = bot_id
    payload = {"team_id": team, "type": "event_callback", "event": event}
    return _envelope("events_api", payload, envelope_id=envelope_id)


def mention(user: str, text: str, **kw: Any) -> Envelope:
    """An ``app_mention`` envelope (``text`` should carry ``<@BOT>``)."""
    return message(user, text, event_type="app_mention", **kw)


def click(
    user: str,
    approval_id: str,
    code: str,
    *,
    approve: bool = True,
    team: str = "T0TEAM",
    channel: str = "C0GENERAL",
    ts: str = "1800000000.000001",
    thread_ts: str | None = None,
    action_ts: str = "1800000500.000001",
    envelope_id: str | None = None,
) -> Envelope:
    """An ``interactive`` ``block_actions`` envelope: ``user`` clicked Approve (or Deny)."""
    message: dict[str, Any] = {"ts": ts}
    if thread_ts is not None:
        message["thread_ts"] = thread_ts
    payload: dict[str, Any] = {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user, "team_id": team},
        "channel": {"id": channel},
        "container": {"type": "message", "channel_id": channel, "message_ts": ts},
        "message": message,
        "actions": [
            {
                "action_id": APPROVE_ACTION if approve else DENY_ACTION,
                "value": button_value(approval_id, code),
                "action_ts": action_ts,
            }
        ],
    }
    return _envelope("interactive", payload, envelope_id=envelope_id)


__all__ = [
    "FakeSlackSocket",
    "FakeSlackTransport",
    "FakeSlackWeb",
    "Post",
    "click",
    "mention",
    "message",
]

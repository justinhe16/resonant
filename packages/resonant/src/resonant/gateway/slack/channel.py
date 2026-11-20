"""``SlackChannel``: the Slack ``Channel`` adapter, over Socket Mode (PER-295).

Usage::

    slack = SlackChannel(settings.channels.slack, principals, conn, secrets=store, gate=gate)
    await slack.start(loop.submit)   # reads the two tokens, auth.test, opens the websocket
    await slack.send("brian", "On it: R12", thread=thread_of(task))
    await slack.post_room("digest", "Tuesday's digest ...")
    await slack.stop()

The daemon builds it when ``channels.slack.enabled`` (``wiring.build_channels``); the hub
routes a send here when the reply's thread is a Slack one or the principal's first running
identity is ``slack:``. Tokens are this instance's Keychain secrets ``slack/bot_token``
(``xoxb-``) and ``slack/app_token`` (``xapp-``), read once at start and handed to the
transport (``gateway.slack.client``); they are never logged, stored or put in an error.

**Inbound.** Every envelope is acked at once, then parsed (``gateway.slack.events``):

- Kept: a message that tags the bot (``app_mention``), a DM (``message.im``), an untagged
  reply in a *followed* thread, and clicks on our approval buttons (``block_actions``).
- Dropped: the bot's own messages, edits, joins and other subtypes, other teams' events,
  untagged messages elsewhere, malformed (truncated) payloads, messages longer than
  ``max_body_chars`` (never replied from), and senders that resolve to no principal
  (audited ``slack.unknown_sender``). Files and images are ignored in v1.
- The sender is resolved with ``Principals.resolve("slack:<U>", vouched=True)``: the team
  check above is the vouch (PER-293), so an unlisted same-team user gets the workspace
  default when there is one.
- The event: ``Event(source="slack", type="message", principal, payload={text, identity:
  "slack:<U>", channel, ts, thread_ts, thread: "<ch>:<thread_ts>", ref: "<ch>:<ts>",
  outside: true, at, truncated: false}, dedupe_key="slack:<ch>:<ts>")``. The bot's
  ``<@BOT>`` is stripped and other ``<@U…>`` are rendered as ``@Name`` (``users.info``,
  cached). A redelivered envelope has the same dedupe key, so the loop drops it. An accepted
  message gets an :eyes: reaction.

**Threads.** A thread is ``(channel, root ts)`` (``store/slack.py``). It is followed from the
first message that tags the bot (or any DM) until it has been idle ``follow_idle_days``. A
reply goes to the thread it answers: ``MessageRef("slack", "<ch>:<ts>", thread="<ch>:<root>")``.

**Outbound.** ``send(principal, text, thread=...)`` posts in ``thread`` when it is a known
thread (listed in ``slack_threads``), else DMs the principal's first ``slack:`` identity
(``conversations.open``, needs ``im:write``). Never any other destination: an unknown
principal, or one without a Slack identity, raises :class:`ChannelRefused` (audited
``slack.send_refused``). Text is escaped and split at 3,000 characters (``" (i/n)"``); PR
URLs and run codes become links (``blocks.format_text``). A 429 waits ``Retry-After`` once
when it is at most 30s, else raises :class:`ChannelRateLimited`; any other failure raises
:class:`ChannelSendError` with Slack's error code, never a body. Sends dedupe on
``dedupe_key`` (kv ``slack.sent:<sha256>``, 7 days), like iMessage.

**Rooms.** :meth:`post_room` posts only to a key of ``channels.slack.rooms``; any other key
raises :class:`ChannelRefused`. It is the one send that takes no principal (the documented
exception, ``ChannelHub.post_room``).

**Approvals.** :meth:`request_approval` posts ``prompt.text()`` with Approve and Deny buttons
(``blocks.approval_blocks``), in ``prompt.thread`` when set, else the principal's DM, deduped
by ``approval_dedupe_key``. Pay gets no buttons (only a typed ``yes <code>``). A click
becomes ``ApprovalReply(actor=resolve(slack:<clicker>), channel="slack", path="button",
code=<the prompt's code>)`` into ``gate.resolve_reply``; only the approval's approvers count:
anyone else gets an ephemeral "Only Justin or Brian can approve this." and nothing changes.
Once the approval is no longer pending (a click, a typed reply on any channel, expiry), the
message is edited to "Approved by Brian" / "Denied by Justin" / "Expired, not done" and its
buttons go (checked on every click and every ``sweep_s``). A second click gets "Already
approved by Brian." (the gate's resolve is atomic: the first wins).

**Health.** ``health()``: ``{ok, last_read_at, last_error, connected, last_event_at,
followed_threads, detail}``; ``self_test()`` is ``auth.test`` plus a connected socket (kv
``slack.selftest``). The monitor's ``slack`` check runs both.

**PII.** Bodies never reach logs or audit rows: audits carry the principal, ids, lengths and
reasons.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NoReturn, cast

from resonant.config import SlackConfig
from resonant.gateway.channel import (
    ApprovalPrompt,
    ChannelRateLimited,
    ChannelRefused,
    ChannelSendError,
    MessageRef,
    SelfTestResult,
    Submit,
    approval_dedupe_key,
)
from resonant.gateway.imessage.channel import split_text
from resonant.gateway.slack import blocks
from resonant.gateway.slack import events as ev
from resonant.gateway.slack.client import (
    Envelope,
    SlackCallError,
    SlackRateLimited,
    SlackSocket,
    SlackWeb,
    Transport,
    sdk_transport,
)
from resonant.principals import Principals
from resonant.store import approvals as ap
from resonant.store import outbound
from resonant.store import slack as store
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow
from resonant_sdk import Effect, Event

if TYPE_CHECKING:
    from resonant.gate.types import Gate
    from resonant.secrets import SecretStore

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]

CHANNEL_NAME = "slack"
DEDUPE_PREFIX = "slack.sent:"
DEDUPE_TTL = timedelta(days=7)
SELFTEST_KV = "slack.selftest"
RATE_MAX_WAIT_S = 30.0
SWEEP_S = 30.0
THREAD_SWEEP_EVERY = timedelta(hours=1)
FUTURE_SKEW = timedelta(minutes=5)
EYES = "eyes"
MAX_MENTION_LOOKUPS = 10  # users.info calls per message at most; the rest stay @U…
UNKNOWN_AUDIT_EVERY = timedelta(hours=1)  # slack.unknown_sender at most this often per identity
_RATE_WINDOW = timedelta(minutes=1)


@dataclass(frozen=True)
class _State:
    """Where an approval stands, for its buttons."""

    status: str  # pending | approved | rejected | expired
    by: str | None  # who decided, as a reply names them
    approvers: tuple[str, ...]
    pay: bool
    code: str | None  # the approval's (a batch: its first approval's) reply code


class SlackChannel:
    """The Slack ``Channel``. ``conn`` is Resonant's store. See the module docstring.

    ``transport`` builds the Web API caller and socket from the two tokens (tests inject
    ``gateway.slack.fixtures``); ``gate`` resolves button clicks (bound later with
    :meth:`bind_gate` when the hub is built before it)."""

    def __init__(
        self,
        config: SlackConfig,
        principals: Principals,
        conn: sqlite3.Connection,
        *,
        secrets: SecretStore | None,
        gate: Gate | None = None,
        transport: Transport | None = None,
        clock: Clock = utcnow,
        sleep: Sleep = asyncio.sleep,
        sweep_s: float = SWEEP_S,
    ) -> None:
        if config.team_id is None or config.bot_user_id is None:
            raise ValueError("channels.slack needs team_id and bot_user_id")
        self.config = config
        self.team_id: str = config.team_id
        self.bot_user_id: str = config.bot_user_id
        self.principals = principals
        self.conn = conn
        self.secrets = secrets
        self.gate = gate
        self.transport: Transport = transport or sdk_transport
        self.clock = clock
        self.sleep = sleep
        self.sweep_s = sweep_s
        self.web: SlackWeb | None = None
        self.socket: SlackSocket | None = None
        self._submit: Submit | None = None
        self._task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._names: dict[str, str] = {}
        self._dms: dict[str, str] = {}
        self._recent: dict[str, deque[datetime]] = {}
        self._unknown: dict[str, datetime] = {}  # identity -> last slack.unknown_sender audit
        self._connected = False
        self._connected_at: datetime | None = None
        self._last_event_at: datetime | None = None
        self._last_error: str | None = None
        self._threads_swept: datetime | None = None

    @property
    def name(self) -> str:
        return CHANNEL_NAME

    def bind_gate(self, gate: Gate) -> None:
        """Set the gate that resolves button clicks (``wiring.build_stack``)."""
        self.gate = gate

    @property
    def idle(self) -> timedelta:
        return timedelta(days=self.config.follow_idle_days)

    # --- lifecycle -----------------------------------------------------------------------

    async def start(self, submit: Submit) -> None:
        """Read the tokens, check them (``auth.test``: this team, this bot), open the socket
        and start the sweep. Raises (the hub then leaves Slack out and the monitor says why)
        on a missing token, a token for another workspace or bot, or a failed connect."""
        if self._task is not None:
            raise RuntimeError("slack channel already started")
        if self.secrets is None:
            raise RuntimeError("slack: no secret store")
        bot = await self.secrets.get(self.config.bot_token_ref)
        app = await self.secrets.get(self.config.app_token_ref)
        web, socket = self.transport(bot, app)
        del bot, app
        try:
            me = await web.call("auth.test", {})
        except SlackCallError as e:
            raise RuntimeError(f"slack: auth.test failed: {e.error}") from None
        if me.get("team_id") != self.team_id:
            raise RuntimeError("slack: the bot token is for another workspace")
        if me.get("user_id") != self.bot_user_id:
            raise RuntimeError("slack: the bot token is for another bot user")
        self.web, self.socket = web, socket
        self._submit = submit
        now = self.clock()
        with atomic(self.conn):
            swept = sweep_sent(self.conn, now)
            store.sweep_threads(self.conn, now, self.idle)
        if swept:
            log.info("slack: swept %d expired send records", swept)
        self._threads_swept = now
        try:
            await socket.connect(self._on_envelope)
        except Exception as e:
            self.web = self.socket = None
            self._submit = None
            raise RuntimeError(f"slack: socket connect failed ({type(e).__name__})") from None
        self._connected = True
        self._connected_at = self.clock()
        self._last_error = None
        self._task = asyncio.create_task(self._sweep_loop(), name="slack-sweep")
        log.info("slack: connected to %s as %s", self.team_id, self.bot_user_id)

    async def stop(self) -> None:
        """Stop the sweep and close the socket. Sends stay possible (the loop drains)."""
        task, self._task = self._task, None
        self._submit = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        socket = self.socket
        if socket is not None:
            try:
                await socket.close()
            except Exception as e:
                log.warning("slack: socket close failed (%s)", type(e).__name__)
        self._connected = False

    def health(self) -> dict[str, Any]:
        """``{ok, last_read_at, last_error, connected, last_event_at, followed_threads,
        detail}``. ``detail`` is code-built (the snapshot shows it)."""
        now = self.clock()
        try:
            n = store.count_followed(self.conn, now, self.idle)
        except sqlite3.Error:
            n = 0
        last = self._last_event_at or self._connected_at
        error = self._last_error
        if error is None and self._task is not None and not self._connected:
            error = "socket disconnected"
        event = iso(self._last_event_at) if self._last_event_at else None
        detail = (
            f"{'connected' if self._connected else 'disconnected'}, "
            f"{n} followed thread{'s' if n != 1 else ''}"
            + (f", last event {event}" if event else "")
        )
        return {
            "ok": self._connected and error is None,
            "last_read_at": iso(last) if last else None,
            "last_error": error,
            "connected": self._connected,
            "last_event_at": event,
            "followed_threads": n,
            "detail": detail,
        }

    async def self_test(self, *, timeout_s: float = 10.0) -> SelfTestResult:
        """``auth.test`` (this team and bot) and a connected socket. Records kv + audit."""
        result = await self._self_test(timeout_s)
        with atomic(self.conn):
            kv_set(
                self.conn,
                SELFTEST_KV,
                {"ok": result.ok, "at": iso(self.clock()), "detail": result.detail},
            )
            audit(
                self.conn,
                "slack.selftest",
                decision="ok" if result.ok else "failed",
                detail=result.detail,
            )
        return result

    async def _self_test(self, timeout_s: float) -> SelfTestResult:
        web, socket = self.web, self.socket
        if web is None or socket is None:
            return SelfTestResult(False, "not connected")
        try:
            async with asyncio.timeout(timeout_s):
                me = await web.call("auth.test", {})
                connected = await socket.connected()
        except TimeoutError:
            return SelfTestResult(False, f"auth.test timed out after {timeout_s:g}s")
        except SlackCallError as e:
            return SelfTestResult(False, f"auth.test failed: {e.error}")
        self._connected = connected
        if me.get("team_id") != self.team_id or me.get("user_id") != self.bot_user_id:
            return SelfTestResult(False, "auth.test: the token is for another workspace or bot")
        if not connected:
            return SelfTestResult(False, "auth.test ok, but the socket is disconnected")
        return SelfTestResult(True, f"auth.test ok as {self.bot_user_id}; socket connected")

    # --- inbound -------------------------------------------------------------------------

    async def _on_envelope(self, envelope: Envelope) -> None:
        """Ack at once, then handle. Never raises (the SDK would log the exception text)."""
        envelope_id = envelope.get("envelope_id")
        socket = self.socket
        if isinstance(envelope_id, str) and envelope_id and socket is not None:
            try:
                await socket.ack(envelope_id)
            except Exception as e:
                log.warning("slack: ack failed (%s)", type(e).__name__)
        self._last_event_at = self.clock()
        self._connected = True
        try:
            await self._dispatch(envelope)
        except Exception as e:  # a bug or bad data: the text may be in str(e)
            self._last_error = f"inbound error: {type(e).__name__}"
            log.warning("slack: inbound error (%s)", type(e).__name__)
        else:
            if self._last_error is not None and self._last_error.startswith("inbound error"):
                self._last_error = None

    async def _dispatch(self, envelope: Envelope) -> None:
        kind = envelope.get("type")
        raw: object = envelope.get("payload")
        if not isinstance(raw, dict):
            return
        payload = cast(dict[str, Any], raw)
        if kind == "events_api":
            parsed = ev.parse_message(payload, team_id=self.team_id, bot_user_id=self.bot_user_id)
            if isinstance(parsed, ev.Dropped):
                if parsed.reason in ("other_team", "malformed"):
                    log.info("slack: dropped an event (%s)", parsed.reason)
                return
            await self._on_message(parsed)
        elif kind == "interactive":
            click = ev.parse_click(payload, team_id=self.team_id)
            if isinstance(click, ev.Dropped):
                if click.reason in ("other_team", "malformed"):
                    log.info("slack: dropped a click (%s)", click.reason)
                return
            await self._on_click(click)

    async def _on_message(self, m: ev.Message) -> None:
        submit = self._submit
        if submit is None:
            return
        identity = f"slack:{m.user}"
        principal = self.principals.resolve(identity, vouched=True)  # same team: vouched
        now = self.clock()
        if principal is None:
            last = self._unknown.get(identity)
            if last is None or now - last >= UNKNOWN_AUDIT_EVERY:
                self._unknown[identity] = now
                with atomic(self.conn):
                    audit(self.conn, "slack.unknown_sender", identity=identity, ref=m.ref)
            return
        if m.kind == "reply":
            with atomic(self.conn):
                followed = store.followed(self.conn, m.channel, m.thread_ts, now, self.idle)
            if not followed:
                return  # an untagged reply in a thread Cori doesn't follow
        text = ev.clean_text(m.text, self.bot_user_id)
        if len(text) > self.config.max_body_chars:
            with atomic(self.conn):
                audit(
                    self.conn,
                    "slack.too_long",
                    requested_by=principal,
                    ref=m.ref,
                    length=len(text),
                    limit=self.config.max_body_chars,
                )
            return
        if not self._allow(principal, now):
            with atomic(self.conn):
                audit(
                    self.conn,
                    "slack.rate_limited",
                    requested_by=principal,
                    ref=m.ref,
                    limit_per_minute=self.config.inbound_per_minute,
                )
            return
        if users := ev.mentions(text)[:MAX_MENTION_LOOKUPS]:
            text = ev.render_mentions(text, {u: await self._user_name(u) for u in users})
        if not text:
            return
        at = ev.ts_time(m.ts) or now
        at = min(at, now + FUTURE_SKEW)
        with atomic(self.conn):
            if m.kind == "reply":
                store.touch(self.conn, m.channel, m.thread_ts, now)
            else:
                store.follow(self.conn, m.channel, m.thread_ts, principal, now)
        event = Event(
            source=CHANNEL_NAME,
            type="message",
            principal=principal,
            payload={
                "text": text,
                "identity": identity,
                "channel": m.channel,
                "ts": m.ts,
                "thread_ts": m.thread_ts,
                "thread": m.thread,
                "ref": m.ref,
                "outside": True,
                "at": iso(at),
                "truncated": False,
            },
            dedupe_key=f"slack:{m.channel}:{m.ts}",
            occurred_at=at,
        )
        if not submit(event):
            return  # a redelivered envelope (or the message twin of an app_mention)
        log.info("slack: message from %s (%d chars, %s)", principal, len(text), m.kind)
        web = self.web
        if web is not None:
            try:
                await web.call(
                    "reactions.add", {"channel": m.channel, "timestamp": m.ts, "name": EYES}
                )
            except SlackCallError as e:
                log.info("slack: reaction failed (%s)", e.error)

    def _allow(self, principal: str, now: datetime) -> bool:
        recent = self._recent.setdefault(principal, deque())
        while recent and now - recent[0] >= _RATE_WINDOW:
            recent.popleft()
        if len(recent) >= self.config.inbound_per_minute:
            return False
        recent.append(now)
        return True

    async def _user_name(self, uid: str) -> str:
        if (name := self._names.get(uid)) is not None:
            return name
        name = uid
        web = self.web
        if web is not None:
            try:
                info = await web.call("users.info", {"user": uid})
            except SlackCallError:
                info = {}
            user = info.get("user")
            if isinstance(user, dict):
                u = cast(dict[str, Any], user)
                profile = u.get("profile")
                p = cast(dict[str, Any], profile) if isinstance(profile, dict) else {}
                for v in (
                    p.get("display_name"),
                    p.get("real_name"),
                    u.get("real_name"),
                    u.get("name"),
                ):
                    if isinstance(v, str) and v.strip():
                        name = v.strip()
                        break
        self._names[uid] = name
        return name

    # --- approval buttons ----------------------------------------------------------------

    async def _on_click(self, click: ev.Click) -> None:
        identity = f"slack:{click.user}"
        actor = self.principals.resolve(identity, vouched=True)
        with atomic(self.conn):
            posted = store.prompt_at(self.conn, click.channel, click.ts)
        state = self._state(click.approval_id)
        if (
            state is None
            or state.code != click.code.upper()
            or (
                posted is not None
                and (posted.approval_id, posted.code) != (click.approval_id, click.code)
            )
        ):
            # the button's approval and code must be the ones posted (the gate binds by code)
            await self._ephemeral(click, "Nothing is waiting for approval.")
            return
        if state.status != "pending":
            await self._ephemeral(click, already_text(state))
            await self.sweep_prompts()
            return
        if actor is None or actor not in state.approvers:
            with atomic(self.conn):
                audit(
                    self.conn,
                    "slack.click_refused",
                    requested_by=actor,
                    identity=identity,
                    approval_id=click.approval_id,
                    reason="not an approver",
                )
            await self._ephemeral(
                click, f"Only {self._names_of(state.approvers)} can approve this."
            )
            return
        if state.pay:
            await self._ephemeral(click, f"Reply yes {click.code} to confirm.")
            return
        gate = self.gate
        if gate is None:
            await self._ephemeral(click, "Approvals aren't set up here. Reply yes or no.")
            return
        from resonant.gate.types import ApprovalReply

        result = gate.resolve_reply(
            ApprovalReply(
                actor=actor,
                actor_identity=identity,
                channel=CHANNEL_NAME,
                raw_text=f"button: {click.decision}",
                decision=click.decision,
                path="button",
                event_id=f"slack:click:{click.channel}:{click.ts}:{click.user}:{click.action_ts}",
                occurred_at=self.clock(),
                code=click.code,
            )
        )
        if not result.ok:
            after = self._state(click.approval_id)
            if after is not None and after.status != "pending":
                await self._ephemeral(click, already_text(after))
            else:
                from resonant.router.approvals import refusal_reply  # the router imports us

                await self._ephemeral(click, refusal_reply(result.reason, click.code, []))
        await self.sweep_prompts()

    def _state(self, approval_id: str) -> _State | None:
        head = ap.get_approval(self.conn, approval_id)
        approvers: tuple[str, ...] | None = None
        rows: list[ap.Approval] = []
        if head is None:
            batch = ap.get_batch(self.conn, approval_id)
            if batch is None:
                return None
            rows = [a for a in (ap.get_approval(self.conn, i) for i in batch.approval_ids) if a]
            if not rows:
                return None
            head, approvers = rows[0], (batch.principal,)
        else:
            rows = [head]
        status: str = head.status
        if status == "pending" and head.expires_at <= self.clock():
            status = "expired"
        by = head.decided_by
        shown = self.principals.display(by) if by and self.principals.known(by) else None
        return _State(
            status=status,
            by=shown,
            approvers=approvers or head.approvers,
            pay=any(a.effect == Effect.PAY for a in rows),
            code=head.code.upper() if head.code else None,
        )

    def _names_of(self, approvers: tuple[str, ...]) -> str:
        names = [self.principals.display(a) for a in approvers if ":" not in a]
        if not names:
            return "an approver"
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " or " + names[-1]

    async def _ephemeral(self, click: ev.Click, text: str) -> None:
        web = self.web
        if web is None:
            return
        args: dict[str, Any] = {"channel": click.channel, "user": click.user, "text": text}
        if click.thread_ts:
            args["thread_ts"] = click.thread_ts
        try:
            await web.call("chat.postEphemeral", args)
        except SlackCallError as e:
            log.info("slack: ephemeral failed (%s)", e.error)

    async def sweep_prompts(self) -> int:
        """Edit every button prompt whose approval is no longer pending (answered on any
        path, or expired): the outcome line, no buttons. Returns how many were edited."""
        web = self.web
        if web is None:
            return 0
        with atomic(self.conn):
            rows = store.prompts(self.conn)
        done = 0
        for p in rows:
            state = self._state(p.approval_id)
            if state is not None and state.status == "pending":
                continue
            outcome = blocks.outcome_text(
                state.status if state else "expired", state.by if state else None
            )
            text = blocks.format_text(p.text, self.config.link_base)
            try:
                await web.call(
                    "chat.update",
                    {
                        "channel": p.channel,
                        "ts": p.ts,
                        "text": f"{text}\n{blocks.escape(outcome)}",
                        "blocks": blocks.resolved_blocks(text, outcome),
                    },
                )
            except SlackRateLimited:
                continue  # the next sweep tries again
            except SlackCallError as e:
                log.info("slack: prompt edit failed (%s); forgetting it", e.error)
            with atomic(self.conn):
                store.forget(self.conn, p)
                audit(
                    self.conn,
                    "slack.prompt_closed",
                    approval_id=p.approval_id,
                    requested_by=p.principal,
                    outcome=outcome,
                )
            done += 1
        return done

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_s)
            try:
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("slack: sweep failed (%s)", type(e).__name__)

    async def _sweep_once(self) -> None:
        socket = self.socket
        if socket is not None:
            self._connected = await socket.connected()
        await self.sweep_prompts()
        now = self.clock()
        if self._threads_swept is None or now - self._threads_swept >= THREAD_SWEEP_EVERY:
            with atomic(self.conn):
                store.sweep_threads(self.conn, now, self.idle)
                sweep_sent(self.conn, now)
            self._threads_swept = now

    # --- outbound ------------------------------------------------------------------------

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        """Post ``text`` to ``principal``: in ``thread`` when it's a known Slack thread, else
        their DM. See the module docstring. Raises :class:`ChannelRefused`,
        :class:`ChannelRateLimited` or :class:`ChannelSendError`."""
        return await self._send(principal, text, thread=thread, dedupe_key=dedupe_key)

    async def _send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None,
        dedupe_key: str | None,
        prompt: ApprovalPrompt | None = None,
    ) -> MessageRef:
        if not text:
            raise ValueError("cannot send an empty message")
        uid = self._resolve(principal)
        if dedupe_key is not None and (prev := self._already_sent(dedupe_key)) is not None:
            return prev
        async with self._send_lock:
            if dedupe_key is not None and (prev := self._already_sent(dedupe_key)) is not None:
                return prev
            channel, thread_ts = await self._target(principal, uid, thread, dedupe_key)
            formatted = blocks.format_text(text, self.config.link_base)
            chunks = [formatted] if prompt is not None else split_text(formatted, blocks.MAX_CHUNK)
            claude = outbound.is_claude_send(dedupe_key)
            at = self.clock()
            if claude:
                outbound.note(self.conn, principal, at, dedupe_key)
            first: str | None = None
            for i, chunk in enumerate(chunks, 1):
                args: dict[str, Any] = {"channel": channel, "text": chunk, "unfurl_links": False}
                if thread_ts is not None:
                    args["thread_ts"] = thread_ts
                if prompt is not None and prompt.effect != Effect.PAY.value:
                    args["blocks"] = blocks.approval_blocks(prompt, chunk)
                body = await self._call(
                    "chat.postMessage", args, principal, dedupe_key, chunk=i, chunks=len(chunks)
                )
                ts = body.get("ts")
                if first is None and isinstance(ts, str):
                    first = ts
            if first is None:
                raise ChannelSendError("slack: chat.postMessage returned no ts")
            ref = MessageRef(
                channel=CHANNEL_NAME,
                ref=f"{channel}:{first}",
                thread=f"{channel}:{thread_ts}" if thread_ts is not None else None,
            )
            with atomic(self.conn):
                if dedupe_key is not None:
                    kv_set(
                        self.conn,
                        _dedupe_kv(dedupe_key),
                        {"ref": ref.ref, "thread": ref.thread, "at": iso(self.clock())},
                    )
                if prompt is not None and prompt.effect != Effect.PAY.value:
                    store.add_prompt(
                        self.conn,
                        store.Prompt(
                            prompt.approval_id, principal, channel, first, prompt.code, text
                        ),
                        self.clock(),
                    )
                audit(
                    self.conn,
                    "slack.sent",
                    requested_by=principal,
                    idempotency_key=dedupe_key,
                    length=len(text),
                    chunks=len(chunks),
                    ref=ref.ref,
                    threaded=thread_ts is not None,
                )
            if not claude:
                outbound.note(self.conn, principal, at, dedupe_key)
        log.info("slack sent to %s (%d chars, %d part(s))", principal, len(text), len(chunks))
        return ref

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        """Post ``prompt.text()`` with Approve/Deny buttons (none for pay), in
        ``prompt.thread`` when set, else the principal's DM; deduped by
        ``approval_dedupe_key`` (a repeat returns the first ref). Audits
        ``slack.approval_sent`` (ids, code and level; never the summary)."""
        key = approval_dedupe_key(prompt.approval_id, principal)
        self._resolve(principal)  # refuse an unknown principal even on a repeat
        if (prev := self._already_sent(key)) is not None:
            return prev
        ref = await self._send(
            principal, prompt.text(), thread=prompt.thread, dedupe_key=key, prompt=prompt
        )
        with atomic(self.conn):
            audit(
                self.conn,
                "slack.approval_sent",
                requested_by=principal,
                level=prompt.level,
                idempotency_key=key,
                approval_id=prompt.approval_id,
                code=prompt.code,
                ref=ref.ref,
                buttons=prompt.effect != Effect.PAY.value,
            )
        return ref

    async def post_room(self, key: str, text: str, *, dedupe_key: str | None = None) -> MessageRef:
        """Post ``text`` to the room ``channels.slack.rooms[key]``. Any other key raises
        :class:`ChannelRefused` (audited ``slack.room_refused``)."""
        if not text:
            raise ValueError("cannot send an empty message")
        room = self.config.rooms.get(key)
        if room is None:
            with atomic(self.conn):
                audit(self.conn, "slack.room_refused", room=key, reason="unknown room")
            raise ChannelRefused(f"slack: unknown room {key!r}")
        if dedupe_key is not None and (prev := self._already_sent(dedupe_key)) is not None:
            return prev
        async with self._send_lock:
            chunks = split_text(blocks.format_text(text, self.config.link_base), blocks.MAX_CHUNK)
            first: str | None = None
            for i, chunk in enumerate(chunks, 1):
                body = await self._call(
                    "chat.postMessage",
                    {"channel": room, "text": chunk, "unfurl_links": False},
                    None,
                    dedupe_key,
                    chunk=i,
                    chunks=len(chunks),
                )
                ts = body.get("ts")
                if first is None and isinstance(ts, str):
                    first = ts
            if first is None:
                raise ChannelSendError("slack: chat.postMessage returned no ts")
            ref = MessageRef(channel=CHANNEL_NAME, ref=f"{room}:{first}")
            with atomic(self.conn):
                if dedupe_key is not None:
                    kv_set(
                        self.conn,
                        _dedupe_kv(dedupe_key),
                        {"ref": ref.ref, "thread": None, "at": iso(self.clock())},
                    )
                audit(
                    self.conn,
                    "slack.room_posted",
                    room=key,
                    idempotency_key=dedupe_key,
                    length=len(text),
                    chunks=len(chunks),
                    ref=ref.ref,
                )
        return ref

    async def _target(
        self, principal: str, uid: str, thread: MessageRef | None, dedupe_key: str | None
    ) -> tuple[str, str | None]:
        """``(channel, thread_ts)``: the known thread, else the principal's DM."""
        if thread is not None and thread.channel == CHANNEL_NAME and thread.thread:
            channel, _, ts = thread.thread.partition(":")
            with atomic(self.conn):
                ok = bool(channel and ts) and store.known(self.conn, channel, ts)
            if ok:
                return channel, ts
            log.info("slack: thread for %s is not one Cori follows; sending a DM", principal)
        dm = self._dms.get(uid)
        if dm is None:
            body = await self._call("conversations.open", {"users": uid}, principal, dedupe_key)
            raw: object = body.get("channel")
            ch = cast(dict[str, Any], raw).get("id") if isinstance(raw, dict) else None
            if not isinstance(ch, str) or not ch:
                raise ChannelSendError("slack: conversations.open returned no channel")
            dm = self._dms[uid] = ch
        return dm, None

    async def _call(
        self,
        method: str,
        args: dict[str, Any],
        principal: str | None,
        dedupe_key: str | None,
        *,
        chunk: int | None = None,
        chunks: int | None = None,
    ) -> dict[str, Any]:
        web = self.web
        if web is None:
            raise ChannelSendError("slack: not connected (the channel isn't started)")
        for attempt in (0, 1):
            try:
                return await web.call(method, args)
            except SlackRateLimited as e:
                wait = e.retry_after
                delayed = attempt == 0 and wait <= RATE_MAX_WAIT_S
                with atomic(self.conn):
                    audit(
                        self.conn,
                        "slack.send_rate_limited",
                        requested_by=principal,
                        idempotency_key=dedupe_key,
                        method=method,
                        decision="delayed" if delayed else "refused",
                        wait_s=round(wait, 3),
                    )
                if not delayed:
                    raise ChannelRateLimited(
                        f"slack: rate limited on {method} (retry after {wait:g}s); not sent"
                    ) from None
                log.info("slack: rate limited on %s, waiting %.1fs", method, wait)
                await self.sleep(wait)
            except SlackCallError as e:
                with atomic(self.conn):
                    audit(
                        self.conn,
                        "slack.send_failed",
                        requested_by=principal,
                        idempotency_key=dedupe_key,
                        method=method,
                        reason=e.error,
                        chunk=chunk,
                        chunks=chunks,
                    )
                log.warning("slack: %s failed for %s: %s", method, principal, e.error)
                raise ChannelSendError(f"slack: {method} failed: {e.error}") from None
        raise ChannelRateLimited(f"slack: rate limited on {method}; not sent")

    def _resolve(self, principal: str) -> str:
        p = self.principals.get(principal)
        if p is None:
            self._refuse(principal, "unknown principal")
        uids = [i.partition(":")[2] for i in p.identities if i.startswith("slack:")]
        if not uids:
            self._refuse(principal, "principal has no slack identity")
        return uids[0]

    def _refuse(self, principal: str, reason: str) -> NoReturn:
        with atomic(self.conn):
            audit(self.conn, "slack.send_refused", requested_by=principal, reason=reason)
        raise ChannelRefused(f"slack: {reason}: {principal!r}")

    def _already_sent(self, dedupe_key: str) -> MessageRef | None:
        record: object = kv_get(self.conn, _dedupe_kv(dedupe_key))
        if not isinstance(record, dict):
            return None
        fields = cast(dict[str, object], record)
        ref, at, thread = fields.get("ref"), fields.get("at"), fields.get("thread")
        if not isinstance(ref, str) or not isinstance(at, str):
            return None
        try:
            expired = self.clock() - datetime.fromisoformat(at) >= DEDUPE_TTL
        except ValueError:
            return None
        if expired:
            return None
        return MessageRef(CHANNEL_NAME, ref, thread if isinstance(thread, str) else None)


def already_text(state: _State) -> str:
    """The ephemeral for a click on an approval that is no longer pending."""
    if state.status == "approved":
        return f"Already approved by {state.by}." if state.by else "Already approved."
    if state.status == "rejected":
        return f"Already denied by {state.by}." if state.by else "Already denied."
    return "That approval expired. Not done."


def _dedupe_kv(dedupe_key: str) -> str:
    return DEDUPE_PREFIX + hashlib.sha256(dedupe_key.encode()).hexdigest()


def sweep_sent(conn: sqlite3.Connection, now: datetime) -> int:
    """Delete send-dedupe records older than :data:`DEDUPE_TTL`. Returns the count. The
    caller owns the transaction."""
    cutoff = iso(now - DEDUPE_TTL)
    cur = conn.execute(
        "DELETE FROM kv WHERE key LIKE ? AND "
        "(json_extract(value, '$.at') IS NULL OR json_extract(value, '$.at') < ?)",
        (DEDUPE_PREFIX + "%", cutoff),
    )
    return cur.rowcount


__all__ = ["CHANNEL_NAME", "DEDUPE_TTL", "SELFTEST_KV", "SlackChannel", "already_text"]

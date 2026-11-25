"""Channel contract. Every channel adapter (iMessage, Slack) implements ``Channel``.

The daemon talks to one ``Channel``: the ``gateway.hub.ChannelHub`` over every enabled
adapter (PER-292). Runners, the router and the monitor never name an adapter; the hub
picks one per send (the reply's thread, else the principal's first identity on a running
adapter).

Inbound: an adapter resolves the sender's channel identity (``imessage:+1555…``,
``slack:U…``) to a principal through principals.yaml before calling ``submit``. The event's
``source`` is the adapter's ``name`` and ``type`` is ``"message"``; its payload carries
``text``, ``identity`` (the sender's full identity, which the approval inbox and the relay
re-check against the principal), ``ref`` (the adapter's id for this message) and, on a
threaded channel, ``thread`` (where replies go: Slack's ``thread_ts``, or the message's
own ``ts``). An adapter may add its own fields (iMessage: ``handle``, ``chat_guid``,
``guid``). Messages from unknown identities are dropped and audited. An adapter never emits
an event for:
- its own outgoing messages,
- messages from its own account or handle,
- self-test messages, which carry a nonce and go only to the self-test checker.

Content is untrusted input. Identity grants permissions, and message text never does.

Outbound: ``send`` takes a principal, never a raw address, and goes only to that
principal's identities from principals.yaml. Any other destination is refused and
audited. Sends are deduped by ``dedupe_key`` (the events idempotency key), rate-limited
by the adapter, and never interpolate text into scripts (e.g. osascript gets argv).

Threads: ``thread_of(task)`` is where a task's replies go (its ``channel_ref`` thread), or
None (iMessage, and every ``channel_ref`` written before PER-292). Replies to a message pass
``thread=``; proactive texts (pulse, briefing, monitor, notify) pass none. An unthreaded
adapter ignores ``thread``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from resonant.gate.codes import approval_text, batch_text
from resonant_sdk import Effect, Event, Level

if TYPE_CHECKING:
    from resonant.loop.types import Task


@dataclass(frozen=True)
class MessageRef:
    channel: str  # "imessage" | "slack"
    ref: str  # adapter-specific: an iMessage guid, a Slack "channel:ts"
    thread: str | None = None


@dataclass(frozen=True)
class ApprovalPrompt:
    approval_id: str
    code: str
    summary: str  # built by code from intent fields; external strings already sanitized
    effect: str
    level: str
    expires_at: datetime
    require_code: bool = False  # L1 (destructive or pay): only "yes <code>" counts
    always: bool = False  # a worker's trust check: the prompt offers "yes always"
    # BA-1: a batch prompt (``approval_id`` is the batch's id): one code-built line per change,
    # answered with one code; ``destructive``: one of them is an L1 write.
    lines: tuple[str, ...] = ()
    destructive: bool = False
    phrased: bool = False  # PER-268: the summary is the tool's own phrase: asked as a question
    # PER-292: the asking task's thread (``thread_of``), set only on the prompt to that task's
    # own principal; the hub sends there, an unthreaded adapter ignores it. Not in the text.
    # PER-320: in a shared thread (``shared_thread``) it is the one copy every approver answers.
    thread: MessageRef | None = None
    # PER-320: principals the threaded copy tags so they're notified (Slack: ``<@U…>`` for
    # each one's Slack identity; one without is left out). Set only when the requester isn't
    # an approver. Not in ``text()``; an unthreaded adapter ignores it.
    mention: tuple[str, ...] = ()
    # PER-300: one arg in full (a code fence built by ``gate.summary.detail_text``), under
    # the question. Never on a batch prompt.
    detail: str | None = None

    def text(self) -> str:
        """The prompt as sent: the destructive variant for L1 writes, the pay variant for pay,
        the ``yes always`` variant for a worker's trust check; a batch lists its changes."""
        if self.lines:
            return batch_text(
                self.code,
                self.lines,
                require_code=self.require_code,
                destructive=self.destructive,
            )
        destructive = self.level == Level.L1.value and self.effect != Effect.PAY.value
        text = approval_text(
            self.code,
            self.summary,
            require_code=self.require_code,
            destructive=destructive,
            always=self.always and not self.require_code and not destructive,
            phrased=self.phrased,
        )
        if not self.detail:
            return text
        # a fence in the line above would unbalance the block's fence (Slack renders both)
        return text.replace("```", "'''") + "\n" + self.detail


def approval_dedupe_key(approval_id: str, principal: str) -> str:
    """The send dedupe key for one approval's prompt to one approver.

    ``request_approval`` dedupes on it, so the runner can re-prompt on every pending step.
    The ``prompt`` part keeps it apart from the ``approval:<id>:resolved`` wake event.
    """
    return f"approval:{approval_id}:prompt:{principal}"


@dataclass(frozen=True)
class SelfTestResult:
    ok: bool
    detail: str


Submit = Callable[[Event], bool]


class ChannelError(Exception):
    """Base class for outbound channel failures. Messages never contain message bodies."""


class ChannelRefused(ChannelError):
    """The destination is not allowed: unknown principal, or no identity on this channel."""


class ChannelRateLimited(ChannelError):
    """The principal's outbound rate limit would need too long a wait. Nothing was sent."""


class ChannelSendError(ChannelError):
    """The adapter tried to send and failed. ``str(e)`` is an actionable, body-free reason."""


class Channel(Protocol):
    @property
    def name(self) -> str: ...

    async def start(self, submit: Submit) -> None:
        """Begin receiving. Inbound messages become Events with ``principal`` resolved."""
        ...

    async def stop(self) -> None: ...

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef: ...

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        """Send an approval request: ``prompt.text()``, deduped by
        ``approval_dedupe_key(prompt.approval_id, principal)`` (a repeat returns the first
        ref without sending), in ``prompt.thread`` when the adapter is threaded."""
        ...

    async def self_test(self) -> SelfTestResult:
        """Startup round-trip check (e.g. send to own handle, read it back from chat.db)."""
        ...


# --- identities and threads (PER-292) -------------------------------------------------------


# A 1:1 conversation's id on a threaded channel: Slack DMs are ``D…`` channels.
_ONE_TO_ONE: dict[str, tuple[str, ...]] = {"slack": ("D",)}


def shared_thread(thread: MessageRef | None) -> bool:
    """PER-320: whether ``thread`` is a conversation other people see (a thread in a team
    channel), where one approval prompt serves every approver. False for None, an unthreaded
    channel (iMessage) and a 1:1 conversation (a Slack DM: its prompt goes to each approver in
    their own conversation, as before)."""
    if thread is None or not thread.thread:
        return False
    return not thread.thread.startswith(_ONE_TO_ONE.get(thread.channel, ()))


def event_identity(event: Event) -> str | None:
    """The sender's channel identity of a message event: ``payload.identity``, set by the
    adapter, and only on the adapter's own channel (``slack:…`` on a ``slack`` event). An
    iMessage event stored before PER-292 has only ``payload.handle``; its identity is
    ``imessage:<handle>``. None otherwise."""
    identity: object = event.payload.get("identity")
    if isinstance(identity, str) and identity:
        return identity if identity.partition(":")[0] == event.source else None
    handle: object = event.payload.get("handle")
    if event.source == "imessage" and isinstance(handle, str) and handle:
        return f"imessage:{handle}"
    return None


def thread_of(task: Task | dict[str, Any] | None, *, to: str | None = None) -> MessageRef | None:
    """Where a task's replies go: its ``channel_ref`` thread as a ``MessageRef``, or None.

    None for a task with no ``channel_ref``, for an unthreaded channel (iMessage) and for
    every ``channel_ref`` written before PER-292 (no ``thread``): their sends pick the
    principal's first running identity, as before. ``to``: the principal the text goes to;
    a thread that is another principal's is None (a thread is only ever its asker's)."""
    ref = task if isinstance(task, dict) or task is None else task.channel_ref
    if ref is None or (to is not None and ref.get("principal") != to):
        return None
    channel, thread, msg = ref.get("channel"), ref.get("thread"), ref.get("ref")
    if not isinstance(channel, str) or not isinstance(thread, str) or not thread:
        return None
    return MessageRef(channel=channel, ref=msg if isinstance(msg, str) else thread, thread=thread)

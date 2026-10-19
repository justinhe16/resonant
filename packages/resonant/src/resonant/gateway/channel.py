"""Channel contract. Every channel adapter (iMessage, Slack) implements ``Channel``.

Inbound: an adapter resolves the sender's channel identity (``imessage:+1555…``,
``slack:U…``) to a principal through principals.yaml before calling ``submit``. Messages
from unknown identities are dropped and audited. An adapter never emits an event for:
- its own outgoing messages,
- messages from its own account or handle,
- self-test messages, which carry a nonce and go only to the self-test checker.

Content is untrusted input. Identity grants permissions, and message text never does.

Outbound: ``send`` takes a principal, never a raw address, and goes only to that
principal's identities from principals.yaml. Any other destination is refused and
audited. Sends are deduped by ``dedupe_key`` (the events idempotency key), rate-limited
by the adapter, and never interpolate text into scripts (e.g. osascript gets argv).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from resonant.gate.codes import approval_text, batch_text
from resonant_sdk import Effect, Event, Level


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
        return approval_text(
            self.code,
            self.summary,
            require_code=self.require_code,
            destructive=destructive,
            always=self.always and not self.require_code and not destructive,
        )


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
        ref without sending)."""
        ...

    async def self_test(self) -> SelfTestResult:
        """Startup round-trip check (e.g. send to own handle, read it back from chat.db)."""
        ...

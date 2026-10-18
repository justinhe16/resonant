"""Router: the Loop's router hook. Decides quickly what each incoming message is about.

``Router.route(event) -> NewTask | None``. For ``imessage``/``message`` events:

(a) Fast path (no model call): ``NewTask(kind="reply", runner="local",
    checkpoint={"fast_reply": text, "tool_used": ..., "intent": ..., "title": command})``.
    A side-effecting slash command (and every ``/<ext> <tool>``, ``/ext mute`` and ``/ext
    run``) instead gives ``NewTask(kind="command", runner="local", checkpoint={"direct_call":
    {"extension", "tool", "args"}, "intent": ..., "title": command})``: the local runner
    runs that one call with ``origin="explicit"``.
(b) Labeled (one model call): ``NewTask(kind="chat", runner="local", checkpoint={"intent",
    "confidence", "toolset": [{"extension", "name"}], "fallback", "toolset_prefix_hash",
    "title"})``.
(b') Brain loop (``brain_mode="loop"``, ``brain.mode``; docs/brain-plan.md D1): no labeler,
    ``NewTask(kind="chat", runner="brain", checkpoint={"title": "chat"})``. The fast path,
    approval replies and the relay run first, unchanged.
(b'') Media (BR-7, docs/brain-plan.md D10): an owner's message with attachments
    (``payload.attachments``: images, PDFs, voice notes, written by the reader) gives
    ``NewTask(kind="media", runner="media", checkpoint={"title": "media"})`` right after the
    slash commands, in both modes (``runners/media.py``: an image or PDF goes to
    ``ask_claude``, a voice note through whisper). A voice transcript comes back as a
    message event with ``payload.voice`` and goes only to the chat path (the labeler or
    the brain, whose model-chosen calls are gated): never a slash command or keyword (they
    run as explicit consent), an approval reply or a worker reply. Those stay typed.
(c) None for anything else (other sources/types, no principal, empty text and no
    attachments). The Loop consumes the event.

Order: slash commands first (so ``/kill`` is never read as a reply), then approval replies
(``router/approvals.py``: the parser, then ``gate.resolve_reply``), then the relay's direct
step (``router/relay.py``: ``R4 …``, an option pick, the answer to ``For R4 or R6?``), then
keywords and the labeler, then the relay's free-text step (the brain picks the question a
reply answers). A plain ``yes``/``no`` while a worker question is open goes to the relay
when no approval prompt is open, and gets "which one?" when one is (EM-5). Relay replies are
code-built ``fast_reply`` tasks titled ``worker reply``. A reply that resolves an approval
returns None (consumed; the woken runner sends the confirmation). A refused reply gets a
code-built ``fast_reply`` titled ``approval reply``. Text the parser doesn't recognize
routes normally.

``gate`` is the full ``Gate`` (``PolicyGate``, or ``DryRunGate`` before it is wired): the
fast path uses ``evaluate``/``consume``/``pending_for``, the inbox ``resolve_reply``.

``title`` is code-built (the command name with any subcommand word, or ``chat: <intent>``)
and is what ``/projects`` shows; it never contains message text.

Priority always comes from the principal (owner or member), never from the model. The
toolset is filtered by ``principals.may_request(principal, spec)`` before anything
reaches the model, so a member never sees global builtins (except a granted read,
``ToolSpec.grant``).

**Extension intents** (Phase 3). ``intents`` is built once at construction from the loaded
extensions (``intents.intent_set``). ``extension:<name>`` gets only that extension's chat
tools from the host (``host.tool_specs(name)``: never registry-wide, never job specs),
filtered by the same ``request`` grant. Tools reach the model by bare name, so a toolset
never holds two tools with one name, and an extension tool named like a builtin tool is
refused (left out, logged): ``chat_toolset``.

``classify(text, labeler)`` is the side-effect-free entry point for evals: it returns the
predicted intent and the path (``fast`` | ``llm``) without running tools or the kill switch.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Literal

from resonant.executor import HandlerKey, ToolHandler
from resonant.gate import Gate
from resonant.gate.replies import parse_reply
from resonant.gateway.imessage.attachments import from_payload
from resonant.loop.types import PRIORITY_MEMBER, PRIORITY_OWNER, NewTask
from resonant.models import ModelClient, SpanFactory
from resonant.observability.spans import span
from resonant.principals import Principals
from resonant.router.approvals import ApprovalInbox
from resonant.router.commands import commands_for
from resonant.router.fast_path import FastPath, match_fast_path
from resonant.router.intents import (
    BUILTIN_INTENTS,
    INTENT_TOOLS,
    ExtensionCatalog,
    IntentLabel,
    IntentSet,
    builtin_intent,
    extension_of,
    intent_set,
)
from resonant.router.labeler import Labeler
from resonant.router.prefixes import toolset_prefix_hash
from resonant.router.relay import Relay, clash_text, parse_addressed, plain_reply_clashes
from resonant.store import approvals as ap
from resonant.store.db import utcnow
from resonant.tools import ToolRegistry
from resonant_sdk import Event, ToolSpec

RUNNER = "local"
BRAIN_RUNNER = "brain"
BRAIN_TITLE = "chat"
KIND_REPLY = "reply"
KIND_CHAT = "chat"
KIND_COMMAND = "command"
MEDIA_RUNNER = "media"
KIND_MEDIA = "media"
MEDIA_TITLE = "media"
APPROVAL_TITLE = "approval reply"
RELAY_TITLE = "worker reply"


log = logging.getLogger(__name__)


def builtin_names(registry: ToolRegistry) -> frozenset[str]:
    return frozenset(s.name for s in registry.specs() if s.extension is None)


def chat_toolset(specs: Iterable[ToolSpec], builtins: frozenset[str]) -> list[ToolSpec]:
    """``specs`` as the model may see them, sorted by name. The model names a tool by its
    bare name, so names must be unique: an extension tool named like a builtin tool, or any
    name held by two different tools, is refused (left out, logged)."""
    unique = list({(s.extension, s.name): s for s in specs}.values())
    counts = Counter(s.name for s in unique)
    out: list[ToolSpec] = []
    for spec in unique:
        if (spec.extension is not None and spec.name in builtins) or counts[spec.name] > 1:
            log.warning(
                "tool name collides in the model's toolset; not offered",
                extra={"extension": spec.extension, "tool": spec.name},
            )
            continue
        out.append(spec)
    return sorted(out, key=lambda s: s.name)


def toolset_for(
    intent: IntentLabel,
    principal: str,
    *,
    registry: ToolRegistry,
    principals: Principals,
    extensions: ExtensionCatalog | None = None,
) -> list[ToolSpec]:
    """The tools ``principal`` may request for ``intent``, sorted by name.

    A builtin intent maps to builtins (``INTENT_TOOLS``); unregistered names are skipped.
    ``extension:<name>`` maps to that extension's chat tools from the host, never its job
    specs. Either way each tool needs ``principals.may_request(principal, spec)``.
    """
    specs: list[ToolSpec] = []
    ext = extension_of(intent)
    builtin = builtin_intent(intent)
    if builtin is not None:
        for name in INTENT_TOOLS[builtin]:
            spec = registry.get(None, name)
            if spec is not None:
                specs.append(spec)
    elif ext is not None and extensions is not None:
        specs = [
            s for s in extensions.tool_specs(ext) if s.extension == ext and not extensions.is_job(s)
        ]
    allowed = [s for s in specs if principals.may_request(principal, s)]
    return chat_toolset(allowed, builtin_names(registry))


def toolset_entries(toolset: Iterable[ToolSpec]) -> list[dict[str, str | None]]:
    """The checkpoint form of a toolset: ``[{"extension", "name"}]``."""
    return [{"extension": s.extension, "name": s.name} for s in toolset]


@dataclass(frozen=True)
class Classification:
    intent: IntentLabel
    path: Literal["fast", "llm"]
    confidence: float  # 1.0 on the fast path
    fallback: bool = False
    command: str | None = None  # the fast-path command, if any


async def classify(
    text: str, labeler: Labeler, *, spans: SpanFactory | None = None
) -> Classification:
    """Predict intent and path the way production does, from the owner's point of view.

    Uses the production fast-path matcher and labeler, but runs no tools and never touches
    the kill switch, so evals can call it on any text.
    """
    cmd = match_fast_path(text)
    if cmd is not None:
        return Classification(cmd.intent, "fast", 1.0, command=cmd.name)
    if parse_addressed(text) is not None:  # "R4 …": the relay's direct step, no model call
        return Classification("run_project", "fast", 1.0, command="relay")
    label = await labeler.label(text, spans=spans)
    return Classification(label.intent, "llm", label.confidence, fallback=label.fallback)


class Router:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        principals: Principals,
        registry: ToolRegistry,
        gate: Gate,
        handlers: Mapping[HandlerKey, ToolHandler],
        model: ModelClient,
        clock: Callable[[], datetime] = utcnow,
        timezone: str = "UTC",
        extensions: ExtensionCatalog | None = None,
        brain_mode: Literal["intents", "loop"] = "intents",
    ) -> None:
        self.conn = conn
        self.brain_mode = brain_mode
        self.principals = principals
        self.registry = registry
        self.extensions = extensions
        # Built once: the labeler prefix changes only with an install, remove or restart.
        self.intents: IntentSet = (
            intent_set(extensions) if extensions is not None else BUILTIN_INTENTS
        )
        self.fast_path = FastPath(
            conn,
            principals=principals,
            registry=registry,
            gate=gate,
            handlers=handlers,
            clock=clock,
            timezone=timezone,
            extensions=extensions,
            command_table=commands_for(extensions, registry),  # built once, like intents
        )
        self.inbox = ApprovalInbox(conn, principals=principals, gate=gate)
        # Worker questions and messages (EM-5); wiring sets relay.workers to the runner.
        self.relay = Relay(conn, principals=principals, model=model, clock=clock)
        self.labeler = Labeler(model, self.intents)

    def toolset_for(self, intent: IntentLabel, principal: str) -> list[ToolSpec]:
        return toolset_for(
            intent,
            principal,
            registry=self.registry,
            principals=self.principals,
            extensions=self.extensions,
        )

    async def __call__(self, event: Event) -> NewTask | None:
        return await self.route(event)

    async def route(self, event: Event) -> NewTask | None:
        if event.source != "imessage" or event.type != "message":
            return None
        principal = event.principal
        if principal is None or principal not in self.principals.principals:
            return None
        text: Any = event.payload.get("text")
        if not isinstance(text, str):
            return None
        is_owner = self.principals.principals[principal].role == "owner"
        media = is_owner and bool(from_payload(event.payload))
        if not text.strip() and not media:
            return None
        voice = event.payload.get("voice") is True
        priority = PRIORITY_OWNER if is_owner else PRIORITY_MEMBER
        spans: SpanFactory = partial(span, self.conn, trace_id=event.trace_id)
        channel_ref = _channel_ref(event, principal)

        def reply(text: str, tool_used: str | None, intent: IntentLabel, title: str) -> NewTask:
            return NewTask(
                kind=KIND_REPLY,
                runner=RUNNER,
                priority=priority,
                principal=principal,
                channel_ref=channel_ref,
                checkpoint={
                    "fast_reply": text,
                    "tool_used": tool_used,
                    "intent": intent,
                    "title": title,
                },
            )

        # A pending "For R4 or R6?" answers only the very next message, whatever it is.
        stash = self.relay.take_stash(principal)

        # Order: slash commands (so /kill is never read as a reply), approval replies, then
        # keywords and the labeler.
        # A voice transcript is chat only: never a slash command or keyword (``/restart``
        # misheard would run as explicit consent), an approval or a worker reply.
        cmd = None if voice else self.fast_path.match(text)
        if cmd is not None and cmd.slash:
            result = await self.fast_path.run(
                cmd, principal=principal, event_id=event.id, trace_id=event.trace_id, spans=spans
            )
            if result is not None and result.direct_call is not None:
                return NewTask(
                    kind=KIND_COMMAND,
                    runner=RUNNER,
                    priority=priority,
                    principal=principal,
                    channel_ref=channel_ref,
                    checkpoint={
                        "direct_call": result.direct_call,
                        "intent": result.intent,
                        "title": result.command,
                    },
                )
            if result is not None:
                return reply(result.reply_text, result.tool_used, result.intent, result.command)

        if media:
            with spans("router.media", principal=principal):
                pass
            return NewTask(
                kind=KIND_MEDIA,
                runner=MEDIA_RUNNER,
                priority=priority,
                principal=principal,
                channel_ref=channel_ref,
                checkpoint={"title": MEDIA_TITLE},
            )

        def relayed(text: str) -> NewTask:
            return reply(text, None, "run_project", RELAY_TITLE)

        # A plain yes/no while a worker question is open: the approval keeps it only when no
        # question competes; with both open, it binds neither (relay.py, "approval clash"),
        # unless the one question open can't take a yes/no (it offers other options).
        skip_inbox = False
        # A voice transcript never answers an approval (approving stays typed).
        plain = (
            None if voice else parse_reply(text, truncated=event.payload.get("truncated") is True)
        )
        opens = (
            self.relay.open_for(event, principal)
            if plain is not None and plain.code is None
            else []
        )
        if opens:
            sent = self.inbox.sent_pending(principal)
            check = any(
                (row := ap.get_approval(self.conn, p.approval_id)) is None
                or row.tool == ap.CHECK_TOOL
                for p in sent
            )
            if plain_reply_clashes(sent, opens, check=check):
                return relayed(clash_text(sent, opens))
            skip_inbox = not sent
        skip_inbox = skip_inbox or voice
        answered = None if skip_inbox else self.inbox.handle(event, text, principal, spans=spans)
        if answered is not None:
            if answered.reply is None:
                return None  # consumed: the woken runner sends the confirmation
            return reply(answered.reply, None, "job_control", APPROVAL_TITLE)

        direct = None if voice else self.relay.direct(event, text, principal, stash)
        if direct is not None:
            return relayed(direct.reply)

        if cmd is not None and not cmd.slash:
            result = await self.fast_path.run(
                cmd, principal=principal, event_id=event.id, trace_id=event.trace_id, spans=spans
            )
            if result is not None:
                return reply(result.reply_text, result.tool_used, result.intent, result.command)

        if self.brain_mode == "loop":
            # D1: no labeler (its prefix would evict the brain's from the model's one KV
            # slot). Any free text may still answer an open worker question.
            free = (
                None
                if voice
                else await self.relay.free(event, text, principal, "question", spans=spans)
            )
            if free is not None:
                return relayed(free.reply)
            with spans("router.brain", principal=principal):
                pass
            return NewTask(
                kind=KIND_CHAT,
                runner=BRAIN_RUNNER,
                priority=priority,
                principal=principal,
                channel_ref=channel_ref,
                checkpoint={"title": BRAIN_TITLE},
            )

        label = await self.labeler.label(text, spans=spans)
        free = (
            None
            if voice
            else await self.relay.free(event, text, principal, label.intent, spans=spans)
        )
        if free is not None:
            return relayed(free.reply)
        toolset = self.toolset_for(label.intent, principal)
        prefix_hash = toolset_prefix_hash(label.intent, toolset, self.intents)
        with spans(
            "router.toolset",
            intent=label.intent,
            principal=principal,
            toolset=[s.name for s in toolset],
            extension=extension_of(label.intent),
            toolset_prefix_hash=prefix_hash,
        ):
            pass
        return NewTask(
            kind=KIND_CHAT,
            runner=RUNNER,
            priority=priority,
            principal=principal,
            channel_ref=channel_ref,
            checkpoint={
                "intent": label.intent,
                "confidence": label.confidence,
                "toolset": toolset_entries(toolset),
                "fallback": label.fallback,
                "toolset_prefix_hash": prefix_hash,
                "title": f"chat: {label.intent}",
            },
        )


def _channel_ref(event: Event, principal: str) -> dict[str, Any]:
    p = event.payload
    return {
        "channel": event.source,
        "principal": principal,
        "handle": p.get("handle"),
        "chat_guid": p.get("chat_guid"),
        "guid": p.get("guid"),
    }

"""Router: the Loop's router hook. Decides quickly what each incoming message is about.

``Router.route(event) -> NewTask | None``. For ``imessage``/``message`` events:

(a) Fast path (no model call): ``NewTask(kind="reply", runner="local",
    checkpoint={"fast_reply": text, "tool_used": ..., "intent": ...})``.
(b) Labeled (one model call): ``NewTask(kind="chat", runner="local", checkpoint={"intent",
    "confidence", "toolset": [names], "fallback", "toolset_prefix_hash"})``.
(c) None for anything else (other sources/types, no principal, empty text). The Loop
    consumes the event.

Priority always comes from the principal (owner or member), never from the model. The
toolset is filtered by ``principals.can(principal, "request", extension)`` before anything
reaches the model, so a member never sees global builtins.

``classify(text, labeler)`` is the side-effect-free entry point for evals: it returns the
predicted intent and the path (``fast`` | ``llm``) without running tools or the kill switch.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal

from resonant.executor import HandlerKey, ToolHandler
from resonant.loop.types import PRIORITY_MEMBER, PRIORITY_OWNER, NewTask
from resonant.models import ModelClient, SpanFactory
from resonant.observability.spans import span
from resonant.principals import Principals
from resonant.router.fast_path import FastGate, FastPath, match_fast_path
from resonant.router.intents import INTENT_TOOLS, IntentLabel
from resonant.router.labeler import Labeler
from resonant.router.prefixes import toolset_prefix_hash
from resonant.tools import ToolRegistry
from resonant_sdk import Event, ToolSpec

RUNNER = "local"
KIND_REPLY = "reply"
KIND_CHAT = "chat"


def toolset_for(
    intent: IntentLabel, principal: str, *, registry: ToolRegistry, principals: Principals
) -> list[ToolSpec]:
    """The tools ``principal`` may request for ``intent``, sorted by name.

    Phase 1 maps intents to builtins only. Unregistered names are skipped.
    """
    specs: list[ToolSpec] = []
    for name in INTENT_TOOLS[intent]:
        spec = registry.get(None, name)
        if spec is not None and principals.can(principal, "request", spec.extension):
            specs.append(spec)
    return sorted(specs, key=lambda s: s.name)


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
    label = await labeler.label(text, spans=spans)
    return Classification(label.intent, "llm", label.confidence, fallback=label.fallback)


class Router:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        principals: Principals,
        registry: ToolRegistry,
        gate: FastGate,
        handlers: Mapping[HandlerKey, ToolHandler],
        model: ModelClient,
    ) -> None:
        self.conn = conn
        self.principals = principals
        self.registry = registry
        self.fast_path = FastPath(
            conn, principals=principals, registry=registry, gate=gate, handlers=handlers
        )
        self.labeler = Labeler(model)

    def toolset_for(self, intent: IntentLabel, principal: str) -> list[ToolSpec]:
        return toolset_for(intent, principal, registry=self.registry, principals=self.principals)

    async def __call__(self, event: Event) -> NewTask | None:
        return await self.route(event)

    async def route(self, event: Event) -> NewTask | None:
        if event.source != "imessage" or event.type != "message":
            return None
        principal = event.principal
        if principal is None or principal not in self.principals.principals:
            return None
        text: Any = event.payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return None

        is_owner = self.principals.principals[principal].role == "owner"
        priority = PRIORITY_OWNER if is_owner else PRIORITY_MEMBER
        spans: SpanFactory = partial(span, self.conn, trace_id=event.trace_id)
        channel_ref = _channel_ref(event, principal)

        cmd = match_fast_path(text)
        if cmd is not None:
            result = await self.fast_path.run(
                cmd, principal=principal, event_id=event.id, trace_id=event.trace_id, spans=spans
            )
            if result is not None:
                return NewTask(
                    kind=KIND_REPLY,
                    runner=RUNNER,
                    priority=priority,
                    principal=principal,
                    channel_ref=channel_ref,
                    checkpoint={
                        "fast_reply": result.reply_text,
                        "tool_used": result.tool_used,
                        "intent": result.intent,
                    },
                )

        label = await self.labeler.label(text, spans=spans)
        toolset = self.toolset_for(label.intent, principal)
        prefix_hash = toolset_prefix_hash(label.intent, toolset)
        with spans(
            "router.toolset",
            intent=label.intent,
            principal=principal,
            toolset=[s.name for s in toolset],
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
                "toolset": [s.name for s in toolset],
                "fallback": label.fallback,
                "toolset_prefix_hash": prefix_hash,
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

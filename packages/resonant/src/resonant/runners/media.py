"""MediaRunner (``runner="media"``, task kind ``media``): an owner's message with attachments
(BR-7, docs/brain-plan.md D10). The router sends it here right after
the slash commands; the reader only attaches files to the owner's messages.

- **Images and PDFs** (up to 4): once the files are on disk (Messages may still be
  downloading them: up to ``file_wait_s``, polled), the step writes a code-built
  ``direct_call`` of ``ask_claude{question: <the message text, or a default>, kind: "image",
  attachments: [paths]}`` into the checkpoint and runs it like a slash command's call
  (``LocalRunner._direct``: pinned, then ``ToolCore.resume`` gates it with
  ``origin="explicit"`` since the owner sent the file, and the executor runs it). The reply is
  ask_claude's own text (``Looking into it…``, or the refusal); the answer comes later from
  the ask runner. The ask runner checks each path against ``brain.ask_claude.attachment_roots``
  again and converts HEIC to JPEG.
- **A voice note** (when there is no image or PDF): with ``brain.whisper_path`` set, it is
  checked like an attachment, copied into ``<RESONANT_HOME>/media/<task>``, converted to a
  16 kHz WAV and transcribed (``runners/convert.py``); the transcript is submitted as a new
  ``imessage``/``message`` event (``payload.voice: true``, dedupe ``<the message's>:voice``)
  and routed like typed text, except that it never answers an approval. The task itself
  sends nothing. An empty transcript or a failure gets a short code-built reply. Without
  whisper the reply is ``VOICE_NEEDS_WHISPER``: Claude takes no audio (neither the API nor
  Claude Code's Read document audio input), so there is nothing to hand it.

Text and transcripts are PII: spans and logs carry kinds and counts, never text or paths.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from resonant.config import Settings
from resonant.gateway.channel import Channel, ChannelError, ChannelRefused, Submit
from resonant.gateway.imessage.attachments import Attachment, from_payload
from resonant.loop.types import Done, Fail, StepContext, StepOutcome, Task
from resonant.models import ModelClient, SpanFactory
from resonant.principals import Principals
from resonant.router.intents import ExtensionCatalog
from resonant.runners import convert
from resonant.runners.ask import AskScope, check_attachment, copy_in
from resonant.runners.local import Answer, LocalRunner, StepResult
from resonant.store.db import utcnow
from resonant.store.tasks import get_event
from resonant.toolcore import NOT_AVAILABLE_REPLY, RunnerGate, ToolExecutor
from resonant.tools import ToolRegistry
from resonant.tools.ask import ASK_CLAUDE, QUESTION_MAX
from resonant_sdk import Event

log = logging.getLogger(__name__)

NAME = "media"
TASK_KIND = "media"
FILE_WAIT_S = 30.0
FILE_POLL_S = 0.5
VOICE_SLACK_S = 60.0  # on top of whisper_timeout_s: the copy, the conversion, the submit

DEFAULT_QUESTION = {
    "image": "What's in this image? Answer briefly.",
    "pdf": "Summarise this PDF briefly.",
}
VOICE_NEEDS_WHISPER = (
    "I can't play voice notes: Claude takes no audio and no local whisper is set up "
    "(brain.whisper_path). Type it instead?"
)
VOICE_FAILED = "Couldn't transcribe that voice note. Type it instead?"
VOICE_EMPTY = "That voice note sounded empty to me."
NOT_READY = "That attachment hasn't finished downloading; send it again in a moment."
NOTHING = "I couldn't use that attachment (images, PDFs and voice notes only)."


def _on_disk(files: list[Attachment]) -> list[Attachment]:
    return [a for a in files if Path(a.path).is_file()]


class MediaRunner(LocalRunner):
    """``runner="media"``: the local runner's gate, executor and reply, plus the attachment
    handling above. ``submit`` is ``Loop.submit`` (a voice transcript re-enters routing)."""

    name = NAME

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        model: ModelClient,
        channel: Channel,
        gate: RunnerGate,
        executor: ToolExecutor,
        registry: ToolRegistry,
        settings: Settings,
        principals: Principals,
        submit: Submit,
        extensions: ExtensionCatalog | None = None,
        now: Callable[[], datetime] = utcnow,
        file_wait_s: float = FILE_WAIT_S,
        file_poll_s: float = FILE_POLL_S,
        afconvert: str = convert.AFCONVERT,
    ) -> None:
        super().__init__(
            conn,
            model=model,
            channel=channel,
            gate=gate,
            executor=executor,
            registry=registry,
            now=now,
            timezone=settings.timezone,
            extensions=extensions,
        )
        self.settings = settings
        self.principals = principals
        self.submit = submit
        self.file_wait_s = file_wait_s
        self.file_poll_s = file_poll_s
        self.afconvert = afconvert
        self.max_step_s = max(file_wait_s, 0.0) + settings.brain.whisper_timeout_s + VOICE_SLACK_S

    # --- the message ----------------------------------------------------------------------

    def _event(self, task: Task, events: list[Event]) -> Event | None:
        event = next((e for e in events if e.id == task.origin_event_id), None)
        if event is None and task.origin_event_id is not None:
            event = get_event(self.conn, task.origin_event_id)
        return event

    async def _ready(self, files: list[Attachment]) -> list[Attachment]:
        """The files that are on disk, waiting up to ``file_wait_s`` for all of them."""
        deadline = time.monotonic() + self.file_wait_s
        while True:
            ready = _on_disk(files)
            if len(ready) == len(files) or time.monotonic() >= deadline:
                return ready
            await asyncio.sleep(self.file_poll_s)

    # --- step: a voice note never makes a reply of its own --------------------------------

    async def step(self, task: Task, events: list[Event], ctx: StepContext) -> StepOutcome:
        cp = task.checkpoint
        if not any(k in cp for k in ("reply", "call", "direct_call")):
            event = self._event(task, events)
            files = from_payload(event.payload) if event is not None else []
            visual = [a for a in files if a.kind != "voice"]
            voice = next((a for a in files if a.kind == "voice"), None)
            if (
                event is not None
                and voice is not None
                and not visual
                and self.settings.brain.whisper_path is not None
            ):
                return await self._voice(task, event, voice)
        return await super().step(task, events, ctx)

    async def _voice(self, task: Task, event: Event, voice: Attachment) -> StepOutcome:
        principal = task.principal
        if principal is None:
            return Fail("media task has no principal")
        ready = await self._ready([voice])
        if not ready:
            return await self._say(task, principal, NOT_READY, "not_ready")
        work = self.settings.home / "media" / task.id
        started = time.monotonic()
        try:
            text = await asyncio.to_thread(self._transcribe, Path(voice.path), work)
        except (convert.ConvertError, OSError) as e:
            log.warning("voice note not transcribed: %s", type(e).__name__)
            return await self._say(task, principal, VOICE_FAILED, "failed")
        finally:
            await asyncio.to_thread(convert.remove_tree, work)
        if not text:
            return await self._say(task, principal, VOICE_EMPTY, "empty")
        limit = self.settings.channels.imessage.max_body_chars
        payload = {
            k: v
            for k, v in event.payload.items()
            if k not in ("attachments", "attachments_skipped")
        }
        payload.update(text=text[:limit], voice=True, truncated=len(text) > limit)
        accepted = self.submit(
            Event(
                source=event.source,
                type=event.type,
                principal=event.principal,
                payload=payload,
                dedupe_key=f"{event.dedupe_key or 'imessage:' + event.id}:voice",
                occurred_at=event.occurred_at,
                trace_id=event.trace_id,
            )
        )
        took = round(time.monotonic() - started, 1)
        log.info("voice note transcribed", extra={"task": task.id, "chars": len(text), "s": took})
        return Done({"path": "voice", "chars": len(text), "submitted": bool(accepted), "s": took})

    def _transcribe(self, src: Path, work: Path) -> str:
        """Check the file like an ask attachment, copy it into ``work`` and transcribe it."""
        attach = AskScope.from_settings(self.settings).attach()
        real, why = check_attachment(str(src), attach)
        if real is None:
            raise convert.ConvertError(why or "not allowed")
        if work.exists():
            convert.remove_tree(work)
        work.mkdir(parents=True, mode=0o700)
        copy = work / f"voice{real.suffix.lower()[:8]}"
        copy_in(real, copy, attach)
        return convert.transcribe(self.settings.brain, copy, work, afconvert=self.afconvert)

    async def _say(self, task: Task, principal: str, text: str, path: str) -> StepOutcome:
        try:
            await self.channel.send(principal, text, dedupe_key=f"reply:{task.id}")
        except ChannelRefused as e:
            return Fail(f"reply refused: {e}")
        except ChannelError as e:
            return Fail(f"reply send failed: {e}", retryable=True)
        return Done({"path": f"voice_{path}", "reply_len": len(text)})

    # --- images and PDFs: ask_claude as a code-built direct call --------------------------

    async def _answer(
        self,
        task: Task,
        principal: str,
        events: list[Event],
        ctx: StepContext,
        spans: SpanFactory,
    ) -> StepResult:
        cp = task.checkpoint
        if any(k in cp for k in ("call", "direct_call", "fast_reply")):
            return await super()._answer(task, principal, events, ctx, spans)
        event = self._event(task, events)
        files = from_payload(event.payload) if event is not None else []
        visual = [a for a in files if a.kind != "voice"]
        with spans("runner.media", files=len(files), visual=len(visual)) as s:
            if not visual:
                s.set(outcome="voice_no_whisper" if files else "nothing")
                return Answer(VOICE_NEEDS_WHISPER if files else NOTHING, "refused", [])
            ready = await self._ready(visual)
            s.set(ready=len(ready))
            if not ready:
                return Answer(NOT_READY, "refused", [])
            spec = self.registry.get(None, ASK_CLAUDE.name)
            if spec is None or not self.principals.may_request(principal, spec):
                return Answer(NOT_AVAILABLE_REPLY, "refused", [])
            text: Any = event.payload.get("text") if event is not None else None
            question = text.strip() if isinstance(text, str) else ""
            if not question:
                kinds = {a.kind for a in ready}
                question = DEFAULT_QUESTION["pdf" if kinds == {"pdf"} else "image"]
            args: dict[str, Any] = {
                "question": question[:QUESTION_MAX],
                "kind": "image",
                "attachments": [a.path for a in ready],
            }
        cp = {
            **cp,
            "direct_call": {"extension": None, "tool": ASK_CLAUDE.name, "args": args},
            "intent": "media",
        }
        if not ctx.save(checkpoint=cp):
            return Fail("task is no longer running")
        return await self._direct(task, principal, cp, ctx, spans)


__all__ = [
    "NAME",
    "TASK_KIND",
    "VOICE_NEEDS_WHISPER",
    "MediaRunner",
]

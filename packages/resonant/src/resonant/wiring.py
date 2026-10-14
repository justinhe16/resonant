"""iMessage composition: the channel, router, local and reminder runners, and health monitor.

``build_imessage_stack`` is the one place these are wired together. The daemon calls it when
``channels.imessage.enabled`` is true; ``resonant bench e2e`` and the end-to-end tests call
it with fakes. With iMessage disabled nothing here runs and the daemon is exactly Phase 0.

Lifecycle (the daemon drives it)::

    stack = build_imessage_stack(settings, conn, components, loop, daemon_healthy=...)
    await stack.start(loop)  # the chat.db reader feeds loop.submit
    ...                      # loop.run(), stack.monitor.run()
    await stack.stop()       # stop the reader and the monitor (no new events)
    ...                      # the loop drains in-flight steps; replies can still be sent
    await stack.aclose()     # close the model client's HTTP pool

Step limit. ``LocalRunner`` defaults to ``max_step_s=30`` but one step can make up to
``MAX_TOOL_ITERATIONS + 1`` model calls of ``model.timeout_s`` (60s) each, or run one tool
for up to its ``timeout_s`` (``update_resonant``: 360s). A step limit below either would
cancel a slow-but-healthy step: before the model client's own timeout could turn it into the
templated fallback reply, or mid-``/update`` into an ambiguous outcome. So the step limit is
the larger of the two plus slack (``local_step_limit``): the model client and the executor
time out first and the user always gets an answer; the loop's stuck check stays a backstop
for a wedged step.

Back up. After a restart the owner asked for (``restart_resonant``, ``update_resonant``),
``IMessageStack.start`` sends ``Back up (<commit>), down Ns`` once (``resonant.ops.back_up``).

Reminders. ``runner="reminder"`` sends due reminders through the iMessage channel, so it is
registered here, not in Phase 0. ``add_reminder`` only runs inside the loop (the router or a
runner step). The loop ticks again after every step and then sleeps only until the earliest
queued ``wake_at`` (``Loop.wait_s``), so a new reminder fires on time with no extra nudge.
With iMessage disabled no reminder runner exists: nothing can create a reminder, and one
left over from an earlier run fails as ``unknown runner`` once it is due (it couldn't be
delivered anyway). Reminders not yet due stay queued.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from resonant.components import Components
from resonant.config import ModelConfig, Settings
from resonant.gateway.imessage.channel import IMessageChannel
from resonant.gateway.imessage.sender import OsaRunner
from resonant.loop import Loop
from resonant.models import ModelClient
from resonant.monitor import HealthMonitor
from resonant.ops import back_up
from resonant.router import Router
from resonant.runners.local import MAX_TOOL_ITERATIONS, LocalRunner
from resonant.runners.reminder import ReminderRunner
from resonant_sdk import ToolSpec

log = logging.getLogger(__name__)

STEP_SLACK_S = 15.0  # sends, tool reads and bookkeeping on top of the model calls


def local_step_limit(model: ModelConfig, tools: Iterable[ToolSpec] = ()) -> float:
    """``max_step_s`` for the local runner: every model call it may make, or the longest
    tool timeout (so ``/update`` is never cancelled mid-run), whichever is larger, plus
    slack."""
    model_s = (MAX_TOOL_ITERATIONS + 1) * model.timeout_s
    tool_s = max((t.timeout_s for t in tools), default=0.0)
    return max(model_s, tool_s) + STEP_SLACK_S


@dataclass
class IMessageStack:
    channel: IMessageChannel
    router: Router
    runner: LocalRunner
    reminders: ReminderRunner
    monitor: HealthMonitor
    model: ModelClient
    owner: str | None = None  # gets "back up" after a requested restart
    _close_model: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    _started: bool = field(default=False, repr=False)

    @property
    def running(self) -> bool:
        """True between :meth:`start` and :meth:`stop` (the chat.db reader is running)."""
        return self._started

    async def start(self, loop: Loop) -> None:
        """Start the chat.db reader (its events go to ``loop.submit``), then say "back up"
        if this start follows a restart the owner asked for."""
        await self.channel.start(loop.submit)
        self._started = True
        log.info("imessage channel started")
        try:
            if await back_up(self.channel.conn, self.channel, self.owner):
                log.info("sent back-up message after a requested restart")
        except Exception:
            log.exception("back-up message failed")  # never blocks the start

    async def stop(self) -> None:
        """Stop the monitor and the reader. Safe to call twice or before ``start``."""
        self.monitor.stop()
        if self._started:
            self._started = False
            await self.channel.stop()
            log.info("imessage channel stopped")

    async def aclose(self) -> None:
        """Close the model client if this stack created it."""
        close, self._close_model = self._close_model, None
        if close is not None:
            await close()


def build_imessage_stack(
    settings: Settings,
    conn: sqlite3.Connection,
    components: Components,
    loop: Loop,
    *,
    daemon_healthy: Callable[[], bool],
    model: ModelClient | None = None,
    osa_runner: OsaRunner | None = None,
) -> IMessageStack:
    """Build the channel, router, runners and monitor, and hook them into ``loop``.

    Sets ``loop.router`` to ``Router.route`` and registers ``runner="local"`` and
    ``runner="reminder"`` in both ``components.runners`` and ``loop.runners``. ``model``
    defaults to an ``OllamaClient`` over ``settings.model`` (closed by
    :meth:`IMessageStack.aclose`); ``osa_runner`` defaults to the real osascript runner.
    Tests and the e2e bench inject fakes for both.
    """
    close_model: Callable[[], Awaitable[None]] | None = None
    if model is None:
        # Imported lazily: the openai SDK is slow to import and Phase 0 never needs it.
        from resonant.models import OllamaClient

        client = OllamaClient(settings.model)
        model, close_model = client, client.aclose

    channel = IMessageChannel(
        settings.channels.imessage, components.principals, conn, runner=osa_runner
    )
    router = Router(
        conn,
        principals=components.principals,
        registry=components.registry,
        gate=components.gate,
        handlers=components.handlers,
        model=model,
    )
    runner = components.register_local_runner(conn, model=model, channel=channel)
    runner.max_step_s = local_step_limit(settings.model, components.registry.specs())
    loop.runners[runner.name] = runner
    reminders = ReminderRunner(conn, channel=channel, timezone=settings.timezone)
    components.runners[reminders.name] = reminders
    loop.runners[reminders.name] = reminders
    loop.router = router.route

    owner = components.principals.owner if components.principals_loaded else None
    monitor = HealthMonitor(
        conn,
        model=model,
        channel=channel,
        owner=owner,
        daemon_healthy=daemon_healthy,
        db_path=settings.db_path,
        model_meta={"model": settings.model.name, "api": settings.model.api},
    )
    return IMessageStack(
        channel=channel,
        router=router,
        runner=runner,
        reminders=reminders,
        monitor=monitor,
        model=model,
        owner=owner,
        _close_model=close_model,
    )

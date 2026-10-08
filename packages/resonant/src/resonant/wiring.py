"""Phase 1 composition: the iMessage channel, router, local runner and health monitor.

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
``MAX_TOOL_ITERATIONS + 1`` model calls of ``model.timeout_s`` (60s) each. A step limit below
that would cancel a slow-but-healthy step before the model client's own timeout could turn
it into the templated fallback reply. So the step limit is derived from the model timeout
(``local_step_limit``): the model client times out first and the user always gets an
answer; the loop's stuck check stays a backstop for a wedged step.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from resonant.components import Components
from resonant.config import ModelConfig, Settings
from resonant.gateway.imessage.channel import IMessageChannel
from resonant.gateway.imessage.sender import OsaRunner
from resonant.loop import Loop
from resonant.models import ModelClient
from resonant.monitor import HealthMonitor
from resonant.router import Router
from resonant.runners.local import MAX_TOOL_ITERATIONS, LocalRunner

log = logging.getLogger(__name__)

STEP_SLACK_S = 15.0  # sends, tool reads and bookkeeping on top of the model calls


def local_step_limit(model: ModelConfig) -> float:
    """``max_step_s`` for the local runner: every model call it may make, plus slack."""
    return (MAX_TOOL_ITERATIONS + 1) * model.timeout_s + STEP_SLACK_S


@dataclass
class IMessageStack:
    channel: IMessageChannel
    router: Router
    runner: LocalRunner
    monitor: HealthMonitor
    model: ModelClient
    _close_model: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    _started: bool = field(default=False, repr=False)

    @property
    def running(self) -> bool:
        """True between :meth:`start` and :meth:`stop` (the chat.db reader is running)."""
        return self._started

    async def start(self, loop: Loop) -> None:
        """Start the chat.db reader; its events go to ``loop.submit``."""
        await self.channel.start(loop.submit)
        self._started = True
        log.info("imessage channel started")

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
    """Build the channel, router, local runner and monitor, and hook them into ``loop``.

    Sets ``loop.router`` to ``Router.route`` and registers ``runner="local"`` in both
    ``components.runners`` and ``loop.runners``. ``model`` defaults to an ``OllamaClient``
    over ``settings.model`` (closed by :meth:`IMessageStack.aclose`); ``osa_runner``
    defaults to the real osascript runner. Tests and the e2e bench inject fakes for both.
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
    runner.max_step_s = local_step_limit(settings.model)
    loop.runners[runner.name] = runner
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
        monitor=monitor,
        model=model,
        _close_model=close_model,
    )

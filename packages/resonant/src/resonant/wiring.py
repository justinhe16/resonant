"""iMessage composition: the channel, router, local, reminder and job runners, and health
monitor.

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
for up to its ``timeout_s`` (``update_resonant``: 360s; an extension tool at most 300s; job
specs never run in a chat step, so they don't count). A step limit below either would
cancel a slow-but-healthy step: before the model client's own timeout could turn it into the
templated fallback reply, or mid-``/update`` into an ambiguous outcome. So the step limit is
the larger of the two plus slack (``local_step_limit``): the model client and the executor
time out first and the user always gets an answer; the loop's stuck check stays a backstop
for a wedged step.

Back up. After a restart the owner asked for (``restart_resonant``, ``update_resonant``),
``IMessageStack.start`` sends ``Back up (<commit>), down Ns`` once (``resonant.ops.back_up``).
Then it sends any queued extension alerts to the owner (``extensions.host.send_alerts``:
``example changed on disk since you approved it; disabled. …``), once each.

Jobs. ``runner="job"`` (``runners.job.JobRunner``) runs scheduled jobs above L0: it prompts
approvers and sends job reports over iMessage, so it is registered here. Its recipients are
the gate's approvers for the extension (owner first). The scheduler itself is the daemon's
(it runs L0 jobs with iMessage off too); a gated job task left with no job runner fails as
``unknown runner``, like a reminder.

Notifications. The loop's router hook is :func:`route_event`: ``ext:*`` events (stored by
job runs) go to ``extensions.notify.Notifier.route``, everything else to ``Router.route``,
which is unchanged. The notifier sends to the same recipients as job reports, and its
``run`` (started by the daemon) sends the end-of-hour ``+N more updates``. With iMessage
off there is no router hook: extension events wait unrouted, as messages would.

Extension health. The monitor gets ``extensions.host.ExtensionHealth`` as its dynamic
``ext:<name>`` checks (health commands, job fail streaks, load refusals, tampering).

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
from resonant.extensions.host import ExtensionHealth, send_alerts
from resonant.extensions.notify import Notifier
from resonant.gate import PolicyGate
from resonant.gateway.imessage.channel import IMessageChannel
from resonant.gateway.imessage.sender import OsaRunner
from resonant.loop import Loop, NewTask
from resonant.models import ModelClient
from resonant.monitor import HealthMonitor
from resonant.ops import back_up
from resonant.router import Router
from resonant.runners.job import JobRunner
from resonant.runners.local import MAX_TOOL_ITERATIONS, LocalRunner
from resonant.runners.reminder import ReminderRunner
from resonant_sdk import Event, ToolSpec
from resonant_sdk.manifest import TOOL_TIMEOUT_MAX_S

log = logging.getLogger(__name__)

STEP_SLACK_S = 15.0  # sends, tool reads and bookkeeping on top of the model calls


def local_step_limit(model: ModelConfig, tools: Iterable[ToolSpec] = ()) -> float:
    """``max_step_s`` for the local runner: every model call it may make, or the longest
    tool timeout (so ``/update`` is never cancelled mid-run), whichever is larger, plus
    slack. Pass chat tools only (``Components.chat_specs()``: no job specs); an extension
    tool counts at most ``TOOL_TIMEOUT_MAX_S`` (300s)."""
    model_s = (MAX_TOOL_ITERATIONS + 1) * model.timeout_s
    tool_s = max(
        (
            t.timeout_s if t.extension is None else min(t.timeout_s, TOOL_TIMEOUT_MAX_S)
            for t in tools
        ),
        default=0.0,
    )
    return max(model_s, tool_s) + STEP_SLACK_S


RouteFn = Callable[[Event], Awaitable[NewTask | None]]


def route_event(router: RouteFn, notifier: Notifier) -> RouteFn:
    """The loop's router hook: ``ext:*`` events to the notifier, the rest to the router."""

    async def route(event: Event) -> NewTask | None:
        if event.source.startswith("ext:"):
            return await notifier.route(event)
        return await router(event)

    return route


@dataclass
class IMessageStack:
    channel: IMessageChannel
    router: Router
    runner: LocalRunner
    reminders: ReminderRunner
    monitor: HealthMonitor
    model: ModelClient
    owner: str | None = None  # gets "back up" after a requested restart
    jobs: JobRunner | None = None
    notifier: Notifier | None = None
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
        try:
            if sent := await send_alerts(self.channel.conn, self.channel, self.owner):
                log.info("sent %d extension alert(s)", sent)
        except Exception:
            log.exception("extension alerts failed")  # never blocks the start

    async def stop(self) -> None:
        """Stop the monitor, the notifier's flush and the reader. Safe to call twice or
        before ``start``."""
        self.monitor.stop()
        if self.notifier is not None:
            self.notifier.stop()
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

    Sets ``loop.router`` to :func:`route_event` (the notifier, then ``Router.route``) and
    registers ``runner="local"``,
    ``runner="reminder"`` and ``runner="job"`` in both ``components.runners`` and
    ``loop.runners``. ``model``
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
        timezone=settings.timezone,
        extensions=components.extensions,
    )
    runner = components.register_local_runner(
        conn,
        model=model,
        channel=channel,
        timezone=settings.timezone,
        model_result_max_chars=settings.extensions.model_result_max_chars,
    )
    runner.max_step_s = local_step_limit(settings.model, components.chat_specs())
    loop.runners[runner.name] = runner
    reminders = ReminderRunner(conn, channel=channel, timezone=settings.timezone)
    components.runners[reminders.name] = reminders
    loop.runners[reminders.name] = reminders
    gate = components.gate
    fallback = (components.principals.owner,)
    recipients: Callable[[str], tuple[str, ...]] = (
        gate.approvers if isinstance(gate, PolicyGate) else lambda _ext: fallback
    )
    host = components.extensions
    notifier = Notifier(
        conn,
        channel=channel,
        recipients=recipients,
        manifest_of=(lambda _ext: None) if host is None else host.manifest,
        config=settings.extensions.notify,
    )
    loop.router = route_event(router.route, notifier)
    jobs = JobRunner(
        conn,
        gate=gate,
        executor=components.executor,
        registry=components.registry,
        channel=channel,
        recipients=recipients,
        timezone=settings.timezone,
    )
    components.runners[jobs.name] = jobs
    loop.runners[jobs.name] = jobs

    owner = components.principals.owner if components.principals_loaded else None
    monitor = HealthMonitor(
        conn,
        model=model,
        channel=channel,
        owner=owner,
        daemon_healthy=daemon_healthy,
        db_path=settings.db_path,
        model_meta={"model": settings.model.name, "api": settings.model.api},
        ext_checks=(
            None
            if host is None
            else ExtensionHealth.from_settings(settings, host, components.executor.secrets)
        ),
    )
    return IMessageStack(
        channel=channel,
        router=router,
        runner=runner,
        reminders=reminders,
        monitor=monitor,
        model=model,
        owner=owner,
        jobs=jobs,
        notifier=notifier,
        _close_model=close_model,
    )

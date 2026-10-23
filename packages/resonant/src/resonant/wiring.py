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

Step limit. ``LocalRunner`` defaults to ``max_step_s=30`` but one step can run one tool for
up to its ``timeout_s`` (``update_resonant``: 360s; an extension tool at most 300s; job specs
never run in a chat step, so they don't count). A step limit below that would cancel a
slow-but-healthy step mid-``/update`` into an ambiguous outcome. So the step limit is the
longest tool timeout plus slack (``local_step_limit``; PER-256: the local runner makes no
model call, the brain's one triage call adds ``model.timeout_s``, ``brain_step_limit``): the
executor times out first and the user always gets an answer; the loop's stuck check stays a
backstop for a wedged step.

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
job runs) go to ``extensions.notify.Notifier.route`` (``ext.<name>.item`` reports go to
``extensions.pulse.Pulse.route``), everything else to ``Router.route``,
which is unchanged. The notifier sends to the same recipients as job reports, and its
``run`` (started by the daemon) sends the end-of-hour ``+N more updates``. With iMessage
off there is no router hook: extension events wait unrouted, as messages would.

Voice (PER-273). With ``brain.voice.enabled`` (the default) one ``runners.voice.ClaudeVoice``
is built before the Claude runner and set as ``components.voice``: the runner uses it for a
run's outcome text, and the core pulse and briefing jobs (built later from the same
components) for theirs. Off: those texts stay code-built.

Extension health. The monitor gets ``extensions.host.ExtensionHealth`` as its dynamic
``ext:<name>`` checks (health commands, job fail streaks, load refusals, tampering).

Brain. ``runner="brain"`` (``runners.brain.BrainRunner``, BR-1, PER-256) is the router's
chat target: the local model only routes (small talk and code-built news are answered there),
everything else is handed to Claude. It gets the daemon's ``started_at``, ``loop.last_tick``
and the slot pool for the context, and the owner's facts store. Its step limit is
``brain_step_limit``.

Ask Claude. ``runner="ask"`` (``runners.ask.AskRunner``, BR-5) runs one read-only ``claude -p``
per ``ask_claude`` call and texts the answer, so it is registered here; the tool reaches it as
``components.runners["ask"]``. With iMessage off the tool answers that it isn't running.

Media and watches (BR-7). ``runner="media"`` (``runners.media.MediaRunner``) answers an
owner's message with attachments: an image or PDF goes to ``ask_claude`` as a direct call, a
voice note through whisper and back into ``loop.submit`` as text. ``runner="watch"``
(``runners.watch.WatchRunner``) runs the checks ``core/watches`` queues; the ask runner hands
it the answers of ask watches (``AskRunner.on_watch``). Both send through this channel.

Claude workers. ``runner="claude"`` (``runners.claude.ClaudeRunner``) runs a Claude session
under the thin guard in a worktree of the owner's checkout (or a scratch dir) and reports the
PR or the answer; it replies over the channel, so it is registered here. The daemon calls its
``start()`` before the loop resumes anything (it ends the processes a crash left and hooks the
kill switch) and ``aclose()`` at the end. The tools and slash commands reach it as
``IMessageStack.claude`` or ``components.runners["claude"]``.

Trust checks. ``IMessageStack.trust`` (``trust.checks.TrustChecks``) answers a worker's
``resonant check``: it asks the owner over this channel when a check is net new or a rule
says to ask. EM-3's per-run socket calls its ``request_check``.

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
from datetime import datetime
from functools import partial
from typing import Any

from resonant.backup import health_check as backup_health
from resonant.brain.facts import FactStore, facts_dir
from resonant.components import Components
from resonant.config import ModelConfig, Settings
from resonant.executor import ToolFailed
from resonant.extensions.briefing import JOB_ID as BRIEFING_JOB
from resonant.extensions.host import ExtensionHealth, ExtensionHost, send_alerts
from resonant.extensions.notify import Notifier
from resonant.extensions.pulse import EVENT_TYPE, Pulse
from resonant.extensions.pulse_job import JOB_ID as PULSE_JOB
from resonant.extensions.scheduler import CoreDeps
from resonant.gate import PolicyGate
from resonant.gateway.imessage.channel import IMessageChannel
from resonant.gateway.imessage.sender import OsaRunner
from resonant.loop import Loop, NewTask
from resonant.models import ModelClient
from resonant.monitor import HealthMonitor
from resonant.ops import SubprocessRunner, back_up
from resonant.router import Router
from resonant.router.fast_path import core_briefing, core_pulse
from resonant.runners.ask import AskRunner, AskScope
from resonant.runners.ask_tools import ToolAccess
from resonant.runners.brain import BrainRunner, taints
from resonant.runners.claude import ClaudeRunner
from resonant.runners.job import JobRunner
from resonant.runners.local import LocalRunner
from resonant.runners.media import MediaRunner
from resonant.runners.reminder import ReminderRunner
from resonant.runners.voice import ClaudeVoice
from resonant.runners.watch import WatchRunner
from resonant.runners.worker_desk import SelfOps
from resonant.secrets import SecretStore
from resonant.store.db import utcnow
from resonant.tools.ops import Ops
from resonant.trust.checks import TrustChecks
from resonant_sdk import Event, ToolSpec
from resonant_sdk.manifest import TOOL_TIMEOUT_MAX_S

log = logging.getLogger(__name__)

STEP_SLACK_S = 15.0  # sends, tool reads and bookkeeping on top of the model calls


def local_step_limit(tools: Iterable[ToolSpec] = ()) -> float:
    """``max_step_s`` for the local runner (PER-256: it makes no model call): the longest
    tool timeout (so ``/update`` is never cancelled mid-run) plus slack. Pass chat tools only
    (``Components.chat_specs()``: no job specs); an extension tool counts at most
    ``TOOL_TIMEOUT_MAX_S`` (300s)."""
    tool_s = max(
        (
            t.timeout_s if t.extension is None else min(t.timeout_s, TOOL_TIMEOUT_MAX_S)
            for t in tools
        ),
        default=0.0,
    )
    return tool_s + STEP_SLACK_S


def brain_step_limit(model: ModelConfig, tools: Iterable[ToolSpec] = ()) -> float:
    """``max_step_s`` for the brain runner: its one triage call (``model.timeout_s`` at most)
    on top of ``local_step_limit`` (a resumed approval runs a tool)."""
    return local_step_limit(tools) + model.timeout_s


RouteFn = Callable[[Event], Awaitable[NewTask | None]]


def route_event(router: RouteFn, notifier: Notifier, pulse: Pulse | None = None) -> RouteFn:
    """The loop's router hook: ``ext.<name>.item`` events to the pulse, other ``ext:*``
    events to the notifier, the rest to the router."""

    async def route(event: Event) -> NewTask | None:
        if event.source.startswith("ext:"):
            if pulse is not None and event.type == f"ext.{event.source[4:]}.{EVENT_TYPE}":
                return await pulse.route(event)
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
    claude: ClaudeRunner | None = None
    notifier: Notifier | None = None
    extensions: ExtensionHost | None = None  # gets ``alert`` once the channel is up
    pulse: Pulse | None = None
    trust: TrustChecks | None = None  # workers' ``resonant check`` (EM-3 calls request_check)
    brain: BrainRunner | None = None  # runner="brain" (BR-1)
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
        if self.extensions is not None:  # MCP pin drift found from now on is sent right away
            self.extensions.alert = self._send_alerts
        try:
            if sent := await send_alerts(self.channel.conn, self.channel, self.owner):
                log.info("sent %d extension alert(s)", sent)
        except Exception:
            log.exception("extension alerts failed")  # never blocks the start

    async def _send_alerts(self) -> None:
        try:
            await send_alerts(self.channel.conn, self.channel, self.owner)
        except Exception:
            log.exception("extension alerts failed")

    async def stop(self) -> None:
        """Stop the monitor, the notifier's flush and the reader. Safe to call twice or
        before ``start``."""
        self.monitor.stop()
        if self.notifier is not None:
            self.notifier.stop()
        if self.extensions is not None:
            self.extensions.alert = None
        if self._started:
            self._started = False
            await self.channel.stop()
            log.info("imessage channel stopped")

    async def aclose(self) -> None:
        """Stop the brain's background folds, then close the model client if this stack
        created it."""
        if self.brain is not None:
            await self.brain.aclose()  # background history folds use the model
        close, self._close_model = self._close_model, None
        if close is not None:
            await close()


def tool_access(components: Components, brain: BrainRunner, settings: Settings) -> ToolAccess:
    """Resonant's tools for a Claude brain session (BR-10): what the brain offers the
    principal, through the same gate and executor as every other tool call."""

    def tools_for(principal: str) -> list[ToolSpec]:
        return brain.tools_for(principal)

    return ToolAccess(
        core=brain.core,  # the brain's ToolCore: the components' registry, gate and executor
        tools_for=tools_for,
        taints=taints,
        extensions=components.extensions,
        timezone=settings.timezone,
        shell=components.shell,
        askable=owner_askable(components),
    )


def owner_askable(components: Components) -> Callable[[ToolSpec], bool] | None:
    """PER-272: which tools the Claude bridge offers the ``owner_asked`` mark on (the gate's
    ``OwnerAsks``), or None when the gate has none."""
    gate = components.gate
    asks = gate.owner_asks if isinstance(gate, PolicyGate) else None
    return asks.askable if asks is not None else None


def _ask_runner(components: Components) -> AskRunner | None:
    """``runner="ask"`` as registered now (a test may swap it after the build)."""
    found = components.runners.get("ask")
    return found if isinstance(found, AskRunner) else None


async def start_asks(components: Components | None) -> None:
    """Daemon start (PER-262), before the loop resumes anything: the ask runner's startup
    cleanup (stale ``rsn-mcp-*`` bridge dirs, a Claude brain session a killed daemon left,
    old brain transcripts). A failure is logged, never raised."""
    asks = _ask_runner(components) if components is not None else None
    if asks is None:
        return
    try:
        await asks.start()
    except Exception:
        log.exception("ask: startup cleanup failed")


async def close_asks(components: Components | None) -> None:
    """Shutdown (BR-11): end the Claude brain's session, RC-1's way, keeping its id for a
    ``--resume`` after the restart. A failure is logged, never raised."""
    asks = _ask_runner(components) if components is not None else None
    if asks is None:
        return
    try:
        await asks.aclose()
    except Exception:
        log.exception("claude brain: shutdown failed")


class _DryRunOps:
    """A worker's restart or update under ``dry_run``: refused the way the gate would say."""

    async def restart_resonant(self) -> dict[str, Any]:
        raise ToolFailed("dry run: would restart Resonant")

    async def update_resonant(self) -> dict[str, Any]:
        raise ToolFailed("dry run: would update Resonant")


def _worker_ops(
    conn: sqlite3.Connection, settings: Settings, components: Components, owner: str | None
) -> SelfOps | None:
    """What a worker's ``resonant restart|update`` runs (GD-1): the ops tools' ``Ops``, which
    tells ``owner`` "Back up" after the restart. None without an owner to tell."""
    if owner is None:
        return None
    if settings.dry_run:
        return _DryRunOps()
    runner = components.ops_runner if components.ops_runner is not None else SubprocessRunner()
    return Ops(conn, settings, runner, owner=owner, clock=utcnow)


def build_imessage_stack(
    settings: Settings,
    conn: sqlite3.Connection,
    components: Components,
    loop: Loop,
    *,
    daemon_healthy: Callable[[], bool],
    model: ModelClient | None = None,
    osa_runner: OsaRunner | None = None,
    started_at: datetime | None = None,
) -> IMessageStack:
    """Build the channel, router, runners and monitor, and hook them into ``loop``.

    Sets ``loop.router`` to :func:`route_event` (the notifier, then ``Router.route``) and
    registers ``runner="local"``, ``runner="reminder"``, ``runner="job"`` and
    ``runner="claude"`` in both ``components.runners`` and ``loop.runners``. ``model``
    defaults to an ``OllamaClient`` over ``settings.model`` (closed by
    :meth:`IMessageStack.aclose`); ``osa_runner`` defaults to the real osascript runner.
    Tests and the e2e bench inject fakes for both. ``started_at`` (the daemon's start, for
    the brain's briefing) defaults to now.
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
        taint_hops=settings.brain.taint_hops,
    )
    runner.max_step_s = local_step_limit(components.chat_specs())
    loop.runners[runner.name] = runner
    brain = BrainRunner(
        conn,
        model=model,
        channel=channel,
        gate=components.gate,
        executor=components.executor,
        registry=components.registry,
        settings=settings,
        principals=components.principals,
        facts=FactStore(facts_dir(settings.home)),
        started_at=started_at,
        last_tick=lambda: loop.last_tick,
        slots=loop.slots,
        max_step_s=brain_step_limit(settings.model, components.chat_specs()),
        extensions=components.extensions,
        model_result_max_chars=settings.extensions.model_result_max_chars,
    )
    components.runners[brain.name] = brain
    loop.runners[brain.name] = brain
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
    pulse = Pulse(
        conn,
        channel=channel,
        recipients=recipients,
        manifest_of=(lambda _ext: None) if host is None else host.manifest,
        config=settings.extensions.notify,
    )
    loop.router = route_event(router.route, notifier, pulse)
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
    # workers' `resonant check` (EM-3 calls request_check; EM-4)
    # A check's approval lives as long as the worker waits for it (runner.ask_timeout), not
    # the gate's 30-minute TTL, so an unanswered check isn't denied early (EM-5).
    trust = TrustChecks(
        conn,
        channel,
        components.principals,
        approval_ttl_s=settings.runner.ask_timeout_minutes * 60,
        cost_ask_over_usd=settings.trust.cost_ask_over_usd,
    )
    owner_name = components.principals.owner if components.principals_loaded else None
    # PER-273: Claude writes the prose of the texts Resonant sends on its own. The pulse and
    # the briefing reach it through ``components.voice`` (the scheduler is built later, from
    # the same components); the runner gets it directly for run outcomes.
    voice = (
        ClaudeVoice(
            conn,
            cfg=settings.brain.voice,
            secrets=components.executor.secrets or SecretStore(),
            cwd=settings.home / "voice",
            guard=settings.runner.guard,
            timezone=settings.timezone,
        )
        if settings.brain.voice.enabled
        else None
    )
    components.voice = voice
    claude = ClaudeRunner(
        conn,
        runner=settings.runner,
        channel=channel,
        secrets=components.executor.secrets or SecretStore(),
        timezone=settings.timezone,
        max_runs=max(1, settings.claude.max_concurrent - settings.claude.reserved_oncall_slots),
        create_task=loop.create_task,
        cancel_task=loop.cancel,
        # answers and owner messages wake a run's task through the loop (EM-3)
        submit=loop.submit,
        trust=trust,
        # GD-1: a worker's `resonant restart|update` runs the same ops as /restart and
        # /update, and the daemon's own checkout is the repo "resonant"
        self_ops=_worker_ops(conn, settings, components, owner_name),
        owner=owner_name,
        self_repo=settings.ops.repo_path,
        voice=voice,
    )
    components.runners[claude.name] = claude
    loop.runners[claude.name] = claude

    # ask_claude (BR-5): a read-only Claude session per question, apart from the worker slots
    asks = AskRunner(
        conn,
        cfg=settings.brain.ask_claude,
        scope=AskScope.from_settings(settings),  # the read tools' roots and deny list (BR-2)
        ask_root=settings.home / "ask",
        channel=channel,
        secrets=components.executor.secrets or SecretStore(),
        owner=components.principals.owner,
        guard=settings.runner.guard,
        timezone=settings.timezone,
        create_task=loop.create_task,
        tools=tool_access(components, brain, settings),  # BR-10: the brain's asks
        session=settings.brain.claude_session,  # BR-11: the Claude brain
        taint_hops=settings.brain.taint_hops,
    )
    components.runners[asks.name] = asks
    loop.runners[asks.name] = asks
    # BR-7: attachments (images and PDFs to ask_claude, voice notes through whisper)
    media = MediaRunner(
        conn,
        model=model,
        channel=channel,
        gate=components.gate,
        executor=components.executor,
        registry=components.registry,
        settings=settings,
        principals=components.principals,
        submit=loop.submit,
        extensions=components.extensions,
    )
    components.runners[media.name] = media
    loop.runners[media.name] = media
    # BR-7: watch checks (core/watches queues them); ask watches answer through on_watch
    watcher = WatchRunner(
        conn,
        gate=components.gate,
        executor=components.executor,
        registry=components.registry,
        principals=components.principals,
        channel=channel,
        settings=settings,
        asks=lambda: _ask_runner(components),
    )
    asks.on_watch = watcher.ask_answered
    components.runners[watcher.name] = watcher
    loop.runners[watcher.name] = watcher
    router.fast_path.runs_killed = lambda: claude.last_killed
    router.relay.workers = lambda: claude  # owner replies to worker questions (EM-5)

    owner = components.principals.owner if components.principals_loaded else None
    # /ext run core/briefing (P3-14): the briefing on demand, through this channel.
    router.fast_path.core_jobs[BRIEFING_JOB] = core_briefing(
        CoreDeps(settings, conn, components, channel, owner)
    )
    router.fast_path.repo_path = settings.ops.repo_path
    router.fast_path.core_jobs[PULSE_JOB] = core_pulse(
        CoreDeps(settings, conn, components, channel, owner)
    )
    monitor = HealthMonitor(
        conn,
        model=model,
        channel=channel,
        owner=owner,
        daemon_healthy=daemon_healthy,
        db_path=settings.db_path,
        model_meta={"model": settings.model.name, "api": settings.model.api},
        backup_check=partial(backup_health, conn, settings),
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
        claude=claude,
        notifier=notifier,
        pulse=pulse,
        trust=trust,
        brain=brain,
        extensions=components.extensions,
        _close_model=close_model,
    )

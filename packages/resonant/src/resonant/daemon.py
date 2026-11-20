"""Daemon entrypoint: the loop and the local API in one asyncio process.

launchd (KeepAlive) supervises the process. SIGTERM/SIGINT trigger a graceful stop:
in-flight steps get a short grace period, and anything unfinished resumes from its
checkpoint on the next start.

With any ``channels.<x>.enabled`` (PER-292; or adapters a test injects with ``channels=``)
the daemon also runs the channel stack (``resonant.wiring.build_stack``): the channel hub's
adapters feed ``loop.submit``, the router is the loop's router hook, ``runner="local"``
answers, ``runner="reminder"`` sends due reminders, and the
health monitor runs; ``runner="job"`` runs scheduled jobs above L0; extension events are
notified (``extensions.notify``) and the notifier's end-of-hour flush runs. On shutdown the readers
and monitor stop first (no new events), then the loop drains in-flight steps (which may
still send their replies), then the model client closes. With every channel disabled, the
daemon is exactly Phase 0 apart from the gate and the scheduler. If no adapter starts, the
daemon exits 1 (iMessage-only, as before); one failing next to others is logged and left to
the monitor.

The status relay (``dashboard.pusher``, docs/dashboard-plan.md D4). With ``relay.enabled`` an aux
task pushes the remote tier of the dashboard snapshot to Supabase every ``relay.every_s``; it is
cancelled with the other aux tasks on shutdown. Disabled, the daemon clears its health row.

Claude workers (docs/employee-plan.md). With the channel stack on, ``runner="claude"`` is
registered by the wiring. Before the loop starts, the daemon calls ``ClaudeRunner.start()``:
it ends every session process a crash left behind (matched by pid and start time), wipes
leftover token dirs and hooks the kill switch, and only then does ``Loop.run`` resume tasks.
The scheduler also gets the ``core/run-cleanup`` job (``runners.claude.cleanup_job``).

The gate is always the Phase 2 ``PolicyGate``; the startup line logs
``gate=policy dry_run=<bool>``.

The scheduler (``extensions.scheduler.Scheduler``) is built after the components and the
channel stack and set as ``loop.scheduler``, channels on or off: L0 and core jobs run
inline from the loop's tick. It registers the kill switch hook at start; on shutdown,
after the reader stops, its inline runs are cancelled (decided again on the next start).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Generator
from functools import partial
from typing import TYPE_CHECKING

import uvicorn

from resonant.api import DaemonState, create_app, local_snapshot, runner_missing
from resonant.components import build_components
from resonant.config import Settings
from resonant.dashboard.pusher import SnapshotPusher, clear_relay_health
from resonant.extensions.scheduler import CORE_JOBS, Scheduler
from resonant.health import Heartbeat, Watchdog, deadman_task, heartbeat_task
from resonant.instance import is_default
from resonant.loop import Loop, SlotPool
from resonant.loop.echo import EchoRunner
from resonant.observability import configure_logging
from resonant.runners.brain import BrainRunner
from resonant.runners.claude import cleanup_job
from resonant.secrets import SecretStore
from resonant.store.db import open_db, utcnow

if TYPE_CHECKING:
    from collections.abc import Mapping

    from resonant.extensions.process import ExtRunner
    from resonant.gateway.channel import Channel
    from resonant.gateway.imessage.sender import OsaRunner
    from resonant.models import ModelClient
    from resonant.ops import OpsRunner
    from resonant.wiring import Stack

log = logging.getLogger(__name__)

# Seconds after start that every extension panel first runs (PER-280), so the dashboard has
# data before the first cron time. Tests set it to None (the conftest's wall-clock switch).
PANEL_FIRST_RUN_S: float | None = 30.0


class _Server(uvicorn.Server):
    """uvicorn without its own signal handling: the daemon owns shutdown."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


class Daemon:
    def __init__(
        self,
        settings: Settings,
        *,
        model: ModelClient | None = None,
        osa_runner: OsaRunner | None = None,
        ops_runner: OpsRunner | None = None,
        secrets: SecretStore | None = None,
        ext_runner: ExtRunner | None = None,
        channels: Mapping[str, Channel] | None = None,
    ) -> None:
        """``model`` and ``osa_runner`` replace Ollama and osascript (tests, ``bench e2e``).

        They are used only when the channel stack runs. ``ops_runner`` replaces the ops
        tools' subprocesses (git, uv, launchctl); ``secrets`` and ``ext_runner`` replace the
        Keychain and the extension process runner. ``channels``: extra adapters for the hub
        (tests: ``e2e.FakeChannel("slack")``); any given turns the channel stack on.
        """
        self.settings = settings
        self.conn = open_db(settings.db_path)
        self.loop = Loop(
            self.conn,
            runners={"echo": EchoRunner()},
            config=settings.loop,
            slots=SlotPool(settings.claude.max_concurrent, settings.claude.reserved_oncall_slots),
        )
        self.state = DaemonState(settings, self.conn, self.loop, started_at=utcnow())
        self.state.components = build_components(
            settings,
            self.conn,
            self.state,
            ops_runner=ops_runner,
            secrets=secrets,
            ext_runner=ext_runner,
        )
        self.stack: Stack | None = None
        if settings.channels.enabled() or channels:
            from resonant.wiring import build_stack

            self.stack = build_stack(
                settings,
                self.conn,
                self.state.components,
                self.loop,
                daemon_healthy=self.healthy,
                model=model,
                osa_runner=osa_runner,
                started_at=self.state.started_at,
                channels=channels,
            )
            self.state.monitor = self.stack.monitor
            brain = self.state.components.runners.get("brain")
            if isinstance(brain, BrainRunner):
                # The briefing's "Workers aren't set up" uses the dashboard's runner check
                # (one cached Keychain-by-name and login check for both, api.RUNNER_CHECK_TTL_S).
                brain.runner_gaps = partial(runner_missing, self.state)
        self.scheduler = Scheduler.from_components(
            settings,
            self.conn,
            self.state.components,
            started_at=self.state.started_at,
            channel=None if self.stack is None else self.stack.channel,
            core_jobs=(*CORE_JOBS, cleanup_job),
            panel_first_run_s=PANEL_FIRST_RUN_S,
        )
        self.loop.scheduler = self.scheduler
        # The status relay (dashboard.pusher): the remote tier of the snapshot to Supabase.
        self.pusher: SnapshotPusher | None = None
        if settings.relay.enabled:
            self.pusher = SnapshotPusher(
                self.conn,
                settings.relay,
                snapshot=lambda redactor: local_snapshot(self.state, redactor=redactor),
                secrets=(
                    secrets
                    if secrets is not None
                    else SecretStore(service=settings.keychain_service)
                ),
                instance=settings.instance.name,  # PER-304: the relay keys rows by it
            )
        else:
            clear_relay_health(self.conn)
        self.server = _Server(
            uvicorn.Config(
                create_app(self.state),
                host=settings.api.host,
                port=settings.api.port,
                log_level="warning",
                lifespan="off",
            )
        )
        self._stop = asyncio.Event()
        self.heartbeat = Heartbeat()

    @property
    def imessage(self) -> Stack | None:
        """The channel stack under its pre-PER-292 name (tests and older callers)."""
        return self.stack

    def healthy(self) -> bool:
        """The loop has ticked recently (within 3 ticks, or at least 90s).

        A startup grace period of the same length avoids sending /fail on every boot.
        """
        limit = max(3 * self.settings.loop.tick_s, 90.0)
        last = self.loop.last_tick or self.state.started_at
        return (utcnow() - last).total_seconds() < limit

    def stop(self) -> None:
        self._stop.set()

    async def _serve(self) -> None:
        # uvicorn calls sys.exit() when it can't bind. asyncio would let a SystemExit escape
        # the event loop, so turn it into an ordinary error.
        try:
            await self.server.serve()
        except SystemExit as e:
            raise RuntimeError(f"api server failed to start (exit {e.code})") from None

    async def run(self) -> int:
        """Run until stopped. Returns a process exit code."""
        aio = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                aio.add_signal_handler(sig, self.stop)

        if self.stack is not None:
            try:
                await self.stack.start(self.loop)
            except Exception:
                log.exception("no channel could start")
                await self.stack.aclose()
                self.conn.close()
                return 1
        self.scheduler.start()
        ext_host = self.state.components.extensions if self.state.components else None
        mcp_start = None
        if ext_host is not None and any(e.mcp for e in ext_host.loaded.values()):
            mcp_start = asyncio.create_task(ext_host.start_mcp(), name="mcp-start")
        if self.stack is not None and self.stack.claude is not None:
            # Before the loop resumes anything: end the processes a crash left behind.
            try:
                await self.stack.claude.start()
            except Exception:
                log.exception("runner startup failed")
        if self.stack is not None:
            from resonant.wiring import start_asks

            await start_asks(self.state.components)  # PER-262: the brain's leftovers
        loop_task = asyncio.create_task(self.loop.run(), name="loop")
        server_task = asyncio.create_task(self._serve(), name="api")
        stop_task = asyncio.create_task(self._stop.wait(), name="stop")
        aux = [asyncio.create_task(heartbeat_task(self.heartbeat), name="heartbeat")]
        monitor = self.state.monitor  # set only when the channel stack runs
        why = None if monitor is None else monitor.deadman_reason
        if url := self.settings.health.healthchecks_url:
            aux.append(
                asyncio.create_task(
                    deadman_task(url, self.settings.health.ping_every_s, self.healthy, reason=why),
                    name="deadman",
                )
            )
        if monitor is not None:
            aux.append(asyncio.create_task(monitor.run(), name="health-monitor"))
        if self.pusher is not None:
            aux.append(asyncio.create_task(self.pusher.run(), name="relay"))
        if self.stack is not None and self.stack.notifier is not None:
            aux.append(asyncio.create_task(self.stack.notifier.run(), name="notify-flush"))
        watchdog = Watchdog(self.heartbeat, self.settings.health.watchdog_stale_s)
        watchdog.start()
        host = self.state.components.extensions if self.state.components else None
        log.info(
            "%s",
            started_line(
                self.settings,
                imessage=self.stack is not None and "imessage" in self.stack.channel.names,
                relay=self.pusher is not None,
                extensions=0 if host is None else len(host.loaded),
                jobs=len(self.scheduler.jobs),
            ),
        )
        done, _ = await asyncio.wait(
            {loop_task, server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        crashed = stop_task not in done
        if crashed:
            log.error("component exited unexpectedly: %s", [t.get_name() for t in done])

        self.server.should_exit = True
        if self.stack is not None:
            await self.stack.stop()  # no new events while the loop drains
        await self.scheduler.aclose()
        if mcp_start is not None:
            mcp_start.cancel()
            await asyncio.gather(mcp_start, return_exceptions=True)
        self.loop.stop()
        stop_task.cancel()
        for t in aux:
            t.cancel()
        await asyncio.gather(*aux, return_exceptions=True)
        watchdog.halt()
        results = await asyncio.gather(loop_task, server_task, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                log.error("component error during shutdown: %r", r)
                crashed = True
        if ext_host is not None:
            await ext_host.aclose()  # after the drain: every MCP server's process group
        if self.stack is not None:
            if self.stack.claude is not None:
                await self.stack.claude.aclose()
            from resonant.wiring import close_asks

            await close_asks(self.state.components)  # BR-11: the Claude brain's session
            await self.stack.aclose()
        self.conn.close()
        log.info("resonant daemon stopped")
        return 1 if crashed else 0


def started_line(
    settings: Settings, *, imessage: bool, relay: bool, extensions: int, jobs: int
) -> str:
    """The startup log line. PER-297: the personal instance's is exactly as before
    instances; any other instance names itself first."""
    named = settings.instance
    who = "" if is_default(named.name) else f"instance {named.name} ({named.display_name}), "
    return (
        f"resonant daemon started ({who}api {settings.api.host}:{settings.api.port}, "
        f"gate=policy dry_run={settings.dry_run}, imessage={'on' if imessage else 'off'}, "
        f"relay={'on' if relay else 'off'}, extensions={extensions}, jobs={jobs})"
    )


def run(settings: Settings) -> int:
    configure_logging(settings.logs_dir)
    return asyncio.run(Daemon(settings).run())

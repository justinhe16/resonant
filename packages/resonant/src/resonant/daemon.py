"""Daemon entrypoint: the loop and the local API in one asyncio process.

launchd (KeepAlive) supervises the process. SIGTERM/SIGINT trigger a graceful stop:
in-flight steps get a short grace period, and anything unfinished resumes from its
checkpoint on the next start.

With ``channels.imessage.enabled`` the daemon also runs the Phase 1 stack
(``resonant.wiring``): the iMessage channel feeds ``loop.submit``, the router is the loop's
router hook, ``runner="local"`` answers, ``runner="reminder"`` sends due reminders, and the
health monitor runs; ``runner="job"`` runs scheduled jobs above L0; extension events are
notified (``extensions.notify``) and the notifier's end-of-hour flush runs. On shutdown the reader
and monitor stop first (no new events), then the loop drains in-flight steps (which may
still send their replies), then the model client closes. With it disabled, the daemon is
exactly Phase 0 apart from the gate and the scheduler.

Claude runs (Phase 5a). With iMessage on, ``runner="claude"`` is registered by the wiring.
Before the loop starts, the daemon calls ``ClaudeRunner.start()``: it ends every session
process a crash left behind (matched by pid and start time), wipes leftover token dirs and
hooks the kill switch, and only then does ``Loop.run`` resume tasks. The scheduler also gets
the ``core/run-cleanup`` job (``runners.claude.cleanup_job``).

The gate is always the Phase 2 ``PolicyGate``; the startup line logs
``gate=policy dry_run=<bool>``.

The scheduler (``extensions.scheduler.Scheduler``) is built after the components and the
iMessage stack and set as ``loop.scheduler``, iMessage on or off: L0 and core jobs run
inline from the loop's tick. It registers the kill switch hook at start; on shutdown,
after the reader stops, its inline runs are cancelled (decided again on the next start).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Generator
from typing import TYPE_CHECKING

import uvicorn

from resonant.api import DaemonState, create_app
from resonant.components import build_components
from resonant.config import Settings
from resonant.extensions.scheduler import CORE_JOBS, Scheduler
from resonant.health import Heartbeat, Watchdog, deadman_task, heartbeat_task
from resonant.loop import Loop, SlotPool
from resonant.loop.echo import EchoRunner
from resonant.observability import configure_logging
from resonant.runners.claude import cleanup_job
from resonant.store.db import open_db, utcnow

if TYPE_CHECKING:
    from resonant.extensions.process import ExtRunner
    from resonant.gateway.imessage.sender import OsaRunner
    from resonant.models import ModelClient
    from resonant.ops import OpsRunner
    from resonant.secrets import SecretStore
    from resonant.wiring import IMessageStack

log = logging.getLogger(__name__)


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
    ) -> None:
        """``model`` and ``osa_runner`` replace Ollama and osascript (tests, ``bench e2e``).

        They are used only when ``channels.imessage.enabled`` is true. ``ops_runner``
        replaces the ops tools' subprocesses (git, uv, launchctl); ``secrets`` and
        ``ext_runner`` replace the Keychain and the extension process runner.
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
        self.imessage: IMessageStack | None = None
        if settings.channels.imessage.enabled:
            from resonant.wiring import build_imessage_stack

            self.imessage = build_imessage_stack(
                settings,
                self.conn,
                self.state.components,
                self.loop,
                daemon_healthy=self.healthy,
                model=model,
                osa_runner=osa_runner,
            )
            self.state.monitor = self.imessage.monitor
        self.scheduler = Scheduler.from_components(
            settings,
            self.conn,
            self.state.components,
            started_at=self.state.started_at,
            channel=None if self.imessage is None else self.imessage.channel,
            core_jobs=(*CORE_JOBS, cleanup_job),
        )
        self.loop.scheduler = self.scheduler
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

        if self.imessage is not None:
            try:
                await self.imessage.start(self.loop)
            except Exception:
                log.exception("imessage channel failed to start")
                await self.imessage.aclose()
                self.conn.close()
                return 1
        self.scheduler.start()
        if self.imessage is not None and self.imessage.claude is not None:
            # Before the loop resumes anything: end the processes a crash left behind.
            try:
                await self.imessage.claude.start()
            except Exception:
                log.exception("runner startup failed")
        loop_task = asyncio.create_task(self.loop.run(), name="loop")
        server_task = asyncio.create_task(self._serve(), name="api")
        stop_task = asyncio.create_task(self._stop.wait(), name="stop")
        aux = [asyncio.create_task(heartbeat_task(self.heartbeat), name="heartbeat")]
        monitor = self.state.monitor  # set only when channels.imessage.enabled
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
        if self.imessage is not None and self.imessage.notifier is not None:
            aux.append(asyncio.create_task(self.imessage.notifier.run(), name="notify-flush"))
        watchdog = Watchdog(self.heartbeat, self.settings.health.watchdog_stale_s)
        watchdog.start()
        host = self.state.components.extensions if self.state.components else None
        log.info(
            "resonant daemon started (api %s:%s, gate=policy dry_run=%s, imessage=%s, "
            "extensions=%d, jobs=%d)",
            self.settings.api.host,
            self.settings.api.port,
            self.settings.dry_run,
            "on" if self.imessage is not None else "off",
            0 if host is None else len(host.loaded),
            len(self.scheduler.jobs),
        )
        done, _ = await asyncio.wait(
            {loop_task, server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        crashed = stop_task not in done
        if crashed:
            log.error("component exited unexpectedly: %s", [t.get_name() for t in done])

        self.server.should_exit = True
        if self.imessage is not None:
            await self.imessage.stop()  # no new events while the loop drains
        await self.scheduler.aclose()
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
        if self.imessage is not None:
            if self.imessage.claude is not None:
                await self.imessage.claude.aclose()
            await self.imessage.aclose()
        self.conn.close()
        log.info("resonant daemon stopped")
        return 1 if crashed else 0


def run(settings: Settings) -> int:
    configure_logging(settings.logs_dir)
    return asyncio.run(Daemon(settings).run())

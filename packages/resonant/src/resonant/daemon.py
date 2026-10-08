"""Daemon entrypoint: the loop and the local API in one asyncio process.

launchd (KeepAlive) supervises the process. SIGTERM/SIGINT trigger a graceful stop:
in-flight steps get a short grace period, and anything unfinished resumes from its
checkpoint on the next start.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Generator

import uvicorn

from resonant.api import DaemonState, create_app
from resonant.config import Settings
from resonant.health import Heartbeat, Watchdog, deadman_task, heartbeat_task
from resonant.loop import Loop, SlotPool
from resonant.loop.echo import EchoRunner
from resonant.observability import configure_logging
from resonant.store.db import open_db, utcnow

log = logging.getLogger(__name__)


class _Server(uvicorn.Server):
    """uvicorn without its own signal handling: the daemon owns shutdown."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


class Daemon:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.conn = open_db(settings.db_path)
        self.loop = Loop(
            self.conn,
            runners={"echo": EchoRunner()},
            config=settings.loop,
            slots=SlotPool(settings.claude.max_concurrent, settings.claude.reserved_oncall_slots),
        )
        self.state = DaemonState(settings, self.conn, self.loop, started_at=utcnow())
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

        loop_task = asyncio.create_task(self.loop.run(), name="loop")
        server_task = asyncio.create_task(self._serve(), name="api")
        stop_task = asyncio.create_task(self._stop.wait(), name="stop")
        aux = [asyncio.create_task(heartbeat_task(self.heartbeat), name="heartbeat")]
        if url := self.settings.health.healthchecks_url:
            aux.append(
                asyncio.create_task(
                    deadman_task(url, self.settings.health.ping_every_s, self.healthy),
                    name="deadman",
                )
            )
        watchdog = Watchdog(self.heartbeat, self.settings.health.watchdog_stale_s)
        watchdog.start()
        log.info(
            "resonant daemon started (api %s:%s, dry_run=%s)",
            self.settings.api.host,
            self.settings.api.port,
            self.settings.dry_run,
        )
        done, _ = await asyncio.wait(
            {loop_task, server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        crashed = stop_task not in done
        if crashed:
            log.error("component exited unexpectedly: %s", [t.get_name() for t in done])

        self.server.should_exit = True
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
        self.conn.close()
        log.info("resonant daemon stopped")
        return 1 if crashed else 0


def run(settings: Settings) -> int:
    configure_logging(settings.logs_dir)
    return asyncio.run(Daemon(settings).run())

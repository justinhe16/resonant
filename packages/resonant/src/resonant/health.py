"""Liveness: an internal watchdog plus an external dead-man switch.

- Heartbeat: an asyncio task beats every second. If the event loop stalls (a blocking
  call, a deadlock), the beats stop.
- Watchdog: a separate *thread* checks the heartbeat age. Past ``watchdog_stale_s`` it
  calls ``os._exit(1)`` and launchd (KeepAlive) restarts the daemon. A whole-process
  freeze (SIGSTOP, a kernel hang) also stops this thread, which is why the dead-man exists.
- Dead-man: pings healthchecks.io every ``ping_every_s`` while healthy, and sends ``/fail``
  (with a plain-text reason body) when the loop is stale or the health monitor reports a
  problem it can't text the owner about. If pings stop entirely (machine down, process
  frozen, network out), healthchecks.io alerts on its own.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable

import httpx

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last = clock()

    def beat(self) -> None:
        self._last = self._clock()

    def age(self) -> float:
        return self._clock() - self._last


async def heartbeat_task(hb: Heartbeat, interval_s: float = 1.0) -> None:
    while True:
        hb.beat()
        await asyncio.sleep(interval_s)


class Watchdog(threading.Thread):
    def __init__(
        self,
        hb: Heartbeat,
        stale_s: float,
        *,
        check_every_s: float = 5.0,
        on_stall: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(name="resonant-watchdog", daemon=True)
        self.hb = hb
        self.stale_s = stale_s
        self.check_every_s = check_every_s
        self.on_stall = on_stall or _hard_exit
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.wait(self.check_every_s):
            if self.hb.age() > self.stale_s:
                log.critical("event loop stalled for %.0fs; exiting for restart", self.hb.age())
                self.on_stall()
                return

    def halt(self) -> None:
        self._halt.set()


def _hard_exit() -> None:
    # Don't flush logging: a stalled thread may hold a handler lock. Write straight to fd 2.
    os.write(2, b"resonant watchdog: event loop stalled; exiting for launchd restart\n")
    os._exit(1)


async def deadman_task(
    url: str,
    interval_s: float,
    healthy: Callable[[], bool],
    client: httpx.AsyncClient | None = None,
    reason: Callable[[], str | None] | None = None,
) -> None:
    """Ping ``url`` while healthy; otherwise POST ``/fail`` with a short plain-text reason.

    ``reason`` (e.g. ``HealthMonitor.deadman_reason``) returns why the owner can't be
    reached by other means, or None. A non-None reason sends ``/fail`` even while the loop
    is healthy. The URL holds the check's secret UUID, so it is never logged.
    """
    owns = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        while True:
            ok, why = healthy(), _safe_reason(reason)
            try:
                if ok and why is None:
                    await client.get(url)
                else:
                    body = "; ".join(r for r in (None if ok else "event loop stale", why) if r)
                    await client.post(
                        f"{url.rstrip('/')}/fail",
                        content=body[:FAIL_BODY_MAX].encode(),
                        headers={"content-type": "text/plain; charset=utf-8"},
                    )
            except httpx.HTTPError as e:
                # str(e) can include the request URL (e.g. HTTPStatusError): log the type only.
                log.warning("dead-man ping failed: %s", type(e).__name__)
            await asyncio.sleep(interval_s)
    finally:
        if owns:
            await client.aclose()


FAIL_BODY_MAX = 500


def _safe_reason(reason: Callable[[], str | None] | None) -> str | None:
    if reason is None:
        return None
    try:
        return reason()
    except Exception:
        log.exception("dead-man reason callback failed")
        return "health reason unavailable"

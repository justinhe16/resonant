from __future__ import annotations

import asyncio
import threading

import httpx

from resonant.health import Heartbeat, Watchdog, deadman_task


def test_watchdog_fires_on_stall() -> None:
    now = [0.0]
    hb = Heartbeat(clock=lambda: now[0])
    fired = threading.Event()
    wd = Watchdog(hb, stale_s=10, check_every_s=0.01, on_stall=fired.set)
    wd.start()
    assert not fired.wait(0.05)
    now[0] = 11.0
    assert fired.wait(1)


def test_watchdog_quiet_while_beating() -> None:
    hb = Heartbeat()
    fired = threading.Event()
    wd = Watchdog(hb, stale_s=0.2, check_every_s=0.01, on_stall=fired.set)
    wd.start()
    for _ in range(30):
        hb.beat()
        assert not fired.wait(0.01)
    wd.halt()


async def test_deadman_pings_success_and_fail() -> None:
    seen: list[str] = []
    healthy = [True]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    task = asyncio.create_task(
        deadman_task("https://hc.example/abc", 0.01, lambda: healthy[0], client)
    )
    await asyncio.sleep(0.03)
    healthy[0] = False
    await asyncio.sleep(0.03)
    task.cancel()
    assert seen[0] == "/abc"
    assert seen[-1] == "/abc/fail"

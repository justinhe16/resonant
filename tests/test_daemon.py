from __future__ import annotations

import asyncio
import socket

import httpx

from resonant.config import load_settings
from resonant.daemon import Daemon
from resonant.e2e import wait_until
from resonant.tools.builtin import BUILTIN_SPECS


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def wait_listening(daemon: Daemon, run: asyncio.Task[int]) -> None:
    """Until the API socket listens (uvicorn's ``started``). Polling with ConnectError
    retries gave up after a fixed 4s, which a loaded machine's startup could outlast."""
    await wait_until(lambda: daemon.server.started or run.done(), 30)
    if run.done():
        raise AssertionError(f"daemon exited during startup: {run.result()!r}")


async def test_daemon_serves_and_stops_cleanly() -> None:
    port = free_port()
    daemon = Daemon(load_settings(api={"port": port}))
    run = asyncio.create_task(daemon.run())
    await wait_listening(daemon, run)
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"http://127.0.0.1:{port}/healthz")).json() == {"ok": True}
        status = (await client.get(f"http://127.0.0.1:{port}/api/status")).json()
        tools = (await client.get(f"http://127.0.0.1:{port}/api/tools")).json()
    assert status["last_tick"] is not None
    assert len(tools) == len(BUILTIN_SPECS)
    daemon.stop()
    assert await asyncio.wait_for(run, 5) == 0


async def test_daemon_exits_nonzero_when_port_taken() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = int(s.getsockname()[1])
        daemon = Daemon(load_settings(api={"port": port}))
        assert await asyncio.wait_for(daemon.run(), 5) == 1

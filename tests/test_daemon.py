from __future__ import annotations

import asyncio
import socket

import httpx

from resonant.config import load_settings
from resonant.daemon import Daemon


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def wait_healthy(port: int) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        for _ in range(200):
            try:
                return await client.get(f"http://127.0.0.1:{port}/healthz")
            except httpx.ConnectError:
                await asyncio.sleep(0.02)
    raise AssertionError("daemon never became healthy")


async def test_daemon_serves_and_stops_cleanly() -> None:
    port = free_port()
    daemon = Daemon(load_settings(api={"port": port}))
    run = asyncio.create_task(daemon.run())
    assert (await wait_healthy(port)).json() == {"ok": True}
    async with httpx.AsyncClient() as client:
        status = (await client.get(f"http://127.0.0.1:{port}/api/status")).json()
        tools = (await client.get(f"http://127.0.0.1:{port}/api/tools")).json()
    assert status["last_tick"] is not None
    assert len(tools) == 5
    daemon.stop()
    assert await asyncio.wait_for(run, 5) == 0


async def test_daemon_exits_nonzero_when_port_taken() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = int(s.getsockname()[1])
        daemon = Daemon(load_settings(api={"port": port}))
        assert await asyncio.wait_for(daemon.run(), 5) == 1

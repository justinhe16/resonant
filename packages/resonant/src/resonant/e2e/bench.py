"""``resonant bench e2e``: text-in to send-call latency, through the real Phase 1 stack.

What runs is production code end to end: the chat.db reader (WAL watch and polling), the
loop, the router (fast path or LLM label), the local runner (tools through gate and
executor), and ``IMessageChannel.send``. Three things are stand-ins, so the bench never
texts anyone and never touches the live daemon's state:

- ``chat.db`` is a ``FakeChatDB`` in a temporary home; each prompt is one inserted row.
- osascript is a ``RecordingOsaRunner``; the clock stops when the send call is made.
- The model is a ``ScriptedModel`` (``model="fake"``, the default and what CI runs) or the
  configured Ollama model (``model="real"``, on the Mini).

Latency is measured from the row insert to the osascript call, per path:

- ``fast``: slash commands and anchored keywords (no model call).
- ``llm``: one label call, then the runner's tool loop and answer.

Messages.app delivery and osascript's own run time are not included (the iMessage self-test
covers that path). Exit criterion on the Mini: ``fast`` p50 <= 1s and ``llm`` p50 <= 3s.
Prompts are synthetic; no real message bodies.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from resonant.api import DaemonState
from resonant.components import build_components
from resonant.config import ChannelsConfig, IMessageConfig, Settings
from resonant.e2e.fakes import RecordingOsaRunner, ScriptedModel, wait_until
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop import Loop, SlotPool
from resonant.loop.echo import EchoRunner
from resonant.models import ModelClient
from resonant.models.bench import percentile
from resonant.store.db import open_db, utcnow
from resonant.wiring import build_imessage_stack

BenchPath = Literal["fast", "llm"]

OWNER_HANDLE = "+15550100100"
TARGET_P50_MS: dict[BenchPath, int] = {"fast": 1000, "llm": 3000}
FAST_PROMPTS = ("what's running?", "status", "ping", "how's the mini", "/tasks")
LLM_PROMPTS = (
    "anything queued up right now?",
    "how many tasks finished today?",
    "is the mini under a lot of memory pressure?",
    "which jobs are waiting on something?",
)
READY_TIMEOUT_S = 10.0
FAKE_SEND_TIMEOUT_S = 10.0

PRINCIPALS_YAML = f"""\
principals:
  owner:
    role: owner
    identities: ["imessage:{OWNER_HANDLE}"]
"""


@dataclass
class PathResult:
    path: BenchPath
    latencies_ms: list[float] = field(default_factory=list[float])
    failures: int = 0

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "n": len(self.latencies_ms),
            "failures": self.failures,
            "target_p50_ms": TARGET_P50_MS[self.path],
        }
        if self.latencies_ms:
            out["p50_ms"] = round(percentile(self.latencies_ms, 50))
            out["p95_ms"] = round(percentile(self.latencies_ms, 95))
        out["pass"] = (
            bool(self.latencies_ms)
            and self.failures == 0
            and out["p50_ms"] <= TARGET_P50_MS[self.path]
        )
        return out


def bench_settings(home: Path, chat_db: Path, base: Settings | None = None) -> Settings:
    """Settings for an isolated bench home. ``model`` comes from ``base`` (the user's config).

    Rate limits are raised so N quick prompts measure latency, not the token bucket.
    """
    model = (base or Settings(home=home)).model
    return Settings(
        home=home,
        model=model,
        channels=ChannelsConfig(
            imessage=IMessageConfig(
                enabled=True,
                db_path=chat_db,
                inbound_per_minute=100_000,
                outbound_per_minute=100_000,
            )
        ),
    )


async def run_e2e_bench(
    n: int = 10,
    *,
    model: ModelClient | None = None,
    base: Settings | None = None,
    send_timeout_s: float | None = None,
    warmup: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> list[PathResult]:
    """Run ``n`` prompts per path in a throwaway home; return the latencies per path.

    ``model`` None uses a ``ScriptedModel``; pass an ``OllamaClient`` for the real thing.
    One untimed prompt per path warms things up first (a cold model load is not latency).
    """
    if send_timeout_s is None:
        timeout = FAKE_SEND_TIMEOUT_S if model is None else (base or Settings()).model.timeout_s * 5
    else:
        timeout = send_timeout_s
    with tempfile.TemporaryDirectory(prefix="resonant-bench-") as tmp:
        home = Path(tmp)
        (home / "principals.yaml").write_text(PRINCIPALS_YAML)
        chat = FakeChatDB(home / "chat.db")
        settings = bench_settings(home, chat.path, base)
        conn = open_db(settings.db_path)
        osa = RecordingOsaRunner(clock)
        loop = Loop(
            conn,
            runners={"echo": EchoRunner()},
            config=settings.loop,
            slots=SlotPool(settings.claude.max_concurrent, settings.claude.reserved_oncall_slots),
        )
        state = DaemonState(settings, conn, loop, started_at=utcnow())
        state.components = build_components(settings, conn, state)
        stack = build_imessage_stack(
            settings,
            conn,
            state.components,
            loop,
            daemon_healthy=lambda: True,
            model=model if model is not None else ScriptedModel(),
            osa_runner=osa,
        )
        await stack.start(loop)
        loop_task = asyncio.create_task(loop.run(), name="bench-loop")
        try:
            await _wait_ready(stack.channel.health)
            results: list[PathResult] = []
            plan: tuple[tuple[BenchPath, tuple[str, ...]], ...] = (
                ("fast", FAST_PROMPTS),
                ("llm", LLM_PROMPTS),
            )
            for path, prompts in plan:
                result = PathResult(path)
                if warmup:
                    with contextlib.suppress(TimeoutError):
                        await _one(chat, osa, prompts[0], timeout, clock)
                for i in range(n):
                    try:
                        ms = await _one(chat, osa, prompts[i % len(prompts)], timeout, clock)
                    except TimeoutError:
                        result.failures += 1
                        continue
                    result.latencies_ms.append(ms)
                results.append(result)
            return results
        finally:
            await stack.stop()
            loop.stop()
            with contextlib.suppress(Exception):
                await loop_task
            await stack.aclose()
            conn.close()
            chat.close()


async def _wait_ready(health: Callable[[], dict[str, Any]]) -> None:
    """The reader starts at chat.db's newest row: wait for that baseline read."""
    await wait_until(lambda: health()["last_read_at"] is not None, READY_TIMEOUT_S)


async def _one(
    chat: FakeChatDB,
    osa: RecordingOsaRunner,
    text: str,
    timeout_s: float,
    clock: Callable[[], float],
) -> float:
    before = len(osa.sent)
    start = clock()
    chat.add_message(handle=OWNER_HANDLE, text=text)
    sent = await osa.wait_for(before + 1, timeout_s)
    return (sent.at - start) * 1000


def report(results: list[PathResult], *, model_name: str) -> dict[str, Any]:
    summaries = [r.summary() for r in results]
    return {
        "model": model_name,
        "results": summaries,
        "pass": all(s["pass"] for s in summaries),
    }

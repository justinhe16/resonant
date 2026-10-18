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

- ``fast``: slash commands and the `status`/`ping` keywords (no model call).
- ``llm``: one label call, then the runner's tool loop and answer.
- ``loop``: ``brain.mode: loop`` (BR-1), in its own home: the brain loop's rounds (the fake
  model reads one directory, then answers) and its reply.

Messages.app delivery and osascript's own run time are not included (the iMessage self-test
covers that path). Exit criterion on the Mini: ``fast`` p50 <= 1s, ``llm`` p50 <= 3s and
``loop`` p50 <= 5s (docs/brain-plan.md §4).
Prompts are synthetic; no real message bodies.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from resonant.api import DaemonState
from resonant.components import build_components
from resonant.config import (
    BrainConfig,
    ChannelsConfig,
    IMessageConfig,
    OpsConfig,
    RunnerConfig,
    Settings,
)
from resonant.e2e.fakes import RecordingOsaRunner, ScriptedModel, wait_until
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop import Loop, SlotPool
from resonant.loop.echo import EchoRunner
from resonant.models import ModelClient
from resonant.models.bench import percentile
from resonant.store.db import open_db, utcnow
from resonant.wiring import build_imessage_stack

BenchPath = Literal["fast", "llm", "loop"]
PATHS: tuple[BenchPath, ...] = ("fast", "llm", "loop")

OWNER_HANDLE = "+15550100100"
# loop: docs/brain-plan.md §4, "a simple loop read ~5 s"
TARGET_P50_MS: dict[BenchPath, int] = {"fast": 1000, "llm": 3000, "loop": 5000}
FAST_PROMPTS = ("status", "ping", "/status", "/tasks", "/help")
LLM_PROMPTS = (
    "anything queued up right now?",
    "how many tasks finished today?",
    "is the mini under a lot of memory pressure?",
    "which jobs are waiting on something?",
)
LOOP_PROMPTS = (
    "where is the resonant repo checked out?",
    "what's in the resonant readme?",
    "list what's in my code folder",
)
PROMPTS: dict[BenchPath, tuple[str, ...]] = {
    "fast": FAST_PROMPTS,
    "llm": LLM_PROMPTS,
    "loop": LOOP_PROMPTS,
}
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


class IsolatedSettings(Settings):
    """``Settings`` from its arguments only: no ``RESONANT_*`` env vars, no ``config.yaml``.
    A throwaway home (the bench, the task eval) must not pick up the live daemon's tuning."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def bench_settings(
    home: Path,
    chat_db: Path,
    base: Settings | None = None,
    *,
    mode: Literal["intents", "loop"] = "intents",
) -> Settings:
    """Settings for an isolated bench home. ``model`` comes from ``base`` (the user's config).

    Rate limits are raised so N quick prompts measure latency, not the token bucket. The
    runner's dirs, the read roots and ``ops.repo_path`` stay inside the home, and
    ``dry_run`` stays on: the loop path never touches ``~/code`` or ``~/.resonant``.
    """
    model = (base or IsolatedSettings(home=home)).model
    code = home / "code"
    return IsolatedSettings(
        home=home,
        model=model,
        runner=RunnerConfig(
            code_dir=code,
            work_dir=home / "runs",
            mirror_dir=home / "runner" / "mirrors",
            secrets_dir=home / "run-secrets",
            cache_dir=home / "runs-cache",
        ),
        ops=OpsConfig(repo_path=None),
        brain=BrainConfig(mode=mode, read_roots=[code, home / "logs"]),
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
    paths: Sequence[BenchPath] = PATHS,
) -> list[PathResult]:
    """Run ``n`` prompts per path in a throwaway home; return the latencies per path.

    ``model`` None uses a ``ScriptedModel``; pass an ``OllamaClient`` for the real thing.
    One untimed prompt per path warms things up first (a cold model load is not latency).
    ``fast`` and ``llm`` run with ``brain.mode: intents``; ``loop`` gets its own home with
    ``brain.mode: loop`` (the fake model reads one directory, then answers).
    """
    if send_timeout_s is None:
        timeout = FAKE_SEND_TIMEOUT_S if model is None else (base or Settings()).model.timeout_s * 5
    else:
        timeout = send_timeout_s
    results: list[PathResult] = []
    intents: list[BenchPath] = [p for p in paths if p != "loop"]
    if intents:
        fake = ScriptedModel() if model is None else None
        async with _stack("intents", model or cast(ModelClient, fake), base, clock) as b:
            for path in intents:
                results.append(await _run_path(b, path, n, timeout, warmup, None))
    if "loop" in paths:
        fake = ScriptedModel(script=[]) if model is None else None
        async with _stack("loop", model or cast(ModelClient, fake), base, clock) as b:
            results.append(await _run_path(b, "loop", n, timeout, warmup, fake))
    return results


@dataclass
class _Bench:
    chat: FakeChatDB
    osa: RecordingOsaRunner
    loop: Loop
    code: Path
    clock: Callable[[], float]


@contextlib.asynccontextmanager
async def _stack(
    mode: Literal["intents", "loop"],
    model: ModelClient,
    base: Settings | None,
    clock: Callable[[], float],
) -> AsyncGenerator[_Bench]:
    with tempfile.TemporaryDirectory(prefix="resonant-bench-") as tmp:
        home = Path(tmp)
        (home / "principals.yaml").write_text(PRINCIPALS_YAML)
        chat = FakeChatDB(home / "chat.db")
        settings = bench_settings(home, chat.path, base, mode=mode)
        code = settings.runner.code_dir
        (code / "resonant").mkdir(parents=True, exist_ok=True)
        (code / "resonant" / "README.md").write_text("# resonant\n")
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
            model=model,
            osa_runner=osa,
        )
        await stack.start(loop)
        loop_task = asyncio.create_task(loop.run(), name="bench-loop")
        try:
            await _wait_ready(stack.channel.health)
            yield _Bench(chat, osa, loop, code, clock)
        finally:
            await stack.stop()
            loop.stop()
            with contextlib.suppress(Exception):
                await loop_task
            await stack.aclose()
            conn.close()
            chat.close()


async def _run_path(
    b: _Bench,
    path: BenchPath,
    n: int,
    timeout_s: float,
    warmup: bool,
    fake: ScriptedModel | None,
) -> PathResult:
    prompts = PROMPTS[path]
    result = PathResult(path)

    async def one(text: str) -> float:
        if fake is not None:  # the loop's fake: one read, then the answer
            fake.script = []  # drop what a timed-out prompt left, so prompts stay aligned
            fake.push([("list_dir", {"path": str(b.code)})], "resonant is in ~/code/resonant.")
        return await _one(b.chat, b.osa, text, timeout_s, b.clock)

    if warmup:
        with contextlib.suppress(TimeoutError):
            await one(prompts[0])
        # A warm-up that timed out may still answer: wait it out so its late
        # send isn't credited to the first timed prompt.
        with contextlib.suppress(TimeoutError):
            await wait_until(lambda: b.loop.inflight == 0, timeout_s)
    for i in range(n):
        try:
            ms = await one(prompts[i % len(prompts)])
        except TimeoutError:
            result.failures += 1
            continue
        result.latencies_ms.append(ms)
        # the loop path may send "On it…" first: let the turn finish before the next one
        with contextlib.suppress(TimeoutError):
            await wait_until(lambda: b.loop.inflight == 0, timeout_s)
    return result


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

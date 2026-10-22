"""``resonant bench e2e``: text-in to send-call latency, through the real Phase 1 stack.

What runs is production code end to end: the chat.db reader (WAL watch and polling), the
loop, the router (fast path, or the brain's triage), the runners, and
``IMessageChannel.send``. Three things are stand-ins, so the bench never
texts anyone and never touches the live daemon's state:

- ``chat.db`` is a ``FakeChatDB`` in a temporary home; each prompt is one inserted row.
- osascript is a ``RecordingOsaRunner``; the clock stops when the send call is made.
- The model is a ``ScriptedModel`` (``model="fake"``, the default and what CI runs) or the
  configured Ollama model (``model="real"``, on the Mini).

Latency is measured from the row insert to the osascript call, per path:

- ``fast``: slash commands and the `status`/`ping` keywords (no model call).
- ``route`` (PER-256): a greeting through the brain: one triage call from the local model,
  then the reply it routes to itself. (Everything else goes to Claude, whose latency is
  Claude's, not this bench's.)

Messages.app delivery and osascript's own run time are not included (the iMessage self-test
covers that path). Exit criterion on the Mini: ``fast`` p50 <= 1s and ``route`` p50 <= 3s
(docs/brain-plan.md §4).
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

BenchPath = Literal["fast", "route"]
PATHS: tuple[BenchPath, ...] = ("fast", "route")

OWNER_HANDLE = "+15550100100"
TARGET_P50_MS: dict[BenchPath, int] = {"fast": 1000, "route": 3000}
FAST_PROMPTS = ("status", "ping", "/status", "/tasks", "/help")
ROUTE_PROMPTS = ("hey", "good morning", "hi there", "hello")
PROMPTS: dict[BenchPath, tuple[str, ...]] = {
    "fast": FAST_PROMPTS,
    "route": ROUTE_PROMPTS,
}
GREETING_TRIAGE = {"handler": "local", "kind": "general", "message_type": "greeting", "say": "Hey!"}
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
        brain=BrainConfig(read_roots=[code, home / "logs"]),
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
    """
    if send_timeout_s is None:
        timeout = FAKE_SEND_TIMEOUT_S if model is None else (base or Settings()).model.timeout_s * 5
    else:
        timeout = send_timeout_s
    results: list[PathResult] = []
    fake = ScriptedModel() if model is None else None
    if fake is not None:
        fake.triage = dict(GREETING_TRIAGE)
    async with _stack(model or cast(ModelClient, fake), base, clock) as b:
        for path in paths:
            results.append(await _run_path(b, path, n, timeout, warmup))
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
    model: ModelClient,
    base: Settings | None,
    clock: Callable[[], float],
) -> AsyncGenerator[_Bench]:
    with tempfile.TemporaryDirectory(prefix="resonant-bench-") as tmp:
        home = Path(tmp)
        (home / "principals.yaml").write_text(PRINCIPALS_YAML)
        chat = FakeChatDB(home / "chat.db")
        settings = bench_settings(home, chat.path, base)
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
) -> PathResult:
    prompts = PROMPTS[path]
    result = PathResult(path)

    async def one(text: str) -> float:
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
        # let the turn finish before the next one
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

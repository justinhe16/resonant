"""Real probe and bench against a local Ollama. Run with: uv run pytest -m integration"""

from __future__ import annotations

import pytest

from resonant.config import ModelConfig, load_settings
from resonant.models import OllamaClient
from resonant.models.bench import run_bench

pytestmark = pytest.mark.integration


def _cfg() -> ModelConfig:
    return load_settings().model


@pytest.mark.parametrize("api", ["ollama", "openai"])
async def test_real_probe(api: str) -> None:
    async with OllamaClient(_cfg(), api="openai" if api == "openai" else "ollama") as client:
        result = await client.probe()
    print(f"{api}: {result}")  # shown with -s; this decides the default adapter
    assert result.model_present and result.ctx_ok
    if api == "ollama":  # the default must turn thinking off
        assert result.thinking_off_ok, result.detail


async def test_real_bench() -> None:
    async with OllamaClient(_cfg()) as client:
        series = await run_bench(client, n=5)
    for s in series:
        print(s.summary())
        assert s.latencies_ms, f"{s.kind}: every call failed"

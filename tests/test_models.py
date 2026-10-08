from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import pytest
from typer.testing import CliRunner

from resonant.cli import app
from resonant.config import ModelConfig, load_settings
from resonant.models import (
    ChatMessage,
    ModelOutputError,
    ModelUnavailableError,
    OllamaClient,
    tools_prefix,
)
from resonant.models.bench import percentile, run_bench
from resonant.models.client import (
    JSON_NUDGE,
    RawToolCall,
    SpanLike,
    has_think,
    ollama_root,
    strip_think,
    validate_tool_calls,
)
from resonant.store.db import kv_get, open_db
from resonant_sdk import Effect, ToolSpec

Api = Literal["openai", "ollama"]
APIS: list[Api] = ["ollama", "openai"]
CFG = ModelConfig(timeout_s=5)
LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["chat", "task"]}},
    "required": ["label"],
}
MSGS: list[ChatMessage] = [{"role": "user", "content": "pii-body-xyz"}]


def _weather() -> ToolSpec:
    return ToolSpec(
        name="weather",
        description="Weather for a city.",
        args_schema={
            "type": "object",
            "required": ["city"],
            "properties": {"city": {"type": "string"}},
            "additionalProperties": False,
        },
        effect=Effect.READ,
    )


def _clock() -> ToolSpec:
    return ToolSpec(name="current_time", description="Now.", effect=Effect.READ)


# --- fake Ollama -----------------------------------------------------------------------


@dataclass
class Turn:
    """One scripted model reply, rendered in whichever wire format was requested."""

    content: str | None = ""
    thinking: str | None = None
    tool_calls: list[tuple[str, object]] = field(default_factory=list[tuple[str, object]])
    truncated: bool = False


@dataclass
class FakeOllama:
    turns: list[Turn] = field(default_factory=list[Turn])
    models: list[str] = field(default_factory=lambda: ["qwen3:30b-a3b"])
    context_length: int = 40960
    parameters: str = ""
    loaded: bool = True
    chat_status: int = 200
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if path == "/api/show":
            return httpx.Response(
                200,
                json={
                    "model_info": {"qwen3moe.context_length": self.context_length},
                    "parameters": self.parameters,
                },
            )
        if path == "/api/ps":
            models = [{"name": self.models[0], "context_length": 32768}] if self.loaded else []
            return httpx.Response(200, json={"models": models})
        if path in ("/api/chat", "/v1/chat/completions"):
            if self.chat_status != 200:
                return httpx.Response(self.chat_status, json={"error": "model not found"})
            turn = self.turns.pop(0)
            if path == "/api/chat":
                return httpx.Response(200, json=self._native(turn))
            return httpx.Response(200, json=self._openai(turn))
        return httpx.Response(404)

    @staticmethod
    def _native(t: Turn) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": t.content or ""}
        if t.thinking is not None:
            msg["thinking"] = t.thinking
        if t.tool_calls:
            msg["tool_calls"] = [{"function": {"name": n, "arguments": a}} for n, a in t.tool_calls]
        return {
            "message": msg,
            "done": True,
            "done_reason": "length" if t.truncated else "stop",
            "prompt_eval_count": 11,
            "eval_count": 3,
        }

    @staticmethod
    def _openai(t: Turn) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": t.content}
        if t.thinking is not None:
            msg["reasoning"] = t.thinking
        if t.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": n,
                        "arguments": a if isinstance(a, str) else json.dumps(a),
                    },
                }
                for i, (n, a) in enumerate(t.tool_calls)
            ]
        return {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "qwen3:30b-a3b",
            "choices": [
                {
                    "index": 0,
                    "message": msg,
                    "finish_reason": "length" if t.truncated else "stop",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        }


class RecordingSpans:
    def __init__(self) -> None:
        self.spans: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, **attrs: Any) -> AbstractContextManager[SpanLike]:
        return self._cm(name, attrs)

    @contextmanager
    def _cm(self, name: str, attrs: dict[str, Any]) -> Generator[SpanLike]:
        rec = _Rec(dict(attrs))
        self.spans.append((name, rec.attrs))
        try:
            yield rec
        except BaseException as e:
            rec.attrs["error"] = type(e).__name__
            raise


@dataclass
class _Rec:
    attrs: dict[str, Any]

    def set(self, **attrs: Any) -> None:
        self.attrs.update(attrs)


def make(
    fake: FakeOllama, api: Api = "ollama", spans: RecordingSpans | None = None
) -> OllamaClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return OllamaClient(CFG, api=api, http=http, spans=spans)


# --- pure helpers ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ('<think>hmm</think>{"a": 1}', '{"a": 1}'),
        ('<think>\n\n</think>\n\n{"a": 1}', '{"a": 1}'),
        ('{"a": 1}<think>trunc', '{"a": 1}'),
        ('reasoning...</think>{"a": 1}', '{"a": 1}'),
        ('{"a": 1}', '{"a": 1}'),
    ],
)
def test_strip_think(raw: str, want: str) -> None:
    assert strip_think(raw) == want


def test_has_think_and_root() -> None:
    assert has_think("<THINK>x") and not has_think(None) and not has_think("{}")
    assert ollama_root("http://127.0.0.1:11434/v1/") == "http://127.0.0.1:11434"
    assert ollama_root("http://localhost:11434") == "http://localhost:11434"


def test_tools_prefix_is_byte_identical_for_any_order() -> None:
    a = tools_prefix([_weather(), _clock()])
    shuffled = ToolSpec(
        name="weather",
        description="Weather for a city.",
        args_schema={
            "additionalProperties": False,
            "properties": {"city": {"type": "string"}},
            "type": "object",
            "required": ["city"],
        },
        effect=Effect.READ,
    )
    b = tools_prefix([_clock(), shuffled])
    assert a == b
    assert json.loads(a)[0]["function"]["name"] == "current_time"  # sorted by name


def test_tools_prefix_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        tools_prefix([_clock(), _clock()])


def test_validate_tool_calls() -> None:
    raw = [
        RawToolCall("weather", '{"city": "Oakland"}'),  # JSON string args
        RawToolCall("weather", {"city": "SF"}),
        RawToolCall("current_time", None),  # no args at all
        RawToolCall("rm_rf", {}),  # unknown
        RawToolCall("weather", {"town": "x"}),  # schema-invalid
        RawToolCall("weather", "{not json"),
        RawToolCall("weather", "[1]"),
    ]
    kept, dropped = validate_tool_calls(raw, [_weather(), _clock()])
    assert [(c.name, c.args) for c in kept] == [
        ("weather", {"city": "Oakland"}),
        ("weather", {"city": "SF"}),
        ("current_time", {}),
    ]
    assert [d["reason"].split(":")[0] for d in dropped] == [
        "unknown_tool",
        "invalid_args",
        "args_not_json",
        "args_not_object",
    ]
    assert all("Oakland" not in json.dumps(d) for d in dropped)  # no arg values recorded


def test_percentile() -> None:
    xs = [float(x) for x in range(1, 21)]
    random.shuffle(xs)
    assert percentile(xs, 50) == 10
    assert percentile(xs, 95) == 19
    assert percentile([5.0], 95) == 5
    with pytest.raises(ValueError):
        percentile([], 50)


# --- chat_json -------------------------------------------------------------------------


@pytest.mark.parametrize("api", APIS)
async def test_chat_json_success(api: Api) -> None:
    fake = FakeOllama([Turn('{"label": "chat"}')])
    spans = RecordingSpans()
    async with make(fake, api, spans) as client:
        reply = await client.chat_json(MSGS, schema=LABEL_SCHEMA, max_tokens=16)
    assert reply.json == {"label": "chat"}
    assert reply.tool_calls == [] and reply.prompt_tokens == 11 and reply.completion_tokens == 3
    name, attrs = spans.spans[0]
    assert name == "llm.call"
    assert attrs["model"] == "qwen3:30b-a3b" and attrs["api"] == api
    assert attrs["attempts"] == 1 and "latency_ms" in attrs and not attrs["thinking_leak"]
    assert "pii-body-xyz" not in json.dumps(attrs)  # no prompt text on spans


async def test_native_request_turns_thinking_off_and_passes_schema() -> None:
    fake = FakeOllama([Turn('{"label": "chat"}')])
    async with make(fake, "ollama") as client:
        await client.chat_json(MSGS, schema=LABEL_SCHEMA, max_tokens=16, temperature=0.2)
    (body,) = fake.bodies("/api/chat")
    assert body["think"] is False and body["stream"] is False
    assert body["format"] == LABEL_SCHEMA
    assert body["options"] == {"temperature": 0.2, "num_predict": 16}


async def test_openai_request_turns_thinking_off_and_passes_schema() -> None:
    fake = FakeOllama([Turn('{"label": "chat"}')])
    async with make(fake, "openai") as client:
        await client.chat_json(MSGS, schema=LABEL_SCHEMA, max_tokens=16)
    (body,) = fake.bodies("/v1/chat/completions")
    assert body["think"] is False and body["reasoning_effort"] == "none"
    assert body["response_format"]["json_schema"]["schema"] == LABEL_SCHEMA
    assert body["max_tokens"] == 16


@pytest.mark.parametrize("api", APIS)
async def test_chat_json_strips_think_leak(api: Api) -> None:
    fake = FakeOllama([Turn('<think>the user greets</think>\n{"label": "chat"}')])
    spans = RecordingSpans()
    async with make(fake, api, spans) as client:
        reply = await client.chat_json(MSGS, schema=LABEL_SCHEMA)
    assert reply.json == {"label": "chat"}
    assert reply.text is not None and "<think>" not in reply.text
    assert spans.spans[0][1]["thinking_leak"] is True


@pytest.mark.parametrize("api", APIS)
async def test_chat_json_invalid_then_retry(api: Api) -> None:
    fake = FakeOllama([Turn("Sure! label: chat"), Turn('```json\n{"label": "task"}\n```')])
    spans = RecordingSpans()
    async with make(fake, api, spans) as client:
        reply = await client.chat_json(MSGS, schema=LABEL_SCHEMA)
    assert reply.json == {"label": "task"}
    assert reply.prompt_tokens == 22  # summed over both attempts
    assert spans.spans[0][1]["attempts"] == 2
    path = "/api/chat" if api == "ollama" else "/v1/chat/completions"
    first, second = fake.bodies(path)
    assert len(first["messages"]) == 1
    assert second["messages"][-1] == {"role": "user", "content": JSON_NUDGE}


@pytest.mark.parametrize("api", APIS)
async def test_chat_json_invalid_twice_raises(api: Api) -> None:
    # Schema-invalid, then truncated by the token limit (valid prefix doesn't count).
    fake = FakeOllama([Turn('{"label": "nope"}'), Turn('{"label": "chat"}', truncated=True)])
    spans = RecordingSpans()
    async with make(fake, api, spans) as client:
        with pytest.raises(ModelOutputError):
            await client.chat_json(MSGS, schema=LABEL_SCHEMA)
    assert spans.spans[0][1]["error"] == "ModelOutputError"
    assert spans.spans[0][1]["truncated"] is True


@pytest.mark.parametrize("api", APIS)
async def test_model_404_is_unavailable(api: Api) -> None:
    fake = FakeOllama(chat_status=404)
    async with make(fake, api) as client:
        with pytest.raises(ModelUnavailableError, match="not found"):
            await client.chat_json(MSGS, schema=LABEL_SCHEMA)


@pytest.mark.parametrize("api", APIS)
async def test_server_error_is_unavailable(api: Api) -> None:
    fake = FakeOllama(chat_status=500)
    async with make(fake, api) as client:
        with pytest.raises(ModelUnavailableError, match="500"):
            await client.chat_json(MSGS, schema=LABEL_SCHEMA)


def _raising(exc: Callable[[httpx.Request], Exception]) -> tuple[list[int], httpx.MockTransport]:
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        raise exc(request)

    return calls, httpx.MockTransport(handler)


@pytest.mark.parametrize("api", APIS)
async def test_unreachable_retries_once_then_unavailable(api: Api) -> None:
    calls, transport = _raising(lambda r: httpx.ConnectError("refused", request=r))
    async with OllamaClient(CFG, api=api, http=httpx.AsyncClient(transport=transport)) as client:
        with pytest.raises(ModelUnavailableError, match="cannot connect"):
            await client.chat_json(MSGS, schema=LABEL_SCHEMA)
    assert calls[0] == 2


@pytest.mark.parametrize("api", APIS)
async def test_timeout_is_unavailable_without_retry(api: Api) -> None:
    calls, transport = _raising(lambda r: httpx.ReadTimeout("slow", request=r))
    async with OllamaClient(CFG, api=api, http=httpx.AsyncClient(transport=transport)) as client:
        with pytest.raises(ModelUnavailableError, match="timed out"):
            await client.chat_json(MSGS, schema=LABEL_SCHEMA)
    assert calls[0] == 1


@pytest.mark.parametrize("api", APIS)
async def test_connect_error_then_success(api: Api) -> None:
    fake = FakeOllama([Turn('{"label": "chat"}')])
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            raise httpx.ConnectError("restarting", request=request)
        return fake.handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with OllamaClient(CFG, api=api, http=http) as client:
        reply = await client.chat_json(MSGS, schema=LABEL_SCHEMA)
    assert reply.json == {"label": "chat"}


# --- chat_tools ------------------------------------------------------------------------


@pytest.mark.parametrize("api", APIS)
async def test_chat_tools_parses_validates_and_records_drops(api: Api) -> None:
    fake = FakeOllama(
        [
            Turn(
                "",
                tool_calls=[
                    ("weather", {"city": "Oakland"}),
                    ("send_money", {"to": "x"}),
                    ("weather", {"city": 3}),
                    ("current_time", "{}"),
                ],
            )
        ]
    )
    spans = RecordingSpans()
    async with make(fake, api, spans) as client:
        reply = await client.chat_tools(MSGS, tools=[_weather(), _clock()])
    assert [(c.name, c.args) for c in reply.tool_calls] == [
        ("weather", {"city": "Oakland"}),
        ("current_time", {}),
    ]
    assert reply.json is None and reply.text is None
    attrs = spans.spans[0][1]
    assert attrs["kind"] == "tools"
    assert attrs["tool_calls"] == ["weather", "current_time"]
    assert [d["name"] for d in attrs["dropped_tool_calls"]] == ["send_money", "weather"]


@pytest.mark.parametrize("api", APIS)
async def test_chat_tools_sends_sorted_canonical_tools(api: Api) -> None:
    fake = FakeOllama([Turn("no tool needed"), Turn("no tool needed")])
    async with make(fake, api) as client:
        r1 = await client.chat_tools(MSGS, tools=[_weather(), _clock()])
        await client.chat_tools(MSGS, tools=[_clock(), _weather()])
    assert r1.text == "no tool needed" and r1.tool_calls == []
    path = "/api/chat" if api == "ollama" else "/v1/chat/completions"
    reqs = [r for r in fake.requests if r.url.path == path]
    first, second = (json.loads(r.content) for r in reqs)
    assert [t["function"]["name"] for t in first["tools"]] == ["current_time", "weather"]
    assert first["tools"] == second["tools"]
    tools_bytes = [r.content[r.content.index(b'"tools"') :] for r in reqs]
    assert tools_bytes[0] == tools_bytes[1]
    assert first["tools"] == json.loads(tools_prefix([_weather(), _clock()]))


# --- probe -----------------------------------------------------------------------------


@pytest.mark.parametrize("api", APIS)
async def test_probe_ok(api: Api) -> None:
    fake = FakeOllama([Turn("{")])
    async with make(fake, api) as client:
        result = await client.probe()
    assert result.ok and result.model_present and result.ctx_ok and result.thinking_off_ok
    assert result.latency_ms is not None
    assert result.detail.startswith(f"ok ({api} adapter)")


async def test_probe_probe_call_is_one_token_with_thinking_off() -> None:
    fake = FakeOllama([Turn("{")])
    async with make(fake, "ollama") as client:
        await client.probe()
    (body,) = fake.bodies("/api/chat")
    assert body["think"] is False and body["options"]["num_predict"] == 1 and "format" in body


@pytest.mark.parametrize(
    ("turn", "api"),
    [
        (Turn("", thinking="Okay"), "ollama"),
        (Turn(None, thinking="Okay"), "openai"),
        (Turn("<think>"), "openai"),
        (Turn(""), "ollama"),
    ],
)
async def test_probe_detects_thinking_on(turn: Turn, api: Api) -> None:
    fake = FakeOllama([turn])
    async with make(fake, api) as client:
        result = await client.probe()
    assert not result.ok and not result.thinking_off_ok
    assert result.model_present and result.ctx_ok
    assert "thinking is not off" in result.detail
    if api == "openai":
        assert "model.api: ollama" in result.detail


async def test_probe_model_missing() -> None:
    fake = FakeOllama(models=["llama3:8b"])
    async with make(fake) as client:
        result = await client.probe()
    assert not result.ok and not result.model_present
    assert "ollama pull qwen3:30b-a3b" in result.detail
    assert not fake.bodies("/api/chat")  # no chat call without the model


async def test_probe_context_too_small() -> None:
    fake = FakeOllama([Turn("{")], context_length=8192)
    async with make(fake) as client:
        result = await client.probe()
    assert not result.ok and not result.ctx_ok and result.thinking_off_ok
    assert "8192" in result.detail


async def test_probe_modelfile_num_ctx_too_small() -> None:
    fake = FakeOllama([Turn("{")], parameters="temperature 0.6\nnum_ctx 4096\n")
    async with make(fake) as client:
        result = await client.probe()
    assert not result.ctx_ok and "num_ctx 4096" in result.detail


async def test_probe_reports_cold_model() -> None:
    fake = FakeOllama([Turn("{")], loaded=False)
    async with make(fake) as client:
        result = await client.probe()
    assert result.ok and "cold start" in result.detail


async def test_probe_unreachable() -> None:
    _, transport = _raising(lambda r: httpx.ConnectError("refused", request=r))
    async with OllamaClient(CFG, http=httpx.AsyncClient(transport=transport)) as client:
        result = await client.probe()
    assert not result.ok and "unreachable" in result.detail


# --- bench -----------------------------------------------------------------------------


async def test_bench_warms_up_and_reports_percentiles() -> None:
    turns = [Turn('{"label": "chat"}') for _ in range(4)]
    turns += [Turn("", tool_calls=[("weather", {"city": "Oakland"})]) for _ in range(3)]
    turns += [Turn("I'd rather not")]  # one tool run fails: no tool call
    fake = FakeOllama(turns)
    ticks = iter(float(i) for i in range(100))
    async with make(fake) as client:
        series = await run_bench(client, n=3, clock=lambda: next(ticks))
    label, tool = (s.summary() for s in series)
    assert label == {"kind": "label", "n": 3, "failures": 0, "p50_ms": 1000, "p95_ms": 1000}
    assert tool["n"] == 2 and tool["failures"] == 1
    assert not fake.turns  # 1 warmup + 3 timed for each kind


# --- CLI -------------------------------------------------------------------------------


@pytest.fixture
def cli_fake(monkeypatch: pytest.MonkeyPatch) -> FakeOllama:
    """Route every OllamaClient the CLI builds to an in-process fake."""
    fake = FakeOllama()
    real_init = OllamaClient.__init__

    def init(self: OllamaClient, cfg: ModelConfig, **kw: Any) -> None:
        kw["http"] = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
        real_init(self, cfg, **kw)

    monkeypatch.setattr(OllamaClient, "__init__", init)
    return fake


def test_cli_probe_ok_writes_kv(cli_fake: FakeOllama) -> None:
    settings = load_settings()
    open_db(settings.db_path).close()
    cli_fake.turns = [Turn("{")]
    result = CliRunner().invoke(app, ["model", "probe"])
    assert result.exit_code == 0, result.output
    assert "thinking    off" in result.output
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    stored = kv_get(conn, "model.probe")
    assert stored["ok"] is True and stored["model"] == "qwen3:30b-a3b" and stored["checked_at"]


def test_cli_probe_fails_without_creating_store(cli_fake: FakeOllama) -> None:
    cli_fake.turns = [Turn("", thinking="hmm")]
    result = CliRunner().invoke(app, ["model", "probe", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["thinking_off_ok"] is False and data["model_present"] is True
    assert not load_settings().db_path.exists()


def test_cli_bench_exits_1_when_probe_fails(cli_fake: FakeOllama) -> None:
    cli_fake.models = []
    result = CliRunner().invoke(app, ["model", "bench", "--n", "2"])
    assert result.exit_code == 1
    assert "present     NO" in result.output
    assert not cli_fake.bodies("/api/chat")


def test_cli_bench_prints_p50_p95(cli_fake: FakeOllama) -> None:
    cli_fake.turns = [Turn("{")] + [Turn('{"label": "chat"}')] * 3
    cli_fake.turns += [Turn("", tool_calls=[("weather", {"city": "Oakland"})])] * 3
    result = CliRunner().invoke(app, ["model", "bench", "--n", "2"])
    assert result.exit_code == 0, result.output
    assert "label  p50" in result.output and "p95" in result.output
    assert "tool   p50" in result.output

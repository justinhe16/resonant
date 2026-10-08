"""Local LLM client: structured JSON and tool calls against Ollama, with thinking off.

- ``ModelClient`` is the Protocol the router and local runner depend on. ``OllamaClient``
  implements it over one of two adapters, picked by ``model.api``:
  - ``ollama`` (default): native ``/api/chat`` via httpx, with top-level ``think: false``,
    which is Ollama's documented switch for qwen3's reasoning mode.
  - ``openai``: the OpenAI-compatible ``/v1`` endpoint via the ``openai`` SDK, with
    ``think: false`` and ``reasoning_effort: "none"`` in ``extra_body``. Portable (a later
    switch to MLX is a config change), but older Ollama builds ignore both fields there.
  ``probe()`` checks that thinking really is off for whichever adapter is configured.
- ``<think>…</think>`` is always stripped before parsing, whatever the adapter says.
- ``chat_json`` validates the reply against the schema. Invalid or truncated JSON gets one
  retry with a "return only JSON" nudge, then ``ModelOutputError``.
- ``chat_tools`` sends tools sorted by name with canonical key order, so the same toolset
  is a byte-identical prompt prefix (KV-cache reuse). Calls to unknown tools, or with args
  that fail ``ToolSpec.args_schema``, are dropped and recorded on the span.
- Each call gets ``model.timeout_s``. A connection error is retried once, immediately.
  Anything else transport-side (timeout, HTTP error, missing model) raises
  ``ModelUnavailableError`` so callers can send a templated fallback.
- Spans carry the model, adapter, latency, token counts and dropped calls. Never prompt or
  reply text: message bodies are PII.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Self, TypedDict, cast

import httpx
import jsonschema
import openai
from openai.types.chat import (
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageParam,
)
from openai.types.shared_params import ResponseFormatJSONSchema

from resonant.config import ModelConfig
from resonant_sdk import ToolSpec

JSON_NUDGE = (
    "Your previous reply was not valid JSON for the required schema. "
    "Return only one JSON object that matches the schema, with no prose and no code fences."
)

# --- types -----------------------------------------------------------------------------


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]  # validated against the tool's ToolSpec.args_schema


@dataclass(frozen=True)
class ModelReply:
    text: str | None  # content with any <think> block removed
    tool_calls: list[ToolCall]
    json: dict[str, Any] | None  # set by chat_json, schema-valid
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    model_present: bool
    ctx_ok: bool
    thinking_off_ok: bool
    latency_ms: int | None  # the 1-token call, including a cold load if there was one
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "model_present": self.model_present,
            "ctx_ok": self.ctx_ok,
            "thinking_off_ok": self.thinking_off_ok,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
        }


class ModelError(Exception):
    """Base class for model client failures."""


class ModelUnavailableError(ModelError):
    """Ollama is unreachable, timed out, errored, or the model isn't pulled."""


class ModelOutputError(ModelError):
    """The model replied, but not with valid output (after the one retry)."""


class SpanLike(Protocol):
    def set(self, **attrs: Any) -> None: ...


class SpanFactory(Protocol):
    """``functools.partial(span, conn, trace_id=..., task_id=...)`` fits this."""

    def __call__(self, name: str, **attrs: Any) -> AbstractContextManager[SpanLike]: ...


class ModelClient(Protocol):
    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        max_tokens: int = 512,
        temperature: float = 0.0,
        spans: SpanFactory | None = None,
    ) -> ModelReply: ...

    async def chat_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        max_tokens: int = 512,
        spans: SpanFactory | None = None,
    ) -> ModelReply: ...

    async def probe(self) -> ProbeResult: ...


# Probe: a tiny JSON call. With thinking on, the first token would be reasoning, not "{".
_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}
_PROBE_MESSAGES: list[ChatMessage] = [
    {"role": "user", "content": 'Reply with the JSON object {"ok": true}.'}
]
_PROBE_MAX_TOKENS = 1


class _NullSpan:
    def set(self, **attrs: Any) -> None:
        pass


@contextmanager
def _null_span_cm() -> Generator[SpanLike]:
    yield _NullSpan()


def null_spans(name: str, **attrs: Any) -> AbstractContextManager[SpanLike]:
    """A SpanFactory that records nothing (CLI, tests)."""
    return _null_span_cm()


# --- pure helpers ----------------------------------------------------------------------

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)  # unclosed (truncated)
_FENCE = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\Z", re.DOTALL)


def strip_think(text: str) -> str:
    """Remove reasoning blocks: closed ones, an unclosed trailing one, or a bare close tag."""
    text = _THINK_BLOCK.sub("", text)
    text = _THINK_OPEN.sub("", text)
    if (i := text.lower().rfind("</think>")) != -1:
        text = text[i + len("</think>") :]
    return text.strip()


def has_think(text: str | None) -> bool:
    return text is not None and "<think>" in text.lower()


def ollama_root(base_url: str) -> str:
    """``http://127.0.0.1:11434/v1`` -> ``http://127.0.0.1:11434`` (native API root)."""
    root = base_url.rstrip("/")
    return root.removesuffix("/v1")


def _canonical(obj: dict[str, Any]) -> dict[str, Any]:
    # Recursively sorted keys: equal schemas serialize to identical bytes.
    return cast(dict[str, Any], json.loads(json.dumps(obj, sort_keys=True)))


def openai_tools(tools: Sequence[ToolSpec]) -> list[ChatCompletionFunctionToolParam]:
    """ToolSpecs in OpenAI ``tools`` format, sorted by name, keys in canonical order."""
    names = [t.name for t in tools]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate tool names: {sorted(names)}")
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": _canonical(t.args_schema),
            },
        }
        for t in sorted(tools, key=lambda t: t.name)
    ]


def tools_prefix(tools: Sequence[ToolSpec]) -> str:
    """The serialized tools block; byte-identical for the same toolset in any order.

    Ollama renders tools into the system part of the prompt via the chat template, so a
    stable tools block plus a stable system message gives a reusable KV-cache prefix.
    """
    return json.dumps(openai_tools(tools), separators=(",", ":"))


def parse_json_reply(text: str | None, schema: dict[str, Any]) -> dict[str, Any] | None:
    """Parse and validate a JSON object reply. None if it's not valid for ``schema``."""
    if not text:
        return None
    body = strip_think(text)
    if m := _FENCE.match(body):
        body = m.group(1)
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    obj = cast(dict[str, Any], data)
    try:
        jsonschema.validate(obj, schema)
    except jsonschema.ValidationError:
        return None
    return obj


@dataclass(frozen=True)
class RawToolCall:
    name: str
    arguments: object  # a dict, or a JSON string (OpenAI style, and some Ollama builds)


def validate_tool_calls(
    raw: Sequence[RawToolCall], tools: Sequence[ToolSpec]
) -> tuple[list[ToolCall], list[dict[str, str]]]:
    """Keep calls to known tools with schema-valid args; report the rest (no arg values)."""
    by_name = {t.name: t for t in tools}
    kept: list[ToolCall] = []
    dropped: list[dict[str, str]] = []
    for call in raw:
        spec = by_name.get(call.name)
        if spec is None:
            dropped.append({"name": call.name, "reason": "unknown_tool"})
            continue
        args: object = call.arguments
        if args is None or args == "":
            args = {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                dropped.append({"name": call.name, "reason": "args_not_json"})
                continue
        if not isinstance(args, dict):
            dropped.append({"name": call.name, "reason": "args_not_object"})
            continue
        obj = cast(dict[str, Any], args)
        try:
            jsonschema.validate(obj, spec.args_schema)
        except jsonschema.ValidationError as e:
            dropped.append({"name": call.name, "reason": f"invalid_args: {e.validator}"})
            continue
        kept.append(ToolCall(name=call.name, args=obj))
    return kept, dropped


# --- adapters --------------------------------------------------------------------------


@dataclass
class RawReply:
    content: str | None
    thinking: str | None  # a separate reasoning field, if the server returned one
    tool_calls: list[RawToolCall] = field(default_factory=list[RawToolCall])
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    truncated: bool = False  # stopped on the token limit


class _ConnectError(Exception):
    """Transport-level connection failure: eligible for the one immediate retry."""


class Adapter(Protocol):
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        schema: dict[str, Any] | None = None,
        tools: list[ChatCompletionFunctionToolParam] | None = None,
    ) -> RawReply: ...


class NativeAdapter:
    """Ollama's native ``/api/chat``. ``think: false`` is a top-level request field."""

    def __init__(self, cfg: ModelConfig, http: httpx.AsyncClient) -> None:
        self._cfg = cfg
        self._http = http
        self._url = f"{ollama_root(cfg.base_url)}/api/chat"

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        schema: dict[str, Any] | None = None,
        tools: list[ChatCompletionFunctionToolParam] | None = None,
    ) -> RawReply:
        body: dict[str, Any] = {
            "model": self._cfg.name,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if schema is not None:
            body["format"] = schema
        if tools:
            body["tools"] = tools
        try:
            resp = await self._http.post(self._url, json=body, timeout=self._cfg.timeout_s)
        except httpx.ConnectError as e:
            raise _ConnectError(str(e)) from e
        except httpx.TimeoutException as e:
            raise ModelUnavailableError(f"timed out after {self._cfg.timeout_s}s") from e
        except httpx.HTTPError as e:
            raise ModelUnavailableError(f"transport error: {type(e).__name__}") from e
        if resp.status_code == 404:
            raise ModelUnavailableError(f"model {self._cfg.name!r} not found (ollama pull it)")
        if resp.status_code >= 400:
            raise ModelUnavailableError(f"ollama returned HTTP {resp.status_code}")
        try:
            data = cast(dict[str, Any], resp.json())
        except ValueError as e:
            raise ModelUnavailableError("ollama returned a non-JSON response") from e
        msg = cast(dict[str, Any], data.get("message") or {})
        raw_calls: list[RawToolCall] = []
        for tc in cast(list[dict[str, Any]], msg.get("tool_calls") or []):
            fn = cast(dict[str, Any], tc.get("function") or {})
            raw_calls.append(RawToolCall(str(fn.get("name", "")), fn.get("arguments")))
        return RawReply(
            content=cast(str | None, msg.get("content")),
            thinking=cast(str | None, msg.get("thinking")),
            tool_calls=raw_calls,
            prompt_tokens=cast(int | None, data.get("prompt_eval_count")),
            completion_tokens=cast(int | None, data.get("eval_count")),
            truncated=data.get("done_reason") == "length",
        )


class OpenAIAdapter:
    """The OpenAI-compatible ``/v1`` endpoint via the official SDK (retries handled above)."""

    def __init__(self, cfg: ModelConfig, http: httpx.AsyncClient) -> None:
        self._cfg = cfg
        self._client = openai.AsyncOpenAI(
            base_url=cfg.base_url,
            api_key="ollama",  # required by the SDK, ignored by Ollama; not a secret
            max_retries=0,
            timeout=cfg.timeout_s,
            # The SDK types this as httpx2.AsyncClient but accepts a legacy httpx client at
            # runtime (openai._httpx2.is_legacy_httpx_async_client); sharing ours keeps one
            # connection pool and lets tests inject httpx.MockTransport.
            http_client=http,  # pyright: ignore[reportArgumentType]
        )

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        schema: dict[str, Any] | None = None,
        tools: list[ChatCompletionFunctionToolParam] | None = None,
    ) -> RawReply:
        response_format: ResponseFormatJSONSchema | openai.Omit = openai.omit
        if schema is not None:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": schema, "strict": True},
            }
        try:
            resp = await self._client.chat.completions.create(
                model=self._cfg.name,
                messages=cast(list[ChatCompletionMessageParam], messages),
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
                tools=tools if tools else openai.omit,
                extra_body={"think": False, "reasoning_effort": "none"},
            )
        except openai.APITimeoutError as e:
            raise ModelUnavailableError(f"timed out after {self._cfg.timeout_s}s") from e
        except openai.APIConnectionError as e:
            raise _ConnectError(str(e)) from e
        except openai.NotFoundError as e:
            raise ModelUnavailableError(
                f"model {self._cfg.name!r} not found (ollama pull it)"
            ) from e
        except openai.APIStatusError as e:
            raise ModelUnavailableError(f"ollama returned HTTP {e.status_code}") from e
        except openai.OpenAIError as e:
            raise ModelUnavailableError(f"client error: {type(e).__name__}") from e
        if not resp.choices:
            return RawReply(content=None, thinking=None)
        choice = resp.choices[0]
        msg = choice.message
        extra = msg.model_extra or {}
        thinking = extra.get("reasoning") or extra.get("reasoning_content")
        raw_calls = [
            RawToolCall(tc.function.name, tc.function.arguments)
            for tc in msg.tool_calls or []
            if tc.type == "function"
        ]
        usage = resp.usage
        return RawReply(
            content=msg.content,
            thinking=thinking if isinstance(thinking, str) else None,
            tool_calls=raw_calls,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            truncated=choice.finish_reason == "length",
        )


# --- client ----------------------------------------------------------------------------


class OllamaClient:
    """``ModelClient`` over Ollama. Use as an async context manager, or call ``aclose()``."""

    def __init__(
        self,
        cfg: ModelConfig,
        *,
        api: Literal["openai", "ollama"] | None = None,
        http: httpx.AsyncClient | None = None,
        spans: SpanFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.api: Literal["openai", "ollama"] = api or cfg.api
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=cfg.timeout_s)
        self._root = ollama_root(cfg.base_url)
        self._spans: SpanFactory = spans or null_spans
        self._clock = clock
        self._adapter: Adapter = (
            OpenAIAdapter(cfg, self._http)
            if self.api == "openai"
            else NativeAdapter(cfg, self._http)
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        schema: dict[str, Any] | None = None,
        tools: list[ChatCompletionFunctionToolParam] | None = None,
    ) -> RawReply:
        async def once() -> RawReply:
            return await self._adapter.complete(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                schema=schema,
                tools=tools,
            )

        try:
            return await once()
        except _ConnectError:
            pass  # one immediate retry: Ollama may be restarting under launchd
        try:
            return await once()
        except _ConnectError as e:
            raise ModelUnavailableError(f"cannot connect to {self._root}: {e}") from e

    def _ms(self, start: float) -> int:
        return round((self._clock() - start) * 1000)

    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        max_tokens: int = 512,
        temperature: float = 0.0,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        with (spans or self._spans)("llm.call", kind="json", **self._attrs()) as s:
            start = self._clock()
            convo: list[ChatMessage] = list(messages)
            prompt_tokens = completion_tokens = 0
            raw: RawReply | None = None
            for attempt in (1, 2):
                raw = await self._complete(
                    convo, max_tokens=max_tokens, temperature=temperature, schema=schema
                )
                prompt_tokens += raw.prompt_tokens or 0
                completion_tokens += raw.completion_tokens or 0
                obj = None if raw.truncated else parse_json_reply(raw.content, schema)
                s.set(
                    attempts=attempt,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    thinking_leak=bool(raw.thinking) or has_think(raw.content),
                )
                if obj is not None:
                    latency = self._ms(start)
                    s.set(latency_ms=latency)
                    return ModelReply(
                        text=strip_think(raw.content or ""),
                        tool_calls=[],
                        json=obj,
                        latency_ms=latency,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                convo = [*messages, {"role": "user", "content": JSON_NUDGE}]
            s.set(latency_ms=self._ms(start), truncated=bool(raw and raw.truncated))
            raise ModelOutputError("model did not return valid JSON for the schema (2 attempts)")

    async def chat_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        max_tokens: int = 512,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        with (spans or self._spans)("llm.call", kind="tools", **self._attrs()) as s:
            start = self._clock()
            raw = await self._complete(
                messages, max_tokens=max_tokens, temperature=0.0, tools=openai_tools(tools)
            )
            calls, dropped = validate_tool_calls(raw.tool_calls, tools)
            latency = self._ms(start)
            s.set(
                latency_ms=latency,
                prompt_tokens=raw.prompt_tokens,
                completion_tokens=raw.completion_tokens,
                tool_calls=[c.name for c in calls],
                dropped_tool_calls=dropped,
                thinking_leak=bool(raw.thinking) or has_think(raw.content),
            )
            text = strip_think(raw.content) if raw.content else None
            return ModelReply(
                text=text or None,
                tool_calls=calls,
                json=None,
                latency_ms=latency,
                prompt_tokens=raw.prompt_tokens,
                completion_tokens=raw.completion_tokens,
            )

    def _attrs(self) -> dict[str, Any]:
        return {"model": self.cfg.name, "api": self.api}

    # --- probe ---------------------------------------------------------------------------

    async def probe(self) -> ProbeResult:
        """Model pulled? Context big enough? Thinking really off? Never raises."""
        notes: list[str] = []
        model_present = await self._model_present(notes)
        if not model_present:
            return ProbeResult(False, False, False, False, None, "; ".join(notes))
        ctx_ok = await self._ctx_ok(notes)
        await self._note_cold(notes)
        thinking_off_ok, latency = await self._thinking_off(notes)
        ok = model_present and ctx_ok and thinking_off_ok
        if ok:
            notes.insert(0, f"ok ({self.api} adapter)")
        return ProbeResult(ok, model_present, ctx_ok, thinking_off_ok, latency, "; ".join(notes))

    async def _get_json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        resp = await self._http.request(
            method, f"{self._root}{path}", timeout=self.cfg.timeout_s, **kw
        )
        resp.raise_for_status()
        data: object = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object")
        return cast(dict[str, Any], data)

    async def _model_present(self, notes: list[str]) -> bool:
        try:
            data = await self._get_json("GET", "/api/tags")
        except (httpx.HTTPError, ValueError) as e:
            notes.append(f"ollama unreachable at {self._root} ({type(e).__name__})")
            return False
        if self._tagged() not in self._names(data):
            notes.append(f"model {self.cfg.name!r} not pulled (run: ollama pull {self.cfg.name})")
            return False
        return True

    def _tagged(self) -> str:
        return self.cfg.name if ":" in self.cfg.name else f"{self.cfg.name}:latest"

    @staticmethod
    def _names(data: dict[str, Any]) -> set[str]:
        out: set[str] = set()
        for m in cast(list[dict[str, Any]], data.get("models") or []):
            for key in ("name", "model"):
                if isinstance(v := m.get(key), str):
                    out.add(v)
        return out

    async def _ctx_ok(self, notes: list[str]) -> bool:
        need = self.cfg.context_tokens
        try:
            data = await self._get_json("POST", "/api/show", json={"model": self.cfg.name})
        except (httpx.HTTPError, ValueError) as e:
            notes.append(f"/api/show failed ({type(e).__name__})")
            return False
        info = cast(dict[str, Any], data.get("model_info") or {})
        max_ctx = next(
            (v for k, v in info.items() if k.endswith(".context_length") and isinstance(v, int)),
            None,
        )
        if max_ctx is None:
            notes.append("model context length unknown")
            return False
        if max_ctx < need:
            notes.append(f"model context {max_ctx} < model.context_tokens {need}")
            return False
        # A Modelfile `num_ctx` overrides OLLAMA_CONTEXT_LENGTH for this model.
        params = str(data.get("parameters") or "")
        m = re.search(r"^\s*num_ctx\s+(\d+)", params, re.MULTILINE)
        if m and int(m.group(1)) < need:
            notes.append(f"Modelfile num_ctx {m.group(1)} < model.context_tokens {need}")
            return False
        return True

    async def _note_cold(self, notes: list[str]) -> None:
        try:
            data = await self._get_json("GET", "/api/ps")
        except (httpx.HTTPError, ValueError):
            return  # informational only
        loaded = [
            m
            for m in cast(list[dict[str, Any]], data.get("models") or [])
            if self._tagged() in (m.get("name"), m.get("model"))
        ]
        if not loaded:
            notes.append("model was not loaded (cold start; first call includes the load)")
            return
        ctx = loaded[0].get("context_length")
        if isinstance(ctx, int) and ctx < self.cfg.context_tokens:
            notes.append(
                f"loaded with context {ctx} < model.context_tokens (check OLLAMA_CONTEXT_LENGTH)"
            )

    async def _thinking_off(self, notes: list[str]) -> tuple[bool, int | None]:
        start = self._clock()
        try:
            raw = await self._complete(
                _PROBE_MESSAGES,
                max_tokens=_PROBE_MAX_TOKENS,
                temperature=0.0,
                schema=_PROBE_SCHEMA,
            )
        except ModelError as e:
            notes.append(f"probe call failed: {e}")
            return False, None
        latency = self._ms(start)
        content = raw.content or ""
        leaked = bool((raw.thinking or "").strip()) or has_think(content)
        if leaked or not content.strip():
            hint = " (try model.api: ollama)" if self.api == "openai" else ""
            notes.append(f"thinking is not off with the {self.api} adapter{hint}")
            return False, latency
        return True, latency

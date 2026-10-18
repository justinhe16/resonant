"""MCP servers as extension tools (stdio transport; docs/dashboard-plan.md D8, MCP-1).

An extension's manifest may declare ``mcp.servers``: local servers spoken to over stdio, each
with the list of tools the extension exposes. Those tools are ordinary extension tools: the
gate, approvals, secrets, audit and kill switch treat them like CLI tools, and the brain sees
them like any other extension tool.

The client is a small JSON-RPC 2.0 client over newline-delimited stdio, not the ``mcp`` SDK.
Resonant needs only ``initialize``, ``tools/list`` (with pagination), ``tools/call``,
cancellation, ``ping`` and the ``tools/list_changed`` notification, and it must own the
process: the scratch env, secrets on fd 4 (never the env), the process group and tracked tree
the kill switch ends, caps on every line, and stderr that is never logged raw. The SDK's
stdio transport spawns the process itself (inheriting a default env and stderr) and brings
anyio, httpx, starlette and uvicorn into the daemon, so it would be fought on exactly those
points.

Design (carried from ``followups.md``):

- **Listed tools only.** A server tool the manifest doesn't list is never registered or
  called. The model sees the manifest's description, never the server's; the server's input
  schema is stripped to an allowlist of validation keywords (``description``, ``title``,
  ``default``, ``examples`` and anything unknown are dropped) and its annotations are ignored.
  A ``pattern`` that is long, unprintable or not a Python regex (ECMA-262 syntax ``re``
  refuses) is dropped too: not shown, not enforced locally; the server validates (LN-1).
  The result is only ever looser (LN-2): a ``oneOf`` over a loosened branch becomes an
  ``anyOf`` and a ``not`` over a loosened subschema is removed, through ``$ref`` too.
- **Pins.** Each tool is pinned as ``sha256(name, stripped input schema, stripped output
  schema)`` (pin version 2, GT-1): only what the model sees and what arguments are checked
  against. The server's description and annotations never reach the model, so a reworded
  description no longer disables a tool. ``ext add`` starts the server once (no secrets, a
  scratch home) to read its tools, shows the pins as escalations and stores them with the
  stripped input schemas and ``"v": 2`` (``ExtensionRecord.mcp_pins``). Every server start
  and every ``notifications/tools/list_changed`` re-lists and compares; a missing or changed
  tool is disabled for the life of the process and the owner is alerted once. Calls wait
  while a re-list is in flight.
- **Pin migration.** A version-1 pin (no ``"v"``; it also hashed the description and
  annotations) is never compared as a hash: if its stored stripped input schema equals the
  live one, the tool is accepted and re-pinned at version 2 through ``on_migrate`` (the host
  saves it and audits ``ext.mcp_pin_migrated`` once). The old pin never stored an output
  schema, so the live one is accepted then. A different input schema is a change, as ever.
- **Process.** ``spawn_stdio``: the extension's scratch env, secrets on fd 4 (the union of
  the server's tools' declared secrets), a new session. The process is tracked by the host's
  ``ProcessTracker``, so ``/kill`` ends it (and refuses restarts while held); stop closes
  stdin and then always ends the whole group and tracked tree, because MCP servers often
  leave children behind on a graceful exit.
- **Caps.** One stdout line (one message) is at most ``message_cap`` bytes, else the server
  is ended; stderr keeps a raw in-memory tail that only ever leaves redacted (the redacting
  sink); calls time out at the tool's ``timeout_s`` with a ``notifications/cancelled``.
- **Outcomes.** A JSON-RPC error or a server that can't start is definite (``McpError``, a
  ``ToolFailed``). ``isError`` results, timeouts and kills are not ``ToolFailed``: the
  executor treats them as failed for reads and ambiguous for writes. Texts are code-built;
  server text reaches the model only inside a successful result, as untrusted data.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import jsonschema

from resonant import __version__
from resonant.executor import CallContext, ContextHandler, ToolFailed
from resonant.extensions.process import (
    STDERR_CAP,
    STDERR_TAIL,
    STDOUT_CAP,
    ExtFailed,
    ExtRequest,
    ProcessTracker,
    RedactFn,
    ensure_data_dir,
    scratch_env,
    spawn_stdio,
)
from resonant.redact import Redactor
from resonant.secrets import SecretError, SecretStore, env_name, values_of
from resonant_sdk import ToolSpec

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = frozenset({"2025-06-18", "2025-03-26", "2024-11-05"})
MESSAGE_CAP = STDOUT_CAP  # one JSON-RPC message (one stdout line)
START_TIMEOUT_S = 20.0
CLOSE_WAIT_S = 0.5  # after closing stdin, before ending the group
MAX_TOOLS = 500
MAX_PAGES = 20
SCHEMA_MAX_BYTES = 16 * 1024
SCHEMA_MAX_DEPTH = 32
ARGS_MAX_BYTES = 64 * 1024
MAX_ANSWERS = 32  # server requests being answered at once

# JSON Schema keywords kept in an input schema. Everything else (description, title, default,
# examples, $comment, x-*) is dropped: free text the model would read as instructions.
# Adding a subschema keyword here (if/then/else, dependentSchemas, …) means revisiting
# ``_strip``'s loosening rules: a dropped pattern must never tighten the local check (LN-2).
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})
_SCHEMA_ONE = frozenset(
    {"items", "additionalProperties", "not", "contains", "propertyNames", "additionalItems"}
)
_SCHEMA_LISTS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_SCHEMA_VALUES = frozenset(
    {
        "type", "required", "enum", "const", "format", "pattern", "$ref", "nullable",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "minLength", "maxLength", "minItems", "maxItems", "uniqueItems",
        "minProperties", "maxProperties",
    }
)  # fmt: skip


class McpError(ToolFailed):
    """Definite: the call never reached the tool (no server, a protocol error, a JSON-RPC
    error reply, a disabled or unlisted tool). Code-built text only."""

    def __init__(self, reason: str, *, stderr_tail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.stderr_tail = stderr_tail


class McpAmbiguous(Exception):
    """The call was sent but cut short (``timeout``, ``killed``, ``server exited``,
    ``output too large``): it may have taken effect."""

    def __init__(self, reason: str, *, stderr_tail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.stderr_tail = stderr_tail


class McpToolError(Exception):
    """The tool ran and reported an error (``isError``). Not definite: a third-party server
    makes no promise that a failed call changed nothing."""


# --- schemas and pins ----------------------------------------------------------------------


def strip_schema(schema: object, depth: int = 0) -> Any:
    """``schema`` with only validation keywords kept (recursively). Property names are kept
    whatever they are called (a property named ``description`` stays). Raises ValueError.

    A string ``pattern`` that can't be checked is dropped (``_checkable_pattern``), and the
    result must only ever be **looser** than the server's schema: the server validates, and
    a local check stricter than it would refuse calls the server accepts. Every keyword kept
    is monotone in its subschemas except ``not`` and ``oneOf``, so (LN-2):

    - a ``oneOf`` with a loosened branch becomes an ``anyOf`` of the same branches (each
      branch only accepts more, and ``anyOf`` accepts whatever ``oneOf`` did);
    - a ``not`` over a loosened subschema is removed (no constraint accepts more).

    Looseness propagates to every enclosing schema, and through ``$ref``: a pointer to a
    root ``$defs``/``definitions`` entry is loosened when that entry is (found by iterating
    to a fixed point), and any other local pointer is treated as loosened whenever
    something was dropped. The rewrite is deterministic, so the pin is stable."""
    loose_defs: frozenset[str] = frozenset()
    any_dropped = False
    while True:
        ctx = _StripCtx(loose_defs, any_dropped)
        out, _ = _strip(schema, depth, ctx, root=True)
        found = frozenset(ctx.found_defs)
        if found == loose_defs and ctx.dropped == any_dropped:
            return out
        loose_defs, any_dropped = found | loose_defs, ctx.dropped or any_dropped


@dataclass
class _StripCtx:
    loose_defs: frozenset[str]  # ``#/$defs/<name>`` pointers known to be loosened
    any_dropped: bool  # from the previous pass: whether any pattern was dropped
    dropped: bool = False
    found_defs: set[str] = field(default_factory=set[str])


_DEF_POINTER = re.compile(r"^#/(?:\$defs|definitions)/[^/~]+$")


def _ref_loose(ref: object, ctx: _StripCtx) -> bool:
    if not isinstance(ref, str):
        return False
    if _DEF_POINTER.match(ref):
        return ref in ctx.loose_defs
    return ctx.any_dropped  # "#", "#/properties/…", nested $defs: assume the worst


def _strip(schema: object, depth: int, ctx: _StripCtx, root: bool = False) -> tuple[Any, bool]:
    """``(stripped, loosened)``: loosened when it accepts more than ``schema`` does."""
    if isinstance(schema, bool):
        return schema, False
    if not isinstance(schema, dict) or depth > SCHEMA_MAX_DEPTH:
        raise ValueError("schema must be an object (or a boolean) of bounded depth")
    out: dict[str, Any] = {}
    loose = one_loose = False
    for key, value in cast(dict[str, Any], schema).items():
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            subs: dict[str, Any] = {}
            for name, sub in cast(dict[str, Any], value).items():
                subs[name], sub_loose = _strip(sub, depth + 1, ctx)
                loose |= sub_loose
                if sub_loose and root and key in ("$defs", "definitions"):
                    ctx.found_defs.add(f"#/{key}/{name}")
            out[key] = subs
        elif key == "not":
            sub, sub_loose = _strip(value, depth + 1, ctx)
            if sub_loose:
                loose = True  # dropped: ``not`` over a looser schema would reject more
            else:
                out[key] = sub
        elif key in _SCHEMA_ONE:
            if isinstance(value, list):  # draft-4 tuple ``items``
                pairs = [_strip(v, depth + 1, ctx) for v in cast(list[Any], value)]
                out[key] = [p[0] for p in pairs]
                loose |= any(p[1] for p in pairs)
            else:
                out[key], sub_loose = _strip(value, depth + 1, ctx)
                loose |= sub_loose
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            pairs = [_strip(v, depth + 1, ctx) for v in cast(list[Any], value)]
            out[key] = [p[0] for p in pairs]
            loose |= any(p[1] for p in pairs)
            one_loose = one_loose or (key == "oneOf" and any(p[1] for p in pairs))
        elif key == "pattern" and isinstance(value, str) and not _checkable_pattern(value):
            ctx.dropped = loose = True  # not shown, not enforced here; the server validates
        elif key in _SCHEMA_VALUES:  # a non-string pattern lands here and is refused
            out[key] = _schema_value(key, value)
            if key == "$ref" and _ref_loose(value, ctx):
                loose = True
    if one_loose:  # ``anyOf`` of the loosened branches accepts whatever ``oneOf`` did
        branches = out.pop("oneOf")
        if "anyOf" in out:  # both present: keep both constraints, side by side
            out.setdefault("allOf", []).append({"anyOf": branches})
        else:
            out["anyOf"] = branches
    for name in cast(dict[str, Any], out.get("properties", {})):
        if not _short(name, NAME_MAX):
            raise ValueError("a property name is too long or not printable")
    for defs in ("$defs", "definitions"):
        for name in cast(dict[str, Any], out.get(defs, {})):
            if not _short(name, NAME_MAX):
                raise ValueError(f"a {defs} name is too long or not printable")
    for name in cast(dict[str, Any], out.get("patternProperties", {})):
        # Refused, not dropped: dropping an entry rejects more under additionalProperties.
        if not _checkable_pattern(name):
            raise ValueError("its schema has a bad 'patternProperties' key")
    return out, loose


_JSON_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
_NUMBERS = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"})
_COUNTS = frozenset(
    {"minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"}
)
NAME_MAX = 64
TEXT_MAX = 100  # enum/const strings, pattern, format: server text the model reads
ENUM_MAX = 100


def _short(v: object, limit: int = TEXT_MAX) -> bool:
    return isinstance(v, str) and len(v) <= limit and v.isprintable()


def _checkable_pattern(p: str) -> bool:
    """Whether a string ``pattern`` is kept: short and printable (the model reads it) and one
    Python's ``re`` compiles (``jsonschema`` checks arguments with ``re.search``).

    A JSON Schema pattern is an ECMA-262 regex. Generated schemas carry long ones (zod's
    UUID and ISO datetime patterns are well over ``TEXT_MAX``) and ECMA-only syntax
    (``\\p{L}``, ``(?<name>…)``, ``[^]``) that ``re`` refuses. Such a
    pattern is dropped rather than refused or translated: a dropped pattern only loosens the
    local check (the server validates its own schema; ``strip_schema`` refuses the cases
    rewrites ``not``/``oneOf`` so it never tightens), it carries no text to the model, and dropping
    is deterministic for the daemon's interpreter (uv pins Python 3.12), so the pin is
    stable. A non-string pattern is still refused."""
    if not _short(p):
        return False
    try:
        re.compile(p)
    except (re.error, OverflowError, RecursionError):  # the last two on pathological ones
        return False
    return True


def _scalar(v: object) -> bool:
    return v is None or isinstance(v, bool | int | float) or _short(v)


def _schema_value(key: str, v: object) -> Any:
    """A validation keyword's value, type-checked and length-capped, so a schema can't carry
    free text to the model through a value. Raises ValueError."""
    ok: bool
    if key == "type":
        items = cast(list[object], v) if isinstance(v, list) else [v]
        ok = bool(items) and all(isinstance(t, str) and t in _JSON_TYPES for t in items)
    elif key in _NUMBERS:
        ok = isinstance(v, int | float) and not isinstance(v, bool)
    elif key in _COUNTS:
        ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
    elif key in ("uniqueItems", "nullable"):
        ok = isinstance(v, bool)
    elif key == "required":
        ok = isinstance(v, list) and all(_short(n, NAME_MAX) for n in cast(list[object], v))
    elif key == "enum":
        vals = cast(list[object], v) if isinstance(v, list) else []
        ok = isinstance(v, list) and len(vals) <= ENUM_MAX and all(_scalar(e) for e in vals)
    elif key == "const":
        ok = _scalar(v)
    elif key == "$ref":
        ok = _short(v, 200) and cast(str, v).startswith("#/")
    else:  # format, pattern
        ok = _short(v)
    if not ok:
        raise ValueError(f"its schema has a bad {key!r} value")
    return cast(Any, v)


def input_schema(tool: Mapping[str, Any]) -> dict[str, Any]:
    """A server tool's stripped input schema, as the model will see it. Raises ValueError."""
    raw: object = tool.get("inputSchema")
    schema = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    if schema.get("type") != "object":
        raise ValueError("its inputSchema is not an object schema")
    stripped = cast(dict[str, Any], strip_schema(schema))
    stripped.setdefault("properties", {})
    if len(json.dumps(stripped)) > SCHEMA_MAX_BYTES:
        raise ValueError(f"its input schema is over {SCHEMA_MAX_BYTES} bytes")
    return stripped


PIN_VERSION = 2  # 1: sha256(name, description, inputSchema, outputSchema, annotations), raw


def _output_schema(tool: Mapping[str, Any]) -> Any:
    """A server tool's output schema, stripped like an input schema (descriptions and titles
    don't count). One that can't be stripped is pinned as reported."""
    raw: object = tool.get("outputSchema")
    if raw is None:
        return None
    try:
        return strip_schema(raw)
    except ValueError:  # e.g. a bad patternProperties key: hashed raw, never shown
        return raw


def tool_pin(tool: Mapping[str, Any]) -> str:
    """The version-2 pin: ``sha256(name, stripped input schema, stripped output schema)``
    over canonical JSON. The description and annotations are left out: the model never sees
    them. Raises ValueError when the input schema is unusable (see ``input_schema``)."""
    fields = [tool.get("name"), input_schema(tool), _output_schema(tool)]
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class McpToolPin:
    """What ``ext add`` approved for one MCP tool: its server, its pin, its stripped input
    schema and the pin's version. Stored in ``ExtensionRecord.mcp_pins`` as JSON; a
    version-1 entry has no ``"v"``."""

    server: str
    pin: str
    input_schema: dict[str, Any]
    version: int = PIN_VERSION

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "server": self.server,
            "pin": self.pin,
            "input_schema": self.input_schema,
        }
        if self.version != 1:
            out["v"] = self.version
        return out


def parse_pins(raw: Mapping[str, Any]) -> dict[str, McpToolPin]:
    """The stored ``mcp_pins`` back as ``{tool: McpToolPin}``. Raises ValueError."""
    out: dict[str, McpToolPin] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"mcp pin for {name} is malformed")
        e = cast(dict[str, Any], entry)
        server, pin, schema = e.get("server"), e.get("pin"), e.get("input_schema")
        version: object = e.get("v", 1)
        if not isinstance(server, str) or not isinstance(pin, str) or not isinstance(schema, dict):
            raise ValueError(f"mcp pin for {name} is malformed")
        if isinstance(version, bool) or version not in (1, PIN_VERSION):
            raise ValueError(f"mcp pin for {name} has an unknown version")
        out[name] = McpToolPin(server, pin, cast(dict[str, Any], schema), cast(int, version))
    return out


def pins_json(pins: Mapping[str, McpToolPin]) -> dict[str, Any]:
    return {name: p.to_json() for name, p in sorted(pins.items())}


def pins_digest(raw: Mapping[str, Any]) -> str:
    """A digest of the stored pins (``""`` when there are none), bound by the confirmation."""
    if not raw:
        return ""
    text = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Discovered:
    """One server's tools as ``ext add`` saw them."""

    pins: dict[str, McpToolPin]
    problems: list[str]
    ignored: list[str]  # server tool names the manifest doesn't list


def approve(server_id: str, listed: tuple[str, ...], tools: list[dict[str, Any]]) -> Discovered:
    """Pin the manifest-``listed`` tools out of a server's ``tools/list``."""
    by_name: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for t in tools:
        name = t.get("name")
        if not isinstance(name, str):
            continue
        if name in by_name:
            problems.append(f"mcp server {server_id} lists a tool twice")
            continue
        by_name[name] = t
    pins: dict[str, McpToolPin] = {}
    ignored = sorted(n for n in by_name if n not in listed)
    for name in listed:
        tool = by_name.get(name)
        if tool is None:
            problems.append(_missing(server_id, name, ignored))
            continue
        try:
            pins[name] = McpToolPin(server_id, tool_pin(tool), input_schema(tool))
        except ValueError as e:
            problems.append(f"mcp tool {name}: {e}")
    return Discovered(pins, problems, ignored)


_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")  # resonant_sdk's TOOL_NAME_PATTERN


def _missing(server_id: str, name: str, unlisted: list[str]) -> str:
    """Why ``ext add`` refuses a manifest-listed tool the server doesn't offer, with the
    server's closest unlisted names (only ones a manifest could list, so no server text
    beyond a tool name reaches the terminal). Install still refuses: the manifest is the
    owner's typed list of what each tool is, and a tool is only disabled after approval."""
    names = [n for n in unlisted if _TOOL_NAME.match(n)]
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
    hint = f" (renamed? it has {', '.join(close)})" if close else ""
    return (
        f"mcp server {server_id} has no tool {name}{hint}; fix or remove it in the manifest's tools"
    )


def server_argv(
    root: Path,
    command: str,
    args: tuple[str, ...],
    pinned: str | None,
    python: str = sys.executable,
) -> tuple[str, ...]:
    """The argv a server runs with: an in-dir ``.py`` under ``python -I -S``, anything else
    by its ``pinned`` absolute path (``argv0_paths["mcp:<id>"]``). Raises ValueError."""
    if "/" in command and not command.startswith("/"):
        base = root.resolve()
        script = (base / command).resolve()
        if not script.is_relative_to(base) or not script.is_file():
            raise ValueError(f"{command!r} is not a file in the extension dir")
        return (python, "-I", "-S", str(script), *args)
    if pinned is None or not os.path.isabs(pinned) or not os.path.isfile(pinned):
        raise ValueError(f"{command!r} has no pinned path that still exists")
    return (pinned, *args)


# --- the session -------------------------------------------------------------------------


class McpSessionExpired(McpError):
    """An HTTP server no longer knows the session (404 with ``Mcp-Session-Id``): the
    request was not processed. The runtime starts a new session and retries once."""


class RpcSession:
    """The JSON-RPC 2.0 side of one MCP session, whatever carries it: request ids and
    pending futures, the handshake, ``tools/list`` and ``tools/call``, cancellation, server
    requests (``ping`` only) and ``tools/list_changed``. A transport subclass provides
    ``start``, ``_send`` and ``_close``, and feeds every message it receives to
    ``_dispatch``. Not restartable: make a new one."""

    def __init__(
        self,
        *,
        label: str,
        redact: RedactFn,
        message_cap: int,
        on_list_changed: Callable[[], None] | None = None,
    ) -> None:
        self.label = label
        self.redact = redact
        self.message_cap = message_cap
        self.on_list_changed = on_list_changed
        self.protocol_version: str | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._side: set[asyncio.Task[Any]] = set()
        self._started = False
        self._ended: str | None = None
        self._close_task: asyncio.Task[None] | None = None

    @property
    def alive(self) -> bool:
        return self._started and self._ended is None

    @property
    def ended(self) -> str | None:
        """Why the session ended, or None while it runs."""
        return self._ended

    def stderr_tail(self) -> str:
        """A redacted tail of the server's stderr ("" when there is none to read)."""
        return ""

    async def list_tools(self, timeout_s: float = START_TIMEOUT_S) -> list[dict[str, Any]]:
        """Every tool the server lists, following ``nextCursor``. Raises ``McpError``."""
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            try:
                result = await self._request("tools/list", params, timeout_s)
            except McpAmbiguous as e:
                why = f"tools/list failed ({e.reason})"
                raise McpError(why, stderr_tail=e.stderr_tail) from None
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpError("tools/list returned no tool list")
            tools += [cast(dict[str, Any], t) for t in cast(list[Any], page) if isinstance(t, dict)]
            if len(tools) > MAX_TOOLS:
                raise McpError(f"server lists over {MAX_TOOLS} tools")
            nxt = result.get("nextCursor")
            if not isinstance(nxt, str) or not nxt:
                return tools
            cursor = nxt
        raise McpError(f"tools/list has over {MAX_PAGES} pages")

    async def call_tool(self, name: str, args: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        """``tools/call``. Returns ``{"text": …, "structured"?: {…}}``: text content joined,
        other content types named but omitted. Raises ``McpError`` (JSON-RPC error),
        ``McpToolError`` (``isError``) or ``McpAmbiguous``."""
        result = await self._request("tools/call", {"name": name, "arguments": args}, timeout_s)
        if result.get("isError") is True:
            raise McpToolError("the tool reported an error")
        parts: list[str] = []
        content = result.get("content")
        for item in cast(list[Any], content) if isinstance(content, list) else []:
            entry = cast(dict[str, Any], item) if isinstance(item, dict) else {}
            kind, text = entry.get("type"), entry.get("text")
            if kind == "text" and isinstance(text, str):
                parts.append(text)
            else:
                label = kind if isinstance(kind, str) and kind.isalnum() else "content"
                parts.append(f"[{label[:20]} omitted]")
        out: dict[str, Any] = {"text": "\n".join(parts)}
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            out["structured"] = structured
        return out

    async def close(self, reason: str = "closed") -> None:
        """End the session and fail anything pending. Idempotent."""
        if self._close_task is None:
            self._close_task = asyncio.ensure_future(self._close(reason))
        await asyncio.shield(self._close_task)

    # --- for transports --------------------------------------------------------------------

    async def _close(self, reason: str) -> None:
        raise NotImplementedError

    async def _send(self, msg: dict[str, Any]) -> None:
        """Send one message. Raises ``McpError`` when it can't be sent."""
        raise NotImplementedError

    def _kill_reason(self) -> str | None:
        """Why the kill switch ended this session, if it did."""
        return None

    async def _handshake(self, timeout_s: float) -> dict[str, Any]:
        """``initialize`` and ``notifications/initialized``. Raises ``McpError``."""
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "resonant", "version": __version__},
        }
        try:
            result = await self._request("initialize", params, timeout_s)
        except McpAmbiguous as e:
            why = f"server did not start ({e.reason})"
            raise McpError(why, stderr_tail=e.stderr_tail) from None
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version not in SUPPORTED_VERSIONS:
            raise McpError("server speaks an unsupported protocol version")
        caps = result.get("capabilities")
        if not isinstance(caps, dict) or "tools" not in caps:
            raise McpError("server offers no tools")
        self.protocol_version = version
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def _end(self, reason: str) -> None:
        """Mark the session over (once) and fail every pending request."""
        if self._ended is not None:
            return
        ended = self._kill_reason() or reason
        self._ended = ended
        tail = self.stderr_tail()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(McpAmbiguous(ended, stderr_tail=tail))
                fut.add_done_callback(_retrieve)
        self._pending.clear()
        if self._ended not in ("closed", "start failed"):
            log.warning(
                "mcp server ended",
                extra={"label": self.label, "reason": self._ended, "stderr_tail": tail},
            )

    async def _request(self, method: str, params: dict[str, Any], timeout_s: float) -> Any:
        if not self.alive:
            raise McpError("server is not running")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            try:
                await asyncio.wait_for(self._send(msg), timeout_s)
            except TimeoutError:  # the server stopped reading; part may have arrived
                self._spawn(self.close("stuck"))
                raise McpAmbiguous("timeout", stderr_tail=self.stderr_tail()) from None
            try:
                return await asyncio.wait_for(fut, timeout_s)
            except TimeoutError:
                await self._cancel(rid, "timeout")
                raise McpAmbiguous("timeout", stderr_tail=self.stderr_tail()) from None
            except asyncio.CancelledError:
                self._spawn(self._cancel(rid, "cancelled"))
                raise
        finally:
            self._pending.pop(rid, None)

    async def _cancel(self, rid: int, reason: str) -> None:
        with contextlib.suppress(McpError, TimeoutError):
            await asyncio.wait_for(
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": rid, "reason": reason},
                    }
                ),
                1.0,
            )

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._side.add(task)
        task.add_done_callback(self._side.discard)

    def _dispatch(self, line: bytes) -> None:
        try:
            msg: object = json.loads(line)
        except (ValueError, RecursionError):
            return  # not a message; servers must not write anything else, so ignore it
        if not isinstance(msg, dict):
            return
        m = cast(dict[str, Any], msg)
        method = m.get("method")
        if isinstance(method, str):
            if "id" in m:  # a request from the server: ping only; we offer no capabilities
                if len(self._side) < MAX_ANSWERS:  # a flood of pings is dropped, not queued
                    self._spawn(self._answer(m.get("id"), method))
            elif method == "notifications/tools/list_changed" and self.on_list_changed:
                self.on_list_changed()
            return
        rid = m.get("id")
        if not isinstance(rid, int) or isinstance(rid, bool):
            return
        fut = self._pending.get(rid)
        if fut is None or fut.done():
            return
        if "error" in m:
            err = m.get("error")
            code = cast(dict[str, Any], err).get("code") if isinstance(err, dict) else None
            label = code if isinstance(code, int) and not isinstance(code, bool) else "?"
            fut.set_exception(McpError(f"server error {label}"))
        elif isinstance(m.get("result"), dict):
            fut.set_result(cast(dict[str, Any], m["result"]))
        else:
            # Sent and maybe run: not definite.
            fut.set_exception(McpAmbiguous("malformed response", stderr_tail=self.stderr_tail()))

    async def _answer(self, rid: Any, method: str) -> None:
        if not isinstance(rid, str | int) or isinstance(rid, bool):
            return
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
        if method == "ping":
            reply["result"] = {}
        else:
            reply["error"] = {"code": -32601, "message": "method not supported"}
        with contextlib.suppress(McpError):
            await self._send(reply)


class McpSession(RpcSession):
    """One running stdio server and its JSON-RPC session. Not restartable: make a new one."""

    def __init__(
        self,
        req: ExtRequest,
        tracker: ProcessTracker,
        *,
        redact: RedactFn,
        python: str = sys.executable,
        message_cap: int = MESSAGE_CAP,
        stderr_cap: int = STDERR_CAP,
        on_list_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            label=req.label, redact=redact, message_cap=message_cap, on_list_changed=on_list_changed
        )
        self.req = req
        self.tracker = tracker
        self.python = python
        self.stderr_cap = stderr_cap
        self.proc: asyncio.subprocess.Process | None = None
        self._entry: Any = None  # process._Tracked
        self._tasks: list[asyncio.Task[Any]] = []
        self._stderr = bytearray()  # raw, in memory only; leaves through stderr_tail()

    def stderr_tail(self) -> str:
        """The last ``STDERR_TAIL`` chars of stderr, redacted. Nothing else of it leaves."""
        return self.redact(self._stderr.decode(errors="replace"))[-STDERR_TAIL:]

    async def start(self, timeout_s: float = START_TIMEOUT_S) -> dict[str, Any]:
        """Spawn the server and run the ``initialize`` handshake. Returns its result.
        Raises ``ExtFailed`` (paused, can't start) or ``McpError``; nothing is left running."""
        if self.tracker.paused:
            raise ExtFailed("paused")
        self.proc = await spawn_stdio(self.req, python=self.python, limit=self.message_cap)
        self._started = True
        self._entry = self.tracker.track(self.proc.pid, self.req.label)
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
            asyncio.create_task(self.tracker.sample_loop(self._entry)),
        ]
        try:
            if self.tracker.paused:
                raise ExtFailed("paused")
            return await self._handshake(timeout_s)
        except BaseException:
            await self.close("start failed")
            raise

    def _kill_reason(self) -> str | None:
        return self._entry.reason if self._entry is not None else None

    async def _close(self, reason: str) -> None:
        """Close stdin, give it a moment, then always end the whole group and tracked tree."""
        self._end(reason)
        proc, entry = self.proc, self._entry
        if proc is not None and proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc is not None and entry is not None:
            if proc.returncode is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(proc.wait()), CLOSE_WAIT_S)
            entry.reason = entry.reason or reason
            await self.tracker.terminate(entry)
            self.tracker.kill_now(entry)
            self.tracker.untrack(entry)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), self.tracker.grace_s + 1)
            # Close the pipes now, not when the transport is collected (maybe after the
            # event loop is gone): a killed tree may never have sent EOF.
            transport: object = getattr(proc, "_transport", None)
            if isinstance(transport, asyncio.BaseTransport):
                transport.close()
        for t in [*self._tasks, *self._side]:
            t.cancel()
        await asyncio.gather(*self._tasks, *self._side, return_exceptions=True)

    async def _send(self, msg: dict[str, Any]) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None or self._ended is not None:
            raise McpError("server is not running")
        line = json.dumps(msg, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        try:
            proc.stdin.write(line)
            await proc.stdin.drain()
        except (OSError, RuntimeError):
            raise McpError("server is not running") from None

    async def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        reader = self.proc.stdout
        reason = "server exited"
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError as e:
                    if e.partial.strip():
                        self._dispatch(e.partial)
                    break
                except asyncio.LimitOverrunError:
                    reason = "output too large"
                    break
                self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except Exception:
            reason = "read error"
        self._end(reason)
        if self._close_task is None:
            self._close_task = asyncio.ensure_future(self._close(reason))

    async def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while chunk := await self.proc.stderr.read(64 * 1024):
            self._stderr += chunk
            if len(self._stderr) > self.stderr_cap:
                del self._stderr[: len(self._stderr) - self.stderr_cap]


def _retrieve(fut: asyncio.Future[Any]) -> None:
    if not fut.cancelled():
        fut.exception()  # its waiter may be gone: don't warn "never retrieved"


# --- one server of an installed extension ----------------------------------------------

DriftFn = Callable[[str, str, str | None], None]  # (server id, tool, live pin or None)
MigrateFn = Callable[[str, str, str], None]  # (server id, tool, new version-2 pin)
INVALID_PIN = "invalid"  # the live tool's input schema can't be used: never a match


def disabled_text(ext: str, tool: str) -> str:
    return (
        f"{tool} is disabled: its MCP server changed it since you approved it; "
        f"resonant ext update {ext} to review"
    )


@dataclass(frozen=True)
class HttpTarget:
    """An MCP server over streamable HTTP: its fixed URL and the declared secret sent as its
    bearer token (None: no auth)."""

    url: str
    secret: str | None = None


@dataclass(eq=False)
class McpServerRuntime:
    """One MCP server of a loaded extension: started on first use (or by ``start``),
    restarted on the next call after it ends, verified against the approved pins on every
    start and every ``list_changed``."""

    ext: str
    server_id: str
    argv: tuple[str, ...]  # the pinned command and its literal args
    root: Path  # the read-only copy (cwd)
    data_dir: Path
    path_env: str
    bins: Mapping[str, str]
    secret_names: tuple[str, ...]  # the union of its tools' declared secrets
    approved: dict[str, str]  # tool -> version-2 pin (a migrated one is replaced in place)
    tracker: ProcessTracker
    secrets: Callable[[], SecretStore | None] = lambda: None
    on_drift: DriftFn | None = None
    # Version-1 pins: tool -> the stored stripped input schema. Matched by schema, not hash,
    # and migrated on the first match (``on_migrate``).
    legacy: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    on_migrate: MigrateFn | None = None
    python: str = sys.executable
    message_cap: int = MESSAGE_CAP
    start_timeout_s: float = START_TIMEOUT_S
    http: HttpTarget | None = None  # set: streamable HTTP, and argv/root/bins are unused
    disabled: dict[str, str] = field(default_factory=dict[str, str])  # tool -> why
    session: RpcSession | None = None
    _redactor: Redactor = field(default_factory=Redactor)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _verified: asyncio.Event = field(default_factory=asyncio.Event)
    _dirty: bool = False
    _recheck_task: asyncio.Task[None] | None = None
    _closed: bool = False

    @property
    def label(self) -> str:
        return f"{self.ext}/mcp:{self.server_id}"

    async def start(self) -> None:
        """Start and verify now (the daemon warms servers up so drift alerts come early)."""
        await self.ensure()

    async def ensure(self) -> RpcSession:
        async with self._lock:
            if self.session is not None and self.session.alive:
                return self.session
            if self.session is not None:
                await self.session.close()
                self.session = None
            if self.tracker.paused:
                raise ExtFailed("paused")
            if self._closed:
                raise McpError("server is shutting down")
            env_secrets: dict[str, str] = {}
            if self.secret_names:
                store = self.secrets()
                try:
                    if store is None:
                        raise SecretError(f"{self.ext}/{self.secret_names[0]}", "unavailable")
                    env_secrets = await store.env_for(self.ext, self.secret_names)
                except SecretError:
                    raise McpError(
                        "a secret this MCP server needs is missing or unreadable "
                        f"(resonant secret list {self.ext})"
                    ) from None
            redactor = Redactor(values_of(env_secrets))
            session = self._new_session(env_secrets, redactor)
            session.on_list_changed = lambda: self._list_changed(session)
            await session.start(self.start_timeout_s)
            try:
                tools = await session.list_tools(self.start_timeout_s)
            except BaseException:
                await session.close("start failed")
                raise
            if self._closed:
                await session.close()
                raise McpError("server is shutting down")
            self._check(tools)
            self._redactor = redactor
            self.session = session
            if self._recheck_task is None or self._recheck_task.done():
                self._verified.set()
            return session

    def _new_session(self, env_secrets: dict[str, str], redactor: Redactor) -> Any:
        """A not-yet-started session for this server's transport."""
        if self.http is not None:
            from resonant.extensions.mcp_http import McpHttpSession

            token = None
            if self.http.secret is not None:
                token = env_secrets.get(env_name(self.http.secret))
            return McpHttpSession(
                self.http.url,
                token=token,
                label=self.label,
                tracker=self.tracker,
                redact=redactor.text,
                message_cap=self.message_cap,
            )
        ensure_data_dir(self.data_dir)
        env = scratch_env(
            ext=self.ext,
            data_dir=self.data_dir,
            secrets=env_secrets,
            path=self.path_env,
            bins=self.bins,
        )
        req = ExtRequest(
            argv=self.argv,
            cwd=self.root,
            env=env,
            timeout_s=self.start_timeout_s,
            label=self.label,
        )
        return McpSession(
            req,
            self.tracker,
            redact=redactor.text,
            python=self.python,
            message_cap=self.message_cap,
        )

    async def call(self, tool: str, args: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        if tool not in self.approved:
            raise McpError(f"{tool} is not a listed tool of this server")
        if tool in self.disabled:
            raise McpError(disabled_text(self.ext, tool))
        for attempt in (1, 2):
            session = await self.ensure()
            try:
                await asyncio.wait_for(self._verified.wait(), timeout_s)
            except TimeoutError:
                raise McpError("the server's tools are being re-checked; try again") from None
            if tool in self.disabled:
                raise McpError(disabled_text(self.ext, tool))
            redactor = self._redactor
            try:
                result = await session.call_tool(tool, args, timeout_s)
            except McpSessionExpired:
                if attempt == 1:  # not processed: a new session (re-verified), then retry
                    continue
                raise
            except (McpError, McpAmbiguous) as e:
                e.stderr_tail = session.stderr_tail()
                raise
            return redactor.deep(result)
        raise McpError("session expired")  # unreachable: the loop returns or raises

    async def aclose(self) -> None:
        self._closed = True
        if self._recheck_task is not None:
            self._recheck_task.cancel()
            with contextlib.suppress(BaseException):
                await self._recheck_task
        if self.session is not None:
            await self.session.close()

    def _check(self, tools: list[dict[str, Any]]) -> None:
        live: dict[str, dict[str, Any] | None] = {}
        for t in tools:
            name = t.get("name")
            if isinstance(name, str):
                live[name] = None if name in live else t  # a duplicate is drift
        for tool, pin in list(self.approved.items()):
            if tool in self.disabled:
                continue
            entry = live.get(tool)
            got = _live_pin(entry)
            if tool in self.legacy:
                if entry is not None and got != INVALID_PIN and _same_input(entry, self.legacy):
                    self._migrate(tool, cast(str, got))
                    continue
            elif got == pin:
                continue
            self.disabled[tool] = "removed" if tool not in live else "changed"
            log.warning(
                "mcp tool disabled",
                extra={"label": self.label, "tool": tool, "why": self.disabled[tool]},
            )
            if self.on_drift is not None:
                try:
                    self.on_drift(self.server_id, tool, got)
                except Exception:
                    log.exception("mcp drift alert failed")

    def _migrate(self, tool: str, pin: str) -> None:
        """A version-1 pin whose input schema still matches: re-pin it at version 2."""
        self.approved[tool] = pin
        del self.legacy[tool]
        log.info("mcp pin migrated", extra={"label": self.label, "tool": tool})
        if self.on_migrate is not None:
            try:
                self.on_migrate(self.server_id, tool, pin)
            except Exception:
                log.exception("mcp pin migration failed to save")

    def _list_changed(self, session: RpcSession) -> None:
        if self.session is not None and session is not self.session:
            return  # an old session's notification
        self._verified.clear()
        self._dirty = True
        if self._recheck_task is None or self._recheck_task.done():
            self._recheck_task = asyncio.ensure_future(self._recheck(session))

    async def _recheck(self, session: RpcSession) -> None:
        try:
            if self.session is None:  # during ensure(): wait for its first list
                async with self._lock:
                    pass
            while self._dirty and session.alive:
                self._dirty = False
                try:
                    tools = await session.list_tools(self.start_timeout_s)
                except Exception:
                    await session.close("re-list failed")
                    return
                self._check(tools)
        finally:
            self._verified.set()


def _live_pin(tool: Mapping[str, Any] | None) -> str | None:
    """The live tool's version-2 pin, None when it's missing (or listed twice), and
    ``INVALID_PIN`` when its input schema can't be used."""
    if tool is None:
        return None
    try:
        return tool_pin(tool)
    except ValueError:
        return INVALID_PIN


def _same_input(tool: Mapping[str, Any], legacy: Mapping[str, dict[str, Any]]) -> bool:
    """Whether a live tool's stripped input schema is the one a version-1 pin stored."""
    try:
        return input_schema(tool) == legacy.get(cast(str, tool.get("name")))
    except ValueError:
        return False


@dataclass(frozen=True, eq=False)
class McpToolHandler(ContextHandler):
    """The executor's handler for one MCP tool: a ``tools/call`` on its server."""

    ext: str
    spec: ToolSpec
    runtime: McpServerRuntime

    def _validator(self) -> Any:
        # Local $refs only: jsonschema's referencing registry never fetches remote ones.
        return jsonschema.Draft202012Validator(self.spec.args_schema)

    async def __call__(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        undeclared = set(ctx.secrets) - {f"RESONANT_SECRET_{s.upper()}" for s in self.spec.secrets}
        if undeclared:  # the executor passes only declared ones; never widen
            raise ToolFailed("secrets not declared by this tool")
        try:
            size = len(json.dumps(args, allow_nan=False))
        except (TypeError, ValueError):
            raise ToolFailed("arguments are not JSON") from None
        if size > ARGS_MAX_BYTES:
            raise ToolFailed(f"arguments are over {ARGS_MAX_BYTES} bytes")
        try:
            errors = sorted(self._validator().iter_errors(args), key=lambda e: list(e.path))
        except Exception:  # e.g. a $ref that doesn't resolve: nothing was sent
            raise ToolFailed("the tool's schema can't be checked") from None
        if errors:  # the pinned schema is enforced here, not only shown to the model
            where = "/".join(str(p) for p in errors[0].path) or "arguments"
            raise ToolFailed(f"{where} doesn't match the tool's schema")
        return await self.runtime.call(self.spec.name, args, self.spec.timeout_s)


# --- install-time discovery ----------------------------------------------------------------


async def discover(
    req: ExtRequest, *, python: str = sys.executable, timeout_s: float = START_TIMEOUT_S
) -> list[dict[str, Any]]:
    """Start a server once (the caller passes no secrets), list its tools, end it."""
    tracker = ProcessTracker(sample_s=0.5, grace_s=1.0)
    session = McpSession(req, tracker, redact=lambda t: t, python=python)
    try:
        await session.start(timeout_s)
        return await session.list_tools(timeout_s)
    finally:
        await session.close()


__all__ = [
    "MESSAGE_CAP",
    "PIN_VERSION",
    "PROTOCOL_VERSION",
    "Discovered",
    "HttpTarget",
    "McpAmbiguous",
    "McpError",
    "McpServerRuntime",
    "McpSession",
    "McpSessionExpired",
    "McpToolError",
    "McpToolHandler",
    "McpToolPin",
    "RpcSession",
    "approve",
    "disabled_text",
    "discover",
    "input_schema",
    "parse_pins",
    "pins_digest",
    "pins_json",
    "server_argv",
    "strip_schema",
    "tool_pin",
]

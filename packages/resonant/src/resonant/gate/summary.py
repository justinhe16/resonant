"""Approval summaries, built by code from intent fields, and the unsafe-args check.

A summary is the one line an approver reads before replying, so nothing in it comes from a
model as prose. The tool name comes from the registry. Arg keys and values are untrusted:
each goes through ``clean_external``, is clipped, and any loss is marked (``(truncated)``,
``+N more args``). Masking code-like tokens is lossy on purpose: "vitamin d3" reads as
``vitamin ?``. The full args stay available to the owner via ``/approvals <code>``.

``unsafe_args`` finds strings an approver couldn't read faithfully anywhere (bidi
overrides, zero-width and other invisible format characters, control characters). The gate
denies such intents outright rather than sanitizing them, because the args that would run
would differ from what any display shows.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Any

from resonant.gate.codes import clean_external
from resonant.gate.types import Intent

MAX_ARGS = 3  # args shown; the rest become "+N more args"
MAX_KEY_LEN = 20
MAX_VALUE_LEN = 40

# Ordinary whitespace controls are readable once whitespace is collapsed for display.
_ALLOWED_CONTROLS = frozenset("\t\n\r")
# Cc control, Cf format (bidi, zero-width, BOM), Zl/Zp line and paragraph separators.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _is_unsafe(text: str) -> bool:
    return any(
        ch not in _ALLOWED_CONTROLS and unicodedata.category(ch) in _UNSAFE_CATEGORIES
        for ch in text
    )


def _walk(value: Any, path: str, found: list[str]) -> None:
    if isinstance(value, str):
        if _is_unsafe(value):
            found.append(path)
    elif isinstance(value, Mapping):
        for k, v in value.items():  # pyright: ignore[reportUnknownVariableType]
            key = str(k)  # pyright: ignore[reportUnknownArgumentType]
            if _is_unsafe(key):
                found.append(f"{path}.<key>")
            _walk(v, f"{path}.{key}", found)
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            _walk(v, f"{path}[{i}]", found)


def unsafe_args(args: Mapping[str, Any]) -> tuple[str, ...]:
    """JSON paths (``$.text``, ``$.items[2]``) of keys or strings holding unsafe characters.

    Empty means safe. Paths may echo arg keys, so they belong in audits, not in replies.
    """
    found: list[str] = []
    _walk(args, "$", found)
    return tuple(found)


def _clip(text: str, max_len: int) -> tuple[str, bool]:
    flat = clean_external(text)
    if len(flat) > max_len:
        return flat[:max_len].rstrip(), True
    return flat, False


def _value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        text, cut = _clip(repr(value), MAX_VALUE_LEN)
        return text + (" (truncated)" if cut else "")
    if isinstance(value, str):
        text, cut = _clip(value, MAX_VALUE_LEN)
        return f'"{text}"' + (" (truncated)" if cut else "")
    if isinstance(value, Mapping):
        return f"(object, {len(value)} keys)"  # pyright: ignore[reportUnknownArgumentType]
    if isinstance(value, list | tuple):
        return f"(list, {len(value)} items)"  # pyright: ignore[reportUnknownArgumentType]
    return f"({type(value).__name__})"


def describe_args(args: Mapping[str, Any]) -> str:
    """``key="value", key=3 +N more args``, in sorted key order. Empty for no args."""
    keys = sorted(args)
    parts: list[str] = []
    for key in keys[:MAX_ARGS]:
        name, _ = _clip(key, MAX_KEY_LEN)
        parts.append(f"{name or '?'}={_value(args[key])}")
    text = ", ".join(parts)
    if len(keys) > MAX_ARGS:
        text += f" +{len(keys) - MAX_ARGS} more args"
    return text


def summarize(intent: Intent, *, action: str | None = None) -> str:
    """The approval summary for ``intent``, e.g. ``add note text="buy milk"``.

    ``action`` is an optional code-built phrase (e.g. ``delete all 4 notes``) that replaces
    the default ``<extension>: <tool name>`` heading. It must come from code, never a model;
    it is cleaned like any other string. Args are always appended after it.
    """
    if action is None:
        head = intent.tool.replace("_", " ")
        if intent.extension is not None:
            head = f"{intent.extension}: {head}"
    else:
        head = action
    head = clean_external(head) or intent.tool
    args = describe_args(intent.args)
    return f"{head} {args}" if args else head

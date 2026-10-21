"""Approval summaries, built by code from intent fields, and the unsafe-args check.

A summary is the one line an approver reads before replying, so nothing in it comes from a
model as prose. The tool name comes from the registry. Arg keys and values are untrusted:
each goes through ``clean_external``, is clipped, and any loss is marked (``(truncated)``,
``+N more args``, ``(non-ASCII hidden)``). Masking code-like tokens is lossy on purpose:
"vitamin d3" reads as ``vitamin ?``. The full args stay available to the owner via
``/approvals <code>``.

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
from resonant_sdk.tool import parse_summary

MAX_ARGS = 3  # args shown; the rest become "+N more args"
MAX_KEY_LEN = 20
MAX_VALUE_LEN = 40

# Ordinary whitespace controls are readable once whitespace is collapsed for display.
_ALLOWED_CONTROLS = frozenset("\t\n\r")
# Cc control, Cf format (bidi, zero-width, BOM), Zl/Zp line and paragraph separators, Cs
# lone surrogates. Variation selectors (emoji presentation) and private-use characters
# (Apple's logo) are allowed: they render visibly and can't reorder or hide text.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})


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
        out = f'"{text}"' + (" (truncated)" if cut else "")
        # clean_external drops non-ASCII; say so rather than show a silently shorter value.
        if any(ord(ch) >= 127 for ch in unicodedata.normalize("NFKC", value)):
            out += " (non-ASCII hidden)"
        return out
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


PHRASED = "OK to "  # PER-268: a summary filled from a tool's template reads as a question


def plain(summary: str) -> str:
    """A stored approval summary as a line, not a question: without ``OK to `` (lists, the
    briefing)."""
    return summary.removeprefix(PHRASED)


def _literal(text: str) -> str:
    """A template's own words, cleaned like any external string but keeping its quotes and
    the spaces around them."""

    def clean(part: str) -> str:
        if not part.strip():
            return " " if part else ""
        lead = " " if part[0].isspace() else ""
        trail = " " if part[-1].isspace() else ""
        return lead + clean_external(part) + trail

    return '"'.join(clean(part) for part in text.split('"'))


def _filled(value: Any) -> str | None:
    """An arg's value in a filled summary (None: unset). A boolean prints nothing: it is
    only a ``[...]`` part's condition."""
    if value is None or value is False or value == "":
        return None
    if value is True:
        return ""
    if isinstance(value, int | float):
        return clean_external(repr(value))[:MAX_VALUE_LEN]
    if isinstance(value, str):
        flat = clean_external(value)
        if not flat:
            return None
        return flat if len(flat) <= MAX_VALUE_LEN else flat[: MAX_VALUE_LEN - 3].rstrip() + "..."
    return None  # a list or an object: the arg list says it better


def fill_summary(template: str, args: Mapping[str, Any]) -> str | None:
    """A tool's ``approval_summary`` filled from ``args`` (PER-268), e.g. ``merge PR #121 in
    justinhe16/resonant``; None when an arg outside ``[...]`` is unset or the template can't
    be read (the caller falls back to the arg list). Each value is cleaned and clipped like
    ``describe_args``'s; the template's own words are cleaned too."""
    try:
        segments = parse_summary(template)
    except ValueError:
        return None
    out: list[str] = []
    for optional, pieces in segments:
        part: list[str] = []
        for kind, name in pieces:
            if kind == "text":
                part.append(_literal(name))
                continue
            value = _filled(args.get(name))
            if value is None:
                if not optional:
                    return None
                part = []
                break
            part.append(value)
        out.extend(part)
    text = " ".join("".join(out).split())
    return text or None


def phrased(intent: Intent) -> str | None:
    """The intent's summary from its tool's template, or None when it has none or it can't
    be filled."""
    template = intent.spec.approval_summary
    return None if template is None else fill_summary(template, intent.args)


def summarize(intent: Intent, *, action: str | None = None) -> str:
    """The approval summary for ``intent``, e.g. ``add note text="buy milk"``.

    ``action`` is an optional code-built phrase (e.g. ``delete all 4 notes``) that replaces
    the default ``<extension>: <tool name>`` heading. It must come from code, never a model;
    it is cleaned like any other string. Args are always appended after it. Without one, a
    tool's ``approval_summary`` (PER-268) is the whole summary when it fills.
    """
    if action is None and (filled := phrased(intent)) is not None:
        return filled
    if action is None:
        head = intent.tool.replace("_", " ")
        if intent.extension is not None:
            head = f"{intent.extension}: {head}"
    else:
        head = action
    head = clean_external(head) or intent.tool
    args = describe_args(intent.args)
    return f"{head} {args}" if args else head

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

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from resonant.gate.codes import clean_external, mask_codes
from resonant.gate.types import Intent
from resonant_sdk.tool import parse_summary, summary_args

MAX_ARGS = 3  # args shown; the rest become "+N more args"
MAX_KEY_LEN = 20
MAX_VALUE_LEN = 40
DETAIL_MAX = 4096  # PER-300: one arg shown in full under the summary
PLAIN_MAX = 140  # PER-324: the brain's plain sentence over a prompt's exact call
_MARKUP = re.compile(r"[`*_~|]")  # PER-324: Slack mrkdwn the plain line never carries
_LINK = re.compile(r"\b(?:https?|ftp|mailto|slack):\S*", re.IGNORECASE)
# PER-325: a bare domain Slack would auto-link (``evil.com``, ``www.x.io/path``). File names
# (``README.md``, ``app.py``) aren't one: the last label must be a common web TLD.
_DOMAIN = re.compile(
    r"(?<![\w@.-])(?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|io|co|dev|app|ai|me|us|uk|ca|de|fr|ru|cn|info|biz|xyz|site|online|link"
    r"|click|top|gg|ly|sh|to|tv|cc|zip|mov|gov|edu)\b(?:/\S*)?",
    re.IGNORECASE,
)

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
    ``describe_args``'s; the template's own words are cleaned too. Every set arg the
    template doesn't name follows in parentheses (``describe_args``), so nothing a call
    does is hidden; ``{~arg}`` names one left out on purpose."""
    try:
        segments = parse_summary(template)
        named = summary_args(template)
    except ValueError:
        return None
    out: list[str] = []
    for optional, pieces in segments:
        part: list[str] = []
        for kind, name in pieces:
            if kind == "text":
                part.append(_literal(name))
                continue
            if kind == "hide":
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
    # an empty string says nothing (a GET's `body=""`): not worth the approver's eye
    rest = {k: v for k, v in args.items() if k not in named and v is not None and v != ""}
    if text and rest:
        text += f" ({describe_args(rest)})"
    return text or None


def phrased(intent: Intent) -> str | None:
    """The intent's summary from its tool's template, or None when it has none or it can't
    be filled."""
    template = intent.spec.approval_summary
    if template is None:
        return None
    # The detail arg stays in the line (clipped) too: a batch prompt shows lines only.
    return fill_summary(template, intent.args)


def detail_arg(intent: Intent) -> str | None:
    """The arg the prompt shows in full (``approval_detail``), when the call sets it."""
    name = intent.spec.approval_detail
    value = intent.args.get(name) if name is not None else None
    return name if isinstance(value, str) and value.strip() else None


def detail_text(intent: Intent) -> str | None:
    """PER-300: the tool's ``approval_detail`` arg as a block under the prompt's line: the
    value verbatim (not normalised) in a code fence (line breaks kept, tabs as spaces, a
    fence inside shown as ``'''``), with anything that could pass for an approval code
    masked (``mask_codes``: a ``yes A7`` inside can't answer another prompt) and at most
    ``DETAIL_MAX`` characters.
    What was changed is said under the block. None when the tool has none or the arg is
    unset. Bidi, zero-width and control characters never get here: ``unsafe_args`` makes the
    gate deny such an intent outright."""
    name = detail_arg(intent)
    if name is None:
        return None
    # The value as it will run (no NFKC: what the approver reads is what executes).
    text = str(intent.args[name]).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", "    ").replace("```", "'''").strip("\n")
    notes: list[str] = []
    masked = mask_codes(text)
    if masked != text:
        notes.append("code-like text shown as ?")
    if len(masked) > DETAIL_MAX:
        masked = masked[:DETAIL_MAX].rstrip()
        notes.append(f"cut at {DETAIL_MAX} characters")
    block = f"```\n{masked}\n```"
    return block + (f"\n({'; '.join(notes)}; /approvals shows it unchanged)" if notes else "")


def plain_line(text: str | None) -> str | None:
    """PER-324: the brain's stated reason for a gated call as the prompt's plain first line
    (``Look up the shop's locations (read only)``), or None when there is none.
    Cleaned like any external string (``clean_external``: one line, printable ASCII, no
    brackets, approval-code look-alikes masked; Slack mrkdwn characters and links dropped,
    fullwidth links and bare domains too),
    trailing ``. ? !`` dropped (the prompt adds
    its own ``?``), first letter capitalised and at most ``PLAIN_MAX`` characters (a cut ends
    in ``...``). It is the model's words, so it never replaces the code-built summary: the
    prompt shows that exact call right under it."""
    if not isinstance(text, str):
        return None
    # no Slack mrkdwn from the model: a backtick or fence could restyle the exact call under
    # it, ``*bold*`` dress a write up; a link auto-links. PER-325: NFKC first, so a fullwidth
    # ``https://`` is a link here too; a bare domain is one as well.
    norm = unicodedata.normalize("NFKC", text)
    flat = clean_external(_MARKUP.sub(" ", _DOMAIN.sub("(link)", _LINK.sub("(link)", norm))))
    flat = " ".join(flat.split()).rstrip(" .?!;:,")
    if len(flat) > PLAIN_MAX:
        cut = flat[: PLAIN_MAX - 2]  # one more than fits: a space there ends a whole word
        space = cut.rfind(" ")
        cut = cut[:space] if space >= PLAIN_MAX // 2 else cut[: PLAIN_MAX - 3]
        flat = cut.rstrip(" .?!;:,") + "..."  # PER-325: at a word's end when one is near
    if not any(ch.isalnum() for ch in flat):
        return None
    return flat[0].upper() + flat[1:]


def without_asker(plain: str | None, names: Iterable[str]) -> str | None:
    """PER-328: ``plain`` without an ``as <name> asked`` aside when ``<name>`` (or ``you``)
    is who the prompt asks: ``Redeploy the web app, as Lead asked`` reads ``Redeploy the
    web app`` to Lead. Also ``as <name> requested`` / ``wanted`` / ``asked for``, and the
    aside at the start (``As Lead asked, redeploy …``). None stays None; an aside that was
    all there was leaves None."""
    if not plain:
        return plain
    who = sorted({n.strip() for n in [*names, "you"] if n.strip()}, key=len, reverse=True)
    alt = "|".join(re.escape(n) for n in who)
    aside = rf"(?:as|like)\s+(?:{alt})\s+(?:asked(?:\s+for)?|requested|wanted)"
    out = re.sub(rf"[\s,;:-]*\b{aside}\b", "", plain, flags=re.IGNORECASE)
    out = re.sub(rf"^\s*{aside}\b[\s,;:-]*", "", out, flags=re.IGNORECASE)
    out = " ".join(out.split()).strip(" ,;:-")
    if not any(ch.isalnum() for ch in out):
        return None
    return out[0].upper() + out[1:]


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

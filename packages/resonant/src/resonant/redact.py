"""Exact-value redaction of secrets in text and in nested results.

``Redactor(values)`` replaces every occurrence of each value with ``[redacted]`` in these
forms (and any per-character mix of the text forms):

- raw;
- URL-encoded (``%XX`` in either case, ``+`` for a space);
- JSON-string-escaped (``\\"``, ``\\\\``, ``\\/``, ``\\n``…, ``\\uXXXX`` incl. surrogate pairs);
- base64, standard or URL-safe alphabet, at all three alignments: the value prefixed by 0, 1
  or 2 bytes. Only the characters that depend on the value alone are matched, so a value
  inside a larger base64 blob is still caught. The one or two edge characters that also
  depend on the neighbouring bytes are left, as is ``=`` padding.

Overlapping matches (of one value or of several) are merged into one ``[redacted]``. Values
in other transforms (hex, double-escaped, reversed…) are not caught: that residual is
accepted in docs/phase3-plan.md §5.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable
from typing import cast

REDACTED = "[redacted]"
_MIN_B64 = 6  # a shorter stable interior would match unrelated base64


def _hex(text: str) -> str:
    """A case-insensitive pattern for a hex string."""
    return "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in text)


def _json_u(c: str) -> list[str]:
    """``\\uXXXX`` (a surrogate pair outside the BMP)."""
    units = c.encode("utf-16-be")
    hexes = [units[i : i + 2].hex() for i in range(0, len(units), 2)]
    return ["".join(re.escape("\\u") + _hex(h) for h in hexes)]


def _char(c: str) -> str:
    forms = {re.escape(c)}
    forms.add("".join("%" + _hex(f"{b:02x}") for b in c.encode()))
    if c == " ":
        forms.add(re.escape("+"))
    escaped = json.dumps(c)[1:-1]
    if escaped != c:
        forms.add(re.escape(escaped))
    if c == "/":
        forms.add(re.escape("\\/"))
    forms.update(_json_u(c))
    if len(forms) == 1:
        return re.escape(c)
    # Longest first, so a match covers the whole encoded character.
    return "(?:" + "|".join(sorted(forms, key=len, reverse=True)) + ")"


_B64_CHAR = {
    "+": r"(?:%2[bB]|\+|-)",
    "/": r"(?:%2[fF]|\\/|/|_)",
}


def _b64_interiors(value: bytes) -> list[str]:
    """The value-only characters of its base64 at prefix lengths 0, 1 and 2."""
    out: list[str] = []
    for k in range(3):
        encoded = base64.b64encode(b"\0" * k + value).decode()
        start = -(-8 * k // 6)  # chars that mix in the prefix bytes
        end = 8 * (k + len(value)) // 6  # chars made only of whole value bits
        interior = encoded[start:end]
        if len(interior) >= _MIN_B64:
            out.append(interior)
    return out


def _patterns(value: str) -> list[re.Pattern[str]]:
    text = "".join(_char(c) for c in value)
    pats = [text]
    for interior in _b64_interiors(value.encode()):
        pats.append("".join(_B64_CHAR.get(c, re.escape(c)) for c in interior))
    # A lookahead finds every start position, so overlapping matches are all seen.
    return [re.compile(f"(?=({p}))") for p in pats]


class Redactor:
    """Redacts a fixed set of secret values. Empty values are ignored."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        unique = sorted({v for v in values if v}, key=len, reverse=True)
        self._patterns = [p for v in unique for p in _patterns(v)]

    def __bool__(self) -> bool:
        return bool(self._patterns)

    def text(self, text: str) -> str:
        if not self._patterns or not text:
            return text
        spans: list[tuple[int, int]] = []
        for pattern in self._patterns:
            spans.extend(
                (m.start(1), m.end(1)) for m in pattern.finditer(text) if m.end(1) > m.start(1)
            )
        if not spans:
            return text
        spans.sort()
        merged: list[list[int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        parts: list[str] = []
        at = 0
        for start, end in merged:
            parts += [text[at:start], REDACTED]
            at = end
        parts.append(text[at:])
        return "".join(parts)

    def deep[T](self, obj: T) -> T:
        """Redact every string in a JSON-like structure (dict keys too). Other values pass."""
        if not self._patterns:
            return obj
        return cast(T, self._deep(obj))

    def _deep(self, obj: object) -> object:
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            items = cast(dict[object, object], obj).items()
            return {self._deep(k): self._deep(v) for k, v in items}
        if isinstance(obj, list):
            return [self._deep(v) for v in cast(list[object], obj)]
        if isinstance(obj, tuple):
            return tuple(self._deep(v) for v in cast(tuple[object, ...], obj))
        return obj

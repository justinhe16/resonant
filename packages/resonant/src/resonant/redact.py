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
in other transforms (hex, HTML entities, double-escaped, line-wrapped MIME base64…) are not
caught: that residual is
accepted in docs/phase3-plan.md §5.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable
from typing import Final, cast

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
        self.values: tuple[str, ...] = tuple(unique)
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
        """Redact every string in a JSON-like structure (dict keys too) and return a copy.
        Other values, including ``bytes`` and sets, pass through unchanged."""
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


# --- pattern-based (values unknown) ---------------------------------------------------

# A word boundary on the left, ASCII only (Postgres's ``\y`` depends on the database locale for
# non-ASCII letters; this one is never narrower, so what the relay's SQL family refuses is
# always replaced here first).
_WORD_START = r"(?<![A-Za-z0-9_])"

_SECRET_SCRUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----.*?"
                r"(?:-----END [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----|\Z)", re.DOTALL), REDACTED),
    # The bare header too (a text cut before its closing dashes), as the relay's SQL family has it.
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY"), REDACTED),
    (re.compile(_WORD_START + r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|sk-[A-Za-z0-9_-]{20,}|xox[abprse]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[0-9A-Z]{16}"
                r"|AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}"
                r"|lin_api_[A-Za-z0-9]{20,}|sb_secret_[A-Za-z0-9_-]{10,})"), REDACTED),
    (re.compile(_WORD_START + r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
     REDACTED),
    (re.compile(r"(?i)" + _WORD_START + r"(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{12,}"),
     rf"\1\2{REDACTED}"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]{0,31}://)[^/\s@'\"]{1,256}@"), rf"\1{REDACTED}@"),
    # A credential-named key (``GITHUB_TOKEN``, ``"password"``, ``api_key``) with a literal
    # value: quoted, or a bare token with both a letter and a digit. Calls are left alone.
    # Every run is bounded, so a long ``a.a.a.…`` line stays linear.
    (re.compile(r"(?i)\b([A-Za-z0-9_.-]{0,40}(?:password|passwd|pwd|secret|secret[_-]?key|token"
                r"|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)[\"']?)"
                r"(\s*[:=]\s*)(\"[^\"\n]{4,256}\"|'[^'\n]{4,256}'"
                r"|(?=[A-Za-z0-9._~+/=-]{0,256}[0-9])(?=[A-Za-z0-9._~+/=-]{0,256}[A-Za-z])"
                r"[A-Za-z0-9._~+/=-]{8,256}(?=[\s,;)}\]\"']|$))"), rf"\1\2{REDACTED}"),
    # A password or secret with any bare value of 6+ characters (no digit needed).
    (re.compile(r"(?i)\b([A-Za-z0-9_.-]{0,40}(?:password|passwd|secret)[\"']?)(\s*[:=]\s*)"
                r"(?![\"']|\[redacted\])[^\s,;)}\]\"'(]{6,256}(?=[\s,;)}\]\"']|$)"),
     rf"\1\2{REDACTED}"),
)  # fmt: skip


REDACT_SLACK = 256  # text past a clip that is still scanned, so a secret across it is seen


def redact_secrets(text: str) -> str:
    """Credential-shaped text replaced by ``[redacted]``. Pattern-based: the read tools
    don't know the values (``redact.Redactor`` is for known ones). Callers bound the text
    (``redact_clip``); the patterns are linear but not free."""
    for pattern, repl in _SECRET_SCRUBS:
        text = pattern.sub(repl, text)
    return text


CUT_MARK: Final = "… [cut]"

# The relay's own check (``relay_ingest``, supabase/migrations/20261102000000): the pattern
# families above that have a fixed shape, as one Postgres ARE (``\y`` is a word boundary).
# ``redact_secrets`` is a superset (tests/test_redact.py), so a snapshot that went through it
# never trips the database; the fuzzy ``password=`` family stays Python-only. The migration
# holds this exact text (a test compares them).
SECRET_SQL_FAMILY: Final = "".join((  # joined: ruff's S105 reads a literal as a password
    r"\y(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}",
    r"|xox[abprse]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}",
    r"|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|lin_api_[A-Za-z0-9]{20,}",
    r"|sb_secret_[A-Za-z0-9_-]{10,}",
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    r"|[Bb]earer\s+[A-Za-z0-9._~+/=-]{12,})",
    r"|-----BEGIN [A-Z0-9 ]*PRIVATE KEY",
))  # fmt: skip


# A Claude Remote Control link (whoever holds it can drive the session): never published in
# any text. ``relay_ingest`` refuses any text holding the prefix (``RC_LINK_SQL``), so the
# scrub takes the bare prefix too (``*``, not ``+``): text that only mentions the prefix must
# never get a whole push refused.
RC_LINK = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_-]*")
RC_LINK_SQL: Final = "https://claude\\.ai/code/"
RC_MARK: Final = "[remote control link]"


def scrub_rc_links(text: str) -> str:
    return RC_LINK.sub(RC_MARK, text)


def clip_text(text: str | None, cap: int, *, known: Redactor | None = None) -> str | None:
    """``text`` made safe to publish, at most ``cap`` characters: credential-shaped text
    replaced (``redact_secrets`` over the first ``cap`` + ``REDACT_SLACK`` characters, so a
    secret that straddles the cut is whole when it is seen; a Remote Control link is replaced
    first, ``scrub_rc_links``), then each value ``known`` (a ``Redactor``) replaced, then cut
    to ``cap`` with ``CUT_MARK`` as the last characters (counted inside ``cap``) when anything
    was cut. None stays None."""
    if text is None:
        return None
    head = text[: cap + REDACT_SLACK]
    out = redact_secrets(scrub_rc_links(head))
    if known:
        out = known.text(out)
    if len(out) > cap or len(head) < len(text):
        if len(out) + len(CUT_MARK) <= cap:
            return out + CUT_MARK
        return out[: max(0, cap - len(CUT_MARK))] + CUT_MARK
    return out

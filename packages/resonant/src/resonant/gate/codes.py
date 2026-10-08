"""Approval codes, approval message text, and sanitizing external strings.

Approval texts are always built by code from intent fields. They are never written by a
model. Any externally sourced string (an email subject, web text, tool output) goes
through ``sanitize_external`` first, so it can't add lines, fake approval codes, or impersonate
a second request.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from collections.abc import Collection

# No look-alikes: no 0/O, 1/I/L, 5/S, 2/Z, 8/B.
CODE_LETTERS = "ACDEFGHJKMNPQRTUVWXY"
CODE_DIGITS = "34679"
CODE_PATTERN = re.compile(r"\[?\b([A-Z][0-9])\b\]?", re.IGNORECASE)


def new_code(pending: Collection[str]) -> str:
    """A 2-character code (letter + digit) unique among pending approvals."""
    taken = {c.upper() for c in pending}
    free = [a + d for a in CODE_LETTERS for d in CODE_DIGITS if a + d not in taken]
    if not free:
        raise RuntimeError("too many pending approvals")
    return secrets.choice(free)


# After normalization, any letter followed by a digit, optionally separated by spaces or
# punctuation, could pass for a code like "A7". Those are masked.
_CODE_LIKE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][\s\-_.:/]*[0-9](?![0-9])")


def sanitize_external(text: str, max_len: int = 60) -> str:
    """Make an untrusted string safe to embed in an approval summary.

    1. Apply NFKC normalization (fullwidth and compatibility forms become ASCII).
    2. Keep printable ASCII only. Zero-width characters, bidi overrides, control and escape
       sequences, and look-alike scripts (e.g. Cyrillic) are dropped.
    3. Remove brackets and mask code-like tokens, so the string can't fake "[K4] ...".
    4. Collapse whitespace, swap double quotes for single quotes, truncate, and wrap the
       result in quotes.
    """
    norm = unicodedata.normalize("NFKC", text)
    ascii_only = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in norm)
    no_brackets = re.sub(r"[\[\]{}<>]", " ", ascii_only)
    masked = _CODE_LIKE.sub("?", no_brackets)
    flat = re.sub(r"\s+", " ", masked).strip().replace('"', "'")
    if len(flat) > max_len:
        flat = flat[: max_len - 3].rstrip() + "..."
    return f'"{flat}"'


def approval_text(code: str, summary: str, *, require_code: bool = False) -> str:
    """The message sent to approvers. ``summary`` must be built by code from intent fields."""
    if require_code:
        return f"[{code}] {summary}? Reply exactly: yes {code} / no {code}"
    return f"[{code}] {summary}? Reply yes / no / yes with comments: …"

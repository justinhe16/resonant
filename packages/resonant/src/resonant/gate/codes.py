"""Approval codes, approval message text, and sanitizing external strings.

Approval texts are always built by code from intent fields. They are never written by a
model. Any externally sourced string (an email subject, web text, tool output) goes
through ``sanitize_external`` first, so it can't add lines, fake approval codes, or impersonate
a second request.
"""

from __future__ import annotations

import re
import secrets
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


def sanitize_external(text: str, max_len: int = 60) -> str:
    """Make an untrusted string safe to embed in an approval summary."""
    flat = re.sub(r"\s+", " ", text).strip()
    flat = CODE_PATTERN.sub("[?]", flat)
    flat = flat.replace('"', "'")
    if len(flat) > max_len:
        flat = flat[: max_len - 1].rstrip() + "…"
    return f'"{flat}"'


def approval_text(code: str, summary: str, *, require_code: bool = False) -> str:
    """The message sent to approvers. ``summary`` must be built by code from intent fields."""
    if require_code:
        return f"[{code}] {summary}? Reply exactly: yes {code} / no {code}"
    return f"[{code}] {summary}? Reply yes / no / yes with comments: …"

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
from collections.abc import Collection, Sequence

# No look-alikes: no 0/O, 1/I/L, 5/S, 2/Z, 8/B.
CODE_LETTERS = "ACDEFGHJKMNPQTUVWXY"  # no R: run codes are R<n> (R3), see tools/runs.py
CODE_DIGITS = "34679"
CODE_PATTERN = re.compile(r"\[?\b([A-Z][0-9])\b\]?", re.IGNORECASE)


def new_code(pending: Collection[str]) -> str:
    """A 2-character code (letter + digit) unique among pending approvals."""
    taken = {c.upper() for c in pending}
    free = [a + d for a in CODE_LETTERS for d in CODE_DIGITS if a + d not in taken]
    if not free:
        raise RuntimeError("too many pending approvals")
    return secrets.choice(free)


# After normalization, an approval-alphabet letter followed by an approval digit, optionally
# separated by spaces or punctuation, could pass for a code like "A7". Those are masked.
# Only the approval alphabet is masked, so run codes (R3), ``k8s``, ``s3`` and ``src/a1.py``
# survive while anything that could pass for a real approval code (``A7``, ``a 7``) does not.
_CODE_LIKE = re.compile(
    rf"(?<![A-Za-z0-9])[{CODE_LETTERS}][\s\-_.:/]*[{CODE_DIGITS}](?![0-9])", re.IGNORECASE
)


def mask_codes(text: str) -> str:
    """Mask anything that could pass for an approval code (``A7``, ``a 7``) and nothing else,
    for longer untrusted text shown as is (an ``ask_claude`` answer)."""
    return _CODE_LIKE.sub("?", text)


def clean_external(text: str) -> str:
    """Steps 1-3 of ``sanitize_external`` plus whitespace collapse, without truncating or
    quoting. Callers that truncate must mark the loss themselves."""
    norm = unicodedata.normalize("NFKC", text)
    ascii_only = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in norm)
    no_brackets = re.sub(r"[\[\]{}<>]", " ", ascii_only)
    masked = _CODE_LIKE.sub("?", no_brackets)
    return re.sub(r"\s+", " ", masked).strip().replace('"', "'")


def sanitize_external(text: str, max_len: int = 60) -> str:
    """Make an untrusted string safe to embed in an approval summary.

    1. Apply NFKC normalization (fullwidth and compatibility forms become ASCII).
    2. Keep printable ASCII only. Zero-width characters, bidi overrides, control and escape
       sequences, and look-alike scripts (e.g. Cyrillic) are dropped.
    3. Remove brackets and mask code-like tokens, so the string can't fake "[K4] ...".
    4. Collapse whitespace, swap double quotes for single quotes, truncate, and wrap the
       result in quotes.
    """
    flat = clean_external(text)
    if len(flat) > max_len:
        flat = flat[: max_len - 3].rstrip() + "..."
    return f'"{flat}"'


def approval_text(
    code: str,
    summary: str,
    *,
    require_code: bool = False,
    destructive: bool = False,
    always: bool = False,
    phrased: bool = False,
) -> str:
    """The message sent to approvers. ``summary`` must be built by code from intent fields.

    ``destructive`` (L1) and ``require_code`` (pay) both mean only ``yes <code>`` approves;
    ``destructive`` takes precedence when both are set. ``always`` (a worker's trust check,
    never with the other two) offers ``yes always``. ``phrased`` (PER-268): the summary is the
    tool's own phrase (``approval_summary``), asked as a question with the code after it:
    ``OK to merge PR #121 in justinhe16/resonant? [P3] yes / no``.
    """
    if phrased:
        summary = f"OK to {summary}"
        if always and not (destructive or require_code):
            return f"{summary}? [{code}] yes / yes always / no"
        if destructive:
            return f"{summary}? This can't be undone. [{code}] yes {code} / no"
        if require_code:
            return f"{summary}? [{code}] reply exactly: yes {code} / no {code}"
        return f"{summary}? [{code}] yes / no"
    if always and not (destructive or require_code):
        return f"[{code}] {summary}. Reply yes / yes always / no"
    if destructive:
        return f"[{code}] {summary}? This can't be undone. Reply yes {code} / no"
    if require_code:
        return f"[{code}] {summary}? Reply exactly: yes {code} / no {code}"
    return f"[{code}] {summary}? Reply yes / no"


def batch_text(
    code: str,
    lines: Sequence[str],
    *,
    require_code: bool = False,
    destructive: bool = False,
) -> str:
    """One prompt for a batch of changes (BA-1): a numbered line per change, then one answer
    for all of them. Lines are never clipped: each is the whole code-built summary a lone
    prompt would show (already bounded: at most 3 args, each clipped and marked), so a yes
    covers only what the owner saw. The batch takes its strictest change's level:
    ``destructive`` (an L1 write) or ``require_code`` (L1, pay, or a change that widens trust)
    means only ``yes <code>`` approves.

    ``[D7] 4 changes, all or none:\\n1. linear: save issue id="RES-12" …\\n…\\nReply yes / no``
    """
    shown = [f"{i}. {line}" for i, line in enumerate(lines, 1)]
    head = f"[{code}] {len(lines)} changes, all or none:"
    if destructive:
        tail = f"Some can't be undone. Reply yes {code} / no"
    elif require_code:
        tail = f"Reply exactly: yes {code} / no {code}"
    else:
        tail = "Reply yes / no"
    return "\n".join([head, *shown, tail])

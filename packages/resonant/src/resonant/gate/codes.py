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


# PER-318: the code-built ``yes always`` notes a summary ends with when its prompt offers one:
# a tool approval's `` (yes always = <rule>)`` (``gate.policy.ALWAYS_NOTE``), a worker check's
# ``(<reason>; yes always = <rule>)`` (``trust.checks.check_summary``).
_TOOL_OFFER = " (yes always = "
_CHECK_OFFER = "; yes always = "


def without_offer(summary: str) -> str:
    """``summary`` without its trailing ``yes always`` note, for an approver who isn't offered
    one (PER-318). The note is code-built and last, so only the last match of either form is
    cut, and only when the summary ends with its ``)``: outside text earlier in the summary can
    never make it cut more than the note."""
    if not summary.endswith(")"):
        return summary
    tool, check = summary.rfind(_TOOL_OFFER), summary.rfind(_CHECK_OFFER)
    if check > tool:
        return summary[:check] + ")"
    return summary[:tool] if tool >= 0 else summary


def approval_text(
    code: str,
    summary: str,
    *,
    require_code: bool = False,
    destructive: bool = False,
    always: bool = False,
    phrased: bool = False,
    plain: str | None = None,
) -> str:
    """The message sent to approvers. ``summary`` must be built by code from intent fields.

    ``destructive`` (L1) and ``require_code`` (pay) both mean only ``yes <code>`` approves;
    ``destructive`` takes precedence when both are set. ``always`` (a worker's trust check,
    never with the other two) offers ``yes always``. ``phrased`` (PER-268): the summary is the
    tool's own phrase (``approval_summary``), asked as a question with the code after it:
    ``OK to merge PR #121 in justinhe16/resonant? [P3] yes / no``.

    ``plain`` (PER-324, ``gate.summary.plain_line``: the brain's stated reason, cleaned) is
    the question instead, in the phrased form without ``OK to``, and the exact code-built
    ``summary`` follows on its own line as inline code (``exact_line``):
    ``Look up the shop's locations? [A7] yes / no`` then ```` `call GET /v2/locations` ````.
    """
    if plain:
        head = approval_text(
            code, plain, require_code=require_code, destructive=destructive, always=always,
            phrased=True,
        ).removeprefix(_OK_TO)  # fmt: skip
        return f"{head}\n{exact_line(summary)}"
    if phrased:
        summary = f"{_OK_TO}{summary}"
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


_OK_TO = "OK to "


def exact_line(summary: str) -> str:
    """PER-324: a code-built summary as the line under a plain question: inline code (Slack
    renders it monospace), a backtick inside shown as ``'``."""
    return "`" + summary.replace("`", "'") + "`"


def pick_example(n: int) -> str:
    """The ``yes 1,3`` example a batch of ``n`` changes shows (PER-255): numbers it has."""
    return "1,3" if n >= 3 else "1"


def batch_text(
    code: str,
    lines: Sequence[str],
    *,
    require_code: bool = False,
    destructive: bool = False,
    plains: Sequence[str | None] = (),
) -> str:
    """One prompt for a batch of changes (BA-1): a numbered line per change, then the answer.
    Lines are never clipped: each is the whole code-built summary a lone prompt would show
    (already bounded: at most 3 args, each clipped and marked), so a yes covers only what the
    owner saw. ``yes`` / ``no`` answer all of them; ``yes 1,3 <code>`` (PER-255) approves only
    those and drops the rest. The batch takes its strictest change's level: ``destructive``
    (an L1 write) or ``require_code`` (L1, pay, or a change that widens trust) means only
    ``yes <code>`` approves all of it.

    ``[D7] OK to make these 4 changes?\n1. linear: save issue id="RES-12" …\n…\n``
    ``Reply yes / no, or yes 1,3 D7 for just those.``

    ``plains`` (PER-324, by position; None or missing: none) puts a change's plain sentence
    first, its exact line under it as inline code (``exact_line``), indented.
    """
    shown: list[str] = []
    for i, line in enumerate(lines, 1):
        plain = plains[i - 1] if i <= len(plains) else None
        if plain:
            shown.append(f"{i}. {plain}\n   {exact_line(line)}")
        else:
            shown.append(f"{i}. {line}")
    head = f"[{code}] OK to make these {len(lines)} changes?"
    some = f"or yes {pick_example(len(lines))} {code} for just those."
    if destructive:
        tail = f"Some can't be undone. Reply yes {code} / no, {some}"
    elif require_code:
        tail = f"Reply exactly: yes {code} / no {code}, {some}"
    else:
        tail = f"Reply yes / no, {some}"
    return "\n".join([head, *shown, tail])

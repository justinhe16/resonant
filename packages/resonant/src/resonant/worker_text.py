"""How a worker's text is shown to the owner (and to the brain): one cleaned line per
question or option, other runs' codes masked (docs/employee-plan.md D3, EM-5).

Kept apart from ``runners`` so the router can use it without importing the runners.
"""

from __future__ import annotations

import re

from resonant.gate.codes import clean_external
from resonant.redact import Redactor

OWNER_TEXT_MAX = 600


def clip(text: str, limit: int) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


OPTION_LETTERS = "abcdefghij"  # worker_cli.MAX_OPTIONS is 10
QUESTION_SHOWN = 400
OPTION_SHOWN = 80
URGENT_MARK = "\u26a0"
# A run code in a worker's text. Another run's code is masked, so a question can't pose as
# another run's (``R6 asks: …``) and steer the owner's ``R6 …`` reply there.
_RUN_CODE = re.compile(r"(?<![A-Za-z0-9])R\d+(?![A-Za-z0-9])", re.ASCII | re.IGNORECASE)


def worker_text(code: str, text: str, limit: int, redactor: Redactor | None = None) -> str:
    """A worker's text as the owner sees it: redacted, ``clean_external`` (one line,
    printable ASCII, no brackets, approval-code look-alikes masked), other runs' codes
    masked, clipped."""
    shown = clean_external((redactor or Redactor()).text(text))
    shown = _RUN_CODE.sub(lambda m: m.group(0) if m.group(0).upper() == code else "R?", shown)
    return clip(shown, limit)


def owner_question_text(
    code: str,
    question: str,
    options: tuple[str, ...],
    *,
    urgent: bool,
    others_open: bool = False,
    redactor: Redactor | None = None,
) -> str:
    """``R4 asks: Which DB?`` then ``(a) postgres (b) sqlite``; ``⚠`` first when urgent.
    Built by code: the worker's text is one cleaned line each, so it can't add lines."""
    text = f"{code} asks: {worker_text(code, question, QUESTION_SHOWN, redactor)}"
    if options:
        text += "\n" + " ".join(
            f"({OPTION_LETTERS[i]}) {worker_text(code, o, OPTION_SHOWN, redactor)}"
            for i, o in enumerate(options[: len(OPTION_LETTERS)])
        )
    if others_open:
        text += f"\nOther runs are waiting too: reply {code} <answer>."
    if urgent:
        text = f"{URGENT_MARK} {text}"
    return text if len(text) <= OWNER_TEXT_MAX else text[: OWNER_TEXT_MAX - 3].rstrip() + "..."

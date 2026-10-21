"""Text helpers shared by the brain's prompt parts. Pure."""

from __future__ import annotations

import re
import unicodedata

# ASCII and fullwidth angle brackets: removed from anything inside a prompt tag, so text can't
# close the tag or fake another one (as ``router.prefixes.wrap_prior_reply``).
ANGLES = str.maketrans({"<": " ", ">": " ", "\uff1c": " ", "\uff1e": " "})


def drop_controls(text: str) -> str:
    """``text`` without control or format characters (Unicode Cc and Cf), newlines kept."""
    return "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf"))


def clip(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, ending ``…`` when cut."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def flat(text: str, limit: int | None = None) -> str:
    """One line: no control or format characters, no angle brackets, whitespace collapsed,
    clipped to ``limit``."""
    out = re.sub(r"\s+", " ", drop_controls(text).translate(ANGLES)).strip()
    return out if limit is None else clip(out, limit)


# PER-268: a reply that asks for a yes, or says a change is queued or waiting on the owner,
# when no approval is pending (qwen looped "The merge is queued. Approve it now?" with
# nothing to approve). Only Resonant's own prompt asks for a yes; the model calls the tool.
NO_PENDING_REPLY = "Nothing is waiting for your yes right now. Say what to do and I'll ask for it."
_PENDING_ASK = re.compile(
    r"\b(?:approve|ok|okay)\s+(?:it|this|that)(?:\s+now)?\s*\?"
    r"|\b(?:reply|say|text|send)\s+(?:me\s+)?['\"\u2018\u201c]?yes\b"
    r"|\b(?:merge|change|request|approval)\s+(?:is|was|has\s+been)\s+queued\b"
    r"|\b(?:waiting|wait)\s+(?:on|for)\s+(?:your\s+)?(?:yes|approval|ok|go-ahead)\b"
    r"|\bawaiting\s+(?:your\s+)?approval\b"
    r"|\bneeds?\s+your\s+(?:yes|approval|ok)\b",
    re.IGNORECASE,
)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")


def drop_pending_asks(reply: str) -> tuple[str, int]:
    """``reply`` without its sentences that ask for a yes or say something is queued for
    the owner's approval (call it only when none is pending), and how many were dropped.
    ``NO_PENDING_REPLY`` when nothing else is left."""
    kept: list[str] = []
    dropped = 0
    for line in reply.splitlines():
        parts = [p for p in _SENTENCES.split(line) if p.strip()]
        keep = [p for p in parts if not _PENDING_ASK.search(p)]
        dropped += len(parts) - len(keep)
        if keep or not parts:
            kept.append(" ".join(keep))
    if not dropped:
        return reply, 0
    rest = "\n".join(kept).strip()
    return (rest or NO_PENDING_REPLY), dropped

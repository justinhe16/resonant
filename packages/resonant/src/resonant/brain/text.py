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

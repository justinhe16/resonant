"""Triage (BR-10): the local model's first call on a chat turn decides who answers it.

One ``chat_json`` call over the turn's own messages and toolset, plus one trailing user turn
(``instruction``): the cached prompt prefix (system, tools, facts, history, the turn) is the
same as the first ``chat_tools`` round's, so the local route's next call is warm. The answer
is ``{"handler": "local" | "claude", "kind": ..., "say": ...}``; Ollama writes an object's keys
in sorted order, so the keys are named for that order: the route first, then the kind, then
the ack written for that route.

``say`` is the instant ack sent while Claude works (``clean_ack``): one short line, no
question, no claim; anything else, or no answer in time, is a canned variant
(``canned_ack``). The ack never leaves this module unchecked.

Pure apart from the model call the runner makes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from resonant.brain.text import flat

Route = Literal["local", "claude"]
ACK_MAX_CHARS = 80
TRIAGE_MAX_TOKENS = 80
KINDS = ("general", "research", "email", "calendar", "link")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handler": {"type": "string", "enum": ["local", "claude"]},
        "kind": {"type": "string", "enum": list(KINDS)},
        "say": {"type": "string"},  # clipped by ``clean_ack``, never a validation failure
    },
    "required": ["handler", "kind", "say"],
    "additionalProperties": False,
}

_LOCAL = {
    "normal": (
        "local: you can fully and correctly handle it yourself right now: a status check, "
        "adding or listing notes or reminders, remembering a fact, one lookup with an "
        "obvious tool (one run, one list, where a repo is), small talk or general knowledge, "
        "or a follow-up already answered above."
    ),
    "claude": ("local: only a status check, adding or listing notes or reminders, or small talk."),
}
_CLAUDE = (
    "claude: anything else: more than one step, a why or how question, code or a repo's "
    "history (commits, PRs, what changed), debugging, research, the web or news, a link, "
    "email, calendar, a photo or file, or anything you aren't sure you can get right. When "
    "in doubt, choose claude."
)
_FORMAT = (
    "Reply with only a JSON object: handler (local or claude), kind (for claude: research "
    "for the web, email, calendar, link for a URL, else general; for local: general), and "
    "say. For claude, say is a short text to the owner while Claude works, terse coworker "
    "style, lowercase, under 60 characters, naming what is being checked, never an answer "
    'and never a question, e.g. "on it, digging through resonant\'s history", "checking '
    'R4\'s logs", "one sec, reading that page", "looking at your calendar". For local, say '
    'is "".'
)


def instruction(bias: str = "normal") -> str:
    """The trailing user turn of the triage call (after the turn's message and nudge)."""
    local = _LOCAL.get(bias, _LOCAL["normal"])
    return (
        "Before you act: decide who handles the owner's message above, you (local) or "
        "Claude, a stronger model that reads files, the web and Resonant's tools and acts "
        "through the same approvals.\n"
        f"{local}\n{_CLAUDE}\n{_FORMAT}"
    )


@dataclass(frozen=True)
class Triage:
    route: Route
    kind: str
    ack: str | None  # a cleaned ack, or None (use a canned one)


def parse(data: dict[str, Any] | None) -> Triage | None:
    """The model's JSON as a ``Triage``, or None when it isn't one."""
    if not isinstance(data, dict):
        return None
    route, kind, say = data.get("handler"), data.get("kind"), data.get("say")
    if route not in ("local", "claude"):
        return None
    kind = kind if kind in KINDS else "general"
    ack = clean_ack(say) if isinstance(say, str) else None
    return Triage(route, cast(str, kind), ack)


_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
# An ack says what is being checked; it never answers, claims a result or asks.
_RESULT_WORDS = re.compile(
    r"\b(?:done|finished|merged|added|saved|created|sent|deleted|removed|cancell?ed|"
    r"started|restarted|scheduled|approved|fixed|found|here(?:'s| is| are))\b",
    re.IGNORECASE,
)


def clean_ack(raw: str) -> str | None:
    """``raw`` as one short line, or None when it isn't a usable ack (empty, too long, a
    question, a link, a claimed result)."""
    text = flat(raw).strip().strip("\"'`").strip()
    if not text or len(text) > ACK_MAX_CHARS or "?" in text or _URL.search(text):
        return None
    if _RESULT_WORDS.search(text) or "{" in text or "}" in text:
        return None
    return text


CANNED_ACKS = (
    "On it.",
    "Looking into it.",
    "One sec, digging in.",
    "Checking, back shortly.",
    "On it, give me a moment.",
    "Taking a closer look.",
)


def canned_ack(seed: str) -> str:
    """A canned ack, varied by ``seed`` (the task id) and stable for it."""
    n = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    return CANNED_ACKS[n % len(CANNED_ACKS)]


# Asks that are hard by their shape: a why or how-does question, debugging, comparing,
# figuring something out. The live model kept these local and answered them thinly (BR-10
# probe), so they go to Claude whatever it says. "how's the mini" isn't one.
_HARD = re.compile(
    r"^\W*(?:why\b|how\s+(?:does|do|did|come|can|could|should|would|is it that)\b)"
    r"|\b(?:debug|troubleshoot|root cause|compare|figure out|dig into|investigate)\b",
    re.IGNORECASE,
)


def looks_hard(text: str) -> bool:
    """The message is hard by its shape (``_HARD``): Claude, whatever the triage says."""
    return _HARD.search(text) is not None


def has_link(text: str) -> bool:
    """A message with a link goes to Claude (WebFetch), whatever the model says."""
    return _URL.search(text) is not None


__all__ = [
    "ACK_MAX_CHARS",
    "CANNED_ACKS",
    "KINDS",
    "SCHEMA",
    "TRIAGE_MAX_TOKENS",
    "Route",
    "Triage",
    "canned_ack",
    "clean_ack",
    "has_link",
    "instruction",
    "parse",
]

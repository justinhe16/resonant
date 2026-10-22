"""Guards on what the brains say (D7, PER-256). Pure.

Only Claude writes prose now (the local model routes, and its few replies are canned or
built from code), so these are the backstop on Claude's answers. A reply the guard stops is
not sent: the turn is redone once with a message that says why (``redo``), and if the
redo still fails a short code-built fallback goes out instead (``FALLBACKS``).

- **Numbers.** A number in the reply that appears in no tool result, no message the session
  was given and nothing it read is unsourced (``unsourced``): the money questions of
  2026-10-22 were answered with $1,247 and three restaurants from no tool at all.
- **Claims.** A reply that says something changed (``claims_change``) when no change ran and
  none waits on the owner. "close PER-301" answered "PER-301 has been closed" with no call.
- **Standing allows.** "Don't ask me before linear tickets" answered "Got it, no more
  permission prompts" with nothing saved: the reply may claim it only after ``allow_tools``
  (or ``allow_trust_rule``) was called.
- **Tool-call text.** A reply that is the text of a tool call (``merge_run(code="R1")``) is
  never sent.

Spans carry kinds and counts only, never the text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from resonant.models.client import text_tool_calls

# --- numbers ---------------------------------------------------------------------------------

# Integers and decimals, with optional thousands separators: 3, 1,024, 63.2.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _norm_number(token: str) -> str:
    """``"1,024"`` -> ``"1024"``, ``"08"`` -> ``"8"``, ``"42.50"`` -> ``"42.5"``."""
    raw = token.replace(",", "")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return raw
    return format(value.normalize(), "f")


def numbers_in(text: str) -> set[str]:
    return {_norm_number(m) for m in _NUMBER.findall(text)}


def unsupported_numbers(reply: str, sources: Sequence[str]) -> set[str]:
    """Numbers in ``reply`` that appear in none of ``sources``."""
    allowed: set[str] = set()
    for source in sources:
        allowed |= numbers_in(source)
    return numbers_in(reply) - allowed


FREE_NUMBERS = frozenset({"0", "1"})  # "no", "one", "1 of 3": never worth a redo
CALENDAR_DAYS = 7  # the days around today whose date numbers need no source


def _sizes(value: object, out: set[str]) -> None:
    """The length of every list and object in a parsed result: "3 runs" is sourced when a
    result holds a list of three."""
    if isinstance(value, list):
        items = cast(list[object], value)
        out.add(str(len(items)))
        for item in items:
            _sizes(item, out)
    elif isinstance(value, dict):
        fields = cast(dict[str, object], value)
        out.add(str(len(fields)))
        for item in fields.values():
            _sizes(item, out)


def counts_in(source: str) -> set[str]:
    """The list and object sizes of ``source`` when it is a JSON result, else nothing."""
    text = source.strip()
    if not text.startswith(("{", "[")):
        return set()
    try:
        parsed: object = json.loads(text)
    except ValueError:
        return set()
    sizes: set[str] = set()
    _sizes(parsed, sizes)
    return sizes


def calendar_text(now: datetime, tz: str) -> str:
    """The dates around ``now`` (``CALENDAR_DAYS`` each way) and its clock time, in ``tz``:
    the numbers of "last week", "Thursday" or "at 3pm" are the calendar's, not an invention."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    days = [local + timedelta(days=d) for d in range(-CALENDAR_DAYS, CALENDAR_DAYS + 1)]
    return " ".join([f"{d:%Y-%m-%d %d %B %b}" for d in days] + [f"{local:%H:%M %I %M}"])


def unsourced(reply: str, sources: Sequence[str]) -> set[str]:
    """Numbers in ``reply`` that no source holds: not in any of ``sources`` (tool results,
    the messages the session was given, the recent thread), not 0 or 1, and not the size of
    a list in a JSON result. Equal values match across formats (``$1,247`` and ``1247``,
    ``507.80`` and ``507.8``)."""
    allowed: set[str] = set(FREE_NUMBERS)
    for source in sources:
        allowed |= numbers_in(source)
        allowed |= counts_in(source)
    return numbers_in(reply) - allowed


# --- claims ----------------------------------------------------------------------------------

# "Added note: …", "I've saved that…", "Done, merged #41", "Cancelled R2": a first-person or
# leading past-tense claim of a change. Reads that quote a log ("R1 started at …") don't
# match: the verb must open the reply (after an optional "ok"/"done"/"sure") or follow "I".
_CHANGE_VERBS = (
    r"(?:added|saved|created|made|merged|started|launched|kicked off|deleted|removed|cleared|"
    r"sent|scheduled|cancell?ed|stopped|killed|restarted|remembered|noted|updated|"
    r"forgot(?:ten)?|opened|filed|closed|approved|logged)"
)
# A promise ("I'll remember that") with no call is as empty as a claim.
_PROMISE_VERBS = (
    r"(?:add|save|create|make|merge|start|launch|delete|remove|clear|send|schedule|set|cancel|"
    r"stop|kill|restart|remember|note|update|forget|open|file|close|approve|remind)"
)
# "set" only after "I": a reply that opens "Set a reminder …?" is a proposal, not a claim.
# A leading past participle that describes something ("Created 3 days ago by sam", "Merged
# 2h ago", "Logged in as …", "Sent from the Mini") is a read, not a claim.
_NOT_A_CLAIM = r"(?!\s+(?:\d|ago\b|by\b|from\b|in\b|at\b|on\b|yesterday\b|today\b|earlier\b))"
# First person: "I've saved that", "I set a reminder", "I'll remember that": a claim or a
# promise wherever it comes in the reply.
_I_CLAIM = re.compile(
    rf"\bI(?:'ve| have| just)?\s+(?:just\s+|already\s+)?(?:set|{_CHANGE_VERBS})\b"
    rf"|\bI(?:'ll| will)\s+{_PROMISE_VERBS}\b",
    re.IGNORECASE,
)
_CLAIM = re.compile(
    rf"(?:^\W*(?:resonant\W+)?(?:(?:ok(?:ay)?|done|sure|got it)\W+)?{_CHANGE_VERBS}\b"
    rf"{_NOT_A_CLAIM}"
    rf"|{_I_CLAIM.pattern}"
    rf"|\b{_CHANGE_VERBS}\s+(?:it\s+)?(?:as an?|to your|to the)\s+"
    r"(?:notes?|reminders?|facts?)\b"
    rf"|\b(?:reminder|note|ticket|issue|run)\s+(?:is\s+|has been\s+)?"
    r"(?:set|saved|added|created|started|filed)\b)",
    re.IGNORECASE,
)
# "PER-301 has been closed", "the ticket is now closed": a passive claim. Said about a state
# it is a plain read ("PR 41 has been merged" from a list), so it counts only when the
# owner asked for the change (``asks_change``).
_PASSIVE_CLAIM = re.compile(
    rf"\b(?:has|have|had)\s+(?:now\s+|already\s+|just\s+)?been\s+{_CHANGE_VERBS}\b"
    r"|\b(?:is|are)\s+now\s+(?:closed|merged|done|cancell?ed|deleted|saved|removed|set)\b",
    re.IGNORECASE,
)
# The owner asks for a change: an imperative opening the text, or "can you" / "please".
_ASK_VERBS = (
    r"(?:add|save|create|make|merge|start|launch|delete|remove|clear|send|schedule|set|cancel|"
    r"stop|kill|restart|remember|note|update|forget|open|file|close|approve|remind|put|jot|"
    r"write|log|track|tell|ping|text|email|enable|disable|turn|reschedule|archive|resolve|"
    r"complete|finish|mark|assign|move|rename|reopen|reply|answer|draft|fix|run)"
)
_ASKS_CHANGE = re.compile(
    rf"^\W*(?:(?:please|pls|hey|ok(?:ay)?)\W+)*(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    rf"{_ASK_VERBS}\b"
    r"|\b(?:please|pls)\s+\w+",
    re.IGNORECASE,
)


def claims_change(reply: str) -> bool:
    """A reply that says it changed something (to check against what actually ran)."""
    return _CLAIM.search(reply) is not None


def asks_change(text: str) -> bool:
    """The owner's text asks for a change ("close PER-301", "can you add a note ..."), as
    opposed to a question."""
    return _ASKS_CHANGE.search(text) is not None


def claims_done(reply: str, owner_text: str) -> bool:
    """``reply`` says a change was made. A first-person claim always counts. The other forms
    (a leading "Added …", "Merged …", the passive "has been closed") describe a state when the
    owner only asked about it, so they count only when the owner asked for a change."""
    if _I_CLAIM.search(reply) is not None:
        return True
    return asks_change(owner_text) and (
        claims_change(reply) or _PASSIVE_CLAIM.search(reply) is not None
    )


# --- standing allows -------------------------------------------------------------------------

# "don't ask me before linear tickets", "stop asking", "no more permission prompts".
_STANDING = re.compile(
    r"\b(?:don'?t|do\s+not|dont|stop|quit|never|no\s+need\s+to|without|no\s+longer)\s+"
    r"(?:you\s+)?(?:have\s+to\s+|need\s+to\s+)?(?:keep\s+)?(?:me\s+)?ask(?:ing)?\b"
    r"|\bno\s+more\s+(?:permission\s+)?(?:prompts?|permissions?|asking|approvals?)\b"
    r"|\bstop\s+(?:the\s+)?(?:permission\s+)?(?:prompts?|approvals?)\b"
    r"|\b(?:don'?t|do\s+not)\s+(?:need|want)\s+(?:the\s+)?(?:permission\s+)?"
    r"(?:prompts?|approvals?)\b",
    re.IGNORECASE,
)
# The reply claims it is set: "Got it", "I won't ask again", "from now on", a claimed change.
_ALLOW_CLAIM = re.compile(
    r"\b(?:got\s+it|won'?t\s+ask|will\s+not\s+ask|from\s+now\s+on|going\s+forward|"
    r"no\s+more\s+(?:permission\s+)?(?:prompts?|permissions?|asking|approvals?)|"
    r"no\s+longer\s+(?:ask|prompt)|stop(?:ped)?\s+asking|all\s+set|understood|noted)\b",
    re.IGNORECASE,
)
_CANT = re.compile(
    r"\b(?:can'?t|cannot|can\s+not|unable|not\s+able|not\s+possible)\b", re.IGNORECASE
)
ALLOW_TOOLS = ("allow_tools", "allow_trust_rule")


def asks_standing_allow(text: str) -> bool:
    """The owner asks Resonant to stop asking before changes ("don't ask me before ...")."""
    return _STANDING.search(text) is not None


def claims_allow(reply: str) -> bool:
    """``reply`` says the owner won't be asked any more, or that a change was made. Not when
    it says it can't ("I can't make trust changes stand")."""
    if _CANT.search(reply) is not None and re.search(r"got\s+it", reply, re.I) is None:
        return False
    return _ALLOW_CLAIM.search(reply) is not None or claims_change(reply)


# --- tool-call text --------------------------------------------------------------------------

_CALL_TEXT = re.compile(
    r"^\W{0,3}(?:mcp__resonant__)?([A-Za-z_][A-Za-z0-9_]*)\s*\(.*\)\W{0,3}$", re.DOTALL
)


def is_call_text(reply: str, tools: Collection[str]) -> bool:
    """``reply`` is the text of a tool call (``merge_run(code="R1")``, ``{"tool_call": …}``,
    ``<tool_call>``) rather than an answer. Names are the session's tools."""
    body = reply.strip()
    if not body:
        return False
    if (m := _CALL_TEXT.match(body)) is not None and m.group(1) in tools:
        return True
    return bool(text_tool_calls(body, tools))


# --- the checks ------------------------------------------------------------------------------

Kind = Literal["numbers", "claim", "allow", "call"]


@dataclass(frozen=True)
class Problem:
    """Why a reply isn't sent. ``numbers`` names the unsourced ones (``numbers`` kind only)."""

    kind: Kind
    numbers: tuple[str, ...] = ()


NUMBER_REDO = (
    "Your last answer was not sent: it had numbers that appear in no tool result and no "
    "message in this conversation ({numbers}). Look them up with a tool (shell_read can "
    "compute a sum, and runs without asking), or answer without them; if you can't check, "
    "say so. Never write a figure you didn't read."
)
CLAIM_REDO = (
    "Your last answer was not sent: it says something changed, but no tool result in this "
    "turn says 'done:'. If the owner asked for a change, call its tool now (Resonant asks "
    "them first when it must); otherwise say plainly that nothing changed."
)
ALLOW_REDO = (
    "Your last answer was not sent: the owner asked you to stop asking before changes, and "
    "no allow_tools call backs what you said. If it can stand (an L2 change such as linear "
    "tickets, or merge_run), call allow_tools (owner_asked: true when the owner's message "
    "said it) and report what its result says. If it can't (start_run already runs without "
    "asking; trust changes always ask), say so plainly. Never answer with a bare 'Got it'."
)
CALL_REDO = (
    "Your last answer was not sent: it was the text of a tool call, not an answer. Call the "
    "tool itself (Resonant's approvals apply), or answer in words."
)
NUMBER_FALLBACK = "I couldn't check that just now."
CLAIM_FALLBACK = "I didn't save that; nothing changed."
ALLOW_FALLBACK = "I didn't save that; nothing changed."
CALL_FALLBACK = "I couldn't do that just now; nothing changed."
# When a change ran or waits on the owner, its code-built report lines follow this.
ACTED_FALLBACK = "I couldn't check the rest of my answer just now."
FALLBACKS: dict[str, str] = {
    "numbers": NUMBER_FALLBACK,
    "claim": CLAIM_FALLBACK,
    "allow": ALLOW_FALLBACK,
    "call": CALL_FALLBACK,
}
NUMBERS_SHOWN = 6


NO_REPEAT = " Don't repeat a change that already ran."


def redo(problem: Problem) -> str:
    """The message for the redo turn: Resonant's own words, never the owner's."""
    if problem.kind == "numbers":
        shown = ", ".join(problem.numbers[:NUMBERS_SHOWN])
        return NUMBER_REDO.format(numbers=shown) + NO_REPEAT
    return {"claim": CLAIM_REDO, "allow": ALLOW_REDO, "call": CALL_REDO}[problem.kind] + NO_REPEAT


def fallback(problem: Problem, *, changed: bool) -> str:
    """The code-built text that goes out instead of a reply the guard stopped twice (or once,
    when a change ran or waits: no redo then). ``changed``: report lines follow it, so it
    never says "nothing changed"."""
    if changed:
        return NUMBER_FALLBACK if problem.kind == "numbers" else ACTED_FALLBACK
    return FALLBACKS[problem.kind]


def check(
    reply: str,
    *,
    owner_text: str,
    sources: Sequence[str],
    tools: Collection[str],
    called: Collection[str],
    changed: bool,
    asked: bool,
) -> Problem | None:
    """The first problem with Claude's ``reply`` (``numbers``, ``claim``, ``allow``,
    ``call``, in that order of cost to redo), or None. ``sources``: what the numbers may come
    from. ``tools``: the session's tool names. ``called``: the Resonant tools this turn
    called. ``changed``: a change ran (code-built report lines exist). ``asked``: a change
    waits on the owner. A turn with a change waiting or run is its own record: only the
    tool-call text and the numbers are checked then."""
    if is_call_text(reply, tools):
        return Problem("call")
    bad = unsourced(reply, sources)
    if bad:
        ordered = sorted(bad, key=lambda n: (len(n), n))
        return Problem("numbers", tuple(ordered))
    if not changed and not asked and claims_done(reply, owner_text):
        return Problem("claim")
    unbacked = asks_standing_allow(owner_text) and not any(t in called for t in ALLOW_TOOLS)
    if unbacked and claims_allow(reply):
        return Problem("allow")
    return None


def span_fields(problem: Problem | None) -> dict[str, Any]:
    """The span fields for a check: counts and kinds only."""
    if problem is None:
        return {"kind": "none", "hits": 0}
    return {"kind": problem.kind, "hits": len(problem.numbers) or 1}


__all__ = [
    "ACTED_FALLBACK",
    "ALLOW_FALLBACK",
    "ALLOW_REDO",
    "CALL_FALLBACK",
    "CALL_REDO",
    "CLAIM_FALLBACK",
    "CLAIM_REDO",
    "FALLBACKS",
    "NUMBER_FALLBACK",
    "NUMBER_REDO",
    "Problem",
    "asks_change",
    "asks_standing_allow",
    "calendar_text",
    "check",
    "claims_allow",
    "claims_change",
    "claims_done",
    "fallback",
    "is_call_text",
    "numbers_in",
    "redo",
    "unsourced",
    "unsupported_numbers",
]

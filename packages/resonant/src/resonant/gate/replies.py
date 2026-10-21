"""The deterministic approval-reply parser.

A reply is the whole message, after normalization (NFKC, curly quotes straightened,
whitespace collapsed, case ignored). Outside comments, a trailing run of ``.`` and ``!``
is dropped ("Yes.", "yes!!"); ``?`` never is ("yes?" is a question). It matches exactly
one of:

- ``(yes|approve|👍)( <code>)?``
- ``<code> yes``
- ``(no|deny|👎)( <code>)?``
- ``<code> no``
- ``yes( <code>)? with comments: <text>`` (text 1-500 characters)
- ``yes always( <code>)?``, ``yes <code> always`` or ``<code> yes always``: approve and
  add a trust rule. Only a worker's trust check offers it; the gate refuses it for any
  other approval (``always_unsupported``), and never reads it as a plain ``yes``.
- ``(yes|approve) <picks>( <code>)?`` or ``(yes|approve) <code> <picks>`` (PER-255): approve
  only these changes of a batch (``1,3``: their numbers in its prompt, comma separated) and
  drop the rest. The gate checks the numbers against the batch it binds.

Anything else is not a reply: ``y``, ``ok``, ``sure``, ``yes?``, ``yes all``, ``always``,
``no always``, ``yes A7 and C4``, a pasted prompt, or a code outside the approval alphabet
(``yes A1``), ``yes 1 3``, ``no 1,3``, ``yes always 1,3``. A message the reader truncated is
never a reply. There is no classifier.

The parser only reads text. Binding a reply to an approval (approvers, the sent-before
rule, single pending, ``require_code``) is the gate's job.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from resonant.gate.codes import CODE_DIGITS, CODE_LETTERS

MAX_COMMENTS_LEN = 500

# Curly single and double quotes become straight ones.
_QUOTES = str.maketrans(
    dict.fromkeys("\u2018\u2019\u201a\u201b", "'") | dict.fromkeys("\u201c\u201d\u201e\u201f", '"')
)

# A code from the approval alphabet only, so look-alikes ("O7", "A1", "B8") never match.
_CODE = f"[{CODE_LETTERS}][{CODE_DIGITS}]"
# Thumbs up/down, with an optional skin tone and emoji presentation selector.
_TONE = "[\U0001f3fb-\U0001f3ff]?\ufe0f?"
_YES = f"(?:yes|approve|\U0001f44d{_TONE})"
_NO = f"(?:no|deny|\U0001f44e{_TONE})"

# re.ASCII keeps IGNORECASE to ASCII case folding.
_FLAGS = re.ASCII | re.IGNORECASE
_WORD_CODE = re.compile(rf"(?P<word>{_YES}|{_NO})(?: (?P<code>{_CODE}))?", _FLAGS)
_CODE_WORD = re.compile(rf"(?P<code>{_CODE}) (?P<word>yes|no)", _FLAGS)
_COMMENTS = re.compile(
    rf"yes(?: (?P<code>{_CODE}))? with comments: ?(?P<comments>.+)", _FLAGS | re.DOTALL
)
_DENY = re.compile(_NO, _FLAGS)
# "yes always", with the code (if any) before or after "always". Plain "yes" only: no
# emoji or "approve", so the one reply that widens trust is spelled out.
_ALWAYS = (
    re.compile(rf"yes always(?: (?P<code>{_CODE}))?", _FLAGS),
    re.compile(rf"yes (?P<code>{_CODE}) always", _FLAGS),
    re.compile(rf"(?P<code>{_CODE}) yes always", _FLAGS),
)
# PER-255: "yes 1,3", "yes 1,3 D7" or "yes D7 1,3": change numbers of a batch, 1-20 (a batch
# has at most 20, ``gate.batch_max``; so "yes 77" stays a look-alike code, not a pick),
# separated by commas.
_NUM = r"(?:1[0-9]|20|[1-9])(?![0-9])"
_PICKS = rf"{_NUM}(?: ?, ?{_NUM})*"
_SOME = (
    re.compile(rf"(?:yes|approve) (?P<picks>{_PICKS})(?: (?P<code>{_CODE}))?", _FLAGS),
    re.compile(rf"(?:yes|approve) (?P<code>{_CODE}) (?P<picks>{_PICKS})", _FLAGS),
)
# Trailing periods and exclamation marks (autocorrect, emphasis), never question marks.
_TRAILING = re.compile(r"[\s.!]+\Z")


@dataclass(frozen=True, slots=True)
class ParsedReply:
    """A recognized reply. ``code`` is uppercase; ``comments`` keeps the sender's case."""

    decision: Literal["approve", "deny"]
    code: str | None = None
    comments: str | None = None
    always: bool = False  # "yes always"
    # PER-255: "yes 1,3": the batch's change numbers to approve (1-based, sorted, distinct);
    # None for a reply that answers all of it.
    picks: tuple[int, ...] | None = None


def normalize_reply(text: str) -> str:
    """NFKC, straight quotes, collapsed whitespace. Case is kept (matching ignores it)."""
    norm = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return re.sub(r"\s+", " ", norm).strip()


def parse_reply(text: str, *, truncated: bool = False) -> ParsedReply | None:
    """The reply ``text`` encodes, or None if it isn't one (or was ``truncated``)."""
    if truncated:
        return None
    norm = normalize_reply(text)
    if m := _COMMENTS.fullmatch(norm):
        comments = m["comments"].strip()
        if not comments or len(comments) > MAX_COMMENTS_LEN:
            return None
        return ParsedReply("approve", _upper(m["code"]), comments)
    norm = _TRAILING.sub("", norm)
    for pattern in _ALWAYS:
        if a := pattern.fullmatch(norm):
            return ParsedReply("approve", _upper(a["code"]), always=True)
    for pattern in _SOME:
        if p := pattern.fullmatch(norm):
            picks = tuple(sorted({int(n) for n in re.findall(r"\d+", p["picks"])}))
            return ParsedReply("approve", _upper(p["code"]), picks=picks)
    m = _WORD_CODE.fullmatch(norm) or _CODE_WORD.fullmatch(norm)
    if m is None:
        return None
    decision = "deny" if _DENY.fullmatch(m["word"]) else "approve"
    return ParsedReply(decision, _upper(m["code"]))


def _upper(code: str | None) -> str | None:
    return code.upper() if code is not None else None

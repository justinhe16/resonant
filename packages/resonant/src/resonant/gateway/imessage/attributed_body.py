"""Extract the text of an iMessage from ``message.attributedBody``.

Recent macOS versions often leave ``message.text`` NULL and keep the text only in
``attributedBody``: an ``NSArchiver`` "typedstream" of an ``NSMutableAttributedString``.
The string we want is the first ``NSString`` object in the stream::

    ... NSMutableString 01 84 84 08 NSString 01 95 84 01 + <len> <utf-8 bytes> ...

``+`` (0x2B) is the typedstream type code for a C string. ``<len>`` is a typedstream integer:
one byte for 0..127, ``0x81`` followed by a little-endian int16, or ``0x82`` followed by a
little-endian int32.

This is a deliberately narrow parser, not a typedstream decoder: it finds that one string
and validates it. Anything unexpected returns None so the caller can audit
``imessage.parse_failed`` instead of guessing. It never raises on malformed input.
"""

from __future__ import annotations

_CLASS = b"NSString"
_STRING_START = b"\x84\x01+"  # object-start, one type, '+' (C string)
_SEARCH_WINDOW = 16  # bytes after the class name in which the string must start
_INT16 = 0x81
_INT32 = 0x82


def parse_attributed_body(blob: bytes | None) -> str | None:
    """Return the message text encoded in ``blob``, or None if it can't be parsed."""
    if not blob:
        return None
    cls = blob.find(_CLASS)
    if cls < 0:
        return None
    after = cls + len(_CLASS)
    marker = blob.find(_STRING_START, after, after + _SEARCH_WINDOW)
    if marker < 0:
        return None
    pos = marker + len(_STRING_START)
    if pos >= len(blob):
        return None
    tag = blob[pos]
    if tag == _INT16:
        width = 2
    elif tag == _INT32:
        width = 4
    elif tag < 0x80:
        width = 0
    else:
        return None
    if width:
        raw = blob[pos + 1 : pos + 1 + width]
        if len(raw) != width:
            return None
        length = int.from_bytes(raw, "little", signed=True)
        pos += 1 + width
    else:
        length = tag
        pos += 1
    if length < 0 or pos + length > len(blob):
        return None
    try:
        return blob[pos : pos + length].decode("utf-8")
    except UnicodeDecodeError:
        return None

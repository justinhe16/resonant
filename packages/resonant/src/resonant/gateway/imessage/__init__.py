"""iMessage channel: the chat.db inbound reader (and, later, the osascript sender)."""

from resonant.gateway.imessage.attributed_body import parse_attributed_body
from resonant.gateway.imessage.reader import (
    SELFTEST_PREFIX,
    SELFTEST_RE,
    IMessageReader,
    SelfTestCallback,
    SelfTestSighting,
)

__all__ = [
    "SELFTEST_PREFIX",
    "SELFTEST_RE",
    "IMessageReader",
    "SelfTestCallback",
    "SelfTestSighting",
    "parse_attributed_body",
]

"""iMessage channel: the chat.db inbound reader, the osascript sender, and the adapter."""

from resonant.gateway.imessage.attributed_body import parse_attributed_body
from resonant.gateway.imessage.channel import IMessageChannel, split_text
from resonant.gateway.imessage.reader import (
    SELFTEST_PREFIX,
    SELFTEST_RE,
    IMessageReader,
    SelfTestCallback,
    SelfTestSighting,
)
from resonant.gateway.imessage.sender import (
    IMessageSender,
    OsaResult,
    OsaRunner,
    OsascriptRunner,
    SendError,
)

__all__ = [
    "SELFTEST_PREFIX",
    "SELFTEST_RE",
    "IMessageChannel",
    "IMessageReader",
    "IMessageSender",
    "OsaResult",
    "OsaRunner",
    "OsascriptRunner",
    "SelfTestCallback",
    "SelfTestSighting",
    "SendError",
    "parse_attributed_body",
    "split_text",
]

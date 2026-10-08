"""iMessage outbound sender: one ``osascript`` call per message.

The handle and the text are passed to the AppleScript as **argv**, never interpolated
into the script, so no text can change what the script does::

    argv = ["/usr/bin/osascript", "-e", SEND_SCRIPT, handle, text]

The runner that executes argv is injectable (:class:`OsaRunner`), so tests use a fake that
records argv and simulates errors. :class:`OsascriptRunner` is the real one: it runs
``asyncio.create_subprocess_exec`` with a timeout (15s by default, which leaves room for
Messages.app to launch on the first send).

Failures raise :class:`SendError` with an actionable, body-free message. osascript's
stderr is never logged or included: it can echo arguments.

This module knows nothing about principals. :class:`~resonant.gateway.imessage.channel.
IMessageChannel` resolves the destination from principals.yaml before calling it.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

OSASCRIPT = "/usr/bin/osascript"
SEND_TIMEOUT_S = 15.0

SEND_SCRIPT = """\
on run argv
  set theHandle to item 1 of argv
  set theText to item 2 of argv
  tell application "Messages"
    set svc to 1st account whose service type = iMessage
    send theText to participant theHandle of svc
  end tell
end run"""

# Same shape principals.yaml enforces for imessage identities. The first character must be
# "+" or alphanumeric: a handle starting with "-" could be read by osascript as an option.
_HANDLE = re.compile(r"^(\+[1-9]\d{7,14}|[a-z0-9][a-z0-9._%+-]*@[a-z0-9][a-z0-9.-]*\.[a-z]{2,})$")
_ERROR_NUMBER = re.compile(r"\((-?\d+)\)\s*$")

AUTOMATION_DENIED = (
    "Messages automation was denied (-1743): grant Automation (daemon -> Messages) in "
    "System Settings > Privacy & Security > Automation, then retry"
)
MESSAGES_NOT_RUNNING = (
    "Messages.app is not running or not reachable ({code}): open Messages, make sure "
    "Resonant's Apple ID is signed in to iMessage, then retry"
)
UNKNOWN_PARTICIPANT = (
    "Messages could not address that handle over iMessage ({code}): check the handle is "
    "registered with iMessage and that an iMessage account is signed in"
)


@dataclass(frozen=True)
class OsaResult:
    returncode: int
    stdout: str
    stderr: str


class OsaRunner(Protocol):
    async def __call__(self, argv: Sequence[str], timeout_s: float) -> OsaResult:
        """Run argv. Raises ``TimeoutError`` if it does not finish within ``timeout_s``."""
        ...


class SendError(Exception):
    """An osascript send failed. ``str(e)`` is actionable and never contains the text."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class OsascriptRunner:
    """The real runner: ``asyncio.create_subprocess_exec`` with a hard timeout."""

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> OsaResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise
        return OsaResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
        )


def send_argv(handle: str, text: str) -> list[str]:
    """The exact argv for one send. ``text`` is a separate argument, never script source."""
    return [OSASCRIPT, "-e", SEND_SCRIPT, handle, text]


def explain(stderr: str, returncode: int) -> SendError:
    """Map osascript's stderr to an actionable :class:`SendError` (without echoing it)."""
    m = _ERROR_NUMBER.search(stderr.strip())
    code = int(m.group(1)) if m else None
    if code == -1743 or "Not authorized to send Apple events" in stderr:
        return SendError(AUTOMATION_DENIED, code=-1743)
    if code in (-600, -609, -903) or "isn't running" in stderr:
        return SendError(MESSAGES_NOT_RUNNING.format(code=code), code=code)
    if code in (-1728, -1719, -1700) or "participant" in stderr:
        return SendError(UNKNOWN_PARTICIPANT.format(code=code), code=code)
    return SendError(f"osascript failed (exit {returncode}, error {code})", code=code)


class IMessageSender:
    """Sends one text to one handle via osascript."""

    def __init__(self, runner: OsaRunner | None = None, *, timeout_s: float = SEND_TIMEOUT_S):
        self.runner: OsaRunner = runner if runner is not None else OsascriptRunner()
        self.timeout_s = timeout_s

    async def send(self, handle: str, text: str) -> None:
        """Send ``text`` to ``handle``. Raises :class:`SendError` on any failure."""
        if not _HANDLE.match(handle):
            raise SendError("refusing to send: handle is not an E.164 number or lowercase email")
        if not text:
            raise SendError("refusing to send an empty message")
        try:
            result = await self.runner(send_argv(handle, text), self.timeout_s)
        except TimeoutError:
            raise SendError(
                f"osascript timed out after {self.timeout_s:g}s: Messages.app may be "
                "launching, hung, or waiting on an Automation prompt"
            ) from None
        except OSError as e:
            raise SendError(f"cannot run {OSASCRIPT}: {e.strerror or type(e).__name__}") from None
        if result.returncode != 0:
            raise explain(result.stderr, result.returncode)


__all__ = [
    "AUTOMATION_DENIED",
    "OSASCRIPT",
    "SEND_SCRIPT",
    "SEND_TIMEOUT_S",
    "IMessageSender",
    "OsaResult",
    "OsaRunner",
    "OsascriptRunner",
    "SendError",
    "explain",
    "send_argv",
]

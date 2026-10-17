"""A worker session's stdin while it lives (RC-1): stream-json user messages and control
requests, one writer at a time.

Workers run ``claude -p --input-format stream-json``. The prompt is the first ``user`` line and
stdin stays open for the life of the process, so Resonant can write the owner's messages and
late answers into the running session instead of ending it and resuming with ``--resume``.
The CLI folds a message written mid-turn into the turn in flight (one ``result`` for both,
checked on Claude Code 2.1.299) and, once idle, waits for more input until stdin closes, so
Resonant decides when a session ends (:meth:`LiveInput.end`): Remote Control off (best
effort), then stdin closed, after which the CLI exits once its turn is over.

Control requests are undocumented (Claude Code 2.1.x; see docs/followups.md): a line
``{"type": "control_request", "request_id": …, "request": {"subtype": "remote_control",
"enabled": true}}`` is answered on stdout by ``{"type": "control_response", "response":
{"subtype": "success", "request_id": …, "response": {"session_url": …}}}`` or by ``subtype``
``error`` with an ``error`` string. A CLI that doesn't know the request may never answer, so
every wait is bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass
from typing import Any, cast

from resonant.gate.codes import clean_external

RC_TIMEOUT_S = 15.0  # how long the remote_control request waits for its answer
RC_OFF_TIMEOUT_S = 3.0  # ... and turning it off at the end, best effort
REASON_MAX = 80
# The URL as the CLI gives it: https://claude.ai/code/session_… (anything else is refused, so a
# changed reply can't put an arbitrary link in the owner's texts).
_SESSION_URL = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_-]{1,200}")
_WRITE_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionError, RuntimeError)


def user_line(text: str) -> bytes:
    """One stream-json user message."""
    msg = {"type": "user", "message": {"role": "user", "content": text}}
    return json.dumps(msg).encode() + b"\n"


@dataclass(frozen=True)
class ControlReply:
    """A ``control_response``: ``ok`` with the ``response`` payload, or ``error`` (clipped)."""

    ok: bool
    response: dict[str, Any]
    error: str | None = None


def parse_reply(msg: dict[str, Any]) -> tuple[str, ControlReply] | None:
    """``(request_id, reply)`` for a ``control_response`` line, else None."""
    if msg.get("type") != "control_response" or not isinstance(msg.get("response"), dict):
        return None
    body = cast(dict[str, Any], msg["response"])
    rid = body.get("request_id")
    if not isinstance(rid, str):
        return None
    if body.get("subtype") == "success":
        payload = body.get("response")
        return rid, ControlReply(
            True, cast(dict[str, Any], payload if isinstance(payload, dict) else {})
        )
    error = body.get("error")
    return rid, ControlReply(False, {}, short_reason(error if isinstance(error, str) else ""))


def short_reason(text: str) -> str:
    """An error from the CLI as a short, clean phrase for a text (never the raw payload)."""
    flat = re.sub(r"\s+", " ", clean_external(text)).strip().rstrip(".")
    if not flat:
        return "an error with no reason"
    return flat if len(flat) <= REASON_MAX else flat[: REASON_MAX - 3].rstrip() + "..."


def session_url(reply: ControlReply) -> str | None:
    """The claude.ai URL in a successful ``remote_control`` reply, or None when it has none or
    it doesn't look like one."""
    url = reply.response.get("session_url")
    if isinstance(url, str) and _SESSION_URL.fullmatch(url):
        return url
    return None


class LiveInput:
    """The stdin of one session process. ``lock`` serializes writers: whoever decides to send
    (a turn's end, an owner message) holds it across the look at the store and the write, so
    a message lands exactly once, in the session or (once ``open`` is False) in the store for
    the next turn. ``feed`` takes ``control_response`` lines from the stdout reader."""

    def __init__(self, stdin: asyncio.StreamWriter) -> None:
        self._stdin = stdin
        self.lock = asyncio.Lock()
        self.open = True  # user messages are still welcome (False once the session is ending)
        self.closed = False  # stdin is closed (or broken)
        self.remote = False  # Remote Control was asked for: turn it off at the end
        self.sent = 0  # user messages written
        self._pending: dict[str, asyncio.Future[ControlReply]] = {}
        self._n = 0

    async def _write(self, data: bytes) -> bool:
        if self.closed:
            return False
        try:
            self._stdin.write(data)
            await self._stdin.drain()
        except _WRITE_ERRORS:
            self.closed = True
            return False
        return True

    async def write_user(self, text: str) -> bool:
        """A user message; the caller holds ``lock``. False when stdin is gone."""
        assert self.lock.locked()
        ok = await self._write(user_line(text))
        self.sent += ok
        return ok

    async def control(self, request: dict[str, Any]) -> asyncio.Future[ControlReply] | None:
        """Write a control request; the caller holds ``lock``. The future gets its reply (see
        :meth:`reply`); None when stdin is gone."""
        assert self.lock.locked()
        self._n += 1
        rid = f"resonant-{self._n}"
        fut: asyncio.Future[ControlReply] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        line = {"type": "control_request", "request_id": rid, "request": request}
        if not await self._write(json.dumps(line).encode() + b"\n"):
            self._pending.pop(rid, None)
            return None
        fut.add_done_callback(lambda _f, r=rid: self._pending.pop(r, None))
        return fut

    @staticmethod
    async def reply(fut: asyncio.Future[ControlReply], wait_s: float) -> ControlReply | None:
        """The reply to a control request, or None when none came within ``wait_s``."""
        try:
            return await asyncio.wait_for(asyncio.shield(fut), wait_s)
        except TimeoutError:
            fut.cancel()
            return None

    def feed(self, msg: dict[str, Any]) -> bool:
        """A stdout line: True when it was a ``control_response`` (resolving its request)."""
        found = parse_reply(msg)
        if found is None:
            return False
        rid, reply = found
        fut = self._pending.get(rid)
        if fut is not None and not fut.done():
            fut.set_result(reply)
        return True

    async def end(self, *, rc_timeout: float = RC_OFF_TIMEOUT_S) -> None:
        """No more user messages; Remote Control off when it was asked for (best effort,
        ``rc_timeout``), then stdin closed. Idempotent; the caller must not hold ``lock``."""
        async with self.lock:
            self.open = False
            fut = None
            if self.remote and not self.closed:
                self.remote = False
                fut = await self.control({"subtype": "remote_control", "enabled": False})
        if fut is not None:
            await self.reply(fut, rc_timeout)
        async with self.lock:
            self.close()

    def close(self) -> None:
        """Close stdin now (no Remote Control off): the process is gone or must stop."""
        self.open = False
        if not self.closed:
            self.closed = True
            with contextlib.suppress(*_WRITE_ERRORS):
                self._stdin.close()
        for fut in list(self._pending.values()):
            fut.cancel()

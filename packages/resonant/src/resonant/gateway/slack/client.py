"""The Slack transport: a Web API caller and a Socket Mode connection, behind two protocols.

``SlackChannel`` talks only to these protocols, so tests drive it with
``gateway.slack.fixtures`` (no network). The real ones wrap ``slack_sdk`` (Socket Mode over
aiohttp); :func:`sdk_transport` builds both from the two tokens. ``slack_sdk`` is imported
lazily, so an instance without ``channels.slack`` never loads it.

- ``SlackWeb.call(method, args)`` returns the response body (``ok`` true) or raises
  :class:`SlackRateLimited` (HTTP 429, with ``Retry-After``) or :class:`SlackCallError`
  (Slack's error code, e.g. ``channel_not_found``). Neither ever carries a token or a body.
- ``SlackSocket.connect(on_envelope)`` opens the websocket (the SDK reconnects on its own,
  and on Slack's ``disconnect``); every envelope (``{envelope_id, type, payload,
  retry_attempt}``) goes to ``on_envelope``, which acks it with ``ack(envelope_id)``.

Tokens: the SDK clients hold them; nothing here logs them. The SDK's own loggers are set to
WARNING (its debug lines would include request URLs and payloads, which carry message text).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

log = logging.getLogger(__name__)

Envelope = dict[str, Any]
EnvelopeHandler = Callable[[Envelope], Awaitable[None]]

_SDK_LOGGER = "resonant.gateway.slack.sdk"


class SlackCallError(Exception):
    """A Web API call failed. ``error`` is Slack's code (or a short reason); never a body."""

    def __init__(self, method: str, error: str) -> None:
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error


class SlackRateLimited(SlackCallError):
    """HTTP 429. ``retry_after`` is the ``Retry-After`` header, in seconds."""

    def __init__(self, method: str, retry_after: float) -> None:
        super().__init__(method, "ratelimited")
        self.retry_after = retry_after


class SlackWeb(Protocol):
    async def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]: ...


class SlackSocket(Protocol):
    async def connect(self, on_envelope: EnvelopeHandler) -> None: ...

    async def ack(self, envelope_id: str) -> None: ...

    async def connected(self) -> bool: ...

    async def close(self) -> None: ...


Transport = Callable[[str, str], tuple[SlackWeb, SlackSocket]]
"""``(bot_token, app_token) -> (web, socket)``."""


# --- the real transport (slack_sdk) ----------------------------------------------------------


def _sdk_logger() -> logging.Logger:
    logger = logging.getLogger(_SDK_LOGGER)
    logger.setLevel(logging.WARNING)
    return logger


class SdkWeb:
    """``SlackWeb`` over ``slack_sdk.web.async_client.AsyncWebClient``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        from slack_sdk.errors import SlackApiError, SlackClientError

        try:
            response = await self._client.api_call(method, json=args)
        except SlackApiError as e:
            resp: Any = e.response
            status = getattr(resp, "status_code", None)
            if status == 429:
                headers: dict[str, str] = getattr(resp, "headers", None) or {}
                raw = headers.get("Retry-After") or headers.get("retry-after") or "30"
                try:
                    retry = float(raw)
                except ValueError:
                    retry = 30.0
                raise SlackRateLimited(method, retry) from None
            error: object = None
            try:
                error = resp["error"]
            except (KeyError, TypeError):
                error = None
            raise SlackCallError(
                method, error if isinstance(error, str) else f"http {status}"
            ) from None
        except SlackClientError as e:
            raise SlackCallError(method, type(e).__name__) from None
        except (OSError, TimeoutError) as e:
            raise SlackCallError(method, f"network ({type(e).__name__})") from None
        data: object = response.data
        return cast(dict[str, Any], data) if isinstance(data, dict) else {}


class SdkSocket:
    """``SlackSocket`` over ``slack_sdk.socket_mode.aiohttp.SocketModeClient``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def connect(self, on_envelope: EnvelopeHandler) -> None:
        from slack_sdk.socket_mode.request import SocketModeRequest

        async def listener(_client: Any, req: SocketModeRequest) -> None:
            payload: object = getattr(req, "payload", None)
            await on_envelope(
                {
                    "envelope_id": req.envelope_id,
                    "type": req.type,
                    "payload": cast(dict[str, Any], payload) if isinstance(payload, dict) else {},
                    "retry_attempt": req.retry_attempt,
                }
            )

        self._client.socket_mode_request_listeners.append(listener)
        await self._client.connect()

    async def ack(self, envelope_id: str) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse

        await self._client.send_socket_mode_response(SocketModeResponse(envelope_id=envelope_id))

    async def connected(self) -> bool:
        return bool(await self._client.is_connected())

    async def close(self) -> None:
        await self._client.close()


def sdk_transport(bot_token: str, app_token: str) -> tuple[SlackWeb, SlackSocket]:
    """The real transport. The tokens live only in the SDK clients from here on."""
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.web.async_client import AsyncWebClient

    logger = _sdk_logger()
    web = AsyncWebClient(token=bot_token, logger=logger, timeout=15)
    socket = SocketModeClient(app_token=app_token, web_client=web, logger=logger)
    return SdkWeb(web), SdkSocket(socket)

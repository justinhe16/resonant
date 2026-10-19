"""MCP servers over streamable HTTP (docs/dashboard-plan.md D8, MCP-2).

The same JSON-RPC session as stdio (``mcp.RpcSession``: handshake, pins, ``list_changed``,
cancellation, ``ping``), carried by HTTP as the MCP spec's streamable HTTP transport
(2025-06-18) defines it:

- Every message the client sends is one ``POST`` to the fixed ``url`` with
  ``Accept: application/json, text/event-stream``. A request's answer is either one JSON
  body or an SSE stream that may carry server requests and notifications before it.
  Notifications and responses we send get ``202 Accepted``.
- ``Mcp-Session-Id`` from the ``initialize`` response goes on every later request, with
  ``MCP-Protocol-Version``. A ``404`` on a request that carried the session id means the
  server dropped the session: nothing ran (``McpSessionExpired``), so the runtime starts a
  new session (re-checking pins) and retries once.
- A ``GET`` stream (when the server offers one) carries ``tools/list_changed`` between calls;
  it is reopened with a backoff while the session lives. ``DELETE`` ends the session.

What it adds over the spec, for an extension's tools:

- **One URL.** https only (``checks.mcp_url_error``); redirects are never followed (a ``3xx``
  is a definite error), and proxies and ``.netrc`` from the environment are ignored
  (``trust_env=False``), so the bearer token only ever goes to the manifest's host.
- **Bearer token.** The declared secret, read from the Keychain at session start like any
  extension secret; sent only as ``Authorization: Bearer``; never logged; every response
  is redacted with it before anything else sees it. A value that isn't a valid header value
  is refused before anything is sent.
- **OAuth** (``auth: oauth``, CAL-1). The declared client id, client secret and refresh
  token are posted (form-encoded, RFC 6749 §6) to the manifest's fixed ``oauth.token_url``
  and nowhere else, under the same rules as ``url`` (https, no redirects, no proxies). The
  access token that comes back lives only in memory (``OAuthToken``), is sent to ``url`` as
  the bearer, is refreshed ``OAUTH_EARLY_S`` before it expires (checked before every
  request) and after a ``401``, and every one issued is redacted from everything the
  server sends. Nothing is written to disk. A refused or unreachable token endpoint is
  definite (``McpError``): nothing reached the MCP server.
- **Caps.** One message (a JSON body or one SSE event) at most ``message_cap`` bytes, one
  ``POST`` response at most ``STREAM_CAP_FACTOR`` times that; past either the call is
  ``McpAmbiguous("output too large")`` and the session is ended. Connect timeout
  ``CONNECT_TIMEOUT_S``; every call is bounded by its tool's ``timeout_s`` and cancelled
  on the server (``notifications/cancelled``) when it runs out.
- **Kill switch.** The session registers a hook on the host's ``ProcessTracker``: ``/kill``
  ends it at once (in-flight calls raise ``McpAmbiguous("killed")``) and, while held, no
  session starts (``ExtFailed("paused")``).
- **Outcomes.** Not connected (DNS, refused, TLS, connect timeout), ``401``/``403``,
  another ``4xx`` or a redirect: definite (``McpError``), nothing ran. A ``5xx``, a dropped
  connection or a malformed answer after the request went out: ``McpAmbiguous``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import httpx

from resonant import __version__
from resonant.extensions.checks import mcp_url_error
from resonant.extensions.mcp import (
    START_TIMEOUT_S,
    McpAmbiguous,
    McpError,
    McpSessionExpired,
    RpcSession,
)
from resonant.extensions.process import ExtFailed, ProcessTracker, RedactFn
from resonant.redact import Redactor

log = logging.getLogger(__name__)

HTTP_MESSAGE_CAP = 1024 * 1024  # one JSON body or SSE event
STREAM_CAP_FACTOR = 4  # one POST response, in messages
CONNECT_TIMEOUT_S = 10.0
CLOSE_TIMEOUT_S = 2.0  # the DELETE on close
SEND_TIMEOUT_S = 10.0  # a notification or our answer to a server request (202 expected)
LISTEN_STABLE_S = 30.0  # a GET stream that lived this long resets the reopen backoff
SESSION_ID_MAX = 256
LISTEN_BACKOFF_S = (1.0, 5.0, 30.0, 60.0)


OAUTH_TIMEOUT_S = 10.0  # one token request, connect to last byte
OAUTH_BODY_CAP = 64 * 1024
OAUTH_EARLY_S = 120.0  # refresh this long before the access token expires
OAUTH_TTL_DEFAULT_S = 3600.0  # no expires_in in the answer
OAUTH_TTL_MIN_S, OAUTH_TTL_MAX_S = 60.0, 86400.0
OAUTH_REDACT_KEEP = 8  # issued access tokens remembered for redaction
TOKEN_MAX = 4096
# RFC 6749 §5.2 error codes: the only server text that is ever shown (they are code-like).
OAUTH_ERRORS = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)


class _TooLarge(Exception):
    pass


def _valid_header(value: str, limit: int) -> bool:
    return 0 < len(value) <= limit and all(0x21 <= ord(c) <= 0x7E for c in value)


@dataclass(frozen=True)
class OAuthCredentials:
    """What a refresh-token grant posts: read from the Keychain, never logged (``repr``
    shows only the URL)."""

    token_url: str
    client_id: str
    client_secret: str
    refresh_token: str

    def __repr__(self) -> str:
        return f"OAuthCredentials(token_url={self.token_url!r})"


class OAuthToken:
    """Short-lived access tokens from one refresh token (``auth: oauth``). ``get`` returns
    the cached one until ``OAUTH_EARLY_S`` before it expires, else posts the grant to
    ``token_url``. Raises ``McpError`` (definite: nothing reached the MCP server) with
    code-built text: the endpoint's own text never surfaces, only an RFC 6749 error code.
    ``text``/``deep`` redact every access token issued (the last ``OAUTH_REDACT_KEEP``)."""

    def __init__(
        self,
        creds: OAuthCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.creds = creds
        self._transport = transport  # tests only
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0
        self._issued: list[str] = []
        self._redactor = Redactor()
        self.fetches = 0  # token requests made (tests)

    def __repr__(self) -> str:
        return f"OAuthToken(token_url={self.creds.token_url!r})"

    async def get(self) -> str:
        async with self._lock:
            if self._token is not None and self._clock() < self._expires_at - OAUTH_EARLY_S:
                return self._token
            token, ttl = await self._fetch()
            self._token, self._expires_at = token, self._clock() + ttl
            self._issued = [*self._issued, token][-OAUTH_REDACT_KEEP:]
            self._redactor = Redactor(self._issued)
            return token

    def expire(self, token: str | None) -> None:
        """``token`` was refused: the next ``get`` fetches a new one."""
        if token is not None and token == self._token:
            self._token = None

    def text(self, text: str) -> str:
        return self._redactor.text(text)

    def deep[T](self, obj: T) -> T:
        return self._redactor.deep(obj)

    async def _fetch(self) -> tuple[str, float]:
        url = self.creds.token_url
        if err := mcp_url_error(url):
            raise McpError(f"oauth token_url refused: {err}")
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self.creds.refresh_token,
            "client_id": self.creds.client_id,
            "client_secret": self.creds.client_secret,
        }
        self.fetches += 1
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(OAUTH_TIMEOUT_S),
                transport=self._transport,
                headers={"User-Agent": f"resonant/{__version__}"},
            ) as client:
                req = client.build_request(
                    "POST", url, data=form, headers={"Accept": "application/json"}
                )
                async with asyncio.timeout(OAUTH_TIMEOUT_S):
                    resp = await client.send(req, stream=True)
                    try:
                        body = bytearray()
                        async for chunk in resp.aiter_bytes():
                            body += chunk
                            if len(body) > OAUTH_BODY_CAP:
                                raise McpError("the OAuth token endpoint's answer is too large")
                    finally:
                        await resp.aclose()
        except McpError:
            raise
        except (httpx.HTTPError, TimeoutError, OSError):
            raise McpError("the OAuth token endpoint is not reachable") from None
        return _token_answer(resp.status_code, bytes(body))


def _token_answer(status: int, body: bytes) -> tuple[str, float]:
    """``(access token, lifetime in s)`` from a token endpoint's answer, or ``McpError``."""
    if 300 <= status < 400:
        raise McpError(
            f"the OAuth token endpoint redirected (http {status}); redirects are refused"
        )
    parsed: Any = None
    with contextlib.suppress(ValueError):
        parsed = json.loads(body)
    data = cast(dict[str, object], parsed) if isinstance(parsed, dict) else None
    if status != 200:
        code = data.get("error") if data is not None else None
        if isinstance(code, str) and code in OAUTH_ERRORS:
            raise McpError(f"the OAuth token endpoint refused the refresh token ({code})")
        raise McpError(f"the OAuth token endpoint failed (http {status})")
    if data is None:
        raise McpError("the OAuth token endpoint sent a malformed answer")
    token, kind, ttl = data.get("access_token"), data.get("token_type"), data.get("expires_in")
    if not isinstance(token, str) or not _valid_header(token, TOKEN_MAX):
        raise McpError("the OAuth token endpoint sent no usable access token")
    if kind is not None and (not isinstance(kind, str) or kind.lower() != "bearer"):
        raise McpError("the OAuth token endpoint sent a token that isn't a bearer token")
    if isinstance(ttl, bool) or not isinstance(ttl, int | float):
        seconds = OAUTH_TTL_DEFAULT_S
    else:
        seconds = min(max(float(ttl), OAUTH_TTL_MIN_S), OAUTH_TTL_MAX_S)
    return token, seconds


class McpHttpSession(RpcSession):
    """One MCP session over streamable HTTP. Not restartable: make a new one."""

    def __init__(
        self,
        url: str,
        *,
        token: str | None,
        label: str,
        tracker: ProcessTracker,
        redact: RedactFn,
        message_cap: int = HTTP_MESSAGE_CAP,
        on_list_changed: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
        oauth: OAuthToken | None = None,
    ) -> None:
        if oauth is not None and token is not None:
            raise ValueError("a session's token is static or OAuth, not both")

        def redact_all(text: str) -> str:
            return oauth.text(redact(text)) if oauth is not None else redact(text)

        super().__init__(
            label=label,
            redact=redact_all,
            message_cap=message_cap,
            on_list_changed=on_list_changed,
        )
        self.url = url
        self.tracker = tracker
        self._token = token
        self._oauth = oauth
        self._transport = transport  # tests only: an in-process transport
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._posts: dict[int, asyncio.Task[None]] = {}
        self._listener: asyncio.Task[None] | None = None
        self._killed: str | None = None

    async def start(self, timeout_s: float = START_TIMEOUT_S) -> dict[str, Any]:
        """Connect and run the ``initialize`` handshake. Raises ``ExtFailed`` (paused) or
        ``McpError``; nothing is left open."""
        if self.tracker.paused:
            raise ExtFailed("paused")
        if err := mcp_url_error(self.url):
            raise McpError(f"mcp url refused: {err}")
        if self._token is not None and not _valid_header(self._token, TOKEN_MAX):
            raise McpError("the bearer token is not a valid header value")
        if self._oauth is not None:
            self._token = await self._oauth.get()  # McpError: nothing was sent to url
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            # No read/write timeout of httpx's own: every request is bounded by its caller
            # (a call by its tool's timeout_s, a notification by SEND_TIMEOUT_S, the GET
            # stream by nothing), so a slow tool isn't cut off at the start timeout.
            timeout=httpx.Timeout(None, connect=CONNECT_TIMEOUT_S),
            transport=self._transport,
            headers={"User-Agent": f"resonant/{__version__}"},
        )
        self._started = True
        self.tracker.add_hook(self._kill)
        try:
            result = await self._handshake(timeout_s)
            self._listener = asyncio.create_task(self._listen())
            return result
        except BaseException:
            await self.close("start failed")
            raise

    # --- transport ---------------------------------------------------------------------------

    async def _fresh(self) -> None:
        """An OAuth session's access token, refreshed when it is about to expire (cached
        otherwise: no request)."""
        if self._oauth is not None:
            self._token = await self._oauth.get()

    def _headers(self, accept: str) -> dict[str, str]:
        h = {"Accept": accept}
        if self._token is not None:
            h["Authorization"] = f"Bearer {self._token}"
        if self._session_id is not None:
            h["Mcp-Session-Id"] = self._session_id
        if self.protocol_version is not None:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._client is None or self._ended is not None:
            raise McpError("server is not running")
        body = json.dumps(msg, separators=(",", ":"), allow_nan=False).encode()
        rid = msg.get("id")
        if "method" in msg and isinstance(rid, int):
            fut = self._pending.get(rid)
            if fut is None:
                return
            task = asyncio.create_task(self._post(body, rid, fut, msg["method"]))
            self._posts[rid] = task
            task.add_done_callback(lambda _t, rid=rid: self._posts.pop(rid, None))
            return
        try:  # a notification or our response to a server request: 202 Accepted
            status = await asyncio.wait_for(self._post_oneway(body), SEND_TIMEOUT_S)
        except (httpx.HTTPError, TimeoutError):
            raise McpError("server is not reachable") from None
        if status >= 300:
            raise McpError(f"server refused a notification (http {status})")

    async def _post_oneway(self, body: bytes) -> int:
        """POST a message that gets no answer; its body is never read (no cap needed)."""
        assert self._client is not None
        await self._fresh()
        headers = {
            **self._headers("application/json, text/event-stream"),
            "Content-Type": "application/json",
        }
        async with self._client.stream("POST", self.url, content=body, headers=headers) as resp:
            return resp.status_code

    async def _post(
        self, body: bytes, rid: int, fut: asyncio.Future[dict[str, Any]], method: str
    ) -> None:
        """One request's POST: its answer goes to ``fut`` (through ``_dispatch``), or the
        failure, definite or not, as an exception."""
        assert self._client is not None
        had_session = self._session_id is not None
        try:
            await self._fresh()
        except McpError as e:  # nothing was sent to the server
            if not fut.done():
                fut.set_exception(e)
            return
        sent_token = self._token
        headers = {
            **self._headers("application/json, text/event-stream"),
            "Content-Type": "application/json",
        }
        failure: BaseException | None = None
        try:
            async with self._client.stream("POST", self.url, content=body, headers=headers) as resp:
                failure = self._status_failure(resp, had_session)
                if resp.status_code == 401 and self._oauth is not None:
                    self._oauth.expire(sent_token)  # the next request gets a new one
                    if had_session:  # refused, so nothing ran: a new session retries once
                        failure = McpSessionExpired("access token refused")
                if failure is None:
                    if method == "initialize":
                        failure = self._take_session_id(resp)
                    if failure is None:
                        await self._consume(resp, fut)
                        if not fut.done():
                            failure = McpAmbiguous("malformed response")
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.UnsupportedProtocol,
        ):
            failure = McpError("server is not reachable")
        except _TooLarge:
            failure = McpAmbiguous("output too large")
        except httpx.HTTPError:
            failure = McpAmbiguous("connection lost")
        except Exception:  # anything else after sending: never leave the call hanging
            log.warning("mcp http request failed", extra={"label": self.label}, exc_info=False)
            failure = McpAmbiguous("connection lost")
        if failure is not None and not fut.done():
            fut.set_exception(failure)
        ended: str | None = None
        if isinstance(failure, McpSessionExpired):
            ended = "session expired"
        elif isinstance(failure, McpAmbiguous) and failure.reason == "output too large":
            ended = "output too large"
        if ended is not None:  # over now, so the next call starts a new session
            self._end(ended)
            self._spawn(self.close(ended))

    def _status_failure(self, resp: httpx.Response, had_session: bool) -> BaseException | None:
        code = resp.status_code
        if code == 200:
            return None
        if code in (401, 403):
            return McpError(f"the server refused the token (http {code})")
        if code == 404 and had_session:
            return McpSessionExpired("session expired")
        if 300 <= code < 400:
            return McpError(f"the server redirected (http {code}); redirects are refused")
        if 400 <= code < 500:
            return McpError(f"the server refused the request (http {code})")
        if code >= 500:
            return McpAmbiguous(f"server error (http {code})")
        return McpAmbiguous(f"unexpected http {code}")

    def _take_session_id(self, resp: httpx.Response) -> BaseException | None:
        sid = resp.headers.get("mcp-session-id")
        if sid is None:
            return None
        if not _valid_header(sid, SESSION_ID_MAX):
            return McpError("the server sent a malformed session id")
        self._session_id = sid
        return None

    async def _consume(self, resp: httpx.Response, fut: asyncio.Future[dict[str, Any]]) -> None:
        kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind == "application/json":
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > self.message_cap:
                    raise _TooLarge
            self._dispatch(bytes(body))
        elif kind == "text/event-stream":
            await self._read_events(resp, self.message_cap * STREAM_CAP_FACTOR, until=fut)

    async def _read_events(
        self,
        resp: httpx.Response,
        total_cap: int | None,
        until: asyncio.Future[dict[str, Any]] | None = None,
    ) -> None:
        """Dispatch each SSE event's data until the stream ends (or ``until`` resolves).
        Raises ``_TooLarge`` past ``message_cap`` for one event or ``total_cap`` overall."""
        buf = bytearray()
        data: list[bytes] = []
        size = 0
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total_cap is not None and total > total_cap:
                raise _TooLarge
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    if len(buf) > self.message_cap:
                        raise _TooLarge
                    break
                line = bytes(buf[:nl]).rstrip(b"\r")
                del buf[: nl + 1]
                if not line:  # end of one event
                    if data:
                        self._dispatch(b"\n".join(data))
                    data, size = [], 0
                    if until is not None and until.done():
                        return
                    continue
                if line.startswith(b"data:"):
                    value = line[5:]
                    value = value[1:] if value.startswith(b" ") else value
                    size += len(value) + 1  # empty data: lines count too
                    if size > self.message_cap:
                        raise _TooLarge
                    data.append(value)
                # event:, id:, retry: and comments are ignored: every event is a message
        if data:
            self._dispatch(b"\n".join(data))

    async def _listen(self) -> None:
        """The server's GET stream, for messages between calls (``list_changed``). Reopened
        with a backoff while the session lives; given up when the server doesn't offer it."""
        attempt = 0
        loop = asyncio.get_running_loop()
        while self.alive and self._client is not None:
            opened = loop.time()
            try:
                await self._fresh()
                async with self._client.stream(
                    "GET",
                    self.url,
                    headers=self._headers("text/event-stream"),
                    timeout=httpx.Timeout(None, connect=CONNECT_TIMEOUT_S),
                ) as resp:
                    kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if resp.status_code != 200 or kind != "text/event-stream":
                        return  # 405: no GET stream; anything else: not worth retrying
                    await self._read_events(resp, None)
            except asyncio.CancelledError:
                raise
            except _TooLarge:
                self._spawn(self.close("output too large"))
                return
            except (httpx.HTTPError, McpError):
                pass
            except Exception:
                log.warning("mcp http listener failed", extra={"label": self.label})
            if loop.time() - opened >= LISTEN_STABLE_S:
                attempt = 0  # it lived a while: reopen quickly
            delay = LISTEN_BACKOFF_S[min(attempt, len(LISTEN_BACKOFF_S) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _cancel(self, rid: int, reason: str) -> None:
        task = self._posts.pop(rid, None)
        if task is not None:
            task.cancel()
        await super()._cancel(rid, reason)

    def _kill_reason(self) -> str | None:
        return self._killed

    async def _kill(self, reason: str) -> None:
        self._killed = self._killed or reason
        await self.close(reason)

    async def _close(self, reason: str) -> None:
        self._end(reason)
        self.tracker.remove_hook(self._kill)
        tasks = [*self._posts.values(), *self._side]
        if self._listener is not None:
            tasks.append(self._listener)
        current = asyncio.current_task()
        tasks = [t for t in tasks if t is not current]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        client = self._client
        if client is None:
            return
        # No DELETE after a kill, or for a session the server already dropped.
        if self._session_id is not None and self._killed is None and reason != "session expired":
            with contextlib.suppress(httpx.HTTPError, TimeoutError):
                await asyncio.wait_for(self._delete(client), CLOSE_TIMEOUT_S)
        await client.aclose()

    async def _delete(self, client: httpx.AsyncClient) -> None:
        async with client.stream("DELETE", self.url, headers=self._headers("application/json")):
            pass  # the body is never read


async def discover_http(
    url: str,
    token: str | OAuthCredentials | None,
    *,
    label: str = "mcp",
    timeout_s: float = START_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Connect once, list the server's tools, end the session (``ext add``). ``token``: a
    static bearer, or OAuth credentials exchanged at their ``token_url`` first."""
    tracker = ProcessTracker(sample_s=0.5, grace_s=1.0)
    oauth = None
    if isinstance(token, OAuthCredentials):
        oauth, token = OAuthToken(token, transport=transport), None
    session = McpHttpSession(
        url,
        token=token,
        label=label,
        tracker=tracker,
        redact=lambda t: t,
        transport=transport,
        oauth=oauth,
    )
    try:
        await session.start(timeout_s)
        return await session.list_tools(timeout_s)
    finally:
        await session.close()


__all__ = [
    "HTTP_MESSAGE_CAP",
    "McpHttpSession",
    "OAuthCredentials",
    "OAuthToken",
    "discover_http",
]

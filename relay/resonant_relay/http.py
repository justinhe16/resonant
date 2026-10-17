"""A small outbound HTTPS client: no redirects (a redirect could carry a bearer token or
a secret URL somewhere else), a timeout and a response size cap."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class HttpError(Exception):
    """A failed request. The message names the failure (status, timeout, ...) and never
    the URL or headers, which may hold secrets."""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except ValueError as e:
            raise HttpError("response is not JSON") from e


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect())

MAX_BODY = 256 * 1024


def send(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float = 10.0,
    method: str = "POST",
) -> Response:
    """Sends a request; any non-2xx answer raises HttpError('HTTP <status>'). URLs are
    checked to be https when the settings load (config.load)."""
    if not url.startswith(("https://", "http://")):
        raise HttpError("refusing a non-http(s) URL")
    req = urllib.request.Request(  # noqa: S310 - scheme checked above
        url,
        data=body if method != "GET" else None,
        headers={"User-Agent": "resonant-relay", **headers},
        method=method,
    )
    try:
        with _opener.open(req, timeout=timeout) as res:
            data = res.read(MAX_BODY + 1)
            status = int(res.status)
    except urllib.error.HTTPError as e:
        raise HttpError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise HttpError(f"network error ({type(e.reason).__name__})") from None
    except TimeoutError:
        raise HttpError("timed out") from None
    except (OSError, ValueError) as e:  # ValueError: http.client.InvalidURL
        raise HttpError(f"network error ({type(e).__name__})") from None
    if len(data) > MAX_BODY:
        raise HttpError("response too large")
    if not 200 <= status < 300:
        raise HttpError(f"HTTP {status}")
    return Response(status, data)

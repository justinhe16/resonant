"""The checker's read path: the latest snapshot's `taken_at` and health states, nothing more.

`StatusSource` is the interface; the checker never sees how Supabase is reached. Two
implementations, chosen by RELAY_READ (relay/README.md, "The checker's read path"):

- `RpcSource` (default, least privilege): calls `public.relay_status(token text)`, a
  SECURITY DEFINER function that checks the token and returns only
  `{"taken_at": ..., "health": [{"name": ..., "state": ...}]}` or null.
- `TableSource`: reads the newest `snapshots` row with a key whose role can SELECT it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlencode

from resonant_relay import http


class ReadError(Exception):
    """Supabase couldn't be read or answered something unexpected. Messages are short
    and never contain secrets or response bodies."""


@dataclass(frozen=True)
class Status:
    taken_at: datetime
    failing: tuple[str, ...]  # names of health rows whose state is "failing"


class StatusSource(Protocol):
    def latest(self) -> Status | None:
        """The newest snapshot's status, None when there are no snapshots at all.
        Raises ReadError when it can't tell."""
        ...


Sender = Callable[..., http.Response]


def parse_status(raw: Any) -> Status | None:
    """`{"taken_at": iso8601, "health": [{"name", "state"}, ...]}` (or null) to a Status."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ReadError("unexpected status shape")
    row: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    taken = row.get("taken_at")
    if not isinstance(taken, str):
        raise ReadError("status has no taken_at")
    try:
        taken_at = datetime.fromisoformat(taken.replace("Z", "+00:00"))
    except ValueError:
        raise ReadError("taken_at is not a timestamp") from None
    if taken_at.tzinfo is None:
        raise ReadError("taken_at has no time zone")
    health = row.get("health")
    if health is None:
        health = []
    if not isinstance(health, list):
        raise ReadError("health is not a list")
    failing: list[str] = []
    for h in health:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(h, dict):
            raise ReadError("health row is not an object")
        item: dict[str, Any] = h  # pyright: ignore[reportUnknownVariableType]
        if item.get("state") == "failing":
            failing.append(str(item.get("name", "?"))[:64])
    return Status(taken_at, tuple(sorted(failing)))


def auth_headers(key: str) -> dict[str, str]:
    """`apikey` always; `Authorization: Bearer` only for a legacy JWT key. The newer
    `sb_publishable_`/`sb_secret_` keys aren't JWTs and go in `apikey` alone."""
    headers = {"apikey": key}
    if key.count(".") == 2:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _call(send: Sender, url: str, body: bytes | None, headers: dict[str, str], method: str) -> Any:
    try:
        return send(url, body or b"", headers, method=method).json()
    except http.HttpError as e:
        raise ReadError(f"Supabase read failed: {e}") from None


class RpcSource:
    """POST /rest/v1/rpc/relay_status with the public anon key for the API gateway and
    the relay's own token as the function's argument."""

    def __init__(self, supabase_url: str, anon_key: str, token: str, send: Sender = http.send):
        self._url = f"{supabase_url}/rest/v1/rpc/relay_status"
        self._anon = anon_key
        self._token = token
        self._send = send

    def latest(self) -> Status | None:
        headers = {
            **auth_headers(self._anon),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps({"token": self._token}).encode()
        return parse_status(_call(self._send, self._url, body, headers, "POST"))


class TableSource:
    """GET the newest `snapshots` row (taken_at and data->health only) with a read key."""

    def __init__(self, supabase_url: str, read_key: str, send: Sender = http.send):
        query = urlencode(
            {"select": "taken_at,health:data->health", "order": "taken_at.desc", "limit": "1"}
        )
        self._url = f"{supabase_url}/rest/v1/snapshots?{query}"
        self._key = read_key
        self._send = send

    def latest(self) -> Status | None:
        headers = {**auth_headers(self._key), "Accept": "application/json"}
        rows = _call(self._send, self._url, None, headers, "GET")
        if not isinstance(rows, list):
            raise ReadError("unexpected snapshots shape")
        items: list[Any] = rows  # pyright: ignore[reportUnknownVariableType]
        return parse_status(items[0]) if items else None

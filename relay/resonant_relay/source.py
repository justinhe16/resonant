"""The checker's read path: each instance's newest `taken_at` and health states, nothing more.

`StatusSource` is the interface; the checker never sees how Supabase is reached. Two
implementations, chosen by RELAY_READ (relay/README.md, "The checker's read path"):

- `RpcSource` (default, least privilege): calls `public.relay_status(token text)`, a
  SECURITY DEFINER function that checks the token and returns only
  `{"instances": [{"instance", "taken_at", "health", "secrets"}, ...], "taken_at", "health"}`
  (PER-304; `secrets` since PER-315) or null. The old shape, `{"taken_at", "health"}` alone,
  still parses, as `resonant`'s, and an entry without `secrets` has none.
- `TableSource`: reads each instance's newest `snapshots` row with a key whose role can
  SELECT it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlencode

from resonant_relay import http


class ReadError(Exception):
    """Supabase couldn't be read or answered something unexpected. Messages are short
    and never contain secrets or response bodies."""


@dataclass(frozen=True)
class Expiry:
    """PER-315: one credential's expiry as the snapshot counted it (names and dates only)."""

    ref: str
    expires_at: datetime
    days_left: int


@dataclass(frozen=True)
class Status:
    taken_at: datetime
    failing: tuple[str, ...]  # names of health rows whose state is "failing"
    # PER-315: the soonest credential expiries (at most 5). Shown, never alerted on here: the
    # daemon alerts, and an expired one already shows as a failing `secrets` health row.
    secrets: tuple[Expiry, ...] = ()


# Instance name -> its newest snapshot's status, None for one that never pushed. An
# instance missing from the map is one Supabase knows nothing about (no ingest token yet):
# the checker treats both as "no snapshots".
Statuses = dict[str, Status | None]

DEFAULT_INSTANCE = "resonant"


class StatusSource(Protocol):
    def latest(self) -> Statuses:
        """Each instance's newest snapshot status; empty when there are no snapshots at
        all. Raises ReadError when it can't tell."""
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
    return Status(taken_at, tuple(sorted(failing)), parse_secrets(row.get("secrets")))


def parse_secrets(raw: Any) -> tuple[Expiry, ...]:
    """`[{"ref", "expires_at", "days_left"}, ...]` (PER-315), at most 5. Absent (an old
    answer) or malformed is none, never a ReadError: the expiries are only shown, and a bad
    one must not stop the dead-man check."""
    if not isinstance(raw, list):
        return ()
    out: list[Expiry] = []
    for it in raw[:5]:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(it, dict):
            continue
        e: dict[str, Any] = it  # pyright: ignore[reportUnknownVariableType]
        ref, at, days = e.get("ref"), e.get("expires_at"), e.get("days_left")
        if not isinstance(ref, str) or not isinstance(at, str) or type(days) is not int:
            continue
        try:
            when = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            continue
        out.append(Expiry(ref[:64], when, days))
    return tuple(out)


def parse_statuses(raw: Any) -> Statuses:
    """`relay_status`'s answer to per-instance statuses. Null is no snapshots at all ({}).
    The PER-304 shape lists `instances`; the old shape (no `instances`) is `resonant`'s."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ReadError("unexpected status shape")
    row: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    if "instances" not in row:
        return {DEFAULT_INSTANCE: parse_status(row)}
    items = row["instances"]
    if not isinstance(items, list):
        raise ReadError("instances is not a list")
    out: Statuses = {}
    for it in items:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(it, dict):
            raise ReadError("instance entry is not an object")
        entry: dict[str, Any] = it  # pyright: ignore[reportUnknownVariableType]
        name = entry.get("instance")
        if not isinstance(name, str) or not name:
            raise ReadError("instance entry has no name")
        out[name[:32]] = None if entry.get("taken_at") is None else parse_status(entry)
    return out


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

    def latest(self) -> Statuses:
        headers = {
            **auth_headers(self._anon),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps({"token": self._token}).encode()
        return parse_statuses(_call(self._send, self._url, body, headers, "POST"))


class TableSource:
    """GET each instance's newest `snapshots` row (taken_at and data->health only) with a
    read key: one request per watched instance, filtered by `instance` (PER-304)."""

    def __init__(
        self,
        supabase_url: str,
        read_key: str,
        instances: Sequence[str] = (DEFAULT_INSTANCE,),
        send: Sender = http.send,
    ):
        self._urls: dict[str, str] = {}
        for name in instances:
            query = urlencode(
                {
                    "select": "taken_at,health:data->health",
                    "instance": f"eq.{name}",
                    "order": "taken_at.desc",
                    "limit": "1",
                }
            )
            self._urls[name] = f"{supabase_url}/rest/v1/snapshots?{query}"
        self._key = read_key
        self._send = send

    def latest(self) -> Statuses:
        headers = {**auth_headers(self._key), "Accept": "application/json"}
        out: Statuses = {}
        for name, url in self._urls.items():
            rows = _call(self._send, url, None, headers, "GET")
            if not isinstance(rows, list):
                raise ReadError("unexpected snapshots shape")
            items: list[Any] = rows  # pyright: ignore[reportUnknownVariableType]
            if items:
                out[name] = parse_status(items[0])
        return out

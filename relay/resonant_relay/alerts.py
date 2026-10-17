"""Alert channels: ntfy (push to the phone) and a generic JSON webhook (plan §1 D6)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from resonant_relay import http
from resonant_relay.source import Sender

Kind = Literal["stale", "failing", "read_error"]
Event = Literal["open", "recovered"]


@dataclass(frozen=True)
class Alert:
    kind: Kind
    event: Event
    title: str
    message: str
    at: datetime


class Notifier(Protocol):
    name: str

    def send(self, alert: Alert) -> None:
        """Delivers the alert or raises http.HttpError."""
        ...


class NtfyNotifier:
    name = "ntfy"

    def __init__(self, base_url: str, topic: str, token: str = "", send: Sender = http.send):
        self._url = f"{base_url}/{topic}"
        self._token = token
        self._send = send

    def send(self, alert: Alert) -> None:
        headers = {
            "Title": alert.title,
            "Priority": "high" if alert.event == "open" else "default",
            "Tags": "rotating_light" if alert.event == "open" else "white_check_mark",
            "Content-Type": "text/plain; charset=utf-8",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._send(self._url, alert.message.encode(), headers)


class WebhookNotifier:
    name = "webhook"

    def __init__(self, url: str, send: Sender = http.send):
        self._url = url
        self._send = send

    def send(self, alert: Alert) -> None:
        body = {
            "source": "resonant-relay",
            "kind": alert.kind,
            "event": alert.event,
            "title": alert.title,
            "message": alert.message,
            "at": alert.at.isoformat(),
        }
        self._send(self._url, json.dumps(body).encode(), {"Content-Type": "application/json"})

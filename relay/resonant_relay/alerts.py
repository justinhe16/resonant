"""Alert channels: ntfy (push to the phone), a generic JSON webhook (plan §1 D6) and a Slack
Incoming Webhook for one instance (PER-305: Cori's alerts in #voyager-pulse)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from resonant_relay import http
from resonant_relay.source import DEFAULT_INSTANCE, Sender

Kind = Literal["stale", "failing", "read_error"]
Event = Literal["open", "recovered"]


@dataclass(frozen=True)
class Alert:
    kind: Kind
    event: Event
    title: str
    message: str
    at: datetime
    # The instance the incident is about; None for read_error (about the relay itself).
    instance: str | None = None
    # One code-built line naming the instance ("Cori: no snapshot for 7m (last 14:02 UTC)"),
    # what Slack posts. Empty means the title is used.
    summary: str = ""


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
        # Only another instance's alerts carry it, so personal's body is exactly as before.
        if alert.instance is not None and alert.instance != DEFAULT_INSTANCE:
            body["instance"] = alert.instance
        self._send(self._url, json.dumps(body).encode(), {"Content-Type": "application/json"})


class SlackWebhookNotifier:
    """POSTs `{"text": "🔴 Cori: …"}` to one instance's Slack Incoming Webhook. The URL is a
    secret (config.check_slack_webhook checked it); it is never logged, and http.HttpError
    messages never contain it."""

    def __init__(self, instance: str, url: str, send: Sender = http.send):
        self.name = f"slack:{instance}"
        self._url = url
        self._send = send

    def send(self, alert: Alert) -> None:
        mark = "🔴" if alert.event == "open" else "✅"
        text = f"{mark} {alert.summary or alert.title}"
        self._send(
            self._url,
            json.dumps({"text": text}).encode(),
            {"Content-Type": "application/json; charset=utf-8"},
        )

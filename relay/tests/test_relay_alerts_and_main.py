"""Alert payloads, log redaction and the entry point's config failure."""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from resonant_relay import http
from resonant_relay.__main__ import RedactFilter, build_checker, build_notifiers, build_routes, main
from resonant_relay.alerts import Alert, NtfyNotifier, SlackWebhookNotifier, WebhookNotifier
from resonant_relay.config import load

AT = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


class Capture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str], **_: Any) -> http.Response:
        self.calls.append((url, body, headers))
        return http.Response(200, b"{}")


def test_ntfy_payload():
    cap = Capture()
    NtfyNotifier("https://ntfy.sh", "my-topic", "tk", send=cap).send(
        Alert("stale", "open", "Resonant: no snapshots", "Snapshots stopped.", AT)
    )
    url, body, headers = cap.calls[0]
    assert url == "https://ntfy.sh/my-topic"
    assert body == b"Snapshots stopped."
    assert headers["Title"] == "Resonant: no snapshots"
    assert headers["Priority"] == "high"
    assert headers["Authorization"] == "Bearer tk"


def test_ntfy_without_token_and_recovery_priority():
    cap = Capture()
    NtfyNotifier("https://ntfy.sh", "t", send=cap).send(Alert("stale", "recovered", "x", "y", AT))
    assert "Authorization" not in cap.calls[0][2]
    assert cap.calls[0][2]["Priority"] == "default"


def test_webhook_payload():
    cap = Capture()
    WebhookNotifier("https://hooks.example/x", send=cap).send(
        Alert("failing", "recovered", "t", "m", AT)
    )
    _, body, headers = cap.calls[0]
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {
        "source": "resonant-relay",
        "kind": "failing",
        "event": "recovered",
        "title": "t",
        "message": "m",
        "at": "2026-10-16T12:00:00+00:00",
    }


def test_both_channels():
    s = load(
        {
            "SUPABASE_URL": "https://p.supabase.co",
            "SUPABASE_ANON_KEY": "sb_publishable_x",
            "RELAY_STATUS_TOKEN": "t",
            "NTFY_TOPIC": "x",
            "WEBHOOK_URL": "https://hooks.example/y",
        }
    )
    assert [n.name for n in build_notifiers(s)] == ["ntfy", "webhook"]


def test_redact_filter_scrubs_secrets_and_tracebacks():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactFilter(["topic-secret", "abc"]))
    logger = logging.getLogger("test.redact")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        try:
            raise ValueError("https://ntfy.sh/topic-secret")
        except ValueError:
            logger.exception("sending to %s failed", "https://ntfy.sh/topic-secret")
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    assert "topic-secret" not in out
    assert "[redacted]" in out
    assert "Traceback" not in out


def test_main_refuses_bad_config_without_echoing_secrets(capsys: pytest.CaptureFixture[str]):
    code = main({"SUPABASE_URL": "https://p.supabase.co", "SUPABASE_ANON_KEY": "sb_secret_zzz"})
    assert code == 2
    err = capsys.readouterr().err
    assert "not an anon" in err and "sb_secret_zzz" not in err


def test_slack_payload_is_the_code_built_line():
    cap = Capture()
    slack = SlackWebhookNotifier("cori", "https://hooks.slack.com/services/x", send=cap)
    assert slack.name == "slack:cori"
    slack.send(Alert("stale", "open", "t", "m", AT, "cori", "Cori: no snapshot for 7m"))
    slack.send(Alert("stale", "recovered", "t", "m", AT, "cori", "Cori: back"))
    assert [json.loads(b) for _, b, _ in cap.calls] == [
        {"text": "🔴 Cori: no snapshot for 7m"},
        {"text": "✅ Cori: back"},
    ]
    assert cap.calls[0][0] == "https://hooks.slack.com/services/x"


def test_webhook_payload_names_the_instance():
    cap = Capture()
    WebhookNotifier("https://hooks.example/x", send=cap).send(
        Alert("stale", "open", "t", "m", AT, "cori")
    )
    assert json.loads(cap.calls[0][1])["instance"] == "cori"
    WebhookNotifier("https://hooks.example/x", send=cap).send(
        Alert("stale", "open", "t", "m", AT, "resonant")
    )
    assert "instance" not in json.loads(cap.calls[1][1])  # personal's body is unchanged


def test_cori_routing_from_settings():
    base = {
        "SUPABASE_URL": "https://p.supabase.co",
        "SUPABASE_ANON_KEY": "sb_publishable_x",
        "RELAY_STATUS_TOKEN": "t",
        "NTFY_TOPIC": "x",
    }
    s = load(
        {
            **base,
            "RELAY_INSTANCES": "resonant,cori",
            "SLACK_WEBHOOK_CORI": "https://hooks.slack.com/services/x",
        }
    )
    assert [n.name for n in build_notifiers(s)] == ["ntfy", "slack:cori"]
    assert build_routes(s) == {"resonant": ["ntfy"], "cori": ["ntfy", "slack:cori"]}
    checker = build_checker(s, build_notifiers(s))
    assert set(checker.told["slack:cori"]) == {("cori", "stale"), ("cori", "failing")}
    assert ("", "read_error") in checker.told["ntfy"]
    # No Slack secret: Cori is watched, alerts go to ntfy, nothing crashes.
    s = load({**base, "RELAY_INSTANCES": "resonant,cori"})
    assert [n.name for n in build_notifiers(s)] == ["ntfy"]
    assert build_routes(s) == {"resonant": ["ntfy"], "cori": ["ntfy"]}

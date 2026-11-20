"""The checker's read path over a fake HTTP sender."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from resonant_relay import http
from resonant_relay.source import (
    ReadError,
    RpcSource,
    Status,
    TableSource,
    parse_status,
    parse_statuses,
)

URL = "https://proj.supabase.co"


class FakeSend:
    def __init__(self, answer: Any = None, error: http.HttpError | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, body: bytes, headers: dict[str, str], method: str = "POST", **_: Any
    ) -> http.Response:
        self.calls.append({"url": url, "body": body, "headers": headers, "method": method})
        if self.error:
            raise self.error
        return http.Response(200, json.dumps(self.answer).encode())


def test_parse_status_lists_only_failing_rows():
    s = parse_status(
        {
            "taken_at": "2026-10-16T12:00:00.123456+00:00",
            "health": [
                {"name": "imessage", "state": "failing"},
                {"name": "brain", "state": "failing"},
                {"name": "ollama", "state": "degraded"},
                {"name": "relay", "state": "unknown"},
                {"name": "db", "state": "ok"},
            ],
        }
    )
    assert s == Status(datetime(2026, 10, 16, 12, 0, 0, 123456, tzinfo=UTC), ("brain", "imessage"))


def test_parse_status_accepts_z_and_null():
    assert parse_status(None) is None
    s = parse_status({"taken_at": "2026-10-16T12:00:00Z", "health": None})
    assert s is not None and s.failing == ()


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"health": []},
        {"taken_at": "yesterday"},
        {"taken_at": "2026-10-16T12:00:00"},  # no time zone
        {"taken_at": "2026-10-16T12:00:00Z", "health": {}},
        {"taken_at": "2026-10-16T12:00:00Z", "health": ["x"]},
    ],
)
def test_parse_status_rejects_bad_shapes(raw: Any):
    with pytest.raises(ReadError):
        parse_status(raw)


def test_rpc_source_posts_the_token_in_the_body_only():
    send = FakeSend({"taken_at": "2026-10-16T12:00:00Z", "health": []})
    s = RpcSource(URL, "anon-key", "relay-token-secret", send=send).latest()
    assert set(s) == {"resonant"}  # the old shape is personal's
    call = send.calls[0]
    assert call["url"] == f"{URL}/rest/v1/rpc/relay_status"
    assert call["method"] == "POST"
    assert json.loads(call["body"]) == {"token": "relay-token-secret"}
    assert call["headers"]["apikey"] == "anon-key"
    assert "Authorization" not in call["headers"]  # not a JWT: apikey only
    assert "relay-token-secret" not in json.dumps(call["headers"])


def test_rpc_source_null_means_no_snapshots():
    assert RpcSource(URL, "anon", "tok", send=FakeSend(None)).latest() == {}


def test_table_source_reads_the_newest_row():
    send = FakeSend(
        [{"taken_at": "2026-10-16T12:00:00Z", "health": [{"name": "brain", "state": "failing"}]}]
    )
    s = TableSource(URL, "h.p.s", send=send).latest()
    personal = s["resonant"]
    assert personal is not None and personal.failing == ("brain",)
    call = send.calls[0]
    assert call["method"] == "GET"
    assert call["url"].startswith(f"{URL}/rest/v1/snapshots?")
    assert "order=taken_at.desc" in call["url"] and "limit=1" in call["url"]
    assert "instance=eq.resonant" in call["url"]
    assert call["headers"]["Authorization"] == "Bearer h.p.s"  # a legacy JWT key


def test_table_source_empty_and_bad():
    assert TableSource(URL, "k", send=FakeSend([])).latest() == {}
    with pytest.raises(ReadError):
        TableSource(URL, "k", send=FakeSend({"not": "a list"})).latest()


@pytest.mark.parametrize("err", ["HTTP 401", "HTTP 500", "timed out", "response is not JSON"])
def test_http_failures_become_read_errors(err: str):
    src = RpcSource(URL, "anon", "tok", send=FakeSend(error=http.HttpError(err)))
    with pytest.raises(ReadError, match=err):
        src.latest()


# --- per instance (PER-304 shape, PER-305) ---------------------------------------------------


def test_parse_statuses_reads_instances_and_ignores_the_old_top_level():
    got = parse_statuses(
        {
            "instances": [
                {"instance": "cori", "taken_at": None, "health": []},
                {
                    "instance": "resonant",
                    "taken_at": "2026-10-16T12:00:00Z",
                    "health": [{"name": "brain", "state": "failing"}],
                },
            ],
            "taken_at": "2026-10-16T11:00:00Z",
            "health": [],
        }
    )
    assert got == {
        "cori": None,
        "resonant": Status(datetime(2026, 10, 16, 12, 0, tzinfo=UTC), ("brain",)),
    }


def test_parse_statuses_old_shape_is_personal():
    assert parse_statuses({"taken_at": "2026-10-16T12:00:00Z", "health": []}) == {
        "resonant": Status(datetime(2026, 10, 16, 12, 0, tzinfo=UTC), ())
    }
    assert parse_statuses(None) == {}


@pytest.mark.parametrize(
    "raw",
    [
        {"instances": {}},
        {"instances": ["cori"]},
        {"instances": [{"taken_at": None}]},
        {"instances": [{"instance": "cori", "taken_at": "yesterday"}]},
    ],
)
def test_parse_statuses_rejects_bad_shapes(raw: Any):
    with pytest.raises(ReadError):
        parse_statuses(raw)


def test_rpc_source_new_shape():
    send = FakeSend(
        {
            "instances": [
                {"instance": "resonant", "taken_at": "2026-10-16T12:00:00Z", "health": []}
            ],
            "taken_at": "2026-10-16T12:00:00Z",
            "health": [],
        }
    )
    assert set(RpcSource(URL, "anon", "tok", send=send).latest()) == {"resonant"}


def test_table_source_reads_each_instance_filtered():
    send = FakeSend([{"taken_at": "2026-10-16T12:00:00Z", "health": []}])
    got = TableSource(URL, "k", ("resonant", "cori"), send=send).latest()
    assert set(got) == {"resonant", "cori"}
    urls = [c["url"] for c in send.calls]
    assert len(urls) == 2
    assert "instance=eq.resonant" in urls[0] and "instance=eq.cori" in urls[1]

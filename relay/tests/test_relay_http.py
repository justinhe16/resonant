"""The outbound client: no redirects, size cap, errors without URLs."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from resonant_relay import http


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        path = self.path.split("?", 1)[0]
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/big":
            body = b"x" * (http.MAX_BODY + 10)
        elif path == "/fail":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def base() -> Iterator[str]:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_ok(base: str):
    assert http.send(f"{base}/ok", b"{}", {}).json() == {"ok": True}


def test_redirects_are_not_followed(base: str):
    with pytest.raises(http.HttpError, match="HTTP 302"):
        http.send(f"{base}/redirect", b"{}", {"Authorization": "Bearer x"})


def test_errors_name_the_status_not_the_url(base: str):
    with pytest.raises(http.HttpError) as e:
        http.send(f"{base}/fail?topic=secret-topic", b"", {})
    assert str(e.value) == "HTTP 500"


def test_body_cap(base: str):
    with pytest.raises(http.HttpError, match="too large"):
        http.send(f"{base}/big", b"", {})


def test_network_errors_hide_the_url():
    with pytest.raises(http.HttpError) as e:
        http.send("http://127.0.0.1:9/secret-path", b"", {}, timeout=2)
    assert "secret" not in str(e.value)


def test_non_json():
    with pytest.raises(http.HttpError, match="not JSON"):
        http.Response(200, b"<html>").json()

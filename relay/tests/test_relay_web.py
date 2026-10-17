"""The static server: generated config.json, security headers everywhere, /healthz."""

from __future__ import annotations

import http.client
import json
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from resonant_relay import web

ORIGIN = "https://proj.supabase.co"
REPO_DIST = Path(__file__).resolve().parents[2] / "dashboard" / "dist"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>R</title>")
    (d / "theme-init.js").write_text("// theme")
    (d / "assets" / "index-abc.js").write_text("console.log(1)")
    (d / "assets" / "index-abc.css").write_text("body{}")
    (d / "config.json").write_text('{"source": "local"}')
    (tmp_path / "secret.txt").write_text("outside dist")
    return d


class Server:
    def __init__(self, dist: Path) -> None:
        self.healthy = True
        site = web.load_site(dist, ORIGIN, "anon-public")
        self.srv = web.make_server(
            0, site, web.security_headers(ORIGIN), lambda: self.healthy, host="127.0.0.1"
        )
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()

    def request(
        self, path: str, method: str = "GET", raw_path: bool = False
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        if raw_path:
            conn.putrequest(method, path, skip_host=False, skip_accept_encoding=True)
            conn.endheaders()
        else:
            conn.request(method, path)
        res = conn.getresponse()
        body = res.read()
        headers = {k.lower(): v for k, v in res.getheaders()}
        conn.close()
        return res.status, headers, body


@pytest.fixture
def server(dist: Path) -> Iterator[Server]:
    s = Server(dist)
    yield s
    s.srv.shutdown()
    s.srv.server_close()


def assert_strict(headers: dict[str, str]) -> None:
    policy = headers["content-security-policy"]
    directives = dict(d.strip().split(" ", 1) for d in policy.split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'"
    assert directives["connect-src"] == f"'self' {ORIGIN}"
    assert directives["frame-ancestors"] == "'none'"
    assert "unsafe" not in policy
    assert headers["strict-transport-security"].startswith("max-age=63072000")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"
    assert headers["server"] == "resonant-relay"


def test_index_and_root(server: Server):
    for path in ("/", "/index.html"):
        status, headers, body = server.request(path)
        assert status == 200 and body.startswith(b"<!doctype html>")
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert headers["cache-control"] == "no-cache"
        assert_strict(headers)


def test_config_json_is_generated_from_env_not_the_committed_file(server: Server):
    status, headers, body = server.request("/config.json?x=1")
    assert status == 200
    assert json.loads(body) == {"source": "supabase", "url": ORIGIN, "anon_key": "anon-public"}
    assert headers["cache-control"] == "no-store"
    assert_strict(headers)


def test_hashed_assets_are_cached_and_typed(server: Server):
    status, headers, _ = server.request("/assets/index-abc.js")
    assert status == 200
    assert headers["content-type"] == "text/javascript; charset=utf-8"
    assert "immutable" in headers["cache-control"]
    _, css, _ = server.request("/assets/index-abc.css")
    assert css["content-type"] == "text/css; charset=utf-8"


@pytest.mark.parametrize(
    "path",
    ["/nope", "/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt", "//etc/passwd"],
)
def test_unknown_paths_404_with_headers(server: Server, path: str):
    status, headers, body = server.request(path, raw_path=True)
    assert status == 404
    assert b"outside dist" not in body
    assert_strict(headers)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_only_get_and_head(server: Server, method: str):
    status, headers, _ = server.request("/", method=method)
    assert status == 405
    assert headers["allow"] == "GET, HEAD"
    assert_strict(headers)


def test_head_has_no_body(server: Server):
    status, headers, body = server.request("/", method="HEAD")
    assert status == 200 and body == b"" and int(headers["content-length"]) > 0


def test_healthz(server: Server):
    status, headers, body = server.request("/healthz")
    assert (status, json.loads(body)) == (200, {"ok": True})
    assert_strict(headers)
    server.healthy = False
    status, _, body = server.request("/healthz")
    assert (status, json.loads(body)) == (503, {"ok": False})


def test_malformed_request_still_gets_headers(server: Server):
    import socket

    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as s:
        headers = b"".join(b"X-%d: y\r\n" % i for i in range(150))
        s.sendall(b"GET / HTTP/1.1\r\n" + headers + b"\r\n")
        reply = s.recv(4096).decode("latin-1").lower()
    assert reply.startswith("http/1.1 431")  # too many headers
    assert "content-security-policy:" in reply


def test_missing_index_fails_at_boot(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        web.load_site(tmp_path, ORIGIN, "k")


def test_symlinks_in_dist_are_not_served(dist: Path, tmp_path: Path):
    (dist / "leak.txt").symlink_to(tmp_path / "secret.txt")
    assert "/leak.txt" not in web.load_site(dist, ORIGIN, "k")


@pytest.mark.skipif(not (REPO_DIST / "index.html").is_file(), reason="dashboard/dist not present")
def test_the_real_dashboard_build_fits_the_csp():
    """dashboard/dist must run under script-src 'self' / style-src 'self': no inline
    scripts, styles or event handlers in index.html, and every referenced file is served."""
    html = (REPO_DIST / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script>"
    assert "<style" not in html and " style=" not in html
    assert not re.search(r"\son[a-z]+=", html), "inline event handler"
    site = web.load_site(REPO_DIST, ORIGIN, "k")
    for ref in re.findall(r'(?:src|href)="\./([^"]+)"', html):
        assert "/" + ref in site, ref

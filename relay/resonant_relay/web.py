"""Serves dashboard/dist (loaded into memory at boot) with a config.json made from the
environment, strict security headers on every response, and /healthz."""

from __future__ import annotations

import json
import logging
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("resonant_relay.web")

MAX_FILES = 500


@dataclass(frozen=True)
class File:
    body: bytes
    content_type: str
    cache: str


def csp(supabase_origin: str) -> str:
    return "; ".join(
        [
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            f"connect-src 'self' {supabase_origin}",
            "manifest-src 'self'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ]
    )


def security_headers(supabase_origin: str) -> dict[str, str]:
    return {
        "Content-Security-Policy": csp(supabase_origin),
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        "X-Robots-Tag": "noindex",
    }


def _type(name: str) -> str:
    known = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
    }
    suffix = Path(name).suffix.lower()
    return known.get(suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def load_site(dist: Path, supabase_origin: str, anon_key: str) -> dict[str, File]:
    """Every file in `dist` by URL path, plus the generated /config.json (the committed
    local-mode config.json in dist is never served). Fails if dist has no index.html."""
    if not (dist / "index.html").is_file():
        raise FileNotFoundError(f"{dist}/index.html is missing (was dashboard/dist copied?)")
    site: dict[str, File] = {}
    for p in sorted(dist.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(dist).as_posix()
        # Vite's hashed assets never change under a name; everything else is re-checked.
        cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
        site["/" + rel] = File(p.read_bytes(), _type(rel), cache)
        if len(site) > MAX_FILES:
            raise ValueError("dist has too many files")
    config = {"source": "supabase", "url": supabase_origin, "anon_key": anon_key}
    site["/config.json"] = File(json.dumps(config).encode(), "application/json", "no-store")
    site["/"] = site["/index.html"]
    return site


def make_handler(
    site: dict[str, File],
    headers: dict[str, str],
    healthy: Callable[[], bool],
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def version_string(self) -> str:
            return "resonant-relay"

        protocol_version = "HTTP/1.1"
        timeout = 10  # seconds a connection may sit idle or trickle a request

        def end_headers(self) -> None:
            # Every response, errors included, carries the security headers.
            for k, v in headers.items():
                self.send_header(k, v)
            super().end_headers()

        def _reply(self, code: int, body: bytes, ctype: str, cache: str, head: bool) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _get(self, head: bool) -> None:
            if self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding"):
                # A GET/HEAD body is never read; close rather than parse it as a request.
                self.close_connection = True
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if path == "/healthz":
                ok = healthy()
                body = json.dumps({"ok": ok}).encode()
                self._reply(200 if ok else 503, body, "application/json", "no-store", head)
                return
            f = site.get(path)
            if f is None:
                self._reply(404, b"not found\n", "text/plain; charset=utf-8", "no-store", head)
                return
            self._reply(200, f.body, f.content_type, f.cache, head)

        def do_GET(self) -> None:
            self._get(head=False)

        def do_HEAD(self) -> None:
            self._get(head=True)

        def _refuse(self) -> None:
            # The body is never read, so the connection can't be reused safely.
            self.close_connection = True
            self.send_response(405)
            self.send_header("Connection", "close")
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse

        def log_message(self, format: str, *args: object) -> None:
            raw = str(getattr(self, "path", "")).split("?", 1)[0][:200]
            # Escaped so a visitor can't put control characters or fake lines in the logs.
            path = raw.encode("unicode_escape").decode("ascii")
            if path != "/healthz":
                log.info("%s %s", getattr(self, "command", "-"), path)

        def log_error(self, format: str, *args: object) -> None:
            log.info("bad request")

    return Handler


def make_server(
    port: int,
    site: dict[str, File],
    headers: dict[str, str],
    healthy: Callable[[], bool],
    host: str = "0.0.0.0",  # noqa: S104 - Fly's proxy reaches the app on its private address
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(site, headers, healthy))
    server.daemon_threads = True
    return server

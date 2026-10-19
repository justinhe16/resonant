"""Resonant's tools as an MCP stdio server for a Claude brain session (BR-10).

Claude Code starts this per session from the ``--mcp-config`` that ``runners/ask_tools.py``
writes: ``python -I <this file>`` with ``RESONANT_MCP_SOCKET`` and ``RESONANT_MCP_TOKEN`` in its
environment. It is a relay and nothing else: it holds no identity, no secrets and no tool
logic. Every ``tools/list`` and ``tools/call`` goes over the per-task unix socket to the daemon,
which checks the token, and runs the call bound to that task's principal, id and trace, through
the gate and the executor. What the daemon answers is what Claude sees.

Standard library only, and run with ``-I``: nothing from the environment, the user site or the
current directory is imported, so it starts fast and a file in Claude's cwd can't shadow a
module.

Wire to the daemon: newline-delimited JSON. First ``{"token": ...}``, answered
``{"ok": true}`` (or the socket closes); then ``{"id", "op": "list"}`` ->
``{"id", "tools": [...]}`` and ``{"id", "op": "call", "name", "args"}`` ->
``{"id", "text", "error": bool}``.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any, cast

PROTOCOL = "2025-06-18"
SOCKET_ENV = "RESONANT_MCP_SOCKET"
TOKEN_ENV = "RESONANT_MCP_TOKEN"  # noqa: S105 - an env var name
LINE_MAX = 4 * 1024 * 1024


class Daemon:
    """The socket to the daemon: one request at a time (Claude waits for each result)."""

    def __init__(self, path: str, token: str) -> None:
        self.path = path
        self.token = token
        self.sock: socket.socket | None = None
        self.buf = b""
        self.n = 0

    def _connect(self) -> socket.socket:
        if self.sock is not None:
            return self.sock
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.path)
        self.sock = sock
        self._send({"token": self.token})
        hello = self._read()
        if hello.get("ok") is not True:
            raise OSError("the daemon refused this session")
        return sock

    def _send(self, obj: dict[str, Any]) -> None:
        assert self.sock is not None
        self.sock.sendall(json.dumps(obj).encode() + b"\n")

    def _read(self) -> dict[str, Any]:
        assert self.sock is not None
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("the daemon closed the session")
            self.buf += chunk
            if len(self.buf) > LINE_MAX:
                raise OSError("reply too long")
        line, self.buf = self.buf.split(b"\n", 1)
        obj: Any = json.loads(line)
        if not isinstance(obj, dict):
            raise OSError("bad reply")
        return cast(dict[str, Any], obj)

    def ask(self, op: str, **kw: Any) -> dict[str, Any]:
        self._connect()
        self.n += 1
        self._send({"id": self.n, "op": op, **kw})
        return self._read()


def out(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle(daemon: Daemon, msg: dict[str, Any]) -> dict[str, Any] | None:
    """The JSON-RPC answer to one message, or None for a notification."""
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return None  # notifications (initialized, cancelled): nothing to answer
    raw: Any = msg.get("params")
    params: dict[str, Any] = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    if method == "initialize":
        version = params.get("protocolVersion")
        result: dict[str, Any] = {
            "protocolVersion": version if isinstance(version, str) else PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "resonant", "version": "1"},
        }
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    try:
        if method == "tools/list":
            reply = daemon.ask("list")
            listed: Any = reply.get("tools")
            tools = cast(list[Any], listed) if isinstance(listed, list) else []
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments")
            reply = daemon.ask("call", name=name, args=args if args is not None else {})
            text = reply.get("text") if isinstance(reply.get("text"), str) else "no result"
            result = {
                "content": [{"type": "text", "text": text}],
                "isError": reply.get("error") is True,
            }
            return {"jsonrpc": "2.0", "id": mid, "result": result}
    except (OSError, ValueError) as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)[:200]}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}}


def main() -> int:
    path, token = os.environ.get(SOCKET_ENV), os.environ.get(TOKEN_ENV)
    if not path or not token:
        sys.stderr.write("resonant mcp: no session\n")
        return 2
    daemon = Daemon(path, token)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg: Any = json.loads(raw)
        except ValueError:
            out({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse"}})
            continue
        if not isinstance(msg, dict):
            continue
        answer = handle(daemon, cast(dict[str, Any], msg))
        if answer is not None:
            out(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())

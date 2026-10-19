"""A fake ``claude -p`` for a Claude brain session (BR-10): scripted evals and e2e tests.

Run as ``python -I fake_claude.py --script PLAN.json [--notes DIR] <the ask runner's argv>``.
It takes the flags ``runners/ask.py`` passes, reads the question on stdin and, like the real
CLI, starts the MCP servers in ``--mcp-config`` itself (here: Resonant's relay,
``brain/mcp_stdio.py``, with the config's command, args and env). Then it acts out the plan:

``{"reads": ["WebFetch"], "calls": [{"name": "add_note", "args": {...}}], "answer": "..."}``

- ``reads``: tools of its own it "uses" first (a ``tool_use`` in the stream, no MCP call): the
  bridge counts them as outside content (taint).
- ``calls``: each is a ``tool_use`` of ``mcp__resonant__<name>`` in the stream, then the MCP
  ``tools/call`` through the relay, then its ``tool_result``; ``"after_read": true`` puts a
  ``WebFetch`` use right before it.
- ``answer``: the final result text; ``{r0}``, ``{r1}``... are replaced by the calls' result
  texts (clipped to 200 characters).

``--notes DIR`` writes ``session.json``: the argv, the prompt, the MCP tool list and every
call's result text (what Claude saw), for tests to check (an approval code must never be in
it). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

BUILTINS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]


class Mcp:
    """One MCP stdio server, started from a config entry."""

    def __init__(self, spec: dict[str, Any]) -> None:
        env = {**os.environ, **cast(dict[str, str], spec.get("env") or {})}
        self.proc = subprocess.Popen(  # noqa: S603 - the config the test wrote
            [spec["command"], *spec.get("args", [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        self.n = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        self.notify("notifications/initialized")

    def notify(self, method: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise OSError("mcp server closed")
        return cast(dict[str, Any], json.loads(line))

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def out(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--notes")
    ap.add_argument("--mcp-config", nargs="+")
    ap.add_argument("--tools", default=",".join(BUILTINS))
    a, _rest = ap.parse_known_args()
    plan: dict[str, Any] = json.loads(Path(a.script).read_text())
    prompt = sys.stdin.read()
    servers: dict[str, Mcp] = {}
    names: list[str] = []
    listed: list[dict[str, Any]] = []
    configs: list[str] = list(a.mcp_config or [])
    for path in configs:
        config = json.loads(Path(path).read_text())
        for name, spec in cast(dict[str, Any], config.get("mcpServers") or {}).items():
            server = Mcp(spec)
            servers[name] = server
            tools = server.request("tools/list", {}).get("result", {}).get("tools", [])
            listed += tools
            names += [f"mcp__{name}__{t['name']}" for t in tools]
    tools = [t for t in a.tools.split(",") if t] + names
    out({"type": "system", "subtype": "init", "session_id": "fake-brain", "tools": tools,
         "mcp_servers": [{"name": n, "status": "connected"} for n in servers],
         "permissionMode": "dontAsk"})  # fmt: skip
    n = 0

    def use(name: str, args: dict[str, Any]) -> str:
        nonlocal n
        n += 1
        out({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{n}", "name": name, "input": args}]}})  # fmt: skip
        return f"t{n}"

    for read in plan.get("reads", []):
        tid = use(read, {})
        out({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "(read)"}]}})  # fmt: skip
    results: list[str] = []
    for call in plan.get("calls", []):
        if call.get("after_read"):
            tid = use("WebFetch", {"url": "https://example.com"})
            out({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "(page)"}]}})  # fmt: skip
        tid = use(f"mcp__resonant__{call['name']}", call.get("args", {}))
        server = servers.get("resonant")
        if server is None:
            text = "no resonant server"
        else:
            reply = server.request(
                "tools/call", {"name": call["name"], "arguments": call.get("args", {})}
            )
            content = reply.get("result", {}).get("content") or [{"text": json.dumps(reply)}]
            text = str(content[0].get("text", ""))
        results.append(text)
        out({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": text}]}})  # fmt: skip
    for server in servers.values():
        server.close()
    answer = str(plan.get("answer", "Done looking."))
    for i, text in enumerate(results):
        answer = answer.replace(f"{{r{i}}}", text[:200])
    if a.notes:
        with open(os.path.join(a.notes, "session.json"), "w") as f:
            json.dump({"argv": sys.argv[1:], "prompt": prompt, "tools": listed,
                       "results": results}, f)  # fmt: skip
    out({"type": "result", "subtype": "success", "is_error": False, "result": answer,
         "num_turns": 1 + len(results), "total_cost_usd": 0.0})  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())

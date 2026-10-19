"""A fake ``claude -p`` for a Claude brain session (BR-10, BR-11): scripted evals and e2e tests.

Run as ``python -I fake_claude.py --script PLAN.json [--notes DIR] <the ask runner's argv>``.
It takes the flags ``runners/ask.py`` passes and, like the real CLI, starts the MCP servers in
``--mcp-config`` itself (here: Resonant's relay, ``brain/mcp_stdio.py``, with the config's
command, args and env). Then it acts out the plan:

``{"reads": ["WebFetch"], "calls": [{"name": "add_note", "args": {...}}], "answer": "..."}``

- ``reads``: tools of its own it "uses" first (a ``tool_use`` in the stream, no MCP call): the
  bridge counts them as outside content (taint).
- ``calls``: each is a ``tool_use`` of ``mcp__resonant__<name>`` in the stream, then the MCP
  ``tools/call`` through the relay, then its ``tool_result``; ``"after_read": true`` puts a
  ``WebFetch`` use right before it.
- ``answer``: the final result text; ``{r0}``, ``{r1}``... are replaced by the calls' result
  texts (clipped to 200 characters). In a session, ``{prev}`` is the previous message this
  session saw (its last line, clipped to 200), ``{n}`` how many it has seen.

**One-shot** (no ``--input-format``): the question is all of stdin, one plan, one result.

**Session** (``--input-format stream-json``, BR-11): stdin stays open; every ``user`` line is
one turn and gets an ``init`` then its own ``result``; ``control_request`` lines get a
``control_response`` (``remote_control`` on: a ``session_url``). ``--session-id ID`` starts a
session; ``--resume ID`` continues one whose messages ``--notes`` kept (unknown: exit 1 at
once, as the CLI does). A plan's ``turns`` (``[{"when": "<text>", ...plan}]``) picks the first
whose ``when`` is in the message (in a session: its last line, the owner's text, not a
primer), else the top-level plan. Extra plan keys: ``"crash":
"once"|"always"`` exits mid-turn after the calls (``once``: only the first time, kept in the
notes), ``"delay": <s>`` waits before answering, ``"hang": true`` never answers, and
``"limit": <epoch or null>`` answers with a usage limit error.

``--notes DIR`` writes ``session.json`` after each turn: the argv, the prompt (this turn's
message), the MCP tool list and every call's result text (what Claude saw), plus in a session
the session id, ``resumed``, the pid and every message so far, for tests to check (an
approval code must never be in it). Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
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


class Fake:
    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        self.servers: dict[str, Mcp] = {}
        self.names: list[str] = []
        self.listed: list[dict[str, Any]] = []
        self.n = 0
        for path in list(a.mcp_config or []):
            config = json.loads(Path(path).read_text())
            for name, spec in cast(dict[str, Any], config.get("mcpServers") or {}).items():
                server = Mcp(spec)
                self.servers[name] = server
                tools = server.request("tools/list", {}).get("result", {}).get("tools", [])
                self.listed += tools
                self.names += [f"mcp__{name}__{t['name']}" for t in tools]
        self.tools = [t for t in a.tools.split(",") if t] + self.names

    def plan(self, message: str) -> dict[str, Any]:
        plan: dict[str, Any] = json.loads(Path(self.a.script).read_text())
        for turn in cast(list[dict[str, Any]], plan.get("turns") or []):
            if str(turn.get("when", "")) in message:
                return turn
        return plan

    def init(self, session_id: str) -> None:
        out({"type": "system", "subtype": "init", "session_id": session_id, "tools": self.tools,
             "mcp_servers": [{"name": n, "status": "connected"} for n in self.servers],
             "permissionMode": "dontAsk"})  # fmt: skip

    def use(self, name: str, args: dict[str, Any]) -> str:
        self.n += 1
        out({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{self.n}", "name": name, "input": args}]}})  # fmt: skip
        return f"t{self.n}"

    def act(self, plan: dict[str, Any]) -> list[str]:
        """The plan's reads and calls; the calls' result texts."""
        for read in plan.get("reads", []):
            tid = self.use(read, {})
            out({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "(read)"}]}})  # fmt: skip
        results: list[str] = []
        for call in plan.get("calls", []):
            if call.get("after_read"):
                tid = self.use("WebFetch", {"url": "https://example.com"})
                out({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tid,
                     "content": "(page)"}]}})  # fmt: skip
            tid = self.use(f"mcp__resonant__{call['name']}", call.get("args", {}))
            server = self.servers.get("resonant")
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
        return results

    @staticmethod
    def answer(plan: dict[str, Any], results: list[str], **fill: str) -> str:
        answer = str(plan.get("answer", "Done looking."))
        for i, text in enumerate(results):
            answer = answer.replace(f"{{r{i}}}", text[:200])
        for key, value in fill.items():
            answer = answer.replace(f"{{{key}}}", value)
        return answer

    def notes(self, **extra: Any) -> None:
        if self.a.notes:
            with open(os.path.join(self.a.notes, "session.json"), "w") as f:
                json.dump({"argv": sys.argv[1:], "tools": self.listed, **extra}, f)

    def close(self) -> None:
        for server in self.servers.values():
            server.close()

    # -- one-shot --

    def one_shot(self) -> int:
        prompt = sys.stdin.read()
        self.init("fake-brain")
        plan = self.plan(prompt)
        results = self.act(plan)
        self.close()
        self.notes(prompt=prompt, results=results)
        out({"type": "result", "subtype": "success", "is_error": False,
             "result": self.answer(plan, results), "num_turns": 1 + len(results),
             "total_cost_usd": 0.0})  # fmt: skip
        return 0

    # -- a session (BR-11) --

    def session(self) -> int:
        a = self.a
        sid = a.resume or a.session_id or "fake-session"
        history = Path(a.notes) / f"history-{sid}.json" if a.notes else None
        messages: list[str] = []
        if a.resume:
            if history is None or not history.exists():
                sys.stderr.write(f"No conversation found with session ID: {sid}\n")
                return 1
            messages = cast(list[str], json.loads(history.read_text()))
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            msg: Any = json.loads(raw)
            if msg.get("type") == "control_request":
                req: dict[str, Any] = msg.get("request") or {}
                resp: dict[str, Any] = {}
                if req.get("subtype") == "remote_control" and req.get("enabled"):
                    resp = {"session_url": f"https://claude.ai/code/session_fake{sid[:8]}"}
                out({"type": "control_response", "response": {"subtype": "success",
                     "request_id": msg.get("request_id"), "response": resp}})  # fmt: skip
                continue
            if msg.get("type") != "user":
                continue
            body: dict[str, Any] = msg.get("message") or {}
            text = str(body.get("content") or "")
            prev = messages[-1].splitlines()[-1][:200] if messages else ""
            messages.append(text)
            if history is not None:
                history.write_text(json.dumps(messages))
            self.init(sid)
            plan = self.plan(text.splitlines()[-1] if text else "")  # the owner's text
            results = self.act(plan)
            self.notes(prompt=text, results=results, session_id=sid, resumed=bool(a.resume),
                       pid=os.getpid(), messages=messages)  # fmt: skip
            crash = plan.get("crash")
            if crash == "always" or (crash == "once" and self._first("crash", plan)):
                os._exit(1)  # mid-turn: no result
            if delay := plan.get("delay"):
                time.sleep(float(delay))
            if plan.get("hang"):
                time.sleep(3600)
            if "limit" in plan:
                reset = plan.get("limit")
                result = "Claude AI usage limit reached" + (f"|{reset}" if reset else "")
                out({"type": "result", "subtype": "error_during_execution", "is_error": True,
                     "result": result})  # fmt: skip
                continue
            answer = self.answer(plan, results, prev=prev, n=str(len(messages)))
            out({"type": "result", "subtype": "success", "is_error": False, "result": answer,
                 "session_id": sid, "num_turns": 1 + len(results),
                 "total_cost_usd": 0.0})  # fmt: skip
        self.close()
        return 0

    def _first(self, what: str, plan: dict[str, Any]) -> bool:
        """True the first time (kept in the notes dir, so a restart doesn't repeat it)."""
        if not self.a.notes:
            return True
        key = hashlib.sha256(str(plan.get("when", "")).encode()).hexdigest()[:12]
        mark = Path(self.a.notes) / f"{what}-{key}"
        if mark.exists():
            return False
        mark.write_text("1")
        return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True)
    ap.add_argument("--notes")
    ap.add_argument("--mcp-config", nargs="+")
    ap.add_argument("--tools", default=",".join(BUILTINS))
    ap.add_argument("--input-format")
    ap.add_argument("--session-id")
    ap.add_argument("--resume")
    a, _rest = ap.parse_known_args()
    fake = Fake(a)
    if a.input_format == "stream-json":
        return fake.session()
    return fake.one_shot()


if __name__ == "__main__":
    sys.exit(main())

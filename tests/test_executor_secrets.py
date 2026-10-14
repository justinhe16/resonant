"""Executor secrets, CallContext and redaction (P3-6, docs/phase3-plan.md D5 / C3)."""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any

import pytest

from resonant.executor import (
    CallContext,
    ContextHandler,
    Executor,
    Handler,
    HandlerKey,
    ToolFailed,
)
from resonant.extensions.process import ExtAmbiguous, ExtFailed
from resonant.gate import Allow, Intent
from resonant.loop import NewTask
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store.tasks import insert_task
from resonant_sdk import Effect, Level, ToolSpec

SECRET = "s3cr3t-Sentinel/Value+42"
READ = ToolSpec(name="peek", effect=Effect.READ, extension="gh", secrets=("token",))
WRITE = ToolSpec(
    name="push", effect=Effect.WRITE, level=Level.L3, extension="gh", secrets=("token",)
)
PLAIN = ToolSpec(name="plain", effect=Effect.READ, extension="gh")


class FakeGate:
    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}

    def allow(self, intent: Intent) -> Allow:
        token = f"tok{len(self.tokens)}"
        self.tokens[token] = intent.hash
        return Allow(token=token)

    def consume(self, token: str, intent_hash: str) -> bool:
        return self.tokens.pop(token, None) == intent_hash


class Recorder(ContextHandler):
    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.seen: list[CallContext] = []

    async def __call__(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        self.seen.append(ctx)
        ctx.events.append({"type": "x", "text": f"event {SECRET}", "dedupe_key": "k"})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def task_id(db: sqlite3.Connection) -> str:
    return insert_task(db, NewTask(kind="k", runner="echo", priority=1))


def keychain(**items: str) -> FakeKeychain:
    return FakeKeychain(items=dict(items))


def build(
    db: sqlite3.Connection,
    handlers: dict[HandlerKey, Handler],
    kc: FakeKeychain | None = None,
    **kw: Any,
) -> tuple[Executor, FakeGate]:
    gate = FakeGate()
    store = SecretStore(kc if kc is not None else keychain(**{"gh/token": SECRET}))
    return Executor(db, gate, handlers, secrets=store, **kw), gate


def dump_db(db: sqlite3.Connection) -> str:
    out: list[str] = []
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        for row in db.execute(f"SELECT * FROM {table}"):
            out.append(json.dumps([str(v) for v in row]))
    return "\n".join(out)


async def test_only_declared_secrets_reach_a_context_handler(
    db: sqlite3.Connection, task_id: str
) -> None:
    h = Recorder({"echo": SECRET, "nested": [{SECRET: "k"}]})
    plain = Recorder({"ok": True})
    kc = keychain(**{"gh/token": SECRET, "gh/other": "other-secret-1"})
    ex, gate = build(db, {("gh", "peek"): h, ("gh", "plain"): plain}, kc)
    i = Intent.for_tool(READ, task_id=task_id, principal="justin", args={})
    res = await ex.execute(i, gate.allow(i), step_no=1)
    assert dict(h.seen[0].secrets) == {"RESONANT_SECRET_TOKEN": SECRET}
    assert h.seen[0].task_id == task_id and h.seen[0].idempotency_key == res.idempotency_key
    assert res.ok and res.data == {"echo": "[redacted]", "nested": [{"[redacted]": "k"}]}
    assert res.events == ({"type": "x", "text": "event [redacted]", "dedupe_key": "k"},)
    j = Intent.for_tool(PLAIN, task_id=task_id, principal="justin", args={})
    assert (await ex.execute(j, gate.allow(j), step_no=2)).ok
    assert dict(plain.seen[0].secrets) == {}
    assert SECRET not in dump_db(db)
    reads = [c.argv[-2] for c in kc.calls]
    assert reads == ["gh/token"]  # one read, only the declared ref, never for PLAIN


async def test_missing_secret_is_definite_with_no_started_row(
    db: sqlite3.Connection, task_id: str
) -> None:
    h = Recorder({"ok": True})
    ex, gate = build(db, {("gh", "push"): h}, keychain())
    i = Intent.for_tool(WRITE, task_id=task_id, principal="justin", args={})
    res = await ex.execute(i, gate.allow(i), step_no=1)
    assert not res.ok and not res.ambiguous and not h.seen
    assert res.notice == (
        "Not done: secret gh/token is missing. On the Mini: resonant secret set gh/token"
    )
    assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    row = db.execute("SELECT action, detail FROM audit_log").fetchone()
    assert row["action"] == "executor.result" and "gh/token is missing" in row["detail"]
    # Set it and retry the same key: it runs (a definite failure may be retried).
    ex.secrets = SecretStore(keychain(**{"gh/token": SECRET}))
    again = await ex.execute(i, gate.allow(i), step_no=1)
    assert again.ok and len(h.seen) == 1


async def test_locked_keychain_is_definite_too(db: sqlite3.Connection, task_id: str) -> None:
    kc = keychain(**{"gh/token": SECRET})
    kc.fail_with = 36
    ex, gate = build(db, {("gh", "push"): Recorder({})}, kc)
    i = Intent.for_tool(WRITE, task_id=task_id, principal="justin", args={})
    res = await ex.execute(i, gate.allow(i), step_no=1)
    assert not res.ok and not res.ambiguous
    assert res.notice is not None and res.notice.startswith("Not done: secret gh/token unavailable")


async def test_no_secret_store_fails_closed(db: sqlite3.Connection, task_id: str) -> None:
    gate = FakeGate()
    ex = Executor(db, gate, {("gh", "push"): Recorder({})})
    i = Intent.for_tool(WRITE, task_id=task_id, principal="justin", args={})
    res = await ex.execute(i, gate.allow(i), step_no=1)
    assert not res.ok and res.notice is not None


async def test_replay_does_not_read_the_keychain(db: sqlite3.Connection, task_id: str) -> None:
    kc = keychain(**{"gh/token": SECRET})
    ex, gate = build(db, {("gh", "push"): Recorder({"ok": 1})}, kc)
    i = Intent.for_tool(WRITE, task_id=task_id, principal="justin", args={})
    assert (await ex.execute(i, gate.allow(i), step_no=1)).ok
    kc.items.clear()
    again = await ex.execute(i, gate.allow(i), step_no=1)
    assert again.replayed and len(kc.calls) == 1


async def test_errors_and_stderr_tail_are_redacted(db: sqlite3.Connection, task_id: str) -> None:
    b64 = base64.b64encode(b"x" + SECRET.encode()).decode()
    fail = Recorder(ExtFailed(f"exit 1 {SECRET}", stderr_tail=f"tail {b64}"))
    amb = Recorder(ExtAmbiguous(f"timeout {SECRET}", stderr_tail=SECRET))
    raw = Recorder(RuntimeError(json.dumps({"v": SECRET})))
    ex, gate = build(db, {("gh", "push"): fail, ("gh", "peek"): amb, ("gh", "p2"): raw})
    i = Intent.for_tool(WRITE, task_id=task_id, principal="justin", args={})
    res = await ex.execute(i, gate.allow(i), step_no=1)
    assert not res.ok and not res.ambiguous and res.error == "ToolFailed: exit 1 [redacted]"
    r = Intent.for_tool(READ, task_id=task_id, principal="justin", args={})
    res2 = await ex.execute(r, gate.allow(r), step_no=2)
    assert not res2.ok and not res2.ambiguous  # a read: definite
    p2 = ToolSpec(name="p2", effect=Effect.WRITE, extension="gh", secrets=("token",))
    w = Intent.for_tool(p2, task_id=task_id, principal="justin", args={})
    res3 = await ex.execute(w, gate.allow(w), step_no=3)
    assert res3.ambiguous and res3.error is not None and SECRET not in res3.error
    text = dump_db(db)
    assert SECRET not in text and b64[4:-4] not in text
    tails = [
        json.loads(r["detail"]).get("stderr_tail")
        for r in db.execute("SELECT detail FROM audit_log WHERE action LIKE 'executor.%'")
    ]
    assert "[redacted]" in [t for t in tails if t]


async def test_plain_handlers_still_work(db: sqlite3.Connection, task_id: str) -> None:
    async def label(args: dict[str, Any]) -> dict[str, Any]:
        return {"labelled": args}

    ex, gate = build(db, {("gh", "plain"): label})
    i = Intent.for_tool(PLAIN, task_id=task_id, principal="justin", args={"x": 1})
    res = await ex.execute(i, gate.allow(i), step_no=1)
    assert res.ok and res.data == {"labelled": {"x": 1}} and res.events == ()


async def test_large_extension_results_are_capped(db: sqlite3.Connection, task_id: str) -> None:
    big = {"items": ["y" * 100] * 100, "s": SECRET}
    ex, gate = build(db, {("gh", "peek"): Recorder(big)}, result_max_bytes=2048)
    i = Intent.for_tool(READ, task_id=task_id, principal="justin", args={})
    res = await ex.execute(i, gate.allow(i), step_no=1)
    assert res.ok and res.data is not None and res.data["truncated"] is True
    assert res.data["bytes"] > 2048 and len(res.data["text"].encode()) <= 2048
    stored = db.execute("SELECT result FROM executions").fetchone()["result"]
    assert len(stored) <= 2048 and SECRET not in stored
    # Builtins are never capped.
    small = Recorder(big)
    builtin = ToolSpec(name="b", effect=Effect.READ)
    ex2, gate2 = build(db, {(None, "b"): small}, result_max_bytes=2048)
    j = Intent.for_tool(builtin, task_id=task_id, principal="justin", args={})
    out = await ex2.execute(j, gate2.allow(j), step_no=2)
    assert out.ok and out.data is not None and "truncated" not in out.data


async def test_tool_failed_from_handler_is_definite(db: sqlite3.Connection, task_id: str) -> None:
    ex, gate = build(db, {("gh", "push"): Recorder(ToolFailed("nope"))})
    i = Intent.for_tool(WRITE, task_id=task_id, principal="justin", args={})
    res = await ex.execute(i, gate.allow(i), step_no=1)
    assert not res.ok and not res.ambiguous and res.notice is None

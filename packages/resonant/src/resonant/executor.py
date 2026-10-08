"""Executor: the only component that performs side effects (and, from Phase 2, reads secrets).

Every execution is keyed by ``idempotency_key(task_id, step_no, intent_hash)`` in the
``executions`` table:
- succeeded: the recorded result is returned and the tool is never run twice.
- started, never finished (a crash mid-call): the outcome is unknown, so it is not re-run
  automatically. It is reported as ambiguous for a human or the runner to resolve.
- failed: it may be retried.

Each call writes an audit row before and after. Phase 2 adds Keychain secret injection
(env vars for the tool subprocess only) and redaction of secret values in the audit log.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from resonant.gate.types import Allow, Decision, Intent
from resonant.store.audit import audit
from resonant.store.db import now_iso, transaction

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def idempotency_key(task_id: str, step_no: int, intent_hash: str) -> str:
    return hashlib.sha256(f"{task_id}:{step_no}:{intent_hash}".encode()).hexdigest()


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: int = 0
    replayed: bool = False  # True: returned from a previous successful execution
    ambiguous: bool = False  # True: an earlier attempt started but never recorded an outcome


class NotAllowedError(Exception):
    pass


class Executor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        handlers: Mapping[str, ToolHandler],
        *,
        timeout_s: float = 30.0,
    ) -> None:
        self.conn = conn
        self.handlers = dict(handlers)
        self.timeout_s = timeout_s

    async def execute(self, intent: Intent, decision: Decision, *, step_no: int) -> ToolResult:
        if not isinstance(decision, Allow):
            raise NotAllowedError(f"executor requires Allow, got {type(decision).__name__}")
        key = idempotency_key(intent.task_id, step_no, intent.hash)

        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT status, result FROM executions WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row is not None and row["status"] == "succeeded":
                return ToolResult(ok=True, data=json.loads(row["result"]), replayed=True)
            if row is not None and row["status"] == "started":
                return ToolResult(ok=False, ambiguous=True, error="earlier attempt never finished")
            self.conn.execute(
                """INSERT INTO executions (idempotency_key, task_id, intent_hash, status,
                       started_at) VALUES (?, ?, ?, 'started', ?)
                   ON CONFLICT (idempotency_key) DO UPDATE SET status = 'started',
                       started_at = excluded.started_at, finished_at = NULL""",
                (key, intent.task_id, intent.hash, now_iso()),
            )
            self._audit("executor.start", intent, key, decision)

        start = time.monotonic()
        handler = self.handlers.get(intent.tool)
        try:
            if handler is None:
                raise LookupError(f"no handler for tool {intent.tool!r}")
            data = await asyncio.wait_for(handler(intent.args), timeout=self.timeout_s)
            json.dumps(data)
            result = ToolResult(ok=True, data=data)
        except Exception as e:
            result = ToolResult(ok=False, error=f"{type(e).__name__}: {e}")
        result = ToolResult(
            ok=result.ok,
            data=result.data,
            error=result.error,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

        with transaction(self.conn):
            self.conn.execute(
                """UPDATE executions SET status = ?, result = ?, finished_at = ?
                   WHERE idempotency_key = ?""",
                (
                    "succeeded" if result.ok else "failed",
                    json.dumps(result.data if result.ok else {"error": result.error}),
                    now_iso(),
                    key,
                ),
            )
            self._audit(
                "executor.result",
                intent,
                key,
                decision,
                ok=result.ok,
                error=result.error,
                duration_ms=result.duration_ms,
            )
        return result

    def _audit(self, action: str, intent: Intent, key: str, decision: Allow, **detail: Any) -> None:
        audit(
            self.conn,
            action,
            task_id=intent.task_id,
            requested_by=intent.principal,
            extension=intent.extension,
            tool=intent.tool,
            effect=intent.effect.value,
            level=intent.level.value,
            intent_hash=intent.hash,
            idempotency_key=key,
            decision=type(decision).__name__,
            trace_id=intent.trace_id,
            approval_id=decision.approval_id,
            **detail,
        )

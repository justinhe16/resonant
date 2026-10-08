"""Executor: the only component that performs side effects (and, from Phase 2, reads secrets).

Before running anything, the executor consumes the decision's single-use token from the
gate (``gate.consume(token, intent.hash)``). An Allow it didn't receive from the gate, or
one issued for different args, is refused and audited.

Every execution is keyed by
``idempotency_key(task_id, step_no, call_index, intent_hash)`` in ``executions``:
- succeeded: the recorded result is returned. The tool never runs twice.
- started: an earlier attempt's outcome is unknown, so it is **ambiguous**. It is never
  re-run automatically. A human or runner resolves it with ``resolve_ambiguous``.
- failed: a *definite* failure (the tool raised ``ToolFailed``, or a read failed). It may
  be retried.

Outcome rules:
- For reads, any error is a definite failure.
- For write and pay, only ``ToolFailed`` is definite. A timeout, cancellation, unexpected
  exception, or unserializable result might have happened after the effect, so the call is
  ambiguous.

Each call writes audit rows before and after it, and on every refusal or ambiguity.
Phase 2 adds Keychain secret injection and redaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from resonant.gate.types import Allow, Decision, Intent, TokenVerifier
from resonant.store.audit import audit
from resonant.store.db import atomic, now_iso
from resonant_sdk import Effect

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
HandlerKey = tuple[str | None, str]  # (extension, tool); builtins use None

_UNKNOWN = "earlier attempt has no recorded outcome"


def idempotency_key(task_id: str, step_no: int, call_index: int, intent_hash: str) -> str:
    raw = f"{task_id}:{step_no}:{call_index}:{intent_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()


class ToolFailed(Exception):
    """Raise from a handler when the tool definitely did NOT take effect (safe to retry)."""


class NotAllowedError(Exception):
    pass


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: int = 0
    replayed: bool = False  # True: returned from a previous successful execution
    ambiguous: bool = False  # True: outcome unknown; not re-run until resolved
    idempotency_key: str | None = None


class Executor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        verifier: TokenVerifier,
        handlers: Mapping[HandlerKey, ToolHandler],
    ) -> None:
        self.conn = conn
        self.verifier = verifier
        self.handlers = dict(handlers)

    async def execute(
        self, intent: Intent, decision: Decision, *, step_no: int, call_index: int = 0
    ) -> ToolResult:
        if not isinstance(decision, Allow) or not self.verifier.consume(
            decision.token, intent.hash
        ):
            with atomic(self.conn):
                self._audit("executor.refused", intent, None, decision)
            raise NotAllowedError("executor requires an Allow issued by the gate for this intent")
        key = idempotency_key(intent.task_id, step_no, call_index, intent.hash)

        with atomic(self.conn):
            row = self.conn.execute(
                "SELECT status, result FROM executions WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if row is not None and row["status"] == "succeeded":
                self._audit("executor.replayed", intent, key, decision)
                data = json.loads(row["result"])
                return ToolResult(ok=True, data=data, replayed=True, idempotency_key=key)
            if row is not None and row["status"] == "started":
                self._audit("executor.ambiguous", intent, key, decision, reason=_UNKNOWN)
                return ToolResult(ok=False, ambiguous=True, error=_UNKNOWN, idempotency_key=key)
            self.conn.execute(
                """INSERT INTO executions (idempotency_key, task_id, intent_hash, status,
                       started_at) VALUES (?, ?, ?, 'started', ?)
                   ON CONFLICT (idempotency_key) DO UPDATE SET status = 'started',
                       started_at = excluded.started_at, finished_at = NULL, result = NULL""",
                (key, intent.task_id, intent.hash, now_iso()),
            )
            self._audit("executor.start", intent, key, decision)

        start = time.monotonic()
        try:
            data = await self._run(intent)
        except asyncio.CancelledError:
            self._record(intent, key, decision, self._classify(intent, "cancelled"), start)
            raise
        except ToolFailed as e:
            outcome = ToolResult(ok=False, error=f"ToolFailed: {e}")
        except Exception as e:
            outcome = self._classify(intent, f"{type(e).__name__}: {e}")
        else:
            outcome = ToolResult(ok=True, data=data)
        return self._record(intent, key, decision, outcome, start)

    async def _run(self, intent: Intent) -> dict[str, Any]:
        handler = self.handlers.get((intent.extension, intent.tool))
        if handler is None:
            raise ToolFailed(f"no handler for {intent.extension or 'builtin'}/{intent.tool}")
        data = await asyncio.wait_for(handler(dict(intent.args)), timeout=intent.spec.timeout_s)
        json.dumps(data, allow_nan=False)  # inside the try: an unserializable result counts
        return data

    @staticmethod
    def _classify(intent: Intent, error: str) -> ToolResult:
        if intent.effect is Effect.READ:
            return ToolResult(ok=False, error=error)
        return ToolResult(ok=False, ambiguous=True, error=error)

    def _record(
        self, intent: Intent, key: str, decision: Allow, outcome: ToolResult, start: float
    ) -> ToolResult:
        outcome = replace(
            outcome, duration_ms=int((time.monotonic() - start) * 1000), idempotency_key=key
        )
        with atomic(self.conn):
            if not outcome.ambiguous:  # ambiguous rows stay 'started' until resolved
                self.conn.execute(
                    """UPDATE executions SET status = ?, result = ?, finished_at = ?
                       WHERE idempotency_key = ?""",
                    (
                        "succeeded" if outcome.ok else "failed",
                        json.dumps(outcome.data if outcome.ok else {"error": outcome.error}),
                        now_iso(),
                        key,
                    ),
                )
            self._audit(
                "executor.ambiguous" if outcome.ambiguous else "executor.result",
                intent,
                key,
                decision,
                ok=outcome.ok,
                error=outcome.error,
                duration_ms=outcome.duration_ms,
            )
        return outcome

    def resolve_ambiguous(
        self,
        key: str,
        *,
        succeeded: bool,
        actor: str,
        result: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> bool:
        """Record the real outcome of an ambiguous call, after a human or runner checked."""
        payload = (result or {}) if succeeded else {"error": note or "resolved as failed"}
        with atomic(self.conn):
            cur = self.conn.execute(
                """UPDATE executions SET status = ?, result = ?, finished_at = ?
                   WHERE idempotency_key = ? AND status = 'started'""",
                ("succeeded" if succeeded else "failed", json.dumps(payload), now_iso(), key),
            )
            if cur.rowcount:
                audit(
                    self.conn,
                    "executor.ambiguous_resolved",
                    idempotency_key=key,
                    approved_by=actor,
                    succeeded=succeeded,
                    note=note,
                )
        return cur.rowcount == 1

    def _audit(
        self, action: str, intent: Intent, key: str | None, decision: Decision, **detail: Any
    ) -> None:
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
            approval_id=decision.approval_id if isinstance(decision, Allow) else None,
            **detail,
        )

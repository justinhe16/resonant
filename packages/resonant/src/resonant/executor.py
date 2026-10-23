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

An Allow that carries an ``approval_id`` spends that approval: ``used_at`` is set in the
same transaction as the ``started`` row, and an approval that is already used (or belongs
to another task or intent) is refused. So one approval runs one execution.

``recorded(key)`` lets a resuming runner check a pinned key before calling the gate:
succeeded is replayed, started is ambiguous, and failed or nothing means it may run
again. The approval is spent with ``started``, so after a definite failure of an approved
call the gate returns ``Deny(APPROVAL_USED)``: a retry needs a new task.

Each call writes audit rows before and after it, and on every refusal or ambiguity.

Secrets (Phase 3). With ``secrets=SecretStore``, a call whose spec declares ``secrets``
reads exactly those from the Keychain just before it runs (after the replay check, before
the ``started`` row). A missing or unavailable secret is a definite failure with no
``executions`` row and a code-built ``notice``. Handlers that subclass
``ContextHandler`` get them in a ``CallContext``; plain handlers never see them.

Redaction. Everything the call produced (the result, the error, its events, the audit
fields) is deep-redacted with ``Redactor`` over that call's secret values before it is
stored, audited or returned. An extension result whose JSON is over ``result_max_bytes``
is stored and returned as ``{"truncated": true, "bytes": n, "text": <prefix>}``.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import sqlite3
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from resonant.gate.codes import clean_external
from resonant.gate.types import Allow, Decision, Intent, TokenVerifier
from resonant.redact import Redactor
from resonant.secrets import SecretError, SecretMissing, SecretStore, values_of
from resonant.store import approvals
from resonant.store.audit import audit
from resonant.store.db import atomic, now_iso, transaction, utcnow
from resonant_sdk import Effect

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
HandlerKey = tuple[str | None, str]  # (extension, tool); builtins use None

_UNKNOWN = "earlier attempt has no recorded outcome"
_USED = "approval already used or not for this intent"


def _stderr_tail(error: BaseException) -> str | None:
    """The redacted stderr tail an extension failure carries, for the audit row only."""
    tail: object = getattr(error, "stderr_tail", None)
    return tail if isinstance(tail, str) else None


REASON_MAX = 200
# Lines of a stderr tail that are never the reason: a traceback's frames and markers.
_NOISE = re.compile(r"^(Traceback |File \"|During handling|The above exception|\^+$|~+$)")


def stderr_reason(tail: str | None, extension: str | None = None) -> str | None:
    """The reason a failed extension call gives (PER-268): the last meaningful line of its
    stderr tail, without the ``<extension>: `` prefix it prints, cleaned with
    ``clean_external`` and clipped to ``REASON_MAX``. None when there is none. The tail is
    already redacted."""
    if not tail:
        return None
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    lines = [line for line in lines if not _NOISE.match(line)]
    if not lines:
        return None
    last = lines[-1]
    if extension and last.lower().startswith(f"{extension.lower()}: "):
        last = last[len(extension) + 2 :]
    flat = clean_external(last)
    if len(flat) > REASON_MAX:
        flat = flat[: REASON_MAX - 3].rstrip() + "..."
    return flat or None


def idempotency_key(task_id: str, step_no: int, call_index: int, intent_hash: str) -> str:
    raw = f"{task_id}:{step_no}:{call_index}:{intent_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()


class ToolFailed(Exception):
    """Raise from a handler when the tool definitely did NOT take effect (safe to retry)."""


class NotAllowedError(Exception):
    pass


@dataclass(frozen=True)
class CallContext:
    """What a ``ContextHandler`` gets besides its args.

    ``secrets`` is ``{RESONANT_SECRET_<NAME>: value}`` for exactly this spec's declared
    secrets. A handler appends the run's events (JSON-like dicts) to ``events``; the
    executor redacts them and returns them on ``ToolResult.events``.
    """

    secrets: Mapping[str, str] = field(repr=False)  # values never in a repr or log
    idempotency_key: str
    task_id: str
    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    # Who chose the call (``Intent.origin``). ``ask_claude`` starts a tainted task's Claude
    # session tainted (BR-10).
    origin: str = "inferred"
    # PER-272: the gate allowed it on nobody's word for this exact call (``Allow.unattended``:
    # an L3, a standing allow, a trust rule): ``merge_run`` then merges only when it's clean.
    unattended: bool = False


class ContextHandler(ABC):
    """A handler that wants a ``CallContext`` (extension tools and jobs)."""

    @abstractmethod
    async def __call__(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]: ...


Handler = ToolHandler | ContextHandler


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: int = 0
    replayed: bool = False  # True: returned from a previous successful execution
    ambiguous: bool = False  # True: outcome unknown; not re-run until resolved
    idempotency_key: str | None = None
    events: tuple[dict[str, Any], ...] = ()  # a ContextHandler's events, redacted
    notice: str | None = None  # a code-built reply for a definite failure (missing secret)
    # PER-268: a failed extension call's own reason, the last meaningful line of its stderr
    # (redacted, cleaned and clipped: ``stderr_reason``), for the failure reply.
    reason: str | None = None


def secret_notice(error: SecretError) -> str:
    """``Not done: secret github/token is missing. On the Mini: resonant secret set …``"""
    if isinstance(error, SecretMissing):
        return (
            f"Not done: secret {error.ref} is missing. On the Mini: resonant secret set {error.ref}"
        )
    return f"Not done: {error}."


class Executor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        verifier: TokenVerifier,
        handlers: Mapping[HandlerKey, Handler],
        *,
        secrets: SecretStore | None = None,
        result_max_bytes: int | None = None,
    ) -> None:
        """``handlers`` is copied: pass builtins and extension handlers together
        (``build_components`` merges them at start). ``result_max_bytes`` caps extension
        results (None: no cap)."""
        self.conn = conn
        self.verifier = verifier
        self.handlers: dict[HandlerKey, Handler] = dict(handlers)
        self.secrets = secrets
        self.result_max_bytes = result_max_bytes

    async def execute(
        self, intent: Intent, decision: Decision, *, step_no: int, call_index: int = 0
    ) -> ToolResult:
        if self.conn.in_transaction:
            # The 'started' row must be durable before the side effect runs.
            raise RuntimeError("Executor.execute must not be called inside a transaction")
        if not isinstance(decision, Allow) or not self.verifier.consume(
            decision.token, intent.hash
        ):
            with atomic(self.conn):
                self._audit("executor.refused", intent, None, decision)
            raise NotAllowedError("executor requires an Allow issued by the gate for this intent")
        key = idempotency_key(intent.task_id, step_no, call_index, intent.hash)

        env: dict[str, str] = {}
        if intent.spec.secrets and self._may_run(key):
            try:
                env = await self._secrets_for(intent)
            except SecretError as e:
                # Before 'started': nothing ran, so this is a definite failure with no row.
                with atomic(self.conn):
                    self._audit("executor.result", intent, key, decision, ok=False, error=str(e))
                return ToolResult(
                    ok=False,
                    error=f"ToolFailed: {e}",
                    idempotency_key=key,
                    notice=secret_notice(e),
                )
        redactor = Redactor(values_of(env))

        with transaction(self.conn):
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
            spent = decision.approval_id is None or self._spend(intent, decision.approval_id)
            if spent:
                self._start(intent, key, decision)
            else:
                self._audit("executor.refused", intent, key, decision, reason=_USED)
        if not spent:
            raise NotAllowedError(_USED)

        start = time.monotonic()
        ctx = CallContext(
            secrets=env,
            idempotency_key=key,
            task_id=intent.task_id,
            origin=intent.origin,
            unattended=decision.unattended,
        )
        tail: str | None = None
        try:
            data = await self._run(intent, ctx)
        except asyncio.CancelledError:
            outcome = self._classify(intent, "cancelled")
            self._record(intent, key, decision, outcome, start, redactor)
            raise
        except ToolFailed as e:
            outcome = ToolResult(ok=False, error=f"ToolFailed: {e}")
            tail = _stderr_tail(e)
        except Exception as e:
            outcome = self._classify(intent, f"{type(e).__name__}: {e}")
            tail = _stderr_tail(e)
        else:
            outcome = ToolResult(ok=True, data=data, events=tuple(ctx.events))
        return self._record(intent, key, decision, outcome, start, redactor, stderr_tail=tail)

    def _may_run(self, key: str) -> bool:
        """False when the key is already succeeded or started (replayed or ambiguous)."""
        row = self.conn.execute(
            "SELECT status FROM executions WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return row is None or row["status"] == "failed"

    async def _secrets_for(self, intent: Intent) -> dict[str, str]:
        if self.secrets is None:
            raise SecretError(f"{intent.extension}/{intent.spec.secrets[0]}", "unavailable")
        if intent.extension is None:
            raise SecretError(f"builtin/{intent.spec.secrets[0]}", "unavailable")
        return await self.secrets.env_for(intent.extension, intent.spec.secrets)

    def _spend(self, intent: Intent, approval_id: str) -> bool:
        """Mark the approval used. Call inside the transaction that writes 'started'."""
        a = approvals.get_approval(self.conn, approval_id)
        if a is None or a.task_id != intent.task_id or a.intent_hash != intent.hash:
            return False
        return approvals.mark_used(self.conn, approval_id, utcnow())

    def _start(self, intent: Intent, key: str, decision: Allow) -> None:
        self.conn.execute(
            """INSERT INTO executions (idempotency_key, task_id, intent_hash, status,
                   started_at) VALUES (?, ?, ?, 'started', ?)
               ON CONFLICT (idempotency_key) DO UPDATE SET status = 'started',
                   started_at = excluded.started_at, finished_at = NULL, result = NULL""",
            (key, intent.task_id, intent.hash, now_iso()),
        )
        self._audit("executor.start", intent, key, decision)

    async def _run(self, intent: Intent, ctx: CallContext) -> dict[str, Any]:
        handler = self.handlers.get((intent.extension, intent.tool))
        if handler is None:
            raise ToolFailed(f"no handler for {intent.extension or 'builtin'}/{intent.tool}")
        args = copy.deepcopy(intent.args)
        call = handler(args, ctx) if isinstance(handler, ContextHandler) else handler(args)
        data = await asyncio.wait_for(call, timeout=intent.spec.timeout_s)
        json.dumps(data, allow_nan=False)  # inside the try: an unserializable result counts
        return data

    @staticmethod
    def _classify(intent: Intent, error: str) -> ToolResult:
        if intent.effect is Effect.READ:
            return ToolResult(ok=False, error=error)
        return ToolResult(ok=False, ambiguous=True, error=error)

    def _cap(self, intent: Intent, data: dict[str, Any] | None) -> dict[str, Any] | None:
        """An extension result whose stored JSON is over ``result_max_bytes`` becomes
        ``{"truncated": true, "bytes": n, "text": <prefix>}``, itself within the cap."""
        cap = self.result_max_bytes
        if data is None or intent.extension is None or cap is None:
            return data
        text = json.dumps(data)  # the same encoding _record stores
        if len(text) <= cap:
            return data

        def capped(n: int) -> dict[str, Any]:
            return {"truncated": True, "bytes": len(text), "text": text[:n]}

        lo, hi = 0, len(text)  # the longest prefix whose stored form fits
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(json.dumps(capped(mid))) <= cap:
                lo = mid
            else:
                hi = mid - 1
        return capped(lo)

    def _record(
        self,
        intent: Intent,
        key: str,
        decision: Allow,
        outcome: ToolResult,
        start: float,
        redactor: Redactor,
        *,
        stderr_tail: str | None = None,
    ) -> ToolResult:
        # Redact first, then cap: a cut can only shorten text that is already redacted.
        outcome = replace(
            outcome,
            data=self._cap(intent, redactor.deep(outcome.data)),
            error=None if outcome.error is None else redactor.text(outcome.error),
            events=redactor.deep(outcome.events),
            duration_ms=int((time.monotonic() - start) * 1000),
            idempotency_key=key,
        )
        detail: dict[str, Any] = {}
        if stderr_tail:
            detail["stderr_tail"] = redactor.text(stderr_tail)
            if not outcome.ok:
                reason = stderr_reason(detail["stderr_tail"], intent.extension)
                outcome = replace(outcome, reason=reason)
        with transaction(self.conn):
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
                **detail,
            )
        return outcome

    def recorded(self, key: str) -> ToolResult | None:
        """What ``executions`` holds for ``key``, for a runner resuming at a pinned key.

        Succeeded: the recorded result (``replayed=True``), so don't call the gate again.
        Started: ambiguous, never re-run. Failed: the definite failure. None: nothing
        recorded. Read-only and not audited.
        """
        row = self.conn.execute(
            "SELECT status, result FROM executions WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "succeeded":
            data = json.loads(row["result"])
            return ToolResult(ok=True, data=data, replayed=True, idempotency_key=key)
        if row["status"] == "started":
            return ToolResult(ok=False, ambiguous=True, error=_UNKNOWN, idempotency_key=key)
        error = json.loads(row["result"] or "{}").get("error")
        return ToolResult(ok=False, error=error, idempotency_key=key)

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

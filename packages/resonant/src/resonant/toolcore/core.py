"""ToolCore (TC-1): the one path from "a caller picked a tool" to "it ran, or here is why not".

Every runner that calls a tool goes through :class:`ToolCore`: the local runner (the intents
path), the brain loop, the Claude bridge (``runners/ask_tools.py``), the job and watch runners,
the media runner and the approval resume of a pinned call. Nothing outside this package calls
``gate.evaluate`` or ``Executor.execute`` (``tests/test_toolcore_boundary.py``), so no caller
can skip the gate. The gate (``gate/``), the trust rules (``trust/``) and the executor
(``executor.py``) stay their own modules; ToolCore is built over them.

``call(target, args, scope)`` is a new call:

1. **Spec.** From the registry, never from the caller or the model: a ``ToolSpec`` or
   ``(extension, name)`` target is looked up again by its key; a bare name only among the
   tools the caller offered (``scope.tools``). Not there: ``Refused("unavailable")``.
2. **Args.** ``scope.check_schema``: checked against the spec's JSON schema (``args_error``)
   first; then ``Intent.for_tool`` (JSON only). A model-chosen call is never ``explicit``.
   Bad: ``Refused("bad_args")``.
3. **Side-effect cap.** A change when ``scope.effects`` already holds ``MAX_SIDE_EFFECTS``
   report lines: ``Refused("capped")`` (GT-1).
4. **Gate.** ``gate.evaluate``. A read the gate allows runs and comes back ungated
   (``Ran``/``Failed``/``Refused`` with ``gated=False``).
5. **Pin.** Anything else is a gated call: refused or dry-run comes back as is; allowed or
   asked, it is pinned first (``checkpoint.call`` = :class:`PinnedCall`, plus
   ``scope.effects`` and ``scope.pin``, saved through ``scope.steps``). A scope with no
   ``steps`` (jobs) has nothing to pin.
6. **Run.** ``Asked`` for an approval (the caller prompts, or defers); else the executor runs
   it with the pinned ``step_no`` and ``call_index`` (its idempotency key), and the outcome
   carries the code-built report line.

``resume(pinned, scope)`` runs a pinned call exactly once: what ``executions`` holds for its
key first (replayed, ambiguous or failed, without the gate), else the gate with the pinned
origin (``pinned_origin``: ``explicit`` only for the router's own ``direct_call``, ``tainted``
when it was pinned so) and the executor. ``recorded`` is that first look on its own (the job
runner checks the kill switch between it and the gate).

Spans keep their pre-TC-1 names (``runner.local.tool`` for a call, ``runner.local.call`` for
a resume) and attrs: the task eval counts gated writes and approvals from them. A scope with no
``spans`` (jobs, watches) emits none.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast, runtime_checkable

import jsonschema

from resonant.executor import NotAllowedError, ToolResult, idempotency_key
from resonant.gate import Allow, ApprovalBatch, Decision, Deny, DryRun, Intent, NeedsApproval
from resonant.gate.policy import APPROVAL_USED, batch_line
from resonant.gate.types import Origin
from resonant.loop.types import StepContext
from resonant.models import SpanFactory
from resonant.store import approvals as ap
from resonant.store import trust as trust_store
from resonant.toolcore.reports import (
    BAD_ARGS_REPLY,
    NOT_AVAILABLE_REPLY,
    failure_reply,
    not_run_reply,
    refusal_reply,
    report_line,
)
from resonant.tools import ToolRegistry
from resonant.tools.registry import ToolKey
from resonant_sdk import Effect, ToolSpec

# Gated side effects that run without asking (L3, a standing allow), per task (GT-1; BA-1
# raised it from 3 so "update these 4 Linear tickets" fits under a standing rule). Changes
# that ask don't count: they wait in the task's batch, up to ``gate.batch_max``.
MAX_SIDE_EFFECTS = 5
CAPPED_REPLY = f"Not done: at most {MAX_SIDE_EFFECTS} changes per task."
STOPPED = "task is no longer running"  # the pin couldn't be saved


def batch_capped_reply(limit: int) -> str:
    """A change past ``gate.batch_max`` in one batch (BA-1)."""
    return f"Not done: at most {limit} changes can wait on one yes."


SPAN_CALL = "runner.local.tool"  # pre-TC-1 names: dashboards and the task eval read them
SPAN_RESUME = "runner.local.call"


class RunnerGate(Protocol):
    """The part of the gate ToolCore uses. The executor consumes the token."""

    def evaluate(self, intent: Intent) -> Decision: ...


@runtime_checkable
class BatchGate(Protocol):
    """BA-1: the part of the gate a task's batch of approvals uses (``PolicyGate``). A gate
    without it (``DryRunGate``) never batches: one approval ends the task, as before."""

    batch_max: int

    @property
    def owner(self) -> str: ...

    def open_batch(self, principal: str, approval_ids: Sequence[str]) -> ApprovalBatch: ...

    def approval_states(self, approval_ids: Sequence[str]) -> Mapping[str, str]: ...

    def drop(self, approval_ids: Sequence[str], *, reason: str) -> None: ...


class ToolExecutor(Protocol):
    async def execute(
        self, intent: Intent, decision: Decision, *, step_no: int, call_index: int = 0
    ) -> ToolResult: ...

    def recorded(self, key: str) -> ToolResult | None: ...


class InlineGate(Protocol):
    """A gate the router's fast path spends a token on itself (no executor)."""

    def evaluate(self, intent: Intent) -> Decision: ...

    def consume(self, token: str, intent_hash: str) -> bool: ...


# --- pinning --------------------------------------------------------------------------------


@dataclass(frozen=True)
class PinnedCall:
    """``checkpoint.call``: a task's gated call and its pinned idempotency inputs."""

    tool: str
    extension: str | None
    args: dict[str, Any]
    step_no: int
    call_index: int

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "extension": self.extension,
            "args": self.args,
            "step_no": self.step_no,
            "call_index": self.call_index,
        }

    @classmethod
    def from_json(cls, value: Any) -> PinnedCall | None:
        d = cast(dict[str, Any], value) if isinstance(value, dict) else {}
        tool, ext, args = d.get("tool"), d.get("extension"), d.get("args")
        step_no, call_index = d.get("step_no"), d.get("call_index")
        if (
            not isinstance(tool, str)
            or not (ext is None or isinstance(ext, str))
            or not isinstance(args, dict)
            or not isinstance(step_no, int)
            or not isinstance(call_index, int)
        ):
            return None
        return cls(tool, ext, cast(dict[str, Any], args), step_no, call_index)


@dataclass(frozen=True)
class BatchItem:
    """BA-1: one change in a task's batch (``checkpoint.batch``): a call the gate asks about,
    pinned while the task goes on, with its approval and the code-built line the batch prompt
    shows. ``tainted``: the origin it was asked with (its resume keeps it)."""

    call: PinnedCall
    approval_id: str
    summary: str
    tainted: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            **self.call.to_json(),
            "approval_id": self.approval_id,
            "summary": self.summary,
            "tainted": self.tainted,
        }

    @classmethod
    def from_json(cls, value: Any) -> BatchItem | None:
        call = PinnedCall.from_json(value)
        d = cast(dict[str, Any], value) if isinstance(value, dict) else {}
        approval_id, summary, tainted = d.get("approval_id"), d.get("summary"), d.get("tainted")
        if call is None or not isinstance(approval_id, str) or not isinstance(summary, str):
            return None
        return cls(call, approval_id, summary, tainted is True)


def batch_items(cp: Mapping[str, Any]) -> list[BatchItem] | None:
    """``checkpoint.batch`` as items, in order; None when an entry is malformed."""
    raw = cp.get("batch")
    if not isinstance(raw, list):
        return None
    items = [BatchItem.from_json(v) for v in cast(list[Any], raw)]
    out = [i for i in items if i is not None]
    return out if len(out) == len(items) else None


def direct_call(cp: Mapping[str, Any]) -> tuple[str | None, str, dict[str, Any]] | None:
    """``checkpoint.direct_call`` (written only by the router's command parser) as
    ``(extension, tool, args)``. A missing ``extension`` (older checkpoints) is a builtin."""
    raw = cp.get("direct_call")
    d = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    ext, tool, args = d.get("extension"), d.get("tool"), d.get("args", {})
    if not (ext is None or isinstance(ext, str)) or not isinstance(tool, str):
        return None
    if not isinstance(args, dict):
        return None
    return ext, tool, cast(dict[str, Any], args)


def is_explicit(cp: Mapping[str, Any], pinned: PinnedCall) -> bool:
    """The one place ``origin="explicit"`` comes from: a router-written ``direct_call`` that
    is exactly the pinned call (extension, tool and args). A call a model chose never is."""
    direct = direct_call(cp)
    return (
        direct is not None
        and pinned.call_index == 0
        and direct == (pinned.extension, pinned.tool, pinned.args)
    )


def pinned_origin(cp: Mapping[str, Any], pinned: PinnedCall) -> Origin:
    """The origin a pinned call is gated with on resume: ``explicit`` only for the router's
    ``direct_call`` (``is_explicit``), else ``tainted`` when it was pinned so (D6, BR-10b),
    else ``inferred``."""
    if is_explicit(cp, pinned):
        return "explicit"
    return "tainted" if cp.get("tainted") is True else "inferred"


def args_error(spec: ToolSpec, args: dict[str, Any]) -> str | None:
    """Why ``args`` don't fit ``spec``'s JSON schema (a short reason, no values), or None."""
    try:
        jsonschema.validate(args, spec.args_schema)
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.path)
        return f"{e.validator}{' at ' + where if where else ''}"
    return None


# --- the call's context and outcome ----------------------------------------------------------


@dataclass(frozen=True)
class CallScope:
    """What a call is bound to: its task, who asked and how, and the task's checkpoint.

    ``origin`` is the caller's (``inferred``, ``tainted`` or ``scheduled``; never ``explicit``
    for ``call``). ``checkpoint`` is the stored checkpoint a pin extends and ``steps`` saves
    it (None: nothing to pin, e.g. a job). ``effects`` is the side-effect counter: the report
    lines of the changes this task already ran, pinned with the call so a resume replies with
    them. ``pin`` adds keys to the pinned checkpoint (the brain's taint). ``tools`` is what
    the caller offered, for a call by bare name. ``batch`` (BA-1; None: off) is the task's
    batch so far: a call the gate asks about joins it (``Asked.item``) instead of ending the
    task, and every pin keeps it."""

    task_id: str
    principal: str
    trace_id: str | None
    step_no: int
    call_index: int = 0
    origin: Origin = "inferred"
    spans: SpanFactory | None = None
    checkpoint: Mapping[str, Any] = field(default_factory=dict[str, Any])
    steps: StepContext | None = None
    effects: Sequence[str] = ()
    pin: Mapping[str, Any] | None = None
    tools: Sequence[ToolSpec] = ()
    check_schema: bool = True
    batch: Sequence[BatchItem] | None = None


@dataclass(frozen=True)
class Ran:
    """Allowed, and the tool ran (or ``executions`` already held its success: ``recorded``).
    ``report``: a gated change's code-built line; ``rule_added``: the ``Rule added: …`` line
    when it ran on a ``yes always`` that saved a standing allow."""

    intent: Intent
    result: ToolResult
    gated: bool
    report: str | None = None
    rule_added: str | None = None
    recorded: bool = False
    decision: Decision | None = None


@dataclass(frozen=True)
class Asked:
    """The gate asks the owner. The call is pinned in ``checkpoint`` (what the task waits
    with); the caller prompts (``prompt_approvers``) or defers. ``summary`` is code-built and
    never carries the approval code; ``decision`` (it does) is for the prompt only."""

    intent: Intent
    decision: NeedsApproval
    checkpoint: dict[str, Any]
    gated: bool = True
    item: BatchItem | None = None  # BA-1: the batch entry it joined (or already was)

    @property
    def summary(self) -> str:
        return self.decision.summary


RefusedWhy = Literal["unavailable", "bad_args", "capped", "denied", "dry_run", "not_allowed"]


@dataclass(frozen=True)
class Refused:
    """Not run. ``text`` is the code-built reply (``Not done: …``, ``Dry run: would …``);
    ``decision`` the gate's, when it got that far."""

    why: RefusedWhy
    text: str
    gated: bool
    intent: Intent | None = None
    decision: Decision | None = None


@dataclass(frozen=True)
class Failed:
    """It ran and failed (definite, or ``result.ambiguous``), or (``result`` None) it never
    ran because its pin couldn't be saved: the task stopped."""

    text: str
    gated: bool
    intent: Intent | None = None
    result: ToolResult | None = None
    recorded: bool = False
    decision: Decision | None = None


CallOutcome = Ran | Asked | Refused | Failed


class _NoSpan:
    def set(self, **attrs: Any) -> None:
        pass


@contextlib.contextmanager
def _no_span(name: str, **attrs: Any) -> Generator[_NoSpan]:
    yield _NoSpan()


def allow_inline(gate: InlineGate, intent: Intent) -> bool:
    """The router's fast path: a read it runs inline, with no executor. True when the gate
    allows it, and its single-use token is then spent here."""
    decision = gate.evaluate(intent)
    return isinstance(decision, Allow) and gate.consume(decision.token, intent.hash)


class ToolCore:
    """The shared tool call path over a registry, gate and executor (see the module doc)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        registry: ToolRegistry,
        gate: RunnerGate,
        executor: ToolExecutor,
    ) -> None:
        self.conn = conn
        self.registry = registry
        self.gate = gate
        self.executor = executor
        # BA-1: batches need the gate's batch half; without it a task asks once, as before.
        self.batch_gate: BatchGate | None = gate if isinstance(gate, BatchGate) else None

    def spec(self, target: ToolSpec | ToolKey | str, scope: CallScope) -> ToolSpec | None:
        """The registry's spec for ``target``: a bare name only among ``scope.tools``."""
        if isinstance(target, str):
            offered = next((t for t in scope.tools if t.name == target), None)
            if offered is None:
                return None
            key: ToolKey = (offered.extension, offered.name)
        elif isinstance(target, ToolSpec):
            key = (target.extension, target.name)
        else:
            key = target
        return self.registry.get(*key)

    def _intent(
        self, spec: ToolSpec, args: dict[str, Any], scope: CallScope, origin: Origin
    ) -> Intent:
        return Intent.for_tool(
            spec,
            task_id=scope.task_id,
            principal=scope.principal,
            args=args,
            trace_id=scope.trace_id,
            origin=origin,
        )

    async def call(
        self, target: ToolSpec | ToolKey | str, args: dict[str, Any], scope: CallScope
    ) -> CallOutcome:
        """A new call (see the module doc). Never raises for a refusal."""
        name = target if isinstance(target, str) else (
            target.name if isinstance(target, ToolSpec) else target[1]
        )  # fmt: skip
        spans = scope.spans or _no_span
        with spans(SPAN_CALL, tool=name, call_index=scope.call_index) as s:
            spec = self.spec(target, scope)
            if spec is None:
                s.set(decision="unavailable", ok=False)
                return Refused("unavailable", NOT_AVAILABLE_REPLY, False)
            s.set(extension=spec.extension)
            try:
                # Never "explicit": a model can never claim the owner typed this.
                if scope.origin == "explicit":
                    raise ValueError("a model-chosen call is never explicit")
                if scope.check_schema and args_error(spec, args) is not None:
                    raise ValueError("args don't fit the schema")
                intent = self._intent(spec, args, scope, scope.origin)
            except (TypeError, ValueError):
                s.set(decision="bad_args", ok=False)
                return Refused("bad_args", BAD_ARGS_REPLY, False)
            read = spec.effect is Effect.READ
            if not read and len(scope.effects) >= MAX_SIDE_EFFECTS:
                s.set(decision="capped", ok=False)
                return Refused("capped", CAPPED_REPLY, False, intent)
            decision = self.gate.evaluate(intent)
            s.set(decision=type(decision).__name__)
            if read and not isinstance(decision, NeedsApproval):
                if not isinstance(decision, Allow):
                    s.set(ok=False)
                    return _refused(intent, decision, gated=False)
                try:
                    result = await self.executor.execute(
                        intent, decision, step_no=scope.step_no, call_index=scope.call_index
                    )
                except NotAllowedError:
                    s.set(ok=False)
                    return Refused(
                        "not_allowed", refusal_reply(intent, Deny(APPROVAL_USED)), False,
                        intent, decision,
                    )  # fmt: skip
                s.set(ok=result.ok, replayed=result.replayed, duration_ms=result.duration_ms)
                if not result.ok:
                    return Failed(failure_reply(intent, result), False, intent, result,
                                  decision=decision)  # fmt: skip
                return Ran(intent, result, False, decision=decision)

            # A gated call: pin it before it can run or be prompted.
            s.set(gated=True)
            if not isinstance(decision, Allow | NeedsApproval):
                return _refused(intent, decision, gated=True)
            pinned = PinnedCall(spec.name, spec.extension, intent.args, scope.step_no,
                                scope.call_index)  # fmt: skip
            batching = scope.batch is not None and self.batch_gate is not None
            if batching and isinstance(decision, NeedsApproval):
                return self._join(intent, decision, pinned, scope)
            cp = dict(scope.checkpoint)
            if scope.steps is not None:
                cp = {
                    **scope.checkpoint,
                    **(scope.pin or {}),
                    "call": pinned.to_json(),
                    "effects": list(scope.effects),
                }
                if batching:  # the batch so far stays pinned under an allowed call
                    cp["batch"] = [i.to_json() for i in scope.batch or ()]
                if not scope.steps.save(checkpoint=cp):
                    return Failed(STOPPED, True, intent, decision=decision)
            return await self._decided(intent, decision, pinned, cp)

    def _join(
        self, intent: Intent, decision: NeedsApproval, pinned: PinnedCall, scope: CallScope
    ) -> Asked | Refused | Failed:
        """BA-1: a call the gate asks about joins the task's batch (pinned there, not
        prompted): the same approval again is the entry it already has; past ``batch_max``
        it is refused and its approval dropped (never prompted, so nothing could bind it)."""
        assert self.batch_gate is not None and scope.batch is not None
        items = list(scope.batch)
        same = next((i for i in items if i.approval_id == decision.approval_id), None)
        if same is not None:
            return Asked(intent, decision, dict(scope.checkpoint), item=same)
        limit = self.batch_gate.batch_max
        if len(items) >= limit:
            self.batch_gate.drop([decision.approval_id], reason="batch_cap")
            return Refused("capped", batch_capped_reply(limit), True, intent, decision)
        item = BatchItem(pinned, decision.approval_id, batch_line(decision.summary),
                         scope.origin == "tainted")  # fmt: skip
        cp = dict(scope.checkpoint)
        if scope.steps is not None:
            # ``call`` (an earlier allowed call, done by now) gives way to the batch.
            cp = {
                **{k: v for k, v in scope.checkpoint.items() if k != "call"},
                **(scope.pin or {}),
                "batch": [i.to_json() for i in [*items, item]],
                "effects": list(scope.effects),
            }
            if not scope.steps.save(checkpoint=cp):
                return Failed(STOPPED, True, intent, decision=decision)
        return Asked(intent, decision, cp, item=item)

    async def resume(self, pinned: PinnedCall, scope: CallScope) -> CallOutcome:
        """Run the pinned call exactly once: what ``executions`` holds first, then the gate
        (with ``pinned_origin(scope.checkpoint, pinned)``). Every outcome is gated."""
        spec = self.registry.get(pinned.extension, pinned.tool)
        if spec is None:
            return Refused("unavailable", NOT_AVAILABLE_REPLY, True)
        origin = pinned_origin(scope.checkpoint, pinned)
        spans = scope.spans or _no_span
        with spans(SPAN_RESUME, tool=pinned.tool, extension=pinned.extension, origin=origin) as s:
            try:
                intent = self._intent(spec, pinned.args, scope, origin)
            except (TypeError, ValueError):
                s.set(decision="bad_args")
                return Refused("bad_args", BAD_ARGS_REPLY, True)
            key = idempotency_key(scope.task_id, pinned.step_no, pinned.call_index, intent.hash)
            prior = self.executor.recorded(key)
            if prior is not None:
                s.set(recorded=True, ok=prior.ok, ambiguous=prior.ambiguous)
                return self._ran(intent, prior, recorded=True)
            decision = self.gate.evaluate(intent)
            s.set(decision=type(decision).__name__)
            return await self._decided(intent, decision, pinned, dict(scope.checkpoint))

    def recorded(
        self, target: ToolSpec | ToolKey, args: dict[str, Any], scope: CallScope
    ) -> ToolResult | None:
        """What ``executions`` holds for this call at ``scope``'s step and index (read-only):
        a resume's first look, for a caller that checks something between it and the gate."""
        spec = self.spec(target, scope)
        if spec is None:
            return None
        intent = self._intent(spec, args, scope, scope.origin)
        key = idempotency_key(scope.task_id, scope.step_no, scope.call_index, intent.hash)
        return self.executor.recorded(key)

    async def _decided(
        self, intent: Intent, decision: Decision, pinned: PinnedCall, cp: dict[str, Any]
    ) -> CallOutcome:
        """Act on the gate's decision for a gated call (``cp`` holds its pin, if any)."""
        if isinstance(decision, NeedsApproval):
            return Asked(intent, decision, cp)
        if not isinstance(decision, Allow):
            return _refused(intent, decision, gated=True)
        try:
            result = await self.executor.execute(
                intent, decision, step_no=pinned.step_no, call_index=pinned.call_index
            )
        except NotAllowedError:
            return Refused(
                "not_allowed", refusal_reply(intent, Deny(APPROVAL_USED)), True, intent, decision
            )
        rule = self._rule_added(decision.approval_id) if result.ok else None
        return self._ran(intent, result, rule_added=rule, decision=decision)

    @staticmethod
    def _ran(
        intent: Intent,
        result: ToolResult,
        *,
        recorded: bool = False,
        rule_added: str | None = None,
        decision: Decision | None = None,
    ) -> Ran | Failed:
        if not result.ok:
            return Failed(failure_reply(intent, result), True, intent, result, recorded,
                          decision)  # fmt: skip
        report = None if intent.effect is Effect.READ else report_line(intent, result.data or {})
        return Ran(intent, result, True, report, rule_added, recorded, decision)

    def _rule_added(self, approval_id: str | None) -> str | None:
        """``Rule added: don't ask before running X in Y (rule 7)`` when the call ran on a
        ``yes always`` that saved a standing allow (the gate saved it with the approval)."""
        if approval_id is None:
            return None
        row = ap.get_approval(self.conn, approval_id)
        if row is None or not row.always or not row.always_scope or not row.always_action:
            return None
        rule = next(
            (
                r
                for r in trust_store.rules_for(self.conn, row.always_scope, row.always_action)
                if r.effect == "allow"
            ),
            None,
        )
        return f"Rule added: {rule.text} (rule {rule.id})" if rule is not None else None


def _refused(intent: Intent, decision: Decision, *, gated: bool) -> Refused:
    why: RefusedWhy = "dry_run" if isinstance(decision, DryRun) else "denied"
    return Refused(why, not_run_reply(intent, decision), gated, intent, decision)

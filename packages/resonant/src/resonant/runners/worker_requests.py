"""Gated tools for workers (PER-296): the daemon side of ``resonant request``.

PER-291's rules tell a worker to ask for a gated action (a production deploy, a prod database
write) instead of doing it. ``resonant request <ext>.<tool> [--arg k=v …] [--reason …]`` is how:
the desk (``worker_desk``) keeps the request's row and the CLI's wait, and this module does
the rest::

    requests = WorkerRequests(conn, core=tool_core, channel=hub, principals=principals)
    spec = requests.find("dori", "prod_deploy")          # None: not requestable here
    args = requests.fit_args(spec, (("commit", "abc"),))  # ValueError: they don't fit
    done = await requests.fulfil(run, qid, spec, args, reason, on_approval=..., on_ran=...)

- **Requestable.** Only an extension tool its manifest marks ``worker_requestable``
  (``ToolSpec.worker_requestable``); anything else is refused before the gate.
- **Args.** The CLI's ``k=v`` strings are fitted to the tool's JSON schema (an integer arg
  becomes an int, a boolean ``true``/``false``), every key must be one the schema names
  (``additionalProperties: false`` whatever the schema says), and the result is checked
  against the schema.
- **The call.** ``ToolCore.call`` with the run's requester as principal (``run.requester``),
  ``origin="inferred"`` (a worker never claims ``explicit``), at step ``REQUEST_STEP`` with
  the request's row id as ``call_index`` (its idempotency key), on the request's own task
  (kind ``request``, runner ``worker_request``: ``running`` while it waits so its approval
  can be answered, never dispatched; done, failed or cancelled with the request). Not the
  run's task: the gate keeps one approval per (task, intent), so on the run's task a request
  asked again after an expiry or a restart could never be asked again. The gate
  decides as for any call: a member must hold ``request`` for the tool, the approvers are the
  gate's (the owner, every approver, the manifest's granted names: a member who requested
  the run can't approve it), and an L1 tool asks with a code (a Slack button carries it).
- **The approval.** An ``Asked`` call's approval is prompted to its approvers, one copy in the
  run's thread (``run_thread``; addressed to the requester when they are an approver, else
  to the first approver) and the rest in their own conversations; the prompt says which run
  asks and quotes the worker's reason (cleaned). It waits (polling the row) for a decision,
  the approval's expiry, or the run ending. Approved: the same call again, which the gate
  allows on that approval, and the executor runs it once. Denied, expired, unreachable or
  ended: the tool never runs.
- **One request per action** (PER-269). A requestable tool's call is an action
  (``gate.actions.args_target``): a second run asking for the same deploy while the first's
  approval is open gets no second approval; it is told which run already asked.
- **Results.** A code-built line for the worker: ``Approved by Brian: Done: <summary>`` plus
  the tool's output (clipped), ``Denied by Justin: the tool didn't run.``, ``No approver
  answered in time …``. A call that ran is reported through ``on_ran`` as a ``did:`` line
  (``reports.record_did``), even when the request's wait was cancelled meanwhile: once
  approved, the run itself is shielded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from resonant.gate import NeedsApproval
from resonant.gate import actions as action_ids
from resonant.gate.codes import clean_external
from resonant.gateway.channel import Channel, ChannelError
from resonant.loop import NewTask
from resonant.loop.types import PRIORITY_OWNER
from resonant.principals import Principals
from resonant.runners.approve import prompt_approvers
from resonant.runners.run_thread import run_thread
from resonant.store import approvals as ap
from resonant.store import questions as qs
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.store.audit import audit
from resonant.store.db import atomic, utcnow
from resonant.store.runs import Run
from resonant.toolcore import Asked, CallOutcome, CallScope, Failed, Ran, Refused, ToolCore
from resonant.toolcore.core import args_error
from resonant.toolcore.reports import summarize_intent
from resonant_sdk import ToolSpec

log = logging.getLogger(__name__)

REQUEST_STEP = 296_000  # the step of every request's call: apart from any task loop's steps
POLL_S = 1.0
REPROMPT_S = 30.0  # re-send the prompt after a transient channel failure
OUTPUT_MAX = 1500
REASON_MAX = 200
_INT = re.compile(r"-?\d{1,18}")

NOT_RUN = "the tool didn't run."
UNREACHABLE = f"No approver could be reached: {NOT_RUN}"
EXPIRED = f"No approver answered in time (the approval expired): {NOT_RUN}"
RUN_ENDED = f"The run ended before an approver answered: {NOT_RUN}"
REQUEST_KIND = "request"  # a request's own task: the gate's approval scope
REQUEST_RUNNER = "worker_request"  # no runner has this name: the task is never dispatched

Waited = Literal["approved", "rejected", "expired", "unreachable", "run_ended"]


@dataclass(frozen=True)
class Fulfilled:
    """How a request ended: ``ok`` (the tool ran and succeeded), the code-built ``text`` the
    worker reads, and ``did`` (the side-effect line) when it ran."""

    ok: bool
    text: str
    did: str | None = None


def close_stale_tasks(conn: sqlite3.Connection) -> int:
    """Daemon start: no request's wait survived, so their tasks are cancelled (never
    dispatched: they have no runner)."""
    return conn.execute(
        "UPDATE tasks SET status = 'cancelled' WHERE kind = ? AND runner = ?"
        " AND status IN ('queued', 'running', 'waiting')",
        (REQUEST_KIND, REQUEST_RUNNER),
    ).rowcount


def describe(spec: ToolSpec) -> tuple[str, str]:
    """``("dori.prod_deploy", "Deploy DORI to production.")`` for the shim's ``--help``."""
    return f"{spec.extension}.{spec.name}", spec.description


def _clip(text: str, limit: int) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


class WorkerRequests:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        core: ToolCore,
        channel: Channel,
        principals: Principals,
        clock: Callable[[], datetime] = utcnow,
        poll_s: float = POLL_S,
        reprompt_s: float = REPROMPT_S,
    ) -> None:
        self.conn = conn
        self.core = core
        self.channel = channel
        self.principals = principals
        self.clock = clock
        self.poll_s = poll_s
        self.reprompt_s = reprompt_s

    # -- what may be requested ---------------------------------------------------------------

    def requestable(self) -> list[ToolSpec]:
        """Every registered extension tool marked ``worker_requestable``, by name."""
        return [s for s in self.core.registry.specs() if s.worker_requestable and s.extension]

    def find(self, extension: str, tool: str) -> ToolSpec | None:
        """The registered spec, when it is requestable; else None."""
        spec = self.core.registry.get(extension, tool)
        return spec if spec is not None and spec.worker_requestable else None

    def fit_args(self, spec: ToolSpec, pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
        """The CLI's ``k=v`` strings as the tool's args (module docstring). Raises
        ``ValueError`` with a short reason (no values) when they don't fit."""
        name = f"{spec.extension}.{spec.name}"
        raw_props = spec.args_schema.get("properties")
        props = cast(dict[str, Any], raw_props) if isinstance(raw_props, dict) else {}
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"--arg {key} is given twice")
            if key not in props:
                takes = ", ".join(sorted(props)) or "no args"
                raise ValueError(f"{name} has no arg {key} (it takes: {takes})")
            out[key] = _coerce(key, value, props[key])
        if (why := args_error(spec, out)) is not None:
            raise ValueError(f"the args don't fit {name} ({why})")
        return out

    # -- one request -------------------------------------------------------------------------

    async def fulfil(
        self,
        run: Run,
        qid: int,
        spec: ToolSpec,
        args: dict[str, Any],
        reason: str = "",
        *,
        on_approval: Callable[[str], None] = lambda _id: None,
        on_ran: Callable[[str], None] = lambda _did: None,
    ) -> Fulfilled:
        """Call ``spec`` for ``run`` through the gate; ask and wait when it asks (module
        docstring). ``on_approval(id)`` hears the approval as soon as it opens, ``on_ran(did)``
        a call that ran. Never raises for a "no"."""
        task_id = self._open_task(run, spec, qid)
        try:
            done = await self._fulfil(run, task_id, qid, spec, args, reason, on_approval, on_ran)
        except asyncio.CancelledError:
            self._close_task(task_id, "cancelled")
            raise
        self._close_task(task_id, "done" if done.ok else "failed")
        return done

    async def _fulfil(
        self,
        run: Run,
        task_id: str,
        qid: int,
        spec: ToolSpec,
        args: dict[str, Any],
        reason: str,
        on_approval: Callable[[str], None],
        on_ran: Callable[[str], None],
    ) -> Fulfilled:
        scope = CallScope(
            task_id=task_id,
            principal=run.requester,
            trace_id=None,
            step_no=REQUEST_STEP,
            call_index=qid,
            origin="inferred",
        )
        outcome = await self.core.call(spec, args, scope)
        if not isinstance(outcome, Asked):
            return self._finish(outcome, spec, args, None, on_ran)
        needs = outcome.decision
        on_approval(needs.approval_id)
        summary = self._prompt_summary(run, outcome, reason)
        waited, row = await self._wait(run, task_id, needs, summary)
        by = self.principals.display(row.decided_by) if row and row.decided_by else "an approver"
        if waited == "rejected":
            return Fulfilled(False, f"Denied by {by}: {NOT_RUN}")
        if waited != "approved":
            return Fulfilled(False, {"expired": EXPIRED, "unreachable": UNREACHABLE}.get(
                waited, RUN_ENDED))  # fmt: skip

        async def run_it() -> Fulfilled:
            again = await self.core.call(spec, args, scope)
            return self._finish(again, spec, args, by, on_ran)

        # Approved: the run of the tool outlives a cancelled wait (its did: line still lands).
        return await asyncio.shield(asyncio.ensure_future(run_it()))

    def _open_task(self, run: Run, spec: ToolSpec, qid: int) -> str:
        """The request's own task (kind ``request``, ``running`` while it waits, so its
        approval can be answered; never dispatched). Its own scope lets a request asked again
        after an expiry or a restart open a fresh approval: the gate keeps one per (task,
        intent)."""
        name = f"{spec.extension}.{spec.name}"
        tid = task_store.insert_task(
            self.conn,
            NewTask(
                kind=REQUEST_KIND,
                runner=REQUEST_RUNNER,
                priority=PRIORITY_OWNER,
                principal=run.principal,
                checkpoint={"title": f"{run.code} requests {name}", "request": qid},
                title=f"{run.code} requests {name}",
            ),
        )
        task_store.set_status(self.conn, tid, "running", expect=("queued",))
        return tid

    def _close_task(self, task_id: str, status: Literal["done", "failed", "cancelled"]) -> None:
        with contextlib.suppress(sqlite3.Error):
            task_store.set_status(self.conn, task_id, status,
                                  expect=("queued", "running", "waiting"))  # fmt: skip

    def _finish(
        self,
        outcome: CallOutcome,
        spec: ToolSpec,
        args: dict[str, Any],
        by: str | None,
        on_ran: Callable[[str], None],
    ) -> Fulfilled:
        head = f"Approved by {by}: " if by else ""
        name = f"{spec.extension}.{spec.name}"
        match outcome:
            case Ran():
                report = outcome.report or f"Done: {summarize_intent(outcome.intent)}"
                data = outcome.result.data or {}
                text = head + report
                if data:
                    text += f"\nOutput: {_clip(json.dumps(data, default=str), OUTPUT_MAX)}"
                did = f"ran {name} {action_ids.args_target(args)}"
                if by:
                    did += f" (approved by {by})"
                with contextlib.suppress(Exception):
                    on_ran(did)
                return Fulfilled(True, text, did)
            case Failed():
                return Fulfilled(False, head + outcome.text)
            case Refused():
                return Fulfilled(False, head + (self._handled(outcome) or outcome.text))
            case Asked():  # an approved call that asks again: its approval was spent
                return Fulfilled(False, f"{head}it asked for approval again, so {NOT_RUN}")

    def _handled(self, refused: Refused) -> str | None:
        """PER-269: the gate refused because another request for this action is open; say
        which run holds it (or Resonant, for one a brain or job opened)."""
        if refused.why != "denied" or refused.intent is None:
            return None
        action = action_ids.for_intent(self.conn, refused.intent)
        if action is None:
            return None
        held = ap.pending_for_action(self.conn, action.key, self.clock())
        if not held:
            return None
        other = qs.by_approval(self.conn, held[0].id)
        who = other.run_code if other is not None else "Resonant"
        return (
            f"{who} already asked an approver for this (one request per action), so no second "
            "approval went out. Don't do it yourself; if it's needed, it is done that way."
        )

    def _prompt_summary(self, run: Run, asked: Asked, reason: str) -> str:
        """``R4 (for Miu) asks to deploy voyager-service-prod abc: "hotfix"``. Code-built:
        the gate's summary, and the worker's reason cleaned and quoted."""
        who = run.code
        if run.requester != run.principal:
            who += f" (for {self.principals.display(run.requester)})"
        summary = asked.decision.summary
        text = f"{who} asks to {summary}" if asked.decision.phrased else f"{who} asks for {summary}"
        shown = _clip(clean_external(reason).replace('"', "'"), REASON_MAX)
        return text + (f': "{shown}"' if shown else "")

    async def _prompt(
        self, run: Run, task_id: str, needs: NeedsApproval, summary: str
    ) -> bool | None:
        """Prompt the approvers (one copy in the run's thread). True: reached; False:
        unreachable for good; None: retry."""
        thread = run_thread(self.conn, run)
        approvers = needs.approvers
        who = run.requester if run.requester in approvers else (approvers[0] if approvers else None)
        in_thread = (who, thread) if who is not None and thread is not None else None
        try:
            result = await prompt_approvers(
                self.conn, self.channel, needs, task_id=task_id, now=self.clock,
                in_thread=in_thread, summary=summary,
            )  # fmt: skip
        except ChannelError:
            return None
        if result.reached:
            return True
        return None if result.retry else False

    async def _wait(
        self, run: Run, task_id: str, needs: NeedsApproval, summary: str
    ) -> tuple[Waited, ap.Approval | None]:
        """Prompt, then wait for a decision, the expiry, or the run ending. A cancelled wait
        expires the approval, so a later click binds nothing."""
        try:
            return await self._wait_inner(run, task_id, needs, summary)
        except asyncio.CancelledError:
            with atomic(self.conn):
                ap.expire_one(self.conn, needs.approval_id, self.clock(), reason="cancelled")
            raise

    async def _wait_inner(
        self, run: Run, task_id: str, needs: NeedsApproval, summary: str
    ) -> tuple[Waited, ap.Approval | None]:
        reached = await self._prompt(run, task_id, needs, summary)
        if reached is False:
            self._expire(needs.approval_id, "unreachable")
            return "unreachable", None
        last_prompt = self.clock()
        while True:
            now = self.clock()
            with atomic(self.conn):
                ap.expire_due(self.conn, now)
                row = ap.get_approval(self.conn, needs.approval_id)
                current = runs_store.get(self.conn, run.code)
                ended = current is None or not current.active
                if row is not None and row.status == "pending" and ended:
                    ap.expire_one(self.conn, row.id, now, reason="run ended")
                    return "run_ended", row
            if row is None or row.status == "expired":
                return "expired", row
            if row.status == "rejected":
                return "rejected", row
            if row.status == "approved":
                return "approved", row
            if reached is None and (now - last_prompt).total_seconds() >= self.reprompt_s:
                reached = await self._prompt(run, task_id, needs, summary)  # the channel dedupes
                last_prompt = now
                if reached is False:
                    self._expire(needs.approval_id, "unreachable")
                    return "unreachable", None
            await asyncio.sleep(self.poll_s)

    def _expire(self, approval_id: str, reason: str) -> None:
        with atomic(self.conn):
            ap.expire_one(self.conn, approval_id, self.clock(), reason=reason)
            audit(self.conn, "run.request_unreachable", approval_id=approval_id, reason=reason)


def _coerce(key: str, value: str, prop: Any) -> Any:
    """One CLI string as the schema's type for ``key`` (the first type it fits)."""
    schema = cast(dict[str, Any], prop) if isinstance(prop, dict) else {}
    raw = schema.get("type")
    types: list[Any] = cast(list[Any], raw) if isinstance(raw, list) else [raw]
    for t in types:
        if t is None or t == "string":
            return value
        if t == "integer" and _INT.fullmatch(value.strip()):
            return int(value)
        if t == "number":
            with contextlib.suppress(ValueError):
                number = float(value)
                if number == number and abs(number) != float("inf"):
                    return int(number) if _INT.fullmatch(value.strip()) else number
        if t == "boolean" and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        if t in ("array", "object"):
            with contextlib.suppress(ValueError):
                parsed: Any = json.loads(value)
                if isinstance(parsed, list if t == "array" else dict):
                    return cast(Any, parsed)
        if t == "null" and value.strip().lower() in ("null", ""):
            return None
    shown = "/".join(str(t) for t in types)
    raise ValueError(f"--arg {key} must be {shown}")

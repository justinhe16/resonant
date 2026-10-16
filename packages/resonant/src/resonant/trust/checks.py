"""Worker trust checks: the daemon side of ``resonant check`` (docs/employee-plan.md D4).

API for EM-3 (the worker CLI's per-run socket)::

    checks = TrustChecks(conn, channel, principals, approval_ttl_s=1800)
    answer = await checks.request_check(
        "R4", system="dori-api", action="deploy", irreversible=False, cost_usd=0.0,
        what="deploy dori-api to prod",
    )  # "ok" | "denied"

``request_check`` never raises for a "no": it returns ``"denied"``. It raises
``ValueError`` only for a bad ``system`` or ``action`` (not a slug, or ``*``), which the
CLI should report to the worker as a usage error. ``IMessageStack.trust`` holds the
daemon's instance.

What it does:

1. ``resonant.trust.evaluate`` decides (rules, then the net-new defaults) and audits.
   ``ok`` returns ``"ok"`` at once.
2. ``ask``: it creates an owner approval on the run's task, through the same store, codes
   and prompt path as the gate's approvals (``runners.approve.prompt_approvers``). The
   approval is plain-yes (L2-style, never ``require_code``) and its prompt reads::

       [K4] R4 wants to: deploy dori-api to prod (new kind of action). Reply yes / yes always / no

   ``yes always`` is offered only when no rule asked: an owner's own ``ask`` rule is
   changed over chat, not overridden from a prompt. The approval's tool is
   ``CHECK_TOOL``; the intent hash covers the run and the check, so a worker repeating
   the same check while it is pending reuses the one approval.
3. It waits (polling the row) until the owner answers or the approval expires
   (``gate.approval_ttl_s``). A ``yes`` returns ``"ok"`` and marks (system, action) seen.
   ``yes always`` also adds ``allow <action> on <system>`` (``created_from="yes always"``)
   and texts ``Rule added: don't ask before deploy in dori-api (rule 5)``. A ``no``, an
   expiry, a run that isn't active, or no reachable owner returns ``"denied"``.

The replies are bound by the gate's ``resolve_reply`` like any approval: only the owner,
over iMessage, after the prompt was sent. A worker can't answer its own check: it has no
way to send an iMessage (the thin guard) and nothing it writes is read as a reply. The
``what`` text is the worker's and is cleaned (``clean_external``: printable ASCII, brackets
dropped, code-like tokens masked) and clipped before it is shown.

Every outcome is audited (``trust.check``), on top of the ``trust.evaluate`` row and the
approval's own ``approval.resolve`` row.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from resonant.gate.codes import new_code
from resonant.gate.types import NeedsApproval, canonical_json
from resonant.gateway.channel import Channel, ChannelError
from resonant.principals import Principals
from resonant.runners.approve import prompt_approvers
from resonant.store import approvals as ap
from resonant.store import runs as runs_store
from resonant.store import trust as store
from resonant.store.audit import audit
from resonant.store.db import atomic, utcnow
from resonant.trust import TrustDecision, clean_what, evaluate
from resonant_sdk import Effect, Level

log = logging.getLogger(__name__)

CHECK_TOOL = "trust_check"  # the approvals' tool name; no registered tool has it
POLL_S = 1.0
REPROMPT_S = 30.0  # re-send the prompt after a transient channel failure

Answer = Literal["ok", "denied"]


def check_summary(run_code: str, what: str, system: str, action: str, reason: str) -> str:
    """``R4 wants to: deploy dori-api to prod (new kind of action)``. Built by code;
    ``what`` is already cleaned."""
    doing = what or f"{action} on {system}"
    return f"{run_code} wants to: {doing} ({reason})"


def check_hash(
    run_code: str, system: str, action: str, irreversible: bool, cost: float, what: str
) -> str:
    payload = canonical_json(
        {
            "tool": CHECK_TOOL,
            "run": run_code,
            "system": system,
            "action": action,
            "irreversible": irreversible,
            "cost_usd": cost,
            "what": what,
        }
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class TrustChecks:
    def __init__(
        self,
        conn: sqlite3.Connection,
        channel: Channel,
        principals: Principals,
        *,
        approval_ttl_s: float,
        clock: Callable[[], datetime] = utcnow,
        poll_s: float = POLL_S,
    ) -> None:
        self.conn = conn
        self.channel = channel
        self.principals = principals
        self.ttl = timedelta(seconds=approval_ttl_s)
        self.clock = clock
        self.poll_s = poll_s

    async def request_check(
        self,
        run_code: str,
        system: str,
        action: str,
        irreversible: bool = False,
        cost_usd: float = 0.0,
        what: str = "",
    ) -> Answer:
        """``"ok"`` or ``"denied"`` for one worker check (see the module docstring)."""
        system, action = store.slug(system), store.slug(action)
        run = runs_store.get(self.conn, run_code)
        if run is None or not run.active or run.task_id is None:
            self._audit(run_code, system, action, "denied", "run not active")
            return "denied"
        decision = evaluate(
            self.conn,
            system,
            action,
            irreversible,
            cost_usd,
            what,
            repo=run.repo or None,
            run=run.code,
            now=self.clock(),
        )
        if decision.ok:
            return "ok"
        approval = self._approval(
            run.code, run.task_id, decision, system, action, irreversible, cost_usd, what
        )
        if approval is None:
            self._audit(run.code, system, action, "denied", "no approval")
            return "denied"
        answer = await self._wait(run.code, run.task_id, approval, system, action)
        self._audit(run.code, system, action, answer, None, approval_id=approval.approval_id)
        return answer

    # -- internals --

    def _approval(
        self,
        run_code: str,
        task_id: str,
        decision: TrustDecision,
        system: str,
        action: str,
        irreversible: bool,
        cost_usd: float,
        what: str,
    ) -> NeedsApproval | None:
        """The pending approval for this check: reused while one is pending, else new."""
        shown = clean_what(what)
        intent_hash = check_hash(
            run_code, system, action, bool(irreversible), float(cost_usd), shown
        )
        now = self.clock()
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            row = ap.pending_for_intent(self.conn, task_id, intent_hash, now)
            if row is None:
                try:
                    code = new_code(ap.pending_codes(self.conn))
                except RuntimeError:  # every approval code is taken
                    return None
                approval_id = ap.insert_approval(
                    self.conn,
                    ap.NewApproval(
                        task_id=task_id,
                        intent_hash=intent_hash,
                        tool=CHECK_TOOL,
                        effect=Effect.WRITE,
                        level=Level.L2,
                        summary=check_summary(run_code, shown, system, action, decision.reason),
                        approvers=(self.principals.owner,),
                        code=code,
                        expires_at=now + self.ttl,
                        require_code=False,
                        requested_by=run_code,
                        # An owner's own ask rule is changed over chat, not from a prompt.
                        allow_always=decision.rule is None,
                    ),
                    now,
                )
                row = ap.get_approval(self.conn, approval_id)
        assert row is not None and row.code is not None
        return NeedsApproval(
            approval_id=row.id,
            code=row.code,
            approvers=row.approvers,
            expires_at=row.expires_at,
            summary=row.summary,
            effect=row.effect,
            level=row.level,
            require_code=row.require_code,
            allow_always=row.allow_always,
        )

    async def _prompt(self, task_id: str, needs: NeedsApproval) -> bool | None:
        """Prompt the owner. True: reached; False: unreachable for good; None: retry."""
        try:
            result = await prompt_approvers(
                self.conn, self.channel, needs, task_id=task_id, now=self.clock
            )
        except ChannelError:
            return None
        if result.reached:
            return True
        return None if result.retry else False

    async def _wait(
        self, run_code: str, task_id: str, needs: NeedsApproval, system: str, action: str
    ) -> Answer:
        """Prompt, then wait for the owner's answer or the expiry."""
        reached = await self._prompt(task_id, needs)
        if reached is False:
            return "denied"
        last_prompt = self.clock()
        while True:
            now = self.clock()
            with atomic(self.conn):
                ap.expire_due(self.conn, now)
                row = ap.get_approval(self.conn, needs.approval_id)
            if row is None or row.status in ("rejected", "expired"):
                return "denied"
            if row.status == "approved":
                return await self._approved(run_code, row, system, action)
            if reached is None and (now - last_prompt).total_seconds() >= REPROMPT_S:
                reached = await self._prompt(task_id, needs)  # the channel dedupes
                last_prompt = now
                if reached is False:
                    return "denied"
            await asyncio.sleep(self.poll_s)

    async def _approved(self, run_code: str, row: ap.Approval, system: str, action: str) -> Answer:
        """Spend the approval, mark the pair seen, and add the rule on ``yes always``."""
        now = self.clock()
        added: store.Added | None = None
        with atomic(self.conn):
            # Waiters on one approval (a worker repeating a pending check) share the answer;
            # only the first spends it and adds the rule. A later check asks again.
            first = ap.mark_used(self.conn, row.id, now)
            store.mark_seen(self.conn, system, action, now)
            if first and row.always and row.allow_always and not row.require_code:
                added = store.add_rule(
                    self.conn,
                    scope=system,
                    action=action,
                    effect="allow",
                    created_from="yes always",
                    now=now,
                    note=f"yes always to {run_code}",
                )
                audit(
                    self.conn,
                    "trust.rule_added",
                    approved_by=row.decided_by,
                    rule_id=added.rule.id,
                    scope=added.rule.scope,
                    action_kind=added.rule.action,
                    effect=added.rule.effect,
                    created_from="yes always",
                    approval_id=row.id,
                    run=run_code,
                    replaced=added.replaced.id if added.replaced else None,
                )
        if added is not None and row.decided_by is not None:
            text = f"Rule added: {added.rule.text} (rule {added.rule.id})"
            try:
                await self.channel.send(row.decided_by, text, dedupe_key=f"trust:{row.id}:rule")
            except ChannelError:
                log.warning("couldn't confirm rule %s", added.rule.id)
        return "ok"

    def _audit(
        self,
        run_code: str,
        system: str,
        action: str,
        answer: Answer,
        why: str | None,
        *,
        approval_id: str | None = None,
    ) -> None:
        with atomic(self.conn):
            audit(
                self.conn,
                "trust.check",
                decision=answer,
                run=run_code,
                system=system,
                action_kind=action,
                reason=why,
                approval_id=approval_id,
            )

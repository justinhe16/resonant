"""Worker trust checks: the daemon side of ``resonant check`` (docs/employee-plan.md D4).

API for EM-3 (the worker CLI's per-run socket)::

    checks = TrustChecks(conn, channel, principals, approval_ttl_s=1800)
    answer = await checks.request_check(
        "R4", system="dori-api", action="deploy", irreversible=False, cost_usd=0.0,
        what="deploy dori-api to prod",
    )  # "ok" | "denied"

``request_check`` never raises for a "no": it returns ``"denied"``. ``"busy"`` means the
check wasn't asked at all, because ``MAX_PENDING`` of the run's checks already wait for the
owner (the worker is told to wait for those answers, which is not an owner "no"). It raises
``ValueError`` only for a bad ``system`` or ``action`` (not a slug, or ``*``), which the
CLI should report to the worker as a usage error. ``IMessageStack.trust`` holds the
daemon's instance.

What it does:

1. ``resonant.trust.evaluate`` decides (rules, then the net-new defaults) and audits.
   ``ok`` returns ``"ok"`` at once.
2. ``ask``: it creates an owner approval on the run's task, through the same store, codes
   and prompt path as the gate's approvals (``runners.approve.prompt_approvers``). The
   approval is plain-yes (L2-style, never ``require_code``) and its prompt reads::

       [K4] R4 wants to deploy on dori-api: "deploy to prod" (new kind of action; yes
       always = don't ask before deploy in dori-api). Reply yes / yes always / no

   The action and system, which a ``yes always`` rule would cover, are always shown, and
   the worker's ``what`` is quoted. ``yes always`` is offered only when no rule asked (an
   owner's own ``ask`` rule is changed over chat, not overridden from a prompt) and the
   system isn't an area of a system already seen or a rule's scope (``dori`` would cover
   ``dori-prod``). At most ``MAX_PENDING`` checks per run wait at once. The approval's tool is
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
from resonant.trust.policy import is_area
from resonant_sdk import Effect, Level

log = logging.getLogger(__name__)

CHECK_TOOL = ap.CHECK_TOOL  # the approvals' tool name; no registered tool has it
POLL_S = 1.0
REPROMPT_S = 30.0  # re-send the prompt after a transient channel failure

Answer = Literal["ok", "denied", "busy"]


MAX_PENDING = 3  # pending checks per run; more is approval spam


def check_summary(
    run_code: str, what: str, system: str, action: str, reason: str, *, always: bool
) -> str:
    """``R4 wants to deploy on dori-api: "deploy to prod" (new kind of action; yes always =
    don't ask before deploy in dori-api)``. Built by code. The action and system (what a
    ``yes always`` rule would cover) are always shown; the worker's ``what`` is already
    cleaned (no double quotes) and is quoted, so it can't pose as the reason or the rule."""
    doing = f"{run_code} wants to {action} on {system}"
    if what:
        doing += f': "{what}"'
    if always:
        reason += f"; yes always = {store.describe(system, action, 'allow')}"
    return f"{doing} ({reason})"


def _covers_others(conn: sqlite3.Connection, system: str, repo: str | None) -> bool:
    """True if ``system`` is an area of the run's repo, a system already seen or a rule's
    scope, so an allow rule on it would cover more than this one system (``dori`` covers
    ``dori-prod``)."""
    rows = conn.execute(
        "SELECT system AS name FROM trust_seen UNION SELECT scope FROM trust_rules"
    ).fetchall()
    names = [r["name"] for r in rows] + ([repo] if repo else [])
    return any(is_area(system, name) for name in names)


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
        """``"ok"``, ``"denied"`` or ``"busy"`` for one worker check (see the module
        docstring)."""
        system, action = store.slug(system), store.slug(action)
        if store.ANY in (system, action):
            raise ValueError("a check names one system and one action, not *")
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
            run.code,
            run.task_id,
            decision,
            system,
            action,
            irreversible,
            cost_usd,
            what,
            repo=store.slug(run.repo) if run.repo else None,
        )
        if approval is None:  # not asked: the worker is told to wait (EM-5), not "no"
            self._audit(run.code, system, action, "busy", "too many pending checks")
            return "busy"
        try:
            answer, why = await self._wait(run.code, run.task_id, approval, system, action)
        except asyncio.CancelledError:
            # The worker went away (its socket closed): a later reply must bind nothing. If
            # the owner had already said yes (the cancel hit the confirmation), say so.
            with atomic(self.conn):
                expired = ap.expire_one(
                    self.conn, approval.approval_id, self.clock(), reason="cancelled"
                )
            answer, why = ("denied", "cancelled") if expired else ("ok", "cancelled after answer")
            self._audit(run.code, system, action, answer, why, approval_id=approval.approval_id)
            raise
        self._audit(run.code, system, action, answer, why, approval_id=approval.approval_id)
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
        *,
        repo: str | None,
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
                pending = self.conn.execute(
                    "SELECT COUNT(*) FROM approvals WHERE task_id = ? AND tool = ?"
                    " AND status = 'pending'",
                    (task_id, CHECK_TOOL),
                ).fetchone()[0]
                if pending >= MAX_PENDING:
                    return None
                # An owner's own ask rule is changed over chat, not from a prompt; and a
                # rule on an area of other systems would cover more than was asked.
                always = decision.rule is None and not _covers_others(self.conn, system, repo)
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
                        summary=check_summary(
                            run_code, shown, system, action, decision.reason, always=always
                        ),
                        approvers=(self.principals.owner,),
                        code=code,
                        expires_at=now + self.ttl,
                        require_code=False,
                        requested_by=run_code,
                        allow_always=always,
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
    ) -> tuple[Answer, str | None]:
        """Prompt, then wait for the owner's answer, the expiry, or the run ending."""
        reached = await self._prompt(task_id, needs)
        if reached is False:
            with atomic(self.conn):
                ap.expire_one(self.conn, needs.approval_id, self.clock(), reason="unreachable")
            return "denied", "owner unreachable"
        last_prompt = self.clock()
        while True:
            now = self.clock()
            with atomic(self.conn):
                ap.expire_due(self.conn, now)
                row = ap.get_approval(self.conn, needs.approval_id)
                run = runs_store.get(self.conn, run_code)
                if row is not None and row.status == "pending" and (run is None or not run.active):
                    ap.expire_one(self.conn, row.id, now, reason="run ended")
                    return "denied", "run ended"
            if row is None or row.status in ("rejected", "expired"):
                return "denied", row.status if row is not None else "not found"
            if row.status == "approved":
                return await self._approved(run_code, row, system, action), None
            if reached is None and (now - last_prompt).total_seconds() >= REPROMPT_S:
                reached = await self._prompt(task_id, needs)  # the channel dedupes
                last_prompt = now
                if reached is False:
                    with atomic(self.conn):
                        ap.expire_one(self.conn, needs.approval_id, now, reason="unreachable")
                    return "denied", "owner unreachable"
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
                if not added.existed:
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
                        replaced=store.rule_fields(added.replaced),
                    )
        if added is not None and not added.existed and row.decided_by is not None:
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

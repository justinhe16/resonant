"""The ``resonant check`` seam (docs/employee-plan.md D4): what the worker CLI calls to decide
whether a worker may go ahead.

``TrustChecker`` is the shape of EM-4's ``resonant.trust.checks.TrustChecks.request_check``:
``"ok"`` or ``"denied"``, never raising for a no (a bad ``system`` or ``action`` raises
``ValueError``, reported to the worker as a usage error). Until EM-4's trust rules are wired
in, ``AskOwnerTrust`` is the default: every check asks the owner, as a plain-yes approval on
the run's task, through the same approvals store, codes and prompt path as the gate's
(``runners.approve.prompt_approvers``). A ``yes`` is ``ok``; a ``no``, an expiry
(``runner.ask_timeout_minutes``), an ended run or an owner who can't be reached is
``denied``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal, Protocol

from resonant.gate.codes import clean_external, new_code
from resonant.gate.types import NeedsApproval
from resonant.gateway.channel import Channel
from resonant.runners.approve import prompt_approvers
from resonant.store import approvals as ap
from resonant.store import runs as runs_store
from resonant.store.audit import audit
from resonant.store.db import atomic, utcnow
from resonant_sdk import Effect, Level

log = logging.getLogger(__name__)

Answer = Literal["ok", "denied"]
CHECK_TOOL = "worker.check"
SUMMARY_MAX = 300
_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class TrustChecker(Protocol):
    async def request_check(
        self,
        run_code: str,
        system: str,
        action: str,
        irreversible: bool = False,
        cost_usd: float = 0.0,
        what: str = "",
    ) -> Answer: ...


def slug(text: str) -> str:
    """``system`` and ``action`` as one lowercase word (``dori-api``, ``deploy``); ValueError
    for anything else, ``*`` included."""
    value = text.strip().lower()
    if not _SLUG.fullmatch(value):
        raise ValueError(f"{text[:40]!r} isn't a name like dori-api or deploy")
    return value


def check_summary(
    run_code: str, system: str, action: str, irreversible: bool, cost_usd: float, what: str
) -> str:
    """The approval prompt's summary, built by code; the worker's ``what`` is cleaned."""
    shown = clean_external(what)
    text = f"{run_code} wants to: {action} on {system}"
    if shown:
        text += f": {shown}"
    if irreversible:
        text += " (irreversible)"
    if cost_usd > 0:
        text += f" (costs ${cost_usd:g})"
    return text if len(text) <= SUMMARY_MAX else text[: SUMMARY_MAX - 3].rstrip() + "..."


class AskOwnerTrust:
    """The default ``TrustChecker`` until EM-4: every check asks the run's requester."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        channel: Channel,
        *,
        ttl_s: float,
        clock: Callable[[], datetime] = utcnow,
        poll_s: float = 1.0,
    ) -> None:
        self.conn = conn
        self.channel = channel
        self.ttl = timedelta(seconds=ttl_s)
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
        system, action = slug(system), slug(action)
        run = runs_store.get(self.conn, run_code)
        if run is None or not run.active or run.task_id is None:
            return "denied"
        summary = check_summary(run.code, system, action, irreversible, cost_usd, what)
        blob = json.dumps([run.code, system, action, irreversible, cost_usd, summary])
        intent_hash = hashlib.sha256(blob.encode()).hexdigest()
        now = self.clock()
        with atomic(self.conn):
            ap.expire_due(self.conn, now)
            row = ap.pending_for_intent(self.conn, run.task_id, intent_hash, now)
            if row is None:  # the same check repeated while pending reuses its approval
                try:
                    code = new_code(ap.pending_codes(self.conn))
                except RuntimeError:  # every approval code is taken
                    return "denied"
                approval_id = ap.insert_approval(
                    self.conn,
                    ap.NewApproval(
                        task_id=run.task_id,
                        intent_hash=intent_hash,
                        tool=CHECK_TOOL,
                        effect=Effect.PAY if cost_usd > 0 else Effect.WRITE,
                        level=Level.L2,
                        summary=summary,
                        approvers=(run.principal,),
                        code=code,
                        expires_at=now + self.ttl,
                        require_code=False,
                        requested_by=run.code,
                    ),
                    now,
                )
                row = ap.get_approval(self.conn, approval_id)
                audit(self.conn, "run.check_asked", run=run.code, system=system, verb=action,
                      approval_id=approval_id)  # fmt: skip
        assert row is not None
        decision = NeedsApproval(
            approval_id=row.id,
            code=row.code or "",
            approvers=row.approvers,
            expires_at=row.expires_at,
            summary=row.summary,
            effect=row.effect,
            level=row.level,
            require_code=False,
        )
        sent = await prompt_approvers(
            self.conn, self.channel, decision, task_id=run.task_id, now=self.clock
        )
        if sent.none_reachable:
            return "denied"
        return await self._wait(row.id, run.code)

    async def _wait(self, approval_id: str, run_code: str) -> Answer:
        while True:
            with atomic(self.conn):
                ap.expire_due(self.conn, self.clock())
            row = ap.get_approval(self.conn, approval_id)
            if row is None or row.status in ("rejected", "expired"):
                return "denied"
            if row.status == "approved":
                return "ok"
            run = runs_store.get(self.conn, run_code)
            if run is None or not run.active:
                return "denied"
            await asyncio.sleep(self.poll_s)

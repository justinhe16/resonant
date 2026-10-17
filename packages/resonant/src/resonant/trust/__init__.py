"""Trust rules: standing trust for Claude workers (docs/employee-plan.md D4, D5).

The policy is pure (``resonant.trust.policy``); these wrappers read the store, decide and
record each evaluation in the audit log:

- ``evaluate(conn, system, action, irreversible, cost_usd, what, *, repo, run)`` ->
  ``TrustDecision(verdict "ok" | "ask", reason, rule)``. An ok check marks (system, action)
  seen, so it isn't net new next time. A cost over ``cost_ask_over_usd`` (config
  ``trust.cost_ask_over_usd``, default $20) asks. Audited ``trust.evaluate``.
- ``notify_level(conn, system, action, event, *, repo)`` -> ``NotifyDecision(level "now" |
  "digest", reason, rule)``: how soon the owner hears about a worker event (EM-6 delivers).
- ``merge_decision(conn, repo)`` -> ``"ok" | "ask"``: a merge is ok unless a rule (``ask
  merge`` on the repo, an area of it, or ``*``) says to ask. Audited ``trust.merge``. EM-7:
  a model-chosen ``merge_run`` and the auto-merge follow it.

Workers reach this through ``resonant.trust.checks.TrustChecks.request_check`` (EM-3's
``resonant check``), which asks the owner when the verdict is ``ask``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal

from resonant.gate.codes import clean_external
from resonant.store import trust as store
from resonant.store.audit import audit
from resonant.store.db import atomic, utcnow
from resonant.trust.policy import (
    COST_ASK_OVER_USD,
    Check,
    NotifyDecision,
    TrustDecision,
    decide,
    merge_rule,
)
from resonant.trust.policy import notify_level as _notify_level

__all__ = [
    "WHAT_MAX",
    "Check",
    "NotifyDecision",
    "TrustDecision",
    "evaluate",
    "merge_decision",
    "notify_level",
]

WHAT_MAX = 120  # the worker's description of the action, as stored and shown


def clean_what(what: str) -> str:
    """The worker's free text, safe to show: printable ASCII, code-like tokens masked,
    clipped to ``WHAT_MAX``."""
    flat = clean_external(what)
    return flat if len(flat) <= WHAT_MAX else flat[: WHAT_MAX - 1].rstrip() + "…"


def evaluate(
    conn: sqlite3.Connection,
    system: str,
    action: str,
    irreversible: bool = False,
    cost_usd: float = 0.0,
    what: str = "",
    *,
    repo: str | None = None,
    run: str | None = None,
    now: datetime | None = None,
    cost_ask_over_usd: float = COST_ASK_OVER_USD,
) -> TrustDecision:
    """Decide one check and record it. ``system`` and ``action`` must be slugs
    (``store.trust.slug`` normalizes them; anything else raises ``ValueError``). A cost
    above ``cost_ask_over_usd`` (config ``trust.cost_ask_over_usd``) asks."""
    check = Check(
        system=store.slug(system),
        action=store.slug(action),
        irreversible=bool(irreversible),
        cost_usd=float(cost_usd),
        what=clean_what(what),
        repo=store.slug(repo) if repo else None,
    )
    if store.ANY in (check.system, check.action):
        raise ValueError("a check names one system and one action, not *")
    at = now or utcnow()
    with atomic(conn):
        decision = decide(
            check,
            store.list_rules(conn),
            store.seen_actions(conn, check.system),
            cost_ask_over_usd=cost_ask_over_usd,
        )
        if decision.ok:
            store.mark_seen(conn, check.system, check.action, at)
        audit(
            conn,
            "trust.evaluate",
            decision=decision.verdict,
            run=run,
            system=check.system,
            action_kind=check.action,
            repo=check.repo,
            irreversible=check.irreversible,
            cost_usd=check.cost_usd,
            what=check.what,
            reason=decision.reason,
            rule_id=decision.rule.id if decision.rule else None,
        )
    return decision


def notify_level(
    conn: sqlite3.Connection, system: str, action: str, event: str, *, repo: str | None = None
) -> NotifyDecision:
    """How soon the owner hears about ``event`` on (system, action). Reads only."""
    return _notify_level(
        store.list_rules(conn),
        store.slug(system),
        store.slug(action),
        store.slug(event),
        store.slug(repo) if repo else None,
    )


def merge_decision(conn: sqlite3.Connection, repo: str) -> Literal["ok", "ask"]:
    """Whether merging ``repo``'s PR may go ahead without the owner (D4). Default ok."""
    name = store.slug(repo)
    with atomic(conn):
        rule = merge_rule(store.list_rules(conn), name)
        verdict: Literal["ok", "ask"] = "ask" if rule is not None and rule.effect == "ask" else "ok"
        audit(
            conn,
            "trust.merge",
            decision=verdict,
            repo=name,
            rule_id=rule.id if rule else None,
            reason=f"rule {rule.id}: {rule.text}" if rule else "default",
        )
    return verdict

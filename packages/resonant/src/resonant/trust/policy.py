"""Trust policy: pure functions over rules and the seen history (docs/employee-plan.md D4/D5).

``decide(check, rules, seen)`` -> ``TrustDecision("ok" | "ask", reason, rule)``:

1. **Rules first.** Of the ``allow``/``ask`` rules that match the check, the most specific
   wins, and at equal specificity ``ask`` beats ``allow``. Specificity is the scope first,
   then the action:

   - scope: the check's system exactly (3); the run's repo, or an *area* (2), the longer
     scope winning; ``*`` (0). An area is a scope that is a leading segment of the system or
     repo: ``trading`` covers ``trading-api`` and ``trading/prod`` (segments split at
     ``- . / : @``), never ``tradingview``.
   - action: the check's action exactly (1); ``*`` (0).

   A matching rule decides on its own: an ``allow`` rule overrides every default below,
   including irreversible and cost.
2. **Defaults** when no rule matches. Ask for a new system (no ok check recorded on it), a
   new kind of action on a system, anything irreversible, and anything that costs money
   (``cost_usd > 0``, or not a finite number). Everything else is ok. The reason lists every
   default that applied: ``new system, irreversible``.

``notify_level(rules, system, action, event)`` -> ``NotifyDecision("now" | "digest", ...)``.
Defaults: ``failed``, ``question``, ``check`` and ``urgent`` are texted right away; the rest
(``done``, ``pr``, ``merged``, ``note``) wait for the digest. ``notify_now``/``notify_digest``
rules change it, matched by scope as above and by action against the event (2), then the
action (1), then ``*`` (0); at equal specificity ``notify_now`` wins. A ``question`` or
``check`` is never lowered to the digest: a worker is blocked on it.

``merge_rule(rules, repo)``: the trust rule that decides a merge of ``repo``, if any. With
none, a merge is ok (D4: merges are ok once CI is green).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from resonant.store.trust import ANY, NOTIFY_EFFECTS, TRUST_EFFECTS, Rule

Verdict = Literal["ok", "ask"]
Level = Literal["now", "digest"]

# Events a worker can report (D5). Unknown events default to the digest.
URGENT_EVENTS: frozenset[str] = frozenset({"failed", "question", "check", "urgent"})
# Blocking events: a rule never lowers them to the digest.
BLOCKING_EVENTS: frozenset[str] = frozenset({"question", "check"})

MERGE = "merge"

_SEPARATORS = re.compile(r"[-./:@]")


@dataclass(frozen=True)
class Check:
    """One worker check. ``system`` and ``action`` are slugs (``store.trust.slug``)."""

    system: str
    action: str
    irreversible: bool = False
    cost_usd: float = 0.0
    what: str = ""
    repo: str | None = None  # the run's repo, a scope candidate


@dataclass(frozen=True)
class TrustDecision:
    verdict: Verdict
    reason: str  # "new kind of action", "rule 3: ask before merge in trading", "seen before"
    rule: Rule | None = None  # the rule that decided it, if one did

    @property
    def ok(self) -> bool:
        return self.verdict == "ok"


@dataclass(frozen=True)
class NotifyDecision:
    level: Level
    reason: str
    rule: Rule | None = None


def is_area(scope: str, name: str) -> bool:
    """``scope`` is a leading segment run of ``name`` (``trading`` of ``trading-api``)."""
    return (
        len(name) > len(scope)
        and name.startswith(scope)
        and _SEPARATORS.fullmatch(name[len(scope)]) is not None
    )


def scope_rank(scope: str, system: str, repo: str | None) -> tuple[int, int] | None:
    """How specifically ``scope`` names this system: (rank, length), or None if it doesn't."""
    if scope == system:
        return (3, len(scope))
    if scope == ANY:
        return (0, 0)
    if repo is not None and scope == repo:
        return (2, len(scope))
    if is_area(scope, system) or (repo is not None and is_area(scope, repo)):
        return (2, len(scope))
    return None


def _best[T](scored: Iterable[tuple[tuple[int, ...], int, T]]) -> T | None:
    """The item with the highest (specificity, tie-break) key."""
    best: tuple[tuple[int, ...], int, T] | None = None
    for item in scored:
        if best is None or (item[0], item[1]) > (best[0], best[1]):
            best = item
    return None if best is None else best[2]


def matching_trust_rule(
    rules: Iterable[Rule], system: str, action: str, repo: str | None
) -> Rule | None:
    """The ``allow``/``ask`` rule that decides (system, action), if any."""

    def scored() -> Iterable[tuple[tuple[int, ...], int, Rule]]:
        for r in rules:
            if r.effect not in TRUST_EFFECTS:
                continue
            s = scope_rank(r.scope, system, repo)
            if s is None:
                continue
            if r.action == action:
                a = 1
            elif r.action == ANY:
                a = 0
            else:
                continue
            # ask beats allow at equal specificity; then the newer rule.
            yield (s[0], s[1], a), (1 if r.effect == "ask" else 0) * 1_000_000_000 + r.id, r

    return _best(scored())


def costs_money(cost_usd: float) -> bool:
    return not math.isfinite(cost_usd) or cost_usd > 0


def decide(check: Check, rules: Iterable[Rule], seen: frozenset[str]) -> TrustDecision:
    """The verdict for ``check``. ``seen``: the actions with an ok check on its system."""
    rule = matching_trust_rule(rules, check.system, check.action, check.repo)
    if rule is not None:
        verdict: Verdict = "ask" if rule.effect == "ask" else "ok"
        return TrustDecision(verdict, f"rule {rule.id}: {rule.text}", rule)
    reasons: list[str] = []
    if not seen:
        reasons.append("new system")
    elif check.action not in seen:
        reasons.append("new kind of action")
    if check.irreversible:
        reasons.append("irreversible")
    if costs_money(check.cost_usd):
        shown = f"${check.cost_usd:.2f}" if math.isfinite(check.cost_usd) else "money"
        reasons.append(f"costs {shown}")
    if reasons:
        return TrustDecision("ask", ", ".join(reasons))
    return TrustDecision("ok", "seen before")


def notify_level(
    rules: Iterable[Rule], system: str, action: str, event: str, repo: str | None = None
) -> NotifyDecision:
    """How soon the owner hears about ``event`` (see the module docstring)."""

    def scored() -> Iterable[tuple[tuple[int, ...], int, Rule]]:
        for r in rules:
            if r.effect not in NOTIFY_EFFECTS:
                continue
            s = scope_rank(r.scope, system, repo)
            if s is None:
                continue
            if r.action == event:
                a = 2
            elif r.action == action:
                a = 1
            elif r.action == ANY:
                a = 0
            else:
                continue
            yield (s[0], s[1], a), (1 if r.effect == "notify_now" else 0) * 1_000_000_000 + r.id, r

    default: Level = "now" if event in URGENT_EVENTS else "digest"
    rule = _best(scored())
    if rule is None:
        return NotifyDecision(default, "default")
    level: Level = "now" if rule.effect == "notify_now" else "digest"
    if level == "digest" and event in BLOCKING_EVENTS:
        return NotifyDecision("now", f"{event} always texts right away")
    return NotifyDecision(level, f"rule {rule.id}: {rule.text}", rule)


def merge_rule(rules: Iterable[Rule], repo: str) -> Rule | None:
    """The trust rule that decides a merge of ``repo`` (system = repo, action ``merge``)."""
    return matching_trust_rule(rules, repo, MERGE, repo)

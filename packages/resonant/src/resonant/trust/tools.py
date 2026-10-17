"""Standing allows for chat tools (GT-1, docs/architecture.md Trust model).

An L2 tool the model chose for the owner asks a plain yes the first time. That prompt also
offers ``yes always``: the owner's answer saves an ``allow`` trust rule for the tool, and
from then on the owner's chat calls of that tool run as L3 (do, then report). The rule lives
in ``trust_rules`` with the worker rules, so ``/rules`` lists it and ``/rules forget <n>``
removes it:

- ``action``: ``tool:<name>`` for a builtin, ``tool:<extension>.<name>`` for an extension's
  tool (``tool_action``). Worker checks never name such an action, so a tool rule only ever
  widens that one tool, and a worker rule (``allow deploy in dori``) never widens a tool.
- ``scope``: the tool's natural scope. A call with a ``repo`` argument is scoped to that repo
  (the registered name when the registry knows it, else the argument as a slug); otherwise
  an extension's tool is scoped to its extension and a builtin to ``*``.

Matching (``StandingAllows.allows``) uses the worker rules' precedence
(``trust.policy.matching_trust_rule``) over the ``ask`` rules and the ``allow`` rules for
exactly this tool: an ``allow`` for another action, even ``*``, never widens a tool, and an
owner's ``ask`` rule that is at least as specific (``ask * in trading``) still asks.

What never gets a standing allow (``scope`` returns None, so the prompt doesn't offer
``yes always`` and no rule matches):

- the tools that loosen trust (``allow_trust_rule``, ``remove_trust_rule``): loosening is
  always the owner's own reply to that one change;
- ``merge_run``, whose level already follows the merge trust rules and flagged paths;
- an action or scope that isn't a valid slug (too long, odd characters).

The gate (``PolicyGate``) applies it only to the owner's own ``inferred`` calls of a tool at
L2, never at L1, never under the kill switch (autonomy off asks for everything) and never
in dry run (which comes first). The slash-only control commands (``/kill``, ``/resume``,
``/approvals``, ``/ext``) aren't tools the model can call, so a rule can't touch them.
A rule is made only by the owner's own ``yes always`` reply or ``/rules allow`` (or an
approved ``allow_trust_rule``), never by extension output.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from resonant.gate.types import Intent
from resonant.store import repos as repos_store
from resonant.store import trust as store
from resonant.trust.policy import matching_trust_rule

# Builtins that never take a standing allow (see the module docstring).
NEVER: frozenset[str] = frozenset({"allow_trust_rule", "remove_trust_rule", "merge_run"})

BUILTIN_SYSTEM = "resonant"  # the system a builtin's call is matched as


@dataclass(frozen=True)
class ToolScope:
    """What a standing allow for one call would cover: the rule's ``scope`` and ``action``,
    and the (system, repo) the call is matched as."""

    scope: str
    action: str
    system: str
    repo: str | None

    @property
    def text(self) -> str:
        """``don't ask before running github.merge_pr in jhe/dori``."""
        return store.describe(self.scope, self.action, "allow")


def tool_action(extension: str | None, tool: str) -> str | None:
    """``tool:<name>`` or ``tool:<extension>.<name>``, or None if it isn't a valid slug."""
    name = tool if extension is None else f"{extension}.{tool}"
    try:
        return store.slug(f"{store.TOOL_PREFIX}{name}")
    except ValueError:
        return None


class StandingAllows:
    """The gate's view of the owner's standing allows for chat tools."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def scope(self, intent: Intent) -> ToolScope | None:
        """The rule a ``yes always`` on this call would save, or None if it can't have one."""
        if intent.extension is None and intent.tool in NEVER:
            return None
        action = tool_action(intent.extension, intent.tool)
        if action is None:
            return None
        repo_arg = intent.args.get("repo")
        repo: str | None = None
        if isinstance(repo_arg, str) and repo_arg.strip():
            repo = self._repo(repo_arg)
            if repo is None:
                return None  # a repo we can't name as a scope: no standing allow
        system = intent.extension or BUILTIN_SYSTEM
        scope = repo or intent.extension or store.ANY
        return ToolScope(scope=scope, action=action, system=system, repo=repo)

    def decider(self, intent: Intent) -> tuple[ToolScope, store.Rule | None] | None:
        """The call's scope and the rule that decides it (an ``ask`` rule, or an ``allow``
        for exactly this tool), or None when the tool can't have a standing allow."""
        s = self.scope(intent)
        if s is None:
            return None
        rules = [
            r
            for r in store.list_rules(self.conn)
            if r.effect == "ask" or (r.effect == "allow" and r.action == s.action)
        ]
        return s, matching_trust_rule(rules, s.system, s.action, s.repo)

    def allows(self, intent: Intent) -> store.Rule | None:
        """The ``allow`` rule that lets this call run without asking, if any."""
        found = self.decider(intent)
        if found is None:
            return None
        rule = found[1]
        return rule if rule is not None and rule.effect == "allow" else None

    def offer(self, intent: Intent) -> ToolScope | None:
        """The rule to offer with ``yes always`` on this call's prompt: None when the tool
        can't have one or an owner's ``ask`` rule decided it (changed over chat, not from a
        prompt)."""
        found = self.decider(intent)
        if found is None:
            return None
        s, rule = found
        return s if rule is None or rule.effect != "ask" else None

    def _repo(self, value: str) -> str | None:
        found = repos_store.find(self.conn, value)
        name = found.name if found is not None else value
        try:
            return store.slug(name)
        except ValueError:
            return None

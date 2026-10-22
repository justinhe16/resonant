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

- the tools that loosen trust (``allow_trust_rule``, ``allow_tools``, ``remove_trust_rule``):
  loosening is always the owner's own word for that one change (a reply, or since PER-272
  their own direct ask, ``trust.asked``);
- a call its tool's ``stands`` hook refuses: ``merge_run`` (standable since PER-272, "stop
  asking before merging run PRs, just merge when green") stands only for a run with no
  flagged paths in a repo no ``ask merge`` rule covers (``tools.runs.merge_standable``); a
  merge that runs on it is unattended (``Allow.unattended``), so it needs green CI (it waits
  while CI runs), a CLEAN merge state and ``--match-head-commit``, and is always texted;
- an action or scope that isn't a valid slug (too long, odd characters).

The Claude brain's shell changes (``shell``, PER-258) are matched per verb and repo: the
action is ``tool:shell.<verb>`` (``tool:shell.git-push``) on the repo the command runs in, so
"don't ask for git push in resonant" is ``allow_trust_rule(scope=resonant,
action=tool:shell.git-push)``. Their prompt never offers ``yes always``.

The gate (``PolicyGate``) applies it only to the owner's own ``inferred`` calls of a tool at
L2, never at L1, never under the kill switch (autonomy off asks for everything) and never
in dry run (which comes first). The slash-only control commands (``/kill``, ``/resume``,
``/approvals``, ``/ext``) aren't tools the model can call, so a rule can't touch them.
A rule is made only by the owner's own ``yes always`` reply or ``/rules allow`` (or an
approved ``allow_trust_rule``), never by extension output.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from resonant.gate.types import Intent
from resonant.store import repos as repos_store
from resonant.store import trust as store
from resonant.tools.registry import ToolRegistry
from resonant.trust.policy import matching_trust_rule
from resonant_sdk import Effect, Level, ToolSpec

# Builtins that never take a standing allow (see the module docstring).
NEVER: frozenset[str] = frozenset({"allow_trust_rule", "allow_tools", "remove_trust_rule"})
MCP_PREFIX = "mcp__resonant__"  # how Claude names Resonant's tools (runners/ask_tools.py)

BUILTIN_SYSTEM = "resonant"  # the system a builtin's call is matched as
SHELL = "shell"  # the Claude brain's shell changes (tools/shell.py): matched per verb
# Only a git or gh verb can stand (never a compound, a script, an interpreter or a force push).
SHELL_VERB = re.compile(r"(git|gh)-[a-z][a-z-]*")


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


def can_stand(spec: ToolSpec) -> bool:
    """A tool a standing allow can cover: a change the gate asks a plain yes for (L2), never
    L1 or pay, never one of ``NEVER``, with a valid action slug."""
    if spec.effect is Effect.READ or spec.effective_level is not Level.L2:
        return False
    if spec.extension is None and spec.name in NEVER:
        return False
    return tool_action(spec.extension, spec.name) is not None


def allow_targets(
    names: Sequence[str],
    *,
    registry: ToolRegistry,
    ext_tools: Callable[[str], Sequence[ToolSpec]],
) -> tuple[list[ToolSpec], str | None]:
    """BA-1: the tools ``allow_tools`` names, as the specs a standing allow would cover, or
    why not (the first name that names nothing a rule can cover). Each name is:

    - an extension (``linear``): every chat tool of it that can stand (``can_stand``: its L2
      changes; for linear ``save_issue`` and ``save_comment``), as loaded now;
    - ``<extension>.<tool>`` (``linear.save_comment``): that one;
    - a tool's own name (``create_watch``, ``save_issue``, also as Claude spells it,
      ``mcp__resonant__save_issue``): a builtin of that name, else the one extension tool.

    Read from the registry and the loaded extensions, never from the model; the approval
    prompt and the rules saved both come from here."""
    found: list[ToolSpec] = []
    for raw in names:
        name = raw.strip().lower().removeprefix(MCP_PREFIX)
        picked: list[ToolSpec]
        if "." in name:
            ext, tool = name.split(".", 1)
            picked = [s for s in ext_tools(ext) if s.name == tool]
        elif ext_tools(name):
            picked = [s for s in ext_tools(name) if can_stand(s)]
            if not picked:
                return [], f"{name} has no changes that ask first"
        else:
            builtin = registry.get(None, name)
            if builtin is not None:
                picked = [builtin]
            else:
                picked = [
                    s for s in registry.specs() if s.extension and s.name == name
                ]  # an extension tool by its own name: only when one extension has it
                picked = [s for s in picked if s in ext_tools(s.extension or "")]
                if len(picked) > 1:
                    return [], f"{name} is a tool of several extensions; name one"
        if not picked or not all(can_stand(s) for s in picked):
            return [], f"{name} isn't a change that asks first"
        found.extend(s for s in picked if s not in found)
    return found, None


def tool_name(spec: ToolSpec) -> str:
    """``linear.save_issue`` or ``create_watch``: a tool as a rule names it."""
    return spec.name if spec.extension is None else f"{spec.extension}.{spec.name}"


class StandingAllows:
    """The gate's view of the owner's standing allows for chat tools."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        stands: Mapping[tuple[str | None, str], Callable[[Intent], bool]] | None = None,
    ) -> None:
        """``stands``: per tool, whether this one call may take a standing allow at all
        (``merge_run``: ``tools.runs.merge_standable``)."""
        self.conn = conn
        self.stands = dict(stands or {})

    def scope(self, intent: Intent) -> ToolScope | None:
        """The rule a ``yes always`` on this call would save, or None if it can't have one."""
        if intent.extension is None and intent.tool in NEVER:
            return None
        stands = self.stands.get((intent.extension, intent.tool))
        if stands is not None and not stands(intent):
            return None
        action = tool_action(intent.extension, intent.tool)
        if intent.extension is None and intent.tool == SHELL:
            verb = intent.args.get("verb")
            ok = isinstance(verb, str) and SHELL_VERB.fullmatch(verb) and verb != "git-push-force"
            action = tool_action(None, f"{SHELL}.{verb}") if ok else None
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
        if intent.extension is None and intent.tool == SHELL:
            return None  # a shell change's rule is made over chat, never by ``yes always``
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

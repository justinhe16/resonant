"""One request per action (PER-269).

An action that Resonant can be asked for by more than one path (a run's ``resonant check``,
the brain's tool call, a job) has one identity: the system, the verb and the target,
``github merge jhe/dori#2`` (``Action.key``). Every approval for such an action records it
(``approvals.action_key`` / ``action_target``, migration 022), so:

- Before a new approval for an action opens, the gate (``PolicyGate._ask``) and the trust
  checks (``TrustChecks._approval``) look for an open one with the same key. If there is one,
  no second approval opens: the brain's call is refused with a pointer (``R8 is already
  asking the owner for this…``; no approval code, as no tool result carries one) and a
  worker's check is answered ``handled`` (don't do it yourself). One request, one code to
  answer. The same task asking again for the same action with other args (``merge_pr`` at a
  newer ``head_sha``) supersedes its own stale ask instead.
- When a PR is merged or closed (by Resonant's ``merge_pr`` or ``merge_run``, by a run, by
  hand: the github watch's ``pr_merged``/``pr_closed``), the open approvals targeting it
  expire as ``obsolete`` (``approvals.obsolete_for_target``), audited and never texted.
- A run whose PR is found merged asks ``merge_record``: did Resonant merge it (a succeeded
  ``github.merge_pr`` for that repo and number), or did the owner approve the run's own merge
  (an approved check for it)? Only a merge with no such record keeps the ⚠ alert.

The identities are code-built from registered tools' args and the run's registered repo,
never from a model's or a worker's words alone (a check's ``what`` only picks the PR number).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from resonant.gate.types import Intent
from resonant.store import approvals as ap
from resonant.store import repos as repos_store
from resonant.store import runs as runs_store

GITHUB = "github"
MERGE = "merge"
RERUN = "rerun"
MERGE_VERBS = frozenset({"merge"})  # a worker check's action that is a PR merge

_REPO = re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+")
_FULL_REF = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")
_PR_REF = re.compile(r"(?:#|\bPR\s*#?|\bpull\s+request\s*#?)(\d+)\b", re.I)

# The extension tools that act on one PR (``repo``, ``number`` args), by verb.
_PR_TOOLS: dict[tuple[str, str], str] = {
    (GITHUB, "merge_pr"): MERGE,
    (GITHUB, "rerun_failed"): RERUN,
}
_MERGE_RUN = "merge_run"  # the builtin that merges a run's PR (``tools/runs.py``)


@dataclass(frozen=True)
class Action:
    """One action's identity: ``github merge jhe/dori#2``."""

    system: str
    verb: str
    target: str

    @property
    def key(self) -> str:
        return f"{self.system} {self.verb} {self.target}"


def pr_target(repo: object, number: object) -> str | None:
    """``jhe/dori#2`` for a repo slug and PR number, or None if they aren't one."""
    if not isinstance(repo, str) or isinstance(number, bool) or not isinstance(number, int):
        return None
    slug = repo.strip().lower()
    if number <= 0 or not _REPO.fullmatch(slug):
        return None
    return f"{slug}#{number}"


def _repo_slug(conn: sqlite3.Connection, name: str) -> str | None:
    repo = repos_store.find(conn, name) if name else None
    return repo.slug if repo is not None else None


def for_intent(conn: sqlite3.Connection, intent: Intent) -> Action | None:
    """The action a gated call asks for, when it is one that has an identity."""
    args: dict[str, Any] = intent.args
    if intent.extension is not None:
        verb = _PR_TOOLS.get((intent.extension, intent.tool))
        target = pr_target(args.get("repo"), args.get("number")) if verb else None
        return Action(GITHUB, verb, target) if verb and target else None
    if intent.tool == _MERGE_RUN and isinstance(code := args.get("code"), str):
        run = runs_store.get(conn, code.strip().upper())
        if run is None or run.pr_number is None:
            return None
        target = pr_target(_repo_slug(conn, run.repo), run.pr_number)
        return Action(GITHUB, MERGE, target) if target else None
    return None


def merged_by(intent: Intent) -> str | None:
    """The PR a successful run of this call merged (``github.merge_pr``), else None."""
    if _PR_TOOLS.get((intent.extension or "", intent.tool)) != MERGE:
        return None
    return pr_target(intent.args.get("repo"), intent.args.get("number"))


def for_check(
    conn: sqlite3.Connection, run: runs_store.Run, action: str, what: str
) -> Action | None:
    """The action a run's worker check asks for: a merge (``--action merge``) of a PR of the
    run's own registered repo. The PR is the one ``what`` names (``owner/name#2`` for that
    repo, ``#2``, ``PR 2``), else the run's own PR. None for any other check, when no PR can
    be named, or when ``what`` names another repo's PR (a worker's words never key an action
    on a repo that isn't its run's)."""
    if action not in MERGE_VERBS:
        return None
    slug = _repo_slug(conn, run.repo)
    if slug is None:
        return None
    number: int | None
    if (full := _FULL_REF.search(what)) is not None:
        if full.group(1).lower() != slug.lower():
            return None
        number = int(full.group(2))
    elif (m := _PR_REF.search(what)) is not None:
        number = int(m.group(1))
    else:
        number = run.pr_number
    target = pr_target(slug, number) if number is not None else None
    return Action(GITHUB, MERGE, target) if target else None


def asker(approval: ap.Approval) -> str:
    """Who holds an open request: the run (``R8``) for a worker check, else Resonant."""
    if approval.tool == ap.CHECK_TOOL and approval.requested_by:
        return approval.requested_by
    return "Resonant"


def already_asking(approval: ap.Approval) -> str:
    """``R8 is already asking the owner for this; …`` (code-built: a run code or "Resonant").
    It carries no approval code: it is a tool result, and no tool result carries one (the
    owner's prompt does)."""
    return (
        f"{asker(approval)} is already asking the owner for this, so no second prompt went "
        "out; tell the owner to answer the open prompt for it (/approvals lists it)"
    )


@dataclass(frozen=True)
class MergeRecord:
    """Resonant's record of a PR's merge: ``resonant`` merged it itself (``merge_pr``), or the
    owner approved the ``run``'s own merge (its check). ``code``: the approval answered."""

    by: Literal["resonant", "run"]
    code: str | None


def merge_record(
    conn: sqlite3.Connection, slug: str, number: int, *, run_code: str | None = None
) -> MergeRecord | None:
    """Did Resonant merge PR ``slug#number``, or approve run ``run_code``'s own check to
    merge it? None: no record of it."""
    target = pr_target(slug, number)
    if target is None:
        return None
    repo, n = target.rsplit("#", 1)
    row = conn.execute(
        """SELECT json_extract(e.detail, '$.approval_id') AS approval_id
           FROM audit_log e JOIN audit_log g
             ON g.intent_hash = e.intent_hash AND g.action = 'gate.evaluate'
                AND g.task_id IS e.task_id
           WHERE e.action = 'executor.result' AND e.extension = ? AND e.tool = 'merge_pr'
             AND json_extract(e.detail, '$.ok') = 1
             AND lower(json_extract(g.detail, '$.args.repo')) = ?
             AND json_extract(g.detail, '$.args.number') = ?
           ORDER BY e.id DESC LIMIT 1""",
        (GITHUB, repo, int(n)),
    ).fetchone()
    if row is not None:
        approval = ap.get_approval(conn, row["approval_id"]) if row["approval_id"] else None
        return MergeRecord("resonant", ap.reply_code(conn, approval) if approval else None)
    if run_code is None:
        return None
    approved = ap.approved_for_action(
        conn, Action(GITHUB, MERGE, target).key, tool=ap.CHECK_TOOL, requested_by=run_code
    )
    if approved is not None:
        return MergeRecord("run", ap.reply_code(conn, approved))
    return None

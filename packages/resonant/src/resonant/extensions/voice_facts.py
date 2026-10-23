"""The facts of the pulse's and the briefing's Workers news, for Claude's voice (PER-273).

The pulse and the briefing list the rollup's lines (``extensions/rollup.py``): a run's end, a
merge, a stall, a review request. :func:`worker_facts` turns those same items back into facts
(``brain/voice.py``) from the store, so Claude can say them in a few sentences. What stays
code-built goes in the **tail**, verbatim after the prose: a ⚠ line, a merged run's deps and
side-effect line, a run's ``along the way`` line. A caller adds its own (extension items,
worker questions, health, ``+N more``). The runs still going are names only (``going``).

``None`` (the text stays code-built) when an item can't be turned into facts: Claude never
writes a text that leaves a listed item out.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace

from resonant.brain import voice
from resonant.brain.voice import Item
from resonant.extensions import rollup
from resonant.store import pulse as store
from resonant.store import repos as repo_store
from resonant.store import runs as runs_store

_CI = re.compile(r"\((?:(no) CI|CI (\w+))[,)]")
_QUIET = re.compile(r"~\d+[hd]")
_PR_REF = re.compile(r"^(\S+#(\d+))(?:\s+(.*))?$")
_QUIET_PAREN = re.compile(r"\s*\([^()]*\)\s*$")
WAITING = ("waiting for your answer", "waiting for usage reset")


@dataclass(frozen=True)
class WorkerFacts:
    items: tuple[Item, ...]
    tail: tuple[str, ...]
    going: tuple[str, ...]


def _ci_of(title: str) -> str | None:
    m = _CI.search(title)
    if m is None:
        return None
    return "none" if m.group(1) else m.group(2)


def _run_facts(conn: sqlite3.Connection, item: store.Item) -> tuple[Item, list[str]] | None:
    """A ``core`` / ``run/<code>`` item as facts and its verbatim tail lines."""
    from resonant.runners import reports  # late: ``resonant.runners`` imports this chain

    code = item.key.removeprefix(rollup.RUN_PREFIX).upper()
    run = runs_store.get(conn, code)
    if run is None:
        return None
    repo = repo_store.find(conn, run.repo) if run.repo else None
    merged = reports.merged_list(conn, code) if run.status == "done" else []
    fact = voice.run_item(
        run,
        ci=_ci_of(item.title),
        no_ci=repo is not None and repo.mode == "push" and repo.ci is False,
        upstream=repo.slug if repo is not None and repo.mode == "fork" else None,
        merged=merged,
        merged_by=reports.merged_by_of(conn, code) if merged else None,
    )
    if fact is None:
        return None
    fact = replace(fact, urgent=item.urgency == "urgent")
    tail: list[str] = []
    if "⚠" in item.title:
        tail.append(rollup.worker_line(item))
    parts = rollup.merged_parts(item.title)
    if parts is not None and parts[2]:  # deps changed, did: … (never dropped from the report)
        tail.append(f"{code}: {parts[2]}")
    elif parts is None:
        did = reports.did_part(reports.shown_did(conn, code), reports.DID_IN_TEXT)
        if did and run.status in ("ready", "done", "failed", "no_changes", "running"):
            tail.append(f"{code} along the way: {did.removeprefix(reports.DID_PREFIX).strip()}")
    return fact, tail


def _pr_fact(item: store.Item, outcome: voice.Outcome) -> Item | None:
    m = _PR_REF.match(item.title)
    if m is None:
        return None
    ref, number, rest = m.group(1), int(m.group(2)), m.group(3)
    needs = f"review {ref}" if outcome == "review_requested" else None
    return Item(
        ref=ref,
        outcome=outcome,
        pr=number,
        title=voice.fact_text(rest, voice.TITLE_MAX),
        needs=needs,
    )


def _stall_fact(conn: sqlite3.Connection, item: store.Item, roll: rollup.Rollup) -> Item | None:
    quiet = _QUIET.search(item.title)
    code = item.key.removeprefix(rollup.STALL_PREFIX)
    if not code.startswith("run-"):  # a PR of the owner's that went quiet
        m = _PR_REF.match(item.title)
        if m is None:
            return None
        rest = _QUIET_PAREN.sub("", m.group(3) or "")
        return Item(
            ref=m.group(1),
            outcome="pr_stalled",
            pr=int(m.group(2)),
            title=voice.fact_text(rest, voice.TITLE_MAX),
            quiet=quiet.group(0) if quiet else None,
            needs=f"a look at {m.group(1)}",
        )
    run = runs_store.get(conn, code.removeprefix("run-").upper())
    if run is None:
        return None
    kind = rollup.stall_kind(item, roll)
    outcome: voice.Outcome
    needs: str | None
    if kind == "ready":
        outcome, needs = "waiting_merge", f"/merge {run.code}"
    elif kind == "queued":
        outcome, needs = "queued", None
    else:
        outcome, needs = "stalled", f"a look at {run.code}"
    return Item(
        ref=run.code,
        outcome=outcome,
        repo=run.repo or None,
        ask=voice.ask_text(run.ask),
        pr=run.pr_number if outcome == "waiting_merge" else None,
        quiet=quiet.group(0) if quiet else None,
        needs=needs,
    )


def going_names(going: Sequence[store.Item], roll: rollup.Rollup) -> tuple[str, ...]:
    """Names of the work still going, as the rollup's ``Still working on`` and ``Quiet``
    lines say them: status, never news."""
    names: list[str] = []
    for item in going:
        if item.cleared:
            continue
        if item.key.startswith(rollup.RUN_PREFIX):
            code = item.key.removeprefix(rollup.RUN_PREFIX).upper()
            if code in roll.stalled:
                continue
            wait = next((f" ({w})" for w in WAITING if f" {w} · " in item.title), "")
            names.append(f"{roll.labels.get(code, code)}{wait}")
        elif item.key.startswith(rollup.STALL_PREFIX):
            names.append(item.title)
    names += roll.working
    return tuple(voice.fact_text(n, voice.TITLE_MAX) or n for n in names)[: voice.GOING_SHOWN]


def worker_facts(
    conn: sqlite3.Connection,
    listed: Sequence[store.Item],
    going: Sequence[store.Item],
    roll: rollup.Rollup,
) -> WorkerFacts | None:
    """The facts of the worker items in ``listed``, their verbatim tail lines and the names
    of what is still ``going``; None when a worker item can't be said (no items is an empty
    result)."""
    items: list[Item] = []
    tail: list[str] = []
    for item in listed:
        if not rollup.is_rollup_item(item):
            continue
        key = item.key
        fact: Item | None
        extra: list[str] = []
        if key.startswith(rollup.RUN_PREFIX):
            made = _run_facts(conn, item)
            fact, extra = (None, []) if made is None else made
        elif key.startswith(rollup.MERGED_PREFIX):
            fact = _pr_fact(item, "pr_merged")
        elif key.startswith(rollup.REVIEW_PREFIX):
            fact = _pr_fact(item, "review_requested")
        else:
            fact = _stall_fact(conn, item, roll)
        if fact is None:
            return None
        items.append(fact)
        tail += extra
    return WorkerFacts(tuple(items), tuple(dict.fromkeys(tail)), going_names(going, roll))


__all__ = ["WorkerFacts", "going_names", "worker_facts"]

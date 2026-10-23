"""The progress rollup (GH-2): the ``Workers:`` section of the hourly pulse and the briefing,
said like an employee's update rather than one line per event::

    Workers:
    Merged: R4 add dark mode (#12), resonant#40 fix login
    R7 done: PR #16 (CI green) · https://github.com/…/pull/16
    Looks like R3 add retries (~5h quiet) got dropped. You should take a look.
    Waiting on you to merge: R5 fix login (#16, ~6h)
    Review requested: dori#12 bump deps
    Still working on: R8 api refactor, R9 docs pass (waiting for your answer), dori#41 cache
    Quiet: R2 cache layer (~9h)

It combines Resonant's own runs (the ``core`` / ``run/<code>`` items of
``runners/reports.py``) with, when the github extension is installed and enabled, the
owner's PRs from its ``watch`` job (``pr_active``, ``pr_merged``, ``pr_closed`` and
``review_requested`` events, which never text on their own).

**News** (a reason to send, told once through the pulse items' told state, so restarts and
the pulse's exactly-once delivery cover it):

- a run's end (``R7 done: …``, ``R3 failed: …``), its merge, or a new ``did:`` side effect on
  a running run (``reports.WorkerReports.running``: nothing else about a running run is news);
- ``merged/<pr>``: a PR of the owner's merged (not a run's own PR, whose ``run/<code>`` item
  says it), in the last ``NEWS_WINDOW``;
- ``review/<pr>``: a review requested of the owner, in the last ``NEWS_WINDOW``, once per PR;
- ``stall/<run or pr>``: a run or open PR with no activity for ``pulse.stall_after`` (4h).
  Its state is when the quiet began, so it is said once, and again only after new activity
  and a new quiet stretch. It is cleared silently when the activity resumes or the run ends.
  How it reads depends on what the quiet means (PER-266): a ``ready`` run's PR waits on the
  owner (``Waiting on you to merge: …``), a queued run whose task waits on ``resource``
  (every worker slot busy, PER-259) is ``Queued behind other runs: …``, and anything else
  ``Looks like … got dropped``. The key and state are the same for all three, so the
  wording never makes an old stall news again.

**Context** (listed when there is news, never a reason to send): ``Still working on``, the
runs going (their items, which are told as they are written) and the open PRs with activity
inside the stall window. A stalled one is left out of it; once told it stays visible
(:func:`quiet_items`) on the last line, ``Quiet: R3 add retries (~6h)`` (or on the waiting
or queued line, by its kind), until it moves.

An untold ``merged/`` item written more than ``OLD_MERGE`` ago (held through quiet hours, a
pause or a long silence) is dropped at the next refresh, cleared and told without a word: a
merge from days ago isn't news.

Activity: a running run's transcript (``<state>/transcripts/R<n>.jsonl``: every stream line,
the worker's notes and commands included) or its start; a queued run's creation; a ``ready``
run's end (its PR waits for the owner's merge). A run waiting (for the owner's answer, or a
usage reset) is never a stall: the first is on the owner, the second on a clock. A PR's
activity is the extension's ``active_at``: its ``updatedAt`` (commits, comments, reviews) or
its latest check, whichever is later. A stall that began before the github extension was
installed, or before the rollup first ran (kv ``rollup.activated_at``: an upgrade of an
existing install), is written told, and a merge or review request stored before that is not
news: state that existed before is never announced, like the notifier's baseline. PR data is
only what the watch last stored, so PR stalls and ``Still working on`` PRs need its last run
to have succeeded in the last ``WATCH_FRESH``; a stale watch leaves PR stalls as they are.
A merged or closed PR stays so unless a later ``pr_active`` moved after it (a reopen). A
draft PR is never a stall (it is often parked on purpose); it still counts as work.

Rendering is pure (:func:`entries`): at most ``LINES_MAX`` lines under the header, each at
most ``LINE_MAX``; a combined line names what fits and ends ``+N more`` (those items stay
pending and ride the next pulse), and the pulse's own ``+N more`` counts news left out.
Every name is code-built (``R4``, ``dori#12``) around text cleaned with
``notify.clean_text`` (the run's ask, a PR title), so none can pose as a prompt or reply.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from resonant.extensions.notify import clean_text
from resonant.extensions.pulse import render_item
from resonant.store import extensions as ext_store
from resonant.store import pulse as store
from resonant.store import runs as runs_store
from resonant.store.db import atomic, iso, kv_get, kv_set
from resonant.store.runs import Run

log = logging.getLogger(__name__)

CORE = "core"
RUN_PREFIX = "run/"
STALL_PREFIX = "stall/"
MERGED_PREFIX = "merged/"
REVIEW_PREFIX = "review/"
PREFIXES = (RUN_PREFIX, STALL_PREFIX, MERGED_PREFIX, REVIEW_PREFIX)
GITHUB = "github"
PR_EVENTS = ("pr_active", "pr_merged", "pr_closed", "review_requested")
NEWS_WINDOW = timedelta(hours=48)  # a merge or review request older than this isn't news
PR_WINDOW = timedelta(days=7)  # the github events read; a PR quiet longer drops out
OLD_MERGE = timedelta(days=3)  # an untold merged/ item older than this is dropped, not said
READY_WINDOW = timedelta(days=7)  # a ready run's PR older than this is no longer watched
STALL_AFTER = timedelta(hours=4)  # the default of ``pulse.stall_after``
WATCH_FRESH = timedelta(minutes=30)  # three missed 10-minute watches: PR data is stale
ACTIVATED_KEY = "rollup.activated_at"  # kv: when the rollup first ran (an upgrade's floor)
ITEM_TTL_S = 7 * 24 * 3600
LINES_MAX = 6
LINE_MAX = 200
NAME_MAX = 40  # an ask or PR title in a name: ``R4 add dark mode to the settings…``
TRANSCRIPTS = "transcripts"
_MERGED = re.compile(r"^(R\d+) merged: PR #(\d+) · \S+(?: · (.+))?$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_BAD_KEY_CHARS = re.compile(r"[^a-z0-9/._:-]")
_WAITING = ("waiting for your answer", "waiting for usage reset")
_QUIET_SUFFIX = " quiet)"
# Only placeholders are interpolated; every value is bound.
_PR_SQL = (
    "SELECT type, payload, occurred_at FROM events WHERE source = ? AND type IN"  # noqa: S608
    f" ({','.join('?' * len(PR_EVENTS))}) AND occurred_at >= ? ORDER BY occurred_at, rowid"
)


@dataclass(frozen=True)
class Rollup:
    """What the pure rendering needs beyond the items: ``labels`` (run code ->
    ``R4 add dark mode``), ``working`` (open PRs with recent activity, named),
    ``stalled`` (run codes with an open stall item, left out of ``Still working on``), and
    the stalled runs whose quiet is the owner's merge (``ready``) or the slot limit
    (``queued``)."""

    labels: Mapping[str, str] = field(default_factory=dict[str, str])
    working: tuple[str, ...] = ()
    stalled: frozenset[str] = frozenset()
    ready: frozenset[str] = frozenset()
    queued: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Pr:
    """One of the owner's PRs as the github watch last described it."""

    repo: str
    number: int
    title: str
    url: str
    state: str  # open | merged | closed
    active_at: datetime | None = None
    merged_at: datetime | None = None
    baseline: bool = False  # what was said about it came from the install's first watch
    ended_at: datetime | None = None  # when it was merged or closed
    seen_at: datetime | None = None  # when the event that left it so was stored
    draft: bool = False

    @property
    def ref(self) -> str:
        return f"{self.repo.rsplit('/', 1)[-1]}#{self.number}"

    @property
    def name(self) -> str:
        return f"{self.ref} {short(self.title)}".strip()


def is_rollup_item(item: store.Item) -> bool:
    """A line of the rollup: a run's item or one this module derives."""
    return item.source == CORE and item.key.startswith(PREFIXES)


# --- names -----------------------------------------------------------------------------------


def short(text: str | None, limit: int = NAME_MAX) -> str:
    """``text`` cleaned and clipped at a word, with ``…`` when cut."""
    flat = clean_text(text or "")
    if len(flat) <= limit:
        return flat
    cut = flat[: limit - 1]
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def label(run: Run) -> str:
    """``R4 add dark mode``: the run's code and the start of its ask."""
    return f"{run.code} {short(run.ask)}".strip()


def quiet_text(quiet: timedelta) -> str:
    """``~5h``, or ``~3d`` from two days on."""
    hours = max(int(quiet.total_seconds() // 3600), 1)
    return f"~{hours // 24}d" if hours >= 48 else f"~{hours}h"


def _code(item: store.Item) -> str:
    return item.key.removeprefix(RUN_PREFIX).upper()


def _stall_code(item: store.Item) -> str | None:
    """``R3`` for a run's stall item (``stall/run-r3``), None for a PR's."""
    rest = item.key.removeprefix(STALL_PREFIX)
    return rest.removeprefix("run-").upper() if rest.startswith("run-") else None


def _key(prefix: str, name: str) -> str:
    return prefix + _BAD_KEY_CHARS.sub("-", name.lower())[: store.KEY_MAX - len(prefix)]


def stall_key(kind: str, name: str) -> str:
    """``stall/run-r3`` or ``stall/pr:own/dori:12``."""
    return _key(STALL_PREFIX, f"{kind}{name}")


def pr_key(prefix: str, pr: Pr) -> str:
    return _key(prefix, f"pr:{pr.repo}:{pr.number}")


# --- rendering: pure -------------------------------------------------------------------------


def joined(
    head: str, names: Sequence[tuple[str, store.Item | None]], tail: str = ""
) -> tuple[str, tuple[store.Item, ...]] | None:
    """``<head><name>, <name> +N more<tail>`` in at most ``LINE_MAX`` characters, with the
    items of the names shown (a name that doesn't fit stays pending). None with no names."""
    if not names:
        return None
    shown: list[str] = []
    items: list[store.Item] = []
    for i, (name, item) in enumerate(names):
        left = len(names) - i - 1
        more = f" +{left} more" if left else ""
        if shown and len(head + ", ".join([*shown, name]) + more + tail) > LINE_MAX:
            break
        shown.append(name)
        if item is not None:
            items.append(item)
    left = len(names) - len(shown)
    line = head + ", ".join(shown) + (f" +{left} more" if left else "") + tail
    if len(line) > LINE_MAX:  # one name too long on its own: clip it
        room = LINE_MAX - len(tail)
        line = line[: room - 1].rstrip() + "…" + tail
    return line, tuple(items)


Entry = tuple[str, tuple[store.Item, ...]]


def worker_line(item: store.Item) -> str:
    """One run's own line (its title is code-built around cleaned parts): ``🔴`` for a
    failure nobody was texted."""
    return render_item(item, "\U0001f534" if item.urgency == "urgent" else "", LINE_MAX)


def _finished_first(items: Sequence[store.Item]) -> list[store.Item]:
    """Ended runs first (urgent first, oldest first: they are news), then running ones."""
    rank = {"urgent": 0, "normal": 1, "low": 2}
    return sorted(items, key=lambda i: (not i.cleared, rank[i.urgency], i.updated_at, i.key))


def entries(
    news: Sequence[store.Item], context: Sequence[store.Item] = (), rollup: Rollup | None = None
) -> list[Entry]:
    """The rollup's lines (without a header), each with the news items it says; a line
    with no items is status. Order: a failure nobody was texted, merges, other run news,
    stalls, review requests, then what is still going. At most ``LINES_MAX``."""
    r = rollup or Rollup()
    mine = [i for i in news if is_rollup_item(i)]
    keys = {(i.source, i.key) for i in mine}
    out: list[Entry] = []
    runs = [i for i in mine if i.key.startswith(RUN_PREFIX)]
    merged: list[tuple[str, store.Item | None]] = []
    details: list[Entry] = []
    others: list[store.Item] = []
    for item in _finished_first(runs):
        m = _MERGED.match(item.title) if item.urgency != "urgent" else None
        if m is None:
            others.append(item)
            continue
        code, number, extra = m.group(1), m.group(2), m.group(3)
        merged.append((f"{r.labels.get(code, code)} (#{number})", item))
        if extra:  # deps changed, did: … (GT-1, TR-1): never dropped from the report
            details.append((f"{code}: {extra}", (item,)))
    merged += [(i.title, i) for i in mine if i.key.startswith(MERGED_PREFIX)]
    out += [(worker_line(i), (i,)) for i in others if i.urgency == "urgent"]
    if line := joined("Merged: ", merged):
        out.append(line)
        named = set(line[1])
        out += [d for d in details if d[1][0] in named]
    out += [(worker_line(i), (i,)) for i in others if i.urgency != "urgent"]
    stalls: dict[str, list[tuple[str, store.Item | None]]] = {"dropped": [], "ready": [],
                                                               "queued": []}  # fmt: skip
    for i in mine:
        if i.key.startswith(STALL_PREFIX):
            stalls[stall_kind(i, r)].append((i.title, i))
    told = sorted(context, key=lambda i: (i.updated_at, i.key))
    quiet: list[tuple[str, store.Item | None]] = []
    for i in told:
        if not i.key.startswith(STALL_PREFIX) or i.cleared or (i.source, i.key) in keys:
            continue
        kind = stall_kind(i, r)
        name = (
            i.title.removesuffix(_QUIET_SUFFIX) + ")"
            if i.title.endswith(_QUIET_SUFFIX)
            else i.title
        )
        (quiet if kind == "dropped" else stalls[kind]).append((name, None))
    if line := joined("Looks like ", stalls["dropped"], " got dropped. You should take a look."):
        out.append(line)
    status: list[Entry] = []  # lines with no news: after the news, dropped first
    for kind, head in (
        ("ready", "Waiting on you to merge: "),
        ("queued", "Queued behind other runs: "),
    ):
        if line := joined(head, _news_first(stalls[kind])):
            (out if line[1] else status).append(line)
    reviews = [(i.title, i) for i in mine if i.key.startswith(REVIEW_PREFIX)]
    if line := joined("Review requested: ", reviews):
        out.append(line)
    going: list[tuple[str, store.Item | None]] = []
    for item in told:
        if not item.key.startswith(RUN_PREFIX) or item.cleared or (item.source, item.key) in keys:
            continue
        code = _code(item)
        if code in r.stalled:
            continue
        wait = next((f" ({w})" for w in _WAITING if f" {w} · " in item.title), "")
        going.append((f"{r.labels.get(code, code)}{wait}", None))
    going += [(name, None) for name in r.working]
    if line := joined("Still working on: ", going):
        status.insert(0, line)
    if line := joined("Quiet: ", quiet):
        status.append(line)
    return [*out, *status][:LINES_MAX]


def stall_kind(item: store.Item, r: Rollup) -> str:
    """What a stall's quiet means: ``ready`` (a PR waiting on the owner's merge), ``queued``
    (behind the slot limit) or ``dropped``."""
    code = _stall_code(item)
    if code in r.ready:
        return "ready"
    return "queued" if code in r.queued else "dropped"


def merged_parts(title: str) -> tuple[str, int, str | None] | None:
    """``(code, PR number, extra)`` of a merged run's item title (``R4 merged: PR #12 · <url>
    · deps changed: …``), or None. ``extra``: the deps and side-effect part."""
    m = _MERGED.match(title)
    if m is None:
        return None
    return m.group(1), int(m.group(2)), m.group(3)


def _news_first(
    names: Sequence[tuple[str, store.Item | None]],
) -> list[tuple[str, store.Item | None]]:
    return sorted(names, key=lambda n: n[1] is None)


# --- reads -----------------------------------------------------------------------------------


def transcripts_dir(mirror_dir: Path) -> Path:
    """Where the runner keeps transcripts (``ClaudeRunner.transcript_dir``)."""
    return mirror_dir.expanduser().parent / TRANSCRIPTS


def last_activity(run: Run, transcripts: Path | None, now: datetime) -> datetime | None:
    """When ``run`` last did something, or None when it can't stall (see the module
    docstring)."""
    match run.status:
        case "running":
            at = run.started_at or run.created_at
            if transcripts is not None:
                try:  # like runners.activity.quiet_for (imported here, it would be a cycle)
                    mtime = (transcripts / f"{run.code}.jsonl").lstat().st_mtime
                except OSError:
                    return at
                at = max(at, datetime.fromtimestamp(mtime, tz=now.tzinfo))
            return at
        case "queued":
            return run.created_at
        case "ready":
            return run.ended_at
        case _:
            return None


def behind_slots(conn: sqlite3.Connection, run: Run) -> bool:
    """A queued run whose task is deferred on ``resource`` (PER-259: every worker slot, a
    repo lock or a turn was busy): it waits behind the slot limit, not dropped."""
    if run.status != "queued" or run.task_id is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE id = ? AND status = 'waiting' AND wait_reason = 'resource'",
        (run.task_id,),
    ).fetchone()
    return row is not None


def stall_title(conn: sqlite3.Connection, run: Run, quiet: timedelta) -> str:
    """``R3 add retries (~5h quiet)``; ``R5 fix login (#16, ~6h)`` for a ``ready`` run (its
    line says it waits on the owner's merge), ``R9 docs pass (~5h)`` for one queued behind
    the slot limit."""
    if run.status == "ready":
        pr = f"#{run.pr_number}, " if run.pr_number else ""
        return f"{label(run)} ({pr}{quiet_text(quiet)})"
    if behind_slots(conn, run):
        return f"{label(run)} ({quiet_text(quiet)})"
    return f"{label(run)} ({quiet_text(quiet)} quiet)"


def quiet_items(conn: sqlite3.Connection, now: datetime) -> list[store.Item]:
    """Open stalls the owner already heard of: context for the ``Quiet:`` line (and the
    waiting and queued lines), never news. PR stalls are left out while the watch is
    stale (:func:`held_while_stale`)."""
    told = [
        i
        for i in store.open_items(conn, now)
        if i.source == CORE and i.key.startswith(STALL_PREFIX) and not i.cleared and not i.changed
    ]
    return held_while_stale(conn, now, told)


def github_installed(conn: sqlite3.Connection) -> datetime | None:
    """The github extension's install time while it is installed and enabled, else None."""
    record = ext_store.get_extension(conn, GITHUB)
    return record.installed_at if record is not None and record.status == "enabled" else None


def _when(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        at = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.utcoffset() is not None else None


def github_prs(
    conn: sqlite3.Connection, now: datetime
) -> tuple[list[Pr], list[tuple[Pr, datetime]]]:
    """``(prs, review requests)`` from the github watch's events of the last ``PR_WINDOW``,
    oldest first: each PR as its latest event left it, and each review request (the PR,
    when it was seen; ``Pr.baseline`` when it came from the install's first watch)."""
    rows = conn.execute(
        _PR_SQL,
        (f"ext:{GITHUB}", *(f"ext.{GITHUB}.{t}" for t in PR_EVENTS), iso(now - PR_WINDOW)),
    ).fetchall()
    prs: dict[tuple[str, int], Pr] = {}
    reviews: dict[tuple[str, int], tuple[Pr, datetime]] = {}
    for etype, raw, occurred in rows:
        try:
            payload = cast(dict[str, Any], json.loads(raw))
        except ValueError:
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        d = cast(dict[str, Any], data)
        repo, number = d.get("repo"), d.get("number")
        if not (isinstance(repo, str) and _REPO.match(repo) and isinstance(number, int)):
            continue
        if number <= 0:
            continue
        key = (repo, number)
        before = prs.get(key)
        title, url = d.get("title"), d.get("url")
        base = Pr(
            repo,
            number,
            title if isinstance(title, str) else before.title if before else "",
            url if isinstance(url, str) else before.url if before else "",
            "open",
            baseline=payload.get("baseline") is True,
        )
        base = replace(base, seen_at=_when(occurred))
        kind = str(etype).removeprefix(f"ext.{GITHUB}.")
        if kind == "pr_active":
            active = _when(d.get("active_at"))
            if before is not None and before.state != "open" and not _reopened(before, active):
                continue  # merged or closed is final unless it moved after (a reopen)
            prs[key] = replace(base, active_at=active, draft=d.get("draft") is True)
        elif kind == "pr_merged":
            merged_at = _when(d.get("merged_at")) or _when(occurred)
            prs[key] = replace(base, state="merged", merged_at=merged_at, ended_at=merged_at)
        elif kind == "pr_closed":
            closed_at = _when(d.get("closed_at")) or _when(occurred)
            prs[key] = replace(base, state="closed", ended_at=closed_at)
        elif (at := _when(occurred)) is not None:
            reviews[key] = (base, at)
    return list(prs.values()), list(reviews.values())


def _reopened(ended: Pr, active: datetime | None) -> bool:
    """A ``pr_active`` after a merge or close counts only when it moved after that (a reopen):
    one watch can see a PR open and then merged, and must not undo the merge."""
    return active is not None and ended.ended_at is not None and active > ended.ended_at


def watch_fresh(conn: sqlite3.Connection, now: datetime) -> bool:
    """The github watch's last run succeeded within ``WATCH_FRESH``. PR activity is only
    what the watch last stored, so a failing or paused watch would make every PR look
    quiet: without a fresh one, no PR is called stalled or still being worked on."""
    job = ext_store.get_job(conn, GITHUB, "watch")
    return (
        job is not None
        and job.last_outcome == "ok"
        and job.last_run_at is not None
        and now - job.last_run_at <= WATCH_FRESH
    )


def activated(conn: sqlite3.Connection, now: datetime) -> datetime:
    """When this rollup first ran (kv ``rollup.activated_at``, set then): a stall that began
    before it, or a merge or review request stored before it, is old news on an upgrade."""
    raw: object = kv_get(conn, ACTIVATED_KEY)
    at = _when(raw)
    if at is None:
        kv_set(conn, ACTIVATED_KEY, iso(now))
        at = _when(iso(now))  # as stored (milliseconds), so every refresh sees the same
        assert at is not None
    return at


def held_while_stale(
    conn: sqlite3.Connection, now: datetime, items: Sequence[store.Item]
) -> list[store.Item]:
    """``items`` without the PR stalls while the github watch is stale: one found while it
    was fresh but not yet told (quiet hours, a mute) waits for fresh data rather than be
    said from old (it stays pending; the next fresh refresh keeps or clears it)."""
    if github_installed(conn) is None or watch_fresh(conn, now):
        return list(items)
    return [i for i in items if not (i.source == CORE and i.key.startswith(STALL_PREFIX + "pr:"))]


def _run_pr_urls(runs: Iterable[Run]) -> set[str]:
    return {r.pr_url for r in runs if r.pr_url}


def gather(
    conn: sqlite3.Connection,
    now: datetime,
    items: Iterable[store.Item],
    *,
    stall_after: timedelta = STALL_AFTER,
) -> Rollup:
    """The :class:`Rollup` for ``items`` (labels of the runs they name, the open PRs still
    being worked on, the runs flagged stalled). Read-only."""
    labels: dict[str, str] = {}
    for item in items:
        if item.source == CORE and item.key.startswith(RUN_PREFIX):
            run = runs_store.get(conn, _code(item))
            if run is not None:
                labels[run.code] = label(run)
    stalled = frozenset(
        i.key.removeprefix(STALL_PREFIX + "run-").upper()
        for i in store.open_items(conn, now)
        if i.source == CORE and i.key.startswith(STALL_PREFIX + "run-")
    )
    ready: set[str] = set()
    queued: set[str] = set()
    for code in {c for i in items if i.source == CORE and (c := _stall_code(i))}:
        run = runs_store.get(conn, code)
        if run is None:
            continue
        if run.status == "ready":
            ready.add(code)
        elif behind_slots(conn, run):
            queued.add(code)
    working: list[str] = []
    if github_installed(conn) is not None and watch_fresh(conn, now):
        prs, _reviews = github_prs(conn, now)
        ours = _run_pr_urls(runs_store.recent(conn, 200))
        working = [
            p.name
            for p in prs
            if p.state == "open"
            and p.url not in ours
            and p.active_at is not None
            and now - p.active_at < stall_after
        ]
    return Rollup(
        labels=labels,
        working=tuple(working),
        stalled=stalled,
        ready=frozenset(ready),
        queued=frozenset(queued),
    )


# --- writes ----------------------------------------------------------------------------------


def refresh(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    stall_after: timedelta = STALL_AFTER,
    transcripts: Path | None = None,
) -> None:
    """Bring the derived items up to date: ``stall/…`` for every run and open PR quiet for
    ``stall_after`` (and cleared silently for the rest), ``merged/…`` and ``review/…`` for
    the github watch's news. Called by the pulse and the briefing before they compose."""
    live: dict[str, tuple[str, datetime, bool]] = {}  # key -> (title, quiet since, silent)
    with atomic(conn):
        since = activated(conn, now)
        recent = runs_store.recent(conn, 200)
        watched = [*runs_store.active(conn),
                   *runs_store.ended_since(conn, "ready", now - READY_WINDOW)]  # fmt: skip
        for run in watched:
            at = last_activity(run, transcripts, now)
            if at is not None and now - at >= stall_after:
                title = stall_title(conn, run, now - at)
                live[stall_key("run-", run.code)] = (title, at, at + stall_after <= since)
        installed = github_installed(conn)
        fresh = installed is not None and watch_fresh(conn, now)
        if installed is not None:
            floor = max(installed, since)
            prs, reviews = github_prs(conn, now)
            ours = _run_pr_urls(recent)
            for pr in prs:
                if pr.url in ours:
                    continue  # the run's own item says it
                if pr.state == "open" and pr.active_at is not None and fresh and not pr.draft:
                    quiet = now - pr.active_at
                    if quiet >= stall_after:
                        title = f"{pr.name} ({quiet_text(quiet)} quiet)"
                        silent = pr.active_at + stall_after <= floor
                        key = stall_key("pr:", f"{pr.repo}:{pr.number}")
                        live[key] = (title, pr.active_at, silent)
                elif _merged_news(pr, now, since):
                    _once(conn, pr_key(MERGED_PREFIX, pr), pr.name, now)
            for pr, at in reviews:
                if not pr.baseline and now - at < NEWS_WINDOW and at > since:
                    _once(conn, pr_key(REVIEW_PREFIX, pr), pr.name, now)
        # a stale watch says nothing new about PRs: their stalls are left as they are
        keep = () if fresh or installed is None else (STALL_PREFIX + "pr:",)
        _sync_stalls(conn, live, now, keep=keep)
        _drop_old_merges(conn, now)


def _drop_old_merges(conn: sqlite3.Connection, now: datetime) -> None:
    """An untold ``merged/`` item written more than ``OLD_MERGE`` ago is told without a word
    (a long hold, through quiet hours or a pause, would otherwise say a stale merge)."""
    old = [
        i
        for i in store.pending(conn, ("normal",), now)
        if i.source == CORE and i.key.startswith(MERGED_PREFIX) and now - i.updated_at > OLD_MERGE
    ]
    if old:
        store.mark_told(conn, old, now)


def _merged_news(pr: Pr, now: datetime, since: datetime) -> bool:
    """A merge to say: seen after the install's first watch and the rollup's activation,
    within ``NEWS_WINDOW``."""
    return (
        pr.state == "merged"
        and not pr.baseline
        and pr.merged_at is not None
        and now - pr.merged_at < NEWS_WINDOW
        and pr.seen_at is not None
        and pr.seen_at > since
        and pr.merged_at > since - WATCH_FRESH  # the first new watch reports two days back
    )


def _once(conn: sqlite3.Connection, key: str, title: str, now: datetime) -> None:
    """A one-time piece of news (a merge, a review request): written once, untold, and
    never again while its row lasts (told rows are pruned a week after)."""
    if store.get(conn, CORE, key) is None:
        store.upsert(conn, CORE, key, title, "normal", {"news": True}, True, ITEM_TTL_S, now)


def _sync_stalls(
    conn: sqlite3.Connection,
    live: Mapping[str, tuple[str, datetime, bool]],
    now: datetime,
    *,
    keep: tuple[str, ...] = (),
) -> None:
    """Make the open ``stall/`` items match ``live``. The state is when the quiet began, so a
    stall is news once; one that ends is cleared silently (cleared and told). Open items
    under a ``keep`` prefix are left alone (what we know about them is stale)."""
    for key, (title, since, silent) in live.items():
        state = {"since": iso(since)}
        _change, item = store.upsert(conn, CORE, key, title, "normal", state, False,
                                     ITEM_TTL_S, now)  # fmt: skip
        if silent:
            store.mark_told(conn, [item], now)
    for item in store.open_items(conn, now):
        if item.source != CORE or not item.key.startswith(STALL_PREFIX) or item.key in live:
            continue
        if keep and item.key.startswith(keep):
            continue
        _change, cleared = store.upsert(conn, CORE, item.key, item.title, "normal",
                                        {"ended": True}, True, ITEM_TTL_S, now)  # fmt: skip
        store.mark_told(conn, [cleared], now)


__all__ = [
    "LINES_MAX",
    "OLD_MERGE",
    "Pr",
    "Rollup",
    "behind_slots",
    "entries",
    "gather",
    "github_prs",
    "is_rollup_item",
    "joined",
    "label",
    "last_activity",
    "quiet_items",
    "quiet_text",
    "refresh",
    "short",
    "stall_title",
    "transcripts_dir",
]

"""Worker reporting by urgency (docs/employee-plan.md D5, EM-6).

Every worker event the owner may hear about gets a level, ``now`` (texted right away) or
``digest`` (the hourly "btw" pulse and the morning briefing, as a line in their ``Workers:``
section). One pulse item per run carries what the digest says about it: ``core`` /
``run/<code>`` (``store.pulse``), so the told state survives restarts and nothing is said
twice.

Events and their defaults (``trust.notify_level``, EM-4):

- ``pr`` (a run ended with its PR open), ``done`` (a run with no repo, or no changes) and
  ``merged``: the digest. A merged line names the dependency manifests and lockfiles the PR
  changed (``deps changed: package.json, uv.lock``, ``deps_line``): they aren't flagged, so
  this is where they show.
- ``failed``: now. A ``notify_digest`` rule lowers it only when the rule names ``failed``
  itself ("stop texting right away on failed"): a ``*`` rule ("only digest trading") never
  silences a failure.
- ``note``: the digest, and many notes of one run collapse into its latest (the run's item
  shows ``last_note``). ``urgent`` (``resonant note --urgent``): now, at most
  ``NOTE_TEXTS_PER_HOUR`` texts per run an hour (the rest only update the digest line).
- ``restart`` and ``update`` (GD-1: a worker's ``resonant restart``/``update`` that went
  through): now, with ``R4 restarted Resonant (abc1234)``; a ``notify_digest`` rule (on
  ``restart``/``update``, scope ``resonant`` or the run's repo) lowers them. Matched with
  system ``resonant`` and the run's repo. Either way it is on the run's ``did:`` list.
- ``question`` and ``check``: not handled here. EM-5's relay texts questions and EM-4's
  approval prompt texts checks; the digest only shows the run as ``waiting for your answer``.

Rules: system and repo are the run's repo (``scratch`` for a run with no repo). ``pr`` and
``done`` are matched with the action ``done``, so "text me right away when trading runs
finish" (``notify_now`` on ``done`` in ``trading``) covers a run that ended with a PR too; a
rule on ``pr`` itself is more specific. A run started with ``report="now"`` (``/run``, or the
owner asked to hear right away) texts its end now whatever the rules say: the request is
about that one run. A cancel is always texted (the owner did it), and so is an unrevoked
GitHub token.

Side effects (``did:``). What a worker did outside the run's own branch and PR is listed on
its digest line, running and final: ``R4 done: PR #3 (CI green) · <url> · did: merged PR #2
with gh; deployed staging``. Two sources: Resonant's own record of a worker's restart or update
(code-built), and the worker's notes that start with ``did:`` (``resonant note "did: merged PR
#2 with gh"``; the prompt asks for one per side effect taken outside Resonant's tools). The
list is kv ``run.did:<code>`` (the last ``DID_KEEP``), so it survives restarts; the line shows
what fits (``DID_IN_TITLE``, ``+N more``) and a texted end reply adds a ``Did: …`` line. A
``did:`` note is still a note: it is the line's latest note and, with ``--urgent``, texted
within the per-run cap like any other; past the cap it still reaches the digest.

Ordering, for exactly once: the item is written untold first, then a ``now`` text goes out
(deduped by the channel on ``run:<code>:<status>``), then the item is marked told. A crash
between the send and the mark reruns the conclusion: the channel drops the repeat and the mark
runs. A recipient the channel refuses leaves the item untold, so the next pulse carries it:
a failure is never lost.

Text that reaches iMessage: titles are built by code (``R4``, ``PR #12``); every part that
came from a worker or GitHub (notes, summaries, failure reasons) is already redacted and
cleaned with ``clean_external`` where it is stored, and goes through ``notify.clean_text``
here, so it can't pose as an approval prompt or a reply.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

from resonant.extensions.notify import clean_text
from resonant.store import pulse as pulse_store
from resonant.store.audit import audit
from resonant.store.db import atomic, kv_get, kv_set
from resonant.store.runs import Run
from resonant.trust import notify_level as trust_notify_level
from resonant.trust.policy import URGENT_EVENTS, NotifyDecision

log = logging.getLogger(__name__)

Level = Literal["now", "digest"]
Report = Literal["now", "digest"]
EndEvent = Literal["pr", "done", "failed", "merged"]

SOURCE = "core"
TTL_S = 48 * 3600
NOTE_TEXTS_PER_HOUR = 6
TITLE_PART_MAX = 120
NOTE_IN_TITLE = 80  # the running line stays under the store's 200-character title
SCRATCH_SYSTEM = "scratch"
_REPORT_KEY = "run.report:"  # kv: a run's report preference, set at start
_DID_KEY = "run.did:"  # kv: a run's side effects, oldest first
DID_PREFIX = "did:"
DID_KEEP = 20
DID_ITEM_MAX = 80
DID_IN_TITLE = 100  # the did part of a digest line
DID_IN_TEXT = 300  # the Did: line of a texted end reply
SELF_SYSTEM = "resonant"  # the trust system of a worker's restart or update
SELF_EVENTS = frozenset({"restart", "update"})


class NotifyLevel(Protocol):
    def __call__(
        self, conn: sqlite3.Connection, system: str, action: str, event: str, *,
        repo: str | None = None,
    ) -> NotifyDecision: ...  # fmt: skip


def item_key(code: str) -> str:
    return f"run/{code.lower()}"


def is_worker_item(item: pulse_store.Item) -> bool:
    return item.source == SOURCE and item.key.startswith("run/")


def end_event(run: Run) -> EndEvent | None:
    """The event of an ended run; None for one that isn't reported by policy (cancelled)."""
    match run.status:
        case "ready":
            return "pr"
        case "done" | "no_changes":
            return "done"
        case "failed":
            return "failed"
        case "merged":
            return "merged"
        case _:
            return None


def did_of(note: str) -> str | None:
    """The side effect a ``did:`` note names (``did: merged PR #3 with gh``, any case), or
    None for an ordinary note."""
    text = note.strip()
    if text[: len(DID_PREFIX)].lower() != DID_PREFIX:
        return None
    return text[len(DID_PREFIX) :].strip() or None


def record_did(conn: sqlite3.Connection, code: str, what: str) -> None:
    """Add a side effect to run ``code``'s list (already redacted and cleaned)."""
    with atomic(conn):
        kv_set(conn, _DID_KEY + code, [*did_list(conn, code), what][-DID_KEEP:])


def did_list(conn: sqlite3.Connection, code: str) -> list[str]:
    value: object = kv_get(conn, _DID_KEY + code)
    if not isinstance(value, list):
        return []
    return [v for v in cast(list[object], value) if isinstance(v, str)]


def did_part(items: list[str], limit: int = DID_IN_TITLE) -> str:
    """``did: merged PR #3 with gh; deployed staging +2 more`` in at most ``limit``
    characters (cleaned parts, oldest first), or "" with none."""
    parts = [p for p in (_part(i, DID_ITEM_MAX) for i in items) if p]
    shown: list[str] = []
    for i, p in enumerate(parts):
        rest = len(parts) - i - 1
        more = f" +{rest} more" if rest else ""
        if len(f"{DID_PREFIX} " + "; ".join([*shown, p]) + more) > limit:
            break
        shown.append(p)
    if parts and not shown:  # the first alone doesn't fit: clip it
        more = f" +{len(parts) - 1} more" if len(parts) > 1 else ""
        shown = [_part(parts[0], max(limit - len(DID_PREFIX) - 1 - len(more), 10))]
    if not shown:
        return ""
    more = len(parts) - len(shown)
    return f"{DID_PREFIX} " + "; ".join(shown) + (f" +{more} more" if more else "")


def _with_did(title: str, items: list[str]) -> str:
    """``title · did: …`` within the store's title limit (the did part shortened to fit)."""
    room = min(DID_IN_TITLE, pulse_store.TITLE_MAX - len(title) - 3)
    part = did_part(items, room) if items and room >= 20 else ""
    return f"{title} · {part}" if part else title


def _part(text: str | None, limit: int = TITLE_PART_MAX) -> str:
    """A worker- or GitHub-supplied part of a title, cleaned once and clipped."""
    flat = clean_text(text or "")
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


DEPS_SHOWN = 4  # dependency files named in a merged line; the rest are counted


def deps_line(paths: Sequence[str]) -> str:
    """``deps changed: package.json, uv.lock`` for the dependency files a merged PR changed
    (``CheckResult.deps``), or an empty string. Code-built: each path cleaned and clipped,
    at most ``DEPS_SHOWN`` named and the rest counted."""
    if not paths:
        return ""
    shown = ", ".join(_part(p, 60) for p in paths[:DEPS_SHOWN])
    more = len(paths) - DEPS_SHOWN
    return f"deps changed: {shown}" + (f" (+{more} more)" if more > 0 else "")


def end_title(run: Run, ci: str | None = None, *, deps: Sequence[str] = ()) -> str:
    """The digest line for an ended run (code-built around cleaned parts). ``deps``: a
    merged run's dependency files, listed after its PR."""
    match run.status:
        case "ready":
            label = "no CI" if ci == "none" else f"CI {ci or 'unknown'}"
            title = f"{run.code} done: PR #{run.pr_number} ({label}) · {_part(run.pr_url)}"
            if run.flag_line or run.failure:
                title += f" · ⚠ /runs {run.code}"
            return title
        case "done" if run.repo and run.branch and not run.pr_url and run.head_sha is None:
            # RX-1: a run that couldn't push (a fork run's done always has a checked head)
            return f"{run.code} done: local branch {_part(run.branch)} (not pushed)"
        case "done":
            summary = _part(run.summary)
            return f"{run.code} done" + (f": {summary}" if summary else "")
        case "no_changes":
            return f"{run.code}: no changes · {_part(run.repo) or 'no repo'}"
        case "failed":
            return f"{run.code} failed: {_part(run.failure) or 'unknown error'}"
        case "merged":
            title = f"{run.code} merged: PR #{run.pr_number} · {_part(run.pr_url)}"
            return f"{title} · {deps_line(deps)}" if deps else title
        case _:
            return f"{run.code} finished · {_part(run.repo) or 'no repo'}"


class WorkerReports:
    """The reporting side of :class:`~resonant.runners.claude.ClaudeRunner` (see the module
    docstring). ``notify_level`` is ``trust.notify_level`` (a seam for tests)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime],
        tz: ZoneInfo,
        notify_level: NotifyLevel = trust_notify_level,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.tz = tz
        self._notify_level = notify_level
        self._note_texts: dict[str, deque[datetime]] = {}

    # -- preference and policy --

    def set_report(self, code: str, report: Report) -> None:
        if report == "now":
            with atomic(self.conn):
                kv_set(self.conn, _REPORT_KEY + code, "now")

    def report_pref(self, code: str) -> Report:
        return "now" if kv_get(self.conn, _REPORT_KEY + code) == "now" else "digest"

    def level(self, run: Run, event: str) -> Level:
        """How soon the owner hears ``event`` of ``run`` (see the module docstring). A
        ``restart``/``update`` is matched with system ``resonant`` (what it acted on)."""
        if event in ("pr", "done", "merged") and self.report_pref(run.code) == "now":
            return "now"
        action = "done" if event in ("pr", "done") else "note" if event == "urgent" else event
        system = SELF_SYSTEM if event in SELF_EVENTS else run.repo or SCRATCH_SYSTEM
        default: Level = "now" if event in URGENT_EVENTS else "digest"
        try:
            decision = self._decide(system, action, event, run.repo or None)
        except Exception:  # no rule can be read: the defaults (a failure still texts)
            log.exception("notify level unavailable", extra={"run": run.code})
            return default
        level: Level = decision.level
        if event == "failed" and level == "digest":
            rule = decision.rule
            if rule is None or rule.action != "failed":
                return "now"  # only a rule on failed itself may hold a failure for the digest
        return level

    def _decide(self, system: str, action: str, event: str, repo: str | None) -> NotifyDecision:
        return self._notify_level(self.conn, system, action, event, repo=repo)

    # -- the run's item --

    def running(self, run: Run, bucket: str) -> pulse_store.Item:
        """The open item while a run goes: mentioned by the next digest when it is new, its
        bucket changes (30 minutes, waiting) or the worker notes something."""
        started = run.started_at or self.clock()
        at = started.astimezone(self.tz).strftime("%H:%M")
        labels = {"waiting": "waiting for usage reset", "question": "waiting for your answer"}
        what = labels.get(bucket, "still running")
        title = f"{run.code} {what} · {_part(run.repo, 60) or 'no repo'} · started {at}"
        did = did_list(self.conn, run.code)
        shows_note = bucket != "question" and not (did and did_of(run.last_note or ""))
        note = _part(run.last_note, NOTE_IN_TITLE) if shows_note else ""  # a did: note: below
        if note:
            title += f" · {note}"
        title = _with_did(title, did)
        state: dict[str, object] = {"bucket": bucket, "note": note}
        if did:
            state["did"] = [*did[-1:], str(len(did))]  # a new side effect is news again
        now = self.clock()
        with atomic(self.conn):
            _change, item = pulse_store.upsert(self.conn, SOURCE, item_key(run.code), title,
                                               "normal", state, False, TTL_S, now)  # fmt: skip
        return item

    def ended(self, run: Run, title: str, *, urgent: bool) -> pulse_store.Item:
        """The run's last word, untold: the next digest says it once, unless :meth:`told`
        marks it heard first. A rerun with the same outcome keeps what was told."""
        state = {"status": run.status, "pr": run.pr_number}
        title = _with_did(title, did_list(self.conn, run.code))
        now = self.clock()
        with atomic(self.conn):
            _change, item = pulse_store.upsert(
                self.conn, SOURCE, item_key(run.code), title,
                "urgent" if urgent else "normal", state, True, TTL_S, now,
            )  # fmt: skip
        return item

    def told(self, item: pulse_store.Item) -> None:
        pulse_store.mark_told(self.conn, [item], self.clock())

    def clear_silently(self, run: Run) -> None:
        """End the item without a digest mention (the owner got a direct reply: a cancel)."""
        key = item_key(run.code)
        with atomic(self.conn):
            if pulse_store.get(self.conn, SOURCE, key) is None:
                return
            item = self.ended(run, f"{run.code} finished · {_part(run.repo) or 'no repo'}",
                              urgent=False)  # fmt: skip
            self.told(item)

    # -- notes --

    def note_text(self, run: Run) -> str | None:
        """``R4 note: …`` for an urgent note texted now, or None when the run already had
        ``NOTE_TEXTS_PER_HOUR`` this hour (the digest line still shows the latest)."""
        note = _part(run.last_note, 300)
        if not note:
            return None
        now = self.clock()
        sent = self._note_texts.setdefault(run.code, deque())
        while sent and now - sent[0] >= timedelta(hours=1):
            sent.popleft()
        if len(sent) >= NOTE_TEXTS_PER_HOUR:
            with atomic(self.conn):
                audit(self.conn, "run.note_held", run=run.code, reason="capped")
            return None
        sent.append(now)
        return f"{run.code} note: {note}"

    def did_line(self, run: Run) -> str | None:
        """``Did: …`` for a texted end reply, or None when the run took no side effects."""
        part = did_part(did_list(self.conn, run.code), DID_IN_TEXT)
        return "Did" + part[len("did") :] if part else None

    def self_op_text(self, run: Run, what: str) -> str:
        """``R4 restarted Resonant (abc1234)``: a worker's restart or update, texted now."""
        return f"{run.code} {_part(what)}"

    def forget(self, code: str) -> None:
        """The run ended: drop its note counter."""
        self._note_texts.pop(code, None)

    def audit_report(self, run: Run, event: str, level: Level) -> None:
        with atomic(self.conn):
            audit(self.conn, "run.report", run=run.code, event=event, notify=level)


__all__ = [
    "DID_KEEP",
    "DID_PREFIX",
    "NOTE_TEXTS_PER_HOUR",
    "WorkerReports",
    "did_list",
    "did_of",
    "did_part",
    "end_event",
    "end_title",
    "is_worker_item",
    "item_key",
    "record_did",
]

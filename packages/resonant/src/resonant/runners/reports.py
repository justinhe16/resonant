"""Worker reporting by urgency (docs/employee-plan.md D5, EM-6).

Every worker event the owner may hear about gets a level, ``now`` (texted right away) or
``digest`` (the hourly "btw" pulse and the morning briefing, as a line in their ``Workers:``
section). One pulse item per run carries what the digest says about it: ``core`` /
``run/<code>`` (``store.pulse``), so the told state survives restarts and nothing is said
twice.

Events and their defaults (``trust.notify_level``, EM-4):

- ``pr`` (a run ended with its PR open), ``done`` (a run with no repo, or no changes) and
  ``merged``: the digest.
- ``failed``: now. A ``notify_digest`` rule lowers it only when the rule names ``failed``
  itself ("stop texting right away on failed"): a ``*`` rule ("only digest trading") never
  silences a failure.
- ``note``: the digest, and many notes of one run collapse into its latest (the run's item
  shows ``last_note``). ``urgent`` (``resonant note --urgent``): now, at most
  ``NOTE_TEXTS_PER_HOUR`` texts per run an hour (the rest only update the digest line).
- ``question`` and ``check``: not handled here. EM-5's relay texts questions and EM-4's
  approval prompt texts checks; the digest only shows the run as ``waiting for your answer``.

Rules: system and repo are the run's repo (``scratch`` for a run with no repo). ``pr`` and
``done`` are matched with the action ``done``, so "text me right away when trading runs
finish" (``notify_now`` on ``done`` in ``trading``) covers a run that ended with a PR too; a
rule on ``pr`` itself is more specific. A run started with ``report="now"`` (``/run``, or the
owner asked to hear right away) texts its end now whatever the rules say: the request is
about that one run. A cancel is always texted (the owner did it), and so is an unrevoked
GitHub token.

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
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal, Protocol
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


def _part(text: str | None, limit: int = TITLE_PART_MAX) -> str:
    """A worker- or GitHub-supplied part of a title, cleaned once and clipped."""
    flat = clean_text(text or "")
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def end_title(run: Run, ci: str | None = None) -> str:
    """The digest line for an ended run (code-built around cleaned parts)."""
    match run.status:
        case "ready":
            label = "no CI" if ci == "none" else f"CI {ci or 'unknown'}"
            title = f"{run.code} done: PR #{run.pr_number} ({label}) · {_part(run.pr_url)}"
            if run.flag_line or run.failure:
                title += f" · ⚠ /runs {run.code}"
            return title
        case "done":
            summary = _part(run.summary)
            return f"{run.code} done" + (f": {summary}" if summary else "")
        case "no_changes":
            return f"{run.code}: no changes · {_part(run.repo) or 'no repo'}"
        case "failed":
            return f"{run.code} failed: {_part(run.failure) or 'unknown error'}"
        case "merged":
            return f"{run.code} merged: PR #{run.pr_number} · {_part(run.pr_url)}"
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
        """How soon the owner hears ``event`` of ``run`` (see the module docstring)."""
        if event in ("pr", "done", "merged") and self.report_pref(run.code) == "now":
            return "now"
        action = "done" if event in ("pr", "done") else "note" if event == "urgent" else event
        system = run.repo or SCRATCH_SYSTEM
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
        note = _part(run.last_note, NOTE_IN_TITLE) if bucket != "question" else ""
        if note:
            title += f" · {note}"
        state = {"bucket": bucket, "note": note}
        now = self.clock()
        with atomic(self.conn):
            _change, item = pulse_store.upsert(self.conn, SOURCE, item_key(run.code), title,
                                               "normal", state, False, TTL_S, now)  # fmt: skip
        return item

    def ended(self, run: Run, title: str, *, urgent: bool) -> pulse_store.Item:
        """The run's last word, untold: the next digest says it once, unless :meth:`told`
        marks it heard first. A rerun with the same outcome keeps what was told."""
        state = {"status": run.status, "pr": run.pr_number}
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

    def forget(self, code: str) -> None:
        """The run ended: drop its note counter."""
        self._note_texts.pop(code, None)

    def audit_report(self, run: Run, event: str, level: Level) -> None:
        with atomic(self.conn):
            audit(self.conn, "run.report", run=run.code, event=event, notify=level)


__all__ = [
    "NOTE_TEXTS_PER_HOUR",
    "WorkerReports",
    "end_event",
    "end_title",
    "is_worker_item",
    "item_key",
]

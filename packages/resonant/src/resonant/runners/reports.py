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
  shows ``last_note``). Since GH-2 a running run's line is status, not news: a note (or the
  run starting, or passing 30 minutes) never makes a digest go out on its own; the rollup
  (``extensions/rollup.py``) lists the run under ``Still working on`` when something else
  does. A new ``did:`` side effect is still news. ``urgent`` (``resonant note --urgent``):
  now, at most ``NOTE_TEXTS_PER_HOUR`` texts per run an hour (the rest only update the
  digest line).
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

Detected side effects (TR-1). The runner also reads the worker's own tool calls as they
stream (``runners/side_effects.py``) and keeps the outside actions it finds in kv
``run.seen:<code>`` (``record_detected``: the last ``DETECTED_KEEP``, one per ``what``; after
``DETECTED_MAX`` distinct ones a run's further ones are dropped). They
join the ``did:`` list after the worker's own items, marked ``(detected)``, unless a ``did:``
item already names them (``side_effects.reported``): ``did: merged PR #2 with gh; pushed to
main (detected)``. A high-impact one (a push to the base branch, a publish, a delete) is the
``side_effect`` event, matched with its kind as the action (``push``, ``publish``, ``repo``,
…): now by default, texted ``R4 detected: force-pushed to main`` within the same per-run cap
as urgent notes; a ``notify_digest`` rule lowers it. The rest are digest only.

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
import re
import sqlite3
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

from resonant.extensions.notify import clean_text
from resonant.gate.codes import clean_external
from resonant.runners.side_effects import Kind, SideEffect, reported
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
_SEEN_KEY = "run.seen:"  # kv: side effects detected in a run's commands (TR-1), oldest first
DETECTED_KEEP = 20
DETECTED_MAX = 200  # distinct side effects recorded (and audited, texted) per run, at most
_SEEN_ALL_KEY = "run.seen_all:"  # kv: every what recorded for a run, for dedupe
DETECTED_MARK = " (detected)"
DID_PREFIX = "did:"
DID_KEEP = 20
DID_ITEM_MAX = 80
DID_IN_TITLE = 100  # the did part of a digest line
DID_IN_TEXT = 300  # the Did: line of a texted end reply
SELF_SYSTEM = "resonant"  # the trust system of a worker's restart or update
_ALERT_KEY = "run.alert:"  # kv: a run's ⚠ lines for its end text (PER-268), oldest first
_MERGED_KEY = "run.merged:"  # kv: the PRs a run merged onto its base (PER-268)
ALERTS_KEEP = 3
# Detected kinds that are said in the run's one end text, never texted mid-run (PER-268): a
# merge is the work a run is often asked to do, so its end text names it.
HELD_KINDS = frozenset({"merge"})
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


def record_detected(conn: sqlite3.Connection, code: str, effect: SideEffect) -> bool:
    """Add a side effect detected in run ``code``'s commands (TR-1). False when the run
    already has it (same ``what``): a command run twice is listed once. Kept to the last
    ``DETECTED_KEEP``. ``what`` is code-built by ``side_effects`` and cleaned again here."""
    what = clean_external(effect.what)[:DID_ITEM_MAX]
    if not what:
        return False
    with atomic(conn):
        seen = _seen(conn, code)
        known: object = kv_get(conn, _SEEN_ALL_KEY + code)
        whats = (
            [w for w in cast(list[object], known) if isinstance(w, str)]
            if isinstance(known, list)
            else []
        )
        if what in whats or any(s.get("what") == what for s in seen):
            return False
        if len(whats) >= DETECTED_MAX:
            return False  # a run looping over many distinct commands: stop recording
        kv_set(conn, _SEEN_ALL_KEY + code, [*whats, what])
        entry: dict[str, object] = {"what": what, "kind": effect.kind, "high": effect.high,
                                    "keys": list(effect.keys)}  # fmt: skip
        kv_set(conn, _SEEN_KEY + code, [*seen, entry][-DETECTED_KEEP:])
    return True


def _seen(conn: sqlite3.Connection, code: str) -> list[dict[str, object]]:
    value: object = kv_get(conn, _SEEN_KEY + code)
    if not isinstance(value, list):
        return []
    out: list[dict[str, object]] = []
    for v in cast(list[object], value):
        if isinstance(v, dict) and isinstance(cast(dict[str, object], v).get("what"), str):
            out.append(cast(dict[str, object], v))
    return out


def detected_list(conn: sqlite3.Connection, code: str) -> list[SideEffect]:
    """The side effects detected in run ``code``'s commands, oldest first."""
    out: list[SideEffect] = []
    for s in _seen(conn, code):
        keys = s.get("keys")
        out.append(SideEffect(
            cast(Kind, str(s.get("kind") or "http")), str(s["what"]), s.get("high") is True,
            tuple(str(k) for k in cast(list[object], keys)) if isinstance(keys, list) else (),
        ))  # fmt: skip
    return out


def shown_did(conn: sqlite3.Connection, code: str) -> list[str]:
    """What the run's ``did:`` line lists: the worker's ``did:`` notes and Resonant's own
    records, then each detected side effect they don't already name, marked
    ``(detected)``."""
    did = did_list(conn, code)
    seen = [f"{e.what}{DETECTED_MARK}" for e in detected_list(conn, code) if not reported(e, did)]
    return [*did, *seen]


def set_alert(conn: sqlite3.Connection, code: str, line: str) -> None:
    """Keep a ⚠ line (code-built) for run ``code``'s end text: a direct push to its base, a
    PR it merged by itself. The end text carries it on its own line and goes out now."""
    with atomic(conn):
        kept = [a for a in alerts(conn, code) if a != line]
        kv_set(conn, _ALERT_KEY + code, [*kept, line][-ALERTS_KEEP:])


def alerts(conn: sqlite3.Connection, code: str) -> list[str]:
    value: object = kv_get(conn, _ALERT_KEY + code)
    if not isinstance(value, list):
        return []
    return [v for v in cast(list[object], value) if isinstance(v, str)]


def set_merged(conn: sqlite3.Connection, code: str, names: Sequence[str]) -> None:
    """The PRs run ``code`` merged onto its base (``PR #121``, code-built), for its end text."""
    with atomic(conn):
        kv_set(conn, _MERGED_KEY + code, list(names))


def merged_list(conn: sqlite3.Connection, code: str) -> list[str]:
    value: object = kv_get(conn, _MERGED_KEY + code)
    if not isinstance(value, list):
        return []
    return [v for v in cast(list[object], value) if isinstance(v, str)]


def merged_names(effects: Sequence[SideEffect], shas: Sequence[str]) -> list[str]:
    """What a run merged onto its base, for its end text: ``PR #121`` for each ``gh pr
    merge`` read from its commands (its keys are ``("merge", "121")``), else one ``a PR
    (abc1234)`` per merge the activity feed shows. Code-built."""
    found: list[str] = []
    for e in effects:
        if e.kind != "merge" or len(e.keys) != 2 or not e.keys[1].isdigit():
            continue
        name = f"PR #{e.keys[1]}"
        if name not in found:
            found.append(name)
    if found:
        return found
    return [f"a PR ({sha[:7]})" if sha != "?" else "a PR" for sha in shas]


def _names_merge(item: str, names: Sequence[str]) -> bool:
    """Whether a ``did:`` item is a merge the headline already names (``squash-merged PR
    #121`` for ``PR #121``)."""
    low = item.lower()
    if "merge" not in low:
        return False
    return any(re.search(rf"#{re.escape(n[4:])}(?![0-9])", low) for n in names
               if n.startswith("PR #")) or "a pr into" in low  # fmt: skip


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


DEPS_CHARS = 80  # the named paths, so a merged line with its URL and did: list fits


def deps_line(paths: Sequence[str]) -> str:
    """``deps changed: package.json, uv.lock`` for the dependency files a merged PR changed
    (``CheckResult.deps``), or an empty string. Code-built: each path cleaned and clipped,
    at most ``DEPS_SHOWN`` named and the rest counted."""
    if not paths:
        return ""
    names: list[str] = []
    for p in paths[:DEPS_SHOWN]:
        part = _part(p, 60)
        if names and len(", ".join([*names, part])) > DEPS_CHARS:
            break  # keep the line short: the digest title is clipped at 200
        names.append(part)
    more = len(paths) - len(names)
    return f"deps changed: {', '.join(names)}" + (f" (+{more} more)" if more > 0 else "")


def end_title(
    run: Run, ci: str | None = None, *, deps: Sequence[str] = (), merged: Sequence[str] = ()
) -> str:
    """The digest line for an ended run (code-built around cleaned parts). ``deps``: a
    merged run's dependency files, listed after its PR. ``merged``: the PRs a ``done`` run
    merged onto its base (PER-268)."""
    match run.status:
        case "ready":
            label = "no CI" if ci == "none" else f"CI {ci or 'unknown'}"
            title = f"{run.code} done: PR #{run.pr_number} ({label}) · {_part(run.pr_url)}"
            if run.flag_line or run.failure:
                title += f" · ⚠ /runs {run.code}"
            return title
        case "done" if merged:  # PER-268: a run that merged a PR onto its base, as asked
            return (
                f"{run.code} done: merged {', '.join(merged)} into {run.base_branch or 'the base'}"
            )
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

    def level(self, run: Run, event: str, action: str | None = None) -> Level:
        """How soon the owner hears ``event`` of ``run`` (see the module docstring). A
        ``restart``/``update`` is matched with system ``resonant`` (what it acted on). A
        detected ``side_effect`` passes its kind as ``action`` (``push``, ``publish``, …)."""
        if event in ("pr", "done", "merged") and self.report_pref(run.code) == "now":
            return "now"
        if action is None:
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
        """The open item while a run goes. It is status, not news (GH-2): the rollup lists it
        under ``Still working on`` when something else is sent, so it is written told, with
        the latest bucket and note in its title. Only a new side effect on its ``did:`` list
        is news on its own (its line says it at the next digest)."""
        started = run.started_at or self.clock()
        at = started.astimezone(self.tz).strftime("%H:%M")
        labels = {"waiting": "waiting for usage reset", "question": "waiting for your answer"}
        what = labels.get(bucket, "still running")
        title = f"{run.code} {what} · {_part(run.repo, 60) or 'no repo'} · started {at}"
        did = shown_did(self.conn, run.code)
        shows_note = bucket != "question" and not (did and did_of(run.last_note or ""))
        note = _part(run.last_note, NOTE_IN_TITLE) if shows_note else ""  # a did: note: below
        if note:
            title += f" · {note}"
        title = _with_did(title, did)
        state: dict[str, object] = {"running": True}
        if did:
            state["did"] = [*did[-1:], str(len(did))]  # a new side effect is news again
        now = self.clock()
        with atomic(self.conn):
            _change, item = pulse_store.upsert(self.conn, SOURCE, item_key(run.code), title,
                                               "normal", state, False, TTL_S, now)  # fmt: skip
            if not did and item.changed:  # status only: heard as it is written
                pulse_store.mark_told(self.conn, [item], now)
                item = replace(item, told_fingerprint=item.fingerprint, told_at=now)
        return item

    def ended(self, run: Run, title: str, *, urgent: bool) -> pulse_store.Item:
        """The run's last word, untold: the next digest says it once, unless :meth:`told`
        marks it heard first. A rerun with the same outcome keeps what was told."""
        state = {"status": run.status, "pr": run.pr_number}
        title = _with_did(title, shown_did(self.conn, run.code))
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
        if not note or not self._take_text(run.code):
            return None
        return f"{run.code} note: {note}"

    def detected_text(self, run: Run, what: str) -> str | None:
        """``R4 detected: force-pushed to main``: a high-impact side effect read from the
        worker's commands (TR-1), texted now; None past the per-run cap it shares with urgent
        notes (the digest line still lists it)."""
        if not self._take_text(run.code):
            return None
        return f"{run.code} detected: {_part(what)}"

    def _take_text(self, code: str) -> bool:
        """One of the run's ``NOTE_TEXTS_PER_HOUR`` immediate texts, or False (audited)."""
        now = self.clock()
        sent = self._note_texts.setdefault(code, deque())
        while sent and now - sent[0] >= timedelta(hours=1):
            sent.popleft()
        if len(sent) >= NOTE_TEXTS_PER_HOUR:
            with atomic(self.conn):
                audit(self.conn, "run.note_held", run=code, reason="capped")
            return False
        sent.append(now)
        return True

    def also_line(self, run: Run, *, skip: Sequence[str] = ()) -> str | None:
        """``Along the way it merged PR #3 with gh; deployed staging.`` for a texted end
        reply, or None when the run took no side effects (PER-268: one sentence, not a
        ``Did:`` dump). ``skip``: what the headline already says (``PR #121`` of a run that
        merged it), left out."""
        items = shown_did(self.conn, run.code)
        if skip:
            items = [i for i in items if not _names_merge(i, skip)]
        part = did_part(items, DID_IN_TEXT)
        if not part:
            return None
        return f"Along the way: {part[len(DID_PREFIX) :].strip()}."

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
    "DETECTED_KEEP",
    "DETECTED_MARK",
    "DID_KEEP",
    "DID_PREFIX",
    "HELD_KINDS",
    "NOTE_TEXTS_PER_HOUR",
    "WorkerReports",
    "alerts",
    "detected_list",
    "did_list",
    "did_of",
    "did_part",
    "end_event",
    "end_title",
    "is_worker_item",
    "item_key",
    "merged_list",
    "merged_names",
    "record_detected",
    "record_did",
    "set_alert",
    "set_merged",
    "shown_did",
]

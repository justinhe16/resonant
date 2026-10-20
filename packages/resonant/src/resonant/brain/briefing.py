"""The brain's environment briefing and "since we last talked" notes (docs/brain-plan.md D2).

Both are code-built text for the last user turn, never the stable prefix: they change every
turn. The briefing is rendered from the dashboard's local-tier snapshot
(``dashboard.snapshot.build_snapshot``), ``status.collect_status`` and the repo registry (for
the checkout paths the snapshot doesn't carry), and kept under ``BRIEFING_MAX_CHARS`` (about
1.5k tokens): every section has an item cap, and past the budget the longest section loses
items from its end first, each trimmed section ending ``- +N more``.

Owner briefing, in order (empty sections are left out):

- ``Resonant <version>, up 3h 12m.`` plus the flags that aren't the default (dry run,
  paused, autonomy off) and the Claude slots in use.
- ``Unhealthy:`` each health row that is degraded or failing, with its detail.
- ``Runs:`` active runs, then the ones that ended in the last ``RECENT_RUN_HOURS``: code
  with its repo (``R2 (mycelium)``: PER-249, so the reply names the run as the owner knows
  it), state (and why it waits), ask, PR, the Remote Control link of a running session
  (``runs.remote_url``), the worker's last note.
- ``Workers ask:`` open worker questions with their options, by code and repo.
- ``Waiting on the owner:`` open approvals by their code-built summary and expiry, never
  their codes (codes are for the owner's eyes only).
- ``Repos:`` name, its GitHub ``owner/name``, checkout path (a NULL ``path`` means
  ``<runner.code_dir>/<name>``), mode and CI; then Resonant's own checkout
  (``ops.repo_path``) when no registered repo is it (BR-9).
- ``Extensions:`` name, status and health.
- ``Workers aren't set up:`` the runner's setup gaps, when it isn't.

A member's briefing is only the header line: the rest is the owner's.

Text from workers, extensions or repos (asks, notes, questions) is untrusted. ``_clean``
drops control characters and angle brackets (so nothing can close the ``<briefing>`` tag)
and clips every field; the system prompt says the briefing is data.

**Since we last talked** (``since_notes``): what changed between the principal's previous
message and now, as short code-built lines: runs that ended or merged, approvals that
expired, worker questions asked; the newest ``SINCE_MAX``. Owner only (a member gets none).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from resonant.brain.text import flat
from resonant.config import Settings
from resonant.dashboard.snapshot import RunItem, Snapshot, build_snapshot
from resonant.loop.slots import SlotPool
from resonant.status import collect_status
from resonant.store import brain as brain_store
from resonant.store import questions, runs
from resonant.store import repos as repo_store
from resonant.store.db import utcnow
from resonant.store.tasks import principal_thread

BRIEFING_MAX_CHARS = 6000  # ~1.5k tokens
ITEM_MAX_CHARS = 300
ASK_CHARS = 80
NOTE_CHARS = 80
QUESTION_CHARS = 160
RECENT_RUN_HOURS = 24
RUNS_MAX = 10
QUESTIONS_MAX = 5
APPROVALS_MAX = 5
REPOS_MAX = 20
EXTENSIONS_MAX = 20
SINCE_MAX = 8
SINCE_LOOKBACK = timedelta(days=7)  # a longer silence shows the last week only

_ENDED_TEXT = {
    "ready": "is ready",
    "done": "finished",
    "failed": "failed",
    "cancelled": "was cancelled",
    "no_changes": "ended with no changes",
    "merged": "was merged",
}


def _clean(text: str, limit: int) -> str:
    return flat(text, limit)


def _when(at: datetime, now: datetime, tz: ZoneInfo) -> str:
    """``15:04`` today, else ``Oct 17 15:04``, in ``tz``."""
    local, today = at.astimezone(tz), now.astimezone(tz)
    return (
        f"{local:%H:%M}"
        if local.date() == today.date()
        else f"{local:%b} {local.day} {local:%H:%M}"
    )


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m"
    return f"{hours // 24}d {hours % 24}h"


@dataclass
class _Section:
    title: str
    items: list[str]
    hidden: int = 0  # items cut by the section cap or the budget
    inline: bool = field(default=False)  # one line, items joined by ", "

    def render(self) -> str:
        if not self.items and not self.hidden:
            return ""
        more = [f"+{self.hidden} more"] if self.hidden else []
        if self.inline:
            return f"{self.title}: " + ", ".join(self.items + more)
        return "\n".join([f"{self.title}:", *(f"- {i}" for i in self.items + more)])


def _capped(title: str, items: Sequence[str], cap: int, *, inline: bool = False) -> _Section:
    clipped = [_clean(i, ITEM_MAX_CHARS) for i in items]
    return _Section(title, clipped[:cap], max(0, len(clipped) - cap), inline)


def _fit(head: str, sections: list[_Section], budget: int) -> str:
    """``head`` and the sections, dropping items from the end of the section with the most
    shown items (the later one on a tie) until the whole fits ``budget``."""

    def text() -> str:
        return "\n".join(p for p in [head, *(s.render() for s in sections)] if p)

    out = text()
    while len(out) > budget:
        candidates = [s for s in sections if s.items]
        if not candidates:
            return out[: budget - 1] + "…"
        biggest = max(range(len(candidates)), key=lambda i: (len(candidates[i].items), i))
        section = candidates[biggest]
        section.items.pop()
        section.hidden += 1
        out = text()
    return out


def _run_line(r: RunItem, now: datetime, tz: ZoneInfo) -> str:
    state = str(r.state)
    if r.state == "waiting" and r.waiting_reason is not None:
        state = f"waiting ({'your answer' if r.waiting_reason == 'question' else 'usage reset'})"
    parts = [f"{run_name(r.code, r.repo)} {state}", f'"{_clean(r.ask, ASK_CHARS)}"']
    if r.started_at is not None and r.ended_at is None:
        parts.append(f"for {_duration((now - r.started_at).total_seconds())}")
    if r.ended_at is not None:
        parts.append(f"ended {_when(r.ended_at, now, tz)}")
    if r.pr_url:
        parts.append(f"PR #{r.pr_number} {r.pr_url}" if r.pr_number else f"PR {r.pr_url}")
    if r.remote_url:
        parts.append(f"Remote Control {r.remote_url}")
    if r.last_note:
        parts.append(f"note: {_clean(r.last_note, NOTE_CHARS)}")
    return " · ".join(parts)


def run_name(code: str, repo: str | None) -> str:
    """A run as the owner knows it: its code with its repo, ``R2 (mycelium)`` (PER-249)."""
    return f"{code} ({_clean(repo, 60) if repo else 'no repo'})"


def repo_path(name: str, path: Path | None, code_dir: Path) -> Path:
    """The owner's checkout of a repo: its ``path``, else ``<runner.code_dir>/<name>``."""
    return path if path is not None else code_dir / name


@dataclass(frozen=True)
class RepoPath:
    name: str
    path: Path
    slug: str = ""  # GitHub owner/name
    note: str = ""  # shown after the path, e.g. "Resonant itself"


def render_briefing(
    snapshot: Snapshot,
    status: Mapping[str, Any],
    *,
    repo_paths: Sequence[RepoPath],
    owner: bool,
    tz: str,
    budget: int = BRIEFING_MAX_CHARS,
) -> str:
    """The briefing text (without the ``<briefing>`` tags). Pure: ``snapshot.taken_at`` is
    now."""
    zone = ZoneInfo(tz)
    now = snapshot.taken_at
    head = [f"Resonant {snapshot.version}"]
    uptime = status.get("uptime_s")
    if isinstance(uptime, int | float):
        head[0] += f", up {_duration(uptime)}"
    head[0] += "."
    if snapshot.dry_run:
        head.append("Dry run is on (side effects are logged, not done).")
    if snapshot.paused:
        head.append("Paused.")
    if snapshot.autonomy == "off":
        head.append("Autonomy is off.")
    if not owner:
        return " ".join(head)
    head.append(f"Claude slots {snapshot.slots.used}/{snapshot.slots.max} in use.")

    health = [
        f"{h.name} {h.state}" + (f" ({h.reason})" if h.reason else "")
        for h in snapshot.health
        if h.state in ("degraded", "failing")
    ]
    recent = now - timedelta(hours=RECENT_RUN_HOURS)
    run_items = [
        r
        for r in snapshot.runs.items or []
        if r.state in ("queued", "running", "waiting")
        or (r.ended_at is not None and r.ended_at >= recent)
    ]
    repo_of = {r.code: r.repo for r in snapshot.runs.items or []}
    asks = [
        f'{run_name(q.run_code, repo_of.get(q.run_code))} asks: "{_clean(q.text, QUESTION_CHARS)}"'
        + (f" [{' / '.join(_clean(o, 40) for o in q.options)}]" if q.options else "")
        for q in snapshot.questions.items or []
    ]
    approvals = [
        f"{a.summary} ({a.level}, expires {_when(a.expires_at, now, zone)})"
        for a in snapshot.approvals.items or []
    ]
    known = {r.name: r for r in snapshot.repos or []}
    repos: list[str] = []
    for rp in repo_paths:
        item = known.get(rp.name)
        tags = [] if item is None else [item.mode, *(["CI"] if item.ci else [])]
        tags += [rp.note] if rp.note else []
        extra = f" ({', '.join(tags)})" if tags else ""
        slug = f" ({_clean(rp.slug, 120)})" if rp.slug else ""
        repos.append(f"{rp.name}{slug}: {rp.path}{extra}")
    extensions = [f"{e.name} ({e.status}, {e.health})" for e in snapshot.extensions]
    sections = [
        _capped("Unhealthy", health, len(health)),
        _capped("Runs", [_run_line(r, now, zone) for r in run_items], RUNS_MAX),
        _capped("Workers ask", asks, QUESTIONS_MAX),
        _capped("Waiting on the owner", approvals, APPROVALS_MAX),
        _capped("Repos", repos, REPOS_MAX),
        _capped("Extensions", extensions, EXTENSIONS_MAX, inline=True),
    ]
    if not snapshot.runner.set_up:
        missing = snapshot.runner.missing or ["unknown"]
        sections.append(_capped("Workers aren't set up", missing, 5, inline=True))
    return _fit(" ".join(head), sections, budget)


def _same_path(a: Path, b: Path) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def gather_briefing(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    owner: bool,
    started_at: datetime,
    now: datetime | None = None,
    last_tick: datetime | None = None,
    slots: SlotPool | None = None,
    runner_missing: Sequence[str] | None = None,
) -> str:
    """``render_briefing`` over the live store: the local-tier snapshot, ``collect_status``
    and each registered repo's checkout path."""
    now = now or utcnow()
    snapshot = build_snapshot(
        conn,
        settings,
        started_at=started_at,
        last_tick=last_tick,
        slots=slots,
        runner_missing=runner_missing,
        now=now,
    )
    status = collect_status(conn, settings, started_at=started_at, last_tick=last_tick)
    code_dir = settings.runner.code_dir
    paths = [
        RepoPath(r.name, repo_path(r.name, r.path, code_dir), slug=r.slug)
        for r in repo_store.list_all(conn)
    ]
    own = settings.ops.repo_path
    if own is not None:
        mine = [i for i, rp in enumerate(paths) if _same_path(rp.path, own)]
        if mine:
            i = mine[0]
            paths[i] = RepoPath(paths[i].name, paths[i].path, paths[i].slug, "Resonant itself")
        else:
            paths.append(RepoPath("resonant", own, note="Resonant itself"))
    return render_briefing(snapshot, status, repo_paths=paths, owner=owner, tz=settings.timezone)


# --- since we last talked ------------------------------------------------------------------


def last_talked(
    conn: sqlite3.Connection,
    principal: str,
    *,
    before_event_id: str,
    source: str = "imessage",
) -> datetime | None:
    """When ``principal`` sent their previous message (any, task or not), or None."""
    prev = principal_thread(
        conn,
        principal,
        source=source,
        event_type="message",
        before_event_id=before_event_id,
        limit=1,
    )
    return prev[-1][0].occurred_at if prev else None


def since_notes(
    conn: sqlite3.Connection,
    *,
    owner: bool,
    since: datetime,
    now: datetime,
    tz: str,
    limit: int = SINCE_MAX,
) -> list[str]:
    """What changed in ``(since, now]``, oldest first, at most ``limit`` lines (the newest;
    ``+N earlier`` first when some were left out). Only the owner gets notes. ``since`` is
    clamped to ``SINCE_LOOKBACK``."""
    if not owner:
        return []
    zone = ZoneInfo(tz)
    since = max(since, now - SINCE_LOOKBACK)
    found: list[tuple[datetime, str]] = []
    recent = runs.recent(conn, limit=50)
    for r in recent:
        if r.status == "merged" and since < r.updated_at <= now:
            at, verb = r.updated_at, _ENDED_TEXT["merged"]
        elif r.ended_at is not None and since < r.ended_at <= now and r.status in _ENDED_TEXT:
            at, verb = r.ended_at, _ENDED_TEXT[r.status]
        else:
            continue
        line = f"{run_name(r.code, r.repo)} {verb}"
        if r.status == "failed" and r.failure:
            line += f": {_clean(r.failure, NOTE_CHARS)}"
        elif r.pr_number is not None and r.status in ("ready", "merged"):
            line += f", PR #{r.pr_number}"
        found.append((at, f"{line} ({_when(at, now, zone)})"))
    for a in brain_store.expired_since(conn, since, now):
        found.append((a.expired_at, f"Approval expired: {_clean(a.summary, ITEM_MAX_CHARS)}"))
    repo_of: dict[str, str | None] = {r.code: r.repo for r in recent}
    for q in questions.open_asks(conn):
        if since < q.asked_at <= now:
            text = _clean(q.question, QUESTION_CHARS)
            if q.run_code not in repo_of:  # an old active run past ``recent``'s limit
                run = runs.get(conn, q.run_code)
                repo_of[q.run_code] = run.repo if run is not None else None
            name = run_name(q.run_code, repo_of[q.run_code])
            found.append((q.asked_at, f'{name} asked: "{text}"'))
    found.sort(key=lambda x: x[0])
    lines = [line for _, line in found]
    if len(lines) > limit:  # the newest win
        keep = max(0, limit - 1)
        return [f"+{len(lines) - keep} earlier", *lines[len(lines) - keep :]]
    return lines

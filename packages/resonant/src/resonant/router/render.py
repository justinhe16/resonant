"""Reply templates over builtin tool output: code computes, text narrates.

Pure functions of the tool's JSON. A missing or odd field renders as ``?``; nothing here
raises on bad data.
"""

from __future__ import annotations

from typing import Any, cast

from resonant.gate.codes import clean_external

# One iMessage chunk (``gateway.imessage.channel.MAX_CHUNK``): a reply never splits.
MAX_REPLY_CHARS = 2000
_FOOTER_RESERVE = 40  # room for "+N more (/projects all)"

_OPEN_ORDER = ("running", "waiting", "queued")


def duration(seconds: Any) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        return "?"
    s = max(0, int(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def as_dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def state_label(item: dict[str, Any]) -> str:
    """``running``, ``waiting(approval)``, ``queued``, ``done``, ``failed``, ``cancelled``."""
    state = str(item.get("state") or "?")
    reason = item.get("wait_reason")
    if state == "waiting":
        return f"waiting({reason or '?'})"
    return state


def _project_line(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "?")
    title = str(item.get("title") or "")
    bits = [state_label(item), kind]
    if title and title != kind:
        bits.append(title)
    bits.append(duration(item.get("age_s")))
    return "- " + " · ".join(bits)


def render_projects(d: dict[str, Any]) -> str:
    """``/projects``: running, waiting, queued, then recently finished; one line per item."""
    items = [i for i in (as_dict(x) for x in as_list(d.get("items"))) if i]
    if not items:
        return "No tasks yet."
    rank = {s: n for n, s in enumerate(_OPEN_ORDER)}
    ordered = sorted(items, key=lambda i: rank.get(str(i.get("state")), len(rank)))  # stable
    n_open = sum(1 for i in items if i.get("state") in rank)
    head = f"Projects ({n_open} open, {len(items) - n_open} recent):"
    more = d.get("more")
    hidden = more if isinstance(more, int) and not isinstance(more, bool) and more > 0 else 0

    lines = [head]
    size = len(head)
    for n, item in enumerate(ordered):
        line = _project_line(item)
        if size + 1 + len(line) > MAX_REPLY_CHARS - _FOOTER_RESERVE:
            hidden += len(ordered) - n
            break
        lines.append(line)
        size += 1 + len(line)
    if hidden:
        hint = "" if d.get("all") is True else " (/projects all)"
        lines.append(f"+{hidden} more{hint}")
    return "\n".join(lines)


def _gb(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "?"
    return f"{value / 1e9:.1f} GB"


def render_disk(d: dict[str, Any]) -> str:
    """``/disk``: ``Disk: / 120.3 GB free of 494.4 GB (76% used)``, one line per path."""
    lines: list[str] = []
    for item in (as_dict(x) for x in as_list(d.get("disk"))):
        pct = item.get("used_pct")
        used = f"{pct:g}%" if isinstance(pct, int | float) and not isinstance(pct, bool) else "?"
        lines.append(
            f"{item.get('path', '?')} {_gb(item.get('free_bytes'))} free of "
            f"{_gb(item.get('total_bytes'))} ({used} used)"
        )
    if not lines:
        return "Disk: unknown."
    return "Disk: " + "\n".join(lines) if len(lines) == 1 else "Disk:\n" + "\n".join(lines)


def render_logs(d: dict[str, Any]) -> str:
    """``/logs``: the scrubbed lines, oldest first, within one iMessage chunk."""
    lines = [str(x) for x in as_list(d.get("lines"))]
    if not lines:
        return "No log lines."
    kept: list[str] = []
    size = 0
    for line in reversed(lines):  # keep the newest when it doesn't all fit
        if size + len(line) + 1 > MAX_REPLY_CHARS:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(reversed(kept))


def _numbered(lines: list[str], head: str, empty: str) -> str:
    """``head`` then ``lines``, within one iMessage chunk (ending ``+N more``)."""
    if not lines:
        return empty
    out = [head]
    size = len(head)
    for n, line in enumerate(lines):
        if size + 1 + len(line) > MAX_REPLY_CHARS - _FOOTER_RESERVE:
            out.append(f"+{len(lines) - n} more")
            break
        out.append(line)
        size += 1 + len(line)
    return "\n".join(out)


def render_notes(d: dict[str, Any]) -> str:
    """``/notes``: ``3. buy milk``, newest first; ``Notes (2 of 25):`` when some aren't shown."""
    notes = [n for n in (as_dict(x) for x in as_list(d.get("notes"))) if n]
    lines = [f"{n.get('id', '?')}. {' '.join(str(n.get('text', '')).split())}" for n in notes]
    total = d.get("total")
    count = str(len(lines))
    if isinstance(total, int) and not isinstance(total, bool) and total > len(lines):
        count = f"{len(lines)} of {total}"
    return _numbered(lines, f"Notes ({count}):", "No notes.")


def render_reminders(d: dict[str, Any]) -> str:
    """``/reminders``: ``3. Tue 3:00pm: stretch``, soonest first."""
    items = [r for r in (as_dict(x) for x in as_list(d.get("reminders"))) if r]
    lines = [f"{r.get('id', '?')}. {r.get('due', '?')}: {r.get('text', '')}" for r in items]
    return _numbered(lines, f"Reminders ({len(lines)}):", "No reminders.")


def _repo_line(r: dict[str, Any]) -> str:
    ci = r.get("ci")
    bits = [str(r.get("mode", "?"))]
    if ci is not None:
        bits.append("CI" if ci else "no CI")
    source = r.get("source")
    if isinstance(source, str) and source.startswith("extension:"):
        bits.append(f"via {source.removeprefix('extension:')}")
    return f"- {r.get('name', '?')} · {r.get('slug', '?')} · {', '.join(bits)}"


def render_repos(d: dict[str, Any]) -> str:
    """``/repos``: ``- dori · jhe/dori · push, CI``, most recently used first."""
    repos = [r for r in (as_dict(x) for x in as_list(d.get("repos"))) if r]
    if not repos:
        return "No repos yet. /repo add <owner/name> adds one, or just name it in /run."
    lines = [f"Repos ({d.get('count', len(repos))}):", *(_repo_line(r) for r in repos)]
    if d.get("truncated") is True:
        lines.append("+ more")
    return "\n".join(lines)


def render_rules(d: dict[str, Any]) -> str:
    """``/rules``: ``3. ask before merge in trading``, oldest first, then how to forget one."""
    items = [r for r in (as_dict(x) for x in as_list(d.get("rules"))) if r]
    lines = [f"{r.get('id', '?')}. {' '.join(str(r.get('text', '')).split())}" for r in items]
    if not lines:
        return "No rules: runs ask only for something new, irreversible or paid."
    body = _numbered(lines, f"Rules ({len(lines)}):", "")
    return f"{body}\n/rules forget <n> drops one."


def _job_line(j: dict[str, Any]) -> str:
    bits = [f"- {j.get('id', '?')} ({j.get('level', '?')})"]
    nxt = j.get("next_due_text")
    bits.append(f"next {nxt}" if isinstance(nxt, str) else "not scheduled")
    outcome, at = j.get("last_outcome"), j.get("last_run_text")
    if isinstance(outcome, str):
        last = f"last {outcome}" + (f" {at}" if isinstance(at, str) else "")
        streak = j.get("fail_streak")
        if isinstance(streak, int) and not isinstance(streak, bool) and streak > 1:
            last += f" ({streak} in a row)"
        bits.append(last)
    else:
        bits.append("never run")
    return " · ".join(bits)


def render_extensions(d: dict[str, Any]) -> str:
    """``/ext list``: one line per extension (name, status, pin, tools, repos, mute, health), then
    one per job (level, next due, last run). Core jobs (the briefing) follow under ``core``."""
    items = [e for e in (as_dict(x) for x in as_list(d.get("extensions"))) if e]
    core = [j for j in (as_dict(x) for x in as_list(d.get("core"))) if j]
    lines: list[str] = []
    for e in items:
        pin = e.get("commit") or "no commit"
        bits = [
            str(e.get("name", "?")),
            str(e.get("status", "?")),
            f"{pin} digest {e.get('digest', '?')}",
            f"{e.get('tools', '?')} tools",
        ]
        if linked := [clean_external(str(r))[:80] for r in as_list(e.get("repos"))]:
            bits.append(f"repos {', '.join(linked)}")
        if isinstance(e.get("muted_until_text"), str):
            bits.append(f"muted until {e['muted_until_text']}")
        if isinstance(e.get("health"), str):
            bits.append(f"health {e['health']}")
        lines.append(" · ".join(bits))
        lines.extend(_job_line(j) for j in (as_dict(x) for x in as_list(e.get("jobs"))) if j)
    if items:
        head = f"Extensions ({len(items)}):"
    else:
        head = "No extensions installed. On the Mini: resonant ext add repo:<name>"
    if core:
        lines.append("core · built in")
        lines.extend(_job_line(j) for j in core)
    text = "\n".join([head, *lines])
    return text if len(text) <= MAX_REPLY_CHARS else text[: MAX_REPLY_CHARS - 1] + "…"


def _run_line(run: dict[str, Any]) -> str:
    code, repo, status = run.get("code", "?"), run.get("repo", "?"), str(run.get("status", "?"))
    bits = [f"{code} {repo}", status]
    if status in ("running", "waiting"):
        bits.append(duration(run.get("elapsed_s")))
    if run.get("pr_number") is not None:
        bits.append(f"PR #{run['pr_number']}")
    flagged = [str(p) for p in as_list(run.get("flagged_paths"))]
    if flagged:
        bits.append(f"changes {', '.join(flagged)}")
    line = "- " + " · ".join(str(b) for b in bits)
    ask = run.get("ask")
    return f'{line}\n  "{ask}"' if isinstance(ask, str) and ask else line


def render_runs(d: dict[str, Any]) -> str:
    """``/runs``: one line per run (active first), or one run in full (``/runs R3``). With no
    runs it says so, or why the runner can't start any."""
    runs = [r for r in (as_dict(x) for x in as_list(d.get("runs"))) if r]
    if not runs:
        code = d.get("code")
        if isinstance(code, str):
            return f"No run {code}."
        setup = d.get("setup")
        return setup if isinstance(setup, str) and setup else "No runs yet."
    if isinstance(d.get("code"), str):
        run = runs[0]
        lines = [_run_line(run)]
        if run.get("pr_url"):
            lines.append(str(run["pr_url"]))
        for key in ("summary", "failure"):
            if run.get(key):
                lines.append(f"{key}: {run[key]}")
        return "\n".join(lines)
    n_active = sum(1 for r in runs if r.get("status") in ("queued", "running", "waiting"))
    lines = [f"Runs ({n_active} active):", *(_run_line(r) for r in runs)]
    if d.get("truncated") is True:
        lines.append("+ more older runs")
    return "\n".join(lines)

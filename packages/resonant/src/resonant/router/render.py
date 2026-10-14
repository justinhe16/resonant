"""Reply templates over builtin tool output: code computes, text narrates.

Pure functions of the tool's JSON. A missing or odd field renders as ``?``; nothing here
raises on bad data.
"""

from __future__ import annotations

from typing import Any, cast

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

"""Reply templates over builtin tool output: code computes, text narrates.

Pure functions of the tool's JSON. A missing or odd field renders as ``?``; nothing here
raises on bad data.
"""

from __future__ import annotations

from typing import Any, cast

# One iMessage chunk (``gateway.imessage.channel.MAX_CHUNK``): a reply never splits.
MAX_REPLY_CHARS = 2000

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
        # Keep room for the "+N more" footer.
        if size + 1 + len(line) > MAX_REPLY_CHARS - 40:
            hidden += len(ordered) - n
            break
        lines.append(line)
        size += 1 + len(line)
    if hidden:
        hint = "" if d.get("all") is True else " (/projects all)"
        lines.append(f"+{hidden} more{hint}")
    return "\n".join(lines)

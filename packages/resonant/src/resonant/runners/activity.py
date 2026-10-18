"""What a worker is doing now, from its transcript (TR-1, ``run_activity``).

The transcript (``<state>/transcripts/R<n>.jsonl``, written by ``TranscriptWriter``) holds
the session's stream-json lines, already redacted for every secret of the run. This module
reads its tail and reduces the last few minutes to a short, deterministic picture:

- ``step``: what the session is on. A tool call with no result yet is ``running $ pytest -q``;
  a result just back is ``thinking (after $ pytest -q)``; text last is ``writing``; a
  ``result`` record is ``turn ended``; Resonant's ``exit`` record is ``session ended``.
- ``calls``: the last ``CALLS_SHOWN`` tool calls, oldest first (``$ git push``, ``Edit
  app.py``, ``Read README.md``, ``Grep "TODO"``, ``Agent: review``, ``linear create_issue``),
  each cleaned and clipped to ``CALL_MAX``.
- ``calls_in_window``: tool calls whose message time is within the window (None when the
  lines carry no times).
- ``last_text``: the session's last words to itself or the owner, cleaned, ``TEXT_MAX``.
- ``quiet_s``: seconds since the transcript last changed (any line, stderr included).

Nothing here calls a model, so the reply never depends on one. Every string shown is cleaned
with ``clean_external`` (brackets, code-like tokens, non-ASCII) and clipped: the transcript is
the worker's text, shown back to the owner as data.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from resonant.gate.codes import clean_external

TAIL_BYTES = 512 * 1024
WINDOW_MIN = 10
WINDOW_MAX = 24 * 60
CALLS_SHOWN = 5
CALL_MAX = 100
TEXT_MAX = 200


@dataclass(frozen=True)
class Activity:
    step: str
    calls: tuple[str, ...]
    calls_in_window: int | None
    last_text: str | None
    quiet_s: float | None
    events: int


def clip(text: str, limit: int) -> str:
    flat = clean_external(text)
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def read_tail(path: Path, limit: int = TAIL_BYTES) -> list[dict[str, Any]]:
    """The JSON objects in the last ``limit`` bytes of ``path`` (a partial first line is
    dropped). [] when there is no file. A symlink is never followed, and anything but a
    regular file (a FIFO would block) is [] too."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return []
        size = st.st_size
        start = max(size - limit, 0)
        os.lseek(fd, start, os.SEEK_SET)
        data = b""
        while len(data) < size - start:
            chunk = os.read(fd, size - start - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]
    out: list[dict[str, Any]] = []
    for line in lines:
        if not line.startswith(b"{"):
            continue
        with contextlib.suppress(ValueError, RecursionError):
            obj: object = json.loads(line)
            if isinstance(obj, dict):
                out.append(cast(dict[str, Any], obj))
    return out


def quiet_for(path: Path, now: datetime) -> float | None:
    """Seconds since ``path`` last changed, or None without it."""
    try:
        mtime = path.lstat().st_mtime
    except OSError:
        return None
    return max(now.timestamp() - mtime, 0.0)


_SCRUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    # header and credential options: the value goes
    (re.compile(r"(?<!\S)(-H|--header|-u|--user|--token|--password|--passwd|--oauth2-bearer|"
                r"--api-key|--secret|--auth)(=|\s+)('[^']*'|\"[^\"]*\"|\S+)"), r"\1\2…"),
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 …"),
    (re.compile(r"(?i)\b(authorization|api[_-]?key|token|secret|password|passwd|auth)\b(\s*[:=]\s*)"
                r"('[^']*'|\"[^\"]*\"|\S+)"), r"\1\2…"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s@'\"]+@"), r"\1"),  # URL user and password
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s?'\"]*)\?[^\s'\"]*"), r"\1?…"),  # query strings
    (re.compile(r"(?<![\S])([A-Za-z_][A-Za-z0-9_]*)=(?!=)('[^']*'|\"[^\"]*\"|\S+)"), r"\1=…"),
)  # fmt: skip


def scrub(text: str) -> str:
    """A command as shown back to the owner: header and credential option values, auth
    words' values, URL credentials and query strings, and ``NAME=value`` values replaced by
    ``…``. On top of the transcript's redaction of known secrets."""
    for pattern, repl in _SCRUBS:
        text = pattern.sub(repl, text)
    return text


def describe_call(name: str, args: Mapping[str, Any]) -> str:
    """A tool call in a few words: ``$ <command>``, ``Edit <file>``, ``linear create_issue``."""

    def s(key: str) -> str:
        v = args.get(key)
        return v if isinstance(v, str) else ""

    if name == "Bash":
        return "$ " + scrub(re.sub(r"\s+", " ", s("command")).strip())
    if name in ("Edit", "MultiEdit", "Write", "Read", "NotebookEdit"):
        path = s("file_path") or s("notebook_path")
        return f"{name} {Path(path).name}" if path else name
    if name in ("Grep", "Glob"):
        return f'{name} "{s("pattern")}"'
    if name in ("Agent", "Task"):
        return f"{name}: {s('description')}".rstrip(": ")
    if name in ("WebFetch", "WebSearch"):
        target = scrub(s("url") or s("query"))
        return f"{name} {target}".rstrip()
    if name.startswith("mcp__"):
        parts = name.split("__")
        return " ".join(parts[1:3]) if len(parts) > 2 else name
    return name


def _time(msg: Mapping[str, Any]) -> datetime | None:
    value = msg.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _blocks(msg: Mapping[str, Any]) -> list[dict[str, Any]]:
    inner: object = msg.get("message")
    content: object = (
        cast(dict[str, object], inner).get("content") if isinstance(inner, dict) else None
    )
    if not isinstance(content, list):
        return []
    return [cast(dict[str, Any], b) for b in cast(list[object], content) if isinstance(b, dict)]


def summarize(
    events: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    window_min: int = WINDOW_MIN,
    quiet_s: float | None = None,
) -> Activity:
    """Reduce transcript ``events`` (oldest first) to an :class:`Activity`."""
    since = now - timedelta(minutes=window_min)
    calls: list[tuple[str, str]] = []  # (id, description)
    answered: set[str] = set()
    in_window = 0
    timed = False
    last_text: str | None = None
    last: str | None = None  # "call", "result", "text", "ended", "exited"
    for msg in events:
        kind = msg.get("type")
        if kind == "assistant":
            at = _time(msg)
            for b in _blocks(msg):
                if b.get("type") == "tool_use" and isinstance(b.get("name"), str):
                    args = b.get("input")
                    desc = describe_call(
                        str(b["name"]), cast(dict[str, Any], args) if isinstance(args, dict) else {}
                    )
                    bid = b.get("id")
                    calls.append((bid if isinstance(bid, str) else "", desc))
                    last = "call"
                    if at is not None:
                        timed = True
                        in_window += at >= since
                elif (
                    b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip()
                ):
                    last_text = str(b["text"])
                    last = "text"
        elif kind == "user":
            for b in _blocks(msg):
                if b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if isinstance(tid, str):
                        answered.add(tid)
                    last = "result"
        elif kind == "result":
            last = "ended"
            text = msg.get("result")
            if isinstance(text, str) and text.strip():
                last_text = text
        elif kind == "resonant.exit":
            last = "exited"
    step = _step(last, calls, answered)
    shown = tuple(clip(d, CALL_MAX) for _id, d in calls[-CALLS_SHOWN:])
    return Activity(
        step=step,
        calls=shown,
        calls_in_window=in_window if timed else None,
        last_text=clip(last_text, TEXT_MAX) if last_text else None,
        quiet_s=quiet_s,
        events=len(events),
    )


def _step(last: str | None, calls: list[tuple[str, str]], answered: Iterable[str]) -> str:
    done = set(answered)
    if last == "exited":
        return "session ended"
    if last == "ended":
        return "turn ended"
    if last is None:
        return "starting"
    pending = [d for i, d in calls if i and i not in done]
    if last == "call" and pending:
        return "running " + clip(pending[-1], CALL_MAX)
    if last == "call" and calls:
        return "running " + clip(calls[-1][1], CALL_MAX)
    if last == "result" and calls:
        return f"thinking (after {clip(calls[-1][1], 60)})"
    return "writing"


def ago(seconds: float | None) -> str:
    """``20s``, ``4m``, ``2h 5m``."""
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {s % 3600 // 60}m"


def headline(code: str, repo: str | None, step: str, act: Activity | None, window_min: int) -> str:
    """One deterministic line: ``R4 dori: running $ pytest -q · 14 calls in 10m · last
    event 20s ago``."""
    bits = [f"{code} {repo or 'no repo'}: {step}"]
    if act is not None and act.calls_in_window is not None:
        bits.append(f"{act.calls_in_window} tool calls in {window_min}m")
    if act is not None and act.quiet_s is not None:
        bits.append(f"last event {ago(act.quiet_s)} ago")
    return " · ".join(bits)


__all__ = [
    "Activity",
    "ago",
    "describe_call",
    "headline",
    "quiet_for",
    "read_tail",
    "scrub",
    "summarize",
]

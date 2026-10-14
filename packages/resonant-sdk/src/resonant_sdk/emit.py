"""``emit()``: send a structured event from an extension process to Resonant.

Resonant runs extension commands with ``RESONANT_EVENTS_FD=3``: fd 3 is a pipe for events,
one JSON object per line, ``{type, text, dedupe_key, data?}``. Events reach the daemon only
when the run succeeds (exit 0 with one JSON object on stdout). The daemon stores each one
as ``ext.<name>.<type>`` with dedupe key ``ext:<name>:<dedupe_key>``, so re-emitting the
same key is harmless; notifications are sent for the types in ``notify.on``.

Extensions run as ``python -I -S``, so this package isn't importable from them: this file
is stdlib-only, and an extension copies it next to its script. Returning
``{"events": [...]}`` in the result JSON is equivalent.

    from emit import emit
    emit("ci_failed", "resonant#40 CI failed (lint)", "resonant#40:run:123:failed")
"""

from __future__ import annotations

import json
import os
from typing import Any

EVENTS_FD_ENV = "RESONANT_EVENTS_FD"
MAX_DEDUPE_KEY = 200


def emit(type: str, text: str, dedupe_key: str, data: dict[str, Any] | None = None) -> bool:
    """Write one event line. False when not running under Resonant (no events fd)."""
    if not dedupe_key or len(dedupe_key) > MAX_DEDUPE_KEY:
        raise ValueError(f"dedupe_key must be 1..{MAX_DEDUPE_KEY} characters")
    fd_text = os.environ.get(EVENTS_FD_ENV)
    if not fd_text or not fd_text.isdigit():
        return False
    event: dict[str, Any] = {"type": type, "text": text, "dedupe_key": dedupe_key}
    if data is not None:
        event["data"] = data
    line = (json.dumps(event, separators=(",", ":")) + "\n").encode()
    try:
        os.write(int(fd_text), line)
    except OSError:
        return False
    return True

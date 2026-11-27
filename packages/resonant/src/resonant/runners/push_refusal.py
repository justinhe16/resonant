"""Why the session's own ``git push`` was refused (PER-355).

A push run's session pushes its branch itself; the daemon never does. When the run ends with
commits of its own that never reached the remote, its end text says ``couldn't push`` and,
when the session's last ``git push`` failed, git's reason in one line (``non-fast-forward``,
GitHub refusing a workflow change without the ``workflows`` permission, a 403).

Where it comes from: the worker's Bash calls in the session's stream-json. A call is noted
when the assistant makes it and its command runs ``git push``; its result is read when it
comes back. A refusal in git's output records the reason (:func:`reason_of`); a call that
went through clears it. Only one line of git's output is kept, and the caller cleans and
clips it like any other outside text before it reaches a message.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from resonant.runners.milestones import tool_results
from resonant.runners.side_effects import commands, tool_calls, unwrap

MAX_OPEN = 50  # push calls remembered while their results are pending

# `` ! [remote rejected] b -> b (reason)`` / `` ! [rejected] b -> b (non-fast-forward)``
_REJECTED = re.compile(r"!\s*\[(?:remote )?rejected\][^\n(]*\(([^)\n]{1,300})\)")
_GENERIC = frozenset({"pre-receive hook declined"})  # the hook's own lines say more
_REMOTE = re.compile(r"^remote:\s*(?:error:\s*)?(?:GH\d+:\s*)?(\S.{0,300})$", re.MULTILINE)
_FATAL = re.compile(r"^(?:fatal|error):\s*(\S.{0,300})$", re.MULTILINE)
_FAILED = re.compile(r"\[(?:remote )?rejected\]|^error: failed to push|^fatal:", re.MULTILINE)
_NOISE = re.compile(r"^(?:-+|=+|\*+)$")
_GIT_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})


def is_push(command: str) -> bool:
    """Whether a shell command line runs ``git push`` (in any of its simple commands), its
    own branch's included (``side_effects`` leaves those out: they aren't outside actions)."""
    for argv in commands(command):
        a = unwrap(argv)
        if not a or a[0].rsplit("/", 1)[-1] != "git":
            continue
        i = 1
        while i < len(a) and a[i].startswith("-"):
            i += 2 if a[i] in _GIT_VALUE else 1
        if i < len(a) and a[i] == "push":
            return True
    return False


def reason_of(text: str) -> str | None:
    """Git's reason for a refused push, from its output, in one line (None if none shows).
    A ref's own ``[rejected]`` reason first, unless it's only the hook's "declined": then
    what the remote said; then git's ``fatal:``/``error:`` line."""
    rejected = _REJECTED.search(text)
    if rejected is not None and rejected[1].strip() not in _GENERIC:
        return rejected[1].strip()
    for m in _REMOTE.finditer(text):
        line = m[1].strip()
        if line and not _NOISE.fullmatch(line):
            return line
    if rejected is not None:
        return rejected[1].strip()
    fatal = [m[1].strip() for m in _FATAL.finditer(text)]
    useful = [f for f in fatal if not f.startswith("failed to push some refs")]
    return (useful or fatal or [None])[0]


class PushWatch:
    """One session's ``git push`` calls and the reason the last one failed, if it did. Pure:
    never raises on odd input."""

    def __init__(self) -> None:
        self._open: dict[str, None] = {}  # insertion-ordered: the oldest goes first
        self.refused: str | None = None

    def calls(self, message: Mapping[str, Any]) -> None:
        """Note the ``git push`` calls in an ``assistant`` message."""
        for tid, name, args in tool_calls(message):
            command: Any = args.get("command")
            if tid and name == "Bash" and isinstance(command, str) and is_push(command):
                if len(self._open) >= MAX_OPEN:
                    self._open.pop(next(iter(self._open)))
                self._open[tid] = None

    def results(self, message: Mapping[str, Any]) -> None:
        """Read a ``user`` message's results of noted push calls: git's own words of a
        refusal record its reason; a call that ended well clears it. A failed call whose
        output shows no refusal (another command in it failed) changes nothing."""
        for tid, is_error, text in tool_results(message):
            if tid not in self._open:
                continue
            del self._open[tid]
            if _FAILED.search(text):
                self.refused = reason_of(text) or "git push failed"
            elif not is_error:
                self.refused = None

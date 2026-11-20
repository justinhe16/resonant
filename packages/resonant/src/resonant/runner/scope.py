"""An instance's scope for its workers and brain (PER-291, docs/employee-plan.md CS-1).

Workers stay unguarded (GD-1): nothing here is a wall. It is what an instance tells and gives
its Claude sessions:

- **Instructions** (``runner.instructions_file``, ``brain.instructions_file``): a text file
  appended to every worker's (or the Claude brain's) system prompt. At most
  ``MAX_INSTRUCTIONS_BYTES``, read at run start, logged by sha only. Missing, unreadable, not
  UTF-8 or too big: the session starts without it and the owner gets one ⚠ line.
- **Scoped credentials** (``runner.session_env``, ``{VAR: secret-ref}``): each value read
  from the instance's Keychain at run start and put in the worker's environment. A missing
  one doesn't fail the run; the end text says so once. Values join the run's ``Redactor``.

Personal Resonant sets neither: nothing changes for it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from resonant.secrets import SecretError, SecretMissing, SecretStore

log = logging.getLogger(__name__)

MAX_INSTRUCTIONS_BYTES = 8 * 1024


@dataclass(frozen=True)
class Instructions:
    """An instructions file as read for one session: ``text`` (None when it isn't used),
    ``problem`` (why not, when a file was configured) and ``sha`` (16 hex, for logs)."""

    path: Path | None
    text: str | None = None
    problem: str | None = None
    sha: str | None = None

    def suffix(self) -> str:
        """What joins the system prompt: the text after a blank line, or nothing."""
        return f"\n\n{self.text}" if self.text else ""

    def warning(self, what: str) -> str | None:
        """The owner's one ⚠ line when a configured file couldn't be used, else None.
        ``what``: whose instructions (``Worker``, ``Brain``)."""
        if self.path is None or self.problem is None:
            return None
        return f"⚠ {what} instructions {self.path} {self.problem}: sessions start without them."


# One per daemon process: a warning is sent once per process and problem, not deduped forever.
_PROCESS = uuid.uuid4().hex[:8]


def warn_key(what: str, line: str) -> str:
    """The send dedupe key of an instructions warning: once per daemon process and line."""
    digest = hashlib.sha256(line.encode()).hexdigest()[:12]
    return f"{what}.instructions:{_PROCESS}:{digest}"


def read_instructions(path: Path | None, *, what: str = "instructions") -> Instructions:
    """Read ``path`` for one session. None: no file configured (no text, no problem)."""
    if path is None:
        return Instructions(None)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return _problem(path, "is missing", what)
    except OSError as e:
        return _problem(path, f"can't be read ({type(e).__name__})", what)
    try:
        size = os.fstat(fd).st_size
        if size > MAX_INSTRUCTIONS_BYTES:
            return _problem(path, f"is over {MAX_INSTRUCTIONS_BYTES // 1024} KB", what)
        raw = os.read(fd, MAX_INSTRUCTIONS_BYTES + 1)
    except OSError as e:
        return _problem(path, f"can't be read ({type(e).__name__})", what)
    finally:
        os.close(fd)
    if len(raw) > MAX_INSTRUCTIONS_BYTES:
        return _problem(path, f"is over {MAX_INSTRUCTIONS_BYTES // 1024} KB", what)
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return _problem(path, "isn't UTF-8", what)
    if not text or "\0" in text:
        return _problem(path, "is empty" if not text else "holds a NUL", what)
    sha = hashlib.sha256(text.encode()).hexdigest()[:16]
    log.info("instructions read", extra={"what": what, "sha": sha, "bytes": len(raw)})
    return Instructions(path, text, None, sha)


def _problem(path: Path, why: str, what: str) -> Instructions:
    log.warning("instructions not used", extra={"what": what, "path": str(path), "why": why})
    return Instructions(path, None, why)


@dataclass(frozen=True)
class ScopedEnv:
    """``runner.session_env`` as read for one run: ``values`` (``VAR -> value``, never logged
    or shown) and ``missing`` (``(VAR, ref)`` for each one that isn't set up or couldn't be
    read)."""

    values: dict[str, str] = field(default_factory=dict[str, str], repr=False)
    missing: tuple[tuple[str, str], ...] = ()

    def secrets(self) -> list[str]:
        """Every value, for the run's ``Redactor``."""
        return [v for v in self.values.values() if v]

    def missing_line(self) -> str | None:
        """One line for the end text naming every scoped variable that isn't set up and the
        command that sets it, or None when every one is there."""
        if not self.missing:
            return None
        return "; ".join(f"{var} not set up (resonant secret set {ref})" for var, ref in
                         self.missing)  # fmt: skip


async def load_session_env(store: SecretStore, spec: Mapping[str, str]) -> ScopedEnv:
    """Read each ``spec`` ref from ``store`` (the instance's Keychain). Never raises: a
    missing or unreadable item is listed in ``missing`` and the rest still load."""
    values: dict[str, str] = {}
    missing: list[tuple[str, str]] = []
    for var, ref in spec.items():
        try:
            values[var] = await store.get(ref)
        except SecretMissing:
            missing.append((var, ref))
        except SecretError as e:
            log.warning("scoped secret unavailable", extra={"var": var, "reason": e.reason})
            missing.append((var, ref))
    return ScopedEnv(values, tuple(missing))

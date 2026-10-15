"""``secret(name)``: read a secret Resonant delivered to this extension process.

Secrets are never in the environment or argv (a same-user process can read both). Resonant
declares them from your manifest's ``secrets:`` list, writes them as one JSON object
``{name: value}`` into a pipe, closes it, and starts you with ``RESONANT_SECRETS_FD`` set to
that pipe's fd (4). The first ``secret()`` call reads and closes it; values are cached in
memory for the life of the process.

Extensions run as ``python -I -S``, so this package isn't importable from them: this file is
stdlib-only, and an extension copies it next to its script (or inlines ``secret()``).

    from secret import secret
    token = secret("token")     # SecretError if not delivered

Never print a secret, put it in argv or in a child's environment. To hand one to another
program, use a private 0600 file (see the github extension's ``gh`` config dir).
"""

from __future__ import annotations

import json
import os
from typing import cast

SECRETS_FD_ENV = "RESONANT_SECRETS_FD"
MAX_BYTES = 64 * 1024

_cache: dict[str, str] | None = None


class SecretError(RuntimeError):
    """A secret was not delivered. The message names the secret, never a value."""


def _load() -> dict[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    fd_text = os.environ.pop(SECRETS_FD_ENV, "")
    if not fd_text.isdigit():
        raise SecretError(
            "no secrets were delivered (not running under Resonant, or none declared)"
        )
    try:
        with os.fdopen(int(fd_text), "rb") as f:
            raw = f.read(MAX_BYTES + 1)
        parsed: object = json.loads(raw)
    except (OSError, ValueError):
        raise SecretError("secrets could not be read") from None
    if (
        len(raw) > MAX_BYTES
        or not isinstance(parsed, dict)
        or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in cast("dict[object, object]", parsed).items()
        )
    ):
        raise SecretError("secrets were malformed")
    _cache = dict(cast("dict[str, str]", parsed))
    return _cache


def secret(name: str) -> str:
    """The value of the declared secret ``name`` (e.g. ``"token"``), or ``SecretError``."""
    value = _load().get(name)
    if not value:
        raise SecretError(f"secret {name!r} is missing; declare it in secrets: and set it")
    return value

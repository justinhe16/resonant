"""Detached restart helper: ``python -m resonant.ops.restart_later <label> <delay_s>``.

Spawned in a new session by the ops tools, so it outlives the daemon it restarts. It
sleeps ``delay_s`` (0..60), then runs ``launchctl kickstart -k gui/<uid>/<label>`` with a
timeout. Only the two ops labels are accepted. It prints nothing and never uses a shell.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from resonant.ops import KICKSTART_TIMEOUT_S, LABELS, kickstart_argv

MAX_DELAY_S = 60.0


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Callable[..., Any] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in LABELS:
        return 2
    try:
        delay = float(args[1])
    except ValueError:
        return 2
    if not 0 <= delay <= MAX_DELAY_S:
        return 2
    sleep(delay)
    try:
        proc = run(
            kickstart_argv(args[0]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=KICKSTART_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

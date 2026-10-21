"""PER-271: no test reaches the owner's live ``~/resonant-runs``, ``~/code`` or ``~/.resonant``.

A runner built from default settings once ran its startup sweep against the live run dirs
(every ``R*/creds`` deprovisioned, every ctl socket removed) from inside a worker's test run.
``tests/conftest.py`` now points ``HOME`` at a temp dir, keeps Resonant's state in the
temp ``RESONANT_HOME``, and an audit hook fails any test that writes, removes, runs a process
in or connects to anything under those trees. These tests prove the guard itself. They never
touch the real dirs: ``sys.audit`` raises the event without doing the operation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from resonant.config import load_settings


def test_defaults_resolve_away_from_the_real_home(real_home: Path) -> None:
    s = load_settings()
    for d in (s.home, s.runner.work_dir, s.runner.code_dir, s.runner.mirror_dir,
              s.runner.secrets_dir, s.runner.cache_dir):  # fmt: skip
        assert not d.is_relative_to(real_home), d


def test_a_removal_in_the_live_run_dirs_is_caught(
    real_home: Path, live_dir_touches: list[str]
) -> None:
    creds = real_home / "resonant-runs" / "R9" / "creds"
    sys.audit("os.remove", str(creds / "token"), -1)
    sys.audit("shutil.rmtree", str(real_home / "resonant-runs" / ".ctl"), None)
    sys.audit("os.mkdir", str(real_home / "code" / ".worktrees"), 0o700, -1)
    sys.audit("sqlite3.connect", str(real_home / ".resonant" / "resonant.db"))
    caught = list(live_dir_touches)
    live_dir_touches.clear()  # it was only the event: let this test pass the guard
    assert [c.split(" ", 1)[0] for c in caught] == [
        "os.remove", "shutil.rmtree", "os.mkdir", "sqlite3.connect",
    ]  # fmt: skip


def test_a_write_a_process_or_a_socket_there_is_caught(
    real_home: Path, live_dir_touches: list[str]
) -> None:
    sys.audit("open", str(real_home / ".resonant" / "config.yaml"), "w", os.O_WRONLY)
    sys.audit("subprocess.Popen", "rm", ["rm", "-rf", "~/resonant-runs/.ctl"], None, None)
    sys.audit("subprocess.Popen", "git", ["git", "status"], str(real_home / "code" / "x"), None)
    sys.audit("socket.connect", None, str(real_home / "resonant-runs" / ".ctl" / "R9" / "s"))
    caught = list(live_dir_touches)
    live_dir_touches.clear()
    assert len(caught) == 4, caught


def test_reads_and_temp_paths_are_not_flagged(
    real_home: Path, tmp_path: Path, live_dir_touches: list[str]
) -> None:
    sys.audit("open", str(real_home / ".resonant" / "config.yaml"), "r", os.O_RDONLY)
    sys.audit("os.remove", str(tmp_path / "resonant-runs" / "R1"), -1)
    sys.audit("subprocess.Popen", "git", ["git", "log", "HEAD~1"], str(tmp_path), None)
    sys.audit("os.mkdir", str(Path(__file__).parent / "__pycache__"), 0o777, -1)  # this checkout
    assert live_dir_touches == []

from __future__ import annotations

import os
import pwd
import sqlite3
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePath
from typing import cast

import pytest

# PER-271: the real home, read before anything below can move HOME. Both the login's home
# (pwd) and $HOME as the session started (a worker's may differ) count.
_REAL_HOMES = tuple(
    dict.fromkeys(
        os.path.realpath(h)
        for h in (pwd.getpwuid(os.getuid()).pw_dir, os.environ.get("HOME") or "")
        if h
    )
)
# What no test may create, delete, change, run a process in or connect to: the live run
# dirs, the owner's checkouts and Resonant's state (the daemon may be running beside us).
PROTECTED = tuple(
    dict.fromkeys(
        root
        for h in _REAL_HOMES
        for name in ("resonant-runs", "code", ".resonant")
        # the link and its target: ~/code may be a symlink to another volume
        for root in (os.path.join(h, name), os.path.realpath(os.path.join(h, name)))
    )
)
# ... except the checkout these tests run from (a worker runs them in its worktree under
# ~/code/.worktrees: pytest's cache, __pycache__, the venv) and the temp dir (a worker's
# TMPDIR can be under its run dir in ~/resonant-runs).
_EXEMPT = tuple(
    dict.fromkeys(
        os.path.realpath(p) for p in (Path(__file__).resolve().parents[1], tempfile.gettempdir())
    )
)
touched: list[str] = []  # what the audit hook saw, emptied before each test


def _under(path: str, roots: Sequence[str]) -> bool:
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def protected_path(raw: object) -> str | None:
    """``raw`` (a path, bytes or a str) as a real path if it is in a protected tree."""
    if isinstance(raw, str | PurePath):
        text = str(raw)
    elif isinstance(raw, bytes):
        text = os.fsdecode(raw)
    else:  # a file descriptor, an inet address, None
        return None
    if not text or "\0" in text:
        return None
    if text.startswith("~"):  # nothing should expand it against the real home either
        text = os.path.join(_REAL_HOMES[0], text[1:].lstrip("/"))
    if not os.path.isabs(text):
        return None
    real = os.path.realpath(text)
    if _under(real, PROTECTED) and not _under(real, _EXEMPT):
        return real
    return None


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# audit events whose leading arguments are paths it changes
_CHANGES = {
    "os.mkdir": 1, "os.remove": 1, "os.rmdir": 1, "os.rename": 2, "os.link": 2,
    "os.symlink": 2, "os.truncate": 1, "os.chmod": 1, "os.chown": 1, "os.chflags": 1,
    "os.utime": 1, "os.setxattr": 1, "os.removexattr": 1, "shutil.rmtree": 1,
    "shutil.copyfile": 2, "shutil.copytree": 2, "shutil.move": 2, "shutil.copymode": 2,
    "shutil.copystat": 2, "shutil.chown": 1, "sqlite3.connect": 1,
}  # fmt: skip


def _audit(event: str, args: tuple[object, ...]) -> None:
    hits: list[object] = []
    if event == "open" and len(args) >= 3:
        mode, flags = args[1], args[2]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS
        )
        if writes:
            hits.append(args[0])
    elif event in _CHANGES:
        hits.extend(args[: _CHANGES[event]])
    elif event == "subprocess.Popen" and len(args) >= 3:  # (executable, args, cwd, env)
        argv = args[1]
        if isinstance(argv, list | tuple) and not isinstance(argv, str | bytes):
            hits.extend(cast("Sequence[object]", argv))  # an argv: its items are checked below
        else:
            hits.append(argv)
        hits.append(args[2])
    elif event in ("socket.bind", "socket.connect") and len(args) >= 2:
        hits.append(args[1])  # a unix socket's path (an inet address is a tuple)
    for raw in hits:
        if (real := protected_path(raw)) is not None:
            touched.append(f"{event} {real}")


sys.addaudithook(_audit)

from resonant.store.db import open_db  # noqa: E402  (after the hook: it sees every import)


@pytest.fixture(autouse=True)
def resonant_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every test from the real ~/.resonant and from RESONANT_* env vars."""
    for key in list(os.environ):
        if key.startswith("RESONANT_"):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RESONANT_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PER-271: ``HOME`` is a temp dir in every test, so every ``~`` default (the runner's
    ``~/resonant-runs`` and ``~/code``, Messages, launchd) lands there and not in the
    owner's live dirs. ``RESONANT_HOME`` above covers Resonant's own state."""
    home = tmp_path / "user-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def never_the_real_dirs() -> Iterator[None]:
    """PER-271: fail any test that creates, deletes or changes anything under the real
    ``~/resonant-runs``, ``~/code`` or ``~/.resonant``, runs a process there or opens its
    sockets or DB (an audit hook sees every file write, removal, ``Popen`` and socket)."""
    touched.clear()
    yield
    seen = list(dict.fromkeys(touched))
    touched.clear()
    assert not seen, f"a test touched the owner's live dirs: {seen[:10]}"


@pytest.fixture
def live_dir_touches() -> list[str]:
    """What the audit hook recorded in this test (``never_the_real_dirs`` asserts it empty)."""
    return touched


@pytest.fixture
def real_home() -> Path:
    """The owner's real home (for tests of the guard itself; never write under it)."""
    return Path(_REAL_HOMES[0])


@pytest.fixture(autouse=True)
def never_the_real_code_dir() -> Iterator[None]:
    """Runner tests must use a temp ``runner.code_dir``: fail any test that creates the real
    ~/code (the default) or its run worktrees or scratch dirs."""
    code = Path(_REAL_HOMES[0]) / "code"
    watched = (code, code / ".worktrees", code / ".scratch")
    before = [p.exists() for p in watched]
    yield
    created = [str(p) for p, was in zip(watched, before, strict=True) if p.exists() and not was]
    assert not created, f"a test wrote the real runner.code_dir: {created}"


@pytest.fixture(autouse=True)
def never_the_real_claude_login(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """No test asks the real ``claude`` whether the owner is logged in (it would read the
    real login): the probe answers "not logged in" and records what it was asked."""
    from resonant.runner import claude_auth

    asked: list[tuple[str, ...]] = []

    def probe(claude: Sequence[str], home: Path, **_kw: object) -> bool:
        asked.append((*claude, str(home)))
        return False

    monkeypatch.setattr(claude_auth, "login_status", probe)
    return asked


@pytest.fixture(autouse=True)
def no_wall_clock_jobs(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``Daemon`` built in a test schedules no core jobs: they run on the real clock (the
    08:00 briefing, the hourly pulse, the 03:30 backup, the 04:30 run cleanup, watches every
    5 minutes), so a test that happened to run at one of those times got extra texts and
    ``job.run`` rows. Tests that drive core jobs build their own ``Scheduler`` on a fake
    clock; ``@pytest.mark.wall_clock_jobs`` keeps the daemon's real ones."""
    if "wall_clock_jobs" in request.keywords:
        return
    from resonant import daemon
    from resonant.extensions.scheduler import CoreDeps, CoreJob

    def no_job(_deps: CoreDeps) -> CoreJob | None:
        return None

    monkeypatch.setattr(daemon, "CORE_JOBS", ())
    monkeypatch.setattr(daemon, "cleanup_job", no_job)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def brew_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake Homebrew prefix whose ``bin/gh`` links into ``Cellar/gh/2.0``, made an install
    dir (``sandbox.profile.HOST_TOOL_DIRS``) so the guard protects it. Returns its ``bin``."""
    from resonant.sandbox import profile

    prefix = tmp_path / "brew"
    real = prefix / "Cellar" / "gh" / "2.0" / "bin" / "gh"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    (prefix / "bin").mkdir()
    (prefix / "bin" / "gh").symlink_to(real)
    monkeypatch.setattr(profile, "HOST_TOOL_DIRS", (str(prefix / "bin"), "/usr/bin", "/bin"))
    return prefix / "bin"


@pytest.fixture(autouse=True)
def never_the_real_claude_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """PER-273: no test's proactive-text voice finds the real ``claude`` on the PATH (it would
    spend the owner's Claude usage): the lookup answers "not installed". Tests that exercise
    the voice pass ``claude_cmd`` (``e2e/fake_claude.py``) or a ``call``."""
    from resonant.runners import voice

    monkeypatch.setattr(voice, "default_claude_cmd", lambda: None)

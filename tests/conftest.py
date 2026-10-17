from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from resonant.store.db import open_db


@pytest.fixture(autouse=True)
def resonant_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every test from the real ~/.resonant and from RESONANT_* env vars."""
    import os

    for key in list(os.environ):
        if key.startswith("RESONANT_"):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RESONANT_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def never_the_real_code_dir() -> Iterator[None]:
    """Runner tests must use a temp ``runner.code_dir``: fail any test that creates the real
    ~/code (the default) or its run worktrees or scratch dirs."""
    code = Path.home() / "code"
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

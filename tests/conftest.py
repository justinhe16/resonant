from __future__ import annotations

import sqlite3
from collections.abc import Iterator
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


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "test.db")
    yield conn
    conn.close()

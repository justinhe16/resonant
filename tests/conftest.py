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


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "test.db")
    yield conn
    conn.close()

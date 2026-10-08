from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from resonant.observability import configure_logging, span


def test_span_records_ok_and_error(db: sqlite3.Connection) -> None:
    with span(db, "gate.evaluate", trace_id="tr", task_id="t1", tool="x") as s:
        s.set(decision="Allow")
    with pytest.raises(ValueError), span(db, "executor", trace_id="tr"):
        raise ValueError("boom")
    rows = db.execute("SELECT name, status, attrs, ended_at FROM spans ORDER BY rowid").fetchall()
    assert [(r["name"], r["status"]) for r in rows] == [
        ("gate.evaluate", "ok"),
        ("executor", "error"),
    ]
    assert json.loads(rows[0]["attrs"]) == {"tool": "x", "decision": "Allow"}
    assert all(r["ended_at"] for r in rows)


def test_json_logs(tmp_path: Path) -> None:
    configure_logging(tmp_path, stderr=False)
    logging.getLogger("resonant.test").info("hello %s", "world")
    for h in logging.getLogger().handlers:
        h.flush()
    line = (tmp_path / "resonant.jsonl").read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["event"] == "hello world" and rec["level"] == "info"
    assert rec["logger"] == "resonant.test"

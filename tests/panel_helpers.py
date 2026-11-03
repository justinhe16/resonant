"""Panel fixtures shared by the panel tests (PER-280): a raw panel output at (or near) its caps,
and a way to store its normalised copy the way the scheduler does."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from resonant.extensions.panels import Normalized, normalize
from resonant.store import extensions as ext_store
from resonant_sdk import panel as p

T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def raw_panel(
    *,
    points: int = 240,
    rows: int = 40,
    items: int = 30,
    details: int = 60,
    ext_tool: str = "go",
    updated_at: datetime | None = None,
) -> dict[str, Any]:
    """A panel with one of every widget, sized by its arguments (defaults: about 20 KB)."""
    times = [T0 + timedelta(minutes=5 * i) for i in range(points)]
    cols = [
        p.column("sym", "Symbol"),
        p.column("qty", "Qty", "number", align="right"),
        p.column("pnl", "P&L", "money", align="right", currency="USD"),
        p.column("at", "Opened", "time"),
        p.column("state", "State", "pill"),
        p.column("act", "Action", "link"),
    ]
    table_rows = [
        {
            "sym": f"SYM{i:03d}",
            "qty": i * 10 + 0.5,
            "pnl": (i - 20) * 12.34,
            "at": times[i % points],
            "state": p.pill("open" if i % 2 else "closed", "ok" if i % 2 else "neutral"),
            "act": p.link("close", ext_tool),
        }
        for i in range(rows)
    ]
    tree = {f"key{i}": {"n": i, "ok": i % 2 == 0, "tags": ["a", "b", None]} for i in range(details)}
    return p.panel(
        [
            p.view(
                "overview",
                "Overview",
                [
                    p.stat("Equity", 10432.5, format="money", delta=12.5, health="ok"),
                    p.stat("Cash", 2210.0, format="money", unit="USD"),
                    p.series(
                        "line",
                        times,
                        {
                            "Equity": [10000 + i * 1.7 for i in range(points)],
                            "Benchmark": [10000 + i * 0.9 for i in range(points)],
                        },
                        format="money",
                        title="Equity",
                    ),
                    p.table(cols, table_rows, title="Positions"),
                ],
            ),
            p.view(
                "system",
                "System",
                [
                    p.status(
                        [
                            {
                                "name": f"svc-{i}",
                                "state": "up",
                                "detail": f"{i} ms",
                                "since": times[0],
                            }
                            for i in range(items)
                        ]
                    ),
                    p.kv({f"metric {i}": i * 1.5 for i in range(items)}),
                    p.text("All quiet."),
                    p.details(tree),
                ],
            ),
        ],
        updated_at=updated_at or T0 + timedelta(minutes=5 * points),
        health="ok",
        local_url="http://127.0.0.1:8765/ledger",
    )


def normalized(raw: dict[str, Any], *, ran_at: datetime | None = None) -> Normalized:
    got = normalize(
        json.loads(json.dumps(raw)),
        max_bytes=32768,
        tools=frozenset({"go"}),
        ran_at=ran_at or T0 + timedelta(days=1),
    )
    assert isinstance(got, Normalized), got
    return got


def store_panel(
    conn: sqlite3.Connection, ext: str, panel_id: str, raw: dict[str, Any], now: datetime
) -> Normalized:
    got = normalized(raw, ran_at=now)
    ext_store.save_panel_ok(
        conn,
        ext,
        panel_id,
        data=json.dumps(got.data, separators=(",", ":")),
        updated_at=got.updated_at,
        ran_at=now,
        bytes=got.bytes,
        expect_by=now + timedelta(minutes=20),
    )
    return got


def panel_manifest(name: str, ids: tuple[str, ...], *, description: str = "") -> dict[str, Any]:
    """A manifest dict (as stored) of ``name`` with these panels."""
    return {
        "name": name,
        "version": 1,
        "description": description,
        "tools": {"go": {"run": "./cli.py go", "effect": "read"}},
        "panels": [
            {
                "id": pid,
                "title": f"Panel {pid}",
                "description": f"The {pid} panel",
                "command": ["./cli.py", "panel", pid],
                "refresh": {"idle": "*/15 * * * *", "active": "*/1 9-16 * * 1-5"},
                "tz": "America/New_York",
                "secrets": [],
                "timeout_s": 30.0,
                "max_bytes": 32768,
            }
            for pid in ids
        ],
    }

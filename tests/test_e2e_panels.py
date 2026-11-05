"""Extension panels end to end (PER-280): a real extension directory with two panels, the real
process runner, host, gate and scheduler on a fake clock, and the snapshot both tiers carry.

``paper`` prints stats, a series, a table (with a link to the extension's own tool) and a local
url; ``oncall`` prints a status list, key/values, text and a details tree. The extension copies
the SDK's stdlib-only ``panel.py`` next to its script, as ``emit.py`` is copied.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from test_panel_scheduler import at, panel_harness, sched_with_boot

from resonant.dashboard.snapshot import build_snapshot, to_remote
from resonant.extensions.host import ExtensionHealth
from resonant.store import extensions as ext_store
from resonant_sdk import panel as sdk_panel
from resonant_sdk import secret as sdk_secret

MANIFEST = """\
name: example
version: 1
description: a ledger with panels
secrets: [token]
tools:
  go:
    run: ./cli.py go
    effect: read
panels:
  - id: paper
    title: Paper trading
    description: PnL and positions
    command: [./cli.py, panel, paper]
    refresh: {idle: "*/15 * * * *", active: "*/1 9-16 * * 1-5"}
    tz: America/Los_Angeles
    secrets: [token]
  - id: oncall
    title: On call
    command: [./cli.py, panel, oncall]
    refresh: {idle: "*/15 * * * *"}
    tz: America/Los_Angeles
  - id: chart
    title: Price
    command: [./cli.py, panel, chart]
    refresh: {idle: "*/15 * * * *"}
    tz: America/Los_Angeles
"""
CLI = """\
#!/usr/bin/env python3
import os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from panel import (column, details, kv, link, marker, price, print_panel, series, stat, status,
                   table, text, trade, view)
from secret import secret

DATA = Path(os.environ["RESONANT_EXT_DATA"])


def main() -> int:
    which = sys.argv[2] if sys.argv[1:2] == ["panel"] else ""
    if which == "paper":
        t0 = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
        times = [t0 + timedelta(minutes=5 * i) for i in range(12)]
        assert secret("token") == "s3cr3t-value-123", "the declared secret reaches the process"
        print_panel(
            [view("overview", "Overview", [
                stat("Equity", 10432.5, format="money", delta=12.5),
                series("line", times, {"Equity": [10000 + 40 * i for i in range(12)]},
                       format="money"),
                table(
                    [column("sym", "Symbol"), column("pnl", "P&L", "money"),
                     column("act", "", "link")],
                    [{"sym": "AAPL", "pnl": 41.5, "act": link("close", "go")}],
                    title="Positions",
                ),
            ])],
            local_url="http://127.0.0.1:8765/paper",
            health="ok",
        )
    elif which == "oncall":
        if (DATA / "break").exists():
            print("pager is down", file=sys.stderr)
            return 1
        print_panel([view("now", "Now", [
            status([{"name": "checkout", "state": "up"},
                    {"name": "search", "state": "warn", "detail": "slow"}]),
            kv({"open incidents": 0, "on call": "maria"}),
            text("All quiet."),
            details({"remote_url": "x", "owners": ["maria", "justin"]}),
        ])])
    elif which == "chart":
        t0 = datetime(2026, 10, 13, 13, 30, tzinfo=timezone.utc)
        x = [t0 + timedelta(minutes=i) for i in range(30)]
        rows = [(100 + i, 101.5 + i, 99.25 + i, 100.75 + i) for i in range(30)]
        print_panel([view("chart", "Chart", [price(
            x, candles=rows, title="NQZ6, 1-min", unit="pts",
            trades=[trade("long", x[2], 102.25, x[9], 109.5, pnl=145.0, currency="USD",
                          label="jev", why="target"),
                    trade("short", x[25], 125.5)],
            markers=[marker(x[0], 99.25, "up", "ok", label="open")],
        )])])
    else:
        print("{}")
    return 0


raise SystemExit(main())
"""


def files() -> dict[str, str]:
    return {
        "cli.py": CLI,
        "panel.py": Path(sdk_panel.__file__).read_text(),
        "secret.py": Path(sdk_secret.__file__).read_text(),
    }


def chart_decodes(chart: dict[str, Any]) -> bool:
    from resonant.extensions.panels import decode_price

    times, rows = decode_price(chart)
    return len(times) == 30 and rows[0] == (100, 101.5, 99.25, 100.75)


def panels_of(snap: Any) -> dict[str, Any]:
    (ext,) = [e for e in snap.model_dump(mode="json")["extensions"] if e["name"] == "example"]
    return {p["id"]: p for p in ext["panels"]}


async def test_two_panels_draw_at_boot_and_a_broken_one_keeps_its_last_copy(
    tmp_path: Path,
) -> None:
    start = at(8, 7)  # no cron time is near; Tuesday 2026-10-13
    async with panel_harness(tmp_path, None, manifest=MANIFEST, files=files(), at=start) as h:
        sched_with_boot(h, start)
        await h.tick(start + timedelta(seconds=30))
        await h.tick(start + timedelta(seconds=33))
        await h.tick(start + timedelta(seconds=36))
        assert {
            r["panel"] for r in map(json.loads, (a["detail"] for a in h.audits("panel.run")))
        } == {
            "example/paper",
            "example/oncall",
            "example/chart",
        }
        now = start + timedelta(seconds=40)
        snap = build_snapshot(h.db, h.settings, started_at=start, now=now)
        panels = panels_of(snap)
        paper, oncall = panels["paper"], panels["oncall"]
        assert paper["ok"] and oncall["ok"] and not paper["stale"] and not oncall["stale"]
        assert [w["type"] for w in paper["views"][0]["widgets"]] == ["stat", "series", "table"]
        assert [w["type"] for w in oncall["views"][0]["widgets"]] == [
            "status",
            "kv",
            "text",
            "details",
        ]
        table = paper["views"][0]["widgets"][2]
        assert table["rows"] == [["AAPL", 41.5, {"label": "close", "tool": "go"}]]
        assert paper["local_url"] == "http://127.0.0.1:8765/paper" and paper["health"] == "ok"
        (chart,) = panels["chart"]["views"][0]["widgets"]
        assert (chart["type"], chart["mode"], len(chart["dt"]), chart["q"]) == (
            "price",
            "candles",
            30,
            25,
        )
        assert [t.get("exit_t") is None for t in chart["trades"]] == [False, True]
        assert chart_decodes(chart)
        keys = json.dumps(oncall)
        assert '"remote_url":' not in keys  # the details key is a value
        # the remote tier: same panels, no local url, still the views
        remote = to_remote(snap).model_dump(mode="json")
        assert "local_url" not in json.dumps(remote) and "8765" not in json.dumps(remote)
        assert [p["id"] for p in remote["extensions"][0]["panels"]] == ["paper", "oncall", "chart"]
        assert remote["extensions"][0]["panels"][0]["views"] == paper["views"]
        assert {n["id"] for n in remote["diagram"]["nodes"] if n["kind"] == "panel"} == {
            "panel:example/paper",
            "panel:example/oncall",
            "panel:example/chart",
        }
        # break oncall; three scheduled runs fail
        host = h.components.extensions
        assert host is not None
        data = host.data_dir / "example"
        (data / "break").write_text("x")
        health = ExtensionHealth(host)
        for minute in (15, 30, 45):
            await h.tick(at(8, minute))
        st = {s.panel_id: s for s in ext_store.list_panels(h.db, "example")}
        assert st["oncall"].fail_streak == 3 and st["paper"].fail_streak == 0
        assert st["oncall"].last_error is not None and "exit" in st["oncall"].last_error
        assert len(h.audits("panel.failed")) == 3
        late = build_snapshot(h.db, h.settings, started_at=start, now=at(8, 46))
        oncall = panels_of(late)["oncall"]
        assert oncall["stale"] and oncall["ok"] is False and oncall["fail_streak"] == 3
        assert oncall["error"] and "error" in oncall["outside"]
        assert oncall["views"] and oncall["views"][0]["widgets"][2]["text"] == "All quiet."
        result, _fresh = (await health.evaluate(at(8, 46)))["ext:example"] or (None, None)
        assert result is not None and not result.ok
        assert "panel oncall failed 3 times in a row" in result.detail
        # fixed: it heals
        (data / "break").unlink()
        await h.tick(at(9, 0))
        healed = {s.panel_id: s for s in ext_store.list_panels(h.db, "example")}["oncall"]
        assert healed.fail_streak == 0 and healed.last_error is None and healed.last_ok
        result, _ = (await health.evaluate(at(9, 1)))["ext:example"] or (None, None)
        assert result is not None and result.ok

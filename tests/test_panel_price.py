"""The ``price`` widget (PER-282): candles or a line with trades and markers, validated,
aggregated over 1440 points, delta-coded into compact columns, redacted and measured."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from panel_helpers import T0

from resonant.dashboard.snapshot import PanelView, PriceWidget
from resonant.extensions.panels import Normalized, PanelRefused, decode_price, normalize
from resonant.redact import CUT_MARK
from resonant_sdk import panel as p
from resonant_sdk import panel_schema

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "docs" / "panel.schema.json"
RAN = T0 + timedelta(days=1)
GHP = "ghp_" + "a" * 36
RC = "https://claude.ai/code/session_" + "q" * 20
S0 = int(T0.timestamp())

Row = tuple[float, ...] | None


def bars(n: int, *, seed: int = 1, start: float = 25000.0) -> tuple[list[int], list[Row]]:
    """``n`` 1-minute quarter-point candles, like NQ."""
    rng = random.Random(seed)
    x = [S0 + 60 * i for i in range(n)]
    rows: list[Row] = []
    close = start
    for _ in range(n):
        o = close
        c = o + rng.randint(-40, 40) * 0.25
        h = max(o, c) + rng.randint(0, 20) * 0.25
        lo = min(o, c) - rng.randint(0, 20) * 0.25
        rows.append((o, h, lo, c))
        close = c
    return x, rows


def run(raw: Any, *, max_bytes: int = 32768) -> Normalized | PanelRefused:
    return normalize(
        json.loads(json.dumps(raw)), max_bytes=max_bytes, tools=frozenset(), ran_at=RAN
    )


def one(*widgets: dict[str, Any]) -> dict[str, Any]:
    return p.panel([p.view("v", "V", list(widgets))])


def widget(raw: dict[str, Any]) -> dict[str, Any]:
    got = run(raw)
    assert isinstance(got, Normalized), got
    return got.data["views"][0]["widgets"][0]


def refused(raw: dict[str, Any], **kw: Any) -> str:
    got = run(raw, **kw)
    assert isinstance(got, PanelRefused), got
    assert got.error.startswith("refused: ") and len(got.error) <= 200
    return got.error


def close_rows(a: list[Row], b: list[Row]) -> bool:
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b, strict=True):
        if (ra is None) != (rb is None):
            return False
        if (
            ra is not None
            and rb is not None
            and any(abs(x - y) > 1e-9 for x, y in zip(ra, rb, strict=True))
        ):
            return False
    return True


def test_candles_are_delta_coded_in_ticks_and_decode_exactly() -> None:
    x, rows = bars(390)
    w = widget(one(p.price(x, candles=rows, title="NQZ6, 1-min", unit="pts")))
    assert (w["type"], w["mode"], w["title"], w["unit"], w["format"]) == (
        "price",
        "candles",
        "NQZ6, 1-min",
        "pts",
        "number",
    )
    assert w["t0"] == S0 and w["dt"][0] == 0 and set(w["dt"][1:]) == {60}
    assert (w["dp"], w["q"]) == (2, 25)  # quarter points
    assert w["o"][0] == 100000 and w["o"][1:5] == [0, 0, 0, 0]  # opens at the last close
    assert all(v >= 0 for v in w["h"] + w["l"])
    assert "aggregated_from" not in w and w["trades"] == [] and w["markers"] == []
    times, got = decode_price(w)
    assert times == x and close_rows(got, rows)
    PriceWidget.model_validate(w)


def test_a_line_carries_closes_only_and_decodes() -> None:
    x = [S0, S0 + 60, S0 + 120, S0 + 180]
    w = widget(one(p.price(x, line=[1.5, None, 1.75, 0.1])))
    assert w["mode"] == "line" and "o" not in w and "h" not in w and "l" not in w
    assert w["c"][1] is None
    times, got = decode_price(w)
    assert times == x
    assert close_rows(got, [(1.5,) * 4, None, (1.75,) * 4, (0.1,) * 4])


def test_iso_times_and_rows_with_gaps() -> None:
    t = [T0 + timedelta(minutes=i) for i in range(3)]
    w = widget(one(p.price(t, candles=[(10, 12, 9, 11), None, (11, 11, 10, 10)])))
    assert w["t0"] == S0 and w["dt"] == [0, 60, 60]
    assert w["o"] == [10, None, 0] and w["c"] == [1, None, -1]  # the gap keeps the last close
    assert close_rows(decode_price(w)[1], [(10, 12, 9, 11), None, (11, 11, 10, 10)])


def test_an_empty_chart_is_allowed() -> None:
    w = widget(one(p.price([], line=[])))
    assert (w["t0"], w["dt"], w["c"], w["dp"], w["q"]) == (0, [], [], 0, 1)
    assert decode_price(w) == ([], [])


def test_over_1440_points_candles_aggregate_never_average() -> None:
    x, rows = bars(3000)
    w = widget(one(p.price(x, candles=rows)))
    assert w["aggregated_from"] == 3000
    times, got = decode_price(w)
    k = 3  # ceil(3000 / 1440)
    assert len(times) == 1000 and times == x[::k]
    for j, agg in enumerate(got):
        chunk = [r for r in rows[j * k : (j + 1) * k] if r is not None]
        assert agg is not None
        assert agg[0] == chunk[0][0] and agg[3] == chunk[-1][3]
        assert agg[1] == max(r[1] for r in chunk) and agg[2] == min(r[2] for r in chunk)


def test_aggregating_a_line_keeps_the_last_close_and_gap_buckets_stay_gaps() -> None:
    n = 1441
    x = [S0 + 60 * i for i in range(n)]
    line: list[float | None] = [float(i) for i in range(n)]
    line[2] = line[3] = None  # bucket [2, 3] is all gaps
    w = widget(one(p.price(x, line=line)))
    assert w["aggregated_from"] == n
    _, got = decode_price(w)
    assert got[0] == (1.0,) * 4 and got[1] is None and got[2] == (5.0,) * 4


def test_a_day_of_one_minute_candles_with_trades_fits_the_cap() -> None:
    x, rows = bars(1440)
    trades = [
        p.trade("long", x[i], 25000.25, x[i + 5], 25004.0, pnl=75.0, currency="USD", label="jev")
        for i in range(0, 400, 20)
    ]
    got = run(one(p.price(x, candles=rows, trades=trades, title="NQZ6, 1-min")))
    assert isinstance(got, Normalized), got
    assert got.bytes < 26000, got.bytes


def test_ohlc_is_checked() -> None:
    x = [S0, S0 + 60]
    assert "candle 2 has h < max(o, c)" in refused(
        one(p.price(x, candles=[(1, 2, 0, 1), (1, 1.5, 0.5, 2)]))
    )
    assert "candle 1 has h < max(o, c) or l > min(o, c)" in refused(
        one(p.price(x, candles=[(1, 2, 1.5, 1.2), (1, 1, 1, 1)]))
    )
    bad = {"o": [1, 1], "h": [1, None], "l": [1, 1], "c": [1, 1]}
    assert "candle 2 is partly null" in refused(one(p.price(x, candles=bad)))
    short = {"o": [1], "h": [1, 1], "l": [1, 1], "c": [1, 1]}
    assert "candles.o has 1 values for 2 x" in refused(one(p.price(x, candles=short)))
    assert "line has 1 values for 2 x" in refused(one(p.price(x, line=[1])))


def test_structure_refusals_name_the_widget() -> None:
    x = [S0, S0 + 60]
    err = refused(one(p.price(x)))
    assert err.startswith("refused: view v widget 1 (price): give exactly one of candles or line")
    assert "exactly one" in refused(one(p.price(x, line=[1, 2], candles=[(1, 1, 1, 1)] * 2)))
    assert "x must strictly increase" in refused(one(p.price([S0, S0], line=[1, 2])))
    assert "x must strictly increase" in refused(one(p.price([S0 + 60, S0], line=[1, 2])))
    assert "offset" in refused(one(p.price(["2026-10-16T09:00:00"], line=[1])))
    many = [S0 + i for i in range(panel_schema.PRICE_POINTS_MAX + 1)]
    assert "20161 points (max 20160)" in refused(one(p.price(many, line=[1] * len(many))))
    assert "price out of range" in refused(one(p.price([S0], line=[1e300])))
    assert "line.0" in refused(one(p.price([S0], line=[True])))  # a bool is not a price


def test_trade_and_marker_caps() -> None:
    x = [S0, S0 + 60]
    trades = [p.trade("long", S0, 1) for _ in range(panel_schema.TRADES_MAX + 1)]
    assert "101 trades (max 100)" in refused(one(p.price(x, line=[1, 2], trades=trades)))
    markers = [p.marker(S0, 1) for _ in range(panel_schema.MARKERS_MAX + 1)]
    assert "201 markers (max 200)" in refused(one(p.price(x, line=[1, 2], markers=markers)))
    exactly = one(p.price(x, line=[1, 2], trades=trades[:100], markers=markers[:200]))
    w = widget(exactly)
    assert len(w["trades"]) == 100 and len(w["markers"]) == 200


def test_open_and_closed_trades() -> None:
    x = [S0, S0 + 60, S0 + 120]
    t1 = T0 + timedelta(minutes=1)
    trades = [
        p.trade("long", T0, 10.25, t1, 11.0, pnl=15.0, currency="USD", label="jev", why="target"),
        p.trade("short", S0 + 120, 11.0),  # open
    ]
    w = widget(one(p.price(x, line=[10, 11, 11], trades=trades)))
    closed, still = w["trades"]
    assert closed == {
        "side": "long",
        "entry_t": S0,
        "entry_px": 10.25,
        "exit_t": S0 + 60,
        "exit_px": 11.0,
        "pnl": 15.0,
        "currency": "USD",
        "label": "jev",
        "why": "target",
    }
    assert still == {"side": "short", "entry_t": S0 + 120, "entry_px": 11.0}  # sparse
    half = {"side": "long", "entry_t": S0, "entry_px": 1, "exit_t": S0 + 60}
    assert "trade 1: exit_t and exit_px" in refused(one(p.price(x, line=[1, 1, 1], trades=[half])))
    back = p.trade("long", S0 + 60, 1, S0, 2)
    assert "trade 1: exit before entry" in refused(one(p.price(x, line=[1, 1, 1], trades=[back])))
    assert "side" in refused(one(p.price(x, line=[1, 1, 1], trades=[p.trade("up", S0, 1)])))


def test_markers_default_and_convert_times() -> None:
    w = widget(
        one(p.price([S0], line=[1], markers=[p.marker(T0, 1.5), p.marker(S0, 2, "up", "ok")]))
    )
    assert w["markers"] == [
        {"t": S0, "price": 1.5, "kind": "dot", "tone": "neutral"},
        {"t": S0, "price": 2, "kind": "up", "tone": "ok"},
    ]
    assert "kind" in refused(one(p.price([S0], line=[1], markers=[p.marker(S0, 1, "left")])))


def test_labels_and_why_are_cut_and_redacted() -> None:
    bad = f"{GHP} {RC}"
    raw = one(
        p.price(
            [S0],
            line=[1],
            title=bad,
            unit=bad,
            trades=[p.trade("long", S0, 1, label=bad, why=bad)],
            markers=[p.marker(S0, 1, label=bad)],
        )
    )
    text = json.dumps(widget(raw))
    assert GHP not in text and "claude.ai/code" not in text and "session_qqq" not in text
    w = widget(
        one(p.price([S0], line=[1], trades=[p.trade("long", S0, 1, label="L" * 99, why="W" * 99)]))
    )
    trade = w["trades"][0]
    assert len(trade["label"]) == 40 and trade["label"].endswith(CUT_MARK)
    assert len(trade["why"]) == 24 and trade["why"].endswith(CUT_MARK)
    m = widget(one(p.price([S0], line=[1], markers=[p.marker(S0, 1, label="M" * 99)])))
    assert len(m["markers"][0]["label"]) == 40


def test_the_size_limit_applies() -> None:
    x, rows = bars(1440)
    err = refused(one(p.price(x, candles=rows)), max_bytes=4096)
    assert "bytes after normalising (max 4096)" in err


def test_it_validates_into_the_snapshot_view_and_is_compact() -> None:
    x, rows = bars(10)
    got = run(one(p.price(x, candles=rows), p.stat("Last", 1)))
    assert isinstance(got, Normalized)
    view = PanelView.model_validate(got.data["views"][0])
    assert [w.type for w in view.widgets] == ["price", "stat"]
    dumped = view.model_dump(mode="json")["widgets"][0]
    assert "id" not in dumped and "currency" not in dumped  # unset optionals are left out


def test_the_helpers_build_the_documented_shapes() -> None:
    when = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)
    assert p.price([when], line=[1.0], unit="pts") == {
        "type": "price",
        "x": ["2026-11-03T14:30:00+00:00"],
        "line": [1.0],
        "unit": "pts",
    }
    assert p.price([1, 2], candles={"o": [1, 2], "h": [2, 3], "l": [0, 1], "c": [2, 3]})[
        "candles"
    ] == {"o": [1, 2], "h": [2, 3], "l": [0, 1], "c": [2, 3]}
    assert p.price([1, 2], candles=[(1, 2, 0, 2), None])["candles"] == {
        "o": [1, None],
        "h": [2, None],
        "l": [0, None],
        "c": [2, None],
    }
    assert p.trade("long", when, 1.5) == {
        "side": "long",
        "entry_t": "2026-11-03T14:30:00+00:00",
        "entry_px": 1.5,
    }
    assert p.trade("short", 1, 2.0, 3, 1.0, pnl=20.0, why="stop")["why"] == "stop"
    assert p.marker(1, 2.0) == {"t": 1, "price": 2.0, "kind": "dot", "tone": "neutral"}
    assert p.marker(1, 2.0, "up", "ok", label="x")["label"] == "x"
    full = p.price([1], line=[1], trades=[p.trade("long", 1, 1)], markers=[p.marker(1, 1)])
    assert len(full["trades"]) == 1 and len(full["markers"]) == 1


def test_the_schema_documents_the_price_caps() -> None:
    schema = json.loads(SCHEMA_FILE.read_text())
    price = schema["$defs"]["Price"]["properties"]
    assert price["x"]["maxItems"] == 20160
    assert price["trades"]["maxItems"] == 100 and price["markers"]["maxItems"] == 200
    trade = schema["$defs"]["Trade"]["properties"]
    assert trade["label"]["anyOf"][0]["x-cut-at"] == 40
    assert trade["why"]["anyOf"][0]["x-cut-at"] == 24


@pytest.mark.parametrize("value", [0.1, 0.3, 1e-8, 123456.789])
def test_awkward_decimals_round_trip(value: float) -> None:
    w = widget(one(p.price([S0, S0 + 60], line=[value, value * 2])))
    _, got = decode_price(w)
    assert got[0] is not None and got[1] is not None
    assert abs(got[0][3] - value) < 1e-12 and abs(got[1][3] - value * 2) < 1e-9


# --- the dashboard's fixture -----------------------------------------------------------------

UI_PRICE = (
    Path(__file__).resolve().parents[1] / "dashboard" / "src" / "test" / "fixtures" / "price.json"
)
F0 = 1792141200  # 2026-10-16T13:00:00Z


def ui_price_fixture() -> dict[str, Any]:
    """The price widgets the dashboard's tests draw: 120 candles with a gap, a winning long, a
    losing short and an open long, two markers; and the same closes as a line."""
    _, rows = bars(120, seed=7)
    t = [F0 + 60 * i for i in range(120)]
    closes = [cast(tuple[float, ...], r)[3] for r in rows]

    def px(i: int) -> float:
        return closes[i]

    rows[40] = rows[41] = None

    trades = [
        p.trade(
            "long",
            t[10],
            px(10),
            t[25],
            px(10) + 7.25,
            pnl=145.0,
            currency="USD",
            label="jev",
            why="target",
        ),
        p.trade(
            "short",
            t[60],
            px(60),
            t[70],
            px(60) + 3.5,
            pnl=-70.0,
            currency="USD",
            label="jev",
            why="stop",
        ),
        p.trade("long", t[110], px(110), label="jev"),
    ]
    markers = [p.marker(t[5], px(5), "up", "ok", label="breakout"), p.marker(t[90], px(90))]
    raw = p.panel(
        [
            p.view(
                "chart",
                "Chart",
                [
                    p.price(
                        t,
                        candles=rows,
                        title="NQZ6, 1-min",
                        unit="pts",
                        trades=trades,
                        markers=markers,
                        id="nq",
                    ),
                    p.price(
                        t,
                        line=[None if r is None else c for r, c in zip(rows, closes, strict=True)],
                        title="NQZ6 close",
                        id="nq-line",
                    ),
                ],
            )
        ]
    )
    got = run(raw)
    assert isinstance(got, Normalized), got
    candles, line = got.data["views"][0]["widgets"]
    return {"candles": candles, "line": line}


def test_the_dashboard_price_fixture_is_the_normaliser_output() -> None:
    want = ui_price_fixture()
    assert json.loads(UI_PRICE.read_text()) == want, (
        "dashboard/src/test/fixtures/price.json is stale: run "
        "`uv run python tests/test_panel_price.py > dashboard/src/test/fixtures/price.json`"
    )
    for w in want.values():
        PriceWidget.model_validate(w)


if __name__ == "__main__":
    print(json.dumps(ui_price_fixture(), indent=2))

"""``extensions.panels.normalize`` (PER-280): a panel command's output, validated, cut,
redacted and flattened into the snapshot's own models."""

from __future__ import annotations

import copy
import json
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest
from panel_helpers import T0, normalized, raw_panel

from resonant.dashboard.snapshot import PanelView
from resonant.extensions.panels import Normalized, PanelRefused, flatten, normalize
from resonant.redact import CUT_MARK
from resonant_sdk import panel as p
from resonant_sdk import panel_schema

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "docs" / "panel.schema.json"
RAN = T0 + timedelta(days=1)
GHP = "ghp_" + "a" * 36
RC = "https://claude.ai/code/session_" + "q" * 20


def run(raw: Any, *, max_bytes: int = 32768, tools: frozenset[str] = frozenset({"go"})) -> Any:
    return normalize(raw, max_bytes=max_bytes, tools=tools, ran_at=RAN)


def one(*widgets: dict[str, Any], **top: Any) -> dict[str, Any]:
    return p.panel([p.view("v", "V", list(widgets))], **top)


def refused(raw: Any, **kw: Any) -> str:
    got = run(raw, **kw)
    assert isinstance(got, PanelRefused), got
    assert got.error.startswith("refused: ") and len(got.error) <= 200
    return got.error


def widget(raw: dict[str, Any], i: int = 0) -> dict[str, Any]:
    got = run(raw)
    assert isinstance(got, Normalized), got
    return got.data["views"][0]["widgets"][i]


def test_every_widget_happy_path_and_the_result_is_snapshot_models() -> None:
    got = normalized(raw_panel())
    assert got.data["health"] == "ok" and got.data["local_url"] == "http://127.0.0.1:8765/ledger"
    assert got.updated_at == T0 + timedelta(minutes=5 * 240)
    kinds = [w["type"] for v in got.data["views"] for w in v["widgets"]]
    assert kinds == ["stat", "stat", "series", "table", "status", "kv", "text", "details"]
    [PanelView.model_validate(v) for v in got.data["views"]]
    assert got.bytes == len(json.dumps(got.data["views"], separators=(",", ":")))


def test_stat_defaults_a_format_and_refuses_a_bool() -> None:
    w = widget(one(p.stat("A", 1)))
    assert w["format"] == "number" and w["value"] == 1 and "unit" not in w
    assert widget(one(p.stat("A", None)))["value"] is None
    assert widget(one(p.stat("A", "up")))["value"] == "up"
    assert "value" in refused(one(p.stat("A", True)))
    assert "value" in refused(one(p.stat("A", float("nan"))))


def test_series_is_checked_and_times_become_unix_seconds() -> None:
    at = [T0, T0 + timedelta(hours=1)]
    w = widget(one(p.series("bar", at, {"A": [1, None]}, stacked=True)))
    assert w["x"] == [int(T0.timestamp()), int(T0.timestamp()) + 3600] and w["x_type"] == "time"
    assert w["series"] == [{"name": "A", "values": [1, None]}] and w["stacked"] is True
    c = widget(one(p.series("line", ["a", "b"], {"A": [1, 2]}, x_type="category")))
    assert c["x"] == ["a", "b"] and c["x_type"] == "category"
    num = widget(one(p.series("line", [10, 20.5], {"A": [1, 2]})))
    assert num["x"] == [10, 20.5]


def test_series_refusals_name_the_widget() -> None:
    lines = {f"s{i}": [1, 2] for i in range(5)}
    assert refused(one(p.series("line", [1, 2], lines))) == (
        "refused: view v widget 1 (series): 5 series (max 4)"
    )
    assert "2401 points (max 2400)" in refused(
        one(p.series("line", list(range(2401)), {"A": [0] * 2401}))
    )
    assert "241 categories (max 240)" in refused(
        one(p.series("line", [str(i) for i in range(241)], {"A": [0] * 241}, x_type="category"))
    )
    assert "time x must ascend" in refused(one(p.series("line", [2, 1], {"A": [1, 2]})))
    assert "has 1 values for 2 x" in refused(one(p.series("line", [1, 2], {"A": [1]})))
    assert "no series" in refused(one(p.series("line", [1], {})))
    assert "not an ISO-8601" in refused(
        one(p.series("line", ["yesterday", "today"], {"A": [1, 2]}))
    )
    assert "needs an offset" in refused(one(p.series("line", ["2026-01-01T00:00:00"], {"A": [1]})))
    assert "category x must be strings" in refused(
        one(p.series("line", [1, 2], {"A": [1, 2]}, x_type="category"))
    )


def test_a_long_time_axis_is_downsampled_keeping_the_ends_and_the_bucket_means() -> None:
    n = 600
    xs = list(range(1000, 1000 + n))
    ys = [float(i) for i in range(n)]
    gaps: list[float | None] = [None if i % 2 else float(i) for i in range(n)]
    w = widget(one(p.series("line", xs, {"A": ys, "B": gaps})))
    assert w["downsampled_from"] == 600 and len(w["x"]) == 240
    a, b = w["series"][0]["values"], w["series"][1]["values"]
    assert w["x"][0] == 1000 and w["x"][-1] == 1000 + n - 1 and a[0] == 0 and a[-1] == n - 1
    assert len(a) == len(b) == 240
    # 598 middle points in 238 buckets: bucket k covers [1 + k*598//238, 1 + (k+1)*598//238)
    for k in (0, 1, 100, 237):
        lo, hi = 1 + k * 598 // 238, 1 + (k + 1) * 598 // 238
        assert w["x"][1 + k] == 1000 + lo
        assert a[1 + k] == pytest.approx(sum(ys[lo:hi]) / (hi - lo))
        got = [v for v in gaps[lo:hi] if v is not None]
        if got:
            assert b[1 + k] == pytest.approx(sum(got) / len(got))
        else:
            assert b[1 + k] is None
    assert "downsampled_from" not in widget(one(p.series("line", xs[:240], {"A": ys[:240]})))
    allnull = widget(one(p.series("line", xs, {"A": [None] * n})))
    assert set(allnull["series"][0]["values"]) == {None}


def test_a_table_is_aligned_cut_and_typed() -> None:
    cols = [
        p.column("a", "A"),
        p.column("n", "N", "number"),
        p.column("t", "T", "time"),
        p.column("s", "S", "pill"),
        p.column("l", "L", "link"),
    ]
    rows = [
        {"a": "x", "n": 1.5, "t": T0, "s": p.pill("ok", "ok"), "l": p.link("go", "go")},
        {"a": None, "s": "plain", "l": p.link("elsewhere", "no_such_tool")},
    ]
    w = widget(one(p.table(cols, rows)))
    assert w["rows"][0] == [
        "x",
        1.5,
        int(T0.timestamp()),
        {"label": "ok", "tone": "ok"},
        {"label": "go", "tool": "go"},
    ]
    # missing keys are null, a bare string is a neutral pill, a link to no tool is text
    assert w["rows"][1] == [None, None, None, {"label": "plain", "tone": "neutral"}, "elsewhere"]
    assert w["truncated"] is False and "total_rows" not in w


def test_a_table_over_fifty_rows_keeps_fifty_and_says_so() -> None:
    cols = [p.column("a", "A", "number")]
    w = widget(one(p.table(cols, [{"a": i} for i in range(80)])))
    assert len(w["rows"]) == 50 and w["truncated"] is True and w["total_rows"] == 80
    got = run(one(p.table(cols, [{"a": i} for i in range(80)])))
    assert got.truncated is True and got.data["truncated"] is True


def test_table_refusals() -> None:
    cols9 = [p.column(f"c{i}", "C") for i in range(9)]
    assert "9 columns (max 8)" in refused(one(p.table(cols9, [])))
    assert "no columns" in refused(one(p.table([], [])))
    assert "unique" in refused(one(p.table([p.column("a", "A"), p.column("a", "B")], [])))
    num = [p.column("a", "A", "number")]
    assert "row 1 column a is not a number cell" in refused(one(p.table(num, [{"a": "high"}])))
    assert "not a text cell" in refused(one(p.table([p.column("a", "A")], [{"a": 3}])))
    assert "not a pill cell" in refused(one(p.table([p.column("a", "A", "pill")], [{"a": 3}])))
    assert "not a link cell" in refused(one(p.table([p.column("a", "A", "link")], [{"a": "x"}])))
    assert "unknown key" in refused(one(p.table(num, [{"a": 1, "zzz": 2}])))
    assert "a" in refused(one(p.table(num, [{"a": True}])))


def test_status_and_kv_are_cut_and_since_is_a_time() -> None:
    items = [{"name": f"s{i}", "state": "up", "since": T0} for i in range(60)]
    w = widget(one(p.status(items)))
    assert len(w["items"]) == 50 and w["truncated"] is True
    assert (
        w["items"][0]["since"] == T0.isoformat().replace("+00:00", "Z")
        or w["items"][0]["since"] == T0.isoformat()
    )
    kv = widget(one(p.kv({f"k{i}": i for i in range(45)})))
    assert len(kv["items"]) == 30 and kv["truncated"] is True
    mixed = widget(one(p.kv([{"key": "a", "value": True}, {"key": "b", "value": None}])))
    assert [i["value"] for i in mixed["items"]] == [True, None]


def test_details_flatten_to_rows_whose_keys_become_values() -> None:
    rows, cut = flatten({"remote_url": "x", "list": [1, {"deep": None}], "ok": True})
    assert cut is False
    shape = [(r["depth"], r["key"], r["kind"], r["value"]) for r in rows]
    assert shape == [
        (0, "", "object", None),
        (1, "remote_url", "string", "x"),
        (1, "list", "array", None),
        (2, "[0]", "number", "1"),
        (2, "[1]", "object", None),
        (3, "deep", "null", None),
        (1, "ok", "bool", "true"),
    ]
    assert flatten("s")[0] == [{"depth": 0, "key": "", "kind": "string", "value": "s"}]
    w = widget(one(p.details({"remote_url": "https://example.com"})))
    assert w["rows"][1]["key"] == "remote_url"
    assert '"remote_url":' not in json.dumps(w)  # the key is a value, never a JSON key


def test_details_depth_and_row_caps() -> None:
    deep: Any = "leaf"
    for _ in range(8):
        deep = {"k": deep}
    rows, _ = flatten(deep)
    assert max(r["depth"] for r in rows) == 5
    assert rows[-1] == {"depth": 5, "key": "k", "kind": "string", "value": "…"}
    big = [{"i": i} for i in range(300)]
    rows, cut = flatten(big)
    assert len(rows) == 200 and cut is True
    w = widget(one(p.details(big)))
    assert w["truncated"] is True and len(w["rows"]) == 200 and w["collapsed"] is True


def test_the_output_is_refused_when_the_structure_is_wrong() -> None:
    good = one(p.text("hi"))
    assert refused("nope") == "refused: output is not a JSON object"
    assert refused({**good, "schema": 2}) == "refused: schema 2 (want 1)"
    assert "schema" in refused({k: v for k, v in good.items() if k != "schema"})
    assert "actions" in refused({**good, "actions": [{"id": "x"}]})
    assert run({**good, "actions": []}).__class__ is Normalized
    assert "extra_forbidden" in refused({**good, "bogus": 1})
    assert "views" in refused({"schema": 1, "views": []})
    assert "views" in refused(
        {"schema": 1, "views": [p.view(f"v{i}", "V", [p.text("t")]) for i in range(7)]}
    )
    bad_widget = {"type": "chart", "id": "x"}
    assert "union_tag_invalid" in refused(one(bad_widget))
    assert "extra_forbidden" in refused(one({**p.text("hi"), "html": "<b>"}))
    assert "health" in refused({**good, "health": "great"})
    many = [p.view(f"v{i}", "V", [p.text("t")] * 24) for i in range(3)]
    assert "72 widgets (max 48)" in refused({"schema": 1, "views": many})
    assert "widgets" in refused(one())


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:80/",
        "http://10.0.0.1:80/",
        "http://localhost:80/",
        "http://127.0.0.1:0/",
        "http://127.0.0.1:65536/",
        "http://127.0.0.1/",
        "http://127.0.0.1:80/ a",
        "http://user@127.0.0.1:80/",
        "http://127.0.0.1:80/" + "x" * 300,
    ],
)
def test_a_local_url_must_be_loopback_http_with_a_port(url: str) -> None:
    assert "local_url" in refused(one(p.text("hi"), local_url=url))


def test_a_local_url_is_kept() -> None:
    for url in ("http://127.0.0.1:1", "http://127.0.0.1:65535/a/b?c=d"):
        assert run(one(p.text("hi"), local_url=url)).data["local_url"] == url


def test_the_size_limit_is_checked_after_normalising() -> None:
    raw = raw_panel()
    size = normalized(raw).bytes
    assert refused(raw, max_bytes=1024).startswith("refused: panel is ")
    assert f"panel is {size} bytes after normalising (max 1024)" in refused(raw, max_bytes=1024)
    assert isinstance(run(raw, max_bytes=size), Normalized)
    assert isinstance(run(raw, max_bytes=size - 1), PanelRefused)
    huge = one(*[p.text("x" * 800)] * 24, **{})
    # a max_bytes above the 32 KiB hard cap is still held to it
    assert "(max 32768)" in refused({**huge, "views": huge["views"] * 2}, max_bytes=10**9)


def test_strings_are_cut_never_refused() -> None:
    w = widget(one(p.stat("L" * 90, "v" * 90, unit="u" * 40, hint="h" * 200, title="t" * 200)))
    assert len(w["label"]) == 40 and w["label"].endswith(CUT_MARK)
    assert len(w["value"]) == 40 and len(w["unit"]) == 16 and len(w["hint"]) == 80
    assert len(w["title"]) == 64
    t = widget(one(p.text("x" * 5000)))
    assert len(t["text"]) == 800 and t["text"].endswith(CUT_MARK)
    cell = widget(one(p.table([p.column("a", "A" * 99)], [{"a": "c" * 500}])))
    assert len(cell["rows"][0][0]) == 120 and len(cell["columns"][0]["label"]) == 32


def test_secrets_and_remote_control_links_are_redacted_in_every_widget_field() -> None:
    bad = f"{GHP} {RC}"
    raw = p.panel(
        [
            p.view(
                "main",
                "T " + bad,
                [
                    p.stat(bad, bad, unit=bad, hint=bad, title=bad),
                    p.series("line", ["a " + bad], {bad: [1]}, x_type="category", title=bad),
                    p.table(
                        [
                            p.column("a", bad),
                            p.column("b", "B", "pill"),
                            p.column("c", "C", "link"),
                        ],
                        [
                            {
                                "a": bad,
                                "b": p.pill(bad, "ok"),
                                "c": p.link(bad, "go"),
                            }
                        ],
                        title=bad,
                    ),
                    p.status([{"name": bad, "state": "up", "detail": bad}], title=bad),
                    p.kv([{"key": bad, "value": bad, "unit": bad}], title=bad),
                    p.text(bad, title=bad),
                    p.details({bad: bad, "list": [bad]}, title=bad),
                ],
            )
        ]
    )
    got = normalized(raw)
    text = json.dumps(got.data)
    assert GHP not in text and "claude.ai/code" not in text and "session_qqq" not in text
    assert "ghp_" not in text.replace("[redacted", "")  # nothing credential-shaped is left


def test_a_future_updated_at_is_clamped_and_a_missing_one_is_the_run_end() -> None:
    future = (RAN + timedelta(days=3)).isoformat()
    assert run(one(p.text("hi"), updated_at=None)).updated_at == RAN
    got = run({**one(p.text("hi")), "updated_at": future})
    assert got.updated_at == RAN
    past = (RAN - timedelta(hours=2)).isoformat()
    assert run({**one(p.text("hi")), "updated_at": past}).updated_at == RAN - timedelta(hours=2)
    assert run({**one(p.text("hi")), "updated_at": "2026-10-17T09:00:00Z"}).updated_at == RAN
    assert "updated_at" in refused({**one(p.text("hi")), "updated_at": "2026-10-17"})
    assert "updated_at" in refused({**one(p.text("hi")), "updated_at": "2026-10-17T09:00:00"})
    assert run(one(p.text("hi"))).updated_at.tzinfo == UTC


def test_normalising_does_not_change_its_input() -> None:
    raw = json.loads(json.dumps(raw_panel()))
    before = copy.deepcopy(raw)
    run(raw)
    assert raw == before


def test_errors_never_echo_a_secret_value() -> None:
    err = refused(one({"type": "stat", "label": "x", "value": GHP, "bogus": GHP}))
    assert GHP not in err
    err = refused({"schema": 1, GHP: 1, "views": []})
    assert GHP not in err


def test_the_panel_schema_file_is_current_and_documents_the_caps() -> None:
    assert SCHEMA_FILE.read_text() == panel_schema.schema_json(), (
        "docs/panel.schema.json is stale: run "
        "`uv run python -m resonant_sdk.panel_schema > docs/panel.schema.json`"
    )
    schema = json.loads(SCHEMA_FILE.read_text())
    stat = schema["$defs"]["Stat"]["properties"]
    assert stat["label"]["x-cut-at"] == 40 and stat["hint"]["anyOf"][0]["x-cut-at"] == 80
    assert schema["$defs"]["Series"]["properties"]["series"]["maxItems"] == 4
    assert schema["properties"]["actions"]["maxItems"] == 0

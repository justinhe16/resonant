"""The output contract of a panel command (PER-280): one JSON object on stdout, ``schema: 1``.

An extension declares ``panels:`` in its manifest; Resonant runs each panel's command on its
schedule, validates what it prints against these models, normalises it (cuts, downsamples,
redacts, flattens ``details``) and draws it on the dashboard. The extension sends data, never
code or markup: every string is plain text.

Two rules apply to every field. A string over its cap is *cut* by core (the cap is the schema's
``x-cut-at``), never refused. Structure is checked strictly: an unknown key or type, a wrong
shape or a count over its limit refuses the whole run (the last good copy stays on the page,
with the error). The limits that core enforces itself, with a message that names the widget,
are listed in the schema as ``maxItems`` and in ``docs/extensions.md`` (6a).

``docs/panel.schema.json`` is generated from :class:`PanelOutput`:
``uv run python -m resonant_sdk.panel_schema > docs/panel.schema.json``. A test fails when it
is stale. Extensions can't import this module at runtime (they run as ``python -I -S``); they
build the same JSON with the stdlib-only helpers in ``resonant_sdk/panel.py``.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from resonant_sdk.tool import TOOL_NAME_PATTERN

PANEL_SCHEMA_VERSION = 1
VIEWS_MAX = 6
WIDGETS_PER_VIEW_MAX = 24
WIDGETS_MAX = 48
SERIES_MAX = 4
SERIES_POINTS_MAX = 2400  # more is refused
SERIES_POINTS_KEPT = 240  # a time series over this is downsampled to it
COLUMNS_MAX = 8
ROWS_MAX = 50
STATUS_ITEMS_MAX = 50
KV_ITEMS_MAX = 30
DETAIL_ROWS_MAX = 200
DETAIL_DEPTH_MAX = 5
PRICE_POINTS_MAX = 20160  # two weeks of 1-minute bars; more is refused (PER-282)
PRICE_POINTS_KEPT = 1440  # a day of 1-minute bars; more is aggregated into wider candles
TRADES_MAX = 100
MARKERS_MAX = 200

SLUG = r"^[a-z0-9][a-z0-9_-]{0,31}$"
LOCAL_URL = r"^http://127\.0\.0\.1:[0-9]{1,5}(/[!-~]*)?$"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# A string core cuts to N characters (``x-cut-at``); never refused for length.
Cut16 = Annotated[str, Field(json_schema_extra={"x-cut-at": 16})]
Cut24 = Annotated[str, Field(json_schema_extra={"x-cut-at": 24})]
Cut32 = Annotated[str, Field(json_schema_extra={"x-cut-at": 32})]
Cut40 = Annotated[str, Field(json_schema_extra={"x-cut-at": 40})]
Cut64 = Annotated[str, Field(json_schema_extra={"x-cut-at": 64})]
Cut80 = Annotated[str, Field(json_schema_extra={"x-cut-at": 80})]
Cut120 = Annotated[str, Field(json_schema_extra={"x-cut-at": 120})]
Cut800 = Annotated[str, Field(json_schema_extra={"x-cut-at": 800})]


def _max_items(n: int) -> Any:
    return Field(json_schema_extra={"maxItems": n})


Slug = Annotated[str, StringConstraints(pattern=SLUG)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
# A bool is not a number here; NaN and infinity are not numbers in JSON.
Num = StrictInt | Annotated[float, Field(strict=True, allow_inf_nan=False)]
Format = Literal["number", "integer", "money", "percent", "duration", "bytes"]
Tone = Literal["ok", "warn", "bad"]
PillTone = Literal["ok", "warn", "bad", "neutral"]
# ISO-8601 with an offset (a string) or unix seconds (a number).
Time = Num | Annotated[str, Field(json_schema_extra={"format": "date-time"})]


class Stat(_In):
    type: Literal["stat"]
    id: Slug | None = None
    title: Cut64 | None = None
    label: Cut40
    value: Num | Cut40 | None
    unit: Cut16 | None = None
    format: Format | None = None
    currency: Currency | None = None
    delta: Num | None = None
    delta_format: Format | None = None
    health: Tone | None = None
    hint: Cut80 | None = None


class SeriesX(_In):
    """``time``: values ascending (ISO-8601 with offset or unix seconds). ``category``: strings,
    cut to 24 characters, at most 240."""

    type: Literal["time", "category"]
    values: list[Time]


class SeriesLine(_In):
    name: Cut32
    values: list[Num | None]


class Series(_In):
    """At most 4 lines of the same length as ``x.values``; at most 2400 points (a time axis over
    240 is downsampled to 240)."""

    type: Literal["series"]
    id: Slug | None = None
    title: Cut64 | None = None
    kind: Literal["line", "area", "bar"]
    x: SeriesX
    series: Annotated[list[SeriesLine], _max_items(SERIES_MAX)]
    unit: Cut16 | None = None
    format: Format | None = None
    currency: Currency | None = None
    stacked: bool = False


class Column(_In):
    key: Slug
    label: Cut32
    type: Literal["text", "number", "money", "time", "pill", "link"] = "text"
    align: Literal["left", "right"] | None = None
    currency: Currency | None = None


class Pill(_In):
    label: Cut24
    tone: PillTone


class Link(_In):
    """A chip that names a tool of this extension (shown as ``ext/tool``); it runs nothing."""

    label: Cut64
    tool: Annotated[str, StringConstraints(pattern=TOOL_NAME_PATTERN)]


# By column type: text = string (cut 120); number, money = number or null; time = Time or null;
# pill = Pill or a string (tone neutral); link = Link. A wrong-typed cell is refused.
Cell = Pill | Link | Num | str | None


class Table(_In):
    """At most 8 columns; the first 50 rows are kept. A row is ``{column key: cell}``; a
    missing key is null, an unknown key is refused."""

    type: Literal["table"]
    id: Slug | None = None
    title: Cut64 | None = None
    columns: Annotated[list[Column], _max_items(COLUMNS_MAX)]
    rows: list[dict[str, Cell]]


class StatusItem(_In):
    name: Cut64
    state: Literal["up", "down", "warn", "unknown"]
    detail: Cut120 | None = None
    since: Time | None = None


class Status(_In):
    """The first 50 items are kept."""

    type: Literal["status"]
    id: Slug | None = None
    title: Cut64 | None = None
    items: list[StatusItem]


class KvItem(_In):
    key: Cut40
    value: bool | Num | Cut120 | None
    format: Format | None = None
    unit: Cut16 | None = None


class Kv(_In):
    """The first 30 items are kept."""

    type: Literal["kv"]
    id: Slug | None = None
    title: Cut64 | None = None
    items: list[KvItem]


class Text(_In):
    """Plain text only: never rendered as markup."""

    type: Literal["text"]
    id: Slug | None = None
    title: Cut64 | None = None
    text: Cut800


class Details(_In):
    """Any JSON value. Core flattens it into at most 200 rows (an object's key is the row's
    text, never a JSON key); containers deeper than 5 levels become ``…``."""

    type: Literal["details"]
    id: Slug | None = None
    title: Cut64 | None = None
    data: Any
    collapsed: bool = True


class Candles(_In):
    """Open, high, low and close, each as long as ``x``. A candle is all four numbers with
    ``h >= max(o, c)`` and ``l <= min(o, c)``, or all four null (a gap)."""

    o: list[Num | None]
    h: list[Num | None]
    l: list[Num | None]  # noqa: E741 (the OHLC name)
    c: list[Num | None]


class Trade(_In):
    """A position on the chart: entered at ``entry_t``/``entry_px``, exited at
    ``exit_t``/``exit_px`` (both null while it is open). ``pnl`` is its money result."""

    side: Literal["long", "short"]
    entry_t: Time
    entry_px: Num
    exit_t: Time | None = None
    exit_px: Num | None = None
    pnl: Num | None = None
    currency: Currency | None = None
    label: Cut40 | None = None
    why: Cut24 | None = None


class Marker(_In):
    """A point of interest: an arrow up or down, or a dot."""

    t: Time
    price: Num
    kind: Literal["up", "down", "dot"] = "dot"
    tone: PillTone = "neutral"
    label: Cut40 | None = None


class Price(_In):
    """A price chart (PER-282): ``candles`` or ``line`` (closes only) over ``x``, the candles'
    open times, strictly increasing (whole seconds; a fraction is rounded). At most 20160
    points; over 1440 they are aggregated into wider candles. At most 100 trades and 200
    markers."""

    type: Literal["price"]
    id: Slug | None = None
    title: Cut64 | None = None
    x: Annotated[list[Time], _max_items(PRICE_POINTS_MAX)]
    candles: Candles | None = None
    line: list[Num | None] | None = None
    unit: Cut16 | None = None
    format: Format | None = None
    currency: Currency | None = None
    trades: Annotated[list[Trade], _max_items(TRADES_MAX)] = []
    markers: Annotated[list[Marker], _max_items(MARKERS_MAX)] = []


Widget = Annotated[
    Stat | Series | Table | Status | Kv | Text | Details | Price, Field(discriminator="type")
]


class View(_In):
    """A tab of the panel. At most 24 widgets (48 per panel)."""

    id: Slug
    title: Cut40
    widgets: Annotated[
        list[Widget],
        Field(min_length=1, max_length=WIDGETS_PER_VIEW_MAX),
    ]


class PanelOutput(_In):
    """What a panel command prints on stdout."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        title="PanelOutput",
    )

    schema_: Literal[1] = Field(alias="schema")
    # The data's time (ISO-8601 with an offset). A future time is clamped to the run's end;
    # absent means the run's end.
    updated_at: Annotated[str, Field(json_schema_extra={"format": "date-time"})] | None = None
    health: Tone | None = None
    # The panel's own web page on this Mac; the local dashboard links it, the relay never sees it.
    local_url: Annotated[str, StringConstraints(pattern=LOCAL_URL, max_length=300)] | None = None
    # Reserved for a later phase; only absent or [] is accepted.
    actions: Annotated[list[Any], Field(max_length=0)] = []
    views: Annotated[list[View], Field(min_length=1, max_length=VIEWS_MAX)]


class PanelNone(_In):
    """PER-286: what a history run (``--date YYYY-MM-DD``) prints for a day with no data (a
    weekend, a holiday). The day is recorded as none, never retried and never listed."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        title="PanelNone",
    )

    schema_: Literal[1] = Field(alias="schema")
    none: Literal[True]


def is_none(raw: object) -> bool:
    """``raw`` is exactly ``{"schema": 1, "none": true}`` (PER-286)."""
    if not isinstance(raw, dict):
        return False
    fields = cast(dict[str, Any], raw)
    return (
        set(fields) == {"schema", "none"}
        and type(fields["schema"]) is int
        and fields["schema"] == PANEL_SCHEMA_VERSION
        and fields["none"] is True
    )


def schema_json() -> str:
    """``docs/panel.schema.json``: the JSON schema of :class:`PanelOutput`, with
    :class:`PanelNone` (a history run's no-data answer) under ``$defs``."""
    schema = PanelOutput.model_json_schema(by_alias=True, ref_template="#/$defs/{model}")
    none = PanelNone.model_json_schema(by_alias=True)
    schema.setdefault("$defs", {})["PanelNone"] = none
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": "Generated from resonant_sdk/panel_schema.py; do not edit.",
        **schema,
    }
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    print(schema_json(), end="")

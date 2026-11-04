"""Normalising a panel command's output (PER-280): what the scheduler stores and the dashboard
draws.

A panel command prints one JSON object (``resonant_sdk.panel_schema``, contract in
``docs/extensions.md`` 6a). :func:`normalize` turns that raw object into the snapshot's own
models (``dashboard.snapshot.PanelView``), in this order:

1. validate against ``panel_schema`` (a ``ValidationError`` becomes a short refusal built from
   the field path and the error type, never from the values it echoes);
2. the structural refusals, each named by view and widget (too many series, points,
   categories or columns, a time axis that goes backwards, a cell of the wrong type, ...);
3. cuts and downsampling (rows, items, a long time axis), flattening of ``details``;
4. times become unix seconds (a status ``since``: a datetime);
5. every string goes through ``redact.clip_text`` at its cap: too long is cut, never refused;
6. the result is validated into ``PanelView`` models and measured as compact JSON against
   ``min(max_bytes, PANEL_BYTES_CAP)``.

A refusal fails the whole run: the scheduler keeps the last good copy and records the error.
``Normalized.truncated`` is True when rows, items, ``details`` rows or a table's cells were
dropped (a cut string or a downsampled axis doesn't count: the latter is marked on the
widget). Nothing here reads the store or runs anything.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, cast

from pydantic import ValidationError

from resonant.dashboard import snapshot as snap
from resonant.redact import clip_text
from resonant_sdk import panel_schema as ps
from resonant_sdk.manifest import PANEL_BYTES_CAP

ERROR_MAX = 200
_HEAD = "refused: "
_PORT = re.compile(r"^http://127\.0\.0\.1:([0-9]{1,5})(?:/|$)")
_MAX_UNIX = 32503680000  # year 3000
ELLIPSIS = "…"


@dataclass(frozen=True)
class Normalized:
    data: dict[str, Any]  # {views, health, local_url, truncated}, what ``ext_panels`` stores
    updated_at: datetime  # the data's time, UTC, never after ``ran_at``
    bytes: int  # the views as compact JSON
    truncated: bool


@dataclass(frozen=True)
class PanelRefused:
    error: str  # code-built, redacted, at most ``ERROR_MAX``


class _Refuse(Exception):
    pass


def _t(value: str, cap: int) -> str:
    return clip_text(value, cap) or ""


def _to(value: str | None, cap: int) -> str | None:
    return None if value is None else clip_text(value, cap)


def _unix(value: float | str, where: str) -> int | float:
    """A time (ISO-8601 with an offset, or unix seconds) as unix seconds."""
    if isinstance(value, str):
        try:
            when = datetime.fromisoformat(value)
        except ValueError:
            raise _Refuse(f"{where}: not an ISO-8601 time") from None
        if when.tzinfo is None or when.utcoffset() is None:
            raise _Refuse(f"{where}: a time needs an offset (like +00:00 or Z)")
        seconds = when.timestamp()
    else:
        seconds = value
    if not 0 <= seconds <= _MAX_UNIX:
        raise _Refuse(f"{where}: time out of range")
    return int(seconds) if float(seconds).is_integer() else seconds


def _dt(value: float | str, where: str) -> datetime:
    return datetime.fromtimestamp(_unix(value, where), UTC)


def _path(loc: Sequence[int | str]) -> str:
    return ".".join(str(p) for p in loc[:8])


def _validation_error(e: ValidationError) -> str:
    parts = [f"{_path(err['loc'])}: {err['type']}" for err in e.errors()[:3]]
    more = "" if e.error_count() <= 3 else f" (+{e.error_count() - 3} more)"
    return "; ".join(parts) + more


# --- widgets --------------------------------------------------------------------------------


class _Ctx:
    """The widget being converted, for messages, and what got cut."""

    def __init__(self, tools: frozenset[str]) -> None:
        self.tools = tools
        self.where = ""
        self.truncated = False

    def fail(self, message: str) -> _Refuse:
        return _Refuse(f"{self.where}: {message}")


def _common(w: Any) -> dict[str, Any]:
    return {"id": w.id, "title": _to(w.title, 64)}


def _stat(w: ps.Stat) -> dict[str, Any]:
    value = w.value
    return {
        **_common(w),
        "type": "stat",
        "label": _t(w.label, 40),
        "value": _t(value, 40) if isinstance(value, str) else value,
        "unit": _to(w.unit, 16),
        "format": w.format or "number",
        "currency": w.currency,
        "delta": w.delta,
        "delta_format": w.delta_format,
        "health": w.health,
        "hint": _to(w.hint, 80),
    }


def _downsample(
    x: list[int | float], lines: list[list[float | None]]
) -> tuple[list[int | float], list[list[float | None]]]:
    """To ``SERIES_POINTS_KEPT``: the first and last points exactly, the middle in equal
    buckets (the x of a bucket's first point, the mean of each line's non-null values)."""
    n = len(x)
    buckets = ps.SERIES_POINTS_KEPT - 2
    mid = n - 2
    xs = [x[0]]
    out: list[list[float | None]] = [[line[0]] for line in lines]
    for b in range(buckets):
        lo = 1 + b * mid // buckets
        hi = 1 + (b + 1) * mid // buckets
        xs.append(x[lo])
        for line, acc in zip(lines, out, strict=True):
            got = [v for v in line[lo:hi] if v is not None]
            acc.append(sum(got) / len(got) if got else None)
    xs.append(x[-1])
    for line, acc in zip(lines, out, strict=True):
        acc.append(line[-1])
    return xs, out


def _series(w: ps.Series, ctx: _Ctx) -> dict[str, Any]:
    if not w.series:
        raise ctx.fail("no series")
    if len(w.series) > ps.SERIES_MAX:
        raise ctx.fail(f"{len(w.series)} series (max {ps.SERIES_MAX})")
    n = len(w.x.values)
    if n == 0:
        raise ctx.fail("no points")
    if n > ps.SERIES_POINTS_MAX:
        raise ctx.fail(f"{n} points (max {ps.SERIES_POINTS_MAX})")
    for line in w.series:
        if len(line.values) != n:
            raise ctx.fail(f"line {_t(line.name, 20)!r} has {len(line.values)} values for {n} x")
    x: list[int | float | str]
    if w.x.type == "time":
        times = [_unix(v, f"{ctx.where}: x") for v in w.x.values]
        if any(b < a for a, b in pairwise(times)):
            raise ctx.fail("time x must ascend")
        x = list(times)
    else:
        if n > ps.SERIES_POINTS_KEPT:
            raise ctx.fail(f"{n} categories (max {ps.SERIES_POINTS_KEPT})")
        if not all(isinstance(v, str) for v in w.x.values):
            raise ctx.fail("category x must be strings")
        x = [_t(cast(str, v), 24) for v in w.x.values]
    lines: list[list[float | None]] = [list(line.values) for line in w.series]
    downsampled: int | None = None
    if w.x.type == "time" and n > ps.SERIES_POINTS_KEPT:
        down_x, lines = _downsample(cast(list[int | float], x), lines)
        x, downsampled = list(down_x), n
    return {
        **_common(w),
        "type": "series",
        "kind": w.kind,
        "x_type": w.x.type,
        "x": x,
        "series": [
            {"name": _t(line.name, 32), "values": vals}
            for line, vals in zip(w.series, lines, strict=True)
        ],
        "unit": _to(w.unit, 16),
        "format": w.format or "number",
        "currency": w.currency,
        "stacked": w.stacked,
        "downsampled_from": downsampled,
    }


def _cell(value: object, col: ps.Column, row_no: int, ctx: _Ctx) -> Any:
    if value is None:
        return None
    where = f"row {row_no + 1} column {col.key}"
    kind = col.type
    if kind == "text":
        if isinstance(value, str):
            return _t(value, 120)
    elif kind in ("number", "money"):
        if isinstance(value, (int, float)):
            return value
    elif kind == "time":
        if isinstance(value, (int, float, str)):
            return _unix(value, f"{ctx.where}: {where}")
    elif kind == "pill":
        if isinstance(value, str):
            return {"label": _t(value, 24), "tone": "neutral"}
        if isinstance(value, ps.Pill):
            return {"label": _t(value.label, 24), "tone": value.tone}
    elif isinstance(value, ps.Link):  # link
        label = _t(value.label, 64)
        if value.tool in ctx.tools:
            return {"label": label, "tool": value.tool}
        return _t(label, 120)  # a tool this extension doesn't have: plain text
    raise ctx.fail(f"{where} is not a {kind} cell")


def _table(w: ps.Table, ctx: _Ctx) -> dict[str, Any]:
    if not w.columns:
        raise ctx.fail("no columns")
    if len(w.columns) > ps.COLUMNS_MAX:
        raise ctx.fail(f"{len(w.columns)} columns (max {ps.COLUMNS_MAX})")
    keys = [c.key for c in w.columns]
    if len(set(keys)) != len(keys):
        raise ctx.fail("column keys must be unique")
    rows: list[list[Any]] = []
    for i, row in enumerate(w.rows[: ps.ROWS_MAX]):
        if unknown := sorted(set(row) - set(keys)):
            raise ctx.fail(f"row {i + 1} has unknown key {_t(unknown[0], 20)!r}")
        rows.append([_cell(row.get(c.key), c, i, ctx) for c in w.columns])
    cut = len(w.rows) > ps.ROWS_MAX
    ctx.truncated |= cut
    return {
        **_common(w),
        "type": "table",
        "columns": [
            {
                "key": c.key,
                "label": _t(c.label, 32),
                "type": c.type,
                "align": c.align,
                "currency": c.currency,
            }
            for c in w.columns
        ],
        "rows": rows,
        "truncated": cut,
        "total_rows": len(w.rows) if cut else None,
    }


def _status(w: ps.Status, ctx: _Ctx) -> dict[str, Any]:
    cut = len(w.items) > ps.STATUS_ITEMS_MAX
    ctx.truncated |= cut
    return {
        **_common(w),
        "type": "status",
        "items": [
            {
                "name": _t(i.name, 64),
                "state": i.state,
                "detail": _to(i.detail, 120),
                "since": None if i.since is None else _dt(i.since, f"{ctx.where}: since"),
            }
            for i in w.items[: ps.STATUS_ITEMS_MAX]
        ],
        "truncated": cut,
    }


def _kv(w: ps.Kv, ctx: _Ctx) -> dict[str, Any]:
    cut = len(w.items) > ps.KV_ITEMS_MAX
    ctx.truncated |= cut
    return {
        **_common(w),
        "type": "kv",
        "items": [
            {
                "key": _t(i.key, 40),
                "value": _t(i.value, 120) if isinstance(i.value, str) else i.value,
                "format": i.format,
                "unit": _to(i.unit, 16),
            }
            for i in w.items[: ps.KV_ITEMS_MAX]
        ],
        "truncated": cut,
    }


def flatten(data: object) -> tuple[list[dict[str, Any]], bool]:
    """``data`` (any JSON value) as at most ``DETAIL_ROWS_MAX`` rows in document order. An
    object's key is the row's *text* (so a key named ``remote_url`` is just a string), an
    array item's key is ``[i]``, the root's is empty. A container at depth
    ``DETAIL_DEPTH_MAX`` is one ``…`` row. Returns the rows and whether any were cut."""
    rows: list[dict[str, Any]] = []
    cut = False

    def walk(key: str, value: object, depth: int) -> None:
        nonlocal cut
        if len(rows) >= ps.DETAIL_ROWS_MAX:
            cut = True
            return
        label = _t(key, 64)
        if isinstance(value, (dict, list)) and depth >= ps.DETAIL_DEPTH_MAX:
            rows.append({"depth": depth, "key": label, "kind": "string", "value": ELLIPSIS})
        elif isinstance(value, dict):
            rows.append({"depth": depth, "key": label, "kind": "object", "value": None})
            for k, v in cast(Mapping[str, object], value).items():
                walk(str(k), v, depth + 1)
        elif isinstance(value, list):
            rows.append({"depth": depth, "key": label, "kind": "array", "value": None})
            for i, v in enumerate(cast(list[object], value)):
                walk(f"[{i}]", v, depth + 1)
        elif isinstance(value, str):
            rows.append({"depth": depth, "key": label, "kind": "string", "value": _t(value, 200)})
        elif isinstance(value, bool):
            rows.append({"depth": depth, "key": label, "kind": "bool", "value": str(value).lower()})
        elif value is None:
            rows.append({"depth": depth, "key": label, "kind": "null", "value": None})
        else:
            rows.append(
                {
                    "depth": depth,
                    "key": label,
                    "kind": "number",
                    "value": _t(json.dumps(value), 200),
                }
            )

    walk("", data, 0)
    return rows, cut


def _details(w: ps.Details, ctx: _Ctx) -> dict[str, Any]:
    rows, cut = flatten(w.data)
    ctx.truncated |= cut
    return {
        **_common(w),
        "type": "details",
        "rows": rows,
        "collapsed": w.collapsed,
        "truncated": cut,
    }


def _widget(w: Any, ctx: _Ctx) -> dict[str, Any]:
    if isinstance(w, ps.Stat):
        return _stat(w)
    if isinstance(w, ps.Series):
        return _series(w, ctx)
    if isinstance(w, ps.Table):
        return _table(w, ctx)
    if isinstance(w, ps.Status):
        return _status(w, ctx)
    if isinstance(w, ps.Kv):
        return _kv(w, ctx)
    if isinstance(w, ps.Text):
        return {**_common(w), "type": "text", "text": _t(w.text, 800)}
    return _details(cast(ps.Details, w), ctx)


# --- the whole panel -------------------------------------------------------------------------


def _updated_at(raw: str | None, ran_at: datetime) -> datetime:
    if raw is None:
        return ran_at
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        raise _Refuse("updated_at is not an ISO-8601 time") from None
    if when.tzinfo is None or when.utcoffset() is None:
        raise _Refuse("updated_at needs an offset (like +00:00 or Z)")
    return min(when.astimezone(UTC), ran_at)


def _local_url(url: str | None) -> str | None:
    if url is None:
        return None
    m = _PORT.match(url)
    if m is None or not 1 <= int(m.group(1)) <= 65535:
        raise _Refuse("local_url must be http://127.0.0.1:<port 1-65535>/...")
    if clip_text(url, 300) != url:
        raise _Refuse("local_url looks like it holds a secret")
    return url


def _normalize(raw: object, max_bytes: int, tools: frozenset[str], ran_at: datetime) -> Normalized:
    if not isinstance(raw, dict):
        raise _Refuse("output is not a JSON object")
    fields = cast(dict[str, Any], raw)
    if fields.get("schema") != ps.PANEL_SCHEMA_VERSION or isinstance(fields.get("schema"), bool):
        raise _Refuse(
            f"schema {_t(repr(fields.get('schema')), 20)} (want {ps.PANEL_SCHEMA_VERSION})"
        )
    try:
        out = ps.PanelOutput.model_validate(fields)
    except ValidationError as e:
        raise _Refuse(_validation_error(e)) from None
    total = sum(len(v.widgets) for v in out.views)
    if total > ps.WIDGETS_MAX:
        raise _Refuse(f"{total} widgets (max {ps.WIDGETS_MAX})")
    ctx = _Ctx(tools)
    views: list[dict[str, Any]] = []
    for view in out.views:
        widgets: list[dict[str, Any]] = []
        for n, w in enumerate(view.widgets, 1):
            ctx.where = f"view {_t(view.id, 20)} widget {n} ({w.type})"
            widgets.append(_widget(w, ctx))
        views.append({"id": view.id, "title": _t(view.title, 40), "widgets": widgets})
    try:
        models = [snap.PanelView.model_validate(v) for v in views]
    except ValidationError as e:  # a bug here, not the extension's: still a clean refusal
        raise _Refuse("normalised output is invalid: " + _validation_error(e)) from None
    dumped = [m.model_dump(mode="json") for m in models]
    size = len(json.dumps(dumped, separators=(",", ":")).encode())
    limit = min(max_bytes, PANEL_BYTES_CAP)
    if size > limit:
        raise _Refuse(f"panel is {size} bytes after normalising (max {limit})")
    data: dict[str, Any] = {
        "views": dumped,
        "health": out.health,
        "local_url": _local_url(out.local_url),
        "truncated": ctx.truncated,
    }
    return Normalized(data, _updated_at(out.updated_at, ran_at), size, ctx.truncated)


def normalize(
    raw: object, *, max_bytes: int, tools: frozenset[str], ran_at: datetime
) -> Normalized | PanelRefused:
    """Validate and normalise a panel command's output. ``tools`` are this extension's tool
    names (a link to another is shown as text); ``ran_at`` (aware) is the run's end."""
    try:
        return _normalize(raw, max_bytes, tools, ran_at)
    except _Refuse as e:
        return PanelRefused(clip_text(_HEAD + str(e), ERROR_MAX) or _HEAD.strip())

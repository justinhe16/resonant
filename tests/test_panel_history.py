"""Panel history (PER-286): the manifest field, the archive schedule across the panel's tz and
day boundary, none/failed/backoff, backfill cap and single flight, prune, the snapshot's day
list, the local API, ``read_panel`` with ``day``, the handler's ``--date`` and the rebuild CLI.
The real gate, host and scheduler over a temp store on a fake clock (``test_scheduler``'s
harness), with a scripted extension process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError
from test_scheduler import Harness, harness
from typer.testing import CliRunner

from resonant.api import DaemonState, create_app
from resonant.cli import app as cli_app
from resonant.config import load_settings
from resonant.dashboard.snapshot import build_snapshot, extension_panels, panel_day, to_remote
from resonant.extensions import panel_history as ph
from resonant.extensions.host import ExtensionHealth
from resonant.extensions.manager import history_words
from resonant.extensions.process import ExtFailed, ExtOutput, ExtRequest, RawResult, RedactFn
from resonant.loop import Loop, SlotPool
from resonant.secrets import secret_ref
from resonant.store import extensions as ext_store
from resonant.store.db import open_db, utcnow
from resonant.tools.base import BuiltinContext
from resonant.tools.ext import read_panel
from resonant_sdk import Manifest
from resonant_sdk import panel as p
from resonant_sdk.manifest import Panel, parse_yaml
from resonant_sdk.panel_schema import is_none

LA = ZoneInfo("America/Los_Angeles")
MANIFEST = """\
name: example
version: 1
secrets: [token]
tools:
  go: {run: ./cli.py go, effect: read}
panels:
  - id: paper
    title: Paper trading
    command: [./cli.py, panel, paper]
    refresh: {idle: "*/15 * * * *"}
    tz: America/Los_Angeles
    secrets: [token]
    timeout_s: 30
    history: {every: daily, keep: 7d}
"""
TWO = (
    MANIFEST
    + """\
  - id: live
    title: Live only
    command: [./cli.py, panel, live]
    refresh: {idle: "*/15 * * * *"}
    tz: America/Los_Angeles
"""
)


def at(hour: int, minute: int = 0, day: int = 14) -> datetime:
    """``hour:minute`` America/Los_Angeles (UTC-7 in October) on 2026-10-<day>, in UTC."""
    return datetime(2026, 10, day, 7, tzinfo=UTC) + timedelta(hours=hour, minutes=minute)


def date_of(req: ExtRequest) -> str | None:
    return req.argv[-1] if len(req.argv) >= 2 and req.argv[-2] == "--date" else None


def good(text: str) -> ExtOutput:
    return ExtOutput(
        p.panel([p.view("v", "V", [p.text(text)])], local_url="http://127.0.0.1:8765/x"),
        (),
        0,
        5,
    )


NONE = ExtOutput({"schema": 1, "none": True}, (), 0, 5)


@dataclass
class Script:
    """An ``ExtRunner``: a live run prints ``live``; a dated run answers ``per_day(day)``
    (default: a panel saying the day). ``hold`` blocks dated runs until it is set."""

    per_day: Callable[[str], ExtOutput | BaseException] | None = None
    hold: asyncio.Event | None = None
    hold_live: asyncio.Event | None = None
    calls: list[ExtRequest] = field(default_factory=list[ExtRequest])

    @property
    def days(self) -> list[str]:
        return [d for c in self.calls if (d := date_of(c)) is not None]

    async def run(self, req: ExtRequest, *, redact: RedactFn) -> ExtOutput:
        self.calls.append(req)
        day = date_of(req)
        if day is None:
            if self.hold_live is not None:
                await self.hold_live.wait()
            return good("live")
        if self.hold is not None:
            await self.hold.wait()
        got = self.per_day(day) if self.per_day else good(f"day {day}")
        if isinstance(got, BaseException):
            raise got
        return got

    async def run_raw(self, req: ExtRequest, *, redact: RedactFn) -> RawResult:
        raise AssertionError("not used")


@contextlib.asynccontextmanager
async def hist_harness(
    tmp_path: Path, runner: Script, *, manifest: str = MANIFEST, at_: datetime | None = None
) -> AsyncGenerator[Harness]:
    async with harness(tmp_path, runner, manifest=manifest, at=at_ or at(0, 1)) as h:
        keychain: Any = h.components.executor.secrets.runner  # type: ignore[union-attr]
        keychain.items[secret_ref("example", "token")] = "s3cr3t-value-123"
        yield h


def days_of(h: Harness, panel: str = "paper") -> dict[str, ext_store.PanelDay]:
    return {d.day: d for d in ext_store.list_panel_days(h.db, "example", panel)}


# --- the rules (pure) ----------------------------------------------------------------------


def test_the_latest_day_turns_over_at_00_15_in_the_panels_zone() -> None:
    assert ph.latest_day(at(0, 14), LA) == date(2026, 10, 12)
    assert ph.latest_day(at(0, 15), LA) == date(2026, 10, 13)
    assert ph.latest_day(at(23, 59), LA) == date(2026, 10, 13)
    # 00:20 in Los Angeles is 03:20 in New York: there the 14th is already archivable
    assert ph.latest_day(at(0, 20), ZoneInfo("America/New_York")) == date(2026, 10, 13)
    ny_0010 = at(0, 20) - timedelta(hours=3, minutes=10)  # 00:10 in New York
    assert ph.latest_day(ny_0010, ZoneInfo("America/New_York")) == date(2026, 10, 12)
    # across the DST change (2026-11-01 in Los Angeles): still local midnight + 15 min
    nov2 = datetime(2026, 11, 2, 8, 14, tzinfo=UTC)  # 00:14 PST
    assert ph.latest_day(nov2, LA) == date(2026, 10, 31)
    assert ph.latest_day(nov2 + timedelta(minutes=1), LA) == date(2026, 11, 1)


def test_the_window_is_min_keep_30_newest_first() -> None:
    w = ph.window(at(1), LA, 365)
    assert len(w) == ph.BACKFILL_DAYS_MAX == 30
    assert w[0] == date(2026, 10, 13) and w[-1] == date(2026, 9, 14)
    assert len(ph.window(at(1), LA, 7)) == 7


def test_next_day_skips_settled_days_and_waits_out_backoff() -> None:
    now = at(1)
    rows = {
        "2026-10-13": ph.DayState("ok", None),
        "2026-10-12": ph.DayState("none", None),
        "2026-10-11": ph.DayState("failed", None),
        "2026-10-10": ph.DayState("pending", now + timedelta(minutes=5)),
    }
    assert ph.next_day(rows, now, LA, 7) == date(2026, 10, 9)
    assert ph.next_day(rows, now + timedelta(minutes=5), LA, 7) == date(2026, 10, 10)
    full = {d.isoformat(): ph.DayState("ok", None) for d in ph.window(now, LA, 7)}
    assert ph.next_day(full, now, LA, 7) is None
    # a rebuild outside the 30-day window but inside keep is picked up; past keep it isn't
    old = {**full, "2026-08-01": ph.DayState("pending", None)}
    assert ph.next_day(old, now, LA, 365) == date(2026, 10, 6)  # missing window days first
    full365 = {d.isoformat(): ph.DayState("ok", None) for d in ph.window(now, LA, 365)}
    assert ph.next_day({**full365, "2026-08-01": ph.DayState("pending", None)}, now, LA, 365) == (
        date(2026, 8, 1)
    )
    assert ph.next_day({**full, "2026-08-01": ph.DayState("pending", None)}, now, LA, 7) is None


def test_retry_backoff_then_failed() -> None:
    now = at(1)
    assert ph.retry_at(1, now) == now + timedelta(minutes=15)
    assert ph.retry_at(2, now) == now + timedelta(hours=1)
    assert ph.retry_at(3, now) is None


def test_the_none_contract_is_exact() -> None:
    assert is_none({"schema": 1, "none": True})
    raws: tuple[object, ...] = (
        {"schema": 1, "none": False},
        {"schema": True, "none": True},
        {"schema": 2, "none": True},
        {"schema": 1, "none": 1},
        {"schema": 1, "none": True, "views": []},
        [],
        None,
    )
    for raw in raws:
        assert not is_none(raw), raw


# --- the manifest --------------------------------------------------------------------------


def _panel(**extra: Any) -> dict[str, Any]:
    return {
        "id": "paper",
        "title": "Paper",
        "command": ["./cli.py", "panel", "paper"],
        "refresh": {"idle": "*/15 * * * *"},
        **extra,
    }


def test_manifest_history_validation() -> None:
    assert Panel.model_validate(_panel()).history is None
    h = Panel.model_validate(_panel(history={"every": "daily"})).history
    assert h is not None and h.keep == "365d" and h.keep_days == 365
    assert Panel.model_validate(_panel(history={"every": "daily", "keep": "7d"})).history
    assert Panel.model_validate(_panel(history={"every": "daily", "keep": "730d"})).history
    for bad in (
        {"every": "weekly"},
        {"every": "daily", "keep": "6d"},
        {"every": "daily", "keep": "731d"},
        {"every": "daily", "keep": "1y"},
        {"every": "daily", "keep": 30},
        {"every": "daily", "extra": 1},
        {"keep": "30d"},
    ):
        with pytest.raises(ValidationError):
            Panel.model_validate(_panel(history=bad))
    for argv in (["./cli.py", "panel", "--date"], ["./cli.py", "--date=2026-01-01"]):
        with pytest.raises(ValidationError, match="--date"):
            Panel.model_validate(_panel(command=argv))


def test_only_a_history_panel_spec_takes_a_date() -> None:
    plain = Panel.model_validate(_panel()).spec("example")
    dated = Panel.model_validate(_panel(history={"every": "daily"})).spec("example")
    assert "date" not in plain.args_schema["properties"]
    assert dated.args_schema["properties"]["date"]["pattern"] == ph.DAY_RE.pattern
    assert dated.args_schema["required"] == ["due_at"]


def test_the_review_shows_history_in_the_panel_line() -> None:
    m = Manifest.model_validate(parse_yaml(MANIFEST))
    assert history_words(m.panels[0]) == (
        "; history daily, keep 7d (the same command with --date YYYY-MM-DD for each ended day)"
    )
    assert history_words(Panel.model_validate(_panel())) == ""


# --- the archiver in the scheduler ---------------------------------------------------------


async def test_a_day_is_archived_after_00_15_local_with_the_date_flag(tmp_path: Path) -> None:
    run = Script()
    async with hist_harness(tmp_path, run, at_=at(0, 9)) as h:
        await h.tick(at(0, 10))
        # before 00:15 on the 14th the newest archivable day is the 12th
        assert run.days[0] == "2026-10-12"
        call = next(c for c in run.calls if date_of(c))
        assert call.argv[-4:] == ("panel", "paper", "--date", "2026-10-12")
        assert call.label == "example/panel_paper" and "RESONANT_JOB_DUE_AT" in call.env
        await h.tick(at(0, 14))
        assert "2026-10-13" not in run.days
        await h.tick(at(0, 16))
        await h.tick(at(0, 17))
        assert "2026-10-13" in run.days  # once 00:15 passed
        # every dated run asked only for a day that had ended by 00:15 local at its start
        for c in run.calls:
            if (d := date_of(c)) is not None:
                started = datetime.fromisoformat(c.env["RESONANT_JOB_DUE_AT"])
                assert date.fromisoformat(d) <= ph.latest_day(started, LA), (d, started)
        rows = days_of(h)
        assert rows["2026-10-13"].status == "ok" and rows["2026-10-13"].upload_due
        stored = json.loads(rows["2026-10-13"].data_json or "")
        assert stored["views"][0]["widgets"][0]["text"] == "day 2026-10-13"
        details = [json.loads(a["detail"]) for a in h.audits("panel.archived")]
        (detail,) = [d for d in details if d["day"] == "2026-10-13"]
        assert detail["status"] == "ok" and detail["panel"] == "example/paper"
        gated = [a for a in h.audits("gate.evaluate") if "2026-10-13" in a["task_id"]]
        assert gated and gated[0]["tool"] == "panel_paper"


async def test_backfill_fills_min_keep_30_newest_first_one_at_a_time_then_stops(
    tmp_path: Path,
) -> None:
    run = Script()
    async with hist_harness(tmp_path, run, at_=at(1, 1)) as h:
        for minute in range(2, 14):
            await h.tick(at(1, minute))
        want = [(date(2026, 10, 13) - timedelta(days=i)).isoformat() for i in range(7)]
        assert run.days == want  # keep 7d: 7 days, the newest first, each once
        assert set(days_of(h)) == set(want)
        await h.tick(at(12, 7))
        await h.tick(at(23, 59))
        assert run.days == want  # immutable: never re-run on their own
        await h.tick(at(0, 16, day=15))
        await h.tick(at(0, 17, day=15))
        assert run.days[-1] == "2026-10-14"  # and the next day after it ends


async def test_one_archive_at_a_time_and_never_while_a_live_panel_runs(tmp_path: Path) -> None:
    hold = asyncio.Event()
    run = Script(hold=hold)
    async with hist_harness(tmp_path, run, at_=at(1, 1)) as h:
        h.clock.set(at(1, 2))
        await h.sched.tick(at(1, 2))
        await asyncio.sleep(0)
        assert h.sched.archiving and run.days == ["2026-10-13"]
        await h.sched.tick(at(1, 3))
        await h.sched.tick(at(1, 4))
        await asyncio.sleep(0)
        assert run.days == ["2026-10-13"]  # still one in flight
        hold.set()
        await h.sched.drain()
        assert not h.sched.archiving and days_of(h)["2026-10-13"].status == "ok"
        # a live run in flight: no archive starts until it is done
        live = asyncio.Event()
        run.hold_live = live
        h.clock.set(at(1, 15, day=14) + timedelta(seconds=5))
        await h.sched.tick(h.clock.now)
        await asyncio.sleep(0)
        assert run.calls[-1].argv[-1] == "paper" and run.days == ["2026-10-13"]
        await h.sched.tick(h.clock.now + timedelta(seconds=30))
        assert run.days == ["2026-10-13"]
        live.set()
        await h.sched.drain()
        await h.tick(at(1, 17))
        assert run.days == ["2026-10-13", "2026-10-12"]


async def test_a_none_day_is_recorded_never_retried_and_never_listed(tmp_path: Path) -> None:
    run = Script(per_day=lambda d: NONE if d == "2026-10-13" else good(d))
    async with hist_harness(tmp_path, run, at_=at(1, 1)) as h:
        for minute in range(2, 12):
            await h.tick(at(1, minute))
        assert run.days.count("2026-10-13") == 1
        row = days_of(h)["2026-10-13"]
        assert row.status == "none" and row.data_json is None and not row.upload_due
        record = ext_store.get_extension(h.db, "example")
        assert record is not None
        (panel,) = extension_panels(h.db, record, h.clock.now)
        assert panel.history is not None
        listed = [*panel.history.days, *(panel.history.pending or [])]
        assert "2026-10-13" not in listed and "2026-10-12" in listed
        assert panel_day(h.db, record, "paper", "2026-10-13") is None
        assert [json.loads(a["detail"])["status"] for a in h.audits("panel.archived")].count(
            "none"
        ) == 1


async def test_failures_back_off_then_fail_after_three_without_degrading_health(
    tmp_path: Path,
) -> None:
    def per_day(d: str) -> ExtOutput | BaseException:
        if d == "2026-10-13":
            return ExtFailed("exit 2", stderr_tail="boom")
        return NONE

    run = Script(per_day=per_day)
    async with hist_harness(tmp_path, run, at_=at(1, 1)) as h:
        await h.tick(at(1, 2))
        row = days_of(h)["2026-10-13"]
        assert row.status == "pending" and row.attempts == 1
        assert row.next_try_at == at(1, 2) + timedelta(minutes=15)
        for minute in range(3, 12):  # the other days, while it waits
            await h.tick(at(1, minute))
        assert run.days.count("2026-10-13") == 1
        await h.tick(at(1, 17))
        row = days_of(h)["2026-10-13"]
        assert run.days.count("2026-10-13") == 2 and row.attempts == 2
        assert row.next_try_at == at(1, 17) + timedelta(hours=1)
        await h.tick(at(2, 16))
        assert run.days.count("2026-10-13") == 2  # 1 min early
        await h.tick(at(2, 18))
        row = days_of(h)["2026-10-13"]
        assert run.days.count("2026-10-13") == 3 and row.status == "failed"
        assert row.next_try_at is None and row.last_error and "exit 2" in row.last_error
        await h.tick(at(6))
        await h.tick(at(12, 1))
        assert run.days.count("2026-10-13") == 3  # failed: never again on its own
        fails = [json.loads(a["detail"]) for a in h.audits("panel.archive_failed")]
        assert [f["failed"] for f in fails] == [False, False, True]
        assert fails[-1]["stderr_tail"] == "boom"
        snap = build_snapshot(h.db, h.settings, started_at=at(0), now=at(12, 2))
        (panel,) = snap.extensions[0].panels
        assert panel.history is not None and panel.history.failed == ["2026-10-13"]
        result, _ = ExtensionHealth(h.components.extensions)._result("example", False)  # pyright: ignore[reportPrivateUsage,reportArgumentType]
        assert result.ok


async def test_a_refused_output_is_a_failed_attempt(tmp_path: Path) -> None:
    bad = ExtOutput({"schema": 1, "views": []}, (), 0, 5)
    run = Script(per_day=lambda d: bad if d == "2026-10-13" else NONE)
    async with hist_harness(tmp_path, run, at_=at(1, 1)) as h:
        await h.tick(at(1, 2))
        row = days_of(h)["2026-10-13"]
        assert row.status == "pending" and row.last_error and row.last_error.startswith("refused")


async def test_paused_defers_without_counting(tmp_path: Path) -> None:
    run = Script()
    async with hist_harness(tmp_path, run, at_=at(1, 1)) as h:
        from resonant import killswitch

        killswitch.engage(h.db, actor="test")
        await h.tick(at(1, 2))
        assert run.days == [] and days_of(h) == {}
        killswitch.release(h.db, actor="test")
        await h.tick(at(1, 3))
        assert run.days == ["2026-10-13"]


async def test_days_past_keep_are_pruned(tmp_path: Path) -> None:
    run = Script(per_day=lambda d: NONE)
    async with hist_harness(tmp_path, run, at_=at(1, 1)) as h:
        for day in ("2026-10-01", "2026-10-06", "2026-10-07"):
            ext_store.save_day_ok(h.db, "example", "paper", day, data="{}", bytes=2, at=at(0))
        h.db.commit()
        await h.tick(at(1, 2))
        # keep 7d on the 14th: the 7th is the oldest kept
        assert "2026-10-01" not in days_of(h) and "2026-10-06" not in days_of(h)
        assert "2026-10-07" in days_of(h)
        (pruned,) = h.audits("panel.history_pruned")
        assert json.loads(pruned["detail"])["days"] == 2


async def test_a_panel_without_history_never_archives(tmp_path: Path) -> None:
    run = Script()
    async with hist_harness(tmp_path, run, manifest=TWO, at_=at(1, 1)) as h:
        for minute in range(2, 12):
            await h.tick(at(1, minute))
        assert all(c.argv[-1] == "paper" or date_of(c) for c in run.calls if "live" in c.argv)
        assert not [c for c in run.calls if "live" in c.argv and date_of(c)]
        assert days_of(h, "live") == {}


async def test_a_dated_call_on_a_plain_panel_is_refused(tmp_path: Path) -> None:
    run = Script()
    async with hist_harness(tmp_path, run, manifest=TWO, at_=at(1, 1)) as h:
        from resonant.executor import CallContext, ToolFailed

        host = h.components.extensions
        assert host is not None
        handler = host.handlers[("example", "panel_live")]
        with pytest.raises(ToolFailed, match="no date"):
            await handler({"due_at": "x", "date": "2026-10-13"}, CallContext({}, "k", "t"))
        dated = host.handlers[("example", "panel_paper")]
        with pytest.raises(ToolFailed, match="YYYY-MM-DD"):
            await dated({"due_at": "x", "date": "2026-13-45"}, CallContext({}, "k", "t"))
        assert run.calls == []


# --- store: removal --------------------------------------------------------------------------


def test_removing_the_extension_or_the_panel_removes_its_days(tmp_path: Path) -> None:
    db = open_db(tmp_path / "s.db")
    now = utcnow()
    _save_ext(db, now)
    for pid in ("paper", "chart"):
        ext_store.save_day_ok(db, "example", pid, "2026-10-13", data="{}", bytes=2, at=now)
    assert ext_store.prune_panel_days(db, "example", ["paper"]) == 1
    assert [d.panel_id for d in ext_store.list_panel_days(db, "example", "paper")] == ["paper"]
    assert ext_store.delete_extension(db, "example")
    assert ext_store.list_panel_days(db, "example", "paper") == []


# --- snapshot, local API, read_panel ---------------------------------------------------------


def _manifest(keep: str = "30d") -> dict[str, Any]:
    m = Manifest.model_validate(parse_yaml(MANIFEST.replace("keep: 7d", f"keep: {keep}")))
    out = m.model_dump(mode="json")
    out["panels"].append({**out["panels"][0], "id": "plain", "title": "Plain", "history": None})
    return out


def _save_ext(db: sqlite3.Connection, now: datetime, keep: str = "30d") -> None:
    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name="example", source="repo:example", commit="a" * 40, tree_digest="d",
            manifest_sha="m", manifest=_manifest(keep), installed_at=now, installed_by="justin",
        ),
    )  # fmt: skip


def _seed_days(db: sqlite3.Connection, now: datetime) -> None:
    from panel_helpers import normalized

    raw = p.panel(
        [p.view("v", "V", [p.stat("Equity", 10432.5, format="money")])],
        local_url="http://127.0.0.1:8765/ledger",
        health="warn",
    )
    data = json.dumps(normalized(raw).data)
    ext_store.save_day_ok(db, "example", "paper", "2026-10-13", data=data, bytes=99, at=now)
    ext_store.save_day_ok(db, "example", "paper", "2026-10-12", data=data, bytes=99, at=now)
    assert ext_store.mark_uploaded(db, "example", "paper", "2026-10-12", data)  # on the relay
    ext_store.save_day_none(db, "example", "paper", "2026-10-11", at=now)
    for _ in range(3):
        ext_store.record_day_failure(
            db, "example", "paper", "2026-10-10", at=now, error="boom", max_attempts=3,
            next_try_at=None,
        )  # fmt: skip
    db.commit()


def test_the_snapshot_lists_only_days_with_a_copy(tmp_path: Path) -> None:
    db = open_db(tmp_path / "s.db")
    now = at(9)
    _save_ext(db, now)
    _seed_days(db, now)
    snap = build_snapshot(db, load_settings(), started_at=now, now=now)
    paper, plain = snap.extensions[0].panels
    assert paper.history is not None
    # the 12th is on the relay; the 13th isn't yet: only the local tier lists it
    assert paper.history.days == ["2026-10-12"] and paper.history.pending == ["2026-10-13"]
    assert paper.history.failed == ["2026-10-10"]
    assert plain.history is None
    dumped = snap.model_dump(mode="json")["extensions"][0]["panels"][0]
    assert dumped["history"] == {
        "days": ["2026-10-12"], "pending": ["2026-10-13"], "failed": ["2026-10-10"]
    }  # fmt: skip
    remote = to_remote(snap).model_dump(mode="json")["extensions"][0]["panels"][0]
    assert remote["history"] == {"days": ["2026-10-12"], "failed": ["2026-10-10"]}
    # a day list never carries a copy: the snapshot is a list of dates
    assert "Equity" not in json.dumps(dumped["history"])


def test_the_day_list_is_capped_at_keep(tmp_path: Path) -> None:
    db = open_db(tmp_path / "s.db")
    now = at(9)
    _save_ext(db, now, keep="7d")
    for i in range(10):
        day = (date(2026, 10, 13) - timedelta(days=i)).isoformat()
        ext_store.save_day_ok(db, "example", "paper", day, data='{"views":[]}', bytes=2, at=now)
    snap = build_snapshot(db, load_settings(), started_at=now, now=now)
    history = snap.extensions[0].panels[0].history
    assert history is not None and history.days == [] and history.pending is not None
    assert len(history.pending) == 7 and history.pending[0] == "2026-10-13"


async def test_the_local_api_serves_a_day_read_only_and_404s_the_rest(tmp_path: Path) -> None:
    db = open_db(tmp_path / "s.db")
    now = at(9)
    _save_ext(db, now)
    _seed_days(db, now)
    settings = load_settings()
    app = create_app(DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)), now))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        got = await client.get("/api/ext/example/panels/paper/days/2026-10-13")
        assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
        body = got.json()
        assert body["day"] == "2026-10-13" and body["health"] == "warn"
        assert body["views"][0]["widgets"][0]["label"] == "Equity"
        assert "local_url" not in json.dumps(body)
        for path in (
            "/api/ext/example/panels/paper/days/2026-10-11",  # none
            "/api/ext/example/panels/paper/days/2026-10-10",  # failed
            "/api/ext/example/panels/paper/days/2026-10-09",  # never archived
            "/api/ext/example/panels/paper/days/2026-13-01",  # not a day
            "/api/ext/example/panels/paper/days/latest",
            "/api/ext/example/panels/plain/days/2026-10-13",  # no history
            "/api/ext/example/panels/nope/days/2026-10-13",
            "/api/ext/nope/panels/paper/days/2026-10-13",
        ):
            assert (await client.get(path)).status_code == 404, path
        for method in ("POST", "PUT", "DELETE"):
            r = await client.request(method, "/api/ext/example/panels/paper/days/2026-10-13")
            assert r.status_code == 405, method
    assert len(ext_store.list_panel_days(db, "example", "paper")) == 4  # nothing changed


async def test_the_local_api_is_loopback_only(tmp_path: Path) -> None:
    db = open_db(tmp_path / "s.db")
    _save_ext(db, at(9))
    _seed_days(db, at(9))
    settings = load_settings()
    app = create_app(DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)), at(9)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://evil.example") as client:
        r = await client.get("/api/ext/example/panels/paper/days/2026-10-13")
    assert r.status_code == 403


def test_read_panel_takes_a_day(tmp_path: Path) -> None:
    db = open_db(tmp_path / "s.db")
    now = at(9)
    _save_ext(db, now)
    _seed_days(db, now)
    from types import SimpleNamespace

    ctx = cast(BuiltinContext, SimpleNamespace(conn=db, clock=lambda: now))  # read_panel's needs
    got = read_panel(ctx, {"extension": "example", "panel": "paper", "day": "2026-10-13"})
    assert got["untrusted"] is True
    assert got["panel"]["day"] == "2026-10-13"
    assert got["panel"]["views"][0]["widgets"][0]["label"] == "Equity"
    listed = read_panel(ctx, {"extension": "example"})
    (head, plain) = listed["panels"]
    assert head["history"] == {
        "days": 2,
        "newest": ["2026-10-13", "2026-10-12"],
        "failed": ["2026-10-10"],
    }
    assert "history" not in plain
    from resonant.executor import ToolFailed

    for args, msg in (
        ({"extension": "example", "day": "2026-10-13"}, "needs a panel"),
        ({"extension": "example", "panel": "paper", "day": "13/10"}, "YYYY-MM-DD"),
        ({"extension": "example", "panel": "paper", "day": "2026-10-11"}, "no archived"),
        ({"extension": "example", "panel": "plain", "day": "2026-10-13"}, "no archived"),
    ):
        with pytest.raises(ToolFailed, match=msg):
            read_panel(ctx, args)


# --- the rebuild CLI -------------------------------------------------------------------------


@pytest.fixture
def cli_db(resonant_home: Path) -> sqlite3.Connection:
    conn = open_db(resonant_home / "resonant.db")
    _save_ext(conn, utcnow())
    conn.commit()
    return conn


def test_panel_history_lists_the_days(cli_db: sqlite3.Connection) -> None:
    runner = CliRunner()
    res = runner.invoke(cli_app, ["ext", "panel-history", "example", "paper"])
    assert res.exit_code == 0 and "no archived days yet (keep 30d)" in res.output
    _seed_days(cli_db, utcnow())
    res = runner.invoke(cli_app, ["ext", "panel-history", "example", "paper"])
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert lines[0].startswith("2026-10-13  ok") and "2026-10-10  failed" in res.output
    assert "boom" in res.output


def test_rebuild_queues_days_and_keeps_their_copies(cli_db: sqlite3.Connection) -> None:
    now = utcnow()
    tz = ZoneInfo(load_settings().timezone)
    last = ph.latest_day(now, tz)
    first = last - timedelta(days=2)
    _seed_days(cli_db, now)
    ext_store.save_day_ok(
        cli_db, "example", "paper", last.isoformat(), data='{"views":[]}', bytes=2, at=now
    )
    cli_db.commit()
    runner = CliRunner()
    res = runner.invoke(
        cli_app,
        ["ext", "panel-history", "example", "paper", "--rebuild", "--since", first.isoformat()],
    )
    assert res.exit_code == 0, res.output
    assert f"Queued 3 days of example/paper ({first} to {last})" in res.output
    rows = {d.day: d for d in ext_store.list_panel_days(cli_db, "example", "paper")}
    for d in ph.days_between(first, last):
        assert rows[d.isoformat()].status == "pending" and rows[d.isoformat()].attempts == 0
    assert rows[last.isoformat()].data_json is not None  # the old copy shows meanwhile
    (audit_row,) = cli_db.execute(
        "SELECT * FROM audit_log WHERE action = 'panel.history_rebuild'"
    ).fetchall()
    assert json.loads(audit_row["detail"])["days"] == 3
    # the scheduler picks them up: a pending day inside keep is due now
    states = {k: ph.DayState(v.status, v.next_try_at) for k, v in rows.items()}
    assert ph.next_day(states, now, tz, 30) == last


@pytest.mark.parametrize(
    ("args", "msg", "code"),
    [
        (["example", "nope", "--rebuild"], "has no panel nope with history", 1),
        (["example", "plain", "--rebuild"], "has no panel plain with history", 1),
        (["nope", "paper"], "nope is not installed", 1),
        (["example", "paper", "--since", "2026-10-01"], "--since goes with --rebuild", 2),
        (["example", "paper", "--rebuild", "--since", "13/10/2026"], "YYYY-MM-DD", 2),
        (["example", "paper", "--rebuild", "--since", "2001-01-01"], "past keep", 1),
        (["example", "paper", "--rebuild", "--since", "2999-01-01"], "hasn't been archived", 1),
    ],
)
def test_rebuild_refusals(cli_db: sqlite3.Connection, args: list[str], msg: str, code: int) -> None:
    res = CliRunner().invoke(cli_app, ["ext", "panel-history", *args])
    assert res.exit_code == code and msg in res.output, res.output
    assert all(d.status != "pending" for d in ext_store.list_panel_days(cli_db, "example", "paper"))


def test_rebuild_without_since_queues_every_kept_day(cli_db: sqlite3.Connection) -> None:
    res = CliRunner().invoke(cli_app, ["ext", "panel-history", "example", "paper", "--rebuild"])
    assert res.exit_code == 0 and "Queued 30 days" in res.output, res.output


async def test_a_killed_run_defers_without_counting_and_holds_the_archiver(
    tmp_path: Path,
) -> None:
    from resonant.extensions.process import ExtAmbiguous

    run = Script(per_day=lambda d: ExtAmbiguous("killed"))
    async with hist_harness(tmp_path, run, at_=at(1, 1)) as h:
        await h.tick(at(1, 2))
        await h.tick(at(1, 3))
        await h.tick(at(1, 10))
        deferred = h.audits("panel.archive_deferred")
        assert len(deferred) == 1 and json.loads(deferred[0]["detail"])["why"] == "killed"
        row = days_of(h)["2026-10-13"]
        assert row.status == "pending" and row.attempts == 0
        assert row.next_try_at == at(1, 2) + ph.DEFER
        assert run.days == ["2026-10-13"]  # held: no other day meanwhile
        await h.tick(at(1, 18))
        assert run.days == ["2026-10-13", "2026-10-13"]


def test_mark_uploaded_keeps_a_day_a_rebuild_changed_meanwhile(tmp_path: Path) -> None:
    db = open_db(tmp_path / "s.db")
    now = utcnow()
    _save_ext(db, now)
    ext_store.save_day_ok(db, "example", "paper", "2026-10-13", data='{"a":1}', bytes=7, at=now)
    sent = ext_store.get_panel_day(db, "example", "paper", "2026-10-13")
    assert sent is not None
    ext_store.save_day_ok(db, "example", "paper", "2026-10-13", data='{"a":2}', bytes=7, at=now)
    assert not ext_store.mark_uploaded(db, "example", "paper", "2026-10-13", sent.data_json)
    row = ext_store.get_panel_day(db, "example", "paper", "2026-10-13")
    assert row is not None and row.upload_due
    assert ext_store.mark_uploaded(db, "example", "paper", "2026-10-13", '{"a":2}')


async def test_an_unexpected_error_holds_the_archiver_instead_of_looping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Script()
    async with hist_harness(tmp_path, run, at_=at(1, 1)) as h:
        calls: list[str] = []

        def boom(*a: Any, **k: Any) -> None:
            calls.append("x")
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(h.sched, "_finish_archive", boom)
        await h.tick(at(1, 2))
        await h.tick(at(1, 3))
        await h.tick(at(1, 10))
        assert calls == ["x"]  # held for 15 minutes, not retried every tick
        await h.tick(at(1, 18))
        assert calls == ["x", "x"]

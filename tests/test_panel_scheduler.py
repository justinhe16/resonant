"""Panels in the scheduler (PER-280): active/idle schedules, the start-up run, pause, state,
failure and health. The real gate, host and scheduler over a temp store, on a fake clock, with
``FakeExtProcess`` (or a blocking fake) for the extension's process."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_scheduler import T0, BlockingRunner, Harness, harness

from resonant import killswitch
from resonant.dashboard.snapshot import build_snapshot
from resonant.extensions.host import ExtensionHealth
from resonant.extensions.process import ExtFailed, ExtOutput, ExtRunner, FakeExtProcess
from resonant.extensions.scheduler import PANEL_GUARD_S, Scheduler
from resonant.secrets import secret_ref
from resonant.store import extensions as ext_store
from resonant.store.db import iso
from resonant_sdk import panel as p

OWNER = "justin"
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
    refresh: {idle: "*/15 * * * *", active: "*/1 9-16 * * 1-5"}
    tz: America/Los_Angeles
    secrets: [token]
    timeout_s: 30
"""
TWO = (
    MANIFEST
    + """\
  - id: oncall
    title: On call
    command: [./cli.py, panel, oncall]
    refresh: {idle: "*/15 * * * *"}
    tz: America/Los_Angeles
"""
)


@contextlib.asynccontextmanager
async def panel_harness(
    tmp_path: Path, runner: ExtRunner | None, **kw: Any
) -> AsyncGenerator[Harness]:
    """``harness`` with the panel's ``token`` secret in the (fake) keychain."""
    async with harness(tmp_path, runner, **kw) as h:
        keychain: Any = h.components.executor.secrets.runner  # type: ignore[union-attr]
        keychain.items[secret_ref("example", "token")] = "s3cr3t-value-123"
        yield h


def good(text: str = "hi", **top: Any) -> ExtOutput:
    return ExtOutput(p.panel([p.view("v", "V", [p.text(text)])], **top), (), 0, 5)


def panels_of(h: Harness) -> dict[str, ext_store.PanelState]:
    return {s.panel_id: s for s in ext_store.list_panels(h.db, "example")}


def at(hour: int, minute: int = 0, second: int = 0, day: int = 13) -> datetime:
    base = datetime(2026, 10, day, 7, tzinfo=UTC)  # 00:00 America/Los_Angeles (UTC-7)
    return base + timedelta(hours=hour, minutes=minute, seconds=second)


async def test_the_active_cron_runs_each_minute_inside_its_window_and_idle_outside(
    tmp_path: Path,
) -> None:
    fake = FakeExtProcess(results=[good()])
    async with panel_harness(tmp_path, fake, manifest=MANIFEST, at=at(7, 59)) as h:
        # 08:00 is outside 09-16: the idle */15 governs; 08:01 is not due for it
        await h.tick(at(8))
        await h.tick(at(8, 1))
        assert len(fake.calls) == 1
        await h.tick(at(8, 15))
        assert len(fake.calls) == 2
        # inside the window the active cron runs every minute
        for minute in (0, 1, 2):
            await h.tick(at(9, minute))
        assert len(fake.calls) == 5
        assert [c.env["RESONANT_JOB_DUE_AT"] for c in fake.calls[-3:]] == [
            iso(at(9, m)) for m in (0, 1, 2)
        ]
        # past 17:00 the idle cron again: due at 17:00 and 17:15, not at 17:01
        await h.tick(at(17))
        await h.tick(at(17, 1))
        assert len(fake.calls) == 6
        await h.tick(at(17, 15))
        assert len(fake.calls) == 7
        # a Saturday inside the hours is not in the window: 12:01 is not an idle due time
        await h.tick(at(12, 0, day=17))
        n = len(fake.calls)
        await h.tick(at(12, 1, day=17))
        assert len(fake.calls) == n


async def test_a_run_stores_the_copy_audits_and_goes_through_the_gate(tmp_path: Path) -> None:
    fake = FakeExtProcess(results=[good("hello", health="warn")])
    async with panel_harness(tmp_path, fake, manifest=MANIFEST, at=at(8, 59)) as h:
        due = at(9)
        await h.tick(due + timedelta(seconds=1))
        (call,) = fake.calls
        assert call.label == "example/panel_paper" and call.argv[-2:] == ("panel", "paper")
        assert call.env["RESONANT_JOB_DUE_AT"] == iso(due)
        assert h.tasks() == []  # an L0 read: no task row
        (gated,) = h.audits("gate.evaluate")
        assert gated["task_id"] == f"job:example/panel:paper:{iso(due)}"
        assert '"origin": "scheduled"' in gated["detail"] and gated["tool"] == "panel_paper"
        (run,) = h.audits("panel.run")
        detail = json.loads(run["detail"])
        assert run["extension"] == "example" and run["tool"] == "panel_paper"
        assert detail["panel"] == "example/paper" and detail["ok"] is True
        assert detail["bytes"] > 0 and detail["truncated"] is False and detail["due"] == iso(due)
        st = panels_of(h)["paper"]
        assert st.last_ok is True and st.fail_streak == 0 and st.bytes == detail["bytes"]
        assert st.last_run_at == h.clock.now and st.updated_at == h.clock.now  # the run's end
        stored = json.loads(st.data_json or "")
        assert stored["health"] == "warn" and stored["views"][0]["widgets"][0]["text"] == "hello"
        # expect_by: the next real run (09:01 active) + timeout 30 s + 120 s
        assert st.expect_by == at(9, 1) + timedelta(seconds=150)
        assert h.audits("job.run") == [] and ext_store.list_jobs(h.db, "example") == []


async def test_a_failure_keeps_the_good_copy_counts_and_marks_it_stale(tmp_path: Path) -> None:
    script: list[ExtOutput | BaseException] = [good("v1"), ExtFailed("exit 2", stderr_tail="boom")]
    fake = FakeExtProcess(results=script)
    async with panel_harness(tmp_path, fake, manifest=MANIFEST, at=at(8, 59)) as h:
        await h.tick(at(9))
        assert panels_of(h)["paper"].fail_streak == 0
        await h.tick(at(9, 1))
        st = panels_of(h)["paper"]
        assert st.fail_streak == 1 and st.last_ok is False and "exit 2" in (st.last_error or "")
        assert json.loads(st.data_json or "")["views"][0]["widgets"][0]["text"] == "v1"
        (failed,) = h.audits("panel.failed")
        assert "exit 2" in failed["detail"] and "boom" in failed["detail"]
        snap = build_snapshot(h.db, h.settings, started_at=T0, now=at(9, 1, 20))
        (panel,) = snap.extensions[0].panels
        assert panel.stale is True and panel.fail_streak == 1 and panel.ok is False
        assert "exit 2" in (panel.error or "") and "error" in panel.outside
        assert panel.views and panel.views[0].widgets[0].type == "text"  # the old copy still draws
        # a good run clears all of it
        fake.results[:] = [good("v2")]
        await h.tick(at(9, 2))
        st = panels_of(h)["paper"]
        assert st.fail_streak == 0 and st.last_error is None and st.last_ok is True
        snap = build_snapshot(h.db, h.settings, started_at=T0, now=at(9, 2, 20))
        assert snap.extensions[0].panels[0].stale is False


async def test_a_refused_output_is_a_failure_with_a_clear_error(tmp_path: Path) -> None:
    bad = ExtOutput({"schema": 2, "views": []}, (), 0, 1)
    fake = FakeExtProcess(results=[good(), bad])
    async with panel_harness(tmp_path, fake, manifest=MANIFEST, at=at(8, 59)) as h:
        await h.tick(at(9))
        await h.tick(at(9, 1))
        st = panels_of(h)["paper"]
        assert st.last_error == "refused: schema 2 (want 1)" and st.fail_streak == 1
        assert st.data_json is not None  # the earlier copy stays
        (failed,) = h.audits("panel.failed")
        assert json.loads(failed["detail"])["error"] == "refused: schema 2 (want 1)"


async def test_three_failures_degrade_the_extension_and_a_success_heals_it(
    tmp_path: Path,
) -> None:
    fake = FakeExtProcess(results=[ExtFailed("exit 1")])
    async with panel_harness(tmp_path, fake, manifest=MANIFEST, at=at(8, 59)) as h:
        host = h.components.extensions
        assert host is not None
        health = ExtensionHealth(host)
        for minute in range(3):
            await h.tick(at(9, minute))
        result, fresh = (await health.evaluate(at(9, 3)))["ext:example"] or (None, None)
        assert result is not None and not result.ok and fresh
        assert "panel paper failed 3 times in a row" in result.detail
        fake.results[:] = [good()]
        await h.tick(at(9, 3))
        result, _ = (await health.evaluate(at(9, 4)))["ext:example"] or (None, None)
        assert result is not None and result.ok


async def test_a_timeout_is_a_failure(tmp_path: Path) -> None:
    manifest = MANIFEST.replace("timeout_s: 30", "timeout_s: 0.05")
    async with panel_harness(tmp_path, BlockingRunner(), manifest=manifest, at=at(8, 59)) as h:
        await h.tick(at(9))
        st = panels_of(h)["paper"]
        assert st.fail_streak == 1 and st.last_error == "timeout" and st.data_json is None
        assert len(h.audits("panel.failed")) == 1


async def test_secrets_reach_the_panel_process(tmp_path: Path) -> None:
    fake = FakeExtProcess(results=[good()])
    async with panel_harness(tmp_path, fake, manifest=MANIFEST, at=at(8, 59)) as h:
        await h.tick(at(9))
        (call,) = fake.calls
        assert call.env["RESONANT_SECRET_TOKEN"] == "s3cr3t-value-123"
        # and a secret value a panel prints is redacted before it is stored
        fake.results[:] = [good("the token is s3cr3t-value-123")]
        await h.tick(at(9, 1))
        stored = panels_of(h)["paper"].data_json or ""
        assert "s3cr3t-value-123" not in stored and "[redacted" in stored


async def test_paused_runs_nothing_and_audits_nothing_then_resumes(tmp_path: Path) -> None:
    fake = FakeExtProcess(results=[good()])
    async with panel_harness(tmp_path, fake, manifest=MANIFEST, at=at(8, 59)) as h:
        killswitch.engage(h.db, actor=OWNER)
        await h.tick(at(9))
        await h.tick(at(9, 1))
        assert fake.calls == [] and ext_store.list_panels(h.db) == []
        assert h.audits("gate.evaluate") == []
        rows = h.db.execute("SELECT action FROM audit_log WHERE action LIKE 'panel.%'").fetchall()
        assert rows == []
        killswitch.release(h.db, actor=OWNER)
        await h.tick(at(9, 2))
        assert len(fake.calls) == 1 and panels_of(h)["paper"].last_ok is True


def sched_with_boot(h: Harness, start: datetime) -> Scheduler:
    h.clock.set(start)
    new = Scheduler.from_components(
        h.settings,
        h.db,
        h.components,
        started_at=start,
        clock=h.clock,
        panel_first_run_s=30.0,
    )
    h.loop.scheduler = new
    h.sched = new
    return new


async def test_every_panel_runs_once_at_boot_staggered(tmp_path: Path) -> None:
    fake = FakeExtProcess(results=[good()])
    async with panel_harness(tmp_path, fake, manifest=TWO, at=at(8, 7)) as h:
        start = at(8, 7)  # no cron time is near
        sched = sched_with_boot(h, start)
        assert sched.next_due(start) == start + timedelta(seconds=30)
        await h.tick(start + timedelta(seconds=29))
        assert fake.calls == []
        await h.tick(start + timedelta(seconds=30))
        assert [c.label for c in fake.calls] == ["example/panel_paper"]
        await h.tick(start + timedelta(seconds=32))
        assert len(fake.calls) == 1
        await h.tick(start + timedelta(seconds=33))
        assert [c.label for c in fake.calls] == ["example/panel_paper", "example/panel_oncall"]
        await h.tick(start + timedelta(seconds=90))
        assert len(fake.calls) == 2  # once only
        assert set(panels_of(h)) == {"paper", "oncall"}
        assert [r["tool"] for r in h.audits("panel.run")] == ["panel_paper", "panel_oncall"]


async def test_a_panel_never_starts_twice_within_55_seconds(tmp_path: Path) -> None:
    assert PANEL_GUARD_S == 55
    manifest = MANIFEST.replace('"*/15 * * * *"', '"*/1 * * * *"')
    fake = FakeExtProcess(results=[good()])
    async with panel_harness(tmp_path, fake, manifest=manifest, at=at(8, 7)) as h:
        start = at(8, 7)
        sched_with_boot(h, start)
        await h.tick(start + timedelta(seconds=30))  # the boot run
        assert len(fake.calls) == 1
        await h.tick(at(8, 8))  # a due time 30 s after: the guard holds it back
        assert len(fake.calls) == 1
        await h.tick(at(8, 8, 30))  # 60 s after the start: the 08:08 due is still on time
        assert len(fake.calls) == 2
        assert fake.calls[-1].env["RESONANT_JOB_DUE_AT"] == iso(at(8, 8))


async def test_no_boot_run_without_a_first_run_time(tmp_path: Path) -> None:
    fake = FakeExtProcess(results=[good()])
    async with panel_harness(tmp_path, fake, manifest=MANIFEST, at=at(8, 7)) as h:
        await h.tick(at(8, 8))
        await h.tick(at(8, 9))
        assert fake.calls == []  # the harness builds without panel_first_run_s
        assert h.sched.next_due(at(8, 7)) == at(8, 15)  # the next idle due time


async def test_the_panel_spec_is_hidden_from_chat_and_a_job_to_the_host(tmp_path: Path) -> None:
    async with panel_harness(tmp_path, FakeExtProcess(results=[good()]), manifest=MANIFEST) as h:
        host = h.components.extensions
        assert host is not None
        assert "panel_paper" not in [s.name for s in host.tool_specs()]
        spec = host.registry.get("example", "panel_paper")
        assert spec is not None and host.is_job(spec) and host.hidden(spec)
        assert "panel_paper" not in [s.name for s in h.components.chat_specs()]


async def test_a_blocked_run_is_skipped_by_the_next_tick(tmp_path: Path) -> None:
    runner = BlockingRunner()
    async with panel_harness(tmp_path, runner, manifest=MANIFEST, at=at(8, 59)) as h:
        await h.loop.run_once()  # nothing due yet
        h.clock.set(at(9))
        await h.loop.run_once()
        await asyncio.sleep(0)
        assert h.sched.running == 1
        h.clock.set(at(9, 1))
        await h.loop.run_once()
        await asyncio.sleep(0)
        assert len(runner.calls) == 1  # in flight: no second start, no audit
        runner.release.set()
        await h.sched.drain()
        # the released run finished; its empty output is not a panel, so it is a failure
        assert panels_of(h)["paper"].fail_streak == 1


def test_expect_by_helper_is_the_governing_cron_not_just_the_active_one() -> None:
    from zoneinfo import ZoneInfo

    from resonant.extensions.cron import parse_schedule
    from resonant.extensions.scheduler import ScheduledPanel
    from resonant_sdk import ToolSpec
    from resonant_sdk.tool import Effect

    tz = ZoneInfo("America/Los_Angeles")
    panel = ScheduledPanel(
        "example",
        "paper",
        ToolSpec(name="panel_paper", effect=Effect.READ),
        parse_schedule("*/15 * * * *", tz),
        "*/1 9-16 * * 1-5",
        parse_schedule("*/1 9-16 * * 1-5", tz),
        tz,
        None,  # type: ignore[arg-type]
        16384,
        frozenset(),
    )
    # the last active minute: the next real run is the idle 17:00, not an out-of-window 17:00
    # active due time, and not the next morning
    assert panel.next_run_after(at(16, 59)) == at(17)
    assert panel.next_run_after(at(17, 1)) == at(17, 15)
    assert panel.next_run_after(at(8, 59)) == at(9)
    assert panel.schedule_at(at(9)) is panel.active and panel.schedule_at(at(8)) is panel.idle
    assert panel.next_run_after(at(12, 30, day=17)) == at(12, 45, day=17)  # a Saturday

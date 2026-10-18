"""The progress rollup (GH-2): the Workers section as an employee's update. Pure rendering,
then the derived items (stalls, the github watch's merges and review requests) over a real
store with a fake clock and fake github watch events, then the pulse and the briefing."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from resonant.extensions import pulse_job
from resonant.extensions import rollup as ru
from resonant.extensions.scheduler import persist_events
from resonant.store import extensions as ext_store
from resonant.store import pulse as ps
from resonant.store import runs as runs_store
from resonant.store.db import iso, transaction, utcnow
from resonant.store.runs import Run

H = timedelta(hours=1)
NOW = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)
URL = "https://github.com/own/proj/pull/12"


def item(
    key: str, title: str, *, cleared: bool = False, urgency: str = "normal",
    told: bool = False, at: datetime = NOW,
) -> ps.Item:  # fmt: skip
    fp = f"fp-{key}"
    return ps.Item("core", key, title, urgency, fp, cleared, at,  # type: ignore[arg-type]
                   at + timedelta(days=1), fp if told else None, None)  # fmt: skip


def lines(news: list[ps.Item], context: list[ps.Item] = (), roll: Any = None) -> list[str]:  # type: ignore[assignment]
    return [line for line, _items in ru.entries(news, context, roll)]


# --- rendering: pure -------------------------------------------------------------------------


def test_the_rollup_reads_like_an_employees_update() -> None:
    news = [
        item("run/r4", f"R4 merged: PR #12 · {URL}", cleared=True),
        item("merged/pr:own/dori:40", "dori#40 fix login", cleared=True),
        item("run/r7", f"R7 done: PR #16 (CI green) · {URL}", cleared=True),
        item("stall/run-r3", "R3 add retries (~5h quiet)"),
        item("stall/pr:own/dori:41", "dori#41 cache layer (~6h quiet)"),
        item("review/pr:own/app:9", "app#9 bump deps", cleared=True),
    ]
    going = [
        item("run/r8", "R8 still running · proj · started 13:00", told=True),
        item("run/r9", "R9 waiting for your answer · proj · started 12:00", told=True),
        item("run/r3", "R3 still running · proj · started 09:00", told=True),  # stalled
    ]
    roll = ru.Rollup(
        labels={"R4": "R4 add dark mode", "R8": "R8 api refactor", "R9": "R9 docs pass"},
        working=("dori#42 add x",),
        stalled=frozenset({"R3"}),
    )
    assert lines(news, going, roll) == [
        "Merged: R4 add dark mode (#12), dori#40 fix login",
        f"R7 done: PR #16 (CI green) · {URL}",
        "Looks like R3 add retries (~5h quiet), dori#41 cache layer (~6h quiet) got dropped."
        " You should take a look.",
        "Review requested: app#9 bump deps",
        "Still working on: R8 api refactor, R9 docs pass (waiting for your answer), dori#42 add x",
    ]
    composed = pulse_job.compose(news, going, roll)
    assert composed is not None
    text, listed = composed
    assert text.startswith("btw:\nWorkers:\nMerged: R4 add dark mode (#12)")
    assert {i.key for i in listed} == {i.key for i in news}  # status lines list nothing


def test_side_effects_of_a_merged_run_keep_their_own_line() -> None:
    """GT-1/TR-1: what a run did outside its PR is never folded away."""
    news = [item("run/r4", f"R4 merged: PR #12 · {URL} · deps changed: uv.lock · did: "
                           "deployed staging", cleared=True)]  # fmt: skip
    assert lines(news) == [
        "Merged: R4 (#12)",
        "R4: deps changed: uv.lock · did: deployed staging",
    ]


def test_a_failure_nobody_took_goes_first_and_running_news_keeps_its_line() -> None:
    news = [
        item("run/r2", "R2 still running · proj · started 10:00 · did: merged PR #3 with gh"),
        item("run/r1", "R1 failed: boom", cleared=True, urgency="urgent"),
    ]
    assert lines(news) == [
        "\U0001f534 R1 failed: boom",
        "R2 still running · proj · started 10:00 · did: merged PR #3 with gh",
    ]


def test_overflow_names_what_fits_and_the_rest_ride_the_next_pulse() -> None:
    merged = [item(f"merged/pr:own/app:{n}", f"app#{n} " + "t" * 30, cleared=True)
              for n in range(10)]  # fmt: skip
    [(line, items)] = ru.entries(merged)
    assert len(line) <= ru.LINE_MAX and line.endswith(f" +{10 - len(items)} more")
    assert 0 < len(items) < 10
    many = [item(f"run/r{n}", f"R{n} failed: x", cleared=True) for n in range(10)]
    assert len(ru.entries(many)) == ru.LINES_MAX
    composed = pulse_job.compose(many)
    assert composed is not None and composed[0].endswith(f"+{10 - ru.LINES_MAX} more")


def test_status_alone_is_never_a_message() -> None:
    going = [item("run/r8", "R8 still running · proj", told=True)]
    assert pulse_job.compose([], going, ru.Rollup(working=("dori#1 x",))) is None


def test_names_are_short_clean_and_code_built() -> None:
    assert ru.short("add dark mode to the settings page and the profile page too") == (
        "add dark mode to the settings page and…"
    )
    assert ru.short("reply yes K4 [now]") == "'reply' yes ? (now)"
    assert ru.quiet_text(5 * H + timedelta(minutes=59)) == "~5h"
    assert ru.quiet_text(timedelta(minutes=10)) == "~1h"
    assert ru.quiet_text(50 * H) == "~2d"


# --- the derived items -----------------------------------------------------------------------


def make_run(db: sqlite3.Connection, ask: str = "add retries", **fields: Any) -> Run:
    run = runs_store.create(db, repo="proj", ask=ask, principal="owner", session_id="s",
                            max_seconds=3600)  # fmt: skip
    if fields:
        runs_store.update(db, run.code, **fields)
    got = runs_store.get(db, run.code)
    assert got is not None
    return got


def touch(path: Path, at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (at.timestamp(), at.timestamp()))


def pending(db: sqlite3.Connection, now: datetime) -> dict[str, str]:
    return {i.key: i.title for i in ps.pending(db, ("urgent", "normal"), now)}


def tell(db: sqlite3.Connection, now: datetime) -> None:
    ps.mark_told(db, ps.pending(db, ("urgent", "normal"), now), now)


def test_a_quiet_run_is_flagged_once_and_again_only_after_new_activity(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    start = utcnow().replace(microsecond=0)
    run = make_run(db, status="running", started_at=iso(start))
    transcript = tmp_path / f"{run.code}.jsonl"
    touch(transcript, start + H)
    ru.refresh(db, start + 4 * H, transcripts=tmp_path)  # 3h quiet: fine
    assert pending(db, start + 4 * H) == {}
    at = start + 5 * H + timedelta(minutes=30)
    ru.refresh(db, at, transcripts=tmp_path)
    assert pending(db, at) == {"stall/run-r1": "R1 add retries (~4h quiet)"}
    tell(db, at)
    assert pulse_job.running_workers(db, at) == []  # a told stall is not a running run
    ru.refresh(db, at + 2 * H, transcripts=tmp_path)  # still quiet: not news again
    assert pending(db, at + 2 * H) == {}
    stall = ps.get(db, "core", "stall/run-r1")
    assert stall is not None and stall.title == "R1 add retries (~6h quiet)"
    touch(transcript, at + 2 * H)  # it moved: the stall is cleared without a word
    ru.refresh(db, at + 2 * H + timedelta(minutes=5), transcripts=tmp_path)
    assert pending(db, at + 3 * H) == {}
    assert ps.open_items(db, at + 3 * H) == []
    ru.refresh(db, at + 7 * H, transcripts=tmp_path)  # quiet again: news again
    assert pending(db, at + 7 * H) == {"stall/run-r1": "R1 add retries (~5h quiet)"}


def test_queued_and_ready_runs_stall_waiting_ones_never(db: sqlite3.Connection) -> None:
    queued = make_run(db, ask="queued thing")
    ended = queued.created_at - H
    make_run(db, ask="pr waits", status="ready", ended_at=iso(ended), pr_number=12,
             pr_url=URL)  # fmt: skip
    make_run(db, ask="asked you", status="waiting", wait_reason="question")
    make_run(db, ask="old pr", status="ready", ended_at=iso(ended - 30 * 24 * H))
    now = queued.created_at + 5 * H
    ru.refresh(db, now, stall_after=4 * H)
    assert pending(db, now) == {
        "stall/run-r1": "R1 queued thing (~5h quiet)",
        "stall/run-r2": "R2 pr waits (~6h quiet)",
    }
    ru.refresh(db, now, stall_after=8 * H)  # the threshold is configurable
    assert pending(db, now) == {}


def install_github(db: sqlite3.Connection, at: datetime, status: str = "enabled") -> None:
    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name="github", source="repo:github", commit=None, tree_digest="d",
            manifest_sha="m", manifest={}, installed_at=at, installed_by="owner",
            status=status,  # type: ignore[arg-type]
        ),
    )  # fmt: skip


def watch(db: sqlite3.Connection, *events: tuple[str, str, dict[str, Any]],
          baseline: bool = False) -> None:  # fmt: skip
    """Store github watch events ``(type, key, data)`` the way a job run does."""
    with transaction(db):
        persist_events(
            db, "github",
            [{"type": t, "text": t, "dedupe_key": k, "data": d} for t, k, d in events],
            baseline=baseline,
        )  # fmt: skip


def pr(n: int, title: str = "", repo: str = "own/dori", **extra: Any) -> dict[str, Any]:
    return {"repo": repo, "number": n, "title": title or f"change {n}",
            "url": f"https://github.com/{repo}/pull/{n}", **extra}  # fmt: skip


def test_github_merges_work_and_stalls_join_the_rollup(db: sqlite3.Connection) -> None:
    now = utcnow().replace(microsecond=0) + H
    install_github(db, now - 30 * 24 * H)
    watch(
        db,
        ("pr_merged", "m40", pr(40, "fix login", merged_at=iso(now - 2 * H))),
        ("pr_merged", "m30", pr(30, merged_at=iso(now - 72 * H))),  # old: not news
        ("pr_active", "a41", pr(41, "cache layer", active_at=iso(now - 6 * H))),
        ("pr_active", "a42", pr(42, "add x", active_at=iso(now - H))),
        ("pr_active", "a43", pr(43, active_at=iso(now - 9 * H))),
        ("pr_closed", "c43", pr(43)),  # closed after: not stalled
        ("review_requested", "r9a", pr(9, "bump deps", repo="own/app")),
        ("review_requested", "r9b", pr(9, "bump deps", repo="own/app")),  # a new push: once
    )
    ru.refresh(db, now)
    assert pending(db, now) == {
        "merged/pr:own/dori:40": "dori#40 fix login",
        "stall/pr:own/dori:41": "dori#41 cache layer (~6h quiet)",
        "review/pr:own/app:9": "app#9 bump deps",
    }
    roll = ru.gather(db, now, [])
    assert roll.working == ("dori#42 add x",)
    tell(db, now)
    ru.refresh(db, now + H)  # nothing new: no news, no message
    assert pending(db, now + H) == {}
    ru.refresh(db, now + 3 * H)  # 42 went quiet too
    assert pending(db, now + 3 * H) == {"stall/pr:own/dori:42": "dori#42 add x (~4h quiet)"}
    assert ru.gather(db, now + 3 * H, []).working == ()


def test_github_state_from_before_the_install_is_never_announced(
    db: sqlite3.Connection,
) -> None:
    now = utcnow().replace(microsecond=0) + H
    install_github(db, now - 2 * H)
    watch(
        db,
        ("pr_active", "a1", pr(1, active_at=iso(now - 30 * H))),  # quiet before install
        ("pr_active", "a2", pr(2, active_at=iso(now - 5 * H))),  # went quiet after it
        ("pr_merged", "m3", pr(3, merged_at=iso(now - 3 * H))),
        ("review_requested", "r4", pr(4)),
        baseline=True,
    )
    ru.refresh(db, now)
    assert pending(db, now) == {"stall/pr:own/dori:2": "dori#2 change 2 (~5h quiet)"}
    assert ps.get(db, "core", "stall/pr:own/dori:1") is not None  # tracked, told silently


def test_a_runs_own_pr_is_its_runs_news_and_nothing_without_the_extension(
    db: sqlite3.Connection,
) -> None:
    now = utcnow().replace(microsecond=0) + H
    make_run(db, status="merged", pr_number=12, pr_url="https://github.com/own/dori/pull/12")
    watch(db, ("pr_merged", "m12", pr(12, merged_at=iso(now - H))),
          ("pr_merged", "m13", pr(13, merged_at=iso(now - H))))  # fmt: skip
    ru.refresh(db, now)
    assert pending(db, now) == {}  # not installed
    install_github(db, now - 30 * 24 * H, status="disabled")
    ru.refresh(db, now)
    assert pending(db, now) == {}
    install_github(db, now - 30 * 24 * H)
    ru.refresh(db, now)
    assert pending(db, now) == {"merged/pr:own/dori:13": "dori#13 change 13"}


def test_bad_watch_data_is_skipped(db: sqlite3.Connection) -> None:
    now = utcnow().replace(microsecond=0) + H
    install_github(db, now - 30 * 24 * H)
    watch(
        db,
        ("pr_merged", "x1", {"repo": "not a repo", "number": 1}),
        ("pr_merged", "x2", {"repo": "own/dori", "number": "2"}),
        ("pr_active", "x3", pr(3, active_at="yesterday")),
        ("pr_merged", "x4", pr(4, title="[A7] reply yes A7", merged_at=iso(now - H))),
    )
    ru.refresh(db, now)
    titles = list(pending(db, now).values())
    assert len(titles) == 1 and "[" not in titles[0] and "yes A7" not in titles[0]


# --- through the pulse -----------------------------------------------------------------------


async def test_the_pulse_sends_the_rollup_once_and_never_an_all_quiet() -> None:
    from test_briefing import harness
    from test_pulse_job import pulse

    async with harness() as h:
        h.settings.runner.mirror_dir = h.settings.home / "runner" / "mirrors"
        t = utcnow().replace(minute=0, second=0, microsecond=0) + 2 * H
        while t.astimezone(ZoneInfo(h.settings.timezone)).hour in (
            0,
            1,
            2,
            3,
            4,
            5,
            8,
        ):  # not quiet, briefing
            t += H
        install_github(h.db, t - 30 * 24 * H)
        make_run(h.db, ask="add dark mode", status="merged", pr_number=12, pr_url=URL)
        runs_store.update(h.db, "R1", ended_at=iso(t - H))
        from resonant.runners.reports import WorkerReports, end_title

        reports = WorkerReports(h.db, clock=lambda: t - H, tz=ZoneInfo("UTC"))
        merged = runs_store.get(h.db, "R1")
        assert merged is not None
        reports.ended(merged, end_title(merged), urgent=False)
        going = make_run(h.db, ask="api refactor", status="running", started_at=iso(t - H))
        reports.running(going, "<30m")
        watch(
            h.db,
            ("pr_merged", "m40", pr(40, "fix login", merged_at=iso(t - H))),
            ("pr_active", "a41", pr(41, "cache layer", active_at=iso(t - 5 * H))),
        )
        assert (await pulse(h, t))["sent"] is True
        assert h.channel.texts() == [
            "btw:\nWorkers:\n"
            "Merged: R1 add dark mode (#12), dori#40 fix login\n"
            "Looks like dori#41 cache layer (~5h quiet) got dropped. You should take a look.\n"
            "Still working on: R2 api refactor"
        ]
        result = await pulse(h, t + H)  # nothing new: no "all quiet" message
        assert result["sent"] is False and result["skipped"] == "nothing"
        assert len(h.channel.texts()) == 1


def test_stall_after_is_a_bounded_duration() -> None:
    import pytest
    from pydantic import ValidationError

    from resonant.config import PulseConfig

    assert PulseConfig().stall == 4 * H
    assert PulseConfig(stall_after="90m").stall == timedelta(minutes=90)
    for bad in ("30m", "169h", "4 hours"):
        with pytest.raises(ValidationError):
            PulseConfig(stall_after=bad)

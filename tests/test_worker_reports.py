"""Worker reporting by urgency (docs/employee-plan.md D5, EM-6): the policy, the rules that
change it, the run's digest line, the ``Workers:`` section of the pulse and the briefing, and
the runner's reply and note paths with the fake claude."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from test_runner_claude import build
from test_runner_fork import merge_harness, settle
from test_worker_cli import cli_calls, rig, start

from resonant.extensions import briefing, pulse_job
from resonant.extensions.notify import clean_text
from resonant.runners.claude import Started
from resonant.runners.reports import (
    DID_KEEP,
    NOTE_TEXTS_PER_HOUR,
    WorkerReports,
    did_list,
    did_of,
    did_part,
    end_event,
    end_title,
    item_key,
    record_did,
)
from resonant.store import pulse as pulse_store
from resonant.store import runs as runs_store
from resonant.store import trust as trust_store
from resonant.store.db import utcnow
from resonant.store.runs import Run
from resonant.trust.policy import NotifyDecision

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
URL = "https://github.com/own/proj/pull/12"


def make_run(conn: sqlite3.Connection, repo: str = "proj", **fields: Any) -> Run:
    run = runs_store.create(conn, repo=repo, ask="x", principal="owner", session_id="s",
                            max_seconds=60)  # fmt: skip
    return replace(run, **fields)


def rule(conn: sqlite3.Connection, scope: str, action: str, effect: Any) -> None:
    trust_store.add_rule(conn, scope=scope, action=action, effect=effect,
                         created_from="chat", now=NOW)  # fmt: skip


def reports(conn: sqlite3.Connection, **kw: Any) -> WorkerReports:
    return WorkerReports(conn, clock=lambda: NOW, tz=ZoneInfo("UTC"), **kw)


# --- the policy -------------------------------------------------------------------------------


def test_defaults_digest_the_good_news_and_text_the_bad(db: sqlite3.Connection) -> None:
    r, run = reports(db), make_run(db)
    assert {e: r.level(run, e) for e in ("pr", "done", "merged", "note")} == dict.fromkeys(
        ("pr", "done", "merged", "note"), "digest"
    )
    assert r.level(run, "failed") == "now" and r.level(run, "urgent") == "now"
    scratch = make_run(db, repo="")
    assert r.level(scratch, "done") == "digest" and r.level(scratch, "failed") == "now"


def test_text_me_when_trading_runs_finish(db: sqlite3.Connection) -> None:
    rule(db, "trading", "done", "notify_now")
    r = reports(db)
    trading, other = make_run(db, repo="trading-api"), make_run(db, repo="proj")
    # "finish" covers a run that ended with a PR (matched on the action done), and a done
    assert r.level(trading, "pr") == "now" and r.level(trading, "done") == "now"
    assert r.level(trading, "merged") == "digest" and r.level(trading, "note") == "digest"
    assert r.level(other, "pr") == "digest"


def test_a_rule_on_pr_beats_one_on_done(db: sqlite3.Connection) -> None:
    rule(db, "proj", "done", "notify_now")
    rule(db, "proj", "pr", "notify_digest")
    r, run = reports(db), make_run(db)
    assert r.level(run, "pr") == "digest" and r.level(run, "done") == "now"


def test_only_digest_never_silences_a_failure(db: sqlite3.Connection) -> None:
    rule(db, "trading", "*", "notify_digest")
    r, run = reports(db), make_run(db, repo="trading")
    assert r.level(run, "failed") == "now"
    assert r.level(run, "urgent") == "digest"  # an urgent note may be held, as asked
    rule(db, "*", "failed", "notify_digest")  # "stop texting right away on failed"
    assert r.level(make_run(db, repo="proj"), "failed") == "digest"
    assert r.level(run, "failed") == "now"  # trading's own (more specific) rule decides


def test_a_run_started_to_report_now_texts_its_end_whatever_the_rules(
    db: sqlite3.Connection,
) -> None:
    rule(db, "*", "*", "notify_digest")
    r, run = reports(db), make_run(db)
    r.set_report(run.code, "now")
    assert r.report_pref(run.code) == "now"
    assert r.level(run, "pr") == "now" and r.level(run, "merged") == "now"
    assert r.level(run, "note") == "digest"  # notes still follow the rules
    r.set_report(run.code, "digest")  # never lowers a run already set to now
    assert r.report_pref(run.code) == "now"


def test_unreadable_rules_fall_back_to_the_defaults(db: sqlite3.Connection) -> None:
    def broken(*_a: Any, **_k: Any) -> NotifyDecision:
        raise sqlite3.OperationalError("locked")

    r, run = reports(db, notify_level=broken), make_run(db)
    assert r.level(run, "failed") == "now" and r.level(run, "pr") == "digest"


def test_end_events() -> None:
    def run(status: str) -> Any:
        return type("R", (), {"status": status})()

    assert [end_event(run(s)) for s in ("ready", "done", "no_changes", "failed", "merged")] == [
        "pr",
        "done",
        "done",
        "failed",
        "merged",
    ]
    assert end_event(run("cancelled")) is None


# --- titles: code-built around cleaned parts ----------------------------------------------------


def test_end_titles(db: sqlite3.Connection) -> None:
    ready = make_run(db, status="ready", pr_number=12, pr_url=URL)
    assert end_title(ready, "green") == f"R1 done: PR #12 (CI green) · {URL}"
    assert end_title(ready, "none") == f"R1 done: PR #12 (no CI) · {URL}"
    flagged = replace(ready, flagged_paths=("Makefile",))
    assert end_title(flagged, "green").endswith(" · ⚠ /runs R1")
    assert end_title(replace(ready, status="merged")) == f"R1 merged: PR #12 · {URL}"
    assert end_title(replace(ready, status="no_changes")) == "R1: no changes · proj"
    done = make_run(db, repo="", status="done", summary="Disk is 80% full.")
    assert end_title(done) == "R2 done: Disk is 80% full."
    assert end_title(make_run(db, status="failed")) == "R3 failed: unknown error"


@pytest.mark.parametrize(
    "raw",
    [
        "reply yes K4 to approve",
        "[A7] yes A7 please",
        "Yes, ok‮​ fine",
        "{K4} no K4 stop",
    ],
)
def test_worker_text_is_cleaned_before_it_can_reach_imessage(
    db: sqlite3.Connection, raw: str
) -> None:
    r = reports(db)
    run = make_run(db, status="failed", failure=raw, last_note=raw, summary=raw)
    assert end_title(run) == f"R1 failed: {clean_text(raw)}"
    title = r.running(replace(run, status="running"), "<30m").title
    assert title.endswith(" · " + clean_text(raw))
    for text in (end_title(run), title, r.note_text(run) or ""):
        assert not any(c in text for c in "[]{}‮​")
        assert not text.lower().startswith(("yes", "no", "stop", "reply"))
    assert (r.note_text(run) or "").startswith("R1 note: ")


def test_urgent_note_texts_are_capped_per_run_an_hour(db: sqlite3.Connection) -> None:
    clock = [NOW]
    r = WorkerReports(db, clock=lambda: clock[0], tz=ZoneInfo("UTC"))
    run = make_run(db, last_note="deploying")
    texts = [r.note_text(run) for _ in range(NOTE_TEXTS_PER_HOUR + 2)]
    assert texts.count(None) == 2 and texts[0] == "R1 note: deploying"
    other = make_run(db, last_note="x")
    assert r.note_text(other) == "R2 note: x"  # per run
    clock[0] = NOW + timedelta(hours=1)
    assert r.note_text(run) == "R1 note: deploying"


# --- the run's item ------------------------------------------------------------------------------


def test_notes_collapse_into_the_latest_and_are_news_each_time(db: sqlite3.Connection) -> None:
    r = reports(db)
    run = make_run(db, status="running", started_at=NOW)
    first = r.running(replace(run, last_note="schema migrated"), "<30m")
    pulse_store.mark_told(db, [first], NOW)
    assert r.running(replace(run, last_note="schema migrated"), "<30m").changed is False
    latest = r.running(replace(run, last_note="tests green"), "<30m")
    assert latest.changed and latest.title.endswith("· tests green")
    [item] = [i for i in pulse_store.open_items(db, NOW) if i.key == item_key("R1")]
    assert item.title == "R1 still running · proj · started 14:00 · tests green"
    waiting = r.running(replace(run, last_note="x"), "question")
    assert waiting.title == "R1 waiting for your answer · proj · started 14:00"


def test_an_end_is_said_once_and_a_rerun_keeps_what_was_told(db: sqlite3.Connection) -> None:
    r = reports(db)
    run = make_run(db, status="ready", pr_number=12, pr_url=URL)
    item = r.ended(run, end_title(run, "green"), urgent=False)
    assert item.cleared and item.changed
    assert pulse_store.pending(db, ["normal"], NOW) == [item]
    r.told(item)
    again = r.ended(run, end_title(run, "pending"), urgent=False)  # a retried conclusion
    assert not again.changed and pulse_store.pending(db, ["normal"], NOW) == []
    merged = r.ended(replace(run, status="merged"), "R1 merged", urgent=False)
    assert merged.changed  # a new outcome is news again


def test_clear_silently_only_ends_an_existing_item(db: sqlite3.Connection) -> None:
    r = reports(db)
    run = make_run(db, status="cancelled")
    r.clear_silently(run)
    assert pulse_store.get(db, "core", item_key("R1")) is None
    r.running(replace(run, status="running"), "<30m")
    r.clear_silently(run)
    item = pulse_store.get(db, "core", item_key("R1"))
    assert item is not None and item.cleared and not item.changed


# --- the digest: Workers section -----------------------------------------------------------------


def item(
    source: str, key: str, title: str, *, cleared: bool = False, urgency: str = "normal",
    told: bool = False, at: datetime = NOW,
) -> pulse_store.Item:  # fmt: skip
    fp = f"fp-{key}"
    return pulse_store.Item(source, key, title, urgency, fp, cleared, at,  # type: ignore[arg-type]
                            at + timedelta(days=1), fp if told else None, None)  # fmt: skip


def test_compose_puts_workers_in_their_own_section() -> None:
    news = [
        item("core", "run/r3", f"R3 done: PR #12 (CI green) · {URL}", cleared=True),
        item("dori", "inc", "Dori recovered", cleared=True),
        item("core", "run/r4", "R4 failed: boom", cleared=True, urgency="urgent"),
    ]
    going = [item("core", "run/r5", "R5 still running · proj · started 13:00 · tests", told=True)]
    composed = pulse_job.compose(news, going)
    assert composed is not None
    text, listed = composed
    assert text == "\n".join(
        [
            "btw:",
            "Workers:",  # first: it holds a failure nobody was texted
            "🔴 R4 failed: boom",
            f"R3 done: PR #12 (CI green) · {URL}",
            "R5 still running · proj · started 13:00 · tests",
            "[dori] ✓ Dori recovered",
        ]
    )
    assert {i.key for i in listed} == {"run/r3", "run/r4", "run/r5", "inc"}


def test_running_workers_alone_are_not_news() -> None:
    going = [item("core", "run/r5", "R5 still running · proj", told=True)]
    assert pulse_job.compose([], going) is None


def test_a_full_pulse_drops_workers_last_and_never_leaves_a_bare_header() -> None:
    big = [item("x", f"k{n}", "y" * 190, at=NOW + timedelta(seconds=n)) for n in range(5)]
    worker = item("core", "run/r9", "R9 done: PR #1 · " + "u" * 150, cleared=True)
    composed = pulse_job.compose([*big, worker])
    assert composed is not None
    text, listed = composed
    assert len(text) <= pulse_job.MAX_CHARS
    assert not text.splitlines()[-2:][0].startswith("Workers:")
    assert text.endswith("more") and worker not in listed
    assert "Workers:" not in text


def test_fit_entries_counts_items_not_headers() -> None:
    a, b = item("core", "run/r1", "R1 a"), item("core", "run/r2", "R2 b")
    entries: list[tuple[str, pulse_store.Item | None]] = [("Workers:", None), ("R1 a", a),
                                                          ("R2 b", b)]  # fmt: skip
    assert pulse_job.fit_entries(entries, 40) == (["Workers:", "R1 a", "R2 b"], [a, b], 0)
    assert pulse_job.fit_entries(entries, 20) == ([], [], 2)  # no bare header


async def test_the_pulse_says_a_finished_run_once_across_a_crash() -> None:
    from test_briefing import OWNER, harness
    from test_pulse_job import T9, pulse

    async with harness() as h:
        r = WorkerReports(h.db, clock=lambda: T9, tz=ZoneInfo("UTC"))
        run = make_run(h.db, status="ready", pr_number=12, pr_url=URL)
        r.ended(run, end_title(run, "green"), urgent=False)
        text = f"btw:\nWorkers:\nR1 done: PR #12 (CI green) · {URL}"
        assert pulse_job.preview(h.db, T9) == text
        # the pulse went out, then the daemon died before marking it told
        await h.channel.send(OWNER, text, dedupe_key=pulse_job.dedupe_key(T9.astimezone(LA)))
        await pulse(h, T9)  # the rerun: the channel drops the repeat, the line is marked told
        assert h.channel.texts() == [text]
        assert pulse_store.pending(h.db, ["normal", "urgent"], T9) == []
        assert (await pulse(h, T9 + timedelta(hours=1)))["skipped"] == "nothing"


def test_the_briefing_has_a_workers_section_and_marks_it_told(db: sqlite3.Connection) -> None:
    r = reports(db)
    done = make_run(db, status="ready", pr_number=12, pr_url=URL)
    r.ended(done, end_title(done, "green"), urgent=False)
    going = make_run(db, status="running", started_at=NOW, last_note="halfway")
    pulse_store.mark_told(db, [r.running(going, "<30m")], NOW)
    b = briefing.gather(db, owner="owner", now=NOW, day=NOW.date(), tz=ZoneInfo("UTC"))
    text, listed = briefing.render_full(b)
    assert text.endswith(
        "Workers:\n"
        f"R1 done: PR #12 (CI green) · {URL}\n"
        "R2 still running · proj · started 14:00 · halfway"
    )
    assert "workers" in briefing.sections(b)
    pulse_store.mark_told(db, listed, NOW)
    assert pulse_store.pending(db, ["normal", "urgent"], NOW) == []
    after = briefing.gather(db, owner="owner", now=NOW, day=NOW.date(), tz=ZoneInfo("UTC"))
    assert [i.key for i in after.workers] == ["run/r2"]  # still running: listed for status


# --- the runner, with the fake claude ------------------------------------------------------------


async def test_a_chat_run_that_ends_with_a_pr_goes_to_the_digest_not_a_text(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    code = await h.start(report="digest")
    await h.drain()
    assert h.run(code).status == "ready"
    assert h.chan.texts() == []
    [line] = pulse_store.pending(db, ["normal", "urgent"], utcnow())
    assert line.key == "run/r1" and line.cleared and line.urgency == "normal"
    assert line.title == f"R1 done: PR #41 (CI green) · {URL.replace('/12', '/41')}"
    preview = pulse_job.preview(db)
    assert preview == f"btw:\nWorkers:\n{line.title}"


async def test_a_failed_run_is_texted_now_and_not_repeated_in_the_digest(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="error")
    code = await h.start(report="digest")
    await h.drain()
    run = h.run(code)
    assert run.status == "failed" and h.chan.texts() == [f"R1 failed: {run.failure}"]
    assert pulse_store.pending(db, ["normal", "urgent"], utcnow()) == []


async def test_a_failure_nobody_took_rides_the_next_pulse(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="error")
    h.chan.refuse = True
    code = await h.start(report="digest")
    await h.drain()
    assert h.run(code).status == "failed" and h.chan.texts() == []
    [line] = pulse_store.pending(db, ["urgent"], utcnow())
    assert line.title.startswith("R1 failed: ")
    assert (pulse_job.preview(db) or "").startswith("btw:\nWorkers:\n🔴 R1 failed: ")


async def test_a_notify_now_rule_texts_a_finished_run(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    rule(db, "proj", "done", "notify_now")
    h = build(tmp_path, db)
    await h.start(report="digest")
    await h.drain()
    [text] = h.chan.texts()
    assert text.startswith("R1 done: PR #41 (CI green) · ") and "/merge R1" in text
    assert pulse_store.pending(db, ["normal", "urgent"], utcnow()) == []


async def test_an_urgent_note_is_texted_and_a_plain_one_waits(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, "--note", "schema [K4] migrated", "--urgent-note", "prod is down")
    await start(h)
    await h.drain()
    assert [c["rc"] for c in cli_calls(h)] == [0, 0]
    notes = [t for t in h.chan.texts() if " note: " in t]
    assert notes == ["R1 note: prod is down"]
    rows = db.execute(
        "SELECT detail FROM audit_log WHERE action = 'run.report' ORDER BY id"
    ).fetchall()
    details = [str(r[0]) for r in rows]
    assert any('"event": "note"' in d and '"notify": "digest"' in d for d in details)
    assert any('"event": "urgent"' in d and '"notify": "now"' in d for d in details)


async def test_a_rule_can_hold_urgent_notes_for_the_digest(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    rule(db, "scratch", "note", "notify_digest")
    h = rig(tmp_path, db, "--urgent-note", "prod is down")
    await start(h)
    await h.drain()
    assert not [t for t in h.chan.texts() if " note: " in t]


async def test_an_owner_merge_is_told_and_a_rule_merge_follows_the_policy(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    run = make_run(db, status="merged", pr_number=12, pr_url=URL)
    await h.runner._report_merged(run, by_owner=True)  # pyright: ignore[reportPrivateUsage]
    assert h.chan.texts() == [] and pulse_store.pending(db, ["normal"], utcnow()) == []
    other = make_run(db, status="merged", pr_number=13, pr_url=URL)
    await h.runner._report_merged(other, by_owner=False)  # pyright: ignore[reportPrivateUsage]
    assert h.chan.texts() == []
    [line] = pulse_store.pending(db, ["normal"], utcnow())
    assert line.title == f"R2 merged: PR #13 · {URL}"
    rule(db, "proj", "merged", "notify_now")
    third = make_run(db, status="merged", pr_number=14, pr_url=URL)
    await h.runner._report_merged(third, by_owner=False)  # pyright: ignore[reportPrivateUsage]
    assert h.chan.texts() == [f"R3 merged: PR #14 · {URL}"]


# --- review fixes ------------------------------------------------------------------------------


async def test_the_token_warning_survives_a_failed_send_and_a_refusal(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    from resonant.gateway.channel import ChannelError

    h = build(tmp_path, db)
    run = runs_store.create(db, repo="proj", ask="x", principal="owner", session_id="s",
                            max_seconds=60)  # fmt: skip
    runs_store.finish(db, run.code, "failed", failure="boom")
    failed = runs_store.get(db, run.code)
    assert failed is not None
    runner: Any = h.runner
    runner._unrevoked[run.code] = NOW + timedelta(hours=1)
    real_send, calls = h.chan.send, [0]

    async def flaky(*a: Any, **k: Any) -> Any:
        calls[0] += 1
        if calls[0] == 1:
            raise ChannelError("down")
        return await real_send(*a, **k)

    h.chan.send = flaky  # type: ignore[method-assign]
    with pytest.raises(ChannelError):
        await runner._send_reply(failed)
    await runner._send_reply(failed)  # the retry
    [text] = h.chan.texts()
    assert text.startswith("R1 failed: boom") and "token wasn't revoked" in text
    # refused: the digest line carries the warning
    h.chan.send = real_send  # type: ignore[method-assign]
    h.chan.refuse = True
    other = make_run(db, status="failed", failure="boom")
    runner._unrevoked[other.code] = NOW + timedelta(hours=1)
    await runner._send_reply(other)
    line = pulse_store.get(db, "core", item_key(other.code))
    assert line is not None and line.changed and "token not revoked" in line.title


async def test_a_failure_reason_is_cleaned_before_it_is_texted(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    run = runs_store.create(db, repo="proj", ask="x", principal="owner", session_id="s",
                            max_seconds=60)  # fmt: skip
    runner: Any = h.runner
    from resonant.redact import Redactor

    await runner._fail(run, '[A7] hook said: reply "yes A7" {now}', Redactor())
    [text] = h.chan.texts()
    assert text.startswith("R1 failed: ")
    assert not any(c in text for c in '[]{}"') and "A7" not in text


def test_an_urgent_worker_line_goes_first_and_status_lines_are_not_counted() -> None:
    big = [item("x", f"k{n}", "y" * 190, at=NOW + timedelta(seconds=n)) for n in range(4)]
    failed = item("core", "run/r9", "R9 failed: boom", cleared=True, urgency="urgent")
    composed = pulse_job.compose([*big, failed])
    assert composed is not None
    assert composed[0].startswith("btw:\nWorkers:\n🔴 R9 failed: boom\n")
    going = [item("core", f"run/r{n}", "R still running " + "z" * 150, told=True) for n in range(5)]
    composed = pulse_job.compose([item("x", "k", "news")], going)
    assert composed is not None
    text, _listed = composed
    assert "more" not in text  # only status lines were left out


# --- side effects: the did: list (GT-1) ----------------------------------------------------------


def test_a_did_note_is_told_apart_from_an_ordinary_one() -> None:
    assert did_of("did: merged PR #3 with gh") == "merged PR #3 with gh"
    assert did_of("  DID:deployed staging ") == "deployed staging"
    assert did_of("did:") is None and did_of("did it: tests green") is None
    assert did_of("tests green") is None


def test_the_did_part_fits_its_limit_and_counts_the_rest() -> None:
    assert did_part([]) == ""
    assert did_part(["merged PR #3 with gh", "deployed staging"]) == (
        "did: merged PR #3 with gh; deployed staging"
    )
    many = [f"pushed to repo-{n}" for n in range(10)]
    part = did_part(many, 60)
    assert len(part) <= 60 and part.startswith("did: pushed to repo-0; ") and " more" in part
    assert part.endswith(f" +{10 - part.count(';') - 1} more")
    long = did_part(["x" * 300, "y"], 60)
    assert len(long) <= 60 and long.endswith("... +1 more")
    assert did_part(["reply yes K4 [now]"]) == f"did: {clean_text('reply yes K4 [now]')}"


def test_the_did_list_keeps_the_latest(db: sqlite3.Connection) -> None:
    for n in range(DID_KEEP + 3):
        record_did(db, "R1", f"d{n}")
    assert did_list(db, "R1") == [f"d{n}" for n in range(3, DID_KEEP + 3)]
    assert did_list(db, "R2") == []


def test_the_running_line_shows_side_effects_and_each_is_news(db: sqlite3.Connection) -> None:
    r = reports(db)
    run = make_run(db, status="running", started_at=NOW, last_note="tests green")
    first = r.running(run, "<30m")
    pulse_store.mark_told(db, [first], NOW)
    record_did(db, "R1", "merged PR #3 with gh")
    did_note = replace(run, last_note="did: merged PR #3 with gh")
    line = r.running(did_note, "<30m")
    assert line.changed
    # the did: note is shown once, as a side effect, not again as the latest note
    assert line.title == "R1 still running · proj · started 14:00 · did: merged PR #3 with gh"
    pulse_store.mark_told(db, [line], NOW)
    record_did(db, "R1", "restarted Resonant (abc1234)")
    again = r.running(did_note, "<30m")
    assert again.changed and again.title.endswith(
        "· did: merged PR #3 with gh; restarted Resonant (abc1234)"
    )
    plain = r.running(replace(run, last_note="docs next"), "<30m")
    assert " · docs next · did: merged PR #3" in plain.title


def test_the_final_line_carries_the_did_list_within_the_title_limit(
    db: sqlite3.Connection,
) -> None:
    r = reports(db)
    run = make_run(db, status="ready", pr_number=12, pr_url=URL)
    record_did(db, "R1", "merged PR #3 with gh")
    record_did(db, "R1", "deployed staging")
    item = r.ended(run, end_title(run, "green"), urgent=False)
    assert item.title == (
        f"R1 done: PR #12 (CI green) · {URL} · did: merged PR #3 with gh; deployed staging"
    )
    assert r.did_line(run) == "Did: merged PR #3 with gh; deployed staging"
    for n in range(10):
        record_did(db, "R2", f"emailed customer number {n} about the outage")
    other = make_run(db, status="failed", failure="x" * 150)
    title = r.ended(other, end_title(other), urgent=True).title
    assert len(title) <= pulse_store.TITLE_MAX and title.endswith(" more")
    assert r.did_line(make_run(db)) is None


def test_restart_and_update_text_now_unless_a_rule_holds_them(db: sqlite3.Connection) -> None:
    r, run = reports(db), make_run(db)
    assert r.level(run, "restart") == "now" and r.level(run, "update") == "now"
    rule(db, "resonant", "restart", "notify_digest")  # the system they act on
    assert r.level(run, "restart") == "digest" and r.level(run, "update") == "now"
    rule(db, "proj", "update", "notify_digest")  # the run's repo
    assert r.level(run, "update") == "digest"
    assert r.self_op_text(run, "restarted Resonant (abc1234)") == "R1 restarted Resonant (abc1234)"


async def test_a_did_note_lands_on_the_runs_final_digest_line(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, extra=["--note", "did: merged PR #3 in dori with gh"])
    await h.start(report="digest")
    await h.drain()
    assert h.chan.texts() == []
    [line] = pulse_store.pending(db, ["normal", "urgent"], utcnow())
    assert line.title.startswith("R1 done: PR #41 (CI green) · ")
    assert line.title.endswith(" · did: merged PR #3 in dori with gh")


async def test_a_texted_end_says_what_the_run_did(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, extra=["--note", "did: deployed staging"])
    await h.start(report="now")
    await h.drain()
    [text] = h.chan.texts()
    assert text.startswith("R1 done: PR #41") and text.endswith("\nDid: deployed staging")


async def test_an_auto_merge_from_chat_reaches_the_digest(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """A run started from plain words with merge intent, merged on its first look at CI: its
    merged line is its only word, so it must reach the digest (it used to be cleared)."""
    h = merge_harness(tmp_path, db)
    res = await h.runner.start_run("proj", "fix it and merge it", "owner", merge=True)
    assert isinstance(res, Started)
    await settle(h, res.code, "merged")
    assert h.chan.texts() == []
    [line] = pulse_store.pending(db, ["normal", "urgent"], utcnow())
    assert line.key == "run/r1" and line.title.startswith("R1 merged: PR #41 · ")
    assert (pulse_job.preview(db) or "").startswith("btw:\nWorkers:\nR1 merged: PR #41")


def test_the_deps_line_stays_short() -> None:
    from resonant.runners.reports import DEPS_CHARS, deps_line

    long = [f"packages/very-long-package-name-{i}/package.json" for i in range(6)]
    line = deps_line(long)
    assert line.startswith("deps changed: packages/very-long-package-name-0/package.json")
    assert len(line.removeprefix("deps changed: ").split(" (+")[0]) <= DEPS_CHARS
    assert line.endswith("more)")
    assert deps_line(["uv.lock", "package.json"]) == "deps changed: uv.lock, package.json"

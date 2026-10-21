"""The hourly pulse (P35-3): core/pulse, core items, quiet hours, the briefing Updates section.

Same harness as the briefing tests: the real store, components and Scheduler on a fake clock,
and a fake channel that dedupes by key like the iMessage adapter.
"""

from __future__ import annotations

import functools
from datetime import datetime, timedelta
from typing import Any

from test_briefing import (
    CTX,
    LA,
    OWNER,
    TZ,
    Harness,
    approval,
    harness,
    local,
)

from resonant.extensions import briefing as bf
from resonant.extensions import notify
from resonant.extensions import pulse_job as pj
from resonant.extensions.scheduler import CORE_JOBS, Scheduler
from resonant.store import pulse as ps
from resonant.store.db import atomic, kv_get, kv_set

T9 = local(10, 13, 9)  # 09:00 PDT: a normal hour
MANUAL = {"enabled": True}


def report(
    h: Harness,
    source: str,
    key: str,
    title: str,
    *,
    urgency: ps.Urgency = "normal",
    state: Any = None,
    clear: bool = False,
    at: datetime = T9,
) -> ps.Item:
    _, item = ps.upsert(h.db, source, key, title, urgency, state, clear, 24 * 3600, at)
    return item


async def pulse(h: Harness, due: datetime, *, now: datetime | None = None) -> dict[str, Any]:
    job = pj.pulse_job(h.deps(h.channel), clock=h.clock)
    assert job is not None
    h.clock.now = now or due
    return await job.run({"due_at": due.isoformat()}, CTX)


def told(h: Harness, source: str, key: str) -> bool:
    item = ps.get(h.db, source, key)
    assert item is not None
    return not item.changed


def set_paused(h: Harness, value: bool) -> None:
    with atomic(h.db):
        kv_set(h.db, "paused", value)


# --- the job -----------------------------------------------------------------------------------


def test_pulse_is_a_core_job() -> None:
    assert pj.pulse_job in CORE_JOBS


async def test_not_scheduled_when_disabled_or_unreachable() -> None:
    async with harness() as h:
        h.settings.pulse.enabled = False
        assert pj.pulse_job(h.deps(h.channel)) is None
        h.settings.pulse.enabled = True
        assert pj.pulse_job(h.deps(None)) is None
        no_owner = h.deps(h.channel).__class__(h.settings, h.db, h.components, h.channel, None)
        assert pj.pulse_job(no_owner) is None
        job = pj.pulse_job(h.deps(h.channel))
        assert job is not None
        assert (job.id, job.schedule, job.catchup) == ("pulse", "0 * * * *", timedelta(minutes=50))


async def test_sends_one_btw_message_with_changed_items_then_nothing() -> None:
    async with harness() as h:
        report(h, "dori", "inc/1", "Dori recovered", clear=True, at=T9 - timedelta(minutes=30))
        report(h, "trader", "pnl", "PnL -$1.8k today", at=T9 - timedelta(minutes=20))
        report(h, "low", "x", "wait for 8", urgency="low")
        out = await pulse(h, T9)
        assert out == {"sent": True, "count": 2}
        assert h.channel.sent == [
            (
                OWNER,
                "btw:\n[dori] ✓ Dori recovered\n[trader] PnL -$1.8k today",
                "pulse:2026-10-13:09",
            )
        ]
        assert told(h, "trader", "pnl") and told(h, "dori", "inc/1")
        assert not told(h, "low", "x")
        # An hour later nothing changed: nothing sent.
        out = await pulse(h, T9 + timedelta(hours=1))
        assert out == {"sent": False, "count": 0, "skipped": "nothing"}
        assert len(h.channel.sent) == 1
        # A changed state is said again.
        report(h, "trader", "pnl", "PnL -$2.5k today", state={"b": -3}, at=T9 + timedelta(hours=1))
        await pulse(h, T9 + timedelta(hours=2))
        assert h.channel.texts()[-1] == "btw:\n[trader] PnL -$2.5k today"


async def test_urgent_held_items_come_first_with_a_marker_and_oldest_first() -> None:
    async with harness() as h:
        report(h, "a", "k1", "older normal", at=T9 - timedelta(minutes=50))
        report(h, "a", "k2", "newer normal", at=T9 - timedelta(minutes=10))
        report(h, "dori", "k", "Dori failing", urgency="urgent", at=T9 - timedelta(minutes=40))
        await pulse(h, T9)
        assert h.channel.texts() == [
            "btw:\n[dori] 🔴 Dori failing\n[a] older normal\n[a] newer normal"
        ]


async def test_skips_the_briefing_hour_and_keeps_items_for_the_next_pulse() -> None:
    async with harness() as h:
        report(h, "a", "k", "x")
        out = await pulse(h, local(10, 13, 8))
        assert out == {"sent": False, "count": 0, "skipped": "briefing"}
        assert h.channel.sent == []
        await pulse(h, local(10, 13, 9))
        assert h.channel.texts() == ["btw:\n[a] x"]
    async with harness(briefing={"enabled": False}) as h:
        report(h, "a", "k", "x")
        assert (await pulse(h, local(10, 13, 8)))["sent"] is True  # no briefing to cover it


async def test_quiet_hours_hold_everything_until_six() -> None:
    async with harness() as h:
        report(h, "a", "k", "x", at=local(10, 13, 23))
        report(h, "dori", "u", "held urgent", urgency="urgent", at=local(10, 13, 23))
        for hour in (0, 3, 5):
            out = await pulse(h, local(10, 14, hour))
            assert out == {"sent": False, "count": 0, "skipped": "quiet"}
        assert h.channel.sent == []
        out = await pulse(h, local(10, 14, 6))
        assert out == {"sent": True, "count": 2}
        assert h.channel.sent[0][2] == "pulse:2026-10-14:06"


async def test_catchup_run_uses_the_due_hour_not_the_run_time() -> None:
    async with harness() as h:
        report(h, "a", "k", "x", at=local(10, 14, 4))
        due = local(10, 14, 5)  # due in quiet hours, run at 05:40 or even 06:20
        assert (await pulse(h, due, now=local(10, 14, 6, 20)))["skipped"] == "quiet"
        assert (await pulse(h, local(10, 14, 6), now=local(10, 14, 6, 20)))["sent"] is True


async def test_paused_sends_nothing_and_resume_carries_what_changed_once() -> None:
    async with harness() as h:
        report(h, "a", "k", "x")
        set_paused(h, True)
        assert (await pulse(h, T9))["skipped"] == "paused"
        assert h.channel.sent == [] and not told(h, "a", "k")
        set_paused(h, False)
        await pulse(h, T9 + timedelta(hours=1))
        await pulse(h, T9 + timedelta(hours=2))
        assert h.channel.texts() == ["btw:\n[a] x"]


async def test_muted_source_is_skipped_and_not_marked_told() -> None:
    async with harness() as h:
        report(h, "noisy", "k", "spam")
        report(h, "dori", "k", "real")
        notify.mute(h.db, "noisy", T9 + timedelta(hours=3), by=OWNER)
        await pulse(h, T9)
        assert h.channel.texts() == ["btw:\n[dori] real"]
        assert not told(h, "noisy", "k")


async def test_only_the_shown_items_are_marked_told_and_the_rest_ride_the_next_pulse() -> None:
    async with harness() as h:
        for i in range(10):
            report(h, "ext", f"k{i}", f"{i} " + "z" * 190, at=T9 - timedelta(minutes=i + 1))
        out = await pulse(h, T9)
        text = h.channel.texts()[0]
        assert len(text) <= pj.MAX_CHARS and text.endswith(f"+{10 - out['count']} more")
        assert 0 < out["count"] < 10
        untold = [i for i in range(10) if not told(h, "ext", f"k{i}")]
        assert len(untold) == 10 - out["count"]
        for n in range(1, 4):  # a few more pulses drain the rest, none repeating
            await pulse(h, T9 + timedelta(hours=n))
        assert all(told(h, "ext", f"k{i}") for i in range(10))
        assert sum(t.count("\n[ext]") for t in h.channel.texts()) == 10


async def test_same_hour_catchup_or_restart_sends_once_and_new_items_wait() -> None:
    async with harness() as h:
        report(h, "a", "k", "x")
        await pulse(h, T9)
        report(h, "a", "k2", "y", at=T9 + timedelta(minutes=5))
        out = await pulse(h, T9, now=T9 + timedelta(minutes=30))  # the same due time again
        assert out["skipped"] == "duplicate"
        assert len(h.channel.sent) == 1 and not told(h, "a", "k2")
        await pulse(h, T9 + timedelta(hours=1))
        assert h.channel.texts()[-1] == "btw:\n[a] y"


async def test_crash_between_send_and_told_resends_nothing_and_marks_told() -> None:
    async with harness() as h:
        report(h, "a", "k", "x")
        await h.channel.send(OWNER, "btw:\n[a] x", dedupe_key="pulse:2026-10-13:09")  # sent
        await pulse(h, T9)  # the retry: the channel drops the repeat key
        assert len(h.channel.sent) == 1 and told(h, "a", "k")


async def test_result_has_no_text() -> None:
    async with harness() as h:
        report(h, "a", "k", "SECRET-TEXT")
        assert "SECRET" not in repr(await pulse(h, T9))


async def test_scheduler_fires_it_on_the_hour_in_local_time() -> None:
    async with harness(at=local(10, 13, 8, 59)) as h:
        report(h, "a", "k", "x")
        s = Scheduler.from_components(
            h.settings,
            h.db,
            h.components,
            started_at=h.clock.now,
            channel=h.channel,
            core_jobs=(functools.partial(pj.pulse_job, clock=h.clock),),
            clock=h.clock,
        )
        h.sched = s
        s.jobs = tuple(j for j in s.jobs if j.core)
        assert s.next_due(h.clock.now) == local(10, 13, 9)
        await h.tick(local(10, 13, 9, 0))
        assert h.channel.texts() == ["btw:\n[a] x"]


# --- manual -------------------------------------------------------------------------------------


async def test_manual_run_ignores_quiet_and_briefing_hours_but_not_pause() -> None:
    async with harness(at=local(10, 14, 3)) as h:
        report(h, "a", "k", "x", at=local(10, 14, 2))
        out = await pj.run_pulse_now(h.deps(h.channel), now=local(10, 14, 3, 7))
        assert out == {"sent": True, "count": 1}
        assert h.channel.sent[0][2] == "pulse:manual:2026-10-14:0307"
        report(h, "a", "k2", "y", at=local(10, 14, 3, 8))
        set_paused(h, True)
        out = await pj.run_pulse_now(h.deps(h.channel), now=local(10, 14, 3, 9))
        assert out["skipped"] == "paused"
        set_paused(h, False)
        assert (await pj.run_pulse_now(h.deps(None)))["skipped"] == "unreachable"
        # During the briefing hour too.
        out = await pj.run_pulse_now(h.deps(h.channel), now=local(10, 14, 8, 5))
        assert out["sent"] is True


async def test_preview_is_the_pending_text_and_read_only() -> None:
    async with harness() as h:
        assert pj.preview(h.db, T9) is None
        report(h, "a", "k", "x")
        report(h, "lowsrc", "k", "wait", urgency="low")
        assert pj.preview(h.db, T9) == "btw:\n[a] x"
        assert not told(h, "a", "k")
        notify.mute(h.db, "a", T9 + timedelta(hours=1), by=OWNER)
        assert pj.preview(h.db, T9) is None


# --- core items ---------------------------------------------------------------------------------


async def test_a_pending_approval_is_never_a_pulse_item() -> None:
    """PER-261: ``core/remind`` texts the reminder at half the TTL; the pulse says nothing."""
    async with harness() as h:
        base = local(10, 13, 9)
        approval(h.db, "A7", base)  # created at `base`, expires an hour later
        h.settings.pulse.remention_after = "30m"
        for minutes in (10, 40, 50):
            at = base + timedelta(minutes=minutes)
            await pulse(h, at, now=at)
        assert h.channel.sent == []
        assert [i.key for i in ps.open_items(h.db, base + timedelta(minutes=50))
                if i.key.startswith("approval/")] == []  # fmt: skip


async def test_an_approval_item_from_before_is_cleared_silently() -> None:
    async with harness() as h:
        base = local(10, 13, 9)
        approval(h.db, "A7", base)
        report(h, "core", "approval/a7", "✋ A7 still waiting: restart Resonant")
        await pulse(h, base + timedelta(minutes=40), now=base + timedelta(minutes=40))
        assert h.channel.sent == []
        item = ps.get(h.db, "core", "approval/a7")
        assert item is not None and item.cleared and not item.changed


# --- briefing Updates ---------------------------------------------------------------------------


async def test_briefing_updates_section_lists_open_items_and_marks_them_told() -> None:
    async with harness() as h:
        report(h, "dori", "k", "Dori failing", urgency="urgent")
        report(h, "trader", "pnl", "PnL -$1.8k")
        report(h, "github", "summary", "3 open PRs", urgency="low")
        report(h, "core", "approval/a7", "✋ A7 still waiting: x")  # core: never listed
        report(h, "dori", "gone", "Dori recovered", clear=True)  # cleared: not listed
        h.db.execute(  # one the owner already heard sorts last
            "UPDATE pulse_items SET told_fingerprint = fingerprint WHERE key = 'summary'"
        )
        out = await bf.send_briefing(h.db, h.channel, OWNER, tz="America/Los_Angeles", now=T9)
        ((_, text, _),) = h.channel.sent
        assert text == (
            "Updates:\n[dori] 🔴 Dori failing\n[trader] PnL -$1.8k\n[github] 3 open PRs"
        )
        assert "updates" in out["sections"]
        assert (
            told(h, "dori", "k") and told(h, "trader", "pnl") and not told(h, "core", "approval/a7")
        )
        assert not told(h, "dori", "gone")  # the held clear still rides the pulse
        # Told items stay listed while they are open.
        await bf.send_briefing(
            h.db,
            h.channel,
            OWNER,
            tz="America/Los_Angeles",
            now=T9 + timedelta(hours=1),
            manual=True,
        )
        assert h.channel.texts()[-1].startswith("Updates:")


async def test_briefing_updates_cap_eight_lines_and_fit_the_1000_char_limit() -> None:
    async with harness() as h:
        for i in range(12):
            report(h, "ext", f"k{i}", f"item {i}")
        await bf.send_briefing(h.db, h.channel, OWNER, tz="America/Los_Angeles", now=T9)
        text = h.channel.texts()[0]
        assert text.count("\n[ext]") == 8 and text.endswith("+4 more")
        assert sum(not told(h, "ext", f"k{i}") for i in range(12)) == 4
    async with harness() as h:
        for i in range(10):
            report(h, "ext", f"k{i}", f"{i} " + "z" * 190)
        out = await bf.send_briefing(h.db, h.channel, OWNER, tz="America/Los_Angeles", now=T9)
        text = h.channel.texts()[0]
        assert len(text) <= bf.MAX_CHARS == 1000 and out["chars"] == len(text)
        shown = text.count("\n[ext]")
        assert shown < 8 and f"+{10 - shown} more" in text
        assert sum(told(h, "ext", f"k{i}") for i in range(10)) == shown


async def test_manual_briefing_marks_told_and_paused_does_not() -> None:
    async with harness() as h:
        report(h, "a", "k", "x")
        set_paused(h, True)
        await bf.run_briefing_now(h.deps(h.channel), now=T9)
        assert h.channel.sent == [] and not told(h, "a", "k")
        set_paused(h, False)
        await bf.run_briefing_now(h.deps(h.channel), now=T9)
        assert h.channel.texts() == ["Updates:\n[a] x"]
        assert told(h, "a", "k")
        await pulse(h, T9 + timedelta(hours=1))
        assert len(h.channel.sent) == 1  # the pulse has nothing to repeat


def test_pulse_reply_texts() -> None:
    from resonant.router.fast_path import pulse_reply

    assert pulse_reply({"sent": True, "count": 2}) == "Pulse sent."
    assert pulse_reply({"sent": False, "skipped": "paused"}) == "Paused: pulse not sent."
    assert pulse_reply({"sent": False, "skipped": "nothing"}) == "Nothing new to send."
    assert pulse_reply({"sent": False, "skipped": "unreachable"}) == "Can't send: no channel/owner."


async def test_crash_between_send_and_told_with_a_new_item_pending_keeps_the_new_one() -> None:
    async with harness() as h:
        report(h, "a", "k", "x")
        job_now = T9
        # The send happened, then the process died before the items were marked.
        text, shown = pj.compose(ps.pending(h.db, ("urgent", "normal"), job_now)) or ("", [])
        with atomic(h.db):
            kv_set(
                h.db,
                "pulse.inflight:pulse:2026-10-13:09",
                {
                    "text": text,
                    "items": [[i.source, i.key, i.fingerprint] for i in shown],
                    "sent": False,
                    "at": job_now.isoformat(),
                },
            )
        await h.channel.send(OWNER, text, dedupe_key="pulse:2026-10-13:09")
        report(h, "a", "new", "arrived since", at=T9 + timedelta(minutes=5))
        out = await pulse(h, T9, now=T9 + timedelta(minutes=10))
        assert out == {"sent": True, "count": 1}
        assert len(h.channel.sent) == 1  # the resend was dropped by the channel
        assert told(h, "a", "k") and not told(h, "a", "new")
        await pulse(h, T9 + timedelta(hours=1))
        assert h.channel.texts()[-1] == "btw:\n[a] arrived since"


async def test_scheduled_manual_scheduled_rerun_in_one_hour() -> None:
    async with harness() as h:
        report(h, "a", "k", "x")
        assert (await pulse(h, T9))["sent"] is True
        report(h, "a", "k2", "y", at=T9 + timedelta(minutes=5))
        out = await pj.run_pulse_now(h.deps(h.channel), now=T9 + timedelta(minutes=10))
        assert out == {"sent": True, "count": 1}
        report(h, "a", "k3", "z", at=T9 + timedelta(minutes=15))
        out = await pulse(h, T9, now=T9 + timedelta(minutes=20))  # catch-up rerun
        assert out["skipped"] == "duplicate"
        assert told(h, "a", "k2") and not told(h, "a", "k3")
        assert h.channel.texts() == ["btw:\n[a] x", "btw:\n[a] y"]


async def test_done_inflight_records_are_pruned_after_two_days() -> None:
    async with harness() as h:
        report(h, "a", "k", "x")
        await pulse(h, T9)
        assert kv_get(h.db, "pulse.inflight:pulse:2026-10-13:09") is not None
        await pulse(h, T9 + timedelta(days=1))
        assert kv_get(h.db, "pulse.inflight:pulse:2026-10-13:09") is not None
        await pulse(h, T9 + timedelta(days=2, hours=1))
        assert kv_get(h.db, "pulse.inflight:pulse:2026-10-13:09") is None


async def test_dst_day_quiet_hours_and_key_follow_local_time() -> None:
    spring = datetime(2027, 3, 14, 3, tzinfo=LA)  # 02:00 does not exist; 03:00 PDT
    async with harness(at=spring) as h:
        report(h, "a", "k", "x", at=spring)
        assert (await pulse(h, spring))["skipped"] == "quiet"
        six = datetime(2027, 3, 14, 6, tzinfo=LA)
        assert (await pulse(h, six))["sent"] is True
        assert h.channel.sent[0][2] == "pulse:2027-03-14:06"


async def test_briefing_rerun_same_day_with_a_new_item_does_not_mark_it() -> None:
    async with harness() as h:
        report(h, "a", "k", "x")
        await bf.send_briefing(h.db, h.channel, OWNER, tz=TZ, now=T9)
        report(h, "a", "new", "later", at=T9 + timedelta(minutes=5))
        out = await bf.send_briefing(h.db, h.channel, OWNER, tz=TZ, now=T9 + timedelta(minutes=10))
        assert out["sent"] is True and len(h.channel.sent) == 1
        assert told(h, "a", "k") and not told(h, "a", "new")
        # A crash before the mark: the stored intent is resent and marks only its items.
        key = "pulse.inflight:briefing:2026-10-13"
        rec = kv_get(h.db, key)
        kv_set(h.db, key, {**rec, "sent": False})
        await bf.send_briefing(h.db, h.channel, OWNER, tz=TZ, now=T9 + timedelta(minutes=20))
        assert len(h.channel.sent) == 1 and not told(h, "a", "new")


async def test_briefing_updates_skip_muted_sources_and_do_not_mark_them() -> None:
    async with harness() as h:
        report(h, "noisy", "k", "spam")
        report(h, "dori", "k", "real")
        notify.mute(h.db, "noisy", T9 + timedelta(hours=3), by=OWNER)
        await bf.send_briefing(h.db, h.channel, OWNER, tz=TZ, now=T9)
        assert h.channel.texts() == ["Updates:\n[dori] real"]
        assert not told(h, "noisy", "k")


def test_remention_delay_is_capped_at_half_the_approval_ttl() -> None:
    from resonant.config import GateConfig, PulseConfig, Settings

    def delay(remention: str, ttl_s: float) -> timedelta:
        settings = Settings(
            pulse=PulseConfig(remention_after=remention), gate=GateConfig(approval_ttl_s=ttl_s)
        )
        return pj.remention_delay(settings)

    assert delay("10m", 1800) == timedelta(minutes=10)  # the defaults
    assert delay("4h", 1800) == timedelta(minutes=15)  # would never fire uncapped
    assert delay("4h", 86400) == timedelta(hours=4)

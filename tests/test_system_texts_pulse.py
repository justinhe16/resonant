"""PER-274: the hourly pulse and the briefing are system texts (migration 024).

What went out is recorded once per dedupe key, both brains read it in the transcript as
Resonant's own turn, and "Already told" leaves out what a text shown there already said.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from importlib import resources
from pathlib import Path

import pytest
from brain_seed import message
from test_briefing import OWNER, TZ, harness
from test_pulse_job import PROSE, T9, Flaky, failed_run, pulse, report, use_voice
from voice_script import Script

from resonant.brain import history
from resonant.brain.briefing import known_notes
from resonant.extensions import briefing as bf
from resonant.extensions import pulse_job as pj
from resonant.gateway.channel import ChannelError
from resonant.store import pulse as ps
from resonant.store import system_texts
from resonant.store.db import atomic, connect, kv_set, migrate, schema_version
from resonant.store.system_texts import SystemText

KEY = "pulse:2026-10-13:09"


def through(version: int) -> list[tuple[int, str]]:
    """The store's migrations up to ``version``, as ``migrate`` takes them."""
    found = [
        (int(f.name[:3]), f.read_text())
        for f in resources.files("resonant.store.migrations").iterdir()
        if f.name.endswith(".sql") and int(f.name[:3]) <= version
    ]
    return sorted(found)


def rows(db: sqlite3.Connection) -> list[SystemText]:
    return system_texts.between(db, OWNER, limit=50)


# --- migration 024 ---------------------------------------------------------------------------


def test_migration_024_keeps_rows_and_ids_and_widens_the_kinds(tmp_path: Path) -> None:
    conn = connect(tmp_path / "v23.db")
    migrate(conn, through(23))
    assert schema_version(conn) == 23
    with pytest.raises(sqlite3.IntegrityError):  # 019's CHECK
        conn.execute(
            "INSERT INTO system_texts (principal, kind, text, ref, sent_at)"
            " VALUES ('o', 'pulse', 't', 'p0', '2026-10-13T16:00:00+00:00')"
        )
    for i, (kind, outside) in enumerate((("approval", 0), ("run", 1), ("result", 0))):
        conn.execute(
            "INSERT INTO system_texts (principal, kind, text, ref, task_id, outside, sent_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (OWNER, kind, f"text {i}", f"r{i}", f"t{i}", outside, f"2026-10-13T16:0{i}:00+00:00"),
        )
    conn.execute("DELETE FROM system_texts WHERE ref = 'r2'")  # the newest pruned
    conn.commit()
    before = [tuple(r) for r in conn.execute("SELECT * FROM system_texts ORDER BY id")]

    assert migrate(conn) == 24 == schema_version(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM system_texts ORDER BY id")] == before
    # the AUTOINCREMENT mark is carried: a pruned id is never reused
    system_texts.record(conn, OWNER, "pulse", "btw:\n[a] x", ref="pulse:2026-10-13:09", now=T9)
    system_texts.record(conn, OWNER, "briefing", "Morning.", ref="briefing:2026-10-13", now=T9)
    got = [(t.id, t.kind) for t in rows(conn)]
    assert got == [(1, "approval"), (2, "run"), (4, "pulse"), (5, "briefing")]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO system_texts (principal, kind, text, ref, sent_at)"
            " VALUES ('o', 'nonsense', 't', 'bad', '2026-10-13T16:00:00+00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # ref stays unique
        conn.execute(
            "INSERT INTO system_texts (principal, kind, text, ref, sent_at)"
            " VALUES ('o', 'run', 't', 'r0', '2026-10-13T16:00:00+00:00')"
        )
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "system_texts_principal" in indexes
    gone = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'system_texts_new'").fetchone()
    assert gone is None
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_migration_024_on_an_empty_table(tmp_path: Path) -> None:
    conn = connect(tmp_path / "empty.db")
    migrate(conn, through(23))
    assert migrate(conn) == 24
    system_texts.record(conn, OWNER, "pulse", "btw:\n[a] x", ref="k")
    assert [t.id for t in rows(conn)] == [1]
    conn.close()


def test_a_pulse_or_briefing_is_kept_whole_and_other_kinds_are_clipped(
    db: sqlite3.Connection,
) -> None:
    long = "\n".join(f"[ext] item number {i} with some words to fill it out" for i in range(18))
    assert 900 < len(long) <= pj.MAX_CHARS
    system_texts.record(db, OWNER, "pulse", long, ref="p")
    system_texts.record(db, OWNER, "run", long, ref="r")
    by_ref = {t.kind: t.text for t in rows(db)}
    assert by_ref["pulse"] == system_texts.flat(long, system_texts.LONG_MAX)
    assert not by_ref["pulse"].endswith("…") and "item number 17" in by_ref["pulse"]
    assert len(by_ref["run"]) == system_texts.TEXT_MAX and by_ref["run"].endswith("…")


# --- recording the sends ---------------------------------------------------------------------


async def test_the_pulse_is_recorded_once_under_its_key() -> None:
    async with harness() as h:
        report(h, "trader", "pnl", "PnL -$1.8k today")
        assert (await pulse(h, T9))["sent"]
        ((t,),) = [rows(h.db)]
        assert (t.kind, t.text, t.sent_at, t.task_id) == (
            "pulse", system_texts.flat(h.channel.texts()[0]), T9, None,
        )  # fmt: skip
        assert t.outside  # an extension's item is outside content
        ref = h.db.execute("SELECT ref FROM system_texts").fetchone()[0]
        assert ref == KEY
        item = ps.get(h.db, "trader", "pnl")
        assert item is not None and item.told_at == t.sent_at  # what "Already told" matches
        # a catch-up rerun of the same key sends and records nothing new
        out = await pulse(h, T9, now=T9 + timedelta(minutes=20))
        assert out["skipped"] == "duplicate" and len(rows(h.db)) == 1


async def test_a_crash_between_send_and_record_records_once_on_the_rerun() -> None:
    async with harness() as h:
        report(h, "a", "k", "x")
        await h.channel.send(OWNER, "btw:\n[a] x", dedupe_key=KEY)  # sent, never recorded
        await pulse(h, T9)  # the retry: the channel drops the repeat key
        assert len(h.channel.sent) == 1
        want = [("pulse", system_texts.flat("btw:\n[a] x"))]
        assert [(t.kind, t.text) for t in rows(h.db)] == want


async def test_a_failed_send_records_nothing_until_it_goes_out() -> None:
    async with harness() as h:
        h.channel = Flaky()
        report(h, "a", "k", "x")
        h.channel.down = True
        with pytest.raises(ChannelError):
            await pulse(h, T9)
        assert rows(h.db) == []
        h.channel.down = False
        await pulse(h, T9 + timedelta(hours=1))  # finishes the unsent record first
        assert [t.text for t in rows(h.db)] == [system_texts.flat("btw:\n[a] x")]
        assert h.db.execute("SELECT ref FROM system_texts").fetchone()[0] == KEY


async def test_an_inflight_record_from_before_counts_as_outside() -> None:
    """A record stored before PER-274 has no ``outside``: its text fails closed."""
    async with harness() as h:
        report(h, "core", "notice/w1", "Your watch ended without firing")
        item = ps.get(h.db, "core", "notice/w1")
        assert item is not None
        with atomic(h.db):
            kv_set(h.db, pj.INFLIGHT + KEY, {
                "text": "btw:\nYour watch ended without firing",
                "items": [["core", "notice/w1", item.fingerprint]],
                "sent": False, "at": T9.isoformat(), "voice": False,
            })  # fmt: skip
        await pulse(h, T9)
        ((t,),) = [rows(h.db)]
        assert t.kind == "pulse" and t.outside


async def test_a_voiced_pulse_is_recorded_as_sent_and_is_outside() -> None:
    async with harness() as h:
        code = failed_run(h)
        use_voice(h, Script(PROSE))
        await pulse(h, T9)
        (sent,) = h.channel.texts()
        assert sent == f"{PROSE}\nNeeds you: a look at {code}."
        ((t,),) = [rows(h.db)]
        assert t.text == system_texts.flat(sent, system_texts.LONG_MAX) and t.outside


async def test_a_pulse_of_only_core_notices_is_not_outside() -> None:
    async with harness() as h:
        report(h, "core", "notice/w1", "Your watch on the deploy ended without firing")
        await pulse(h, T9)
        ((t,),) = [rows(h.db)]
        assert t.kind == "pulse" and not t.outside


def test_is_outside_counts_everything_but_core_notices() -> None:
    def item(source: str, key: str) -> ps.Item:
        return ps.Item(source, key, "t", "normal", "fp", False, T9, T9, None, None)

    assert not pj.is_outside([])
    assert not pj.is_outside([item("core", "notice/a")])
    for source, key in (("core", "run/R1"), ("core", "question/R1"), ("core", "stall/x"),
                        ("github", "merged/1"), ("trader", "pnl")):  # fmt: skip
        assert pj.is_outside([item("core", "notice/a"), item(source, key)])
    assert pj.text_kind("briefing:2026-10-13") == pj.text_kind("briefing:manual:x") == "briefing"
    assert pj.text_kind(KEY) == pj.text_kind("pulse:manual:2026-10-13:0905") == "pulse"


async def test_the_briefing_is_recorded_once_and_outside_only_with_outside_items() -> None:
    async with harness() as h:
        await bf.send_briefing(h.db, h.channel, OWNER, tz=TZ, now=T9)
        await bf.send_briefing(h.db, h.channel, OWNER, tz=TZ, now=T9 + timedelta(minutes=5))
        ((quiet,),) = [rows(h.db)]
        assert quiet.kind == "briefing" and not quiet.outside  # nothing but our own lines
        assert quiet.text == system_texts.flat(h.channel.texts()[0])
        report(h, "trader", "pnl", "PnL -$1.8k")
        later = T9 + timedelta(hours=1)
        await bf.send_briefing(h.db, h.channel, OWNER, tz=TZ, now=later, manual=True)
        got = [(t.kind, t.outside) for t in rows(h.db)]
        assert got == [("briefing", False), ("briefing", True)]
        refs = [r[0] for r in h.db.execute("SELECT ref FROM system_texts ORDER BY id")]
        assert refs[0] == "briefing:2026-10-13" and refs[1].startswith("briefing:manual:")


async def test_a_briefing_naming_a_worker_is_outside() -> None:
    async with harness() as h:
        failed_run(h)
        await bf.send_briefing(h.db, h.channel, OWNER, tz=TZ, now=T9)
        ((t,),) = [rows(h.db)]
        assert t.kind == "briefing" and t.outside


# --- the transcript and "Already told" -------------------------------------------------------


async def test_both_brains_read_the_pulse_and_already_told_does_not_repeat_it() -> None:
    from zoneinfo import ZoneInfo

    async with harness() as h:
        first = message(h.db, "start the trader", reply="ok", at=T9 - timedelta(minutes=30))
        titles = [f"PnL line {i} with enough words to make the pulse long" for i in range(9)]
        for i, title in enumerate(titles):
            report(h, "trader", f"pnl{i}", title, at=T9 - timedelta(minutes=20))
        await pulse(h, T9)
        (sent,) = h.channel.texts()
        assert len(sent) > system_texts.TEXT_MAX  # it would have been clipped before
        current = message(h.db, "how's it going?", at=T9 + timedelta(minutes=5))

        found = history.replayed(h.db, OWNER, before_event_id=current)
        whole = system_texts.flat(sent, system_texts.LONG_MAX)
        assert found.messages[-1] == {"role": "assistant", "content": whole}
        assert found.told == frozenset({T9})
        assert found.untrusted == "tainted_reply"  # extension items are outside content

        now = T9 + timedelta(minutes=6)
        assert len(known_notes(h.db, owner=True, now=now, tz=TZ, limit=20)) == len(titles)
        assert known_notes(h.db, owner=True, now=now, tz=TZ, shown=found.told) == []

        delta = history.since_last(h.db, OWNER, after_event_id=first, before_event_id=current,
                                   since=T9 - timedelta(hours=1), tz=ZoneInfo(TZ))  # fmt: skip
        assert delta is not None and f"Resonant texted: {whole}" in delta.text and delta.outside


async def test_an_item_whose_pulse_is_not_shown_stays_already_told() -> None:
    """A told item whose pulse fell out of the transcript (before the first exchange shown, or
    past the newest three in a gap) keeps its "Already told" line."""
    async with harness() as h:
        report(h, "trader", "old", "PnL told early", at=T9 - timedelta(minutes=20))
        await pulse(h, T9)  # before any exchange: not in the transcript
        message(h.db, "hi", reply="hey", at=T9 + timedelta(minutes=30))
        report(h, "trader", "new", "PnL told later", at=T9 + timedelta(minutes=40))
        await pulse(h, T9 + timedelta(hours=1))
        current = message(h.db, "news?", at=T9 + timedelta(hours=1, minutes=5))
        found = history.replayed(h.db, OWNER, before_event_id=current)
        assert found.told == frozenset({T9 + timedelta(hours=1)})
        notes = known_notes(h.db, owner=True, now=T9 + timedelta(hours=1, minutes=6), tz=TZ,
                            shown=found.told)  # fmt: skip
        assert [n.split(" (told")[0] for n in notes] == ["[trader] PnL told early"]

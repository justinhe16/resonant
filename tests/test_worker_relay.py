"""EM-5 on the desk: question texts, the pulse items, the ask dedupe, the busy check reply and
answers bound to the question shown (runners/worker_desk.py, extensions/pulse_job.py)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

from test_worker_cli import Trust, rig, start

from resonant.extensions.pulse_job import STILL_WAITING, remention_questions
from resonant.runners.claude import Refused
from resonant.runners.worker_cli import EXIT_BUSY, EXIT_OK, EXIT_PENDING, Ask, Check
from resonant.runners.worker_desk import BUSY, Relayed, question_item_key
from resonant.store import pulse as pulse_store
from resonant.store import questions as qs
from resonant.store import runs as runs_store
from resonant.store.db import utcnow


def _gone() -> asyncio.Future[None]:
    return asyncio.get_running_loop().create_future()


async def test_a_repeat_after_the_answer_gets_it_again_and_isnt_retexted(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, ask_block_s=0.2)
    code = await start(h)
    runs_store.update(db, code, status="running")
    first = await h.runner.desk.handle(code, Ask("Which DB?", ("pg", "sqlite")), _gone())
    assert first is not None and first.exit == EXIT_PENDING
    assert h.runner.answer_question(code, "pg") == Relayed(code, f"→ {code}")
    for _ in range(2):  # before and after it was delivered
        again = await h.runner.desk.handle(code, Ask("Which DB?", ("pg", "sqlite")), _gone())
        assert again is not None and again.exit == EXIT_OK
        assert again.text == "You asked this already. The owner answered: pg"
        assert again.on_sent is not None
        again.on_sent()
    [q] = qs.for_run(db, code)
    assert q.delivered_at is not None
    assert [t for t in h.chan.texts() if "asks" in t] == [
        f"{code} asks: Which DB?\n(a) pg (b) sqlite"
    ]
    # a different question is asked as usual
    other = await h.runner.desk.handle(code, Ask("And the region?"), _gone())
    assert other is not None and other.exit == EXIT_PENDING
    assert len(qs.for_run(db, code)) == 2


async def test_the_question_is_a_pulse_item_until_it_is_answered(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, ask_block_s=0.1)
    code = await start(h)
    runs_store.update(db, code, status="running")
    await h.runner.desk.handle(code, Ask("Which DB?", ("pg",), urgent=True), _gone())
    [q] = qs.for_run(db, code)
    key = question_item_key(code, q.id)
    item = pulse_store.get(db, "core", key)
    assert item is not None and not item.changed  # texted now: not news for this pulse
    assert item.title == f"⚠ {code} asks: Which DB? (a) pg"
    later = utcnow() + timedelta(hours=1)
    remention_questions(db, later, timedelta(minutes=10))
    item = pulse_store.get(db, "core", key)
    assert item is not None and item.changed and item.title.startswith(STILL_WAITING)
    pulse_store.mark_told(db, [item], later)
    remention_questions(db, later + timedelta(minutes=5), timedelta(minutes=10))
    item = pulse_store.get(db, "core", key)
    assert item is not None and not item.changed  # once per pulse, not per refresh
    assert item.title.count(STILL_WAITING) == 1
    assert isinstance(h.runner.answer_question(code, "pg"), Relayed)
    item = pulse_store.get(db, "core", key)
    assert item is not None and item.cleared and not item.changed  # cleared silently


async def test_an_answer_is_bound_to_the_question_shown(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = rig(tmp_path, db, ask_block_s=0.1)
    code = await start(h)
    runs_store.update(db, code, status="running")
    await h.runner.desk.handle(code, Ask("Which DB?"), _gone())
    [q] = qs.for_run(db, code)
    refused = h.runner.answer_question(code, "pg", question_id=q.id + 1)
    assert refused == Refused(f"{code}'s question was already answered or timed out.")
    assert qs.get(db, q.id).status == "open"  # type: ignore[union-attr]
    assert isinstance(h.runner.answer_question(code, "pg", question_id=q.id), Relayed)


async def test_a_fourth_pending_check_is_busy_not_denied(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    gate = asyncio.Event()
    h = rig(tmp_path, db, trust=Trust("ok", gate), ask_block_s=0.05)
    code = await start(h)
    runs_store.update(db, code, status="running")
    for n in range(3):
        r = await h.runner.desk.handle(code, Check("dori", "deploy", f"step {n}"), _gone())
        assert r is not None and r.exit == EXIT_PENDING
    busy = await h.runner.desk.handle(code, Check("dori", "deploy", "step 4"), _gone())
    assert busy is not None and (busy.exit, busy.text) == (EXIT_BUSY, BUSY)
    assert len(qs.open_for_run(db, code, "check")) == 3
    gate.set()
    await h.runner.desk.aclose()


async def test_the_trust_layers_busy_is_passed_on(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = rig(tmp_path, db, trust=Trust("busy"), ask_block_s=1)
    code = await start(h)
    runs_store.update(db, code, status="running")
    r = await h.runner.desk.handle(code, Check("dori", "deploy", "ship"), _gone())
    assert r is not None and (r.exit, r.text) == (EXIT_BUSY, BUSY)

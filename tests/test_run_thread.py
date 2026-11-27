"""PER-294: a run's texts go to its thread, addressed to who asked (``runners.run_thread``)."""

from __future__ import annotations

import sqlite3

from resonant.e2e import FakeChannel
from resonant.principals import Principal, Principals
from resonant.runners.run_thread import run_thread, send_run
from resonant.store import runs

TEAM = Principals(principals={
    "alice": Principal(role="owner", identities=("slack:U1",)),
    "bob": Principal(role="approver", identities=("slack:U2",)),
})  # fmt: skip
REF = {"channel": "slack", "principal": "bob", "identity": "slack:U2", "thread": "T-1",
       "ref": "slack-in:1"}  # fmt: skip


def _run(db: sqlite3.Connection, **kw: object) -> runs.Run:
    return runs.create(db, repo="", ask="do it", principal="alice", session_id="s",
                       max_seconds=60, **kw)  # type: ignore[arg-type]  # fmt: skip


async def test_a_run_texts_its_requester_in_its_thread(db: sqlite3.Connection) -> None:
    fake = FakeChannel("slack", TEAM)
    run = _run(db, channel_ref=REF, requested_by="bob")
    assert run.requester == "bob" and run.channel_ref == REF
    thread = run_thread(db, run)
    assert thread is not None and thread.thread == "T-1"
    await send_run(fake, db, run, "R1 asks: which?", dedupe_key="k")
    assert [(m.principal, m.thread, m.text) for m in fake.sent] == [
        ("bob", "T-1", "R1 asks: which?")
    ]


async def test_a_requester_who_left_goes_to_the_owner_in_the_same_thread(
    db: sqlite3.Connection,
) -> None:
    fake = FakeChannel("slack", TEAM)
    gone = {**REF, "principal": "carol"}
    run = _run(db, channel_ref=gone, requested_by="carol")  # no longer a principal
    await send_run(fake, db, run, "R1 asks: which?")
    assert [(m.principal, m.thread) for m in fake.sent] == [("alice", "T-1")]


async def test_an_owners_run_without_a_thread_sends_as_before(db: sqlite3.Connection) -> None:
    fake = FakeChannel("slack", TEAM, threaded=False)
    run = _run(db)
    assert run.requester == "alice" and run.requested_by is None
    assert run_thread(db, run) is None
    await send_run(fake, db, run, "done")
    assert [(m.principal, m.thread) for m in fake.sent] == [("alice", None)]

"""PER-347: a failed run says so once in its thread.

Before, a run that failed in a thread posted its end text (``R1 (…) failed: …``) and then
``Couldn't finish that: R1 failed (…)`` under it. Now the end text, once it is out, is the one
failure message: the run's code, what failed and, when a limit stopped it, the next step
(``continue R1``). ``Couldn't finish that`` is said only when the end text didn't go out (a
refused send), still once per task on its dedupe key. Asks that fail without a run keep their
own line (``StatusLines``, unchanged: ``test_status_lines``)."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

from test_runner_claude import Chan, Harness, build, until

from resonant.gateway.channel import Channel, ChannelRefused, MessageRef
from resonant.runners.claude import Cancelled, Refused, continue_step

THREAD = {"channel": "test", "thread": "T1", "ref": "in:1", "principal": "owner"}
CONTINUE = 'Say "continue R1" to pick it up from its kept work.'


async def start_in_thread(h: Harness) -> None:
    res = await h.runner.start_run("proj", "fix the flaky test", "owner", report="now",
                                   channel_ref=THREAD)  # fmt: skip
    assert not isinstance(res, Refused), res
    await h.drain()


def failure_texts(h: Harness) -> list[str]:
    return [t for t in h.chan.texts() if "R1" in t and ("failed" in t or "timed out" in t)]


async def test_a_failed_run_posts_one_failure_message_in_its_thread(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="error")
    await start_in_thread(h)
    assert h.run("R1").status == "failed"
    (only,) = failure_texts(h)
    assert only.startswith("R1 (fix the flaky test) failed: "), only
    assert not any(t.startswith("Couldn't finish that") for t in h.chan.texts())
    assert CONTINUE not in only  # not a limit: nothing to continue


async def test_a_run_a_limit_stopped_says_continue_in_its_one_message(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="hang", max_session_s=1)
    await start_in_thread(h)
    run = h.run("R1")
    assert run.status == "failed" and run.failure == "timed out at 1s (work kept: /runs R1)"
    (only,) = failure_texts(h)
    assert only.splitlines()[0].startswith("R1 (fix the flaky test) failed: timed out at 1s")
    assert only.splitlines()[-1] == CONTINUE
    assert not any(t.startswith("Couldn't finish that") for t in h.chan.texts())


class RefusesEnds(Chan):
    """Takes every send but a run's end text (``run:<code>:<status>``), which nobody takes."""

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        if dedupe_key is not None and dedupe_key.startswith("run:"):
            raise ChannelRefused("no")
        return await super().send(principal, text, thread=thread, dedupe_key=dedupe_key)


async def test_a_refused_end_text_leaves_one_couldnt_finish_line(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """The end text didn't go out: the thread hears the failure once, as ``Couldn't finish
    that``, with the next step."""
    h = build(tmp_path, db, scenario="hang", max_session_s=1)
    chan = RefusesEnds()
    h.runner.channel = cast(Channel, chan)
    await start_in_thread(h)
    assert h.run("R1").status == "failed"
    failures = [t for t in chan.texts() if "R1" in t and "failed" in t]
    assert failures == [
        "Couldn't finish that: R1 failed (timed out at 1s (work kept: /runs R1)). " + CONTINUE
    ]


async def test_a_cancelled_run_says_so_once(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, scenario="hang")
    res = await h.runner.start_run("proj", "fix the flaky test", "owner", report="now",
                                   channel_ref=THREAD)  # fmt: skip
    assert not isinstance(res, Refused), res
    t = h.spawn_loop()
    await until(lambda: (h.tmp("R1") / "claude.pid").exists())
    assert isinstance(await h.runner.cancel_run("R1"), Cancelled)
    await asyncio.wait_for(t, 20)
    said = [x for x in h.chan.texts() if "R1" in x and "cancelled" in x]
    assert said == ["R1 (fix the flaky test) was cancelled."], said


async def test_continue_step_is_only_for_a_run_a_limit_stopped(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="error")
    await start_in_thread(h)
    run = h.run("R1")
    assert continue_step(replace(run, failure="timed out at 45m (work kept: /runs R1)")) == (
        CONTINUE
    )
    assert continue_step(run) is None  # Claude failed: a fresh run, not a continue
    assert continue_step(replace(run, status="cancelled", failure="timed out at 45m")) is None

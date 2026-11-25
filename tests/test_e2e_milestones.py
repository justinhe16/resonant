"""PER-323: runs report back like a teammate, end to end on the fake Slack channel.

The real daemon (router, gate, loop, ``ClaudeRunner``) over ``e2e.FakeChannel("slack")`` as in
``test_e2e_status_lines``. The fake session shows its calls and their results (``--step``):
a PR opened, merged, deployed to staging.

- Past the quiet window, the asking thread gets: the start line (PER-319), one line per
  milestone, and one final result with the suggested next step; nothing twice.
- A run under a minute (the default quiet window) posts only its final result.
- ``runs.milestones: off`` (an iMessage-only instance's default) posts none either.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

import pytest
from test_e2e_runner import REPO, FakeChatDB, RunModel, World, attach_claude, chat, make_world
from test_runner_claude import until

from resonant.config import ChannelsConfig, RunsConfig
from resonant.daemon import Daemon
from resonant.e2e import FakeChannel
from resonant.ops.fakes import FakeOpsRunner
from resonant.principals import load_principals
from resonant.runners import milestones as ms
from resonant.secrets import SecretStore
from resonant.store.db import kv_get

__all__ = ["chat"]  # the fixture

LEAD = "U1"
TEAM = f"""\
principals:
  lead: {{role: owner, identities: ["slack:{LEAD}"]}}
"""
THREAD = "C0TEAM:1700000000.000100"
PR_URL = "https://github.com/own/app/pull/41"
STEPS = [
    {"command": "gh pr create --fill", "result": f"Creating pull request\n{PR_URL}\n"},
    {"command": "gh pr checks 41 --watch", "result": "test\tfail\t1m\n1 failing", "error": True},
    {"command": "gh pr merge 41 --squash", "result": "Merged pull request #41"},
    {"command": "fly deploy -a app-staging", "result": "v12 deployed successfully"},
]


@contextlib.asynccontextmanager
async def slack_run(
    tmp_path: Path,
    home: Path,
    chat: FakeChatDB,
    mp: pytest.MonkeyPatch,
    *,
    steps: Sequence[dict[str, Any]] = STEPS,
    quiet_s: float | None = 0.0,
    mode: str | None = "thread",
) -> AsyncGenerator[tuple[World, Daemon, FakeChannel]]:
    extra = [x for s in steps for x in ("--step", json.dumps(s))]
    opts = {} if quiet_s is None else {"milestone_quiet_s": quiet_s}
    w = make_world(tmp_path, home, chat, mp, extra=extra, runner_opts=opts)
    (home / "principals.yaml").write_text(TEAM)
    runs = RunsConfig.model_validate({"milestones": mode})
    w.settings = w.settings.model_copy(update={"channels": ChannelsConfig(), "runs": runs})
    fake = FakeChannel("slack", load_principals(home / "principals.yaml"), vouch=True)
    daemon = w.daemon = Daemon(
        w.settings,
        model=RunModel(),
        osa_runner=w.osa,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(w.keychain),
        channels={"slack": fake},
    )
    attach_claude(w, daemon)
    task = asyncio.create_task(daemon.run())
    assert daemon.stack is not None
    hub = daemon.stack.channel
    await until(lambda: hub.health()["last_read_at"] is not None, 10, "the channels to read")
    try:
        yield w, daemon, fake
    finally:
        if not task.done():
            daemon.stop()
        assert await asyncio.wait_for(task, 20) == 0
        w.daemon = None


async def start(d: Daemon) -> None:
    assert d.stack is not None and d.stack.claude is not None
    ref = {"channel": "slack", "thread": THREAD, "ref": "slack-in:9", "principal": "lead"}
    started = await d.stack.claude.start_run(REPO, "fix the flaky test and ship it to staging",
                                             "lead", report="now", channel_ref=ref,
                                             announce=True)  # fmt: skip
    assert getattr(started, "code", None) == "R1", started


def thread_texts(fake: FakeChannel) -> list[str]:
    return [m.text for m in fake.sent if m.thread == THREAD]


def final(fake: FakeChannel) -> list[str]:
    return [t for t in thread_texts(fake) if t.startswith("R1") and "opened PR #41 (" in t]


MILESTONES = [
    f"R1 opened PR #41: {PR_URL}",
    "R1: CI failed on PR #41; fixing it.",
    "R1 merged PR #41.",
    "R1 deployed to staging.",
]


async def test_a_slack_run_posts_its_milestones_and_one_final_result(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with slack_run(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        await start(d)
        await until(lambda: bool(final(fake)), 60, "the final result")
        texts = thread_texts(fake)
        assert texts[0] == "On it: starting R1 (fix the flaky test and ship it to staging)."
        lines = [t for t in texts if t in MILESTONES]
        assert lines == MILESTONES  # in order, each once
        (end,) = final(fake)
        assert texts.index(end) > max(texts.index(t) for t in MILESTONES)
        assert end.endswith(ms.NEXT_PROD)  # the suggested next step, last
        assert all(m.principal == "lead" for m in fake.sent if m.thread == THREAD)
        keys = {m.dedupe_key for m in fake.sent if m.text in MILESTONES}
        assert keys == {
            f"status:{d.conn.execute('SELECT task_id FROM runs').fetchone()[0]}:milestone:{k}"
            for k in ("pr_opened", "ci_failed", "merged", "deployed:staging")
        }
        record = kv_get(d.conn, f"{ms.KV_PREFIX}R1")
        assert {r["state"] for r in record.values()} == {"posted"}
        # nothing is said twice: a second flush (a restart's) finds nothing pending
        assert d.stack is not None and d.stack.claude is not None
        n = len(fake.sent)
        await d.stack.claude._post_milestones("R1")  # pyright: ignore[reportPrivateUsage]
        assert len(fake.sent) == n


async def test_a_run_under_a_minute_posts_only_its_final_result(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with slack_run(tmp_path, resonant_home, chat, monkeypatch, quiet_s=None) as (
        _w,
        d,
        fake,
    ):
        await start(d)
        await until(lambda: bool(final(fake)), 60, "the final result")
        texts = thread_texts(fake)
        assert not [t for t in texts if t in MILESTONES]
        (end,) = final(fake)
        assert end.endswith(ms.NEXT_PROD)  # the final result still says what it did
        record = kv_get(d.conn, f"{ms.KV_PREFIX}R1")
        assert {r["state"] for r in record.values()} == {"folded"}


async def test_milestones_off_posts_none(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with slack_run(tmp_path, resonant_home, chat, monkeypatch, mode="off") as (_w, d, fake):
        await start(d)
        await until(lambda: bool(final(fake)), 60, "the final result")
        assert not [t for t in thread_texts(fake) if t in MILESTONES]
        assert len(final(fake)) == 1

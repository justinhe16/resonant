"""PER-341: one voice for run updates, end to end on the fake Slack channel.

The real daemon over ``e2e.FakeChannel("slack")`` as in ``test_e2e_milestones`` (a run that
opens a PR, fails CI, merges it and deploys to staging), with ``runner.update_voice``:

- ``brain``: the start line, each milestone and the final result are Claude's words (the
  real ``ClaudeVoice`` over a scripted call), each sent once with the template's dedupe key;
  no ``Along the way`` line in the thread (the run's ``did:`` list keeps it).
- ``brain`` with a voice that fails: the code-built lines go out (never a lost update).
- ``template`` (the default): exactly the code-built lines, ``Along the way`` included.

Nothing touches the real ~/.resonant, ~/code, Keychain or claude.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Any

import pytest
from test_e2e_milestones import (
    MILESTONES,
    PR_URL,
    THREAD,
    FakeChatDB,
    chat,
    slack_run,
    start,
    thread_texts,
)
from test_runner_claude import until
from voice_script import Script, scripted_voice

from resonant.brain.voice import TEAM_LINE
from resonant.daemon import Daemon
from resonant.runners.reports import shown_did
from resonant.runners.voice import CallResult

__all__ = ["chat"]  # the fixture

START = "On it: starting R1 (fix the flaky test and ship it to staging)."
WORDS = {
    "started": "R1 is picking up the flaky test now and will ship it to staging after.",
    "opened_pr": f"R1 opened PR #41 for the flaky test: {PR_URL}",
    "ci_failed": "CI failed on R1's PR #41; it's fixing that now.",
    "merged": "R1 merged PR #41, so the fix is in.",
    "deployed": "R1 deployed the fix to staging.",
    "opened_pr:end": "R1 is done: the flaky test is fixed in PR #41.",
}
_OUTCOME = re.compile(r'"outcome":"([a-z_]+)"')


class ByOutcome(Script):
    """Claude's words per update, picked by the facts' outcome (the final result is an
    ``outcome`` text with ``opened_pr``)."""

    async def __call__(
        self, argv: list[str], env: dict[str, str], cwd: Path, prompt: str, timeout_s: float
    ) -> CallResult:
        self.prompts.append(prompt)
        m = _OUTCOME.search(prompt)
        outcome = m.group(1) if m else ""
        if '"kind":"outcome"' in prompt:
            outcome += ":end"
        return CallResult("answered", WORDS.get(outcome, "nonsense R99"))


def brain(d: Daemon, tmp_path: Path, script: Script) -> None:
    """``runner.update_voice: brain`` with a scripted voice, before the run starts."""
    assert d.stack is not None and d.stack.claude is not None
    claude = d.stack.claude
    claude.cfg = claude.cfg.model_copy(update={"update_voice": "brain"})
    claude.voice = scripted_voice(d.conn, tmp_path, script)


async def run_to_end(d: Daemon) -> None:
    await start(d)
    await until(lambda: _status(d) not in ("queued", "running", "waiting"), 60, "the run end")
    await until(lambda: _task_done(d), 30, "the final result")


def _status(d: Daemon) -> str:
    return str(d.conn.execute("SELECT status FROM runs WHERE code = 'R1'").fetchone()[0])


def _task_done(d: Daemon) -> bool:
    row = d.conn.execute(
        "SELECT t.status FROM tasks t JOIN runs r ON r.task_id = t.id WHERE r.code = 'R1'"
    ).fetchone()
    return row is not None and row[0] in ("done", "failed")


async def test_brain_words_the_start_each_milestone_and_the_result(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = ByOutcome()
    async with slack_run(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        brain(d, tmp_path, script)
        await run_to_end(d)
        texts = thread_texts(fake)
        assert texts[0] == WORDS["started"]
        assert START not in texts
        mid = [WORDS[k] for k in ("opened_pr", "ci_failed", "merged", "deployed")]
        assert [t for t in texts if t in mid] == mid  # in order, each once
        assert not [t for t in texts if t in MILESTONES]  # no code-built line
        (end,) = [t for t in texts if t.startswith(WORDS["opened_pr:end"])]
        assert texts.index(end) == len(texts) - 1
        assert not [t for t in texts if "Along the way" in t]
        assert shown_did(d.conn, "R1")  # the dashboard's did: list keeps the side effects
        task_id = d.conn.execute("SELECT task_id FROM runs").fetchone()[0]
        keys = {m.dedupe_key for m in fake.sent if m.thread == THREAD}
        assert f"status:{task_id}:started" in keys
        assert f"status:{task_id}:milestone:pr_opened" in keys
        # one call per update: start, four milestones, the result
        assert script.calls == 6
        assert all(TEAM_LINE not in p for p in script.prompts)  # the lead is the owner


async def test_brain_falls_back_to_the_code_built_lines_when_the_voice_fails(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with slack_run(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        brain(d, tmp_path, Script(CallResult("failed")))
        await run_to_end(d)
        texts = thread_texts(fake)
        assert texts[0] == START
        assert [t for t in texts if t in MILESTONES] == MILESTONES
        (end,) = [t for t in texts if t.startswith("R1") and "opened PR #41 (" in t]
        assert "Along the way" not in end  # brain mode: the thread never gets it


async def test_brain_with_no_voice_is_the_template(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with slack_run(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        assert d.stack is not None and d.stack.claude is not None
        claude = d.stack.claude
        claude.cfg = claude.cfg.model_copy(update={"update_voice": "brain"})
        claude.voice = None
        await run_to_end(d)
        texts = thread_texts(fake)
        assert texts[0] == START
        assert [t for t in texts if t in MILESTONES] == MILESTONES
        assert [t for t in texts if "Along the way" in t]


async def test_template_is_the_code_built_lines(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = ByOutcome()
    async with slack_run(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        assert d.stack is not None and d.stack.claude is not None
        assert d.stack.claude.cfg.update_voice == "template"
        d.stack.claude.voice = scripted_voice(d.conn, tmp_path, script)
        await run_to_end(d)
        texts = thread_texts(fake)
        assert texts[0] == START
        assert [t for t in texts if t in MILESTONES] == MILESTONES
        # the outcome is voiced as before (PER-273), with its Along the way line
        assert script.calls == 1 and '"kind":"outcome"' in script.prompts[0]
        assert [t for t in texts if "Along the way" in t]


async def test_a_cancel_drops_a_start_line_still_being_written(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start line's wording is still out when the run is cancelled: it never posts."""
    async with slack_run(tmp_path, resonant_home, chat, monkeypatch) as (_w, d, fake):
        assert d.stack is not None and d.stack.claude is not None
        claude = d.stack.claude
        brain(d, tmp_path, ByOutcome())
        gate = asyncio.Event()
        claude._announce = _held(claude._announce, gate)  # pyright: ignore[reportPrivateUsage]
        await start(d)
        await until(lambda: "R1" in claude._announcing, 20, "the start line")  # pyright: ignore[reportPrivateUsage]
        job = claude._announcing["R1"]  # pyright: ignore[reportPrivateUsage]
        await claude._on_cancelled("R1")  # pyright: ignore[reportPrivateUsage]
        with contextlib.suppress(asyncio.CancelledError):
            await job
        assert job.cancelled()
        assert "R1" not in claude._announcing  # pyright: ignore[reportPrivateUsage]
        await claude._announced("R1")  # pyright: ignore[reportPrivateUsage]
        assert WORDS["started"] not in thread_texts(fake)


def _held(announce: Any, gate: asyncio.Event) -> Any:
    async def held(task: Any, run: Any) -> None:
        await gate.wait()  # never set: the wording is still out
        await announce(task, run)

    return held

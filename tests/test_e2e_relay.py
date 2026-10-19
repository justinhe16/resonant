"""EM-5 end to end: the owner's iMessages reach the right worker (docs/employee-plan.md D3).

The real daemon, router, runner and worker CLI, with the fake claude asking through
``resonant ask`` (``--ask-map`` gives each run its own question). The brain is the scripted
model plus a word-overlap pick for the relay's schema.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import test_e2e_runner
from test_e2e_runner import REPO, FakeChatDB, World, chat, make_world, running, settled
from test_runner_claude import until

from resonant.models import ChatMessage, ModelReply, SpanFactory
from resonant.store import pulse as pulse_store
from resonant.store import questions as qs
from resonant.store.db import iso, utcnow
from resonant_sdk import ToolSpec

__all__ = ["chat"]  # the fixture

TYPO_Q = "Fix both READMEs or just the top one?"
DOCS_Q = "Should the docs keep the changelog page?"
ASKS = {"typo": TYPO_Q, "docs": DOCS_Q}


class RelayModel(test_e2e_runner.RunModel):
    """RunModel plus the relay's pick: the question sharing the most words (4+ letters) with
    the message, confidently; no overlap is an unsure pick."""

    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        max_tokens: int = 512,
        temperature: float = 0.0,
        spans: SpanFactory | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> ModelReply:
        if "run" not in schema.get("properties", {}):
            return await super().chat_json(
                messages, schema=schema, max_tokens=max_tokens, temperature=temperature
            )
        body = messages[-1]["content"]
        text = body.split("<message>", 1)[1].split("</message>", 1)[0].lower()
        said = set(re.findall(r"[a-z]{4,}", text))
        scores = {
            code: len(said & set(re.findall(r"[a-z]{4,}", q.lower())))
            for code, q in re.findall(r'^- (R\d+) \(.*?\): "(.*?)"', body, re.MULTILINE)
        }
        best = max(scores.values(), default=0)
        top = [c for c, n in scores.items() if n == best]
        data = {"run": top[0], "confidence": 0.9 if best and len(top) == 1 else 0.3}
        return ModelReply(json.dumps(data), [], data, 1, 10, 5)


@pytest.fixture(autouse=True)
def relay_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_e2e_runner, "RunModel", RelayModel)


async def text_like(w: World, pred: Callable[[str], bool], what: str) -> str:
    found: list[str] = []

    def seen() -> bool:
        found[:] = [t for t in w.texts() if pred(t)]
        return bool(found)

    await until(seen, 30, what)
    return found[-1]


async def ask(w: World, text: str, pred: Callable[[str], bool], what: str) -> str:
    """Text the Mini and wait for a new text that ``pred`` accepts."""
    before = len(w.texts())
    w.chat.add_message(handle=test_e2e_runner.OWNER_PHONE, text=text)
    found: list[str] = []

    def seen() -> bool:
        found[:] = [t for t in w.texts()[before:] if pred(t)]
        return bool(found)

    await until(seen, 30, what)
    return found[0]


def prompts(w: World, code: str) -> list[str]:
    return [c["prompt"] for c in json.loads((w.tmp(code) / "argv.json").read_text())]


async def two_asking(w: World) -> None:
    await ask(w, f"/run {REPO} fix the typo in the README", lambda t: t.startswith("On it: R1"),
              "R1 started")  # fmt: skip
    await text_like(w, lambda t: t == f"R1 asks: {TYPO_Q}", "R1's question")
    await until(settled(w, "R1", "waiting"), 20, "R1 waits")
    await ask(w, f"/run {REPO} update the docs", lambda t: t.startswith("On it: R2"),
              "R2 started")  # fmt: skip
    asked = await text_like(w, lambda t: t.startswith("R2 asks:"), "R2's question")
    assert asked == f"R2 asks: {DOCS_Q}\nOther runs are waiting too: reply R2 <answer>."
    await until(settled(w, "R2", "waiting"), 20, "R2 waits")


async def test_two_runs_ask_and_replies_go_by_content_by_code_and_after_which_one(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = ["--ask-map", json.dumps(ASKS)]
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="happy", extra=extra,
                   runner_opts={"ask_block_s": 0.5})  # fmt: skip
    async with running(w):
        await two_asking(w)
        # by content: the brain picks R2, and the confirmation names its question
        line = await ask(w, "yes keep the changelog page please", lambda t: t.startswith("→"),
                         "the content pick")  # fmt: skip
        assert line == "→ R2 (Should the docs keep the changelog page?)"
        await text_like(w, lambda t: t.startswith("R2 done"), "R2 done")
        assert prompts(w, "R2")[1].startswith(
            f'The owner answered your question "{DOCS_Q}": yes keep the changelog page please'
        )
        # a third run asks; an unclear reply gets "which one?" and the code resolves it
        await ask(w, f"/run {REPO} tidy the docs again", lambda t: t.startswith("On it: R3"),
                  "R3 started")  # fmt: skip
        await until(settled(w, "R3", "waiting"), 20, "R3 waits")
        which = await ask(w, "sure", lambda t: t.startswith("For "), "the which-one prompt")
        assert which == f"For R1 ({TYPO_Q}) or R3 ({DOCS_Q})?"
        assert await ask(w, "R1", lambda t: t.startswith("→"), "the stash") == "→ R1"
        await text_like(w, lambda t: t.startswith("R1 done"), "R1 done")
        assert prompts(w, "R1")[1].startswith(f'The owner answered your question "{TYPO_Q}": sure')
        # by code, the code stripped
        assert await ask(w, "R3: drop it", lambda t: t.startswith("→"), "by code") == "→ R3"
        await text_like(w, lambda t: t.startswith("R3 done"), "R3 done")
        answered = f'The owner answered your question "{DOCS_Q}": drop it'
        assert prompts(w, "R3")[1].startswith(answered)
        # every question's pulse item is cleared
        assert not [i for i in pulse_store.open_items(w.conn, utcnow())
                    if i.key.startswith("question/")]  # fmt: skip


async def test_a_plain_yes_goes_to_the_only_question_and_options_resolve(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No approval prompt is open, so ``yes`` is an answer, not "Nothing is waiting"."""
    asks = {"typo": "Which file?|README.md,docs/index.md"}
    extra = ["--ask-map", json.dumps(asks)]
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="happy", extra=extra,
                   runner_opts={"ask_block_s": 0.5})  # fmt: skip
    async with running(w):
        await ask(w, f"/run {REPO} fix the typo in the README", lambda t: t.startswith("On it"),
                  "started")  # fmt: skip
        asked = await text_like(w, lambda t: t.startswith("R1 asks:"), "the question")
        assert asked == "R1 asks: Which file?\n(a) README.md (b) docs/index.md"
        await until(settled(w, "R1", "waiting"), 20, "the wait")
        assert await ask(w, "use the second option", lambda t: t.startswith("→"), "answer") == (
            "→ R1"
        )
        await text_like(w, lambda t: t.startswith("R1 done"), "done")
        assert prompts(w, "R1")[1].startswith(
            'The owner answered your question "Which file?": docs/index.md'
        )


async def test_tell_r1_reaches_a_running_worker_as_a_message(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="happy",
                   extra=["--wait-message", "20"])  # fmt: skip
    async with running(w):
        await ask(w, f"/run {REPO} fix the typo in the README", lambda t: t.startswith("On it"),
                  "started")  # fmt: skip
        await until(settled(w, "R1", "running"), 20, "running")
        await until(lambda: (w.tmp() / "cli.json").exists(), 20, "the first note")
        line = await ask(w, "tell R1 to also update the README", lambda t: t.startswith("→"),
                         "the message")  # fmt: skip
        assert line == "→ R1 (message)"
        await text_like(w, lambda t: t.startswith("R1 done"), "done")
        # RC-1: written into the live session as a user message
        assert (
            "Message from the owner: also update the README" in (w.tmp() / "turns.json").read_text()
        )


async def test_a_timed_out_question_and_a_late_answer(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="happy",
                   extra=["--ask", TYPO_Q], runner_opts={"ask_block_s": 0.5})  # fmt: skip
    async with running(w):
        await ask(w, f"/run {REPO} fix the typo in the README", lambda t: t.startswith("On it"),
                  "started")  # fmt: skip
        await until(settled(w, "R1", "waiting"), 20, "the wait")
        [q] = qs.for_run(w.conn, "R1")
        assert pulse_store.get(w.conn, "core", f"question/r1-{q.id}") is not None
        claude_task = "SELECT status FROM tasks WHERE runner = 'claude'"
        await until(lambda: w.conn.execute(claude_task).fetchone()[0] == "waiting", 20,
                    "the task's wait")  # fmt: skip
        # ask_timeout (4 h) passes; any message wakes the loop to notice
        w.conn.execute("UPDATE run_questions SET asked_at = ?",
                       (iso(utcnow() - timedelta(hours=5)),))  # fmt: skip
        w.conn.execute("UPDATE tasks SET wake_at = ? WHERE status = 'waiting'",
                       (iso(utcnow() - timedelta(seconds=1)),))  # fmt: skip
        for _ in range(10):  # nudge the loop until it notices (it may be mid-sleep)
            w.chat.add_message(handle=test_e2e_runner.OWNER_PHONE, text="/runs")
            with contextlib.suppress(AssertionError):
                await until(lambda: any(t.startswith("R1 done") for t in w.texts()), 3, "done")
                break
        await text_like(w, lambda t: t.startswith("R1 done"), "done after the timeout")
        assert prompts(w, "R1")[1].startswith(f'Your question "{TYPO_Q}": No answer from the owner')
        item = pulse_store.get(w.conn, "core", f"question/r1-{q.id}")
        assert item is not None and item.cleared
        late = await ask(w, "R1 just the top one", lambda t: t.startswith("R1 isn't"), "late")
        assert late == f"R1 isn't running ({w.run().status}); nothing sent."

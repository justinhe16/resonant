"""PER-328: one voice in a team thread, end to end on the fake Slack adapter.

The whole daemon in process with the fake Claude brain (``resonant/e2e/fake_claude.py``)
through the real MCP relay, bridge, gate and executor (``test_e2e_claude_session``'s
``start(slack=True)``: the owner and an approver). A replay of a thread where the owner asked
for something, Claude asked for a yes, the owner said no and corrected it in the same breath:

- the ack in a shared thread is plain ``On it.``, never the owner's queue label;
- a turn that ends in an approval posts the prompt alone (its plain line says what the yes
  is for, without "as <the approver> asked"); Claude's text about it isn't posted again;
- the decline and the message sent with it get one answer from one Claude turn;
- no failure is said twice (no ``Not done`` or ``Couldn't finish that`` under Claude's
  answer).

Nothing touches the real ~/.resonant, ~/code, Keychain or claude.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from test_e2e_brain_shell import prompt_recorded
from test_e2e_claude_session import CLAUDE, World, chat, start

from resonant.e2e import FakeChannel, wait_until
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.runners import ask as ask_mod
from resonant.runners import continuation

__all__ = ["chat"]  # the fixture

OWNER_ID = "U1"  # the fixture's owner (an approver of everything)
THREAD = "C0TEAM:1700000000.000200"
CODE = re.compile(r"\[([A-Z0-9]+)\]")
WATCH: dict[str, Any] = {"what": "notes change", "tool": "list_notes", "args": {},
                         "when": "changed"}  # fmt: skip


def owner_name(w: World) -> str:
    return w.asks.owner[:1].upper() + w.asks.owner[1:]


def in_thread(fake: FakeChannel, since: int = 0) -> list[str]:
    return [m.text for m in fake.sent[since:] if m.thread == THREAD]


def ask_rows(w: World) -> list[dict[str, Any]]:
    return [{"id": r["id"], "status": r["status"], "cp": json.loads(r["checkpoint"] or "{}"),
             "result": json.loads(r["result"] or "{}")} for r in w.ask_tasks()]  # fmt: skip


async def test_the_thread_replay_is_one_prompt_then_one_answer(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path, slack=True) as w:
        fake = w.slack
        assert fake is not None
        name = owner_name(w)
        w.model.triage = CLAUDE
        w.claude(
            turns=[{
                "when": "redeploy the staging site",
                "calls": [{"name": "create_watch",
                           "args": {**WATCH, "approval_reason":
                                    f"Redeploy the backend to staging, as {name} asked"}}],
                "answer": "I'll redeploy the backend to staging once it's approved: {r0}",
            }],
            continued=[{"answer": "OK, not the backend. Redeploy the web app instead?"}],
        )  # fmt: skip
        assert fake.inject(OWNER_ID, "can you redeploy the staging site", thread=THREAD)
        await wait_until(lambda: any(CODE.search(t) for t in in_thread(fake)), 20)
        await prompt_recorded(w)  # pyright: ignore[reportArgumentType]
        await asyncio.sleep(0.3)
        # the turn ended in an approval: the prompt alone, its plain line without "as … asked"
        (prompt,) = in_thread(fake)
        assert prompt.startswith("Redeploy the backend to staging? [")
        assert "asked" not in prompt.split("\n", 1)[0]
        m = CODE.search(prompt)
        assert m is not None
        (first,) = ask_rows(w)
        assert first["cp"]["held"].startswith("I'll redeploy the backend")  # kept for history

        # the owner says no and corrects it in the same breath
        n = len(fake.sent)
        assert fake.inject(OWNER_ID, f"no {m.group(1)}", thread=THREAD)
        assert fake.inject(OWNER_ID, "the web app, not the backend", thread=THREAD)
        await wait_until(lambda: any(t.startswith("OK, not the backend") for t in
                                     in_thread(fake, n)), 30)  # fmt: skip
        await w.settled()
        await asyncio.sleep(0.5)
        said = in_thread(fake, n)
        assert said == ["OK, not the backend. Redeploy the web app instead?"], said
        assert not [t for t in said if "(after" in t or t.startswith(("Not done", "Couldn't"))]
        assert len(w.executions("create_watch")) == 0
        # one Claude turn took both: the decision, then the new message
        seen = w.session()["prompt"]
        assert seen.index(continuation.TAIL) < seen.index(ask_mod.FOLDED_HEAD)
        assert seen.rstrip().endswith("the web app, not the backend")
        assert "not run:" in seen and "said no" in seen
        rows = ask_rows(w)
        cont = [r for r in rows if "continuation" in r["cp"]]
        folded = [r for r in rows if r["result"].get("status") == "folded"]
        assert len(cont) == 1 and len(folded) == 1
        assert folded[0]["result"]["folded_into"] == cont[0]["id"]
        assert w.audits("ask.folded")[0]["detail"]["folded"] == [folded[0]["id"]]


async def test_a_queued_turns_ack_in_a_shared_thread_is_plain(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path, slack=True,
                     session={"ack_after_s": 0.2}) as w:  # fmt: skip
        fake = w.slack
        assert fake is not None
        w.model.triage = CLAUDE
        w.claude(turns=[{"when": "slow", "delay": 1.5, "answer": "first"},
                        {"when": "fix", "answer": "second"}])  # fmt: skip
        assert fake.inject(OWNER_ID, "why is R4 slow?", thread=THREAD)
        await wait_until(lambda: w.session_proc.busy, 10)
        assert fake.inject(OWNER_ID, "how do I fix the build?", thread=THREAD)
        await wait_until(lambda: "second" in in_thread(fake), 20)
        acks = [t for t in in_thread(fake) if t not in ("first", "second")]
        assert acks and set(acks) == {ask_mod.SHARED_ACK}, acks


async def test_a_continuation_that_cant_answer_lets_the_folded_turn_run(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude ended the continuation without an answer: its code-built text goes out and the
    message that came with the decision gets its own turn, as before."""
    async with start(resonant_home, chat, tmp_path, slack=True) as w:
        fake = w.slack
        assert fake is not None
        w.model.triage = CLAUDE
        w.claude(
            turns=[
                {"when": "watch my notes", "calls": [{"name": "create_watch", "args": WATCH}],
                 "answer": "Asked: {r0}"},
                {"when": "never mind", "answer": "Fine, nothing to watch."},
            ],
            continued=[{"answer": ""}],
        )  # fmt: skip
        assert fake.inject(OWNER_ID, "watch my notes for changes", thread=THREAD)
        await wait_until(lambda: any(CODE.search(t) for t in in_thread(fake)), 20)
        await prompt_recorded(w)  # pyright: ignore[reportArgumentType]
        m = next(CODE.search(t) for t in in_thread(fake) if CODE.search(t))
        assert m is not None
        n = len(fake.sent)
        assert fake.inject(OWNER_ID, f"no {m.group(1)}", thread=THREAD)
        assert fake.inject(OWNER_ID, "never mind then", thread=THREAD)
        await wait_until(lambda: "Fine, nothing to watch." in in_thread(fake, n), 30)
        await w.settled()
        said = in_thread(fake, n)
        # the decision, code-built, said once; then the message's own answer
        assert said[-1] == "Fine, nothing to watch."
        assert len([t for t in said if "said no" in t]) == 1, said
        assert not [t for t in said if t.startswith("Not done")]
        assert not [r for r in ask_rows(w) if r["result"].get("status") == "folded"]


async def test_a_continuation_that_asks_again_still_answers_the_folded_turn_once(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    """The folded turn is answered by the continuation's reply even when that turn ends in a
    new approval: the reply is posted (it answers the new message), then the prompt; the
    folded turn never runs on its own."""
    async with start(resonant_home, chat, tmp_path, slack=True) as w:
        fake = w.slack
        assert fake is not None
        w.model.triage = CLAUDE
        reason = {"approval_reason": "Watch the notes"}
        w.claude(
            turns=[{"when": "redeploy the staging site",
                    "calls": [{"name": "create_watch", "args": {**WATCH, **reason}}],
                    "answer": "{r0}"}],
            continued=[{"calls": [{"name": "create_watch",
                                   "args": {**WATCH, "what": "web app", **reason}}],
                        "answer": "OK, the web app then: {r0}"}],
        )  # fmt: skip
        assert fake.inject(OWNER_ID, "can you redeploy the staging site", thread=THREAD)
        await wait_until(lambda: any(CODE.search(t) for t in in_thread(fake)), 20)
        await prompt_recorded(w)  # pyright: ignore[reportArgumentType]
        m = next(CODE.search(t) for t in in_thread(fake) if CODE.search(t))
        assert m is not None
        n = len(fake.sent)
        assert fake.inject(OWNER_ID, f"no {m.group(1)}", thread=THREAD)
        assert fake.inject(OWNER_ID, "the web app, not the backend", thread=THREAD)
        await wait_until(lambda: any(t.startswith("OK, the web app") for t in
                                     in_thread(fake, n)), 30)  # fmt: skip
        await wait_until(lambda: any(r["result"].get("status") == "folded"
                                     for r in ask_rows(w)), 20)  # fmt: skip
        await asyncio.sleep(0.5)
        answers = [t for t in in_thread(fake, n) if not CODE.search(t)]
        assert len([t for t in answers if t.startswith("OK, the web app")]) == 1, answers
        assert len(w.audits("ask.folded")) == 1

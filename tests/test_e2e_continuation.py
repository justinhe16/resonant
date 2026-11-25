"""PER-321: after an approval, the brain picks the conversation back up, end to end.

Two worlds, both the whole daemon in process with the fake Claude brain
(``resonant/e2e/fake_claude.py``) through the real MCP relay, bridge, gate and executor:

- owner-only Resonant over iMessage with the fake ``linear`` extension (``test_e2e_batch``'s
  ``start``: ``save_issue`` and ``save_comment`` are L2 writes, run by the real process
  runner); a continuation's plans come from the plan's ``continued`` list.
- a team on the fake Slack adapter (``test_e2e_claude_session``'s ``start(slack=True)``: the
  owner and an approver), where the answer has to land in the asking thread.

Nothing touches the real ~/.resonant, ~/code, Keychain or claude.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_e2e_batch import CLAUDE, World, chat, start
from test_e2e_brain_shell import prompt_recorded
from test_e2e_claude_session import CLAUDE as SESSION_CLAUDE
from test_e2e_claude_session import start as session_start

from resonant.e2e import wait_until
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.loop.types import Task
from resonant.runners import continuation
from resonant.runners.ask import AskRunner
from resonant.store.db import atomic, iso, kv_set, utcnow

__all__ = ["chat"]  # the fixture

SAVE = {"name": "save_issue", "args": {"id": "RES-12", "state": "Done"}}
COMMENT = {"name": "save_comment", "args": {"issue": "RES-12", "body": "closed"}}
SAVE_LINE = 'Done: linear: save issue id="RES-12", state="Done"'
COMMENT_LINE = 'Done: linear: save comment body="closed", issue="RES-12"'
WATCH: dict[str, Any] = {"what": "notes change", "tool": "list_notes", "args": {},
                         "when": "changed"}  # fmt: skip
CODE = re.compile(r"^\[([A-Z0-9]+)\]")


def ask_rows(w: World) -> list[dict[str, Any]]:
    rows = w.rows("SELECT id, status, checkpoint, result FROM tasks WHERE runner = 'ask' "
                  "ORDER BY rowid")  # fmt: skip
    return [{"id": r["id"], "status": r["status"], "cp": json.loads(r["checkpoint"] or "{}"),
             "result": json.loads(r["result"] or "{}")} for r in rows]  # fmt: skip


def audits(w: World, action: str) -> list[dict[str, Any]]:
    rows = w.rows("SELECT detail FROM audit_log WHERE action = ? ORDER BY id", action)
    return [json.loads(r["detail"] or "{}") for r in rows]


async def settled(w: World) -> None:
    def done() -> bool:
        rows = ask_rows(w)
        return bool(rows) and all(
            r["status"] in ("done", "failed")
            or (r["status"] == "waiting" and r["cp"].get("phase") == "approval")
            for r in rows
        )

    await wait_until(done, 20)
    await w.asks.idle()


async def asked(w: World, text: str, **plan: Any) -> tuple[str, str]:
    """Claude calls a gated tool in its turn: its answer, then the approval prompt."""
    w.model.triage = CLAUDE
    w.claude(**plan)
    answer, prompt = await w.say(text, replies=2)
    await w.prompt_recorded()
    return answer, prompt


# --- owner-only Resonant (iMessage) -------------------------------------------------------------


async def test_yes_feeds_the_result_back_and_claudes_answer_replaces_the_done_line(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        answer, prompt = await asked(
            w, "close RES-12", calls=[SAVE], answer="{r0}",
            continued=[{"answer": "Closed RES-12; Linear confirmed it."}],
        )  # fmt: skip
        assert answer.startswith("asked the owner:") and "Reply yes" in prompt
        assert w.runs("save_issue") == []
        (done,) = await w.say("yes")
        # Claude's answer, with the change's code-built line: never a bare "Done: …"
        assert done == f"Closed RES-12; Linear confirmed it.\n{SAVE_LINE}"
        await settled(w)
        assert len(w.runs("save_issue")) == 1
        # Claude saw the tool, its args and its result, as untrusted data, in Resonant's words
        seen = w.session()["prompt"]
        assert seen.startswith(continuation.HEAD) and seen.endswith(continuation.TAIL)
        assert "The request it was for: close RES-12" in seen
        assert '1. save_issue {"id":"RES-12","state":"Done"}: approved and ran.' in seen
        assert re.search(r"<message>\{.*\"ok\":true.*\}</message>", seen)
        first, second = ask_rows(w)
        assert first["result"]["status"] == "continued"
        assert first["result"]["continued"] == second["id"]
        cont = second["cp"]["continuation"]
        assert (cont["depth"], cont["of"], cont["reports"]) == (1, first["id"], [SAVE_LINE])
        assert cont["key"].startswith("approval:")
        assert second["cp"]["tainted"] is True  # an extension's result is outside content
        assert second["result"]["status"] == "answered"
        (row,) = audits(w, "ask.continued")
        assert (row["depth"], row["calls"], row["ran"], row["not_run"]) == (1, 1, 1, 0)
        # one answer, nothing else
        await asyncio.sleep(0.3)
        assert not [m for m in w.osa.sent if m.text.startswith(("Done:", "Couldn't"))]
        # the answer is in the transcript for the brains, as outside text
        (text,) = w.rows("SELECT text, outside FROM system_texts WHERE ref = ?",
                         f"ask:{second['id']}")  # fmt: skip
        assert text["text"].startswith("Closed RES-12") and text["outside"] == 1


async def test_a_chain_of_two_gated_calls_ends_in_one_answer(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        await asked(
            w, "close RES-12 and say so on it", calls=[SAVE], answer="{r0}",
            continued=[
                {"calls": [COMMENT], "answer": "Closed it; {r0}"},
                {"answer": "RES-12 is closed and commented."},
            ],
        )  # fmt: skip
        middle, prompt = await w.say("yes", replies=2)
        # the continuation's next call asks again: its answer, then the second prompt
        assert middle.startswith("Closed it; asked the owner:") and middle.endswith(SAVE_LINE)
        assert "save comment" in prompt and "Reply yes" in prompt
        await w.prompt_recorded()
        assert len(w.runs("save_issue")) == 1 and w.runs("save_comment") == []
        (final,) = await w.say("yes")
        assert final == f"RES-12 is closed and commented.\n{COMMENT_LINE}"
        await settled(w)
        assert len(w.runs("save_issue")) == 1 and len(w.runs("save_comment")) == 1
        assert [r["status"] for r in w.rows("SELECT status FROM approvals ORDER BY rowid")] == [
            "approved", "approved"
        ]  # fmt: skip
        rows = ask_rows(w)
        assert len(rows) == 3
        assert [r["cp"].get("continuation", {}).get("depth") for r in rows] == [None, 1, 2]
        assert rows[2]["cp"]["continuation"]["request"] == "close RES-12 and say so on it"
        # the second call came after outside content: tainted, so it asked
        assert w.evaluated("save_comment")[0] == ("NeedsApproval", "tainted")
        assert len(audits(w, "ask.continued")) == 2


async def test_no_is_one_message_from_claude_and_nothing_runs(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        await asked(w, "close RES-12", calls=[SAVE], answer="{r0}",
                    continued=[{"answer": "Left RES-12 open: you said no."}])  # fmt: skip
        assert await w.say("no") == ["Left RES-12 open: you said no."]
        await settled(w)
        await asyncio.sleep(0.3)
        assert w.osa.sent[-1].text == "Left RES-12 open: you said no."  # nothing after it
        assert w.runs("save_issue") == []
        seen = w.session()["prompt"]
        assert "1. save_issue" in seen and "not run: Justin said no." in seen
        assert "approved and ran" not in seen  # nothing ran, so there is no result
        assert not [m for m in w.osa.sent if m.text.startswith(("Not done", "Couldn't"))]


async def test_expiry_is_one_message_from_claude(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        await asked(
            w,
            "close RES-12",
            calls=[SAVE],
            answer="{r0}",
            continued=[{"answer": "Nobody answered in time, so RES-12 is still open."}],
        )
        past = iso(utcnow() - timedelta(minutes=1))
        w.conn.execute("UPDATE approvals SET expires_at = ? WHERE status = 'pending'", (past,))
        w.conn.execute("UPDATE tasks SET wake_at = ? WHERE runner = 'ask'", (past,))
        w.conn.commit()
        n = len(w.osa.sent)
        w.daemon.loop._wakeup.set()  # pyright: ignore[reportPrivateUsage]  # wake_at passed
        await w.osa.wait_for(n + 1, 20)
        await settled(w)
        await asyncio.sleep(0.3)
        assert [m.text for m in w.osa.sent[n:]] == [
            "Nobody answered in time, so RES-12 is still open."
        ]
        assert w.runs("save_issue") == []
        assert "not run: the approval expired before anyone answered." in w.session()["prompt"]


async def test_a_crash_before_the_continuation_feeds_the_result_back_once(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The call ran, then the step died before the continuation was queued: the retry
    replays the recorded call (never runs it again) and feeds it back exactly once."""
    async with start(resonant_home, chat, tmp_path) as w:
        await asked(w, "close RES-12", calls=[SAVE], answer="{r0}",
                    continued=[{"answer": "Closed RES-12."}])  # fmt: skip
        real = AskRunner._continue  # pyright: ignore[reportPrivateUsage]
        crashed: list[str] = []

        def crash_once(self: AskRunner, task: Task, *args: Any) -> Any:
            if not crashed:
                crashed.append(task.id)
                raise RuntimeError("daemon died here")
            return real(self, task, *args)

        monkeypatch.setattr(AskRunner, "_continue", crash_once)
        n = len(w.osa.sent)
        w.chat.add_message(handle="+15551234567", text="yes")
        await wait_until(lambda: bool(crashed) and len(w.runs("save_issue")) == 1, 20)
        # the retry is due at once (a restart picks it up the same way)
        await wait_until(lambda: ask_rows(w)[0]["status"] == "queued", 20)
        w.conn.execute("UPDATE tasks SET wake_at = ? WHERE id = ?",
                       (iso(utcnow() - timedelta(seconds=1)), crashed[0]))  # fmt: skip
        w.conn.commit()
        w.daemon.loop._wakeup.set()  # pyright: ignore[reportPrivateUsage]
        await w.osa.wait_for(n + 1, 20)
        await settled(w)
        await asyncio.sleep(0.3)
        assert [m.text for m in w.osa.sent[n:]] == [f"Closed RES-12.\n{SAVE_LINE}"]
        assert len(w.runs("save_issue")) == 1  # replayed, never run twice
        assert len(ask_rows(w)) == 2 and len(audits(w, "ask.continued")) == 1


async def test_a_second_resume_of_the_same_approval_queues_nothing(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    """A restart after the continuation was queued but before the approval's step ended:
    the step runs again and finds the continuation by its approval key."""
    async with start(resonant_home, chat, tmp_path) as w:
        await asked(w, "close RES-12", calls=[SAVE], answer="{r0}",
                    continued=[{"answer": "Closed RES-12."}])  # fmt: skip
        (waiting,) = ask_rows(w)
        before = waiting["cp"]  # the approval phase, as a crash would leave it
        assert before["phase"] == "approval"
        (done,) = await w.say("yes")
        assert done == f"Closed RES-12.\n{SAVE_LINE}"
        await settled(w)
        n = len(w.osa.sent)
        w.conn.execute(
            "UPDATE tasks SET status = 'queued', checkpoint = ?, wake_at = NULL, "
            "result = NULL WHERE id = ?",
            (json.dumps(before), waiting["id"]),
        )
        w.conn.commit()
        w.daemon.loop._wakeup.set()  # pyright: ignore[reportPrivateUsage]
        await wait_until(lambda: ask_rows(w)[0]["status"] == "done", 20)
        await settled(w)
        await asyncio.sleep(0.3)
        assert len(w.osa.sent) == n  # nothing said twice
        assert len(ask_rows(w)) == 2 and len(audits(w, "ask.continued")) == 1
        assert len(w.runs("save_issue")) == 1
        assert ask_rows(w)[0]["result"]["continued"] == ask_rows(w)[1]["id"]


async def test_past_the_cap_the_code_built_line_goes_out(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(continuation, "MAX_CONTINUATIONS", 1)
    async with start(resonant_home, chat, tmp_path) as w:
        await asked(
            w, "close RES-12 and say so on it", calls=[SAVE], answer="{r0}",
            continued=[{"calls": [COMMENT], "answer": "{r0}"}, {"answer": "never asked"}],
        )  # fmt: skip
        _middle, _prompt = await w.say("yes", replies=2)
        await w.prompt_recorded()
        (done,) = await w.say("yes")
        assert done == COMMENT_LINE  # no third turn: the old line
        await settled(w)
        assert len(ask_rows(w)) == 2
        (capped,) = audits(w, "ask.continue_capped")
        assert capped["depth"] == 1


async def test_paused_after_the_yes_sends_the_code_built_text(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The continuation was queued, then Resonant was paused: Claude isn't asked; the thread
    still hears what was decided, code-built."""
    async with start(resonant_home, chat, tmp_path) as w:
        await asked(w, "close RES-12", calls=[SAVE], answer="{r0}",
                    continued=[{"answer": "never asked"}])  # fmt: skip
        real = AskRunner._continue  # pyright: ignore[reportPrivateUsage]

        def then_pause(self: AskRunner, task: Task, *args: Any) -> Any:
            out = real(self, task, *args)
            with atomic(w.conn):  # the kill switch's flag (its hooks aren't the point)
                kv_set(w.conn, "paused", True)
            return out

        monkeypatch.setattr(AskRunner, "_continue", then_pause)
        (done,) = await w.say("yes")
        assert done == SAVE_LINE
        await settled(w)
        assert "never asked" not in [m.text for m in w.osa.sent]


# --- a team thread (fake Slack) ----------------------------------------------------------------


async def test_in_a_thread_the_approvers_yes_brings_claudes_answer_to_that_thread(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    thread = "C0TEAM:1700000000.000100"
    async with session_start(resonant_home, chat, tmp_path, slack=True) as w:
        fake = w.slack
        assert fake is not None
        w.model.triage = SESSION_CLAUDE
        w.claude(calls=[{"name": "create_watch", "args": WATCH}], answer="Asked: {r0}",
                 continued=[{"answer": "Watching your notes now (W1)."}])  # fmt: skip
        assert fake.inject("U1", "watch my notes for changes", thread=thread)
        await wait_until(lambda: any(CODE.match(m.text) for m in fake.sent), 20)
        m = next(CODE.match(m.text) for m in fake.sent if CODE.match(m.text))
        assert m is not None
        await prompt_recorded(w)
        n = len(fake.sent)
        assert fake.inject("U2", f"yes {m.group(1)}", thread=thread)
        await wait_until(lambda: any(s.text.startswith("Watching your notes") for s in fake.sent),
                         20)  # fmt: skip
        await w.settled()
        await asyncio.sleep(0.3)
        after = [s for s in fake.sent[n:] if s.thread == thread]
        answer = [s for s in after if s.text.startswith("Watching your notes")]
        assert len(answer) == 1 and answer[0].thread == thread
        assert answer[0].text.startswith("Watching your notes now (W1).")
        assert not [s for s in after if s.text.startswith(("Done:", "Couldn't"))]
        assert len(w.executions("create_watch")) == 1
        seen = w.session()["prompt"]
        assert f"Thread: {thread}" in seen and "1. create_watch" in seen
        assert seen.endswith(continuation.TAIL)

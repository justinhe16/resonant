"""BR-7 end to end (docs/brain-plan.md D10): screenshots, PDFs and voice notes, links, and
watches, through the whole daemon (``test_e2e_brain.start``: a FakeChatDB, the recording
osascript, the scripted model and the fake claude CLI's ask mode). Attachments are files
under the test's read root; whisper is a fake script. Nothing touches ~/.resonant, ~/code,
Messages, the Keychain or the real claude."""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_e2e_brain import ANSWER, Brain, start

from resonant.brain import watches as watch_store
from resonant.brain.context import BRAIN_SYSTEM
from resonant.brain.triage import CANNED_ACKS
from resonant.e2e import wait_until
from resonant.gateway.imessage.fixtures import FakeAttachment, FakeChatDB
from resonant.runners.media import VOICE_NEEDS_WHISPER
from resonant.runners.watch import queue_due
from resonant.store.db import utcnow

OWNER_PHONE = "+15551234567"


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[FakeChatDB]:
    db = FakeChatDB(tmp_path / "chat.db")
    yield db
    db.close()


def attach(b: Brain, name: str, data: bytes = b"\x89PNG fake") -> Path:
    d = b.code / "Attachments"
    d.mkdir(exist_ok=True)
    (d / name).write_bytes(data)
    return d / name


async def send(b: Brain, text: str, atts: list[FakeAttachment], replies: int) -> list[str]:
    n = len(b.osa.sent)
    b.chat.add_message(handle=OWNER_PHONE, text=text, attachments=atts)
    await b.osa.wait_for(n + replies, 15)
    return [m.text for m in b.osa.sent[n : n + replies]]


def ask_rows(b: Brain) -> list[dict[str, Any]]:
    rows = b.rows("SELECT checkpoint FROM tasks WHERE runner = 'ask' ORDER BY rowid")
    return [json.loads(r["checkpoint"]) for r in rows]


def notes(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "claude-notes" / "argv.json").read_text())


def not_quiet() -> dict[str, Any]:
    """Quiet hours (UTC) that never hold a notice sent in the next few minutes."""
    start_h = (utcnow() + timedelta(hours=3)).hour
    return {
        "timezone": "UTC",
        "pulse": {"quiet_hours": f"{start_h:02d}:00-{(start_h + 1) % 24:02d}:00"},
    }


def fake_whisper(tmp_path: Path, said: str) -> Path:
    path = tmp_path / "whisper-cli"
    path.write_text(f"#!{sys.executable}\nprint({said!r})\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# --- images and PDFs -----------------------------------------------------------------------


async def test_a_screenshot_goes_to_claude_in_intents_mode(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path, mode="intents") as b:
        shot = attach(b, "Screenshot.png")
        replies = await send(b, "what's this error?", [FakeAttachment(str(shot), "image/png")], 2)
        assert replies == ["Looking into it…", ANSWER]
        assert b.model.requests == [] and b.model.calls["label"] == 0  # no model on the way
        (cp,) = ask_rows(b)
        assert cp["kind"] == "image" and cp["question"] == "what's this error?"
        assert cp["attachments"] == [str(shot.resolve())]
        seen = notes(tmp_path)
        assert seen["files"] == ["attachment-1.png"]
        assert "Look at the attached file(s)" in seen["prompt"]
        (media,) = b.rows("SELECT * FROM tasks WHERE runner = 'media'")
        assert media["status"] == "done"
        call = [a for a in b.audits("gate.evaluate") if a["tool"] == "ask_claude"]
        assert [a["detail"]["origin"] for a in call] == ["explicit"]


async def test_a_pdf_alone_gets_a_default_question(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as b:
        pdf = attach(b, "invoice.pdf", b"%PDF-1.4")
        replies = await send(b, "￼", [FakeAttachment(str(pdf), "application/pdf")], 2)
        assert replies == ["Looking into it…", ANSWER]
        (cp,) = ask_rows(b)
        assert cp["question"] == "Summarise this PDF briefly." and b.model.requests == []


async def test_a_file_outside_the_allowed_roots_is_refused(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as b:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "x.png").write_bytes(b"png")
        (reply,) = await send(b, "?", [FakeAttachment(str(elsewhere / "x.png"), "image/png")], 1)
        assert reply.startswith("Can't attach that:") and ask_rows(b) == []


# --- voice notes ---------------------------------------------------------------------------


async def test_voice_without_whisper_says_so(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as b:
        voice = attach(b, "Audio Message.caf", b"caff")
        (reply,) = await send(b, "￼", [FakeAttachment(str(voice), "audio/x-caf")], 1)
        assert reply == VOICE_NEEDS_WHISPER and ask_rows(b) == [] and b.model.requests == []


async def test_voice_is_transcribed_and_answered_like_text(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    whisper = fake_whisper(tmp_path, " Where is mycelium? ")
    async with start(resonant_home, chat, tmp_path, brain={"whisper_path": str(whisper)}) as b:
        voice = attach(b, "Audio Message.caf", b"caff")
        b.model.push("It's in ~/code/mycelium.")
        (reply,) = await send(b, "￼", [FakeAttachment(str(voice), "audio/x-caf")], 1)
        assert reply == "It's in ~/code/mycelium."
        (req,) = b.model.requests
        assert "Where is mycelium?" in req.messages[-1]["content"]
        (event,) = b.rows("SELECT payload, dedupe_key FROM events WHERE dedupe_key LIKE '%:voice'")
        assert json.loads(event["payload"])["voice"] is True
        assert not (resonant_home / "media").exists() or not any(
            (resonant_home / "media").iterdir()
        )


async def test_a_spoken_yes_never_approves(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    whisper = fake_whisper(tmp_path, "yes")
    async with start(resonant_home, chat, tmp_path, brain={"whisper_path": str(whisper)}) as b:
        b.model.push(
            [
                (
                    "create_watch",
                    {
                        "what": "milk noted",
                        "tool": "list_notes",
                        "args": {},
                        "when": "contains",
                        "text": "milk",
                    },
                )
            ],
        )
        _answer, prompt = await b.say("tell me when milk is on my notes", replies=2)
        assert "Reply yes" in prompt
        await b.prompt_recorded()
        b.model.push("Type yes to approve it.")
        voice = attach(b, "Audio Message.caf", b"caff")
        (reply,) = await send(b, "￼", [FakeAttachment(str(voice), "audio/x-caf")], 1)
        assert reply == "Type yes to approve it."
        assert b.rows("SELECT status FROM approvals")[0]["status"] == "pending"
        (done,) = await b.say("yes")
        assert done.startswith("Watching W1: milk noted, every 1h until ")
        w = watch_store.get(b.conn, 1)
        assert w is not None and w.state == "active"


# --- links ---------------------------------------------------------------------------------


async def test_a_link_goes_to_claude_as_kind_link(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    assert "kind link" in BRAIN_SYSTEM
    async with start(resonant_home, chat, tmp_path) as b:
        url = "https://example.com/posts/agents"
        # BR-10: the triage hands a link to Claude whatever the model says (kind link)
        ack, answer = await b.say(url, replies=2)
        assert ack in CANNED_ACKS and answer == ANSWER
        assert b.model.requests == [] and b.model.calls["triage"] == 1
        (cp,) = ask_rows(b)
        assert cp["kind"] == "link" and url in cp["question"]
        assert "Fetch the link with WebFetch" in notes(tmp_path)["prompt"]


async def test_with_the_local_route_the_model_sends_a_link_to_claude(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path, brain={"route": "local"}) as b:
        url = "https://example.com/posts/agents"
        b.model.push([("ask_claude", {"question": f"Summarise {url}", "kind": "link"})])
        assert await b.say(url, replies=2) == ["Looking into it…", ANSWER]
        (req,) = b.model.requests
        assert "kind link" in req.messages[0]["content"]
        (cp,) = ask_rows(b)
        assert cp["kind"] == "link" and url in cp["question"]


# --- watches -------------------------------------------------------------------------------


async def test_an_ask_watch_texts_once_when_claude_says_yes(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(
        resonant_home, chat, tmp_path, ask_args=("--answer", "YES. Back in stock."), **not_quiet()
    ) as b:
        b.model.push(
            [
                (
                    "create_watch",
                    {"what": "Pro in stock", "ask": "Is the Pro in stock at https://x.test?"},
                )
            ],
        )
        _answer, prompt = await b.say(
            "watch the Pro page and tell me when it's in stock", replies=2
        )  # PER-261: the answer goes out first
        assert "Reply yes" in prompt
        await b.prompt_recorded()
        (done,) = await b.say("yes")
        assert done.startswith("Watching W1: Pro in stock")
        n = len(b.osa.sent)
        assert queue_due(b.conn, utcnow()) == {"queued": 1, "expired": 0}
        b.daemon.loop._wakeup.set()  # pyright: ignore[reportPrivateUsage]
        await b.osa.wait_for(n + 1, 15)
        assert [m.text for m in b.osa.sent[n:]] == [
            "\U0001f440 W1 Pro in stock: Claude: Back in stock"
        ]
        await wait_until(
            lambda: (w := watch_store.get(b.conn, 1)) is not None and w.state == "fired", 10
        )
        (cp,) = ask_rows(b)
        assert cp["watch"] == 1 and cp["kind"] == "research"
        assert [a["decision"] for a in b.audits("brain.notice")] == ["sent"]


async def test_a_spoken_slash_command_is_only_chat(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    whisper = fake_whisper(tmp_path, "/restart")
    async with start(resonant_home, chat, tmp_path, brain={"whisper_path": str(whisper)}) as b:
        b.model.push("Type /restart if you want that.")
        voice = attach(b, "Audio Message.caf", b"caff")
        (reply,) = await send(b, "￼", [FakeAttachment(str(voice), "audio/x-caf")], 1)
        assert reply == "Type /restart if you want that."
        assert b.rows("SELECT * FROM tasks WHERE kind = 'command'") == []
        assert [a for a in b.audits("gate.evaluate") if a["tool"] == "restart_resonant"] == []

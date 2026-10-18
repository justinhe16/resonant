"""BR-7 attachments (docs/brain-plan.md D10), below the end-to-end tests
(``test_e2e_media.py``): the chat.db attachment reader, the conversions (sips, afconvert and
whisper are fakes or absent), the config, and the ask runner's HEIC and watch handling. The
chat.db is a FakeChatDB and the attachment files live under ``tmp_path``."""

from __future__ import annotations

import json
import sqlite3
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_ask_claude import Ctx, make

from resonant.config import BrainConfig, IMessageConfig, load_settings
from resonant.gateway.imessage import IMessageReader
from resonant.gateway.imessage.attachments import (
    MAX_ATTACHMENTS,
    MAX_BYTES,
    classify,
    from_payload,
)
from resonant.gateway.imessage.fixtures import FakeAttachment, FakeChatDB
from resonant.loop.types import Done
from resonant.principals import Principals
from resonant.runners import convert
from resonant.runners.ask import WATCH_HINT, Queued
from resonant_sdk import Event

OWNER = "+15551234567"
MEMBER = "+15557654321"


def principals() -> Principals:
    return Principals.model_validate(
        {
            "principals": {
                "justin": {"role": "owner", "identities": [f"imessage:{OWNER}"]},
                "cofounder": {"identities": [f"imessage:{MEMBER}"]},
            }
        }
    )


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[FakeChatDB]:
    c = FakeChatDB(tmp_path / "chat.db")
    c.add_message(handle=OWNER, text="history")
    yield c
    c.close()


def read(
    chat: FakeChatDB, db: sqlite3.Connection, home: Path | None = None, **add: Any
) -> list[Event]:
    reader = IMessageReader(IMessageConfig(db_path=chat.path), principals(), db, home=home)
    events: list[Event] = []

    def submit(e: Event) -> bool:
        events.append(e)
        return True

    reader.poll_once(submit)  # the first read sets the cursor
    chat.add_message(**add)
    reader.poll_once(submit)
    reader.close()
    return events


def files(tmp_path: Path, *names: str) -> list[Path]:
    d = tmp_path / "Attachments" / "ab" / "01"
    d.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for n in names:
        (d / n).write_bytes(b"x" * 10)
        out.append(d / n)
    return out


# --- the reader ----------------------------------------------------------------------------


def test_owner_screenshot_with_text(
    chat: FakeChatDB, db: sqlite3.Connection, tmp_path: Path
) -> None:
    (shot,) = files(tmp_path, "IMG_0001.PNG")
    (event,) = read(
        chat,
        db,
        handle=OWNER,
        text="what's this error?￼",
        attachments=[FakeAttachment(str(shot), "image/png", name="IMG_0001.PNG")],
    )
    assert event.payload["text"] == "what's this error?"
    assert event.payload["attachments"] == [
        {"path": str(shot), "kind": "image", "name": "IMG_0001.PNG", "bytes": 1024}
    ]
    assert event.payload["attachments_skipped"] == 0


def test_attachment_only_message_from_the_owner_is_an_event(
    chat: FakeChatDB, db: sqlite3.Connection, tmp_path: Path
) -> None:
    (voice,) = files(tmp_path, "Audio Message.caf")
    (event,) = read(
        chat, db, handle=OWNER, text="￼", attachments=[FakeAttachment(str(voice), "audio/x-caf")]
    )
    assert event.payload["text"] == "" and from_payload(event.payload)[0].kind == "voice"


def test_members_attachments_are_ignored(
    chat: FakeChatDB, db: sqlite3.Connection, tmp_path: Path
) -> None:
    (shot,) = files(tmp_path, "a.png")
    assert (
        read(
            chat, db, handle=MEMBER, text="￼", attachments=[FakeAttachment(str(shot), "image/png")]
        )
        == []
    )
    (event,) = read(
        chat, db, handle=MEMBER, text="look", attachments=[FakeAttachment(str(shot), "image/png")]
    )
    assert "attachments" not in event.payload


def test_unusable_big_and_extra_attachments_are_skipped(
    chat: FakeChatDB, db: sqlite3.Connection, tmp_path: Path
) -> None:
    paths = files(
        tmp_path, *[f"p{i}.jpg" for i in range(6)], "card.vcf", "x.pluginPayloadAttachment"
    )
    atts = [FakeAttachment(str(p), None) for p in paths[:5]]  # by suffix: no mime type
    atts.append(FakeAttachment(str(paths[5]), "image/jpeg", total_bytes=MAX_BYTES + 1))
    atts += [FakeAttachment(str(paths[6]), "text/vcard"), FakeAttachment(str(paths[7]))]
    (event,) = read(chat, db, handle=OWNER, text="pics", attachments=atts)
    got = from_payload(event.payload)
    assert len(got) == MAX_ATTACHMENTS and all(a.kind == "image" for a in got)
    assert event.payload["attachments_skipped"] == 4


def test_tilde_paths_expand_against_home(
    chat: FakeChatDB, db: sqlite3.Connection, tmp_path: Path
) -> None:
    (event,) = read(
        chat,
        db,
        home=tmp_path / "me",
        handle=OWNER,
        text="see",
        attachments=[FakeAttachment("~/Library/Messages/Attachments/x/IMG.HEIC", "image/heic")],
    )
    (a,) = from_payload(event.payload)
    assert a.path == str(tmp_path / "me/Library/Messages/Attachments/x/IMG.HEIC")


def test_chat_db_without_attachment_tables(db: sqlite3.Connection, tmp_path: Path) -> None:
    old = FakeChatDB(
        tmp_path / "old.db",
        omit={
            "attachment": {
                "ROWID",
                "guid",
                "created_date",
                "filename",
                "uti",
                "mime_type",
                "transfer_state",
                "is_outgoing",
                "transfer_name",
                "total_bytes",
            },
            "message_attachment_join": {"message_id", "attachment_id"},
        },
    )
    try:
        (event,) = read(old, db, handle=OWNER, text="hi")
        assert event.payload["text"] == "hi" and "attachments" not in event.payload
        assert read(old, db, handle=OWNER, text="￼") == []
    finally:
        old.close()


@pytest.mark.parametrize(
    ("name", "mime", "kind"),
    [
        ("IMG_1.HEIC", None, "image"),
        ("x.bin", "image/webp", "image"),
        ("doc.pdf", None, "pdf"),
        ("Audio Message.caf", None, "voice"),
        ("memo.m4a", "audio/x-m4a", "voice"),
        ("clip.mov", "video/quicktime", None),
        ("x.pluginPayloadAttachment", None, None),
    ],
)
def test_classify(name: str, mime: str | None, kind: str | None) -> None:
    assert classify(name, mime) == kind


def test_from_payload_drops_bad_entries() -> None:
    payload = {
        "attachments": [
            {"path": "rel.png", "kind": "image"},
            {"path": "/a.png", "kind": "exe"},
            "x",
            {"path": "/b.pdf", "kind": "pdf", "name": 3},
        ]
    }
    (a,) = from_payload(payload)
    assert (a.path, a.kind, a.name) == ("/b.pdf", "pdf", "b.pdf")


# --- conversions ---------------------------------------------------------------------------


def script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\nimport sys, shutil\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_heic_is_converted_with_sips(tmp_path: Path) -> None:
    sips = script(tmp_path / "sips", "a = sys.argv; shutil.copy(a[a.index('--out') - 1], a[-1])")
    src = tmp_path / "attachment-1.heic"
    src.write_bytes(b"heic")
    out = convert.heic_to_jpeg(src, str(sips))
    assert out == tmp_path / "attachment-1.jpg" and out.read_bytes() == b"heic"
    assert not src.exists()


def test_heic_stays_when_sips_fails_or_is_missing(tmp_path: Path) -> None:
    bad = script(tmp_path / "sips", "sys.exit(3)")
    src = tmp_path / "a.heic"
    src.write_bytes(b"heic")
    assert convert.heic_to_jpeg(src, str(bad)) == src and src.exists()
    assert convert.heic_to_jpeg(src, str(tmp_path / "nope")) == src
    png = tmp_path / "b.png"
    png.write_bytes(b"png")
    assert convert.heic_to_jpeg(png, str(bad)) == png  # only HEIC/HEIF are converted


def test_whisper_transcribes_and_cleans(tmp_path: Path) -> None:
    whisper = script(
        tmp_path / "whisper-cli",
        "print(' [BLANK_AUDIO]\\n Remind me to\\x07 call mom.  (music)'); "
        "open(sys.argv[-1] + '.args', 'w').write(' '.join(sys.argv[1:]))",
    )
    cfg = BrainConfig(whisper_path=whisper, whisper_model=tmp_path / "ggml.bin")
    audio = tmp_path / "voice.caf"
    audio.write_bytes(b"caf")
    text = convert.transcribe(cfg, audio, tmp_path, afconvert=str(tmp_path / "no-afconvert"))
    assert text == "Remind me to call mom."
    args = (tmp_path / "voice.caf.args").read_text()
    assert args == f"-m {tmp_path / 'ggml.bin'} -nt -np -f {audio}"


def test_whisper_failure_raises(tmp_path: Path) -> None:
    cfg = BrainConfig(whisper_path=script(tmp_path / "w", "sys.exit(2)"))
    audio = tmp_path / "v.m4a"
    audio.write_bytes(b"m4a")
    with pytest.raises(convert.ConvertError, match="exited 2"):
        convert.transcribe(cfg, audio, tmp_path, afconvert=str(tmp_path / "none"))


def test_whisper_config_is_checked() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        BrainConfig.model_validate({"whisper_path": "whisper-cli"})
    with pytest.raises(ValidationError, match="input"):
        BrainConfig.model_validate({"whisper_args": ["-f", "x.wav"]})
    s = load_settings()
    assert s.brain.whisper_path is None and s.brain.proactive_per_day == 3
    assert (s.brain.watches.max_active, s.brain.watches.min_interval_minutes) == (10, 15)


# --- the ask runner: HEIC and watch asks ---------------------------------------------------


async def test_heic_attachment_reaches_claude_as_jpeg(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    h = make(db, tmp_path)
    h.runner.sips = str(
        script(tmp_path / "sips", "a = sys.argv; shutil.copy(a[a.index('--out') - 1], a[-1])")
    )
    photo = h.roots[0] / "IMG_7.HEIC"
    photo.write_bytes(b"heic")
    out, _ = await h.ask("what's this?", kind="image", attachments=[str(photo)])
    assert out.result["status"] == "answered"
    notes = h.argv_notes()
    assert notes["files"] == ["attachment-1.jpg"]
    assert "Attached (in the current folder): attachment-1.jpg" in notes["prompt"]


async def test_a_watch_ask_goes_to_the_hook_not_the_owner(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    h = make(db, tmp_path, extra=("--answer", "YES. It's in stock."))
    seen: list[tuple[int, str, str, str]] = []

    async def hook(watch: int, task: str, status: str, reply: str) -> None:
        seen.append((watch, task, status, reply))

    h.runner.on_watch = hook
    queued = h.runner.enqueue("Is it in stock?", kind="research", principal="justin", watch=3)
    assert isinstance(queued, Queued)
    out = await h.runner.step(h.task(queued.task_id), [], Ctx())
    assert isinstance(out, Done)
    assert out.result == {
        "status": "answered",
        "kind": "research",
        "watch": 3,
        "untrusted": True,
        "proposed": False,
    }
    assert seen == [(3, queued.task_id, "answered", "Claude: YES. It's in stock.")]
    assert h.channel.sent == []
    assert h.argv_notes()["prompt"].startswith(WATCH_HINT)
    cp = json.loads(
        db.execute("SELECT checkpoint FROM tasks WHERE id = ?", (queued.task_id,)).fetchone()[0]
    )
    assert cp["watch"] == 3


async def test_a_plain_ask_has_no_watch_hint(db: sqlite3.Connection, tmp_path: Path) -> None:
    h = make(db, tmp_path)
    await h.ask("hi?")
    assert WATCH_HINT not in h.argv_notes()["prompt"]

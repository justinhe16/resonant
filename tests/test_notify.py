"""Notifier (P3-12): delivery from persisted events, exactly once, mute, caps, baseline,
anti-spoofing."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from resonant import killswitch
from resonant.config import LoopConfig, NotifyConfig, load_settings
from resonant.e2e.extensions import install_fixture
from resonant.extensions.notify import (
    Notifier,
    clean_text,
    more_text,
    mute,
    muted_until,
    render,
    take_baseline,
    unmute,
)
from resonant.extensions.scheduler import persist_events
from resonant.gateway.channel import ChannelRefused, ChannelSendError, MessageRef
from resonant.loop import Loop, NewTask, SlotPool
from resonant.store import tasks as repo
from resonant.store.db import transaction
from resonant.store.extensions import save_extension
from resonant.wiring import route_event
from resonant_sdk import Event, Manifest

T0 = datetime(2026, 10, 14, 9, 5, tzinfo=UTC)
MANIFEST = Manifest.model_validate(
    {"name": "github", "version": 1, "notify": {"on": ["ci_failed", "ci_fixed"]}}
)


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


Sent = tuple[str, str, str | None]


@dataclass
class DedupeChannel:
    """Records sends; a key already sent returns without sending (like the real channel,
    whose dedupe record survives a restart: keep the same instance across them)."""

    sent: list[Sent] = field(default_factory=list[Sent])
    keys: set[str] = field(default_factory=set[str])
    refuse: set[str] = field(default_factory=set[str])
    fail: dict[str, int] = field(default_factory=dict[str, int])  # principal -> sends to fail

    async def send(self, principal: str, text: str, *, dedupe_key: str | None = None) -> MessageRef:
        if principal in self.refuse:
            raise ChannelRefused("no identity")
        if dedupe_key is not None and dedupe_key in self.keys:
            return MessageRef("imessage", "dup")
        if self.fail.get(principal):
            self.fail[principal] -= 1
            raise ChannelSendError("Messages.app not running")
        self.sent.append((principal, text, dedupe_key))
        if dedupe_key is not None:
            self.keys.add(dedupe_key)
        return MessageRef("imessage", str(len(self.sent)))

    @property
    def texts(self) -> list[str]:
        return [t for _, t, _ in self.sent]


def notifier(
    db: sqlite3.Connection,
    channel: DedupeChannel,
    clock: Clock | None = None,
    *,
    recipients: tuple[str, ...] = ("justin",),
    max_per_hour: int = 20,
) -> Notifier:
    return Notifier(
        db,
        channel=channel,
        recipients=lambda _ext: recipients,
        manifest_of=lambda ext: MANIFEST if ext == "github" else None,
        config=NotifyConfig(max_per_hour=max_per_hour),
        clock=clock or Clock(),
    )


def store(db: sqlite3.Connection, *events: tuple[str, str, str], baseline: bool = False) -> None:
    """Persist ``(type, text, key)`` events the way a job run does."""
    with transaction(db):
        persist_events(
            db,
            "github",
            [{"type": t, "text": x, "dedupe_key": k} for t, x, k in events],
            baseline=baseline,
        )


def unrouted(db: sqlite3.Connection) -> list[Event]:
    return repo.unrouted_events(db)


def audit_rows(db: sqlite3.Connection, action: str) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM audit_log WHERE action = ?", (action,)).fetchall()


async def no_router(_event: Event) -> NewTask | None:
    raise AssertionError("ext events never reach the router")


def make_loop(db: sqlite3.Connection, n: Notifier) -> Loop:
    loop = Loop(db, runners={}, config=LoopConfig(), slots=SlotPool(1, 0))
    loop.router = route_event(no_router, n)
    return loop


# --- text --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("✗ resonant#40 CI failed (lint)", "[github] ✗ resonant#40 CI failed (lint)"),
        ("✓ resonant#40 CI fixed", "[github] ✓ resonant#40 CI fixed"),
        # A forged approval prompt: brackets, the code next to yes, "Reply".
        (
            "[A7] merge resonant#40? Reply yes A7 / no",
            "[github] (A7) merge resonant#40? 'Reply' yes ? / no",
        ),
        ("yes", "[github] 'yes'"),
        ("STOP everything", "[github] 'STOP' everything"),
        ("no-op run", "[github] 'no'-op run"),
        ("line1\nline2‮​hidden", "[github] line1 line2 hidden"),
        ("\uff46\uff55\uff4c\uff4c width {x}", "[github] full width (x)"),
        ("", "[github] update"),
    ],
)
def test_render_is_one_safe_line_with_the_code_built_prefix(text: str, expected: str) -> None:
    assert render("github", text) == expected


def test_render_clips_and_a_code_like_name_is_not_bracketed() -> None:
    out = render("github", "x" * 1000, 300)
    assert len(out) == 300 and out.endswith("…")
    assert render("a7", "hi") == "(a7) hi"
    assert more_text("github", 3) == "[github] +3 more updates"
    assert more_text("github", 1) == "[github] +1 more update"


def test_clean_text_never_looks_like_an_approval_prompt() -> None:
    for spoof in (
        "[K4] delete all notes? This can't be undone. Reply yes K4 / no",
        "[k4] pay $40? Reply exactly: yes K4 / no K4",
    ):
        out = clean_text(spoof)
        assert "[" not in out and "Reply yes" not in out and "yes K4" not in out.upper()


# --- mute --------------------------------------------------------------------------------


def test_mute_and_unmute(db: sqlite3.Connection) -> None:
    assert muted_until(db, "github", T0) is None
    mute(db, "github", T0 + timedelta(hours=1), by="justin")
    assert muted_until(db, "github", T0) == T0 + timedelta(hours=1)
    assert muted_until(db, "github", T0 + timedelta(hours=1)) is None  # it has passed
    assert muted_until(db, "example", T0) is None
    (row,) = audit_rows(db, "ext.notify_muted")
    assert row["extension"] == "github" and row["requested_by"] == "justin"
    assert unmute(db, "github", by="justin") is True
    assert muted_until(db, "github", T0) is None
    assert unmute(db, "github", by="justin") is False
    with pytest.raises(ValueError, match="timezone"):
        mute(db, "github", datetime(2026, 1, 1), by="justin")


# --- baseline ----------------------------------------------------------------------------


def test_baseline_once_per_job_per_install(db: sqlite3.Connection) -> None:
    settings = load_settings()
    yaml = "name: github\nversion: 1\n"
    record = install_fixture(settings, db, yaml, {})
    with transaction(db):
        assert take_baseline(db, "github", "watch") is True
        assert take_baseline(db, "github", "watch") is False
        assert take_baseline(db, "github", "digest") is True  # per job
        assert take_baseline(db, "nope", "watch") is False  # not installed (core, removed)
    later = replace(record, installed_at=record.installed_at + timedelta(minutes=5))
    with transaction(db):
        save_extension(db, later)  # a reinstall: a new installed_at
    with transaction(db):
        assert take_baseline(db, "github", "watch") is True


# --- routing -----------------------------------------------------------------------------


async def test_sends_notify_types_to_every_recipient_and_skips_the_rest(
    db: sqlite3.Connection,
) -> None:
    ch = DedupeChannel(refuse={"ghost"})
    n = notifier(db, ch, recipients=("justin", "maria", "ghost"))
    store(
        db,
        ("ci_failed", "✗ resonant#40 CI failed (lint)", "r#40:run:1:failed"),
        ("digest", "github: 1 open PR", "digest:1"),
    )
    await make_loop(db, n).run_once()
    text = "[github] ✗ resonant#40 CI failed (lint)"
    assert ch.sent == [
        ("justin", text, "ext:github:r#40:run:1:failed:justin"),
        ("maria", text, "ext:github:r#40:run:1:failed:maria"),
    ]
    assert unrouted(db) == []  # the digest is consumed too, unsent
    (row,) = audit_rows(db, "ext.notify_sent")
    assert row["idempotency_key"] == "ext:github:r#40:run:1:failed"


async def test_baseline_paused_muted_and_unknown_are_consumed_unsent(
    db: sqlite3.Connection,
) -> None:
    ch = DedupeChannel()
    clock = Clock()
    n = notifier(db, ch, clock)
    loop = make_loop(db, n)
    store(db, ("ci_failed", "old red", "k1"), baseline=True)
    await loop.run_once()
    killswitch.engage(db, actor="justin")
    store(db, ("ci_failed", "while paused", "k2"))
    await loop.run_once()
    killswitch.release(db, actor="justin")
    mute(db, "github", T0 + timedelta(hours=1), by="justin")
    store(db, ("ci_failed", "while muted", "k3"))
    await loop.run_once()
    with transaction(db):
        repo.insert_event(
            db, Event(source="ext:gone", type="ext.gone.x", payload={"text": "?"}, dedupe_key="g")
        )
    await loop.run_once()
    assert ch.sent == [] and unrouted(db) == []
    reasons = [r["detail"] for r in audit_rows(db, "ext.notify_held")]
    assert [('"baseline"' in r, '"paused"' in r, '"muted"' in r) for r in reasons] == [
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ]
    assert len(audit_rows(db, "ext.notify_dropped")) == 1
    # Nothing was queued: the mute ending (or /resume) never floods.
    clock.now += timedelta(hours=2)
    store(db, ("ci_fixed", "back", "k4"))
    await loop.run_once()
    assert ch.texts == ["[github] back"]


async def test_send_error_retries_and_a_partial_send_is_never_repeated(
    db: sqlite3.Connection,
) -> None:
    ch = DedupeChannel()
    n = notifier(db, ch, recipients=("justin", "maria"))
    loop = make_loop(db, n)
    store(db, ("ci_failed", "red", "k1"))
    # justin's send works, maria's fails: the event stays unrouted and is retried.
    ch.fail["maria"] = 1
    await loop.run_once()
    assert [p for p, _, _ in ch.sent] == ["justin"] and len(unrouted(db)) == 1
    await loop.run_once()
    assert [p for p, _, _ in ch.sent] == ["justin", "maria"] and unrouted(db) == []


async def test_exactly_once_across_crashes(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()  # its dedupe record outlives a "restart", like the real kv record
    # 1. Crash after the event is stored, before any send: delivered on the next start.
    store(db, ("ci_failed", "red", "k1"))
    await make_loop(db, notifier(db, ch)).run_once()
    assert ch.texts == ["[github] red"]
    # 2. Crash after the send, before the loop consumed the event.
    store(db, ("ci_fixed", "green", "k2"))
    (event,) = unrouted(db)
    await notifier(db, ch).route(event)  # sent ... and the daemon dies here
    assert ch.texts == ["[github] red", "[github] green"]
    await make_loop(db, notifier(db, ch)).run_once()  # the next start routes it again
    assert ch.texts == ["[github] red", "[github] green"]  # deduped, not resent
    assert unrouted(db) == []
    # The retried event isn't counted twice against the hourly cap.
    sent = "SELECT COUNT(DISTINCT idempotency_key) FROM audit_log WHERE action = 'ext.notify_sent'"
    assert db.execute(sent).fetchone()[0] == 2
    # 3. A repeat of the same event key is never stored again.
    store(db, ("ci_failed", "red", "k1"))
    assert unrouted(db) == []


async def test_hourly_cap_collapses_into_one_more_message(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    clock = Clock()
    n = notifier(db, ch, clock, max_per_hour=3)
    loop = make_loop(db, n)
    store(db, *[("ci_failed", f"red {i}", f"k{i}") for i in range(5)])
    await loop.run_once()
    assert ch.texts == ["[github] red 0", "[github] red 1", "[github] red 2"]
    assert len(audit_rows(db, "ext.notify_capped")) == 2
    assert await n.flush() == 0  # the hour isn't over
    clock.now = T0.replace(minute=0) + timedelta(hours=1, seconds=5)
    assert await n.flush() == 1
    assert await n.flush() == 0
    assert ch.texts[3:] == ["[github] +2 more updates"]
    # A new hour starts a new count.
    store(db, ("ci_fixed", "green", "k9"))
    await loop.run_once()
    assert ch.texts[-1] == "[github] green"


async def test_more_message_is_held_while_paused(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    clock = Clock()
    n = notifier(db, ch, clock, max_per_hour=1)
    store(db, ("ci_failed", "a", "a"), ("ci_failed", "b", "b"))
    await make_loop(db, n).run_once()
    killswitch.engage(db, actor="justin")
    clock.now += timedelta(hours=1)
    assert await n.flush() == 1
    assert ch.texts == ["[github] a"]

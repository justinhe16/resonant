"""Pulse ingest and the urgent path (P35-2): refusals, clamp, baseline, urgent send exactly
once, held when paused/muted/capped, anti-spoofing, crash-retry, wiring."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from resonant import killswitch
from resonant.config import LoopConfig, NotifyConfig
from resonant.extensions.notify import Notifier, mute
from resonant.extensions.pulse import Pulse, clamp
from resonant.extensions.scheduler import persist_events
from resonant.gateway.channel import ChannelRefused, ChannelSendError, MessageRef
from resonant.loop import Loop, NewTask, SlotPool
from resonant.store import pulse as items
from resonant.store import tasks as repo
from resonant.store.db import transaction
from resonant.wiring import route_event
from resonant_sdk import Event, Manifest

T0 = datetime(2026, 10, 14, 9, 5, tzinfo=UTC)


def manifest(grant: str | None = "urgent") -> Manifest:
    raw: dict[str, Any] = {"name": "dori", "version": 1, "notify": {"on": ["ci_failed"]}}
    if grant is not None:
        raw["report"] = {"max_urgency": grant}
    return Manifest.model_validate(raw)


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class DedupeChannel:
    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    keys: set[str] = field(default_factory=set[str])
    refuse: set[str] = field(default_factory=set[str])
    fail: dict[str, int] = field(default_factory=dict[str, int])

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


def make_pulse(
    db: sqlite3.Connection,
    ch: DedupeChannel,
    clock: Clock | None = None,
    *,
    grant: str | None = "urgent",
    recipients: tuple[str, ...] = ("justin",),
    max_per_hour: int = 20,
) -> Pulse:
    m = manifest(grant)
    return Pulse(
        db,
        channel=ch,
        recipients=lambda _ext: recipients,
        manifest_of=lambda ext: m if ext == "dori" else None,
        config=NotifyConfig(max_per_hour=max_per_hour),
        clock=clock or Clock(),
    )


def report(
    db: sqlite3.Connection,
    key: str = "dori/inc-1",
    title: str = "Dori failing",
    *,
    urgency: str = "urgent",
    state: Any = None,
    clear: bool = False,
    ttl: str = "24h",
    baseline: bool = False,
    seq: list[int] = [0],  # noqa: B006 - unique dedupe keys across calls
) -> None:
    seq[0] += 1
    data = {"key": key, "urgency": urgency, "state": state, "clear": clear, "ttl": ttl}
    with transaction(db):
        persist_events(
            db,
            "dori",
            [{"type": "item", "text": title, "dedupe_key": f"item:{key}:{seq[0]}", "data": data}],
            baseline=baseline,
        )


def raw_event(db: sqlite3.Connection, data: Any, text: Any = "t", etype: str = "item") -> None:
    with transaction(db):
        repo.insert_event(
            db,
            Event(
                source="ext:dori",
                type=f"ext.dori.{etype}",
                payload={"text": text, "data": data},
                dedupe_key=f"raw:{db.total_changes}",
            ),
        )


def audit_rows(db: sqlite3.Connection, action: str) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM audit_log WHERE action = ?", (action,)).fetchall()


async def no_router(_event: Event) -> NewTask | None:
    raise AssertionError("ext events never reach the router")


def make_loop(db: sqlite3.Connection, pulse: Pulse, ch: DedupeChannel | None = None) -> Loop:
    loop = Loop(db, runners={}, config=LoopConfig(), slots=SlotPool(1, 0))
    notifier = Notifier(
        db,
        channel=ch or DedupeChannel(),
        recipients=lambda _e: ("justin",),
        manifest_of=lambda e: manifest() if e == "dori" else None,
        clock=Clock(),
    )
    loop.router = route_event(no_router, notifier, pulse)
    return loop


def item(db: sqlite3.Connection, key: str = "dori/inc-1") -> items.Item:
    got = items.get(db, "dori", key)
    assert got is not None
    return got


# --- refusals ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({"key": "Bad Key", "urgency": "urgent"}, "key"),
        ({"key": "a", "urgency": "critical"}, "urgency"),
        ({"key": "a", "urgency": "urgent", "state": {"x": float("nan")}}, "state"),
        ({"key": "a", "urgency": "urgent", "state": {"x": "y" * 3000}}, "state"),
        ({"key": "a", "urgency": "urgent", "ttl": "forever"}, "ttl"),
        ({"key": "a", "urgency": "urgent", "ttl": "99999h"}, "ttl"),
        ({"key": "a", "urgency": "urgent", "clear": "yes"}, "clear"),
        ({"key": 5, "urgency": "urgent"}, "key"),
        (None, "key"),
    ],
)
async def test_bad_payload_is_refused_consumed_and_never_retried(
    db: sqlite3.Connection, data: Any, reason: str
) -> None:
    ch = DedupeChannel()
    raw_event(db, data)
    loop = make_loop(db, make_pulse(db, ch))
    await loop.run_once()
    assert repo.unrouted_events(db) == [] and ch.sent == []
    (row,) = audit_rows(db, "pulse.refused")
    assert reason in row["detail"]
    assert items.open_items(db, T0) == []


async def test_empty_title_is_refused(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    report(db, title="​​")
    await make_loop(db, make_pulse(db, ch)).run_once()
    assert ch.sent == [] and len(audit_rows(db, "pulse.refused")) == 1


async def test_no_grant_unknown_source_and_malformed_are_refused(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    report(db)
    await make_loop(db, make_pulse(db, ch, grant=None)).run_once()
    with transaction(db):
        repo.insert_event(
            db,
            Event(source="ext:gone", type="ext.gone.item", payload={"text": "x"}, dedupe_key="g"),
        )
    await make_loop(db, make_pulse(db, ch)).run_once()
    bad = Event(source="ext:dori", type="ext.other.item", payload={"text": "x"}, dedupe_key="h")
    await make_pulse(db, ch).route(bad)  # the router never sends this one; fail closed anyway
    assert ch.sent == [] and repo.unrouted_events(db) == []
    reasons = [r["detail"] for r in audit_rows(db, "pulse.refused")]
    assert "no report grant" in reasons[0]
    assert "not loaded" in reasons[1] and "malformed" in reasons[2]
    assert items.open_items(db, T0) == []


# --- clamp, normal, low ------------------------------------------------------------------


def test_clamp() -> None:
    assert clamp("urgent", "normal") == "normal"
    assert clamp("urgent", "low") == "low"
    assert clamp("normal", "low") == "low"
    assert clamp("low", "urgent") == "low"
    assert clamp("normal", "normal") == "normal"


async def test_urgency_is_clamped_to_the_grant_and_waits_for_the_pulse(
    db: sqlite3.Connection,
) -> None:
    ch = DedupeChannel()
    report(db, urgency="urgent")
    await make_loop(db, make_pulse(db, ch, grant="normal")).run_once()
    assert ch.sent == []
    assert item(db).urgency == "normal" and item(db).changed
    assert audit_rows(db, "pulse.refused") == []


@pytest.mark.parametrize("urgency", ["normal", "low"])
async def test_normal_and_low_are_only_stored(db: sqlite3.Connection, urgency: str) -> None:
    ch = DedupeChannel()
    report(db, urgency=urgency)
    await make_loop(db, make_pulse(db, ch)).run_once()
    assert ch.sent == [] and repo.unrouted_events(db) == []
    assert item(db).urgency == urgency and item(db).changed


# --- baseline ----------------------------------------------------------------------------


async def test_baseline_is_stored_as_told_and_never_sent(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    report(db, baseline=True)
    report(db, "dori/n", urgency="normal", baseline=True)
    pulse = make_pulse(db, ch)
    await make_loop(db, pulse).run_once()
    assert ch.sent == []
    assert not item(db).changed and not item(db, "dori/n").changed
    assert all("baseline" in r["detail"] for r in audit_rows(db, "pulse.held"))
    # The same state again later is stale; a real change is news.
    report(db)
    await make_loop(db, pulse).run_once()
    assert ch.sent == []
    report(db, title="Dori down")
    await make_loop(db, pulse).run_once()
    assert ch.texts == ["[dori] \U0001f534 Dori down"]


# --- the urgent path ---------------------------------------------------------------------


async def test_urgent_sends_once_to_every_recipient_then_marks_told(
    db: sqlite3.Connection,
) -> None:
    ch = DedupeChannel(refuse={"ghost"})
    pulse = make_pulse(db, ch, recipients=("justin", "maria", "ghost"))
    report(db, state={"status": "triaging"})
    (event,) = repo.unrouted_events(db)
    await make_loop(db, pulse).run_once()
    text = "[dori] \U0001f534 Dori failing"
    assert ch.sent == [
        ("justin", text, f"pulse:urgent:{event.id}:justin"),
        ("maria", text, f"pulse:urgent:{event.id}:maria"),
    ]
    assert not item(db).changed
    (row,) = audit_rows(db, "pulse.sent")
    assert row["extension"] == "dori" and row["idempotency_key"] == event.dedupe_key
    # Same state reported again: stale, nothing to say. Changed state: told again.
    report(db, title="Dori failing (still)", state={"status": "triaging"})
    await make_loop(db, pulse).run_once()
    assert len(ch.sent) == 2
    report(db, state={"status": "fixing"})
    await make_loop(db, pulse).run_once()
    assert len(ch.sent) == 4


async def test_clear_sends_a_check_mark(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    pulse = make_pulse(db, ch)
    report(db, "dori/inc-1", "Dori failing")
    report(db, "dori/inc-1", "Dori recovered", clear=True)
    await make_loop(db, pulse).run_once()
    assert ch.texts[-1] == "[dori] ✓ Dori recovered"
    assert item(db).cleared and not item(db).changed


async def test_title_is_sanitized_and_cannot_spoof_an_approval(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    report(db, title="[A7] merge it? Reply yes A7 / no\x1b[31m‮")
    await make_loop(db, make_pulse(db, ch)).run_once()
    (text,) = ch.texts
    assert text == "[dori] \U0001f534 (A7) merge it? 'Reply' yes ? / no (31m"
    assert "[A7]" not in text and "yes A7" not in text and "\x1b" not in text
    stored = item(db).title
    assert "[" not in stored and "‮" not in stored and "yes A7" not in stored


async def test_code_like_extension_name_gets_parentheses(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    m = Manifest.model_validate({"name": "a7", "version": 1, "report": {"max_urgency": "urgent"}})
    pulse = Pulse(
        db,
        channel=ch,
        recipients=lambda _e: ("justin",),
        manifest_of=lambda e: m if e == "a7" else None,
        clock=Clock(),
    )
    with transaction(db):
        persist_events(
            db,
            "a7",
            [
                {
                    "type": "item",
                    "text": "down",
                    "dedupe_key": "k",
                    "data": {"key": "x", "urgency": "urgent"},
                }
            ],
        )
    (event,) = repo.unrouted_events(db)
    await pulse.route(event)
    assert ch.texts == ["(a7) \U0001f534 down"]


# --- held --------------------------------------------------------------------------------


async def test_held_when_paused_muted_or_capped_stays_pending(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    clock = Clock()
    pulse = make_pulse(db, ch, clock, max_per_hour=1)
    loop = make_loop(db, pulse)
    killswitch.engage(db, actor="justin")
    report(db, "dori/a", "while paused")
    await loop.run_once()
    killswitch.release(db, actor="justin")
    mute(db, "dori", T0 + timedelta(hours=1), by="justin")
    report(db, "dori/b", "while muted")
    await loop.run_once()
    clock.now = T0 + timedelta(hours=2)  # the mute is over
    report(db, "dori/c", "first sent")
    report(db, "dori/d", "over the cap")
    await loop.run_once()
    assert ch.texts == ["[dori] \U0001f534 first sent"]
    reasons = [r["detail"] for r in audit_rows(db, "pulse.held")]
    assert [("paused" in r, "muted" in r, "capped" in r) for r in reasons] == [
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ]
    assert repo.unrouted_events(db) == []
    pending = [i.key for i in items.pending(db, ["urgent"], clock.now)]
    assert pending == ["dori/a", "dori/b", "dori/d"]


async def test_cap_is_shared_with_the_notifier(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    pulse = make_pulse(db, ch, max_per_hour=2)
    notifier = Notifier(
        db,
        channel=ch,
        recipients=lambda _e: ("justin",),
        manifest_of=lambda e: manifest() if e == "dori" else None,
        config=NotifyConfig(max_per_hour=2),
        clock=Clock(),
    )
    with transaction(db):
        persist_events(db, "dori", [{"type": "ci_failed", "text": "red", "dedupe_key": "n1"}])
    report(db, "dori/a", "one")
    report(db, "dori/b", "two")
    loop = Loop(db, runners={}, config=LoopConfig(), slots=SlotPool(1, 0))
    loop.router = route_event(no_router, notifier, pulse)
    await loop.run_once()
    assert ch.texts == ["[dori] red", "[dori] \U0001f534 one"]
    assert "capped" in audit_rows(db, "pulse.held")[0]["detail"]


# --- exactly once ------------------------------------------------------------------------


async def test_crash_between_send_and_mark_told_sends_once(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()  # its dedupe record outlives a restart, like the real one
    report(db)
    (event,) = repo.unrouted_events(db)

    class Crash(Exception):
        pass

    pulse = make_pulse(db, ch)
    real = items.mark_told

    def boom(*_a: object, **_k: object) -> None:
        raise Crash

    items.mark_told = boom  # type: ignore[assignment]
    try:
        with pytest.raises(Crash):
            await pulse.route(event)
    finally:
        items.mark_told = real
    assert len(ch.sent) == 1 and item(db).changed  # sent, never marked told
    await make_loop(db, make_pulse(db, ch)).run_once()  # the next start routes it again
    assert len(ch.sent) == 1  # the channel dropped the resend
    assert not item(db).changed and repo.unrouted_events(db) == []
    sent = "SELECT COUNT(DISTINCT idempotency_key) FROM audit_log WHERE action = 'pulse.sent'"
    assert db.execute(sent).fetchone()[0] == 1


async def test_send_error_retries_and_a_partial_send_is_never_repeated(
    db: sqlite3.Connection,
) -> None:
    ch = DedupeChannel()
    pulse = make_pulse(db, ch, recipients=("justin", "maria"))
    loop = make_loop(db, pulse)
    report(db)
    ch.fail["maria"] = 1
    await loop.run_once()
    assert [p for p, _, _ in ch.sent] == ["justin"] and len(repo.unrouted_events(db)) == 1
    assert item(db).changed
    await loop.run_once()
    assert [p for p, _, _ in ch.sent] == ["justin", "maria"]
    assert repo.unrouted_events(db) == [] and not item(db).changed


# --- wiring ------------------------------------------------------------------------------


async def test_notify_events_still_go_to_the_notifier(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    with transaction(db):
        persist_events(db, "dori", [{"type": "ci_failed", "text": "red", "dedupe_key": "n1"}])
    await make_loop(db, make_pulse(db, ch), ch).run_once()
    assert ch.texts == ["[dori] red"]
    assert audit_rows(db, "pulse.refused") == [] and items.open_items(db, T0) == []


async def test_without_a_pulse_item_events_are_consumed_by_the_notifier(
    db: sqlite3.Connection,
) -> None:
    ch = DedupeChannel()
    report(db)
    notifier = Notifier(
        db,
        channel=ch,
        recipients=lambda _e: ("justin",),
        manifest_of=lambda e: manifest() if e == "dori" else None,
        clock=Clock(),
    )
    loop = Loop(db, runners={}, config=LoopConfig(), slots=SlotPool(1, 0))
    loop.router = route_event(no_router, notifier)
    await loop.run_once()
    assert ch.sent == [] and repo.unrouted_events(db) == []


async def test_title_is_cleaned_once(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    report(db, title="Reply YES A7 to approve")
    await make_loop(db, make_pulse(db, ch)).run_once()
    assert ch.texts == ["[dori] \U0001f534 'Reply' YES ? to approve"]


async def test_all_recipients_refused_leaves_the_item_pending(db: sqlite3.Connection) -> None:
    ch = DedupeChannel(refuse={"justin"})
    report(db)
    await make_loop(db, make_pulse(db, ch)).run_once()
    assert ch.sent == [] and repo.unrouted_events(db) == []
    assert item(db).changed and len(audit_rows(db, "pulse.unreachable")) == 1
    assert audit_rows(db, "pulse.sent") == []
    assert [i.key for i in items.pending(db, ["urgent"], T0)] == ["dori/inc-1"]


async def test_out_of_order_retry_is_stale(db: sqlite3.Connection) -> None:
    ch = DedupeChannel()
    pulse = make_pulse(db, ch)
    report(db, title="state A", state={"s": "A"})
    await asyncio.sleep(0.01)  # occurred_at has millisecond resolution
    report(db, title="state B", state={"s": "B"})
    e1, e2 = sorted(repo.unrouted_events(db), key=lambda e: e.occurred_at)
    await pulse.route(e2)
    assert ch.texts == ["[dori] \U0001f534 state B"]
    await pulse.route(e1)  # E1's retry arrives after E2
    assert ch.texts == ["[dori] \U0001f534 state B"]
    assert item(db).title == "state B" and not item(db).changed
    assert len(audit_rows(db, "pulse.stale")) == 1


async def test_clear_of_an_urgent_item_is_urgent_even_when_rated_normal(
    db: sqlite3.Connection,
) -> None:
    ch = DedupeChannel()
    pulse = make_pulse(db, ch)
    report(db, title="Dori failing")
    report(db, title="Dori recovered", urgency="normal", clear=True)
    await make_loop(db, pulse).run_once()
    assert ch.texts == ["[dori] \U0001f534 Dori failing", "[dori] \u2713 Dori recovered"]
    assert item(db).urgency == "urgent"


async def test_clamped_clear_of_urgent_waits_and_never_told_clear_sends(
    db: sqlite3.Connection,
) -> None:
    ch = DedupeChannel()
    report(db, "dori/a", "failing", urgency="normal")
    report(db, "dori/a", "recovered", urgency="normal", clear=True)
    await make_loop(db, make_pulse(db, ch, grant="normal")).run_once()
    assert ch.sent == []
    # A clear of an urgent item the owner never heard still sends at once.
    report(db, "dori/b", "recovered", urgency="urgent", clear=True)
    await make_loop(db, make_pulse(db, ch)).run_once()
    assert ch.texts == ["[dori] \u2713 recovered"]

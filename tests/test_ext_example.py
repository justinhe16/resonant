"""The example extension's reporters (P35-4): ``pnl`` and ``incident`` run the way Resonant
runs them (``python -I -S``, events on fd 3), their reports are persisted like a job run's and
routed through the real ``Pulse``."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from resonant.extensions.checks import dir_errors, name_errors
from resonant.extensions.pulse import Pulse
from resonant.extensions.scheduler import persist_events
from resonant.gateway.channel import MessageRef
from resonant.store import pulse as items
from resonant.store import tasks as repo
from resonant.store.db import transaction, utcnow
from resonant_sdk import Effect, Level, load_manifest

REPO = Path(__file__).resolve().parents[1]
EXT = REPO / "extensions/example"
SDK_EMIT = REPO / "packages/resonant-sdk/src/resonant_sdk/emit.py"


class Channel:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.keys: set[str] = set()

    async def send(self, principal: str, text: str, *, dedupe_key: str | None = None) -> MessageRef:
        if dedupe_key not in self.keys:
            self.texts.append(text)
            if dedupe_key:
                self.keys.add(dedupe_key)
        return MessageRef("imessage", str(len(self.texts)))


def run_cli(data: Path, *args: str, due: str = "2026-10-14T10:55:00-07:00") -> list[dict[str, Any]]:
    """Run ``cli.py`` as Resonant does and return the events it wrote to fd 3."""
    r, w = os.pipe()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "RESONANT_EXT_DATA": str(data),
        "RESONANT_EVENTS_FD": str(w),
        "RESONANT_JOB_DUE_AT": due,
    }
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(EXT / "cli.py"), *args],
        env=env,
        pass_fds=(w,),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    os.close(w)
    with os.fdopen(r) as f:
        lines = f.read().splitlines()
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in lines]


def make_pulse(db: sqlite3.Connection, ch: Channel) -> Pulse:
    m = load_manifest(EXT / "resonant.yaml")
    return Pulse(
        db,
        channel=ch,
        recipients=lambda _e: ("justin",),
        manifest_of=lambda e: m if e == "example" else None,
        clock=utcnow,
    )


async def ingest(db: sqlite3.Connection, pulse: Pulse, events: list[dict[str, Any]]) -> None:
    with transaction(db):
        persist_events(db, "example", events)
    for event in repo.unrouted_events(db):
        assert await pulse.route(event) is None
        with transaction(db):
            repo.consume_events(db, [event.id])


def test_manifest_passes_every_check_and_grants_urgent_reports() -> None:
    m = load_manifest(EXT / "resonant.yaml")
    assert dir_errors(EXT, m) == [] and name_errors(m) == []
    assert m.report is not None and m.report.max_urgency == "urgent"
    jobs = {j.id: j for j in m.jobs}
    assert jobs["pnl"].schedule == "55 * * * *" and jobs["pnl"].level is Level.L0
    assert jobs["incident"].effect is Effect.READ
    tool = m.tools["incident"]
    assert tool.effect is Effect.WRITE and tool.level is Level.L3


def test_vendored_emit_is_the_sdks() -> None:
    assert (EXT / "emit.py").read_text() == SDK_EMIT.read_text()


async def test_pnl_is_normal_and_only_a_new_bucket_is_news(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    ch = Channel()
    pulse = make_pulse(db, ch)
    # Hour 10: 10 * 7919 % 6000 - 3000 = -$1,810. The title has the figure, state its bucket.
    (event,) = run_cli(tmp_path, "pnl")
    assert event["type"] == "item" and event["text"] == "PnL -$1.8k today"
    assert event["data"]["urgency"] == "normal"
    assert event["data"]["state"] == {"bucket": -2000}
    await ingest(db, pulse, [event])
    assert ch.texts == []  # normal: waits for the pulse
    item = items.get(db, "example", "example/pnl/today")
    assert item is not None and item.urgency == "normal" and item.changed
    with transaction(db):
        items.mark_told(db, [item], utcnow())
    # Same hour again: same bucket, not news. Another hour in the same bucket: still not.
    await ingest(db, pulse, run_cli(tmp_path, "pnl"))
    again = items.get(db, "example", "example/pnl/today")
    assert again is not None and not again.changed


async def test_incident_urgent_once_then_all_clear(db: sqlite3.Connection, tmp_path: Path) -> None:
    ch = Channel()
    pulse = make_pulse(db, ch)
    assert run_cli(tmp_path, "incident-check") == []  # nothing set: nothing reported
    assert run_cli(tmp_path, "incident", "--state", "failing") == []  # a tool reports nothing
    for _ in range(3):  # the job runs every 5 minutes; the same state is told once
        await ingest(db, pulse, run_cli(tmp_path, "incident-check"))
    assert ch.texts == ["[example] \U0001f534 Demo service failing: checkout 5xx"]
    run_cli(tmp_path, "incident", "--state", "resolved")
    for _ in range(2):
        await ingest(db, pulse, run_cli(tmp_path, "incident-check"))
    assert ch.texts[1:] == ["[example] ✓ Demo service recovered"]
    assert not (tmp_path / "incident").exists()

"""P3-11, PER-256: extension tools in a chat toolset (name collisions) and the local runner's
direct (slash-command) extension calls (docs/phase3-plan.md D9). The intents, the labeler
prefix and the per-intent toolsets are gone; the real PolicyGate and Executor; a fake
extension host (the ``ExtensionCatalog`` seam)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import pytest

from resonant.config import LoopConfig
from resonant.e2e.fakes import ScriptedModel
from resonant.executor import Executor, HandlerKey, ToolHandler
from resonant.gate import PolicyGate
from resonant.gateway.channel import (
    ApprovalPrompt,
    MessageRef,
    SelfTestResult,
    Submit,
    approval_dedupe_key,
)
from resonant.loop import Loop, NewTask, SlotPool
from resonant.loop.types import PRIORITY_OWNER, Task
from resonant.principals import Principal, Principals
from resonant.router.router import chat_toolset
from resonant.runners.local import LocalRunner, PinnedCall, is_explicit
from resonant.store import tasks as repo
from resonant.store.db import kv_set, transaction
from resonant.tools import ToolRegistry, notes
from resonant.tools.builtin import BUILTIN_SPECS
from resonant.tools.tasks import TASK_COUNTS
from resonant_sdk import Event, Manifest, ToolSpec
from resonant_sdk.manifest import parse_yaml

OWNER, MEMBER, HELPER = "justin", "cofounder", "helper"

DEMO = """\
name: demo
version: 1
description: Demo things; tells the time and purges the demo cache.
examples:
  - what time is it in tokyo
  - purge the demo cache
tools:
  get_time:
    description: Current time in a timezone.
    run: ./cli.py time --tz {tz}
    effect: read
    args: {tz: string}
  purge:
    description: Delete the demo cache.
    run: ./cli.py purge
    effect: write
    level: L1
  big:
    description: A big read.
    run: ./cli.py big
    effect: read
  disk:
    description: Shadows the builtin disk tool.
    run: ./cli.py disk
    effect: read
jobs:
  - id: nightly
    schedule: "0 3 * * *"
    run: ./cli.py nightly
    effect: read
"""
OTHER = """\
name: other
version: 1
description: Other things.
tools:
  ping:
    run: ./cli.py ping
    effect: read
"""


def manifest(text: str) -> Manifest:
    return Manifest.model_validate(parse_yaml(text))


@dataclass
class FakeHost:
    """The ``ExtensionCatalog`` seam of ``ExtensionHost``: tools, jobs and manifests."""

    manifests: dict[str, Manifest]

    def tool_specs(self, extension: str | None = None) -> list[ToolSpec]:
        return [
            s
            for name in sorted(self.manifests)
            if extension is None or name == extension
            for s in sorted(self.manifests[name].tool_specs(), key=lambda s: s.name)
        ]

    def job_specs(self) -> list[ToolSpec]:
        return [s for m in self.manifests.values() for s in m.job_specs()]

    def is_job(self, spec: ToolSpec) -> bool:
        return any((s.extension, s.name) == (spec.extension, spec.name) for s in self.job_specs())

    def manifest(self, name: str) -> Manifest | None:
        return self.manifests.get(name)


@pytest.fixture
def host() -> FakeHost:
    return FakeHost({"demo": manifest(DEMO), "other": manifest(OTHER)})


@pytest.fixture
def principals() -> Principals:
    return Principals(
        principals={
            OWNER: Principal(role="owner", identities=("imessage:+15550000000",)),
            MEMBER: Principal(identities=("imessage:+15551111111",)),
            HELPER: Principal(identities=("imessage:+15552222222",), grants={"demo": ("request",)}),
        }
    )


@pytest.fixture
def registry(host: FakeHost) -> ToolRegistry:
    reg = ToolRegistry()
    for spec in BUILTIN_SPECS:
        reg.register(spec)
    for spec in (*host.tool_specs(), *host.job_specs()):
        reg.register(spec)
    return reg


# --- toolsets -----------------------------------------------------------------------------


def names(specs: list[ToolSpec]) -> list[tuple[str | None, str]]:
    return [(s.extension, s.name) for s in specs]


def test_chat_toolset_refuses_name_collisions(registry: ToolRegistry) -> None:
    builtins = frozenset(s.name for s in BUILTIN_SPECS)
    demo = {s.name: s for s in manifest(DEMO).tool_specs()}
    clash = manifest(OTHER.replace("ping:", "get_time:")).tool_specs()[0]
    out = chat_toolset([demo["get_time"], demo["disk"], clash, demo["big"]], builtins)
    assert names(out) == [("demo", "big")]  # get_time held twice, disk is a builtin name
    disk = next(s for s in BUILTIN_SPECS if s.name == "disk")
    assert chat_toolset([disk, disk], builtins) == [disk]  # the same tool twice is fine


# --- the local runner ---------------------------------------------------------------------


@dataclass
class Channel:
    name: str = "imessage"
    sent: list[str] = field(default_factory=list[str])
    prompts: list[str] = field(default_factory=list[str])
    keys: set[str] = field(default_factory=set[str])

    async def start(self, submit: Submit) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        if dedupe_key not in self.keys:
            self.sent.append(text)
        if dedupe_key is not None:
            self.keys.add(dedupe_key)
        return MessageRef("imessage", f"out:{len(self.sent)}")

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        key = approval_dedupe_key(prompt.approval_id, principal)
        if key not in self.keys:
            self.prompts.append(prompt.text())
            self.keys.add(key)
        return MessageRef("imessage", f"prompt:{len(self.prompts)}")

    async def self_test(self) -> SelfTestResult:
        return SelfTestResult(True, "fake")


@dataclass
class Bench:
    db: sqlite3.Connection
    channel: Channel
    loop: Loop
    ran: list[tuple[str | None, str]]

    def add(self, checkpoint: dict[str, Any], text: str = "hi", kind: str = "chat") -> str:
        event = Event(
            source="imessage",
            type="message",
            principal=OWNER,
            payload={"text": text, "handle": "+15550000000"},
        )
        with transaction(self.db):
            repo.insert_event(self.db, event)
            task_id = repo.insert_task(
                self.db,
                NewTask(
                    kind=kind,
                    runner="local",
                    priority=PRIORITY_OWNER,
                    principal=OWNER,
                    checkpoint=checkpoint,
                    origin_event_id=event.id,
                    trace_id=event.trace_id,
                ),
            )
            repo.attach_event(self.db, event.id, task_id)
        return task_id

    def task(self, task_id: str) -> Task:
        t = repo.get_task(self.db, task_id)
        assert t is not None
        return t

    def origins(self, task_id: str) -> list[tuple[str | None, str, str, str]]:
        rows = self.db.execute(
            "SELECT extension, tool, decision, detail FROM audit_log "
            "WHERE task_id = ? AND action = 'gate.evaluate' ORDER BY id",
            (task_id,),
        ).fetchall()
        return [
            (r["extension"], r["tool"], r["decision"], json.loads(r["detail"])["origin"])
            for r in rows
        ]


BIG = {"rows": ["x" * 100] * 100}


@pytest.fixture
def bench(db: sqlite3.Connection, host: FakeHost, principals: Principals) -> Any:
    def make() -> Bench:
        registry = ToolRegistry()
        disk = next(s for s in BUILTIN_SPECS if s.name == "disk")
        for spec in (*notes.SPECS, TASK_COUNTS, disk, *host.tool_specs(), *host.job_specs()):
            registry.register(spec)
        ran: list[tuple[str | None, str]] = []

        def handler(key: HandlerKey, data: dict[str, Any]) -> ToolHandler:
            async def run(args: dict[str, Any]) -> dict[str, Any]:
                ran.append(key)
                return data

            return run

        handlers: dict[HandlerKey, ToolHandler] = {
            (None, "task_counts"): handler((None, "task_counts"), {"total": 7}),
            ("demo", "get_time"): handler(("demo", "get_time"), {"time": "09:00"}),
            ("demo", "purge"): handler(("demo", "purge"), {"purged": True}),
            ("demo", "big"): handler(("demo", "big"), BIG),
            ("demo", "job_nightly"): handler(("demo", "job_nightly"), {}),
        }
        with transaction(db):
            kv_set(db, "autonomy", "on")
        gate = PolicyGate(db, principals, registry, dry_run=False, approval_ttl_s=1800.0)
        executor = Executor(db, gate, handlers)
        channel = Channel()
        runner = LocalRunner(
            db,
            model=ScriptedModel(),
            channel=channel,
            gate=gate,
            executor=executor,
            registry=registry,
            extensions=host,
        )
        loop = Loop(
            db,
            {"local": runner},
            LoopConfig(tick_s=0.01, stuck_after_s=60, max_attempts=3),
            SlotPool(5, 1),
        )
        return Bench(db, channel, loop, ran)

    return make


async def test_direct_call_with_extension_is_explicit(bench: Any) -> None:
    b: Bench = bench()
    cp = {"direct_call": {"extension": "demo", "tool": "get_time", "args": {"tz": "UTC"}}}
    task_id = b.add(cp, text="/demo get_time UTC", kind="command")
    await b.loop.run_until_idle()
    assert b.ran == [("demo", "get_time")]
    assert b.origins(task_id) == [("demo", "get_time", "Allow", "explicit")]
    assert b.task(task_id).checkpoint["call"]["extension"] == "demo"


async def test_direct_call_to_an_unknown_extension_tool_runs_nothing(bench: Any) -> None:
    b: Bench = bench()
    cp: dict[str, Any] = {"direct_call": {"extension": "demo", "tool": "nope", "args": {}}}
    b.add(cp, kind="command")
    await b.loop.run_until_idle()
    assert b.ran == [] and b.channel.sent == ["Not done: that tool isn't available."]


def test_explicit_needs_the_exact_pinned_call() -> None:
    cp: dict[str, Any] = {"direct_call": {"extension": "demo", "tool": "purge", "args": {}}}
    assert is_explicit(cp, PinnedCall("purge", "demo", {}, 0, 0))
    assert not is_explicit(cp, PinnedCall("purge", None, {}, 0, 0))  # a builtin of that name
    assert not is_explicit(cp, PinnedCall("purge", "other", {}, 0, 0))
    assert not is_explicit(cp, PinnedCall("purge", "demo", {"x": 1}, 0, 0))
    assert not is_explicit(cp, PinnedCall("purge", "demo", {}, 0, 1))
    builtin: dict[str, Any] = {
        "direct_call": {"tool": "add_note", "args": {"text": "a"}}
    }  # older checkpoint
    assert is_explicit(builtin, PinnedCall("add_note", None, {"text": "a"}, 0, 0))
    assert not is_explicit(builtin, PinnedCall("add_note", "demo", {"text": "a"}, 0, 0))

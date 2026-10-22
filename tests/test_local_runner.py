"""LocalRunner (PER-256: it has no model loop): the fast reply, the resend and the send
failures, and the checkpoint of a chat task from before the retirement of the intents path.
Direct calls, pinned calls and batches are in ``test_e2e_*`` and ``test_toolcore.py``."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

import pytest

from resonant.config import LoopConfig
from resonant.e2e.fakes import ScriptedModel
from resonant.executor import Executor, HandlerKey, ToolHandler
from resonant.gate import DryRunGate
from resonant.gateway.channel import (
    ApprovalPrompt,
    ChannelRefused,
    ChannelSendError,
    MessageRef,
    SelfTestResult,
    Submit,
)
from resonant.loop import Loop, NewTask, SlotPool
from resonant.loop.types import PRIORITY_OWNER, StepContext, Task
from resonant.principals import Principal, Principals
from resonant.runners.local import NO_ANSWER_REPLY, LocalRunner, summarize
from resonant.store import tasks as repo
from resonant.store.db import transaction
from resonant.tools import ToolRegistry
from resonant.tools.builtin import BUILTIN_SPECS
from resonant_sdk import Event

OWNER, MEMBER = "justin", "cofounder"

CANNED: dict[str, dict[str, Any]] = {
    "task_counts": {
        "by_status": {"running": 2, "queued": 1, "done": 14},
        "waiting_by_reason": {},
        "total": 17,
    },
}


class Crash(BaseException):
    """Simulates the process dying mid-step (not an Exception, so nothing catches it)."""


@dataclass
class FakeChannel:
    """Records delivered texts. Dedupes by key like ``IMessageChannel``."""

    name: str = "imessage"
    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    keys: set[str] = field(default_factory=set[str])
    fail_with: list[BaseException] = field(default_factory=list[BaseException])
    crash_after_send: bool = False
    attempts: int = 0

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
        self.attempts += 1
        if self.fail_with:
            raise self.fail_with.pop(0)
        if dedupe_key is None or dedupe_key not in self.keys:
            self.sent.append((principal, text, dedupe_key))
            if dedupe_key is not None:
                self.keys.add(dedupe_key)
        if self.crash_after_send:
            self.crash_after_send = False
            raise Crash
        return MessageRef("imessage", f"imessage-out:{len(self.sent)}")

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        raise NotImplementedError

    async def self_test(self) -> SelfTestResult:
        return SelfTestResult(True, "fake")


# --- fixtures ----------------------------------------------------------------------------


@pytest.fixture
def principals() -> Principals:
    return Principals(
        principals={
            OWNER: Principal(role="owner", identities=("imessage:+15550000000",)),
            MEMBER: Principal(identities=("imessage:+15551111111",), grants={"dori": ("request",)}),
        }
    )


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    for spec in BUILTIN_SPECS:
        reg.register(spec)
    return reg


@pytest.fixture
def tool_log() -> list[tuple[str, dict[str, Any]]]:
    return []


@pytest.fixture
def handlers(tool_log: list[tuple[str, dict[str, Any]]]) -> dict[HandlerKey, ToolHandler]:
    def make(name: str) -> ToolHandler:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            tool_log.append((name, args))
            return CANNED.get(name, {})

        return handler

    return {(None, s.name): make(s.name) for s in BUILTIN_SPECS}


@dataclass
class Harness:
    db: sqlite3.Connection
    channel: FakeChannel
    runner: LocalRunner
    loop: Loop

    def add_task(
        self,
        checkpoint: dict[str, Any],
        text: str = "what's running?",
        principal: str = OWNER,
        kind: str = "chat",
    ) -> str:
        event = Event(
            source="imessage",
            type="message",
            principal=principal,
            payload={"text": text, "guid": "G", "handle": "+15550000000"},
        )
        with transaction(self.db):
            repo.insert_event(self.db, event)
            task_id = repo.insert_task(
                self.db,
                NewTask(
                    kind=kind,
                    runner="local",
                    priority=PRIORITY_OWNER,
                    principal=principal,
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

    def audit_actions(self, task_id: str) -> list[str]:
        rows = self.db.execute(
            "SELECT action FROM audit_log WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        return [r["action"] for r in rows]


def make_harness(
    db: sqlite3.Connection,
    principals: Principals,
    registry: ToolRegistry,
    handlers: dict[HandlerKey, ToolHandler],
    channel: FakeChannel | None = None,
) -> Harness:
    gate = DryRunGate(db, principals, registry)
    executor = Executor(db, gate, handlers)
    channel = channel or FakeChannel()
    runner = LocalRunner(
        db, model=ScriptedModel(), channel=channel, gate=gate, executor=executor, registry=registry
    )
    loop = Loop(
        db,
        {"local": runner},
        LoopConfig(tick_s=0.01, stuck_after_s=60, max_attempts=3),
        SlotPool(5, 1),
    )
    return Harness(db, channel, runner, loop)


@pytest.fixture
def make(
    db: sqlite3.Connection,
    principals: Principals,
    registry: ToolRegistry,
    handlers: dict[HandlerKey, ToolHandler],
) -> Any:
    def _make(channel: FakeChannel | None = None) -> Harness:
        return make_harness(db, principals, registry, handlers, channel)

    return _make


@dataclass
class DirectCtx:
    """StepContext over the store, for driving ``step`` by hand."""

    db: sqlite3.Connection
    task_id: str

    def save(
        self,
        *,
        checkpoint: dict[str, Any] | None = None,
        claude_session_id: str | None = None,
        worktree_path: str | None = None,
    ) -> bool:
        with transaction(self.db):
            return repo.save_progress(
                self.db,
                self.task_id,
                checkpoint=checkpoint,
                claude_session_id=claude_session_id,
                worktree_path=worktree_path,
            )


async def crash_step(h: Harness, task_id: str) -> None:
    """Run one step that dies with ``Crash``, leaving the task 'running' like a real crash."""
    with transaction(h.db):
        assert repo.set_status(h.db, task_id, "running", expect=("queued",))
    t = h.task(task_id)
    ctx: StepContext = DirectCtx(h.db, task_id)
    with pytest.raises(Crash):
        await h.runner.step(t, repo.pending_events(h.db, task_id), ctx)


async def test_fast_reply_is_sent_as_is(make: Any) -> None:
    h: Harness = make()
    task_id = h.add_task(
        {"fast_reply": "pong", "tool_used": "daemon_status", "intent": "system", "extra": 1},
        text="ping",
        kind="reply",
    )
    await h.loop.run_until_idle()
    assert h.channel.sent == [(OWNER, "pong", f"reply:{task_id}")]
    t = h.task(task_id)
    assert t.status == "done"
    assert t.result is not None
    assert t.result["tools_used"] == ["daemon_status"]
    assert t.result["path"] == "fast"


async def test_an_old_chat_checkpoint_gets_the_no_answer_reply(make: Any) -> None:
    """PER-256: no intents path. A chat task queued with an ``intent`` before the upgrade
    (or ``fallback``) is not answered by a model: it says so, once, and is done."""
    h: Harness = make()
    task_id = h.add_task({"intent": "job_control", "toolset": ["task_counts"], "fallback": False})
    other = h.add_task({"intent": "unknown", "toolset": [], "fallback": True})
    await h.loop.run_until_idle()
    assert [s[1] for s in h.channel.sent] == [NO_ANSWER_REPLY, NO_ANSWER_REPLY]
    for tid in (task_id, other):
        t = h.task(tid)
        assert t.status == "done" and t.result is not None and t.result["path"] == "fallback"


# --- crash safety and send failures ------------------------------------------------------


async def test_crash_before_send_sends_exactly_once_on_retry(make: Any) -> None:
    channel = FakeChannel(fail_with=[Crash()])
    h: Harness = make(channel)
    task_id = h.add_task({"fast_reply": "17 total."}, kind="reply")
    await crash_step(h, task_id)
    assert h.task(task_id).checkpoint["reply"] == "17 total."
    assert channel.sent == []

    assert h.loop.resume() == 1
    await h.loop.run_until_idle()
    assert channel.sent == [(OWNER, "17 total.", f"reply:{task_id}")]
    assert h.task(task_id).status == "done"


async def test_crash_after_send_does_not_double_send(make: Any) -> None:
    channel = FakeChannel(crash_after_send=True)
    h: Harness = make(channel)
    task_id = h.add_task({"fast_reply": "hi"}, text="hello", kind="reply")
    await crash_step(h, task_id)
    h.loop.resume()
    await h.loop.run_until_idle()
    assert channel.attempts == 2  # resent with the same dedupe key...
    assert channel.sent == [(OWNER, "hi", f"reply:{task_id}")]  # ...delivered once
    assert h.task(task_id).status == "done"


async def test_send_failure_is_retryable_then_succeeds(make: Any) -> None:
    channel = FakeChannel(fail_with=[ChannelSendError("Messages is not running")])
    h: Harness = make(channel)
    task_id = h.add_task({"fast_reply": "hi"}, text="hello", kind="reply")
    await h.loop.run_until_idle()
    t = h.task(task_id)
    assert t.status == "queued"
    assert t.attempts == 1
    assert t.error is not None and "Messages is not running" in t.error

    with transaction(h.db):  # skip the backoff
        h.db.execute("UPDATE tasks SET wake_at = NULL WHERE id = ?", (task_id,))
    await h.loop.run_until_idle()
    assert channel.sent == [(OWNER, "hi", f"reply:{task_id}")]
    assert h.task(task_id).status == "done"


async def test_refused_send_fails_for_good(make: Any) -> None:
    channel = FakeChannel(fail_with=[ChannelRefused("no imessage identity")])
    h: Harness = make(channel)
    task_id = h.add_task({"fast_reply": "pong"}, kind="reply")
    await h.loop.run_until_idle()
    assert h.task(task_id).status == "failed"


async def test_spans_never_carry_message_text(make: Any) -> None:
    secret = "my bank pin is 9911"
    h: Harness = make()
    h.add_task({"fast_reply": "17 total."}, text=secret, kind="reply")
    await h.loop.run_until_idle()
    rows = h.db.execute("SELECT name, attrs FROM spans").fetchall()
    assert {"runner.local", "runner.local.send"} <= {r["name"] for r in rows}
    for r in rows:
        assert secret not in r["attrs"]
        assert "17 total." not in r["attrs"]


# --- pure helpers ------------------------------------------------------------------------


def test_summarize_without_results() -> None:
    assert "/status" in summarize([])


def test_components_register_local_runner(db: sqlite3.Connection) -> None:
    from resonant.api import DaemonState
    from resonant.components import build_components
    from resonant.config import load_settings
    from resonant.store.db import utcnow

    settings = load_settings()
    state = DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)), utcnow())
    comps = build_components(settings, db, state)
    assert comps.runners == {}
    runner = comps.register_local_runner(db, model=ScriptedModel(), channel=FakeChannel())
    assert comps.runners == {"local": runner}
    assert runner.name == "local"
    assert runner.uses_claude_slot is False
    assert runner.max_step_s == 30

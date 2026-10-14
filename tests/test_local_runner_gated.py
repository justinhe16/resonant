"""LocalRunner gated calls: direct_call (explicit), approvals (Wait/resume), exactly once,
templated reports, refusals and dry-run replies. Uses the real PolicyGate and Executor."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest

from resonant.config import LoopConfig, load_settings
from resonant.executor import Executor, HandlerKey, ToolFailed, ToolHandler
from resonant.gate import ApprovalResolution, Deny, DryRun, Intent, PolicyGate
from resonant.gate.policy import APPROVAL_EXPIRED, APPROVAL_USED, NOT_APPROVED
from resonant.gateway.channel import (
    ApprovalPrompt,
    ChannelRefused,
    ChannelSendError,
    MessageRef,
    SelfTestResult,
    Submit,
    approval_dedupe_key,
)
from resonant.launchd import DAEMON_LABEL
from resonant.loop import Loop, NewTask, SlotPool
from resonant.loop.types import PRIORITY_OWNER, StepContext, Task
from resonant.models import (
    ChatMessage,
    ModelReply,
    ModelUnavailableError,
    ProbeResult,
    SpanFactory,
    ToolCall,
)
from resonant.ops import RESTART_DELAY_S, restart_later_argv, restart_pending
from resonant.ops.fakes import FakeOpsRunner
from resonant.principals import Principal, Principals
from resonant.runners.local import (
    LocalRunner,
    dry_run_reply,
    refusal_reply,
    report_line,
)
from resonant.store import approvals as ap
from resonant.store import tasks as repo
from resonant.store.db import kv_set, transaction
from resonant.tools import ToolRegistry, notes, reminders
from resonant.tools.ops import RESTART_OLLAMA, RESTART_RESONANT, Ops
from resonant.tools.tasks import TASK_COUNTS
from resonant_sdk import Effect, Event, Level, ToolSpec

OWNER = "justin"
T0 = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
TTL_S = 1800.0


class Crash(BaseException):
    """The process dying mid-step: not an Exception, so nothing catches it."""


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeModel:
    """Scripted ``chat_tools`` replies (then none) and one ``chat_json`` reply."""

    def __init__(
        self,
        tool_replies: list[list[ToolCall]] | None = None,
        final: dict[str, Any] | Exception | None = None,
    ) -> None:
        self.tool_replies = list(tool_replies or [])
        self.final = final if final is not None else {"reply": "ok"}
        self.tool_calls_made = 0
        self.json_calls: list[list[ChatMessage]] = []

    async def chat_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        max_tokens: int = 512,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        self.tool_calls_made += 1
        out = self.tool_replies.pop(0) if self.tool_replies else []
        return ModelReply(None, out, None, 5, 10, 3)

    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        max_tokens: int = 512,
        temperature: float = 0.0,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        self.json_calls.append(list(messages))
        if isinstance(self.final, Exception):
            raise self.final
        return ModelReply(json.dumps(self.final), [], self.final, 5, 10, 3)

    async def probe(self) -> ProbeResult:
        raise AssertionError("runner never probes")


@dataclass
class FakeChannel:
    """Records replies and approval prompts, deduping both by key like the real adapter."""

    name: str = "imessage"
    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    prompts: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    prompt_calls: int = 0
    refs: dict[str, MessageRef] = field(default_factory=dict[str, MessageRef])
    fail_send: list[BaseException] = field(default_factory=list[BaseException])
    fail_prompt: list[BaseException] = field(default_factory=list[BaseException])
    unreachable: set[str] = field(default_factory=set[str])  # refused: no identity

    async def start(self, submit: Submit) -> None:
        pass

    async def stop(self) -> None:
        pass

    def _deliver(self, principal: str, text: str, key: str | None) -> MessageRef:
        if key is not None and key in self.refs:
            return self.refs[key]
        self.sent.append((principal, text, key))
        ref = MessageRef("imessage", f"imessage-out:{len(self.sent)}")
        if key is not None:
            self.refs[key] = ref
        return ref

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        if self.fail_send:
            raise self.fail_send.pop(0)
        return self._deliver(principal, text, dedupe_key)

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        self.prompt_calls += 1
        if self.fail_prompt:
            raise self.fail_prompt.pop(0)
        if principal in self.unreachable:
            raise ChannelRefused(f"no imessage identity for {principal}")
        key = approval_dedupe_key(prompt.approval_id, principal)
        if key not in self.refs:
            self.prompts.append((principal, prompt.text()))
        return self._deliver(principal, prompt.text(), key)

    def replies(self) -> list[str]:
        return [text for _, text, key in self.sent if key is not None and key.startswith("reply:")]

    async def self_test(self) -> SelfTestResult:
        return SelfTestResult(True, "fake")


@dataclass
class Harness:
    db: sqlite3.Connection
    clock: Clock
    model: FakeModel
    channel: FakeChannel
    gate: PolicyGate
    executor: Executor
    runner: LocalRunner
    loop: Loop
    tool_log: list[str]

    def add_task(self, checkpoint: dict[str, Any], text: str = "hi", kind: str = "chat") -> str:
        event = Event(
            source="imessage",
            type="message",
            principal=OWNER,
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

    def approvals(self, task_id: str) -> list[ap.Approval]:
        rows = self.db.execute(
            "SELECT id FROM approvals WHERE task_id = ? ORDER BY created_at", (task_id,)
        ).fetchall()
        return [a for r in rows if (a := ap.get_approval(self.db, r["id"])) is not None]

    def resolve(self, task_id: str, *, approved: bool) -> None:
        (approval,) = self.approvals(task_id)
        result = self.gate.resolve(ApprovalResolution(approval.id, approved, OWNER, "cli", "cli"))
        assert result.ok, result.reason

    def count(self, sql: str, *params: Any) -> int:
        return int(self.db.execute(sql, params).fetchone()[0])

    def executions(self, task_id: str) -> int:
        return self.count("SELECT COUNT(*) FROM executions WHERE task_id = ?", task_id)

    def evaluations(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT decision, detail FROM audit_log WHERE task_id = ? AND action = "
            "'gate.evaluate' ORDER BY id",
            (task_id,),
        ).fetchall()
        return [{"decision": r["decision"], **json.loads(r["detail"])} for r in rows]

    def notes(self) -> list[str]:
        return [n["text"] for n in notes.list_notes(self.db)["notes"]]

    def skip_backoff(self, task_id: str) -> None:
        with transaction(self.db):
            self.db.execute("UPDATE tasks SET wake_at = NULL WHERE id = ?", (task_id,))


@pytest.fixture
def make(db: sqlite3.Connection) -> Any:
    def _make(
        model: FakeModel | None = None,
        *,
        dry_run: bool = False,
        restart: ToolHandler | None = None,
        add_note: ToolHandler | None = None,
        principals: Principals | None = None,
        approvers_for: Callable[[str], list[str]] | None = None,
        extra_specs: tuple[ToolSpec, ...] = (),
    ) -> Harness:
        clock = Clock()
        principals = principals or Principals(
            principals={OWNER: Principal(role="owner", identities=("imessage:+15550000000",))}
        )
        registry = ToolRegistry()
        for spec in (*notes.SPECS, TASK_COUNTS, RESTART_RESONANT, RESTART_OLLAMA, *extra_specs):
            registry.register(spec)
        tool_log: list[str] = []

        def logged(name: str, fn: Callable[[dict[str, Any]], dict[str, Any]]) -> ToolHandler:
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                tool_log.append(name)
                return fn(args)

            return handler

        handlers: dict[HandlerKey, ToolHandler] = {
            (None, "add_note"): add_note or logged("add_note", partial(notes.add_note, db)),
            (None, "delete_note"): logged("delete_note", partial(notes.delete_note, db)),
            (None, "clear_notes"): logged("clear_notes", lambda _a: notes.clear_notes(db)),
            (None, "list_notes"): logged("list_notes", lambda _a: notes.list_notes(db)),
            (None, "task_counts"): logged("task_counts", lambda _a: {"total": 7}),
            (None, "restart_resonant"): restart or logged("restart_resonant", lambda _a: {}),
            (None, "restart_ollama"): logged("restart_ollama", lambda _a: {}),
        }
        with transaction(db):
            kv_set(db, "autonomy", "on")
        gate = PolicyGate(
            db,
            principals,
            registry,
            dry_run=dry_run,
            approval_ttl_s=TTL_S,
            actions={(None, "clear_notes"): partial(notes.summary_action, db)},
            approvers_for=approvers_for or (lambda _ext: []),
            clock=clock,
        )
        executor = Executor(db, gate, handlers)
        model = model or FakeModel()
        channel = FakeChannel()
        runner = LocalRunner(
            db,
            model=model,
            channel=channel,
            gate=gate,
            executor=executor,
            registry=registry,
            now=clock,
        )
        loop = Loop(
            db,
            {"local": runner},
            LoopConfig(tick_s=0.01, stuck_after_s=60, max_attempts=3),
            SlotPool(5, 1),
            clock=clock,
        )
        return Harness(db, clock, model, channel, gate, executor, runner, loop, tool_log)

    return _make


def direct(tool: str, **args: Any) -> dict[str, Any]:
    return {"direct_call": {"tool": tool, "args": args}, "title": f"/{tool}"}


def chat(*toolset: str) -> dict[str, Any]:
    return {"intent": "question", "confidence": 0.9, "toolset": list(toolset), "fallback": False}


def tc(name: str, **args: Any) -> list[ToolCall]:
    return [ToolCall(name, args)]


@dataclass
class CrashCtx:
    """A StepContext that commits a save, then dies if ``crash_if(checkpoint)``."""

    db: sqlite3.Connection
    task_id: str
    crash_if: Callable[[dict[str, Any]], bool]

    def save(
        self,
        *,
        checkpoint: dict[str, Any] | None = None,
        claude_session_id: str | None = None,
        worktree_path: str | None = None,
    ) -> bool:
        with transaction(self.db):
            ok = repo.save_progress(
                self.db,
                self.task_id,
                checkpoint=checkpoint,
                claude_session_id=None,
                worktree_path=None,
            )
        if checkpoint is not None and self.crash_if(checkpoint):
            raise Crash
        return ok


async def crash_step(h: Harness, task_id: str, crash_if: Callable[[dict[str, Any]], bool]) -> None:
    with transaction(h.db):
        assert repo.set_status(h.db, task_id, "running", expect=("queued",))
    ctx: StepContext = CrashCtx(h.db, task_id, crash_if)
    with pytest.raises(Crash):
        await h.runner.step(h.task(task_id), repo.pending_events(h.db, task_id), ctx)
    h.loop.resume()  # a new daemon on the same DB requeues the interrupted task


def pinned_only(cp: dict[str, Any]) -> bool:
    return "call" in cp and "reply" not in cp


# --- explicit (direct_call) --------------------------------------------------------------


async def test_explicit_l3_runs_with_no_prompt(make: Any) -> None:
    h: Harness = make()
    task_id = h.add_task(direct("add_note", text="buy milk"), text="/note buy milk", kind="command")
    await h.loop.run_until_idle()

    assert h.channel.sent == [(OWNER, 'Saved note 1: "buy milk"', f"reply:{task_id}")]
    assert h.channel.prompts == []
    assert h.executions(task_id) == 1
    assert [(e["decision"], e["origin"]) for e in h.evaluations(task_id)] == [("Allow", "explicit")]
    t = h.task(task_id)
    assert t.status == "done"
    assert t.checkpoint["call"]["step_no"] == 0 and t.checkpoint["call"]["call_index"] == 0
    assert h.model.tool_calls_made == 0 and h.model.json_calls == []


async def test_explicit_l2_runs_with_no_prompt(make: Any) -> None:
    h: Harness = make()
    task_id = h.add_task(direct("restart_resonant"), kind="command")
    await h.loop.run_until_idle()
    assert h.channel.replies() == ["Done: restart Resonant"]
    assert h.channel.prompts == []
    assert h.tool_log == ["restart_resonant"]
    assert [(e["decision"], e["origin"]) for e in h.evaluations(task_id)] == [("Allow", "explicit")]


async def test_explicit_l1_asks_with_the_destructive_text(make: Any) -> None:
    h: Harness = make()
    for text in ("a", "b", "c", "d"):
        notes.add_note(h.db, {"text": text})
    task_id = h.add_task(direct("clear_notes"), kind="command")
    await h.loop.run_until_idle()

    (prompt,) = h.channel.prompts
    code = h.approvals(task_id)[0].code
    assert prompt == (
        OWNER,
        f"[{code}] delete all 4 notes? This can't be undone. Reply yes {code} / no",
    )
    assert h.tool_log == []
    assert h.channel.replies() == []

    h.resolve(task_id, approved=True)
    await h.loop.run_until_idle()
    assert h.channel.replies() == ["Cleared 4 notes."]
    assert h.notes() == []


async def test_unknown_direct_call_tool_runs_nothing(make: Any) -> None:
    h: Harness = make()
    task_id = h.add_task(direct("format_disk"), kind="command")
    await h.loop.run_until_idle()
    assert h.channel.replies() == ["Not done: that tool isn't available."]
    assert h.evaluations(task_id) == []


# --- inferred: approvals, Wait and resume ------------------------------------------------


async def test_inferred_l2_asks_waits_until_expiry_then_runs_once_on_yes(make: Any) -> None:
    h: Harness = make(FakeModel([tc("restart_resonant")], final={"reply": "Restarting."}))
    task_id = h.add_task(chat("restart_resonant"), text="please restart resonant")
    await h.loop.run_until_idle()

    (approval,) = h.approvals(task_id)
    assert h.channel.prompts == [(OWNER, f"[{approval.code}] restart resonant? Reply yes / no")]
    assert [r.principal for r in approval.channel_refs] == [OWNER]
    assert approval.channel_refs[0].at == T0
    t = h.task(task_id)
    assert (t.status, t.wait_reason, t.wake_at) == ("waiting", "approval", approval.expires_at)
    assert t.checkpoint["call"]["tool"] == "restart_resonant"
    assert h.tool_log == [] and h.channel.replies() == []

    h.resolve(task_id, approved=True)
    await h.loop.run_until_idle()
    assert h.tool_log == ["restart_resonant"]
    assert h.channel.replies() == ["Done: restart Resonant"]  # templated: no model round
    assert h.model.tool_calls_made == 1 and h.model.json_calls == []
    assert h.task(task_id).status == "done"
    assert [e["origin"] for e in h.evaluations(task_id)] == ["inferred", "inferred"]

    # Nothing can run it again: the task is done and the approval is spent.
    assert h.approvals(task_id)[0].used_at is not None
    await h.loop.run_until_idle()
    assert h.tool_log == ["restart_resonant"] and h.executions(task_id) == 1


async def test_no_gives_not_done(make: Any) -> None:
    h: Harness = make(FakeModel([tc("restart_resonant")]))
    task_id = h.add_task(chat("restart_resonant"))
    await h.loop.run_until_idle()
    h.resolve(task_id, approved=False)
    await h.loop.run_until_idle()
    assert h.channel.replies() == ["Not done."]
    assert h.tool_log == []
    assert h.task(task_id).status == "done"


async def test_expiry_wakes_the_task_and_says_so(make: Any) -> None:
    h: Harness = make(FakeModel([tc("restart_resonant")]))
    task_id = h.add_task(chat("restart_resonant"))
    await h.loop.run_until_idle()
    h.clock.advance(TTL_S - 1)
    await h.loop.run_until_idle()
    assert h.task(task_id).status == "waiting"

    h.clock.advance(1)
    await h.loop.run_until_idle()
    assert h.channel.replies() == ["Not done: the approval for restart Resonant expired."]
    assert h.tool_log == []
    assert h.approvals(task_id)[0].status == "expired"


async def test_spurious_wake_reprompts_idempotently(make: Any) -> None:
    h: Harness = make(FakeModel([tc("restart_resonant")]))
    task_id = h.add_task(chat("restart_resonant"))
    await h.loop.run_until_idle()
    wake_at = h.task(task_id).wake_at

    h.loop.submit(Event(source="test", type="poke", task_id=task_id, principal=OWNER))
    await h.loop.run_until_idle()
    assert h.channel.prompt_calls == 2  # re-prompted...
    assert len(h.channel.prompts) == 1  # ...but the channel deduped it
    (approval,) = h.approvals(task_id)
    assert len(approval.channel_refs) == 1 and approval.channel_refs[0].at == T0
    t = h.task(task_id)
    assert (t.status, t.wake_at) == ("waiting", wake_at)
    assert h.model.tool_calls_made == 1


async def test_prompt_send_failure_is_retryable(make: Any) -> None:
    h: Harness = make(FakeModel([tc("restart_resonant")]))
    h.channel.fail_prompt.append(ChannelSendError("Messages is not running"))
    task_id = h.add_task(chat("restart_resonant"))
    await h.loop.run_until_idle()
    t = h.task(task_id)
    assert t.status == "queued" and t.error is not None and "approval prompt" in t.error
    assert "call" in t.checkpoint  # saved before the prompt

    h.skip_backoff(task_id)
    await h.loop.run_until_idle()
    assert len(h.channel.prompts) == 1
    assert h.task(task_id).status == "waiting"
    assert h.model.tool_calls_made == 1
    assert len(h.approvals(task_id)) == 1


async def test_refused_prompt_fails_for_good(make: Any) -> None:
    h: Harness = make(FakeModel([tc("restart_resonant")]))
    h.channel.fail_prompt.append(ChannelRefused("no imessage identity"))
    task_id = h.add_task(chat("restart_resonant"))
    await h.loop.run_until_idle()
    assert h.task(task_id).status == "failed"
    assert h.approvals(task_id)[0].status == "expired"  # orphaned with the task


async def test_crash_between_save_and_prompt_prompts_on_retry(make: Any) -> None:
    h: Harness = make(FakeModel([tc("restart_resonant")]))
    task_id = h.add_task(chat("restart_resonant"))
    await crash_step(h, task_id, pinned_only)
    assert h.channel.prompts == []

    await h.loop.run_until_idle()
    assert len(h.channel.prompts) == 1
    assert len(h.approvals(task_id)) == 1  # the pending approval was reused
    assert h.task(task_id).status == "waiting"
    assert h.model.tool_calls_made == 1  # resumed from the pinned call, not the model


# --- exactly once ------------------------------------------------------------------------


async def test_crash_after_pin_before_started_runs_exactly_once(make: Any) -> None:
    h: Harness = make()
    task_id = h.add_task(direct("add_note", text="milk"), kind="command")
    await crash_step(h, task_id, pinned_only)
    assert h.executions(task_id) == 0

    await h.loop.run_until_idle()
    assert h.notes() == ["milk"]
    assert h.executions(task_id) == 1
    assert h.channel.replies() == ['Saved note 1: "milk"']


async def test_crash_after_started_is_ambiguous_and_never_reruns(make: Any) -> None:
    calls: list[dict[str, Any]] = []

    async def dies(args: dict[str, Any]) -> dict[str, Any]:
        calls.append(args)
        raise Crash

    h: Harness = make(add_note=dies)
    task_id = h.add_task(direct("add_note", text="milk"), kind="command")
    with transaction(h.db):
        repo.set_status(h.db, task_id, "running", expect=("queued",))
    ctx: StepContext = CrashCtx(h.db, task_id, lambda _cp: False)
    with pytest.raises(Crash):
        await h.runner.step(h.task(task_id), [], ctx)
    h.loop.resume()

    await h.loop.run_until_idle()
    assert len(calls) == 1
    (reply,) = h.channel.replies()
    assert reply.startswith('Not sure whether add note text="milk" ran')
    assert (h.task(task_id).result or {})["path"] == "ambiguous"
    assert len(h.evaluations(task_id)) == 1  # recorded() answered; the gate wasn't asked


async def test_crash_after_execute_replays_without_the_gate(make: Any) -> None:
    h: Harness = make()
    task_id = h.add_task(direct("add_note", text="milk"), kind="command")
    await crash_step(h, task_id, lambda cp: "reply" in cp)  # dies right after saving reply
    # The reply was saved, so the retry only resends it.
    await h.loop.run_until_idle()
    assert h.channel.replies() == ['Saved note 1: "milk"']
    assert h.notes() == ["milk"]
    assert len(h.evaluations(task_id)) == 1


async def test_lost_reply_replays_from_executions(make: Any) -> None:
    # Executed and recorded, but the step died before the reply was saved.
    h: Harness = make()
    task_id = h.add_task(direct("add_note", text="milk"), kind="command")

    with transaction(h.db):
        repo.set_status(h.db, task_id, "running", expect=("queued",))
    ctx: StepContext = CrashCtx(h.db, task_id, lambda cp: "reply" in cp)
    with pytest.raises(Crash):
        await h.runner.step(h.task(task_id), [], ctx)
    with transaction(h.db):  # drop the saved reply, as if the crash came just before it
        cp = {k: v for k, v in h.task(task_id).checkpoint.items() if k != "reply"}
        h.db.execute("UPDATE tasks SET checkpoint = ? WHERE id = ?", (json.dumps(cp), task_id))
    h.loop.resume()

    await h.loop.run_until_idle()
    assert h.notes() == ["milk"]
    assert h.executions(task_id) == 1
    assert h.channel.replies() == ['Saved note 1: "milk"']
    assert len(h.evaluations(task_id)) == 1
    assert (h.task(task_id).result or {})["path"] == "replay"


async def test_reply_send_failure_retries_without_rerunning(make: Any) -> None:
    h: Harness = make()
    h.channel.fail_send.append(ChannelSendError("Messages is not running"))
    task_id = h.add_task(direct("add_note", text="milk"), kind="command")
    await h.loop.run_until_idle()
    assert h.task(task_id).status == "queued"

    h.skip_backoff(task_id)
    await h.loop.run_until_idle()
    assert h.channel.replies() == ['Saved note 1: "milk"']
    assert h.notes() == ["milk"] and h.executions(task_id) == 1
    assert len(h.evaluations(task_id)) == 1


async def test_definite_failure_is_reported_not_retried(make: Any) -> None:
    async def fails(args: dict[str, Any]) -> dict[str, Any]:
        raise ToolFailed("launchctl exited 5")

    h: Harness = make(restart=fails)
    task_id = h.add_task(direct("restart_resonant"), kind="command")
    await h.loop.run_until_idle()
    assert h.channel.replies() == ["Failed: restart Resonant (launchctl exited 5)"]
    assert h.task(task_id).status == "done"


# --- the model path ----------------------------------------------------------------------


async def test_inferred_l3_runs_and_the_report_line_is_always_there(make: Any) -> None:
    h: Harness = make(FakeModel([tc("add_note", text="eggs")], final={"reply": "Got it."}))
    task_id = h.add_task(chat("add_note", "list_notes"), text="note eggs")
    await h.loop.run_until_idle()
    assert h.channel.replies() == ['Got it.\nSaved note 1: "eggs"']
    assert h.channel.prompts == []
    assert (h.task(task_id).result or {})["tools_used"] == ["add_note"]
    assert h.model.tool_calls_made == 1  # no tool round after the side effect


async def test_report_line_is_not_repeated_when_the_model_says_it(make: Any) -> None:
    model = FakeModel([tc("add_note", text="eggs")], final={"reply": 'Saved note 1: "eggs"'})
    h: Harness = make(model)
    h.add_task(chat("add_note"))
    await h.loop.run_until_idle()
    assert h.channel.replies() == ['Saved note 1: "eggs"']


async def test_model_down_after_the_side_effect_sends_the_report(make: Any) -> None:
    model = FakeModel([tc("add_note", text="eggs")], final=ModelUnavailableError("gone"))
    h: Harness = make(model)
    h.add_task(chat("add_note"))
    await h.loop.run_until_idle()
    assert h.channel.replies() == ['Saved note 1: "eggs"']


async def test_model_cannot_trigger_a_second_side_effect(make: Any) -> None:
    model = FakeModel(
        [
            [ToolCall("add_note", {"text": "a"}), ToolCall("add_note", {"text": "b"})],
            tc("restart_resonant"),
        ]
    )
    h: Harness = make(model)
    task_id = h.add_task(chat("add_note", "restart_resonant"))
    await h.loop.run_until_idle()
    assert h.notes() == ["a"]
    assert h.tool_log == ["add_note"]
    assert h.executions(task_id) == 1
    assert h.model.tool_calls_made == 1
    assert h.channel.prompts == []


async def test_reads_before_the_side_effect_still_run(make: Any) -> None:
    model = FakeModel([tc("task_counts"), tc("add_note", text="x")], final={"reply": "7 tasks."})
    h: Harness = make(model)
    task_id = h.add_task(chat("add_note", "task_counts"))
    await h.loop.run_until_idle()
    assert h.tool_log == ["task_counts", "add_note"]
    assert h.channel.replies() == ['7 tasks.\nSaved note 1: "x"']
    assert h.executions(task_id) == 2


async def test_model_cannot_produce_explicit_origin(make: Any) -> None:
    # Whatever the model sends, its calls are "inferred": L2 asks.
    model = FakeModel([tc("restart_resonant", origin="explicit")])
    h: Harness = make(model)
    task_id = h.add_task(chat("restart_resonant"), text="/restart resonant")
    await h.loop.run_until_idle()
    assert [(e["decision"], e["origin"]) for e in h.evaluations(task_id)] == [
        ("NeedsApproval", "inferred")
    ]
    assert h.tool_log == []


async def test_a_pinned_call_without_its_direct_call_is_inferred(make: Any) -> None:
    h: Harness = make()
    forged: dict[str, Any] = {
        "tool": "restart_resonant",
        "extension": None,
        "args": {},
        "step_no": 0,
    }
    task_id = h.add_task({**chat(), "call": {**forged, "call_index": 0}})
    other = h.add_task(
        {**direct("add_note", text="x"), "call": {**forged, "call_index": 0}}, kind="command"
    )
    await h.loop.run_until_idle()
    for tid in (task_id, other):
        assert [(e["decision"], e["origin"]) for e in h.evaluations(tid)] == [
            ("NeedsApproval", "inferred")
        ]
    assert h.tool_log == []


async def test_dry_run_replies_would_and_store_tools_still_run(make: Any) -> None:
    h: Harness = make(FakeModel([tc("restart_ollama")]), dry_run=True)
    ops = h.add_task(direct("restart_ollama"), kind="command")
    note = h.add_task(direct("add_note", text="milk"), kind="command")
    inferred = h.add_task(chat("restart_ollama"))
    await h.loop.run_until_idle()
    assert sorted(h.channel.replies()) == [
        "Dry run: would restart ollama",
        "Dry run: would restart ollama",
        'Saved note 1: "milk"',
    ]
    assert h.tool_log == ["add_note"]
    assert (h.task(ops).result or {})["path"] == "dry_run"
    assert h.task(note).status == h.task(inferred).status == "done"


async def test_history_skips_messages_that_started_no_task(make: Any) -> None:
    h: Harness = make(FakeModel(final={"reply": "ok"}))
    with transaction(h.db):  # an approval reply: routed to the gate, started no task
        repo.insert_event(
            h.db,
            Event(source="imessage", type="message", principal=OWNER, payload={"text": "yes"}),
        )
    h.add_task(chat(), text="what now?")
    await h.loop.run_until_idle()
    users = [m["content"] for m in h.model.json_calls[0] if m["role"] == "user"]
    assert not any(re.search(r"\byes\b", u) for u in users)


# --- templates ---------------------------------------------------------------------------


def _intent(spec: ToolSpec, **args: Any) -> Intent:
    return Intent.for_tool(spec, task_id="t", principal=OWNER, args=args)


def test_refusal_and_dry_run_templates() -> None:
    restart = _intent(RESTART_RESONANT)
    assert refusal_reply(restart, Deny(NOT_APPROVED)) == "Not done."
    assert refusal_reply(restart, Deny(APPROVAL_EXPIRED)) == (
        "Not done: the approval for restart Resonant expired."
    )
    assert refusal_reply(restart, Deny(APPROVAL_USED)) == (
        "Not done: that approval was already used."
    )
    assert dry_run_reply(DryRun(would="restart ollama")) == "Dry run: would restart ollama"


def test_report_lines() -> None:
    add, delete, clear = notes.ADD_NOTE, notes.DELETE_NOTE, notes.CLEAR_NOTES
    assert report_line(_intent(add, text="x"), {"id": 3, "text": "x" * 80}) == (
        f'Saved note 3: "{"x" * 59}…"'
    )
    assert report_line(_intent(delete, id=3), {"id": 3}) == "Deleted note 3."
    assert report_line(_intent(clear), {"cleared": 1}) == "Cleared 1 note."
    assert report_line(_intent(add, text="x"), {}) == 'Done: add note text="x"'
    ext = ToolSpec(
        name="add_note", effect=Effect.WRITE, level=Level.L3, source="ext:x", extension="x"
    )
    assert report_line(_intent(ext), {"id": 1, "text": "y"}) == "Done: x: add note"


def test_reminder_report_lines_carry_the_due_time() -> None:
    add, cancel = reminders.ADD_REMINDER, reminders.CANCEL_REMINDER
    data = {"id": 2, "task_id": "t", "at": "x", "due": "3:20pm", "text": "stretch"}
    assert report_line(_intent(add, at="x", text="stretch"), data) == (
        '⏰ Reminder 2 set for 3:20pm: "stretch"'
    )
    assert report_line(_intent(cancel, id=2), {"cancelled": True, "id": 2}) == (
        "Cancelled reminder 2."
    )
    assert report_line(_intent(cancel, id=2), {}) == "Done: cancel reminder id=2"


def test_prompt_text_and_dedupe_key() -> None:
    def prompt(effect: Effect, level: Level, require_code: bool) -> ApprovalPrompt:
        return ApprovalPrompt("ap1", "D7", "do it", effect.value, level.value, T0, require_code)

    assert prompt(Effect.WRITE, Level.L2, False).text() == "[D7] do it? Reply yes / no"
    assert prompt(Effect.WRITE, Level.L1, True).text() == (
        "[D7] do it? This can't be undone. Reply yes D7 / no"
    )
    assert prompt(Effect.PAY, Level.L1, True).text() == (
        "[D7] do it? Reply exactly: yes D7 / no D7"
    )
    assert approval_dedupe_key("ap1", OWNER) == "approval:ap1:prompt:justin"


# --- ops (P2-14): the reply goes out before the detached restart -------------------------


def real_restart(h: Harness, runner: FakeOpsRunner) -> ToolHandler:
    ops = Ops(h.db, load_settings(), runner, owner=OWNER, clock=h.clock, uid=501)

    async def handler(_args: dict[str, Any]) -> dict[str, Any]:
        return await ops.restart_resonant()

    return handler


async def test_restart_reply_is_recorded_before_the_helper_fires(make: Any) -> None:
    runner = FakeOpsRunner()
    h: Harness = make()
    h.executor.handlers[(None, "restart_resonant")] = real_restart(h, runner)
    task_id = h.add_task(direct("restart_resonant"), text="/restart resonant", kind="command")
    await h.loop.run_until_idle()

    # The helper was spawned with a delay (it kickstarts 3 s later), and within the same
    # step the reply was saved to the checkpoint and sent.
    assert runner.spawned == [tuple(restart_later_argv(DAEMON_LABEL, RESTART_DELAY_S))]
    assert RESTART_DELAY_S == 3
    t = h.task(task_id)
    assert t.status == "done" and t.checkpoint["reply"] == "Restarting Resonant…"
    assert h.channel.replies() == ["Restarting Resonant…"]
    assert restart_pending(h.db) is not None  # the next start says "back up"


async def test_replayed_restart_does_not_restart_twice(make: Any) -> None:
    runner = FakeOpsRunner()
    h: Harness = make()
    h.executor.handlers[(None, "restart_resonant")] = real_restart(h, runner)
    task_id = h.add_task(direct("restart_resonant"), text="/restart resonant", kind="command")
    await h.loop.run_until_idle()
    assert len(runner.spawned) == 1

    # The kill landed after the tool ran but before the reply was saved: the new daemon
    # requeues the task with only the pinned call in its checkpoint.
    cp = {k: v for k, v in h.task(task_id).checkpoint.items() if k not in ("reply", "reply_meta")}
    with transaction(h.db):
        h.db.execute(
            "UPDATE tasks SET status = 'queued', checkpoint = ? WHERE id = ?",
            (json.dumps(cp), task_id),
        )
    await h.loop.run_until_idle()

    assert len(runner.spawned) == 1  # replayed from executions: no second restart
    assert h.executions(task_id) == 1
    assert h.task(task_id).checkpoint["reply"] == "Restarting Resonant…"
    assert h.channel.replies() == ["Restarting Resonant…"]  # deduped by reply:<task id>


# --- extension approvers: send to every reachable approver ------------------------------

MEMBER = "cofounder"
ROTATE = ToolSpec(
    name="rotate", effect=Effect.WRITE, level=Level.L2, source="ext:dori", extension="dori"
)


def team() -> Principals:
    return Principals.model_validate(
        {
            "principals": {
                OWNER: {"role": "owner", "identities": ["imessage:+15550000000"]},
                MEMBER: {
                    "identities": ["imessage:+15550000001"],
                    "grants": {"dori": ["request", "approve"]},
                },
                "viewer": {
                    "identities": ["imessage:+15550000002"],
                    "grants": {"dori": ["request"]},
                },
            }
        }
    )


def make_team(make: Any) -> Harness:
    def manifest_approvers(_ext: str) -> list[str]:
        # A granted member, a member without approve, and an unknown name.
        return [MEMBER, "viewer", "ghost"]

    return make(principals=team(), approvers_for=manifest_approvers, extra_specs=(ROTATE,))


def rotate_call() -> dict[str, Any]:
    call: dict[str, Any] = {
        "tool": "rotate",
        "extension": "dori",
        "args": {},
        "step_no": 0,
        "call_index": 0,
    }
    return {"call": call}


def unreachable_audits(h: Harness, task_id: str) -> list[str]:
    rows = h.db.execute(
        "SELECT requested_by FROM audit_log WHERE action = 'approval.unreachable' "
        "AND task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    return [r["requested_by"] for r in rows]


async def test_extension_approval_goes_to_every_granted_approver(make: Any) -> None:
    h = make_team(make)
    task_id = h.add_task(rotate_call())
    await h.loop.run_until_idle()

    assert h.task(task_id).status == "waiting"
    (approval,) = h.approvals(task_id)
    assert approval.approvers == (OWNER, MEMBER)  # viewer and ghost dropped
    assert [p for p, _ in h.channel.prompts] == [OWNER, MEMBER]
    assert {r.principal for r in approval.channel_refs} == {OWNER, MEMBER}
    assert unreachable_audits(h, task_id) == []


async def test_member_approver_can_approve(make: Any) -> None:
    h = make_team(make)
    task_id = h.add_task(rotate_call())
    await h.loop.run_until_idle()
    (approval,) = h.approvals(task_id)
    assert [p.approval_id for p in h.gate.pending_for(MEMBER)] == [approval.id]
    assert h.gate.pending_for("viewer") == ()

    result = h.gate.resolve(ApprovalResolution(approval.id, True, MEMBER, "cli", "cli"))
    assert result.ok, result.reason
    assert h.task(task_id).status == "queued"


async def test_one_unreachable_approver_is_skipped(make: Any) -> None:
    h = make_team(make)
    h.channel.unreachable.add(OWNER)
    task_id = h.add_task(rotate_call())
    await h.loop.run_until_idle()

    assert h.task(task_id).status == "waiting"
    assert [p for p, _ in h.channel.prompts] == [MEMBER]
    (approval,) = h.approvals(task_id)
    assert [r.principal for r in approval.channel_refs] == [MEMBER]
    assert unreachable_audits(h, task_id) == [OWNER]


async def test_no_reachable_approver_fails_for_good(make: Any) -> None:
    h = make_team(make)
    h.channel.unreachable.update({OWNER, MEMBER})
    task_id = h.add_task(rotate_call())
    await h.loop.run_until_idle()

    t = h.task(task_id)
    assert t.status == "failed" and t.error is not None and "no approver" in t.error
    assert h.channel.prompts == []
    assert unreachable_audits(h, task_id) == [OWNER, MEMBER]


async def test_transient_failure_still_prompts_the_others_then_retries(make: Any) -> None:
    h = make_team(make)
    h.channel.fail_prompt.append(ChannelSendError("Messages is not running"))  # the owner's
    task_id = h.add_task(rotate_call())
    await h.loop.run_until_idle()

    t = h.task(task_id)
    assert t.status == "queued" and t.error is not None and "approval prompt" in t.error
    assert [p for p, _ in h.channel.prompts] == [MEMBER]

    h.skip_backoff(task_id)
    await h.loop.run_until_idle()
    assert h.task(task_id).status == "waiting"
    assert [p for p, _ in h.channel.prompts] == [MEMBER, OWNER]  # the member's deduped
    assert len(h.approvals(task_id)) == 1


async def test_builtin_approvals_stay_owner_only(make: Any) -> None:
    calls: list[str] = []

    def approvers_for(ext: str) -> list[str]:
        calls.append(ext)
        return [MEMBER]

    h: Harness = make(
        FakeModel([tc("restart_resonant")]), principals=team(), approvers_for=approvers_for
    )
    task_id = h.add_task(chat("restart_resonant"))
    await h.loop.run_until_idle()
    (approval,) = h.approvals(task_id)
    assert approval.approvers == (OWNER,)
    assert [p for p, _ in h.channel.prompts] == [OWNER]
    assert calls == []  # a builtin never consults a manifest

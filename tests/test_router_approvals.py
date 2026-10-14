"""Router approval inbox (P2-12): text replies bind approvals through the real PolicyGate,
the woken LocalRunner confirms, and ``/approvals [code]`` lists them. In process, with a
fake channel and clock; nothing is sent for real."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest

from resonant.config import LoopConfig
from resonant.executor import Executor, HandlerKey, ToolHandler
from resonant.gate import PendingApproval, PolicyGate
from resonant.gate.replies import parse_reply
from resonant.gateway.channel import (
    ApprovalPrompt,
    MessageRef,
    SelfTestResult,
    Submit,
    approval_dedupe_key,
)
from resonant.loop import Loop, NewTask, SlotPool
from resonant.loop.types import PRIORITY_OWNER
from resonant.models import ChatMessage, ModelReply, ProbeResult, SpanFactory, ToolCall
from resonant.principals import Principal, Principals
from resonant.router import Router
from resonant.router.approvals import NOTHING_WAITING, ask_codes, refusal_reply
from resonant.router.commands import approvals_args
from resonant.router.fast_path import KILL_REPLY, NOT_ALLOWED
from resonant.router.router import APPROVAL_TITLE
from resonant.runners.local import LocalRunner
from resonant.store import approvals as ap
from resonant.store import tasks as repo
from resonant.store.db import kv_get, kv_set, transaction
from resonant.tools import ToolRegistry, notes
from resonant_sdk import Effect, Event, Level, ToolSpec

OWNER, MEMBER = "justin", "cofounder"
OWNER_HANDLE, MEMBER_HANDLE = "+15550000000", "+15557654321"
T0 = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
TTL_S = 1800.0
RESTART = ToolSpec(name="restart_resonant", effect=Effect.WRITE, level=Level.L2)
RESTART_OLLAMA = ToolSpec(name="restart_ollama", effect=Effect.WRITE, level=Level.L2)


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


class Model:
    """The labeler always says ``question``; ``chat_tools`` replays scripted tool calls."""

    def __init__(self) -> None:
        self.tool_replies: list[list[ToolCall]] = []
        self.label_calls = 0

    async def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        schema: dict[str, Any],
        max_tokens: int = 512,
        temperature: float = 0.0,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        self.label_calls += 1
        out = {"intent": "question", "confidence": 0.9}
        return ModelReply(json.dumps(out), [], out, 5, 10, 3)

    async def chat_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        max_tokens: int = 512,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        out = self.tool_replies.pop(0) if self.tool_replies else []
        return ModelReply(None, out, None, 5, 10, 3)

    async def probe(self) -> ProbeResult:
        raise AssertionError("never probed")


@dataclass
class FakeChannel:
    """Records sends and prompts, deduping by key like the real adapter."""

    name: str = "imessage"
    sent: list[tuple[str, str, str | None]] = field(
        default_factory=list[tuple[str, str, str | None]]
    )
    refs: dict[str, MessageRef] = field(default_factory=dict[str, MessageRef])
    on_prompt: Callable[[], Awaitable[None]] | None = None

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
        if dedupe_key is not None and dedupe_key in self.refs:
            return self.refs[dedupe_key]
        self.sent.append((principal, text, dedupe_key))
        ref = MessageRef("imessage", f"imessage-out:{len(self.sent)}")
        if dedupe_key is not None:
            self.refs[dedupe_key] = ref
        return ref

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        if self.on_prompt is not None:
            await self.on_prompt()
        key = approval_dedupe_key(prompt.approval_id, principal)
        return await self.send(principal, prompt.text(), dedupe_key=key)

    def prompts(self) -> list[str]:
        return [t for _, t, k in self.sent if k is not None and k.startswith("approval:")]

    def replies(self) -> list[str]:
        return [t for _, t, k in self.sent if k is not None and k.startswith("reply:")]

    async def self_test(self) -> SelfTestResult:
        return SelfTestResult(True, "fake")


@dataclass
class Harness:
    db: sqlite3.Connection
    clock: Clock
    model: Model
    channel: FakeChannel
    gate: PolicyGate
    router: Router
    loop: Loop
    tool_log: list[str]
    n_events: int = 0

    def add_task(self, checkpoint: dict[str, Any], kind: str = "chat") -> str:
        event = Event(
            source="imessage",
            type="message",
            principal=OWNER,
            payload={"text": "please do it", "guid": "G0", "handle": OWNER_HANDLE},
            occurred_at=self.clock.now,
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

    async def ask(self, tool: str = "restart_resonant") -> tuple[str, ap.Approval]:
        """A chat task whose model calls ``tool``: it prompts and waits."""
        self.clock.now += timedelta(seconds=1)  # approvals are ordered by creation time
        self.model.tool_replies.append([ToolCall(tool, {})])
        task_id = self.add_task(
            {"intent": "question", "confidence": 0.9, "toolset": [tool], "fallback": False}
        )
        await self.loop.run_until_idle()
        (approval,) = self.approvals(task_id)
        return task_id, approval

    def message(
        self,
        text: str,
        *,
        principal: str = OWNER,
        handle: str = OWNER_HANDLE,
        truncated: bool = False,
    ) -> Event:
        """An inbound iMessage sent a few seconds from now, after any prompt so far."""
        self.n_events += 1
        self.clock.now += timedelta(seconds=5)
        at = self.clock.now
        self.clock.now += timedelta(seconds=1)  # routed just after it arrives
        guid = f"G{self.n_events}"
        return Event(
            source="imessage",
            type="message",
            principal=principal,
            payload={
                "text": text,
                "guid": guid,
                "handle": handle,
                "chat_guid": f"iMessage;-;{handle}",
                "at": at.isoformat(),
                "truncated": truncated,
            },
            dedupe_key=f"imessage:{guid}",
            occurred_at=at,
        )

    async def say(self, text: str, **kw: Any) -> Event:
        """Deliver a message through the loop (route, then run whatever it starts)."""
        event = self.message(text, **kw)
        assert self.loop.submit(event)
        await self.loop.run_until_idle()
        return event

    def approvals(self, task_id: str) -> list[ap.Approval]:
        rows = self.db.execute(
            "SELECT id FROM approvals WHERE task_id = ? ORDER BY created_at, id", (task_id,)
        ).fetchall()
        return [a for r in rows if (a := ap.get_approval(self.db, r["id"])) is not None]

    def task_status(self, task_id: str) -> str:
        task = repo.get_task(self.db, task_id)
        assert task is not None
        return task.status

    def status(self, approval: ap.Approval) -> str:
        row = ap.get_approval(self.db, approval.id)
        assert row is not None
        return row.status

    def tasks(self, kind: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT checkpoint FROM tasks WHERE kind = ? ORDER BY created_at", (kind,)
        ).fetchall()
        return [json.loads(r["checkpoint"]) for r in rows]

    def resolve_audits(self) -> int:
        q = "SELECT COUNT(*) FROM audit_log WHERE action = 'approval.resolve'"
        return int(self.db.execute(q).fetchone()[0])


@pytest.fixture
def make(db: sqlite3.Connection) -> Callable[[], Harness]:
    def _make() -> Harness:
        clock = Clock()
        principals = Principals(
            principals={
                OWNER: Principal(role="owner", identities=(f"imessage:{OWNER_HANDLE}",)),
                MEMBER: Principal(identities=(f"imessage:{MEMBER_HANDLE}",)),
            }
        )
        registry = ToolRegistry()
        for spec in (*notes.SPECS, RESTART, RESTART_OLLAMA):
            registry.register(spec)
        tool_log: list[str] = []

        def logged(name: str, fn: Callable[[dict[str, Any]], dict[str, Any]]) -> ToolHandler:
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                tool_log.append(name)
                return fn(args)

            return handler

        handlers: dict[HandlerKey, ToolHandler] = {
            (None, "add_note"): logged("add_note", partial(notes.add_note, db)),
            (None, "clear_notes"): logged("clear_notes", lambda _a: notes.clear_notes(db)),
            (None, "list_notes"): logged("list_notes", lambda _a: notes.list_notes(db)),
            (None, "restart_resonant"): logged("restart_resonant", lambda _a: {}),
            (None, "restart_ollama"): logged("restart_ollama", lambda _a: {}),
        }
        with transaction(db):
            kv_set(db, "autonomy", "on")
        gate = PolicyGate(
            db,
            principals,
            registry,
            dry_run=False,
            approval_ttl_s=TTL_S,
            actions={(None, "clear_notes"): partial(notes.summary_action, db)},
            clock=clock,
        )
        model = Model()
        channel = FakeChannel()
        runner = LocalRunner(
            db,
            model=model,
            channel=channel,
            gate=gate,
            executor=Executor(db, gate, handlers),
            registry=registry,
            now=clock,
        )
        router = Router(
            db,
            principals=principals,
            registry=registry,
            gate=gate,
            handlers=handlers,
            model=model,
            clock=clock,
        )
        loop = Loop(
            db,
            {"local": runner},
            LoopConfig(tick_s=0.01, stuck_after_s=60, max_attempts=3),
            SlotPool(5, 1),
            router=router.route,
            clock=clock,
        )
        return Harness(db, clock, model, channel, gate, router, loop, tool_log)

    return _make


# --- approve and deny end to end ---------------------------------------------------------


async def test_plain_yes_with_one_pending_resolves_wakes_and_runs_once(
    make: Callable[[], Harness],
) -> None:
    h = make()
    task_id, approval = await h.ask()
    assert h.channel.prompts() == [f"[{approval.code}] restart resonant? Reply yes / no"]

    await h.say("Yes!")
    assert h.tool_log == ["restart_resonant"]
    assert h.channel.replies() == ["Done: restart resonant"]
    assert h.tasks("reply") == []  # the reply started no task of its own
    assert h.task_status(task_id) == "done"
    assert h.model.label_calls == 0  # never reached the labeler


async def test_plain_no_denies(make: Callable[[], Harness]) -> None:
    h = make()
    _, approval = await h.ask()
    await h.say("no")
    assert h.status(approval) == "rejected"
    assert h.tool_log == []
    assert h.channel.replies() == ["Not done."]


async def test_two_pending_re_asks_with_codes_then_the_code_binds(
    make: Callable[[], Harness],
) -> None:
    h = make()
    _, first = await h.ask("restart_resonant")
    _, second = await h.ask("restart_ollama")

    await h.say("yes")
    assert h.tool_log == []
    (re_ask,) = h.channel.replies()
    assert re_ask == (
        f"Which one? {first.code} (restart resonant), {second.code} (restart ollama). "
        "Reply yes <code> or no <code>."
    )
    assert [c["title"] for c in h.tasks("reply")] == [APPROVAL_TITLE]

    await h.say(f"yes {second.code}")
    assert h.tool_log == ["restart_ollama"]
    assert h.status(first) == "pending" and h.status(second) == "approved"
    assert h.channel.replies()[-1] == "Done: restart ollama"


async def test_l1_needs_the_code(make: Callable[[], Harness]) -> None:
    h = make()
    for text in ("a", "b"):
        notes.add_note(h.db, {"text": text})
    task_id = h.add_task({"direct_call": {"tool": "clear_notes", "args": {}}}, kind="command")
    await h.loop.run_until_idle()
    (approval,) = h.approvals(task_id)
    code = approval.code
    assert h.channel.prompts() == [
        f"[{code}] delete all 2 notes? This can't be undone. Reply yes {code} / no"
    ]

    await h.say("yes")
    assert h.channel.replies() == [f"Reply yes {code} to confirm."]
    assert h.status(approval) == "pending" and h.tool_log == []

    await h.say(f"yes {code}")
    assert h.tool_log == ["clear_notes"]
    assert h.channel.replies()[-1] == "Cleared 2 notes."


async def test_kill_with_pending_approvals_still_kills(make: Callable[[], Harness]) -> None:
    h = make()
    _, approval = await h.ask()
    await h.say("/kill")
    assert kv_get(h.db, "autonomy") == "off"
    assert h.channel.replies() == [KILL_REPLY]
    assert h.status(approval) == "pending"


async def test_yes_with_nothing_pending(make: Callable[[], Harness]) -> None:
    h = make()
    await h.say("yes")
    assert h.channel.replies() == [NOTHING_WAITING]
    assert h.model.label_calls == 0


async def test_non_approver_gets_the_generic_reply(make: Callable[[], Harness]) -> None:
    h = make()
    _, approval = await h.ask()
    await h.say(f"yes {approval.code}", principal=MEMBER, handle=MEMBER_HANDLE)
    await h.say("yes", principal=MEMBER, handle=MEMBER_HANDLE)
    assert [t for p, t, _ in h.channel.sent if p == MEMBER] == [NOTHING_WAITING] * 2
    assert h.status(approval) == "pending" and h.tool_log == []


async def test_identity_must_match_the_principal(make: Callable[[], Harness]) -> None:
    """The gate trusts ``reply.actor``: an event whose handle maps to someone else never
    reaches it."""
    h = make()
    _, approval = await h.ask()
    await h.say("yes", principal=OWNER, handle=MEMBER_HANDLE)
    assert h.channel.replies() == [NOTHING_WAITING]
    assert h.status(approval) == "pending" and h.resolve_audits() == 0


async def test_pasted_prompt_is_not_a_reply_and_routes_normally(
    make: Callable[[], Harness],
) -> None:
    h = make()
    _, approval = await h.ask()
    prompt = h.channel.prompts()[0]
    task = await h.router.route(h.message(prompt))
    assert task is not None and task.kind == "chat"
    assert h.model.label_calls == 1
    assert h.status(approval) == "pending" and h.resolve_audits() == 0


@pytest.mark.parametrize("text", ["yes please", "ok", "y", "sure", "yes?", "status update"])
async def test_unparsed_text_routes_normally(make: Callable[[], Harness], text: str) -> None:
    h = make()
    _, approval = await h.ask()
    task = await h.router.route(h.message(text))
    assert task is not None and task.kind == "chat"
    assert h.status(approval) == "pending"


async def test_keywords_still_work(make: Callable[[], Harness]) -> None:
    h = make()
    task = await h.router.route(h.message("ping"))
    assert task is not None and task.kind in ("reply", "chat")


async def test_truncated_payload_is_never_a_reply(make: Callable[[], Harness]) -> None:
    h = make()
    _, approval = await h.ask()
    task = await h.router.route(h.message("yes", truncated=True))
    assert task is not None and task.kind == "chat"
    assert h.status(approval) == "pending" and h.resolve_audits() == 0


async def test_same_event_twice_resolves_once_and_replies_nothing(
    make: Callable[[], Harness],
) -> None:
    h = make()
    _, approval = await h.ask()
    event = h.message("yes")
    assert await h.router.route(event) is None
    assert await h.router.route(event) is None  # a router retry
    assert h.status(approval) == "approved"
    assert h.resolve_audits() == 1
    rows = h.db.execute(
        "SELECT attrs FROM spans WHERE name = 'router.approval' ORDER BY rowid"
    ).fetchall()
    attrs = [json.loads(r["attrs"]) for r in rows]
    assert [(a["outcome"], a["path"], a["reason"]) for a in attrs] == [
        ("resolved", "parser", None),
        ("replay", "parser", None),
    ]
    assert "yes" not in json.dumps(attrs)  # never the text


async def test_yes_while_the_task_is_running_is_caught_by_the_wait_requeue(
    make: Callable[[], Harness],
) -> None:
    """A reply that lands mid-step (here: during a re-prompt) wakes the task when its step
    returns ``Wait``."""
    h = make()
    task_id, approval = await h.ask()
    replied: list[NewTask | None] = []

    async def reply_now() -> None:
        assert h.task_status(task_id) == "running"
        replied.append(await h.router.route(h.message("yes")))

    h.channel.on_prompt = reply_now
    h.loop.submit(Event(source="test", type="nudge", task_id=task_id))  # a spurious wake
    await h.loop.run_until_idle()
    h.channel.on_prompt = None
    await h.loop.run_until_idle()
    assert replied == [None]
    assert h.status(approval) == "approved"
    assert h.tool_log == ["restart_resonant"]
    assert h.channel.replies() == ["Done: restart resonant"]


# --- /approvals --------------------------------------------------------------------------


async def test_approvals_lists_and_shows_full_args_to_the_owner(
    make: Callable[[], Harness],
) -> None:
    h = make()
    h.model.tool_replies.append([ToolCall("add_note", {"text": "vitamin d3 and zinc"})])
    with transaction(h.db):
        kv_set(h.db, "autonomy", "off")  # so an L3 call asks
    task_id = h.add_task(
        {"intent": "question", "confidence": 0.9, "toolset": ["add_note"], "fallback": False}
    )
    await h.loop.run_until_idle()
    (approval,) = h.approvals(task_id)
    code = approval.code
    assert code is not None

    await h.say("/approvals")
    assert h.channel.replies()[-1] == (
        "Waiting for approval (1):\n"
        f'- {code} · add note text="vitamin ? and zinc" · expires in 29m\n'
        "Reply yes <code> or no <code>. /approvals <code> shows the details."
    )
    await h.say(f"/approvals {code.lower()}")
    assert h.channel.replies()[-1] == (
        f'[{code}] add note text="vitamin ? and zinc"\n'
        "Tool: add_note (L3, write)\n"
        'Args: {\n "text": "vitamin d3 and zinc"\n}\n'
        f"Expires in 29m. Reply yes / no {code}"
    )
    await h.say("/approvals Q9")
    assert h.channel.replies()[-1] == "Nothing is waiting with code Q9."
    await h.say("/approvals 12")
    assert h.channel.replies()[-1] == "that's not an approval code. Usage: /approvals [code]"

    await h.say("/approvals", principal=MEMBER, handle=MEMBER_HANDLE)
    await h.say(f"/approvals {code}", principal=MEMBER, handle=MEMBER_HANDLE)
    member = [t for p, t, _ in h.channel.sent if p == MEMBER]
    assert member == [NOT_ALLOWED, NOT_ALLOWED]
    assert h.status(approval) == "pending"


async def test_approvals_with_nothing_pending(make: Callable[[], Harness]) -> None:
    h = make()
    await h.say("/approvals")
    assert h.channel.replies() == [NOTHING_WAITING]


# --- templates and parsing ---------------------------------------------------------------


def _pending(code: str, summary: str, *, require_code: bool = False) -> PendingApproval:
    return PendingApproval(
        f"id-{code}", code, "t", summary, Effect.WRITE, Level.L2, require_code, T0
    )


def test_refusal_replies() -> None:
    a7, d3 = _pending("A7", "restart resonant"), _pending("D3", "wipe", require_code=True)
    assert refusal_reply("nothing_pending", None, [a7]) == NOTHING_WAITING
    assert refusal_reply("unsupported", None, []) == NOTHING_WAITING
    assert refusal_reply("not_found", None, []) == NOTHING_WAITING
    assert refusal_reply("approvals unavailable", None, []) == NOTHING_WAITING  # DryRunGate
    assert refusal_reply("ambiguous", None, [a7, d3]) == ask_codes([a7, d3])
    assert refusal_reply("ambiguous", None, []) == NOTHING_WAITING
    assert refusal_reply("code_required", None, [a7, d3]) == "Reply yes D3 to confirm."
    assert refusal_reply("code_required", None, []) == "Reply yes <code> to confirm."
    assert refusal_reply("unknown_code", "Q9", [a7]) == ("Nothing is waiting with code Q9.")
    for reason in ("expired", "already_resolved", "task_not_waiting", "not_sent_before"):
        assert refusal_reply(reason, "A7", [a7]) != NOTHING_WAITING


def test_approvals_args() -> None:
    assert approvals_args("") == {}
    assert approvals_args(" a7 ") == {"code": "A7"}
    for bad in ("A1", "O7", "all", "A7 B3"):
        with pytest.raises(ValueError):
            approvals_args(bad)


def test_kill_is_never_a_reply() -> None:
    assert parse_reply("/kill") is None

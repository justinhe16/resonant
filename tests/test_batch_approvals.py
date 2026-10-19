"""BA-1: batch approvals at the gate and in the runner's shared batch step.

The real PolicyGate, Executor and LocalRunner (the batch step the brain loop and the Claude
bridge both end in), a fake channel and builtin test tools. The e2e flows (Claude and qwen
through the daemon, a real extension) are in ``test_e2e_batch.py``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_local_runner_gated import Clock, FakeChannel, FakeModel

from resonant.executor import Executor, HandlerKey, ToolFailed, ToolHandler
from resonant.gate import ApprovalReply, ApprovalResolution, NeedsApproval, PolicyGate
from resonant.gate.codes import batch_text
from resonant.loop import NewTask
from resonant.loop.types import Fail, StepOutcome, Task, Wait
from resonant.models.client import null_spans
from resonant.principals import Principal, Principals
from resonant.router.approvals import ApprovalInbox, approvals_reply
from resonant.runners.local import Answer, LocalRunner
from resonant.store import approvals as ap
from resonant.store import tasks as repo
from resonant.store.db import kv_set, transaction
from resonant.toolcore import Asked, BatchItem, CallScope, Refused
from resonant.tools import ToolRegistry
from resonant_sdk import Effect, Event, Level, ToolSpec

OWNER = "justin"
MEMBER = "cofounder"
OWNER_ID = "imessage:+15550000000"
MEMBER_ID = "imessage:+15550000001"
TTL_S = 1800.0

CLOSE = ToolSpec(
    name="close_ticket",
    description="Close a ticket.",
    args_schema={"type": "object", "properties": {"id": {"type": "string"}}},
    effect=Effect.WRITE,
    level=Level.L2,
)
PURGE = ToolSpec(
    name="purge_ticket",
    description="Delete a ticket for good.",
    args_schema={"type": "object", "properties": {"id": {"type": "string"}}},
    effect=Effect.WRITE,
    level=Level.L1,
)
WIDEN = ToolSpec(  # stands in for the builtin of that name (it widens trust)
    name="allow_tools",
    args_schema={"type": "object", "properties": {"id": {"type": "string"}}},
    effect=Effect.WRITE,
    level=Level.L2,
)
EXT_CLOSE = ToolSpec(
    name="close_ticket",
    description="Close a ticket (an extension's).",
    args_schema={"type": "object", "properties": {"id": {"type": "string"}}},
    effect=Effect.WRITE,
    level=Level.L2,
    source="ext:linear",
    extension="linear",
)


class Crash(BaseException):
    """The process dying mid-call: not an Exception, so nothing catches it."""


@dataclass
class Ctx:
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
                self.db, self.task_id, checkpoint=checkpoint, claude_session_id=None,
                worktree_path=None,
            )  # fmt: skip


@dataclass
class H:
    db: sqlite3.Connection
    clock: Clock
    gate: PolicyGate
    runner: LocalRunner
    channel: FakeChannel
    ran: list[str]
    crash_on: set[str]
    fail_on: set[str]

    def task(self, task_id: str) -> Task:
        t = repo.get_task(self.db, task_id)
        assert t is not None
        return t

    def new_task(self, principal: str = OWNER) -> str:
        event = Event(source="imessage", type="message", principal=principal,
                      payload={"text": "close them", "handle": "+15550000000"})  # fmt: skip
        with transaction(self.db):
            repo.insert_event(self.db, event)
            task_id = repo.insert_task(
                self.db,
                NewTask(kind="chat", runner="local", priority=1, principal=principal,
                             checkpoint={}, origin_event_id=event.id, trace_id=event.trace_id),
            )  # fmt: skip
            self.db.execute(
                "UPDATE tasks SET status = 'running', wait_reason = NULL WHERE id = ?", (task_id,)
            )
        return task_id

    async def propose(
        self, task_id: str, calls: list[tuple[ToolSpec, str]], *, origin: str = "inferred"
    ) -> list[BatchItem]:
        """The model proposes ``calls`` in one turn (batch mode, as the brain loop does)."""
        task = self.task(task_id)
        items: list[BatchItem] = []
        for i, (spec, key) in enumerate(calls):
            scope = CallScope(
                task.id, task.principal or OWNER, task.trace_id, task.step_no, i,
                origin,  # pyright: ignore[reportArgumentType]
                checkpoint=self.task(task_id).checkpoint, steps=Ctx(self.db, task_id),
                batch=items,
            )  # fmt: skip
            outcome = await self.runner.core.call(spec, {"id": key}, scope)
            assert isinstance(outcome, Asked) and outcome.item is not None, outcome
            if outcome.item not in items:
                items.append(outcome.item)
        return items

    async def close(self, task_id: str, items: list[BatchItem]) -> Any:
        """The turn ends: one prompt for the batch, and the task waits (as the loop would
        store the ``Wait``)."""
        task = self.task(task_id)
        principal = task.principal or OWNER
        out = await self.runner.close_batch(task, principal, items, [], Ctx(self.db, task_id),
                                            null_spans)  # fmt: skip
        self.settle(task_id, out)
        return out

    async def step(self, task_id: str) -> Any:
        """The task woke (or a crash restarted it): running, then its step again."""
        with transaction(self.db):
            self.db.execute(
                "UPDATE tasks SET status = 'running', wait_reason = NULL WHERE id = ?", (task_id,)
            )
        out = await self.runner.step(self.task(task_id), [], Ctx(self.db, task_id))
        self.settle(task_id, out)
        return out

    def settle(self, task_id: str, out: StepOutcome | Any) -> None:
        if isinstance(out, Wait):
            with transaction(self.db):
                self.db.execute(
                    "UPDATE tasks SET status = 'waiting', wait_reason = 'approval', checkpoint = ?"
                    " WHERE id = ?",
                    (json.dumps(out.checkpoint), task_id),
                )

    def reply(
        self, text: str, *, actor: str = OWNER, identity: str = OWNER_ID, event: str = "e1",
        code: str | None = None, decision: str = "approve", always: bool = False,
    ) -> Any:  # fmt: skip
        return self.gate.resolve_reply(
            ApprovalReply(
                actor=actor, actor_identity=identity, channel="imessage", raw_text=text,
                decision=decision,  # pyright: ignore[reportArgumentType]
                path="parser", event_id=event,
                occurred_at=self.clock.now + timedelta(seconds=1), code=code, always=always,
            )
        )  # fmt: skip

    def code(self) -> str:
        (b,) = self.db.execute("SELECT approval_ids FROM approval_batches").fetchall()
        first = ap.get_approval(self.db, json.loads(b["approval_ids"])[0])
        assert first is not None and first.code is not None
        return first.code


def principals() -> Principals:
    return Principals(
        principals={
            OWNER: Principal(role="owner", identities=(OWNER_ID,)),
            MEMBER: Principal(identities=(MEMBER_ID,), grants={"linear": ("request", "approve")}),
        }
    )


@pytest.fixture
def h(db: sqlite3.Connection) -> H:
    clock = Clock(datetime(2026, 10, 19, 15, 0, tzinfo=UTC))
    registry = ToolRegistry()
    for spec in (CLOSE, PURGE, EXT_CLOSE, WIDEN):
        registry.register(spec)
    ran: list[str] = []
    crash_on: set[str] = set()
    fail_on: set[str] = set()

    def handler(name: str) -> ToolHandler:
        async def run(args: dict[str, Any]) -> dict[str, Any]:
            key = str(args.get("id"))
            if key in crash_on:
                crash_on.discard(key)
                ran.append(f"{name}:{key}:crashed")
                raise Crash
            if key in fail_on:
                raise ToolFailed("locked")
            ran.append(f"{name}:{key}")
            return {"id": key}

        return run

    handlers: dict[HandlerKey, ToolHandler] = {
        (None, "close_ticket"): handler("close"),
        (None, "purge_ticket"): handler("purge"),
        ("linear", "close_ticket"): handler("ext_close"),
        (None, "allow_tools"): handler("widen"),
    }
    with transaction(db):
        kv_set(db, "autonomy", "on")
        kv_set(db, "paused", False)
    gate = PolicyGate(db, principals(), registry, dry_run=False, approval_ttl_s=TTL_S,
                      approvers_for=lambda _e: [OWNER, MEMBER], clock=clock,
                      batch_max=4)  # fmt: skip
    executor = Executor(db, gate, handlers)
    channel = FakeChannel()
    runner = LocalRunner(db, model=FakeModel(), channel=channel, gate=gate, executor=executor,
                         registry=registry, now=clock)  # fmt: skip
    return H(db, clock, gate, runner, channel, ran, crash_on, fail_on)


def mark_sent(h: H, task_id: str) -> None:
    """Prompted: the fake channel's refs are recorded by ``close``; advance past them."""
    h.clock.advance(1)


async def asked_batch(h: H, keys: tuple[str, ...] = ("A", "B", "C")) -> tuple[str, list[str]]:
    task_id = h.new_task()
    items = await h.propose(task_id, [(CLOSE, k) for k in keys])
    wait = await h.close(task_id, items)
    assert isinstance(wait, Wait) and wait.reason == "approval"
    mark_sent(h, task_id)
    return task_id, [i.approval_id for i in items]


# --- the prompt -----------------------------------------------------------------------------


def test_batch_text_lists_each_change_and_takes_the_strictest_answer() -> None:
    lines = ['close ticket id="A"', 'close ticket id="B"']
    assert batch_text("D7", lines) == (
        '[D7] 2 changes, all or none:\n1. close ticket id="A"\n2. close ticket id="B"\n'
        "Reply yes / no"
    )
    assert batch_text("D7", lines, destructive=True).endswith(
        "Some can't be undone. Reply yes D7 / no"
    )
    assert batch_text("D7", lines, require_code=True).endswith("Reply exactly: yes D7 / no D7")
    long = batch_text("D7", ["x" * 300, "y"])
    assert "1. " + "x" * 300 + "\n" in long  # never clipped: yes covers only what shows


async def test_one_prompt_for_the_whole_batch_to_the_owner_only(h: H) -> None:
    task_id, ids = await asked_batch(h)
    (prompt,) = h.channel.prompts
    assert prompt[0] == OWNER  # not the member, though they approve linear's tools
    assert prompt[1].startswith(f"[{h.code()}] 3 changes, all or none:")
    rows = [ap.get_approval(h.db, i) for i in ids]
    assert {r.expires_at for r in rows if r} == {h.clock.now - timedelta(seconds=1) + timedelta(
        seconds=TTL_S)}  # fmt: skip  # one expiry for all of them, from the prompt
    assert all(r is not None and r.sent_at(OWNER) is not None for r in rows)
    # a spurious wake re-prompts the same batch: nothing new is sent, nothing runs
    again = await h.step(task_id)
    assert isinstance(again, Wait) and len(h.channel.prompts) == 1 and h.ran == []
    assert len(h.db.execute("SELECT * FROM approval_batches").fetchall()) == 1


async def test_a_lone_change_keeps_the_plain_prompt(h: H) -> None:
    task_id = h.new_task()
    items = await h.propose(task_id, [(CLOSE, "A")])
    wait = await h.close(task_id, items)
    assert isinstance(wait, Wait)
    (prompt,) = h.channel.prompts
    assert prompt[1].endswith("? Reply yes / no") and "changes" not in prompt[1]
    assert h.db.execute("SELECT * FROM approval_batches").fetchall() == []
    assert "call" in h.task(task_id).checkpoint and "batch" not in h.task(task_id).checkpoint


# --- binding --------------------------------------------------------------------------------


async def test_yes_runs_every_change_once_in_order(h: H) -> None:
    task_id, ids = await asked_batch(h)
    result = h.reply("yes")
    assert result.ok and {r.approval_id for r in result.resolved} == set(ids)
    done = await h.step(task_id)
    assert h.ran == ["close:A", "close:B", "close:C"]
    assert h.channel.replies()[-1].splitlines() == [
        'Done: close ticket id="A"', 'Done: close ticket id="B"', 'Done: close ticket id="C"',
    ]  # fmt: skip
    assert done.result["batch"] == 3 and done.result["batch_ran"] == 3
    # the same reply event again: the same result, nothing new
    assert set(h.reply("yes").resolved) == set(result.resolved)


async def test_a_crash_mid_batch_never_runs_a_change_twice(h: H) -> None:
    task_id, _ = await asked_batch(h, ("A", "B", "C", "D"))
    assert h.reply("yes").ok
    h.crash_on.add("C")
    with pytest.raises(Crash):
        await h.step(task_id)  # A and B ran, C was running when the process died
    assert h.ran == ["close:A", "close:B", "close:C:crashed"]
    await h.step(task_id)  # restarted: A and B replay, C is ambiguous, D runs
    assert h.ran == ["close:A", "close:B", "close:C:crashed", "close:D"]
    lines = h.channel.replies()[-1].splitlines()
    assert lines[0] == 'Done: close ticket id="A"' and lines[1] == 'Done: close ticket id="B"'
    assert lines[2].startswith('Not sure whether close ticket id="C" ran')
    assert lines[3] == 'Done: close ticket id="D"'
    n = h.db.execute("SELECT COUNT(*) FROM executions WHERE task_id = ?", (task_id,)).fetchone()
    assert n[0] == 4


async def test_a_failure_doesnt_stop_the_rest(h: H) -> None:
    task_id, _ = await asked_batch(h)
    h.fail_on.add("B")
    assert h.reply("yes").ok
    await h.step(task_id)
    assert h.ran == ["close:A", "close:C"]
    assert h.channel.replies()[-1].splitlines()[1] == 'Failed: close ticket id="B" (locked)'


async def test_no_and_expiry_run_nothing(h: H) -> None:
    task_id, _ = await asked_batch(h)
    assert h.reply("no", decision="deny").ok
    await h.step(task_id)
    assert h.ran == [] and h.channel.replies()[-1] == "Not done."
    other, _ = await asked_batch(h)
    h.clock.advance(TTL_S + 1)
    await h.step(other)
    assert h.ran == []
    assert h.channel.replies()[-1] == "Not done: the approval for those 3 changes expired."


async def test_plain_yes_with_a_batch_and_a_lone_approval_is_ambiguous(h: H) -> None:
    await asked_batch(h)
    lone = h.new_task()
    items = await h.propose(lone, [(CLOSE, "Z")])
    await h.close(lone, items)
    mark_sent(h, lone)
    result = h.reply("yes")
    assert not result.ok and result.reason == "ambiguous"
    inbox = ApprovalInbox(h.db, principals=principals(), gate=h.gate)
    listed = [p.code for p in inbox.sent_pending(OWNER)]
    assert len(listed) == 2 and h.code() in listed  # the batch is one entry, by its code
    batch = next(p for p in h.gate.pending_for(OWNER) if p.code == h.code())
    assert batch.summary.startswith('3 changes: close ticket id="A"; close ticket id="B"')


async def test_only_the_batch_code_names_it(h: H) -> None:
    _, ids = await asked_batch(h)
    hidden = [ap.get_approval(h.db, i) for i in ids[1:]]
    for row in hidden:
        assert row is not None and row.code is not None
        result = h.reply(f"yes {row.code}", code=row.code, event=f"e-{row.code}")
        assert not result.ok and result.reason == "unknown_code"
    assert h.reply(f"yes {h.code()}", code=h.code()).ok


async def test_yes_always_is_never_offered_for_a_batch(h: H) -> None:
    await asked_batch(h)
    result = h.reply("yes always", always=True)
    assert not result.ok and result.reason == "always_unsupported"


async def test_an_l1_in_the_batch_needs_the_code(h: H) -> None:
    task_id = h.new_task()
    items = await h.propose(task_id, [(CLOSE, "A"), (PURGE, "B")])
    await h.close(task_id, items)
    mark_sent(h, task_id)
    (prompt,) = h.channel.prompts
    assert prompt[1].endswith(f"Reply yes {h.code()} / no")
    result = h.reply("yes")
    assert not result.ok and result.reason == "code_required"
    assert h.reply("no", decision="deny", event="e2").ok  # a plain no may still deny


async def test_a_member_can_never_answer_the_owners_batch(h: H) -> None:
    task_id = h.new_task()
    items = await h.propose(task_id, [(EXT_CLOSE, "A"), (EXT_CLOSE, "B")])
    await h.close(task_id, items)
    mark_sent(h, task_id)
    # the member approves linear's tools, and each approval lists them: not the batch
    rows = [ap.get_approval(h.db, i.approval_id) for i in items]
    assert all(r is not None and MEMBER in r.approvers for r in rows)
    code = h.code()
    for text, c in (("yes", None), (f"yes {code}", code)):
        result = h.reply(text, actor=MEMBER, identity=MEMBER_ID, code=c, event=f"m-{text}")
        assert not result.ok and result.reason == "nothing_pending"
    assert h.gate.pending_for(MEMBER) == ()
    by_id = h.gate.resolve(ApprovalResolution(items[0].approval_id, True, MEMBER, "cli", "cli"))
    assert not by_id.ok and by_id.reason == "not_found"
    assert h.reply("yes", code=None).ok


async def test_resolving_a_member_by_id_resolves_the_whole_batch(h: H) -> None:
    task_id, ids = await asked_batch(h)
    result = h.gate.resolve(ApprovalResolution(ids[1], True, OWNER, "cli", "cli"))
    assert result.ok and {r.approval_id for r in result.resolved} == set(ids)
    await h.step(task_id)
    assert h.ran == ["close:A", "close:B", "close:C"]


async def test_the_kill_switch_blocks_the_batch(h: H) -> None:
    task_id, _ = await asked_batch(h)
    assert h.reply("yes").ok
    with transaction(h.db):
        kv_set(h.db, "paused", True)
    await h.step(task_id)
    assert h.ran == []
    assert h.channel.replies()[-1] == (
        "Not done: Resonant is paused (/kill), so none of the 3 changes ran."
    )


async def test_the_batch_cap(h: H) -> None:
    task_id = h.new_task()
    items = await h.propose(task_id, [(CLOSE, k) for k in "ABCD"])
    assert len(items) == 4
    task = h.task(task_id)
    scope = CallScope(task.id, OWNER, task.trace_id, task.step_no, 9, checkpoint=task.checkpoint,
                      steps=Ctx(h.db, task_id), batch=items)  # fmt: skip
    capped = await h.runner.core.call(CLOSE, {"id": "E"}, scope)
    assert isinstance(capped, Refused) and capped.why == "capped"
    assert capped.text == "Not done: at most 4 changes can wait on one yes."
    assert isinstance(capped.decision, NeedsApproval)
    dropped = ap.get_approval(h.db, capped.decision.approval_id)
    assert dropped is not None and dropped.status == "expired"  # nothing can bind it
    # the same change again is the entry it already has, not a fifth
    again = await h.runner.core.call(CLOSE, {"id": "A"}, scope)
    assert isinstance(again, Asked) and again.item == items[0]


async def test_a_tainted_change_keeps_its_taint_through_the_batch(h: H) -> None:
    task_id = h.new_task()
    items = await h.propose(task_id, [(CLOSE, "A"), (CLOSE, "B")], origin="tainted")
    assert all(i.tainted for i in items)
    await h.close(task_id, items)
    mark_sent(h, task_id)
    assert h.reply("yes").ok
    await h.step(task_id)
    origins = [
        json.loads(r["detail"])["origin"]
        for r in h.db.execute(
            "SELECT detail FROM audit_log WHERE action = 'gate.evaluate' ORDER BY id"
        ).fetchall()
    ]
    assert set(origins) == {"tainted"}


async def test_a_model_chosen_arg_cant_shorten_a_batch_line(h: H) -> None:
    """Review of BA-1: the ``yes always`` note is cut only as the summary's own suffix."""
    task_id = h.new_task()
    items = await h.propose(task_id, [(CLOSE, "x (yes always = "), (CLOSE, "B")])
    row = ap.get_approval(h.db, items[0].approval_id)
    assert row is not None and items[0].summary == row.summary
    await h.close(task_id, items)
    (prompt,) = h.channel.prompts
    assert f"1. {row.summary}\n" in prompt[1]


async def test_a_pinned_change_not_yet_asked_never_makes_a_plain_yes_ambiguous(h: H) -> None:
    """Review of BA-1: a turn still running holds pending approvals nobody was prompted for."""
    lone = h.new_task()
    items = await h.propose(lone, [(CLOSE, "Z")])
    await h.close(lone, items)
    mark_sent(h, lone)
    running = h.new_task()
    await h.propose(running, [(CLOSE, "A"), (CLOSE, "B")])  # pinned, the turn goes on
    assert h.reply("yes").ok
    row = ap.get_approval(h.db, items[0].approval_id)
    assert h.ran == [] and row is not None and row.status == "approved"


async def test_a_batch_that_widens_trust_needs_the_code_and_says_so(h: H) -> None:
    task_id = h.new_task()
    items = await h.propose(task_id, [(CLOSE, "A"), (WIDEN, "x")])
    assert items[1].summary.startswith("widens trust: ")
    await h.close(task_id, items)
    mark_sent(h, task_id)
    (prompt,) = h.channel.prompts
    assert "2. widens trust: " in prompt[1]
    assert prompt[1].endswith(f"Reply exactly: yes {h.code()} / no {h.code()}")
    result = h.reply("yes")
    assert not result.ok and result.reason == "code_required"


def test_open_batch_refuses_what_isnt_one_task_s_pending_approvals(h: H) -> None:
    with pytest.raises(ValueError, match="at least two"):
        h.gate.open_batch(OWNER, ["x"])
    with pytest.raises(ValueError, match="owner"):
        h.gate.open_batch(MEMBER, ["x", "y"])
    with pytest.raises(ValueError, match="pending"):
        h.gate.open_batch(OWNER, ["x", "y"])
    with pytest.raises(ValueError, match="at most 4"):
        h.gate.open_batch(OWNER, list("abcde"))


async def test_approvals_detail_for_a_batch_shows_every_change(h: H) -> None:
    await asked_batch(h)
    pending = h.gate.pending_for(OWNER)
    text = approvals_reply(h.db, pending, h.code(), is_owner=True, now=h.clock.now)
    assert text is not None
    assert text.startswith(f"[{h.code()}] 3 changes, all or none:")
    assert '1. close_ticket (L2): {"id": "A"}' in text
    assert text.endswith(f"Reply yes / no {h.code()}")


async def test_the_brain_never_gets_a_wait_for_an_asked_change(h: H) -> None:
    """``_call_tool`` with a batch: the asked change comes back to the model as data."""
    task_id = h.new_task()
    task = h.task(task_id)
    call: Callable[..., Any] = h.runner._call_tool  # pyright: ignore[reportPrivateUsage]
    batch: list[BatchItem] = []
    got = await call(task, OWNER, [CLOSE], "close_ticket", {"id": "A"}, 0,
                     Ctx(h.db, task_id), null_spans, [], batch=batch)  # fmt: skip
    assert not isinstance(got, Wait | Fail | Answer)
    assert got.asked is not None and got.data["status"] == "asked the owner"
    row = ap.get_approval(h.db, got.asked.approval_id)
    assert row is not None and row.code is not None and row.code not in json.dumps(got.data)
    assert h.channel.prompts == []  # nothing prompted mid-turn


# --- allow_tools: what "don't ask for linear tickets" covers ------------------------------------


def _ext_registry() -> tuple[ToolRegistry, dict[str, list[ToolSpec]]]:
    def ext(name: str, effect: Effect, level: Level | None = None) -> ToolSpec:
        return ToolSpec(name=name, effect=effect, level=level, source="ext:linear",
                        extension="linear")  # fmt: skip

    linear = [
        ext("list_issues", Effect.READ),
        ext("save_issue", Effect.WRITE, Level.L2),
        ext("save_comment", Effect.WRITE, Level.L2),
        ext("delete_issue", Effect.WRITE, Level.L1),
    ]
    registry = ToolRegistry()
    for spec in (*linear, CLOSE, PURGE):
        registry.register(spec)
    return registry, {"linear": linear}


def test_allow_targets_maps_names_to_the_changes_a_rule_can_cover() -> None:
    from resonant.trust.tools import allow_targets, tool_name

    registry, loaded = _ext_registry()

    def targets(*names: str) -> tuple[list[str], str | None]:
        specs, why = allow_targets(names, registry=registry, ext_tools=lambda e: loaded.get(e, []))
        return [tool_name(s) for s in specs], why

    # an extension: its L2 changes (never its reads or its L1 delete)
    assert targets("linear") == (["linear.save_issue", "linear.save_comment"], None)
    assert targets("Linear ") == (["linear.save_issue", "linear.save_comment"], None)
    assert targets("linear.save_comment") == (["linear.save_comment"], None)
    assert targets("mcp__resonant__save_issue") == (["linear.save_issue"], None)  # Claude's name
    assert targets("close_ticket", "linear") == (
        ["close_ticket", "linear.save_issue", "linear.save_comment"], None
    )  # fmt: skip
    for bad in ("linear.delete_issue", "purge_ticket", "linear.list_issues", "jira", "x.y"):
        found, why = targets(bad)
        assert found == [] and why is not None, bad

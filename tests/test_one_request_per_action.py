"""PER-269: one request per action.

A run's merge check, the brain's ``merge_pr`` and a job each could open their own approval for
one PR merge (mycelium PR #2: Y6 from R8's check, M6 from the brain). Now an action has one
identity (``gate.actions``), a second path points to the open request instead of asking again,
and when the PR is merged or closed the open asks for it expire as ``obsolete``. The real
PolicyGate, TrustChecks, Executor and ToolCore over the fake channel of
``test_trust_checks.py``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from typing import Any

import pytest
from test_trust_checks import HANDLE, NOW, OWNER, TTL_S, Clock
from test_trust_checks import Channel as PromptChannel

from resonant.executor import Executor, HandlerKey, ToolHandler
from resonant.extensions.scheduler import persist_events
from resonant.gate import PolicyGate
from resonant.gate import actions as action_ids
from resonant.gate.types import Intent
from resonant.gateway.channel import ApprovalPrompt, SelfTestResult
from resonant.loop import NewTask
from resonant.principals import Principal, Principals
from resonant.router.approvals import ApprovalInbox
from resonant.runners.approve import remind_due
from resonant.store import approvals as ap
from resonant.store import repos as repo_store
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.store.db import kv_set, transaction
from resonant.toolcore import Asked, CallScope, Ran, Refused, ToolCore
from resonant.tools import ToolRegistry
from resonant.trust.checks import TrustChecks
from resonant_sdk import Effect, Event, Level, ToolSpec


class Channel(PromptChannel):
    """The trust checks' fake channel, complete enough for the reminders too."""

    async def self_test(self) -> SelfTestResult:  # pragma: no cover - unused
        return SelfTestResult(True, "ok")


SLUG = "justinhe16/mycelium"
ARGS: dict[str, Any] = {"repo": SLUG, "number": 2, "head_sha": "ab" * 20}


def merge_pr(level: Level) -> ToolSpec:
    return ToolSpec(
        name="merge_pr",
        description="Squash-merge a pull request.",
        args_schema={
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "head_sha": {"type": "string"},
            },
            "required": ["repo", "number", "head_sha"],
        },
        effect=Effect.WRITE,
        level=level,
        source="ext:github",
        extension="github",
        approval_summary="merge PR #{number} in {repo}",
    )


class H:
    """A run (R1 on mycelium) whose worker can check, and a brain that can call merge_pr.
    ``level`` is merge_pr's: L2 asks (as installed), L3 runs at once (a standing allow)."""

    def __init__(self, db: sqlite3.Connection, level: Level = Level.L2) -> None:
        self.db = db
        self.clock = Clock()
        self.channel = Channel()
        self.principals = Principals(
            principals={OWNER: Principal(role="owner", identities=(f"imessage:{HANDLE}",))}
        )
        kv_set(db, "autonomy", "on")
        self.spec = merge_pr(level)
        registry = ToolRegistry()
        registry.register(self.spec)
        self.gate = PolicyGate(db, self.principals, registry, dry_run=False,
                               approval_ttl_s=TTL_S, clock=self.clock)  # fmt: skip
        self.merged: list[dict[str, Any]] = []

        async def merge(args: dict[str, Any]) -> dict[str, Any]:
            self.merged.append(args)
            return {"summary": f"merged {args['repo']}#{args['number']}"}

        handlers: dict[HandlerKey, ToolHandler] = {("github", "merge_pr"): merge}
        self.core = ToolCore(db, registry=registry, gate=self.gate,
                             executor=Executor(db, self.gate, handlers))  # fmt: skip
        self.inbox = ApprovalInbox(db, principals=self.principals, gate=self.gate)
        self.checks = TrustChecks(db, self.channel, self.principals,
                                  approval_ttl_s=TTL_S, clock=self.clock, poll_s=0.001)  # fmt: skip
        repo_store.add(db, SLUG, base="main", source="test", name="mycelium")
        tid = task_store.insert_task(db, NewTask(kind="run", runner="claude", priority=1))
        task_store.set_status(db, tid, "running")
        run = runs_store.create(db, repo="mycelium", ask="x", principal=OWNER, session_id="s",
                                max_seconds=60)  # fmt: skip
        runs_store.update(db, run.code, task_id=tid, status="running")
        self.code = run.code
        self.n = 0

    def check(self, what: str = "merge PR #2") -> asyncio.Task[str]:
        call = self.checks.request_check(self.code, "github", "merge", what=what)
        return asyncio.ensure_future(call)

    async def prompted(self, n: int = 1) -> ApprovalPrompt:
        for _ in range(500):
            if len(self.channel.prompts) >= n:
                return self.channel.prompts[n - 1]
            await asyncio.sleep(0.001)
        raise AssertionError("no prompt")

    async def brain(self, args: dict[str, Any] | None = None) -> Ran | Asked | Refused:
        tid = task_store.insert_task(self.db, NewTask(kind="message", runner="claude",
                                                      priority=1))  # fmt: skip
        task_store.set_status(self.db, tid, "running")
        scope = CallScope(tid, OWNER, None, 0, 0, "inferred")
        out = await self.core.call(self.spec, dict(args or ARGS), scope)
        assert isinstance(out, Ran | Asked | Refused), out
        return out

    def reply(self, text: str) -> str | None:
        self.n += 1
        self.clock.now += timedelta(seconds=5)
        event = Event(source="imessage", type="message", principal=OWNER,
                      payload={"text": text, "handle": HANDLE, "guid": f"G{self.n}"},
                      dedupe_key=f"imessage:G{self.n}", occurred_at=self.clock.now)  # fmt: skip
        self.clock.now += timedelta(seconds=1)
        result = self.inbox.handle(event, text, OWNER)
        assert result is not None, f"{text!r} wasn't read as a reply"
        return result.reply

    def approvals(self) -> list[ap.Approval]:
        rows = self.db.execute("SELECT id FROM approvals ORDER BY created_at, id").fetchall()
        found = [ap.get_approval(self.db, r["id"]) for r in rows]
        return [a for a in found if a is not None]


def audits(db: sqlite3.Connection, action: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT decision, detail FROM audit_log WHERE action = ? ORDER BY id", (action,)
    ).fetchall()
    return [{"decision": r["decision"], **json.loads(r["detail"])} for r in rows]


@pytest.fixture
def h(db: sqlite3.Connection) -> H:
    return H(db)


# --- one request per action -------------------------------------------------------------------


async def test_a_brain_merge_while_the_runs_check_is_open_points_to_it(h: H) -> None:
    task = h.check()
    prompt = await h.prompted()
    out = await h.brain()
    assert isinstance(out, Refused) and out.why == "denied"
    assert out.text == (
        f"Not done: {h.code} is already asking for this: {prompt.code}; "
        f"the owner approves it with yes {prompt.code}."
    )
    [only] = h.approvals()  # no second approval
    assert only.id == prompt.approval_id
    assert only.action_key == f"github merge {SLUG}#2" and only.action_target == f"{SLUG}#2"
    h.reply(f"yes {prompt.code}")  # approving the one request answers the run's check
    assert await task == "ok"
    assert h.merged == []


async def test_a_check_while_resonant_asks_for_the_merge_is_handled(h: H) -> None:
    asked = await h.brain()
    assert isinstance(asked, Asked)
    code = asked.decision.code
    assert await h.checks.request_check(h.code, "github", "merge", what="merge #2") == "handled"
    [only] = h.approvals()
    assert only.id == asked.decision.approval_id
    last = audits(h.db, "trust.check")[-1]
    assert last["decision"] == "handled" and last["approval_id"] == only.id
    assert last["reason"] == f"Resonant is already asking for this: {code}; " + (
        f"the owner approves it with yes {code}"
    )


async def test_a_second_brain_merge_points_to_the_first(h: H) -> None:
    asked = await h.brain()
    assert isinstance(asked, Asked)
    again = await h.brain(ARGS | {"head_sha": "cd" * 20})  # another turn, a newer head
    assert isinstance(again, Refused) and asked.decision.code in again.text
    assert len(h.approvals()) == 1


async def test_the_runs_reworded_check_shares_its_open_approval(h: H) -> None:
    first = h.check("merge PR #2")
    prompt = await h.prompted()
    second = h.check("squash-merge #2 now that CI is green")
    await asyncio.sleep(0.01)
    assert len(h.approvals()) == 1 and len(h.channel.prompts) == 1
    h.reply(f"yes {prompt.code}")
    assert await first == "ok" and await second == "ok"


async def test_another_prs_merge_is_its_own_request(h: H) -> None:
    task = h.check()
    await h.prompted()
    out = await h.brain(ARGS | {"number": 3})
    assert isinstance(out, Asked)
    assert len(h.approvals()) == 2
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --- obsolete once the PR is merged or closed -------------------------------------------------


async def test_resonants_merge_obsoletes_the_runs_check_and_its_reminder(
    db: sqlite3.Connection,
) -> None:
    h = H(db, Level.L3)  # merge_pr runs without asking (a standing allow, say)
    task = h.check()
    prompt = await h.prompted()
    ran = await h.brain()
    assert isinstance(ran, Ran) and h.merged == [ARGS]
    assert await task == "denied"
    approval = ap.get_approval(db, prompt.approval_id)
    assert approval is not None and approval.status == "expired"
    assert audits(db, "approval.expired")[-1]["reason"] == ap.OBSOLETE
    h.clock.now += timedelta(seconds=TTL_S / 2 + 1)
    assert await remind_due(db, h.channel, h.clock()) == 0  # nothing left to remind of
    assert h.reply("yes") == "Nothing is waiting for approval."
    assert action_ids.merge_record(db, SLUG, 2) == action_ids.MergeRecord("resonant", None)


@pytest.mark.parametrize("kind", ["pr_merged", "pr_closed"])
async def test_a_pr_merged_or_closed_elsewhere_obsoletes_its_check(h: H, kind: str) -> None:
    task = h.check()
    prompt = await h.prompted()
    data = {"repo": SLUG, "number": 2, "title": "t", "url": "u"}
    with transaction(h.db):
        event = {"type": kind, "text": "merged", "dedupe_key": "k", "data": data}
        persist_events(h.db, "github", [event])
    assert await task == "denied"
    approval = ap.get_approval(h.db, prompt.approval_id)
    assert approval is not None and approval.status == "expired"
    assert audits(h.db, "approval.expired")[-1]["reason"] == ap.OBSOLETE
    h.clock.now += timedelta(seconds=TTL_S / 2 + 1)
    assert await remind_due(h.db, h.channel, h.clock()) == 0
    assert action_ids.merge_record(h.db, SLUG, 2) is None  # merged, but not by Resonant


async def test_other_extensions_events_obsolete_nothing(h: H) -> None:
    task = h.check()
    prompt = await h.prompted()
    data = {"repo": SLUG, "number": 2}
    with transaction(h.db):
        event = {"type": "pr_merged", "text": "x", "dedupe_key": "k", "data": data}
        persist_events(h.db, "linear", [event])
    approval = ap.get_approval(h.db, prompt.approval_id)
    assert approval is not None and approval.status == "pending"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_a_batched_approval_is_left_to_its_batch(db: sqlite3.Connection) -> None:
    """A batch is answered as one unit: one member expiring on its own would leave the rest
    unanswerable, so the batch keeps it (its merge then fails as already merged)."""
    tid = task_store.insert_task(db, NewTask(kind="message", runner="claude", priority=1))
    ids: list[str] = []
    for n, code in ((2, "M6"), (3, "M7")):
        ids.append(ap.insert_approval(db, ap.NewApproval(
            task_id=tid, intent_hash=f"h{n}", tool="merge_pr", effect=Effect.WRITE,
            level=Level.L2, summary="s", approvers=(OWNER,), code=code,
            expires_at=NOW + timedelta(minutes=30), extension="github",
            action_key=f"github merge {SLUG}#{n}", action_target=f"{SLUG}#{n}",
        ), NOW))  # fmt: skip
    ap.insert_batch(db, OWNER, ids, NOW)
    assert ap.obsolete_for_target(db, f"{SLUG}#2", NOW) == []
    member = ap.get_approval(db, ids[1])
    assert member is not None and ap.reply_code(db, member) == "M6"  # the batch's code


# --- identities ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("what", "target"),
    [
        ("merge PR #2", f"{SLUG}#2"),
        ("squash-merge pr 2 into main", f"{SLUG}#2"),
        ("merge pull request 7", f"{SLUG}#7"),
        ("merge Other/Repo#5 too", "other/repo#5"),
        ("merge it", None),  # no PR named and the run has none yet
    ],
)
def test_a_merge_checks_target(h: H, what: str, target: str | None) -> None:
    run = runs_store.get(h.db, h.code)
    assert run is not None
    act = action_ids.for_check(h.db, run, "merge", what)
    assert (act.target if act else None) == target
    assert action_ids.for_check(h.db, run, "deploy", what) is None


def test_a_merge_check_falls_back_to_the_runs_pr(h: H) -> None:
    runs_store.update(h.db, h.code, pr_number=9)
    run = runs_store.get(h.db, h.code)
    assert run is not None
    act = action_ids.for_check(h.db, run, "merge", "merge it")
    assert act is not None and act.key == f"github merge {SLUG}#9"


def test_intent_identities(h: H) -> None:
    def intent(spec: ToolSpec, args: dict[str, Any]) -> Intent:
        return Intent.for_tool(spec, task_id="t", principal=OWNER, args=args)

    act = action_ids.for_intent(h.db, intent(h.spec, ARGS | {"repo": "JustinHe16/Mycelium"}))
    assert act is not None and act.key == f"github merge {SLUG}#2"
    assert action_ids.merged_by(intent(h.spec, ARGS)) == f"{SLUG}#2"
    assert action_ids.for_intent(h.db, intent(h.spec, ARGS | {"number": 0})) is None
    rerun = ToolSpec(name="rerun_failed", effect=Effect.WRITE, level=Level.L3,
                     source="ext:github", extension="github")  # fmt: skip
    act = action_ids.for_intent(h.db, intent(rerun, {"repo": SLUG, "number": 2}))
    assert act is not None and act.verb == "rerun"
    assert action_ids.merged_by(intent(rerun, {"repo": SLUG, "number": 2})) is None

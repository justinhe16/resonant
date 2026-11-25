"""PER-296: ``resonant request`` at the desk, over the real gate and ToolCore.

``WorkerDesk.handle(code, ToolRequest(...))`` with ``WorkerRequests`` over a real
``PolicyGate`` (the team's principals and approvers) and a fake executor that counts runs.
The approval is resolved the way a Slack button does (``gate.resolve`` by id).
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from resonant.executor import NotAllowedError, ToolResult
from resonant.gate import Allow, Decision, Intent, PolicyGate
from resonant.gate import actions as action_ids
from resonant.gate.types import ApprovalResolution
from resonant.gateway.channel import ApprovalPrompt, MessageRef, approval_dedupe_key
from resonant.loop import NewTask
from resonant.principals import Principals
from resonant.runners.reports import did_list
from resonant.runners.worker_cli import (
    EXIT_BUSY,
    EXIT_DENIED,
    EXIT_OK,
    EXIT_PENDING,
    EXIT_USAGE,
    Reply,
    ToolRequest,
)
from resonant.runners.worker_desk import REQUEST_RESTARTED, REQUEST_TIMEOUT, WorkerDesk
from resonant.runners.worker_requests import WorkerRequests
from resonant.store import approvals as ap
from resonant.store import questions as qs
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.store.db import connect, kv_set, migrate, open_db, utcnow
from resonant.toolcore import ToolCore
from resonant.tools import ToolRegistry
from resonant_sdk import Effect, Level, ToolSpec
from resonant_sdk.manifest import Manifest, parse_yaml

TEAM = {
    "principals": {
        "justin": {"role": "owner", "identities": ["slack:U1"]},
        "brian": {"role": "approver", "identities": ["slack:U2"]},
        "miu": {"identities": ["slack:U3"], "grants": {"dori": {"request": ["prod_deploy"]}}},
    }
}
PROD_DEPLOY = ToolSpec(
    name="prod_deploy",
    description="Deploy DORI to production.",
    args_schema={
        "type": "object",
        "properties": {"commit": {"type": "string"}, "build": {"type": "integer"}},
        "required": ["commit"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L1,
    source="ext:dori",
    extension="dori",
    worker_requestable=True,
)
FILE_TICKET = ToolSpec(
    name="file_ticket",
    args_schema={"type": "object", "properties": {"title": {"type": "string"}}},
    effect=Effect.WRITE,
    level=Level.L3,
    source="ext:dori",
    extension="dori",
)
THREAD = "C0GENERAL:1700000000.000100"


def ref(principal: str) -> dict[str, Any]:
    return {"channel": "slack", "principal": principal, "identity": "slack:U2",
            "thread": THREAD, "ref": THREAD}  # fmt: skip


class Channel:
    name = "slack"

    def __init__(self) -> None:
        # threaded: a prompt with a thread lands there (PER-320); else in a DM (no thread)
        self.threaded = False
        self.prompts: list[tuple[str, ApprovalPrompt]] = []
        self.sent: list[tuple[str, str]] = []
        self._keys: set[str] = set()

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        self.sent.append((principal, text))
        return MessageRef("slack", f"out:{len(self.sent)}", thread=thread.ref if thread else None)

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        key = approval_dedupe_key(prompt.approval_id, principal)
        if key not in self._keys:
            self._keys.add(key)
            self.prompts.append((principal, prompt))
        where = prompt.thread.thread if self.threaded and prompt.thread is not None else None
        return MessageRef("slack", f"p:{len(self.prompts)}", where)


class Executor:
    """Spends the gate's token and the approval (as the real one does) and counts runs."""

    def __init__(self, gate: PolicyGate, conn: sqlite3.Connection) -> None:
        self.gate = gate
        self.conn = conn
        self.ran: list[dict[str, Any]] = []
        self.release: asyncio.Event | None = None

    async def execute(
        self, intent: Intent, decision: Decision, *, step_no: int, call_index: int = 0
    ) -> ToolResult:
        if not isinstance(decision, Allow) or not self.gate.consume(decision.token, intent.hash):
            raise NotAllowedError("no allow")
        if decision.approval_id is not None and not ap.mark_used(
            self.conn, decision.approval_id, utcnow()
        ):
            raise NotAllowedError("used")
        if self.release is not None:
            await self.release.wait()
        self.ran.append(dict(intent.args))
        return ToolResult(ok=True, data={"deployed": intent.args.get("commit"), "version": 142})

    def recorded(self, key: str) -> ToolResult | None:
        return None


@dataclass
class Harness:
    db: sqlite3.Connection
    ttl_s: float = 1800.0
    block_s: float = 5.0
    channel: Channel = field(default_factory=Channel)
    woken: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def __post_init__(self) -> None:
        kv_set(self.db, "autonomy", "on")
        self.principals = Principals.model_validate(TEAM)
        reg = ToolRegistry()
        for spec in (PROD_DEPLOY, FILE_TICKET):
            reg.register(spec)
        self.gate = PolicyGate(self.db, self.principals, reg, dry_run=False,
                               approval_ttl_s=self.ttl_s)  # fmt: skip
        self.executor = Executor(self.gate, self.db)
        core = ToolCore(self.db, registry=reg, gate=self.gate, executor=self.executor)
        self.requests = WorkerRequests(self.db, core=core, channel=self.channel,  # pyright: ignore[reportArgumentType]
                                       principals=self.principals, poll_s=0.01)  # fmt: skip
        self.desk = WorkerDesk(
            self.db,
            channel=self.channel,  # pyright: ignore[reportArgumentType]
            trust=None,
            clock=utcnow,
            ask_timeout_s=3600,
            wake=lambda run, key: self.woken.append((run.code, key)),
            block_s=self.block_s,
            requests=self.requests,
        )

    def run(self, requested_by: str | None = "brian", *, thread: bool = True) -> str:
        tid = task_store.insert_task(self.db, NewTask(kind="run", runner="claude", priority=1))
        task_store.set_status(self.db, tid, "running")
        run = runs_store.create(
            self.db, repo="voyager-service", ask="ship it", principal="justin", session_id="s",
            max_seconds=600, requested_by=requested_by,
            channel_ref=ref(requested_by or "justin") if thread else None,
        )  # fmt: skip
        runs_store.update(self.db, run.code, task_id=tid, status="running")
        return run.code

    def request(
        self, code: str, *args: tuple[str, str], tool: str = "prod_deploy", reason: str = ""
    ) -> asyncio.Task[Reply | None]:
        gone: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        req = ToolRequest("dori", tool, args, reason)
        return asyncio.ensure_future(self.desk.handle(code, req, gone))

    async def prompted(self, n: int = 1) -> list[tuple[str, ApprovalPrompt]]:
        for _ in range(500):
            if len(self.channel.prompts) >= n:
                return self.channel.prompts
            await asyncio.sleep(0.01)
        raise AssertionError("no prompt")

    def click(self, approval_id: str, who: str, *, approve: bool = True) -> bool:
        done = self.gate.resolve(ApprovalResolution(approval_id, approve, who, f"slack:{who}",
                                                    "button"))  # fmt: skip
        return done.ok


@pytest.fixture
def h(db: sqlite3.Connection) -> Harness:
    return Harness(db)


async def test_an_approvers_click_runs_the_tool_once_and_the_cli_gets_the_result(
    h: Harness,
) -> None:
    code = h.run("brian")
    call = h.request(code, ("commit", "abc"), ("build", "7"), reason='hotfix "login"')
    prompts = dict(await h.prompted(2))
    # one copy in the run's thread, to Brian (requester and approver); Justin's in his own DM
    prompt, dm = prompts["brian"], prompts["justin"]
    assert prompt.thread is not None and prompt.thread.thread == THREAD
    assert dm.thread is None
    assert prompt.require_code  # L1: a Slack button carries the code
    assert (
        prompt.summary.startswith(f"{code} (for Brian) asks for dori: prod deploy ")
        and "hotfix 'login'" in prompt.summary
    )
    (q,) = qs.for_run(h.db, code)
    assert (q.kind, q.question, q.approval_id) == (
        "request", "dori.prod_deploy build=7 commit=abc", prompt.approval_id
    )  # fmt: skip
    assert h.click(prompt.approval_id, "brian")
    reply = await asyncio.wait_for(call, 5)
    assert reply is not None and reply.exit == EXIT_OK, reply
    assert reply.text.startswith("Approved by Brian: Done: ") and '"version": 142' in reply.text
    assert h.executor.ran == [{"commit": "abc", "build": 7}]  # once, with the schema's types
    assert did_list(h.db, code) == ["ran dori.prod_deploy build=7 commit=abc (approved by Brian)"]


@pytest.mark.parametrize("how", ["deny", "expire"])
async def test_deny_or_expiry_resumes_with_that_text_and_never_runs(
    db: sqlite3.Connection, how: str
) -> None:
    h = Harness(db, ttl_s=0.3 if how == "expire" else 1800.0)
    code = h.run("brian")
    call = h.request(code, ("commit", "abc"))
    (_to, prompt), *_ = await h.prompted()
    if how == "deny":
        assert h.click(prompt.approval_id, "justin", approve=False)
    reply = await asyncio.wait_for(call, 5)
    assert reply is not None and reply.exit == EXIT_DENIED
    if how == "deny":
        assert reply.text == "Denied by Justin: the tool didn't run."
    else:
        assert reply.text.startswith("No approver answered in time")
    assert h.executor.ran == [] and did_list(h.db, code) == []


async def test_asked_again_after_an_expiry_it_opens_a_fresh_approval(
    db: sqlite3.Connection,
) -> None:
    """Each request has its own task (the gate keeps one approval per task and intent), so
    "ask again if you still need it" works after an expiry or a restart."""
    h = Harness(db, ttl_s=0.3)
    code = h.run("brian")
    first = await asyncio.wait_for(h.request(code, ("commit", "abc")), 5)
    assert first is not None and first.exit == EXIT_DENIED
    h.gate.ttl = timedelta(seconds=1800)
    again = h.request(code, ("commit", "abc"))
    prompts = await h.prompted(4)
    assert prompts[2][1].approval_id != prompts[0][1].approval_id
    assert h.click(prompts[2][1].approval_id, "brian")
    reply = await asyncio.wait_for(again, 5)
    assert reply is not None and reply.exit == EXIT_OK and h.executor.ran == [{"commit": "abc"}]
    kinds = db.execute(
        "SELECT status FROM tasks WHERE kind = 'request' ORDER BY created_at, rowid"
    ).fetchall()
    assert [r["status"] for r in kinds] == ["failed", "done"]


async def test_refusals_before_anything_is_asked(h: Harness) -> None:
    code = h.run("brian")
    reply = await h.request(code, ("title", "x"), tool="file_ticket")
    assert reply is not None and reply.exit == EXIT_USAGE
    assert reply.text.startswith("Not requestable: dori.file_ticket. You may request: ")
    assert "dori.prod_deploy" in reply.text
    for args, why in [
        ((("commit", "abc"), ("env", "prod")), "has no arg env"),
        ((("commit", "abc"), ("build", "seven")), "--arg build must be integer"),
        ((), "the args don't fit dori.prod_deploy (required)"),
    ]:
        reply = await h.request(code, *args)
        assert reply is not None and reply.exit == EXIT_USAGE and why in reply.text, reply
    assert h.channel.prompts == [] and qs.for_run(h.db, code) == []


async def test_the_fourth_open_request_is_refused(h: Harness) -> None:
    code = h.run("brian")
    calls = [h.request(code, ("commit", c)) for c in ("a", "b", "c")]
    await h.prompted(6)  # three approvals, two approvers each
    fourth = await h.request(code, ("commit", "d"))
    assert fourth is not None and fourth.exit == EXIT_BUSY and "not a no" in fourth.text
    again = h.request(code, ("commit", "a"))  # a repeat joins the open one: nothing new
    await asyncio.sleep(0.05)
    assert len(qs.for_run(h.db, code)) == 3 and len(h.channel.prompts) == 6
    for c in [*calls, again]:
        c.cancel()
    await h.desk.aclose()


async def test_a_member_requested_run_still_needs_an_approvers_click(h: Harness) -> None:
    h.channel.threaded = True
    code = h.run("miu")
    call = h.request(code, ("commit", "abc"))
    prompts = await h.prompted(1)
    approval_id = prompts[0][1].approval_id
    a = ap.get_approval(h.db, approval_id)
    assert a is not None and a.approvers == ("justin", "brian") and a.requested_by == "miu"
    # PER-320: Miu isn't an approver: one copy in the run's thread, addressed to Miu and
    # tagging both approvers (no DMs); it counts as sent to each approver
    await asyncio.sleep(0.05)
    assert [(p, pr.thread is not None, pr.mention) for p, pr in h.channel.prompts] == [
        ("miu", True, ("justin", "brian"))
    ]
    a = ap.get_approval(h.db, approval_id)
    assert a is not None and {(c.principal, c.thread) for c in a.channel_refs} == {
        ("justin", THREAD),
        ("brian", THREAD),
    }
    assert prompts[0][1].summary.startswith(f"{code} (for Miu) asks for ")
    assert not h.click(approval_id, "miu")
    await asyncio.sleep(0.05)
    assert not call.done() and h.executor.ran == []
    assert h.click(approval_id, "brian")
    reply = await asyncio.wait_for(call, 5)
    assert reply is not None and reply.exit == EXIT_OK and h.executor.ran == [{"commit": "abc"}]


async def test_a_scheduled_runs_request_dms_each_approver(h: Harness) -> None:
    # PER-320: a scheduled job's run has no thread to post in: each approver gets their own
    h.channel.threaded = True
    code = h.run("justin", thread=False)
    call = h.request(code, ("commit", "abc"))
    prompts = await h.prompted(2)
    assert [(p, pr.thread, pr.mention) for p, pr in prompts] == [
        ("justin", None, ()),
        ("brian", None, ()),
    ]
    a = ap.get_approval(h.db, prompts[0][1].approval_id)
    assert a is not None and {(c.principal, c.thread) for c in a.channel_refs} == {
        ("justin", None),
        ("brian", None),
    }
    call.cancel()
    await h.desk.aclose()


async def test_two_runs_asking_the_same_deploy_share_one_approval(h: Harness) -> None:
    first, second = h.run("brian"), h.run("justin")
    call = h.request(first, ("commit", "abc"))
    await h.prompted(2)
    reply = await h.request(second, ("commit", "abc"))
    assert reply is not None and reply.exit == EXIT_DENIED
    assert reply.text.startswith(f"{first} already asked an approver for this")
    assert len(h.channel.prompts) == 2  # no second approval went out
    call.cancel()
    await h.desk.aclose()


async def test_no_answer_yet_parks_the_cli_and_the_result_comes_on_resume(
    db: sqlite3.Connection,
) -> None:
    h = Harness(db, block_s=0.1)
    code = h.run("brian")
    reply = await h.request(code, ("commit", "abc"))
    assert reply is not None and reply.exit == EXIT_PENDING
    (_to, prompt), *_ = await h.prompted()
    assert h.click(prompt.approval_id, "brian")
    for _ in range(300):
        if h.desk.has_deliveries(code):
            break
        await asyncio.sleep(0.01)
    assert (code, f"answer:{qs.for_run(db, code)[0].id}") in h.woken
    resumed = h.desk.resume_text(code)
    assert resumed is not None and resumed.startswith(
        "Your request (dori.prod_deploy commit=abc): Approved by Brian: Done: "
    )
    assert h.executor.ran == [{"commit": "abc"}]


async def test_a_timeout_or_restart_closes_it_and_expires_the_approval(
    db: sqlite3.Connection,
) -> None:
    h = Harness(db, block_s=0.1)
    code = h.run("brian")
    await h.request(code, ("commit", "abc"))
    (_to, prompt), *_ = await h.prompted()
    h.desk.ask_timeout = timedelta(0)
    h.desk.time_out_due(code)
    await asyncio.sleep(0.05)
    (q,) = qs.for_run(db, code)
    assert (q.status, q.answer) == ("timed_out", REQUEST_TIMEOUT)
    a = ap.get_approval(db, prompt.approval_id)
    assert a is not None and a.status == "expired"
    assert not h.click(prompt.approval_id, "brian") and h.executor.ran == []

    # a restart: the open request is closed and its approval expired ("ask again")
    later = h.run("brian")
    await h.request(later, ("commit", "def"))
    prompts = await h.prompted(4)
    approval_id = prompts[2][1].approval_id
    await h.desk.aclose()
    # (a crash, not a clean stop: the approval was still pending)
    db.execute("UPDATE approvals SET status = 'pending' WHERE id = ?", (approval_id,))
    db.execute("UPDATE tasks SET status = 'running' WHERE kind = 'request'")
    Harness(db).desk.startup()
    left = db.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'request' AND status = 'running'")
    assert left.fetchone()[0] == 0
    (q,) = qs.for_run(db, later)
    assert (q.status, q.answer) == ("timed_out", REQUEST_RESTARTED)
    a = ap.get_approval(db, approval_id)
    assert a is not None and a.status == "expired"


# --- the flag, the action identity and migration 033 ------------------------------------------


def test_only_extension_tools_are_worker_requestable() -> None:
    with pytest.raises(ValueError, match="only extension tools"):
        ToolSpec(name="x", effect=Effect.WRITE, worker_requestable=True)
    manifest = Manifest.model_validate(
        parse_yaml(
            "name: dori\nversion: 1\ntools:\n  prod_deploy:\n    run: ./cli.py {commit}\n"
            "    effect: write\n    level: L1\n    args: {commit: string}\n"
            "    worker_requestable: true\n  status:\n    run: ./cli.py s\n    effect: read\n"
        )
    )
    flags = {s.name: s.worker_requestable for s in manifest.tool_specs()}
    assert flags == {"prod_deploy": True, "status": False}


def test_a_requestable_call_is_an_action_keyed_by_its_args(db: sqlite3.Connection) -> None:
    intent = Intent.for_tool(PROD_DEPLOY, task_id="t", principal="brian",
                             args={"commit": "abc", "build": 7})  # fmt: skip
    action = action_ids.for_intent(db, intent)
    assert action is not None and action.key == "dori prod_deploy build=7 commit=abc"
    plain = Intent.for_tool(FILE_TICKET, task_id="t", principal="brian", args={"title": "x"})
    assert action_ids.for_intent(db, plain) is None  # not requestable: no identity, as before
    assert action_ids.args_target({"x": "y" * 300}).startswith("sha256:")


def test_migration_033_keeps_questions_and_allows_requests(tmp_path: Path) -> None:
    found = sorted(
        (int(f.name[:3]), f.read_text())
        for f in resources.files("resonant.store.migrations").iterdir()
        if f.name.endswith(".sql")
    )
    conn = connect(tmp_path / "old.db")
    prior = max(v for v, _sql in found if v < 33)  # the migration before 033
    assert migrate(conn, [m for m in found if m[0] <= prior]) == prior
    conn.execute(
        "INSERT INTO run_questions (run_code, kind, question, asked_at) VALUES"
        " ('R1', 'ask', 'which one?', '2026-11-20T12:00:00+00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO run_questions (run_code, kind, question, asked_at) VALUES"
            " ('R1', 'request', 'x', '2026-11-20T12:00:00+00:00')"
        )
    conn.close()
    conn = open_db(tmp_path / "old.db")
    (q,) = qs.for_run(conn, "R1")
    assert (q.id, q.kind, q.question, q.approval_id) == (1, "ask", "which one?", None)
    r = qs.ask(conn, "R1", "request", "dori.prod_deploy commit=abc", at=utcnow())
    qs.set_approval(conn, r.id, "A1")
    assert r.id == 2 and qs.by_approval(conn, "A1") == qs.get(conn, r.id)
    assert [x.id for x in qs.open_requests(conn)] == [2]
    conn.close()


async def test_a_restart_after_the_approval_says_it_may_have_run(db: sqlite3.Connection) -> None:
    h = Harness(db, block_s=0.1)
    h.executor.release = asyncio.Event()  # the deploy hangs after the yes
    code = h.run("brian")
    await h.request(code, ("commit", "abc"))
    (_to, prompt), *_ = await h.prompted()
    assert h.click(prompt.approval_id, "brian")
    h.desk.ask_timeout = timedelta(0)
    for _ in range(200):
        await asyncio.sleep(0.01)
        h.desk.time_out_due(code)  # approved and running: no timeout
    (q,) = qs.for_run(db, code)
    assert q.status == "open"
    Harness(db).desk.startup()  # the daemon died mid-deploy
    (q,) = qs.for_run(db, code)
    assert q.status == "timed_out" and q.answer is not None and "it may have run" in q.answer
    h.executor.release.set()
    await h.desk.aclose()

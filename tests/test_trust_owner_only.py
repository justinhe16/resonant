"""PER-318: trust rules are the owner's. An approver approves one action at a time: they never
request or approve a trust tool, their ``yes always`` is a plain yes (no rule, one short line
says so), and their prompts never offer ``yes always``. The owner's behaviour is unchanged."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from typing import Any

import pytest
from test_trust_checks import NOW, TTL_S, Channel, Clock, audits

from resonant.executor import HandlerKey, ToolHandler
from resonant.gate import (
    ApprovalResolution,
    Deny,
    DryRunGate,
    Intent,
    NeedsApproval,
    PolicyGate,
)
from resonant.gate.codes import without_offer
from resonant.loop import NewTask
from resonant.principals import TRUST_TOOLS, Principals
from resonant.router import Router
from resonant.router.approvals import ApprovalInbox, once_reply
from resonant.router.fast_path import NOT_ALLOWED
from resonant.runners.approve import prompt_approvers
from resonant.store import approvals as ap
from resonant.store import runs as runs_store
from resonant.store import tasks as repo
from resonant.store import trust as trust_store
from resonant.store.db import kv_set
from resonant.tools import ToolRegistry
from resonant.tools.builtin import BUILTIN_SPECS
from resonant.trust.checks import TrustChecks
from resonant_sdk import Effect, Event, Level

OWNER, APPROVER, MEMBER = "owner", "approver", "member"
HANDLES = {OWNER: "+15550000001", APPROVER: "+15550000002", MEMBER: "+15550000003"}
TEAM: dict[str, Any] = {
    "principals": {
        OWNER: {"role": "owner", "identities": [f"imessage:{HANDLES[OWNER]}"]},
        APPROVER: {"role": "approver", "identities": [f"imessage:{HANDLES[APPROVER]}"]},
        MEMBER: {
            "identities": [f"imessage:{HANDLES[MEMBER]}"],
            "grants": {"builtin:files": ["request"]},
        },
    }
}
BUILTINS = {s.name: s for s in BUILTIN_SPECS}


@pytest.fixture
def team() -> Principals:
    return Principals.model_validate(TEAM)


# --- principals and the gate: trust tools are the owner's -------------------------------------


@pytest.mark.parametrize("name", sorted(TRUST_TOOLS))
def test_only_the_owner_requests_or_approves_trust_tools(team: Principals, name: str) -> None:
    spec = BUILTINS[name]
    assert team.may_request(OWNER, spec)
    assert team.can(OWNER, "approve", None, tool=name)
    for other in (APPROVER, MEMBER):
        assert not team.may_request(other, spec)
        assert not team.can(other, "approve", None, tool=name)
    # Approvers still request and approve the rest of the builtins.
    assert team.may_request(APPROVER, BUILTINS["list_trust_rules"])
    assert team.may_request(APPROVER, BUILTINS["start_run"])


def _gate(db: sqlite3.Connection, team: Principals, clock: Clock | None = None) -> PolicyGate:
    reg = ToolRegistry()
    for spec in BUILTIN_SPECS:
        reg.register(spec)
    kv_set(db, "autonomy", "on")
    return PolicyGate(db, team, reg, dry_run=False, approval_ttl_s=TTL_S, clock=clock or Clock())


def _task(db: sqlite3.Connection, principal: str) -> str:
    tid = repo.insert_task(
        db, NewTask(kind="chat", runner="local", priority=1, principal=principal)
    )
    repo.set_status(db, tid, "running")
    return tid


@pytest.mark.parametrize("name", sorted(TRUST_TOOLS))
def test_trust_tools_are_asked_of_the_owner_alone(
    db: sqlite3.Connection, team: Principals, name: str
) -> None:
    gate = _gate(db, team)
    assert gate.approvers(None, name) == (OWNER,)
    assert gate.approvers(None, "restart_resonant") == (OWNER, APPROVER)


TRUST_ARGS: dict[str, dict[str, Any]] = {
    "add_trust_rule": {"scope": "site", "action": "deploy", "effect": "allow"},
    "allow_trust_rule": {"scope": "site", "action": "deploy"},
    "remove_trust_rule": {"id": 1},
    "allow_tools": {"tools": ["merge_run"]},
}


@pytest.mark.parametrize("name", sorted(TRUST_TOOLS))
@pytest.mark.parametrize("origin", ["inferred", "explicit"])
def test_an_approver_asking_for_a_trust_tool_is_refused_and_audited(
    db: sqlite3.Connection, team: Principals, name: str, origin: Any
) -> None:
    trust_store.add_rule(db, scope="site", action="deploy", effect="ask", created_from="chat",
                         now=NOW)  # fmt: skip
    before = [r.text for r in trust_store.list_rules(db)]
    gate = _gate(db, team)
    intent = Intent.for_tool(BUILTINS[name], task_id=_task(db, APPROVER), principal=APPROVER,
                             args=TRUST_ARGS[name], origin=origin)  # fmt: skip
    decision = gate.evaluate(intent)
    assert isinstance(decision, Deny) and decision.reason == "not permitted to request this tool"
    assert [r.text for r in trust_store.list_rules(db)] == before
    assert not db.execute("SELECT 1 FROM approvals").fetchall()
    (row,) = db.execute(
        "SELECT requested_by, decision, tool FROM audit_log WHERE action = 'gate.evaluate'"
    ).fetchall()
    assert (row["requested_by"], row["decision"], row["tool"]) == (APPROVER, "Deny", name)


# --- the prompt: the owner's alone offers yes always ------------------------------------------


@pytest.mark.parametrize(
    ("summary", "plain"),
    [
        (
            'R4 wants to deploy on prod-api: "x" (new system; yes always = don\'t ask before'
            " deploy in prod-api)",
            'R4 wants to deploy on prod-api: "x" (new system)',
        ),
        (
            "restart Resonant (yes always = don't ask before running restart_resonant)",
            "restart Resonant",
        ),
        # outside text earlier in the summary never makes it cut more than the note
        (
            'R4 wants to deploy on site: "a (yes always = b)" (new; yes always = don\'t ask)',
            'R4 wants to deploy on site: "a (yes always = b)" (new)',
        ),
        ("no offer here", "no offer here"),
        ("no offer (here)", "no offer (here)"),
    ],
)
def test_without_offer(summary: str, plain: str) -> None:
    assert without_offer(summary) == plain


async def test_only_the_owners_prompt_offers_yes_always(db: sqlite3.Connection) -> None:
    channel = Channel()
    tid = _task(db, OWNER)
    summary = "R4 wants to deploy on prod-api (new system; yes always = don't ask before deploy)"
    approval_id = ap.insert_approval(
        db,
        ap.NewApproval(task_id=tid, intent_hash="h", tool="restart_resonant", effect=Effect.WRITE,
                       level=Level.L2, summary=summary, approvers=(APPROVER, OWNER), code="K4",
                       expires_at=NOW + timedelta(hours=1), allow_always=True),
        NOW,
    )  # fmt: skip
    needs = NeedsApproval(approval_id=approval_id, code="K4", approvers=(APPROVER, OWNER),
                          expires_at=NOW + timedelta(hours=1), summary=summary,
                          effect=Effect.WRITE, level=Level.L2, require_code=False,
                          allow_always=True, offer_to=OWNER)  # fmt: skip
    result = await prompt_approvers(
        db, channel,  # pyright: ignore[reportArgumentType]
        needs, task_id=tid, now=lambda: NOW,
    )  # fmt: skip
    assert result.reached == (APPROVER, OWNER)
    texts = {who: text for who, text, _key in channel.sent}
    assert texts[OWNER] == f"[K4] {summary}. Reply yes / yes always / no"
    assert texts[APPROVER] == "[K4] R4 wants to deploy on prod-api (new system)? Reply yes / no"
    # What the brains see is what each one was sent.
    seen = db.execute("SELECT principal, text FROM system_texts ORDER BY id").fetchall()
    assert {r["principal"]: "always" in r["text"] for r in seen} == {OWNER: True, APPROVER: False}


# --- a worker's check on a team: the approver's yes always is a plain yes --------------------


class TeamHarness:
    def __init__(self, db: sqlite3.Connection, team: Principals) -> None:
        self.db = db
        self.clock = Clock()
        self.channel = Channel()
        self.principals = team
        self.gate = _gate(db, team, self.clock)
        self.inbox = ApprovalInbox(db, principals=team, gate=self.gate)
        self.checks = TrustChecks(
            db,
            self.channel,  # pyright: ignore[reportArgumentType]
            team,
            approval_ttl_s=TTL_S,
            clock=self.clock,
            poll_s=0.001,
        )
        self.n = 0

    def run(self, requester: str) -> str:
        tid = repo.insert_task(self.db, NewTask(kind="run", runner="claude", priority=1))
        repo.set_status(self.db, tid, "running")
        run = runs_store.create(
            self.db, repo="site", ask="x", principal=requester, session_id="s", max_seconds=60
        )
        runs_store.update(self.db, run.code, task_id=tid, status="running")
        return run.code

    async def prompts(self, n: int) -> dict[str, str]:
        for _ in range(500):
            if len(self.channel.sent) >= n:
                return {who: text for who, text, _key in self.channel.sent}
            await asyncio.sleep(0.001)
        raise AssertionError("no prompt")

    def reply(self, who: str, text: str) -> str | None:
        self.n += 1
        self.clock.now += timedelta(seconds=5)
        event = Event(
            source="imessage",
            type="message",
            principal=who,
            payload={"text": text, "handle": HANDLES[who], "guid": f"G{self.n}"},
            dedupe_key=f"imessage:G{self.n}",
            occurred_at=self.clock.now,
        )
        self.clock.now += timedelta(seconds=1)
        result = self.inbox.handle(event, text, who)
        assert result is not None, f"{text!r} wasn't read as a reply"
        return result.reply


@pytest.fixture
def th(db: sqlite3.Connection, team: Principals) -> TeamHarness:
    return TeamHarness(db, team)


async def test_an_approvers_yes_always_approves_once_and_adds_no_rule(th: TeamHarness) -> None:
    code = th.run(APPROVER)
    task = asyncio.ensure_future(th.checks.request_check(code, "prod-api", "deploy"))
    texts = await th.prompts(2)
    assert "yes always" in texts[OWNER]
    assert "always" not in texts[APPROVER]
    (row,) = th.db.execute("SELECT code FROM approvals").fetchall()
    assert th.reply(APPROVER, f"yes always {row['code']}") == (
        "Approved this once. Only Owner can make it standing."
    )
    assert await task == "ok"
    assert trust_store.list_rules(th.db) == []
    assert not audits(th.db, "trust.rule_added")
    (resolved,) = audits(th.db, "approval.resolve")
    assert resolved["decision"] == "approved" and resolved["approved_by"] == APPROVER
    assert resolved["always"] is True and resolved["once"] is True
    (stored,) = th.db.execute("SELECT status, always FROM approvals").fetchall()
    assert stored["status"] == "approved" and not stored["always"]


async def test_the_owners_yes_always_still_adds_the_rule(th: TeamHarness) -> None:
    code = th.run(OWNER)
    task = asyncio.ensure_future(th.checks.request_check(code, "prod-api", "deploy"))
    await th.prompts(2)
    (row,) = th.db.execute("SELECT code FROM approvals").fetchall()
    assert th.reply(OWNER, f"yes always {row['code']}") is None
    assert await task == "ok"
    (rule,) = trust_store.list_rules(th.db)
    assert (rule.scope, rule.action, rule.effect) == ("prod-api", "deploy", "allow")
    (resolved,) = audits(th.db, "approval.resolve")
    assert resolved["approved_by"] == OWNER and resolved.get("once") is None


def test_an_approvers_always_by_id_is_a_plain_yes_too(
    db: sqlite3.Connection, team: Principals
) -> None:
    gate = _gate(db, team)
    tid = _task(db, OWNER)
    approval_id = ap.insert_approval(
        db,
        ap.NewApproval(task_id=tid, intent_hash="h", tool="restart_resonant", effect=Effect.WRITE,
                       level=Level.L2, summary="restart (yes always = x)",
                       approvers=(OWNER, APPROVER), code="K4",
                       expires_at=NOW + timedelta(hours=1), allow_always=True,
                       always_scope="*", always_action="tool:restart_resonant"),
        NOW,
    )  # fmt: skip
    resolution = ApprovalResolution(
        approval_id=approval_id,
        approved=True,
        actor=APPROVER,
        actor_identity="cli",
        path="cli",
        always=True,
    )
    result = gate.resolve(resolution)
    assert result.ok and result.once
    row = ap.get_approval(db, approval_id)
    assert row is not None and row.status == "approved" and not row.always
    assert trust_store.list_rules(db) == []


def test_once_reply_names_the_owner() -> None:
    assert once_reply("Owner") == "Approved this once. Only Owner can make it standing."


# --- /rules: approvers list, only the owner changes -----------------------------------------


@pytest.fixture
def router(db: sqlite3.Connection, team: Principals) -> Router:
    reg = ToolRegistry()
    for spec in BUILTIN_SPECS:
        reg.register(spec)

    async def handler(_args: dict[str, Any]) -> dict[str, Any]:
        return {"rules": []}

    handlers: dict[HandlerKey, ToolHandler] = {(None, s.name): handler for s in BUILTIN_SPECS}
    return Router(db, principals=team, registry=reg, gate=DryRunGate(db, team, reg),
                  handlers=handlers, model=None)  # pyright: ignore[reportArgumentType]  # fmt: skip


async def _reply(router: Router, text: str, principal: str) -> str:
    payload = {"text": text, "identity": "imessage:x", "ref": "r", "truncated": False}
    event = Event(source="imessage", type="message", payload=payload, principal=principal)
    task = await router.route(event)
    assert task is not None
    return str(task.checkpoint.get("fast_reply"))


@pytest.mark.parametrize("text", ["/rules forget 1", "/rules allow deploy in site"])
async def test_an_approvers_rules_change_is_refused_and_audited(
    db: sqlite3.Connection, router: Router, text: str
) -> None:
    assert "/rules" in {c.name for c in router.fast_path.available(APPROVER)}
    assert await _reply(router, text, APPROVER) == NOT_ALLOWED
    (row,) = db.execute(
        "SELECT requested_by, detail FROM audit_log WHERE action = 'router.command_refused'"
    ).fetchall()
    assert row["requested_by"] == APPROVER and "/rules" in row["detail"]

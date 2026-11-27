"""PER-293: roles and grants. The approver role, the workspace default and per-tool grants,
through principals, the gate's approvers, the extension checks and the fast path.

End to end on the fake Slack channel: ``test_e2e_team.py``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from resonant import killswitch
from resonant.executor import HandlerKey, ToolHandler
from resonant.extensions.checks import approver_errors, grant_warnings
from resonant.gate import ApprovalResolution, DryRunGate, Intent, NeedsApproval, PolicyGate
from resonant.loop import NewTask
from resonant.principals import ADMIN_TOOLS, CLAUDE_GRANT, Principals, ToolGrants
from resonant.router import Router
from resonant.router.fast_path import (
    KILL_REPLY,
    NOT_ALLOWED,
    RESUME_REPLY,
    SIBLING_PAUSE_REPLY,
    start_work_reply,
)
from resonant.store import tasks as repo
from resonant.store.db import kv_get, kv_set
from resonant.tools import ToolRegistry
from resonant.tools.ask import ASK_CLAUDE
from resonant.tools.builtin import BUILTIN_SPECS
from resonant_sdk import Effect, Event, Level, Manifest, ToolSpec

TEAM: dict[str, Any] = {
    "principals": {
        "alice": {"role": "owner", "identities": ["slack:U1"]},
        "bob": {"role": "approver", "identities": ["slack:U2"]},
        "carol": {
            "identities": ["slack:U3"],
            "grants": {"dori": {"request": ["file_ticket", "find_tickets"]}},
        },
        "family": {
            "identities": ["imessage:+15557654321"],
            "grants": {"builtin:files": ["request"]},
        },
        "ops": {"identities": ["slack:U4"], "grants": {"dori": {"approve": ["prod_deploy"]}}},
    },
    "default": {"channel": "slack", "grants": {"dori": {"request": ["file_ticket"]}}},
}
OWNER_ONLY: dict[str, Any] = {"principals": {"alice": {"role": "owner"}}}


def tool(name: str, ext: str | None = "dori", *, effect: Effect = Effect.WRITE) -> ToolSpec:
    level = Level.L0 if effect is Effect.READ else Level.L2
    return ToolSpec(name=name, effect=effect, level=level, extension=ext)


FILE_TICKET = tool("file_ticket")
FIND_TICKETS = tool("find_tickets", effect=Effect.READ)
PROD_DEPLOY = tool("prod_deploy")
SYS_WRITE = tool("restart_resonant", None)


@pytest.fixture
def team() -> Principals:
    return Principals.model_validate(TEAM)


# --- principals -------------------------------------------------------------------------


def test_roles_owner_approver_member(team: Principals) -> None:
    assert team.owner == "alice" and team.approver_names == ("bob",)
    assert team.can("bob", "request", None) and team.can("bob", "approve", None)
    assert team.can("bob", "request", "trade-jev") and team.can("bob", "approve", "dori")
    assert not team.can("bob", "admin", None) and not team.can("bob", "admin", "dori")
    assert team.is_approver("alice") and team.is_approver("bob")
    assert not team.is_approver("carol") and not team.is_approver("slack:U9")


def test_per_tool_grants_check_the_tool_name(team: Principals) -> None:
    assert team.may_request("carol", FILE_TICKET) and team.may_request("carol", FIND_TICKETS)
    assert not team.may_request("carol", PROD_DEPLOY)
    assert not team.can("carol", "request", "dori")  # no tool named: a per-tool grant covers none
    assert not team.can("carol", "approve", "dori", tool="file_ticket")
    assert team.can("ops", "approve", "dori", tool="prod_deploy")
    assert not team.can("ops", "approve", "dori", tool="file_ticket")
    assert team.holds("ops", "approve", "dori") and not team.holds("carol", "approve", "dori")


def test_the_workspace_default_needs_a_vouching_adapter(team: Principals) -> None:
    assert team.resolve("slack:U9") is None  # unvouched: unknown, as before
    assert team.resolve("slack:U9", vouched=True) == "slack:U9"
    assert team.resolve("slack:U3", vouched=True) == "carol"  # a listed identity stays its own
    assert team.resolve("imessage:+15550000009", vouched=True) is None  # never iMessage
    assert team.is_default("slack:U9") and not team.is_default("slack:U3")
    p = team.get("slack:U9")
    assert p is not None and p.role == "member" and p.identities == ("slack:U9",)
    assert team.may_request("slack:U9", FILE_TICKET)
    assert not team.may_request("slack:U9", PROD_DEPLOY)
    assert not team.can("slack:U9", "approve", None) and not team.is_approver("slack:U9")
    assert team.get("slack:bad id") is None


def test_no_default_means_unknown_stays_unknown() -> None:
    p = Principals.model_validate(OWNER_ONLY)
    assert p.resolve("slack:U9", vouched=True) is None and p.get("slack:U9") is None


@pytest.mark.parametrize(
    "default",
    [
        {"channel": "imessage", "grants": {}},
        {"channel": "slack", "grants": {"dori": ["request", "approve"]}},
        {"channel": "slack", "grants": {"dori": {"approve": ["prod_deploy"]}}},
        {"channel": "slack", "grants": {"builtin:files": {"request": ["read_file"]}}},
    ],
)
def test_bad_defaults_are_refused(default: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Principals.model_validate({**OWNER_ONLY, "default": default})


def test_principal_names_cant_look_like_identities() -> None:
    with pytest.raises(ValidationError, match="may not contain"):
        Principals.model_validate({"principals": {"alice": {"role": "owner"}, "slack:U9": {}}})


def test_builtin_grants_stay_lists_of_request() -> None:
    with pytest.raises(ValidationError, match="may only list request"):
        Principals.model_validate(
            {
                "principals": {
                    "alice": {"role": "owner"},
                    "x": {"grants": {"builtin:files": {"request": ["read_file"]}}},
                }
            }
        )


def test_claude_grant_reaches_claude_for_members_with_extension_grants(
    team: Principals,
) -> None:
    assert ASK_CLAUDE.grant == CLAUDE_GRANT
    assert team.may_request("carol", ASK_CLAUDE)  # a dori grant: Claude is how she uses it
    assert team.may_request("slack:U9", ASK_CLAUDE)
    assert team.may_request("bob", ASK_CLAUDE) and team.may_request("alice", ASK_CLAUDE)
    assert not team.may_request("family", ASK_CLAUDE)  # builtin grants alone: unchanged
    explicit = Principals.model_validate(
        {
            "principals": {
                "alice": {"role": "owner"},
                "kid": {"grants": {"builtin:claude": ["request"]}},
            }
        }
    )
    assert explicit.may_request("kid", ASK_CLAUDE)
    # No team config (no approver, no default): an extension grant alone changes nothing.
    solo = Principals.model_validate(
        {
            "principals": {
                "alice": {"role": "owner"},
                "cofounder": {"grants": {"dori": ["request", "approve"]}},
            }
        }
    )
    assert not solo.team and team.team
    assert not solo.may_request("cofounder", ASK_CLAUDE)


def test_display_names(team: Principals) -> None:
    assert team.display("alice") == "Alice"
    assert start_work_reply(team, can_file=True) == (
        "Only Alice or Bob can start work. I can file a ticket for it instead."
    )
    owner_only = Principals.model_validate(OWNER_ONLY)
    assert start_work_reply(owner_only, can_file=False) == "Only Alice can start work."


def test_tool_grants_model() -> None:
    g = ToolGrants(request=("a",), approve=("b",))
    assert g.tools("request") == ("a",) and g.tools("approve") == ("b",)


# --- gate approvers -------------------------------------------------------------------------

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def policy(db: sqlite3.Connection, principals: Principals, named: list[str]) -> PolicyGate:
    reg = ToolRegistry()
    for spec in (FILE_TICKET, FIND_TICKETS, PROD_DEPLOY, SYS_WRITE):
        reg.register(spec)
    kv_set(db, "autonomy", "on")
    return PolicyGate(db, principals, reg, dry_run=False, approval_ttl_s=1800,
                      clock=lambda: NOW, approvers_for=lambda _ext: named)  # fmt: skip


def waiting_task(db: sqlite3.Connection, principal: str) -> str:
    tid = repo.insert_task(
        db, NewTask(kind="chat", runner="local", priority=1, principal=principal)
    )
    repo.set_status(db, tid, "running")
    return tid


def ask(gate: PolicyGate, db: sqlite3.Connection, spec: ToolSpec, principal: str) -> NeedsApproval:
    tid = waiting_task(db, principal)
    asked = gate.evaluate(Intent.for_tool(spec, task_id=tid, principal=principal, args={"x": 1}))
    assert isinstance(asked, NeedsApproval), asked
    repo.set_status(db, tid, "waiting", wait_reason="approval", wake_at=asked.expires_at)
    return asked


def test_builtin_approvers_are_the_owner_and_every_approver(
    db: sqlite3.Connection, team: Principals
) -> None:
    gate = policy(db, team, ["carol"])
    assert gate.approvers(None) == ("alice", "bob")
    asked = ask(gate, db, SYS_WRITE, "bob")  # bob asks for a gated builtin
    assert asked.approvers == ("alice", "bob")
    # self-approval: bob answers his own request
    done = gate.resolve(ApprovalResolution(asked.approval_id, True, "bob", "slack", "cli"))
    assert done.ok, done.reason


def test_extension_approvers_add_manifest_names_with_the_tools_grant(
    db: sqlite3.Connection, team: Principals
) -> None:
    gate = policy(db, team, ["ops", "carol", "slack:U9", "default"])
    assert gate.approvers("dori", "prod_deploy") == ("alice", "bob", "ops")
    assert gate.approvers("dori", "file_ticket") == ("alice", "bob")
    assert gate.approvers("dori") == ("alice", "bob", "ops")
    asked = ask(gate, db, FILE_TICKET, "carol")
    assert asked.approvers == ("alice", "bob")
    nope = gate.resolve(ApprovalResolution(asked.approval_id, True, "ops", "slack", "cli"))
    assert not nope.ok and nope.reason == "not_found"  # ops approves prod_deploy only


def test_a_removed_approver_is_refused_the_owner_still_answers(
    db: sqlite3.Connection, team: Principals
) -> None:
    gate = policy(db, team, [])
    asked = ask(gate, db, SYS_WRITE, "alice")
    assert "bob" in asked.approvers
    demoted = dict(TEAM, principals={**TEAM["principals"], "bob": {"identities": ["slack:U2"]}})
    gate.principals = Principals.model_validate(demoted)
    nope = gate.resolve(ApprovalResolution(asked.approval_id, True, "bob", "slack", "cli"))
    assert not nope.ok and nope.reason == "not_found"
    done = gate.resolve(ApprovalResolution(asked.approval_id, True, "alice", "slack", "cli"))
    assert done.ok, done.reason


def test_owner_only_config_keeps_owner_only_approvers(db: sqlite3.Connection) -> None:
    gate = policy(db, Principals.model_validate(OWNER_ONLY), [])
    assert gate.approvers(None) == ("alice",) and gate.approvers("dori") == ("alice",)


# --- extension checks ---------------------------------------------------------------------


def manifest(approvers: list[str]) -> Manifest:
    return Manifest.model_validate(
        {
            "name": "dori",
            "version": 1,
            "approvers": approvers,
            "tools": {
                "file_ticket": {"run": "./cli.py file", "effect": "write"},
                "prod_deploy": {"run": "./cli.py deploy", "effect": "write"},
            },
        }
    )


def test_a_default_member_cant_be_a_manifest_approver(team: Principals) -> None:
    errors = approver_errors(manifest(["slack:U9", "default", "ops", "bob", "carol"]), team)
    assert errors == [
        "approver 'slack:U9' is a workspace-default member; it never approves",
        "approver 'default' is a workspace-default member; it never approves",
        "approver 'carol' lacks the approve grant for 'dori'",
    ]


def test_a_per_tool_grant_naming_an_unknown_tool_warns(team: Principals) -> None:
    assert grant_warnings(manifest([]), team) == [
        "carol: request grant names unknown tool dori.find_tickets (grants nothing)",
    ]
    typo = dict(TEAM, default={"channel": "slack", "grants": {"dori": {"request": ["fil"]}}})
    warned = grant_warnings(manifest([]), Principals.model_validate(typo))
    assert "default: request grant names unknown tool dori.fil (grants nothing)" in warned


# --- fast path: staff commands, the instance-only pause, start work ---------------------------


@pytest.fixture
def router(db: sqlite3.Connection, team: Principals) -> Router:
    reg = ToolRegistry()
    for spec in BUILTIN_SPECS:
        reg.register(spec)
    for spec in (FILE_TICKET, FIND_TICKETS, PROD_DEPLOY):
        reg.register(spec)

    async def handler(_args: dict[str, Any]) -> dict[str, Any]:
        return {}

    handlers: dict[HandlerKey, ToolHandler] = {(None, s.name): handler for s in BUILTIN_SPECS}
    return Router(db, principals=team, registry=reg, gate=DryRunGate(db, team, reg),
                  handlers=handlers, model=NO_MODEL)  # fmt: skip


NO_MODEL: Any = None  # the fast path never reaches the model


def msg(text: str, principal: str) -> Event:
    payload = {"text": text, "identity": "slack:x", "ref": "r", "truncated": False}
    return Event(source="imessage", type="message", payload=payload, principal=principal)


async def reply(router: Router, text: str, principal: str) -> str:
    task = await router.route(msg(text, principal))
    assert task is not None
    return str(task.checkpoint.get("fast_reply"))


@pytest.mark.parametrize("text", ["/update", "/ext list", "/pulse"])
async def test_approvers_cant_use_admin_commands(router: Router, text: str) -> None:
    assert await reply(router, text, "bob") == NOT_ALLOWED


async def test_approver_pause_is_this_instance_only(db: sqlite3.Connection, router: Router) -> None:
    fanned: list[bool] = []

    async def siblings(paused: bool) -> list[str]:
        fanned.append(paused)
        return ["personal: paused"]

    router.fast_path.siblings = siblings
    killswitch.release(db, actor="test")
    assert await reply(router, "pause", "bob") == KILL_REPLY
    assert kv_get(db, "paused") is True and killswitch.paused_by(db) == "approver:bob"
    assert await reply(router, "resume", "bob") == RESUME_REPLY
    assert kv_get(db, "paused") is False
    assert await reply(router, "/kill", "bob") == KILL_REPLY
    assert await reply(router, "/resume", "bob") == RESUME_REPLY
    assert fanned == []  # never reaches the siblings
    rows = db.execute(
        "SELECT requested_by FROM audit_log WHERE action LIKE 'killswitch.%' ORDER BY id"
    ).fetchall()
    assert {r["requested_by"] for r in rows} >= {"approver:bob"}


async def test_approver_cant_resume_a_pause_from_personal_resonant(
    db: sqlite3.Connection, router: Router
) -> None:
    killswitch.release(db, actor="test")
    killswitch.engage(db, actor=killswitch.SIBLING_SOURCE)
    said = await reply(router, "resume", "bob")
    assert said == SIBLING_PAUSE_REPLY.format(owner="Alice")
    assert said == "Alice paused everything from personal Resonant; he resumes it there."
    assert kv_get(db, "paused") is True
    refused = db.execute(
        "SELECT detail FROM audit_log WHERE action = 'router.resume_refused'"
    ).fetchone()
    assert json.loads(refused["detail"])["paused_by"] == killswitch.SIBLING_SOURCE


@pytest.mark.parametrize("who", ["carol", "slack:U9"])
async def test_members_cant_pause_or_resume(
    db: sqlite3.Connection, router: Router, who: str
) -> None:
    killswitch.release(db, actor="test")
    assert await reply(router, "/kill", who) == NOT_ALLOWED
    assert await reply(router, "/resume", who) == NOT_ALLOWED
    assert router.fast_path.match("pause", who) is None  # plain words: the brain's
    assert kv_get(db, "paused") is False


async def test_owner_plain_pause_is_not_a_command(router: Router) -> None:
    """No change for the owner: ``pause`` is plain words (only ``/kill`` pauses)."""
    assert router.fast_path.match("pause", "alice") is None


@pytest.mark.parametrize("text", ["/run fix the login bug", "/merge R1"])
@pytest.mark.parametrize("who", ["carol", "slack:U9"])
async def test_members_asking_for_work_hear_who_can_start_it(
    router: Router, text: str, who: str
) -> None:
    assert await reply(router, text, who) == (
        "Only Alice or Bob can start work. I can file a ticket for it instead."
    )


async def test_member_without_a_ticket_tool_hears_no_ticket_offer(router: Router) -> None:
    assert await reply(router, "/run fix it", "family") == "Only Alice or Bob can start work."


async def test_approvers_get_staff_commands(router: Router) -> None:
    available = {c.name for c in router.fast_path.available("bob")}
    assert {"/kill", "/resume", "/rules", "/approvals", "/disk"} <= available
    assert not {"/update", "/ext", "/pulse"} & available
    assert not {"/kill", "/rules", "/run"} & {c.name for c in router.fast_path.available("carol")}


# --- review fixes: admin builtins from any path, pause ownership -----------------------------


@pytest.mark.parametrize("name", sorted(ADMIN_TOOLS))
def test_approvers_never_reach_admin_builtins(team: Principals, name: str) -> None:
    spec = tool(name, None)
    assert not team.may_request("bob", spec)
    assert not team.can("bob", "approve", None, tool=name)
    assert team.may_request("alice", spec)


def test_admin_builtins_are_asked_of_the_owner_alone(
    db: sqlite3.Connection, team: Principals
) -> None:
    gate = policy(db, team, [])
    assert gate.approvers(None, "update_resonant") == ("alice",)
    assert gate.approvers(None, "restart_resonant") == ("alice", "bob")


def test_approver_brain_tools_leave_out_admin_builtins(team: Principals) -> None:
    offered = [s for s in BUILTIN_SPECS if team.may_request("bob", s)]
    names = {s.name for s in offered}
    assert "start_run" in names and "merge_run" in names
    assert not names & ADMIN_TOOLS


async def test_approver_cant_resume_the_owners_pause(
    db: sqlite3.Connection, router: Router
) -> None:
    killswitch.release(db, actor="test")
    assert await reply(router, "/kill", "alice") == KILL_REPLY
    assert await reply(router, "resume", "bob") == "Alice paused this; he resumes it."
    assert kv_get(db, "paused") is True


async def test_the_owner_takes_over_an_approvers_pause(
    db: sqlite3.Connection, router: Router
) -> None:
    killswitch.release(db, actor="test")
    assert await reply(router, "pause", "bob") == KILL_REPLY
    assert await reply(router, "/kill", "alice") == KILL_REPLY
    assert killswitch.paused_by(db) == "alice"
    assert await reply(router, "resume", "bob") == "Alice paused this; he resumes it."
    assert kv_get(db, "paused") is True

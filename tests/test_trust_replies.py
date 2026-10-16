"""``yes always`` (EM-4): the parser, the prompt text, and the gate's binding rules. It only
ever answers a worker's trust check: never an L1 or pay approval, never an ordinary L2."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from resonant.gate import ApprovalReply, Intent, NeedsApproval, PolicyGate, ResolveResult
from resonant.gate.codes import approval_text
from resonant.gate.replies import ParsedReply, parse_reply
from resonant.gate.types import REPLY_REFUSALS
from resonant.gateway.channel import ApprovalPrompt
from resonant.loop import NewTask
from resonant.principals import Principals
from resonant.router.approvals import ALWAYS_UNSUPPORTED, refusal_reply
from resonant.store import approvals as ap
from resonant.store import tasks as repo
from resonant.store.db import kv_set
from resonant.tools import ToolRegistry
from resonant.trust.checks import CHECK_TOOL
from resonant_sdk import Effect, Level, ToolSpec

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
SENT = NOW + timedelta(seconds=10)
REPLY = NOW + timedelta(seconds=20)
RESTART = ToolSpec(name="restart_resonant", effect=Effect.WRITE, level=Level.L2)
WIPE = ToolSpec(name="wipe", effect=Effect.WRITE, level=Level.L1)
PAY = ToolSpec(name="pay", effect=Effect.PAY)


# --- parser --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("yes always", None),
        ("Yes Always", None),
        ("yes always.", None),
        ("yes  always!", None),
        ("yes always a7", "A7"),
        ("yes A7 always", "A7"),
        ("A7 yes always", "A7"),
    ],
)
def test_yes_always_parses(text: str, code: str | None) -> None:
    assert parse_reply(text) == ParsedReply("approve", code, always=True)


@pytest.mark.parametrize(
    "text",
    [
        "always",
        "no always",
        "always yes",
        "approve always",
        "\U0001f44d always",
        "yes always?",
        "yes always A1",  # not an approval code
        "yes always A7 C4",
        "yes always with comments: fine",
        "yes, always",
    ],
)
def test_not_yes_always(text: str) -> None:
    parsed = parse_reply(text)
    assert parsed is None or not parsed.always


def test_truncated_is_never_a_reply() -> None:
    assert parse_reply("yes always", truncated=True) is None


def test_plain_replies_are_not_always() -> None:
    for text in ("yes", "yes A7", "A7 yes", "no", "yes with comments: ok"):
        parsed = parse_reply(text)
        assert parsed is not None and not parsed.always


# --- prompt text -----------------------------------------------------------------------


def test_prompt_text() -> None:
    assert approval_text("K4", "R4 wants to: deploy (new system)", always=True) == (
        "[K4] R4 wants to: deploy (new system). Reply yes / yes always / no"
    )
    # Never offered with a code or on a destructive prompt.
    assert "always" not in approval_text("K4", "x", always=True, require_code=True)
    assert "always" not in approval_text("K4", "x", always=True, destructive=True)
    prompt = ApprovalPrompt("a", "K4", "x", "pay", "L1", NOW, require_code=True, always=True)
    assert "always" not in prompt.text()
    prompt = ApprovalPrompt("a", "K4", "x", "write", "L1", NOW, always=True)
    assert "always" not in prompt.text()


# --- binding -------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def gate(db: sqlite3.Connection) -> PolicyGate:
    kv_set(db, "autonomy", "on")
    reg = ToolRegistry()
    for spec in (RESTART, WIPE, PAY):
        reg.register(spec)
    principals = Principals.model_validate({"principals": {"justin": {"role": "owner"}}})
    return PolicyGate(db, principals, reg, dry_run=False, approval_ttl_s=1800, clock=Clock())


def _task(db: sqlite3.Connection) -> str:
    tid = repo.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    repo.set_status(db, tid, "running")
    return tid


def ask(db: sqlite3.Connection, gate: PolicyGate, spec: ToolSpec) -> NeedsApproval:
    tid = _task(db)
    asked = gate.evaluate(Intent.for_tool(spec, task_id=tid, principal="justin", args={"x": 1}))
    assert isinstance(asked, NeedsApproval) and not asked.allow_always
    ap.add_channel_ref(db, asked.approval_id, ref=f"r-{asked.code}", at=SENT, principal="justin")
    return asked


def check_approval(db: sqlite3.Connection, *, allow_always: bool = True) -> ap.Approval:
    """A worker's trust check, as ``TrustChecks`` creates it."""
    tid = _task(db)
    approval_id = ap.insert_approval(
        db,
        ap.NewApproval(
            task_id=tid,
            intent_hash="h" + tid,
            tool=CHECK_TOOL,
            effect=Effect.WRITE,
            level=Level.L2,
            summary="R4 wants to: deploy (new system)",
            approvers=("justin",),
            code="K4",
            expires_at=NOW + timedelta(hours=1),
            allow_always=allow_always,
        ),
        NOW,
    )
    ap.add_channel_ref(db, approval_id, ref="r-K4", at=SENT, principal="justin")
    row = ap.get_approval(db, approval_id)
    assert row is not None
    return row


def always(gate: PolicyGate, code: str | None = None, event_id: str = "e1") -> ResolveResult:
    clock = gate.clock
    assert isinstance(clock, Clock)
    clock.now = REPLY + timedelta(seconds=1)
    return gate.resolve_reply(
        ApprovalReply(
            actor="justin",
            actor_identity="imessage:+15551234567",
            channel="imessage",
            raw_text="yes always",
            decision="approve",
            path="parser",
            event_id=event_id,
            occurred_at=REPLY,
            code=code,
            always=True,
        )
    )


def status(db: sqlite3.Connection, approval_id: str) -> str:
    row = ap.get_approval(db, approval_id)
    assert row is not None
    return row.status


def test_yes_always_binds_a_check(db: sqlite3.Connection, gate: PolicyGate) -> None:
    a = check_approval(db)
    result = always(gate)
    assert result.ok
    row = ap.get_approval(db, a.id)
    assert row is not None and row.status == "approved" and row.always


@pytest.mark.parametrize("spec", [WIPE, PAY])
@pytest.mark.parametrize("with_code", [True, False])
def test_yes_always_never_approves_a_code_required_approval(
    db: sqlite3.Connection, gate: PolicyGate, spec: ToolSpec, with_code: bool
) -> None:
    asked = ask(db, gate, spec)
    assert asked.require_code
    result = always(gate, asked.code if with_code else None)
    assert (result.ok, result.reason) == (False, "always_unsupported")
    assert status(db, asked.approval_id) == "pending"


def test_yes_always_is_not_a_plain_yes_for_an_ordinary_l2(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    asked = ask(db, gate, RESTART)
    for code in (None, asked.code):
        result = always(gate, code, event_id=f"e-{code}")
        assert (result.ok, result.reason) == (False, "always_unsupported")
    assert status(db, asked.approval_id) == "pending"


def test_a_check_without_always(db: sqlite3.Connection, gate: PolicyGate) -> None:
    a = check_approval(db, allow_always=False)
    assert always(gate).reason == "always_unsupported"
    assert status(db, a.id) == "pending"


def test_yes_always_still_needs_the_usual_binding(db: sqlite3.Connection, gate: PolicyGate) -> None:
    check_approval(db)
    ask(db, gate, RESTART)
    assert always(gate).reason == "ambiguous"  # two pending: a code is needed
    assert always(gate, "K4", event_id="e2").ok


def test_the_store_refuses_always_too(db: sqlite3.Connection, gate: PolicyGate) -> None:
    """Defense in depth: the conditional UPDATE re-checks what the gate checked."""
    asked = ask(db, gate, WIPE)
    reason = ap.resolve(
        db, asked.approval_id, approved=True, decided_by="justin", now=REPLY, always=True
    )
    assert reason == "always_unsupported"
    assert status(db, asked.approval_id) == "pending"
    with pytest.raises(ValueError):
        ap.resolve(
            db, asked.approval_id, approved=False, decided_by="justin", now=REPLY, always=True
        )


def test_contract_guards() -> None:
    with pytest.raises(ValueError):
        ap.NewApproval(
            task_id="t",
            intent_hash="h",
            tool="x",
            effect=Effect.WRITE,
            level=Level.L1,
            summary="s",
            approvers=("justin",),
            code="K4",
            expires_at=NOW,
            require_code=True,
            allow_always=True,
        )
    with pytest.raises(ValueError):
        ApprovalReply(
            actor="justin",
            actor_identity="imessage:+1",
            channel="imessage",
            raw_text="no always",
            decision="deny",
            path="parser",
            event_id="e",
            occurred_at=REPLY,
            always=True,
        )
    assert "always_unsupported" in REPLY_REFUSALS


def test_refusal_reply() -> None:
    from resonant.gate import PendingApproval

    coded = PendingApproval("a", "A7", "t", "wipe", Effect.WRITE, Level.L1, True, NOW)
    plain = PendingApproval("b", "C4", "t", "restart", Effect.WRITE, Level.L2, False, NOW)
    assert refusal_reply("always_unsupported", None, [coded]) == (
        f"{ALWAYS_UNSUPPORTED} Reply yes A7 to confirm."
    )
    assert refusal_reply("always_unsupported", "C4", [coded, plain]) == (
        f"{ALWAYS_UNSUPPORTED} Reply yes or no."
    )

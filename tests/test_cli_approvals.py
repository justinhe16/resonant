from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from resonant.cli import EX_TEMPFAIL, app
from resonant.gate import Intent, NeedsApproval, PolicyGate
from resonant.loop import NewTask
from resonant.principals import Principals
from resonant.store import approvals as ap
from resonant.store import tasks as repo
from resonant.store.db import iso, kv_set, open_db, utcnow
from resonant.tools import ToolRegistry
from resonant_sdk import Effect, Level, ToolSpec

ADD = ToolSpec(name="add_item", effect=Effect.WRITE, level=Level.L2)
WIPE = ToolSpec(name="wipe", effect=Effect.WRITE, level=Level.L1)

PRINCIPALS = "principals:\n  justin:\n    role: owner\n"


@pytest.fixture
def db(resonant_home: Path) -> Iterator[sqlite3.Connection]:
    (resonant_home / "principals.yaml").write_text(PRINCIPALS)
    conn = open_db(resonant_home / "resonant.db")
    kv_set(conn, "autonomy", "on")
    yield conn
    conn.close()


@pytest.fixture
def gate(db: sqlite3.Connection) -> PolicyGate:
    registry = ToolRegistry()
    registry.register(ADD)
    registry.register(WIPE)
    principals = Principals.model_validate({"principals": {"justin": {"role": "owner"}}})
    return PolicyGate(db, principals, registry, dry_run=False, approval_ttl_s=1800)


def ask(db: sqlite3.Connection, gate: PolicyGate, spec: ToolSpec = ADD, **args: object) -> str:
    """A waiting task with one pending approval. Returns the approval id."""
    task_id = repo.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    decision = gate.evaluate(
        Intent.for_tool(spec, task_id=task_id, principal="justin", args=dict(args) or {"x": 1})
    )
    assert isinstance(decision, NeedsApproval)
    repo.set_status(db, task_id, "waiting", wait_reason="approval")
    return decision.approval_id


def get(db: sqlite3.Connection, approval_id: str) -> ap.Approval:
    a = ap.get_approval(db, approval_id)
    assert a is not None
    return a


def run(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, ["approvals", *args])
    return result.exit_code, result.stdout, result.stderr


# --- list / show -----------------------------------------------------------------------


def test_list_empty(db: sqlite3.Connection) -> None:
    assert run("list") == (0, "no pending approvals\n", "")
    code, out, _ = run("list", "--json")
    assert (code, json.loads(out)) == (0, [])


def test_list_shows_pending_only(db: sqlite3.Connection, gate: PolicyGate) -> None:
    first = get(db, ask(db, gate, item="milk"))
    done = ask(db, gate, item="eggs")
    assert run("approve", done)[0] == 0
    code, out, _ = run("list")
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"{first.code} ")
    for part in ("add_item", "29m", first.summary, first.id):
        assert part in lines[0]

    code, out, _ = run("list", "--json")
    [row] = json.loads(out)
    assert row["id"] == first.id
    assert row["code"] == first.code
    assert row["tool"] == "add_item"
    assert row["summary"] == first.summary
    assert 1700 < row["expires_in_s"] <= 1800


def test_show_has_full_args(db: sqlite3.Connection, gate: PolicyGate) -> None:
    a = get(db, ask(db, gate, item="vitamin d3"))
    assert "vitamin ?" in a.summary  # the summary masks it
    code, out, _ = run("show", a.code or "")
    assert code == 0
    assert a.id in out
    assert "pending, expires in 29m" in out
    assert '"item": "vitamin d3"' in out

    code, out, _ = run("show", a.id, "--json")
    data = json.loads(out)
    assert code == 0
    assert data["args"] == {"item": "vitamin d3"}
    assert data["status"] == "pending"


def test_show_resolved_by_id(db: sqlite3.Connection, gate: PolicyGate) -> None:
    approval_id = ask(db, gate)
    code = get(db, approval_id).code or ""
    assert run("deny", approval_id)[0] == 0
    _, out, _ = run("show", approval_id)
    assert "rejected by justin" in out
    # A resolved approval's code no longer matches anything.
    assert run("show", code)[0] == 1


# --- approve / deny --------------------------------------------------------------------


def test_approve_by_code_wakes_task(db: sqlite3.Connection, gate: PolicyGate) -> None:
    a = get(db, ask(db, gate, item="milk"))
    code, out, err = run("approve", (a.code or "").lower())
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == f"[{a.code}] {a.summary}"
    assert "approved" in out.splitlines()[1]
    done = get(db, a.id)
    assert (done.status, done.decided_by) == ("approved", "justin")
    assert a.task_id is not None
    task = repo.get_task(db, a.task_id)
    assert task is not None and task.status == "queued"
    events = repo.pending_events(db, a.task_id)
    assert [(e.type, e.payload["approved"]) for e in events] == [("approval.reply", True)]
    row = db.execute(
        "SELECT detail FROM audit_log WHERE action = 'approval.resolve' ORDER BY id DESC"
    ).fetchone()
    detail = json.loads(row["detail"])
    assert (detail["path"], detail["actor_identity"], detail["ok"]) == ("cli", "cli", True)


def test_deny_by_id_wakes_task(db: sqlite3.Connection, gate: PolicyGate) -> None:
    a = get(db, ask(db, gate, WIPE))
    assert a.require_code
    code, out, _ = run("deny", a.id)
    assert code == 0
    assert "denied" in out
    assert get(db, a.id).status == "rejected"
    assert a.task_id is not None
    events = repo.pending_events(db, a.task_id)
    assert [(e.type, e.payload["approved"]) for e in events] == [("approval.reply", False)]


def test_unknown_code(db: sqlite3.Connection, gate: PolicyGate) -> None:
    ask(db, gate)
    code, out, err = run("approve", "Z9")
    assert (code, out) == (1, "")
    assert "no approval with id Z9 or pending code Z9" in err


def test_reused_code_resolves_the_pending_one(db: sqlite3.Connection, gate: PolicyGate) -> None:
    old = ask(db, gate, item="old")
    assert run("deny", old)[0] == 0
    new = ask(db, gate, item="new")
    db.execute("UPDATE approvals SET code = ? WHERE id = ?", (get(db, old).code, new))
    assert run("approve", get(db, old).code or "")[0] == 0
    assert (get(db, old).status, get(db, new).status) == ("rejected", "approved")


def test_ambiguous_code_resolves_nothing(db: sqlite3.Connection, gate: PolicyGate) -> None:
    # The unique index makes this impossible; the CLI still refuses rather than guess.
    db.execute("DROP INDEX approvals_pending_code")
    one, two = ask(db, gate, item="a"), ask(db, gate, item="b")
    db.execute("UPDATE approvals SET code = 'A7' WHERE id IN (?, ?)", (one, two))
    code, _, err = run("approve", "A7")
    assert code == 1
    assert "ambiguous" in err
    assert {get(db, one).status, get(db, two).status} == {"pending"}


def test_already_resolved(db: sqlite3.Connection, gate: PolicyGate) -> None:
    approval_id = ask(db, gate)
    assert run("approve", approval_id)[0] == 0
    code, out, err = run("deny", approval_id)
    assert code == 1
    assert out.startswith("[")  # the summary is still shown
    assert "not denied: already approved" in err
    assert get(db, approval_id).status == "approved"


def test_expired(db: sqlite3.Connection, gate: PolicyGate) -> None:
    approval_id = ask(db, gate)
    past = iso(utcnow() - timedelta(minutes=1))
    db.execute("UPDATE approvals SET expires_at = ? WHERE id = ?", (past, approval_id))
    code, _, err = run("approve", approval_id)
    assert code == 1
    assert "not approved: expired" in err
    assert get(db, approval_id).status == "expired"


@pytest.mark.parametrize("args", [["list"], ["show", "A7"], ["approve", "A7"], ["deny", "A7"]])
def test_missing_store_is_not_created(resonant_home: Path, args: list[str]) -> None:
    code, _, err = run(*args)
    assert code == EX_TEMPFAIL
    assert "does not exist" in err
    assert not (resonant_home / "resonant.db").exists()


# --- approve --always (PER-261) ----------------------------------------------------------


def offered(db: sqlite3.Connection, *, scope: str | None = "linear") -> str:
    """A waiting task with one pending approval whose prompt offered ``yes always`` (an L2
    chat tool's standing allow, or with ``scope=None`` a worker's trust check)."""
    task_id = repo.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    now = utcnow()
    approval_id = ap.insert_approval(
        db,
        ap.NewApproval(
            task_id=task_id,
            intent_hash="h-always",
            tool="save_issue",
            effect=Effect.WRITE,
            level=Level.L2,
            summary="linear: save issue id=RES-1",
            approvers=("justin",),
            code="K4",
            expires_at=now + timedelta(minutes=30),
            extension="linear",
            requested_by="justin",
            allow_always=True,
            always_scope=scope,
            always_action="tool:linear.save_issue" if scope else None,
        ),
        now,
    )
    repo.set_status(db, task_id, "waiting", wait_reason="approval")
    db.commit()
    return approval_id


def test_approve_always_saves_the_offered_rule(db: sqlite3.Connection) -> None:
    from resonant.store import trust as trust_store

    approval_id = offered(db)
    code, out, err = run("approve", "K4", "--always")
    assert (code, err) == (0, "")
    assert "approved (yes always); task woken" in out
    assert "rule added: don't ask before running linear.save_issue in linear (rule 1)" in out
    a = get(db, approval_id)
    assert (a.status, a.always) == ("approved", True)
    rules = [(r.scope, r.action, r.effect) for r in trust_store.list_rules(db)]
    assert rules == [("linear", "tool:linear.save_issue", "allow")]
    (row,) = db.execute("SELECT detail FROM audit_log WHERE action = 'approval.resolve'").fetchall()
    assert json.loads(row["detail"])["always"] is True


def test_approve_always_on_a_trust_check_records_it_without_a_rule(
    db: sqlite3.Connection,
) -> None:
    approval_id = offered(db, scope=None)
    code, out, _ = run("approve", approval_id, "--always")
    assert code == 0 and "rule added" not in out
    assert get(db, approval_id).always is True


def test_approve_always_is_refused_where_not_offered(
    db: sqlite3.Connection, gate: PolicyGate
) -> None:
    approval_id = ask(db, gate)  # an L2 change with no standing allow on offer
    code, _, err = run("approve", approval_id, "--always")
    assert code == 1 and "not approved: it doesn't offer yes always" in err
    assert get(db, approval_id).status == "pending"
    wipe = ask(db, gate, WIPE)  # L1: never
    assert run("approve", wipe, "--always")[0] == 1

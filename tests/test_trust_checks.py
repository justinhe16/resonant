"""Worker trust checks (EM-4): ``TrustChecks.request_check`` end to end, through the real
gate binding and the router's approval inbox (``yes``, ``yes always``, ``no``)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from resonant.gate import PolicyGate
from resonant.gateway.channel import ApprovalPrompt, ChannelRefused, MessageRef, approval_dedupe_key
from resonant.loop import NewTask
from resonant.principals import Principal, Principals
from resonant.router.approvals import ALWAYS_UNSUPPORTED, ApprovalInbox
from resonant.store import approvals as ap
from resonant.store import runs as runs_store
from resonant.store import tasks as repo
from resonant.store import trust as store
from resonant.store.db import kv_set
from resonant.tools import ToolRegistry
from resonant.trust.checks import CHECK_TOOL, TrustChecks, check_summary
from resonant_sdk import Event

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
OWNER = "justin"
HANDLE = "+15551234567"
TTL_S = 1800.0


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Channel:
    name = "imessage"

    def __init__(self) -> None:
        self.prompts: list[ApprovalPrompt] = []
        self.sent: list[tuple[str, str, str | None]] = []
        self.unreachable = False
        self._refs: dict[str, MessageRef] = {}

    async def start(self, submit: Any) -> None:  # pragma: no cover - unused
        pass

    async def stop(self) -> None:  # pragma: no cover - unused
        pass

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef:
        if dedupe_key is not None and dedupe_key in self._refs:
            return self._refs[dedupe_key]
        self.sent.append((principal, text, dedupe_key))
        ref = MessageRef("imessage", f"out:{len(self.sent)}")
        if dedupe_key is not None:
            self._refs[dedupe_key] = ref
        return ref

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        if self.unreachable:
            raise ChannelRefused("no identity")
        key = approval_dedupe_key(prompt.approval_id, principal)
        if key not in self._refs:
            self.prompts.append(prompt)
        return await self.send(principal, prompt.text(), dedupe_key=key)


class Harness:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.clock = Clock()
        self.channel = Channel()
        self.principals = Principals(
            principals={OWNER: Principal(role="owner", identities=(f"imessage:{HANDLE}",))}
        )
        kv_set(db, "autonomy", "on")
        self.gate = PolicyGate(
            db,
            self.principals,
            ToolRegistry(),
            dry_run=False,
            approval_ttl_s=TTL_S,
            clock=self.clock,
        )
        self.inbox = ApprovalInbox(db, principals=self.principals, gate=self.gate)
        self.checks = TrustChecks(
            db,
            self.channel,  # pyright: ignore[reportArgumentType]
            self.principals,
            approval_ttl_s=TTL_S,
            clock=self.clock,
            poll_s=0.001,
        )
        self.n = 0

    def run(self, repo_name: str = "dori", status: runs_store.Status = "running") -> str:
        tid = repo.insert_task(self.db, NewTask(kind="run", runner="claude", priority=1))
        repo.set_status(self.db, tid, "running")
        run = runs_store.create(
            self.db, repo=repo_name, ask="x", principal=OWNER, session_id="s", max_seconds=60
        )
        runs_store.update(self.db, run.code, task_id=tid, status=status)
        return run.code

    def start(self, code: str, **kw: Any) -> asyncio.Task[str]:
        args: dict[str, Any] = {"system": "dori-api", "action": "deploy"} | kw
        return asyncio.ensure_future(self.checks.request_check(code, **args))

    async def prompted(self, n: int = 1) -> ApprovalPrompt:
        for _ in range(500):
            if len(self.channel.prompts) >= n:
                return self.channel.prompts[n - 1]
            await asyncio.sleep(0.001)
        raise AssertionError("no prompt")

    def reply(self, text: str) -> str | None:
        """The owner texts ``text`` a few seconds from now; returns the inbox's reply."""
        self.n += 1
        self.clock.now += timedelta(seconds=5)
        event = Event(
            source="imessage",
            type="message",
            principal=OWNER,
            payload={"text": text, "handle": HANDLE, "guid": f"G{self.n}"},
            dedupe_key=f"imessage:G{self.n}",
            occurred_at=self.clock.now,
        )
        self.clock.now += timedelta(seconds=1)
        result = self.inbox.handle(event, text, OWNER)
        assert result is not None, f"{text!r} wasn't read as a reply"
        return result.reply


@pytest.fixture
def h(db: sqlite3.Connection) -> Harness:
    return Harness(db)


def audits(db: sqlite3.Connection, action: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT decision, approved_by, detail FROM audit_log WHERE action = ? ORDER BY id",
        (action,),
    ).fetchall()
    return [
        {"decision": r["decision"], "approved_by": r["approved_by"], **json.loads(r["detail"])}
        for r in rows
    ]


async def test_ok_returns_at_once_without_asking(h: Harness) -> None:
    store.mark_seen(h.db, "dori-api", "deploy", NOW)
    code = h.run()
    assert await h.checks.request_check(code, "dori-api", "deploy") == "ok"
    assert h.channel.prompts == []


async def test_yes_allows_once_and_marks_seen(h: Harness) -> None:
    code = h.run()
    task = h.start(code, what="deploy dori-api to prod")
    prompt = await h.prompted()
    assert prompt.text() == (
        f'[{prompt.code}] {code} wants to deploy on dori-api: "deploy dori-api to prod" '
        "(new system; yes always = don't ask before deploy in dori-api). "
        "Reply yes / yes always / no"
    )
    assert not prompt.require_code and prompt.level == "L2"
    assert h.reply("yes") is None  # resolved: the check confirms by returning
    assert await task == "ok"
    approval = ap.get_approval(h.db, prompt.approval_id)
    assert approval is not None and approval.tool == CHECK_TOOL
    assert approval.used_at is not None and not approval.always
    assert store.list_rules(h.db) == []
    assert store.seen_actions(h.db, "dori-api") == {"deploy"}
    # Seen now: the same check doesn't ask again (it isn't net new), a new kind does.
    assert await h.checks.request_check(code, "dori-api", "deploy") == "ok"
    assert [a["decision"] for a in audits(h.db, "trust.check")] == ["ok"]
    assert [a["decision"] for a in audits(h.db, "trust.evaluate")] == ["ask", "ok"]


async def test_yes_always_adds_the_rule_and_confirms(h: Harness) -> None:
    code = h.run()
    task = h.start(code, irreversible=True)
    prompt = await h.prompted()
    assert "wants to deploy on dori-api (new system, irreversible; yes always" in prompt.text()
    assert h.reply(f"yes always {prompt.code}") is None
    assert await task == "ok"
    (rule,) = store.list_rules(h.db)
    assert (rule.scope, rule.action, rule.effect, rule.created_from) == (
        "dori-api",
        "deploy",
        "allow",
        "yes always",
    )
    assert h.channel.sent[-1] == (
        OWNER,
        f"Rule added: don't ask before deploy in dori-api (rule {rule.id})",
        f"trust:{prompt.approval_id}:rule",
    )
    # The rule overrides the irreversible default next time.
    assert await h.checks.request_check(code, "dori-api", "deploy", irreversible=True) == "ok"
    (added,) = audits(h.db, "trust.rule_added")
    assert added["approved_by"] == OWNER and added["approval_id"] == prompt.approval_id
    resolved = audits(h.db, "approval.resolve")
    assert resolved[-1]["always"] is True and resolved[-1]["decision"] == "approved"


@pytest.mark.parametrize("text", ["no", "no {code}", "deny"])
async def test_no_denies(h: Harness, text: str) -> None:
    code = h.run()
    task = h.start(code)
    prompt = await h.prompted()
    h.reply(text.format(code=prompt.code))
    assert await task == "denied"
    assert store.seen_actions(h.db, "dori-api") == frozenset()
    assert audits(h.db, "trust.check")[-1]["decision"] == "denied"


async def test_expiry_denies(h: Harness) -> None:
    code = h.run()
    task = h.start(code)
    await h.prompted()
    h.clock.now += timedelta(seconds=TTL_S + 1)
    assert await task == "denied"


async def test_an_owner_ask_rule_offers_no_yes_always(h: Harness) -> None:
    store.add_rule(h.db, scope="dori", action="merge", effect="ask", created_from="chat", now=NOW)
    store.mark_seen(h.db, "dori", "merge", NOW)
    code = h.run("dori")
    task = h.start(code, system="dori", action="merge")
    prompt = await h.prompted()
    assert prompt.text().endswith("? Reply yes / no")
    assert "(rule 1: ask before merge in dori)" in prompt.text()
    assert h.reply("yes always") == f"{ALWAYS_UNSUPPORTED} Reply yes or no."
    assert not task.done()
    approval = ap.get_approval(h.db, prompt.approval_id)
    assert approval is not None and approval.status == "pending"
    h.reply("yes")
    assert await task == "ok"
    assert [r.effect for r in store.list_rules(h.db)] == ["ask"]  # unchanged


async def test_a_repeated_check_reuses_the_pending_approval(h: Harness) -> None:
    code = h.run()
    first = h.start(code, what="deploy")
    p1 = await h.prompted()
    second = h.start(code, what="deploy")
    await asyncio.sleep(0.01)
    assert len(h.channel.prompts) == 1
    h.reply("yes always")
    assert (await first, await second) == ("ok", "ok")  # both waiters share the answer
    assert len(store.list_rules(h.db)) == 1
    assert sum(1 for _, _, key in h.channel.sent if key and key.endswith(":rule")) == 1
    approval = ap.get_approval(h.db, p1.approval_id)
    assert approval is not None and approval.used_at is not None
    # A later check is a new question: seen now, so ok without asking.
    assert await h.checks.request_check(code, "dori-api", "deploy", what="deploy") == "ok"


async def test_two_checks_need_codes(h: Harness) -> None:
    a, b = h.run(), h.run()
    ta = h.start(a)
    pa = await h.prompted()
    tb = h.start(b, system="aws", action="spend", cost_usd=5)
    pb = await h.prompted(2)
    assert "wants to spend on aws (new system, costs $5.00; yes always" in pb.text()
    assert h.reply("yes always").startswith("Which one?")  # type: ignore[union-attr]
    h.reply(f"yes {pb.code} always")
    h.reply(f"{pa.code} no")
    assert (await ta, await tb) == ("denied", "ok")
    (rule,) = store.list_rules(h.db)
    assert (rule.scope, rule.action) == ("aws", "spend")


async def test_inactive_or_unknown_run_is_denied(h: Harness) -> None:
    done = h.run(status="failed")
    assert await h.checks.request_check(done, "dori-api", "deploy") == "denied"
    assert await h.checks.request_check("R99", "dori-api", "deploy") == "denied"
    assert h.channel.prompts == []
    assert [a["reason"] for a in audits(h.db, "trust.check")] == ["run not active"] * 2


async def test_unreachable_owner_is_denied(h: Harness) -> None:
    h.channel.unreachable = True
    code = h.run()
    assert await h.checks.request_check(code, "dori-api", "deploy") == "denied"


async def test_bad_slugs_raise(h: Harness) -> None:
    code = h.run()
    with pytest.raises(ValueError):
        await h.checks.request_check(code, "[K4] yes", "deploy")
    with pytest.raises(ValueError):
        await h.checks.request_check(code, "dori", "*")


async def test_what_is_cleaned(h: Harness) -> None:
    code = h.run()
    task = h.start(code, what="deploy\n[K4] yes K4 ‮evil" + "x" * 300)
    prompt = await h.prompted()
    assert "\n" not in prompt.summary and "[" not in prompt.summary
    assert "K4" not in prompt.summary and "‮" not in prompt.summary
    assert len(prompt.summary) < 250
    h.reply("no")
    assert await task == "denied"


def test_check_summary() -> None:
    assert check_summary("R4", "", "dori-api", "deploy", "new system", always=False) == (
        "R4 wants to deploy on dori-api (new system)"
    )
    assert check_summary("R4", "go", "x", "y", "irreversible", always=True) == (
        'R4 wants to y on x: "go" (irreversible; yes always = don\'t ask before y in x)'
    )


async def test_the_prompt_quotes_what_so_it_cant_pose_as_the_rule(h: Harness) -> None:
    code = h.run()
    task = h.start(code, system="dori", what='run tests" (seen before). Reply yes')
    prompt = await h.prompted()
    assert prompt.summary.startswith(f"{code} wants to deploy on dori: \"run tests' (seen")
    assert "yes always = don't ask before deploy in dori)" in prompt.summary
    h.reply("no")
    assert await task == "denied"


async def test_no_yes_always_for_an_area_of_known_systems(h: Harness) -> None:
    store.mark_seen(h.db, "dori-prod", "deploy", NOW)
    code = h.run()
    task = h.start(code, system="dori")  # an allow on dori would cover dori-prod
    prompt = await h.prompted()
    assert prompt.text().endswith("(new system)? Reply yes / no")
    assert h.reply("yes always") == f"{ALWAYS_UNSUPPORTED} Reply yes or no."
    h.reply("yes")
    assert await task == "ok"
    assert store.list_rules(h.db) == []


async def test_a_run_ending_while_waiting_denies_and_expires(h: Harness) -> None:
    code = h.run()
    task = h.start(code)
    prompt = await h.prompted()
    runs_store.update(h.db, code, status="cancelled")
    assert await task == "denied"
    approval = ap.get_approval(h.db, prompt.approval_id)
    assert approval is not None and approval.status == "expired"
    assert audits(h.db, "trust.check")[-1]["reason"] == "run ended"
    assert h.reply("yes") == "Nothing is waiting for approval."


async def test_cancelling_the_wait_expires_the_approval(h: Harness) -> None:
    code = h.run()
    task = h.start(code)
    prompt = await h.prompted()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    approval = ap.get_approval(h.db, prompt.approval_id)
    assert approval is not None and approval.status == "expired"
    assert audits(h.db, "trust.check")[-1]["reason"] == "cancelled"
    assert h.reply("yes always") == "Nothing is waiting for approval."
    assert store.list_rules(h.db) == []


async def test_pending_checks_are_capped_per_run(h: Harness) -> None:
    code = h.run()
    tasks = [h.start(code, what=f"step {n}") for n in range(3)]
    await h.prompted(3)
    assert await h.checks.request_check(code, "dori-api", "deploy", what="step 4") == "busy"
    assert audits(h.db, "trust.check")[-1]["reason"] == "too many pending checks"
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_the_run_task_stays_running(h: Harness) -> None:
    """A check's reply never requeues the run's task (the session is alive, running)."""
    code = h.run()
    run = runs_store.get(h.db, code)
    assert run is not None and run.task_id is not None
    task = h.start(code)
    await h.prompted()
    h.reply("yes")
    assert await task == "ok"
    row = h.db.execute("SELECT status FROM tasks WHERE id = ?", (run.task_id,)).fetchone()
    assert row["status"] == "running"


async def test_an_existing_rule_is_not_re_announced(h: Harness) -> None:
    code = h.run()
    first = h.start(code, what="a")
    await h.prompted()
    # The same rule lands meanwhile (another check, or chat).
    store.add_rule(
        h.db, scope="dori-api", action="deploy", effect="allow", created_from="chat", now=NOW
    )
    h.reply("yes always")
    assert await first == "ok"
    assert not any(key and key.endswith(":rule") for _, _, key in h.channel.sent)
    assert audits(h.db, "trust.rule_added") == []


async def test_no_yes_always_for_an_area_of_the_runs_repo(h: Harness) -> None:
    code = h.run("dori-api")
    task = h.start(code, system="dori")  # first check ever: only the repo shows the overlap
    prompt = await h.prompted()
    assert prompt.text().endswith("(new system)? Reply yes / no")
    h.reply("no")
    assert await task == "denied"

"""PER-342: replays of Cori's 2026-11-25 Slack threads, end to end on the team paths.

A day of regressions on the Cori instance (the Slack team bot) reached people before CI: the
guard swapped correct replies for its canned fallback, run-start lines came twice, a stray
``On it.`` followed the run lines, and a bare ``yes`` was eaten by the approval handler. The
other e2e files mostly play the owner on iMessage; these play a team thread.

The world is Cori-shaped: the real daemon (router, gate, loop, ``ClaudeRunner`` with the fake
worker, tool bridge, executor) over ``e2e.FakeChannel("slack")`` only, with an owner
(justin), an approver (brian) and a member (miu, a read-only github grant, so she reaches
Claude); ``gate.tainted_writes: allow``; ``github/token`` minted per call by the instance's
GitHub App (``FakeGitHubApi``) for the real ``extensions/github`` over the fake ``gh``; and
Cori's own ``config/cori/brain-rules.md``. The brain is ``e2e/fake_claude.py`` acting out a
plan through the real MCP relay. ``runner.update_voice`` stays ``template`` (code-built run
lines). Nothing touches the real ~/.resonant, ~/code, Keychain, GitHub or claude.

1. robot-surf three tickets: one start line per run, the reply as written, no ``On it.`` after
   (#193, #195).
2. A retry after failed runs starts new runs; Cori's rule to retry reaches Claude (#190).
3. An approver merges a PR no run opened: ``github.merge_pr``, App-minted (PER-335/336).
4. A bare ``yes`` in a thread with nothing pending goes to the brain (#193).
5. Guard replays: denials, conditional promises and an announced start go out unchanged
   (#190, #195); ``brain.reply_guard: lenient`` sends a flagged reply in Claude's words
   (PER-340).
6. A member can't start a run (PER-293).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import sys
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_e2e_runner import FAKE_ASK, REPO, FakeChatDB, RunModel, World, chat, make_world
from test_ext_app_secrets import FAKE_GH, GITHUB
from test_runner_claude import until

from resonant.brain import guard
from resonant.config import DEFAULT_READ_DENY, ChannelsConfig
from resonant.daemon import Daemon
from resonant.e2e import FakeChannel
from resonant.e2e.extensions import install_fixture
from resonant.extensions.app_secrets import GitHubAppMinter
from resonant.ops.fakes import FakeOpsRunner
from resonant.principals import load_principals
from resonant.router.approvals import NOTHING_WAITING
from resonant.runner.github_app import GitHubApp
from resonant.runners import ask as ask_mod
from resonant.runners.ask import AskRunner, AskScope
from resonant.secrets import SecretStore
from resonant.store import runs as runs_store
from resonant.store.db import open_db
from resonant.wiring import tool_access

__all__ = ["chat"]  # the fixture

JUSTIN, BRIAN, MIU = "U1", "U2", "U3"
TEAM = f"""\
principals:
  justin: {{role: owner, identities: ["slack:{JUSTIN}"]}}
  brian: {{role: approver, identities: ["slack:{BRIAN}"]}}
  miu:
    identities: ["slack:{MIU}"]
    grants: {{github: {{request: [pr]}}}}
"""
RULES = Path(__file__).resolve().parents[1] / "config/cori/brain-rules.md"
PR_REPO = "own/voyager-web"  # FakeGitHubApi's installation account is "own"
HEAD = "a1b2c3d" + "0" * 33
CLAUDE = {"handler": "claude", "kind": "general", "message_type": "other", "say": "on it"}
# What Cori's brain-rules say a robot-surf worker's ask is (never paraphrased)
SURF = (
    "Run /robot-surf {t}. Follow every step of the skill, including code review and lint. "
    "Open a pull request; do not merge, deploy or release."
)
FALLBACKS = (guard.CLAIM_FALLBACK, guard.NUMBER_FALLBACK, guard.ALLOW_FALLBACK,
             guard.CALL_FALLBACK)  # fmt: skip


def surf(ticket: str) -> dict[str, Any]:
    return {"name": "start_run", "args": {"repo": REPO, "ask": SURF.format(t=ticket)}}


def thread(n: int) -> str:
    return f"C0CORI:1764100000.{n:06d}"


@dataclass
class Cori:
    w: World
    daemon: Daemon
    fake: FakeChannel
    asks: AskRunner
    notes: Path
    gh_state: Path

    @property
    def conn(self) -> sqlite3.Connection:
        return self.daemon.conn

    def claude(self, **plan: Any) -> None:
        """What the fake Claude brain does with the next hand-off."""
        self.w.claude(**plan)

    def said(self, where: str, since: int = 0) -> list[str]:
        return [m.text for m in self.fake.sent[since:] if m.thread == where]

    def runs(self) -> list[runs_store.Run]:
        codes = [r["code"] for r in self.conn.execute("SELECT code FROM runs ORDER BY rowid")]
        return [r for c in codes if (r := runs_store.get(self.conn, c)) is not None]

    def guard_spans(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT attrs FROM spans WHERE name = 'runner.ask.guard' ORDER BY rowid"
        ).fetchall()
        return [json.loads(r["attrs"]) for r in rows]

    def seen(self) -> dict[str, Any]:
        """What the fake Claude saw on its last turn (argv, prompt, call results)."""
        return json.loads((self.notes / "session.json").read_text())

    def gh_calls(self) -> list[dict[str, Any]]:
        path = self.gh_state.parent / "calls.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    async def settled(self) -> None:
        """Every ask task has finished and the runner is idle (its last sends are out)."""

        def done() -> bool:
            rows = self.conn.execute("SELECT status FROM tasks WHERE runner = 'ask'").fetchall()
            return bool(rows) and all(r["status"] in ("done", "failed") for r in rows)

        await until(done, 30, "the ask tasks to finish")
        await self.asks.idle()

    async def ask(
        self, uid: str, text: str, where: str, expect: Callable[[list[str]], bool], what: str
    ) -> list[str]:
        """``uid`` says ``text`` in thread ``where``; wait until ``expect`` holds over what was
        said there since, then for the brain to settle; return what was said there."""
        n = len(self.fake.sent)
        assert self.fake.inject(uid, text, thread=where), f"{uid} wasn't submitted"
        try:
            await until(lambda: expect(self.said(where, n)), 30, what)
        except AssertionError as e:  # say what was said instead, everywhere
            sent = [(m.thread, m.text) for m in self.fake.sent]
            raise AssertionError(f"{e}; sent: {sent}") from None
        await self.settled()
        await asyncio.sleep(0.3)  # anything late (a second line, an ack) would land by now
        return self.said(where, n)


def attach_brain(
    w: World, daemon: Daemon, notes: Path, *, reply_guard: str, ack_after_s: float
) -> AskRunner:
    """``test_e2e_runner.attach_claude`` as Cori runs it (``wiring``): the Claude brain
    session (only its turns are guarded; ``attach_claude``'s one-shot asks aren't), Cori's
    brain-rules, the team's principals, ``brain.reply_guard``, and the fake's notes."""
    assert daemon.imessage is not None and daemon.state.components is not None
    brain = daemon.imessage.brain
    assert brain is not None
    brain.runner_gaps = None
    if not w.plan.exists():
        w.claude(answer="Nothing to report.")
    settings = w.settings
    asks = AskRunner(
        daemon.conn,
        cfg=settings.brain.ask_claude,
        scope=AskScope((settings.runner.code_dir,), DEFAULT_READ_DENY),
        ask_root=w.root / "ask",
        channel=daemon.imessage.channel,
        secrets=SecretStore(w.keychain),
        owner="justin",
        create_task=daemon.loop.create_task,
        claude_cmd=[
            sys.executable,
            "-I",
            str(FAKE_ASK),
            "--script",
            str(w.plan),
            "--notes",
            str(notes),
        ],
        home=w.root / "claude-home",
        tools=tool_access(daemon.state.components, brain, settings),
        session=settings.brain.claude_session.model_copy(update={"ack_after_s": ack_after_s}),
        reply_guard=reply_guard,
        principals=load_principals(settings.principals_path),
        instructions_file=RULES,
        poll_s=0.05,
        kill_grace_s=0.5,
        bridge_root=w.root / "bridges",
    )
    daemon.state.components.runners[asks.name] = asks
    daemon.loop.runners[asks.name] = asks
    return asks


def pr_state() -> dict[str, Any]:
    """The fake ``gh``'s world: PR 159 in voyager-web, opened by a person (no run), green,
    approved and CLEAN. Any App-minted token is accepted."""
    pr = {
        "number": 159, "title": "Voyager onboarding", "url": f"https://github.com/{PR_REPO}/pull/159",
        "state": "OPEN", "isDraft": False, "author": {"login": "brian"},
        "headRefName": "onboarding", "headRefOid": HEAD, "baseRefName": "main",
        "reviewDecision": "APPROVED", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED",
                               "conclusion": "SUCCESS"}],
    }  # fmt: skip
    tokens = [f"ghs_{n:04d}" + "Tk" * 17 for n in range(1, 40)]
    return {"token": "unused", "tokens": tokens, "prs": {f"{PR_REPO}#159": pr}}


def install_github(w: World, tmp_path: Path) -> Path:
    """The real ``extensions/github`` with a pinned fake ``gh``; returns its state file."""
    state = tmp_path / "gh-ext" / "state.json"
    state.parent.mkdir()
    state.write_text(json.dumps(pr_state()))
    shim = tmp_path / "gh-ext" / "gh"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I -S "{FAKE_GH}" "{state}" "$@"\n')
    shim.chmod(0o755)
    files = {rel: (GITHUB / rel).read_text() for rel in ("cli.py", "README.md")}
    conn = open_db(w.settings.db_path)
    try:
        install_fixture(w.settings, conn, (GITHUB / "resonant.yaml").read_text(), files,
                        argv0_paths={"bin:gh": str(shim)})  # fmt: skip
    finally:
        conn.close()
    return state


@contextlib.asynccontextmanager
async def cori(
    tmp_path: Path,
    home: Path,
    chat: FakeChatDB,
    mp: pytest.MonkeyPatch,
    *,
    scenario: str = "happy",
    reply_guard: str = "strict",
    ack_after_s: float = 8.0,
) -> AsyncGenerator[Cori]:
    """The Cori-shaped daemon (see the module docstring), stopped on exit. ``ack_after_s``:
    ``brain.claude_session.ack_after_s`` (when a slow turn's ``On it.`` is due)."""
    w = make_world(tmp_path, home, chat, mp, scenario=scenario)
    (home / "principals.yaml").write_text(TEAM)
    s = w.settings
    w.settings = s.model_copy(update={
        "channels": ChannelsConfig(),  # Slack only
        "gate": s.gate.model_copy(update={"tainted_writes": "allow"}),
        "extensions": s.extensions.model_copy(
            update={"secret_sources": {"github/token": "github_app"}}),
    })  # fmt: skip
    gh_state = install_github(w, tmp_path)
    store = SecretStore(w.keychain)
    fake = FakeChannel("slack", load_principals(home / "principals.yaml"), vouch=True)
    model = RunModel()
    model.triage = dict(CLAUDE)
    daemon = w.daemon = Daemon(w.settings, model=model, osa_runner=w.osa,
                               ops_runner=FakeOpsRunner(), secrets=store,
                               channels={"slack": fake})  # fmt: skip
    minter = store.minter
    assert isinstance(minter, GitHubAppMinter) and minter.mints("github/token")
    assert w.settings.runner.github_app is not None
    minter.app = GitHubApp(w.settings.runner.github_app, store, transport=w.api.transport(),
                           backoff_s=0)  # fmt: skip
    notes = tmp_path / "claude-notes"
    notes.mkdir()
    asks = attach_brain(w, daemon, notes, reply_guard=reply_guard, ack_after_s=ack_after_s)
    task = asyncio.create_task(daemon.run())
    assert daemon.stack is not None
    hub = daemon.stack.channel
    await until(lambda: hub.health()["last_read_at"] is not None, 10, "the channels to read")
    try:
        yield Cori(w, daemon, fake, asks, notes, gh_state)
    finally:
        if not task.done():
            daemon.stop()
        assert await asyncio.wait_for(task, 20) == 0
        w.daemon = None


def has(text: str) -> Callable[[list[str]], bool]:
    return lambda said: text in said


def start_lines(said: list[str]) -> list[str]:
    return [t for t in said if t.startswith("On it: starting R")]


def passed(c: Cori) -> int:
    """How many replies the guard checked; asserts it passed every one (no redo)."""
    spans = c.guard_spans()
    assert [(a["kind"], a["redone"]) for a in spans] == [("none", False)] * len(spans), spans
    return len(spans)


def no_noise(said: list[str]) -> None:
    """Nothing Cori's threads showed on 2026-11-25: a canned guard line, a slash command's
    start text (``On it: R7 · … /cancel R7 to stop.``), a queue ack after the run lines."""
    assert not [t for t in said if any(f in t for f in FALLBACKS)], said
    assert not [t for t in said if "/cancel" in t or "On it: R" in t], said
    assert ask_mod.SHARED_ACK not in said, said
    assert NOTHING_WAITING not in said, said


# --- 1. "robot-surf DORI-1173, DORI-1174 and DORI-1175" ---------------------------------------


async def test_robot_surf_three_tickets_one_start_line_each_and_the_reply_as_written(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#193: each run posts its own start line once, the reply carries no second copy, and
    the shared thread's ``On it.`` isn't sent after the runs announced (Claude is still
    writing when the ack is due). #195: an announced start is a change that ran, so the
    reply isn't swapped for the guard's fallback."""
    # first person, so the guard's claim check applies whatever the owner's words were
    reply = "I started R1 (DORI-1173), R2 (DORI-1174), R3 (DORI-1175); I'll post PRs here."
    async with cori(tmp_path, resonant_home, chat, monkeypatch, ack_after_s=2.0) as c:
        c.claude(calls=[surf("DORI-1173"), surf("DORI-1174"), surf("DORI-1175")],
                 delay=4.0, answer=reply)  # fmt: skip
        where = thread(1)
        said = await c.ask(
            JUSTIN, "robot-surf DORI-1173, DORI-1174 and DORI-1175", where,
            lambda s: reply in s and len(start_lines(s)) == 3, "three start lines and the reply",
        )  # fmt: skip
        assert said.count(reply) == 1  # exactly as Claude wrote it, nothing appended
        lines = start_lines(said)
        assert [t.split(" ")[3] for t in lines] == ["R1", "R2", "R3"]  # once each
        assert all("/robot-surf DORI-117" in t for t in lines)
        assert said.index(reply) > max(said.index(t) for t in lines)
        no_noise(said)
        runs = c.runs()
        assert [r.ask for r in runs] == [SURF.format(t=f"DORI-117{i}") for i in (3, 4, 5)]
        assert all(r.principal == "justin" for r in runs)
        assert passed(c) == 1  # checked once, passed: never flagged, never redone


# --- 2. a retry after the earlier runs failed --------------------------------------------------


async def test_a_retry_after_failed_runs_starts_new_runs(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#190: asked to retry, Cori starts the work again instead of pointing at the failure.
    Code side: nothing refuses a start over an older failed run of the same ask, the reply
    goes out as written, and Cori's rule to retry is in what Claude reads."""
    async with cori(tmp_path, resonant_home, chat, monkeypatch, scenario="error") as c:
        where = thread(2)
        c.claude(calls=[surf("DORI-1173")], answer="Started R1 (DORI-1173); I'll post here.")
        await c.ask(JUSTIN, "robot-surf DORI-1173", where,
                    lambda s: any(t.startswith("R1 (") and " failed: " in t for t in s),
                    "R1's failure")  # fmt: skip
        assert c.runs()[0].status == "failed"
        await until(lambda: c.conn.execute("SELECT status FROM tasks WHERE runner = 'claude'")
                    .fetchone()["status"] == "failed", 30, "R1's task to end")  # fmt: skip
        # PER-347: one failure message per run, not the end text and a Couldn't finish that
        failures = [t for t in c.said(where) if "R1" in t and "failed" in t]
        assert len(failures) == 1 and not failures[0].startswith("Couldn't"), failures
        reply = "I started R2 (DORI-1173) again; I'll post the PR here."
        c.claude(calls=[surf("DORI-1173")], answer=reply)
        said = await c.ask(JUSTIN, "retry DORI-1173", where,
                           lambda s: reply in s and len(start_lines(s)) == 1,
                           "R2's start line and the reply")  # fmt: skip
        assert start_lines(said)[0].startswith("On it: starting R2 (")
        no_noise(said)
        r1, r2 = c.runs()
        assert r1.ask == r2.ask and r2.code == "R2"
        assert c.seen()["results"][0].startswith("done:")  # the call ran, not "not run"
        system = c.seen()["argv"][c.seen()["argv"].index("--append-system-prompt") + 1]
        assert "Never skip it because an earlier run failed" in system


# --- 3. an approver merges a PR no run opened -------------------------------------------------


async def test_an_approver_merges_a_pr_no_run_opened_through_the_github_extension(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brian asks Cori to merge a PR a person opened (``merge_run`` has no run for it): the
    brain reads it, then ``github.merge_pr`` runs on Brian's word (L2 after a tainting read,
    ``gate.tainted_writes: allow``) with a token the App minted for that repo alone."""
    async with cori(tmp_path, resonant_home, chat, monkeypatch) as c:
        reply = "Merged PR #159 in voyager-web."
        c.claude(calls=[
            {"name": "pr", "args": {"repo": PR_REPO, "number": 159}},
            {"name": "merge_pr", "args": {"repo": PR_REPO, "number": 159, "head_sha": HEAD}},
        ], answer=reply)  # fmt: skip
        said = await c.ask(
            BRIAN,
            f"merge https://github.com/{PR_REPO}/pull/159",
            thread(3),
            lambda s: any(t.startswith(reply) for t in s),
            "the reply",
        )
        # Claude's words as written, with the change's report line under them
        assert said == [f"{reply}\nDone: merge PR #159 in {PR_REPO}"]
        no_noise(said)
        pr_read, merged = c.seen()["results"]
        assert '"author":"brian"' in pr_read and merged.startswith("done:")
        merges = [x for x in c.gh_calls() if x["argv"][:2] == ["pr", "merge"]]
        assert len(merges) == 1 and merges[0]["argv"][2] == "159" and merges[0]["token_ok"]
        (ev,) = [r for r in c.conn.execute(
            "SELECT decision, requested_by FROM audit_log WHERE action = 'gate.evaluate' "
            "AND tool = 'merge_pr'")]  # fmt: skip
        assert ev["decision"] == "Allow" and ev["requested_by"] == "brian"
        mints = [m for m in c.w.api.mints if m["repositories"] == ["voyager-web"]]
        assert mints and mints[-1]["permissions"]["contents"] == "write"
        assert c.conn.execute("SELECT 1 FROM approvals").fetchone() is None  # nobody asked
        assert c.runs() == []


# --- 4. a bare "yes" with nothing pending -------------------------------------------------------


async def test_a_bare_yes_in_a_thread_with_nothing_pending_goes_to_the_brain(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#193: Cori asked a question; the owner's bare ``yes`` answers it. With nothing waiting
    for approval it reaches Claude like any message, never ``Nothing is waiting for
    approval.``"""
    async with cori(tmp_path, resonant_home, chat, monkeypatch) as c:
        where = thread(4)
        question = "DORI-1173 is in voyager-web. Want me to start a worker on it?"
        c.claude(answer=question)
        await c.ask(JUSTIN, "can you look at DORI-1173", where, has(question), "the question")
        reply = "Started R1 (DORI-1173); I'll post the PR here."
        c.claude(calls=[surf("DORI-1173")], answer=reply)
        said = await c.ask(JUSTIN, "yes", where, has(reply), "the reply to the yes")
        no_noise(said)
        assert c.seen()["prompt"].rstrip().endswith("yes")  # Claude got the yes itself
        assert [r.code for r in c.runs()] == ["R1"]
        assert c.conn.execute("SELECT 1 FROM approvals").fetchone() is None


# --- 5. the guard's replays ---------------------------------------------------------------------


async def test_honest_replies_go_out_unchanged(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#190: a denial and a conditional promise aren't claims of a change. #195: a reply
    after an announced start isn't an empty claim. Each goes out as written, never redone."""
    async with cori(tmp_path, resonant_home, chat, monkeypatch) as c:
        for n, (text, reply) in enumerate([
            ("robot-surf DORI-1173 DORI-1174 DORI-1175", "Nothing changed. I started no runs."),
            ("start them after the deploy", "Once that's done, I'll start them."),
        ], start=10):  # fmt: skip
            c.claude(answer=reply)
            said = await c.ask(JUSTIN, text, thread(n), has(reply), repr(reply))
            assert said == [reply]
        assert c.runs() == [] and passed(c) == 2
        reply = "Started R1; I'll post the PR here."
        c.claude(calls=[surf("DORI-1173")], answer=reply)
        # the owner asks for a change, so the bare "Started …" is checked as a claim too
        said = await c.ask(JUSTIN, "start a worker on DORI-1173", thread(12),
                           lambda s: reply in s and len(start_lines(s)) == 1,
                           "R1's start line and the reply")  # fmt: skip
        assert [t for t in said if t != reply and not t.startswith("R1 ")] == start_lines(said)
        no_noise(said)
        assert passed(c) == 3


@pytest.mark.parametrize("mode", ["strict", "lenient"])
async def test_an_empty_claim_is_redone_then_strict_replaces_it_and_lenient_sends_it(
    tmp_path: Path,
    resonant_home: Path,
    chat: FakeChatDB,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """PER-340: a claim with no change behind it is redone once either way; then ``strict``
    (personal Resonant's default) sends the canned fallback and ``lenient`` (Cori) Claude's
    own words, flagged in the span."""
    claim = "PER-301 has been closed."
    async with cori(tmp_path, resonant_home, chat, monkeypatch, reply_guard=mode) as c:
        c.claude(answer=claim)
        said = await c.ask(JUSTIN, "close PER-301", thread(20), lambda s: bool(s), "a reply")
        lenient = mode == "lenient"
        assert said == [claim if lenient else guard.CLAIM_FALLBACK]
        spans = c.guard_spans()
        assert [(a["kind"], a["redone"], a["fallback"]) for a in spans] == [
            ("claim", False, False),
            ("claim", True, not lenient),
        ]
        assert spans[-1].get("sent_flagged", False) is lenient


# --- 6. a member can't start a run --------------------------------------------------------------


async def test_a_member_cant_start_a_run(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PER-293: miu reaches Claude (she has a read grant) but start_run isn't hers: the call
    is refused by code, no run is made, and she gets Claude's polite no. ``/run`` says who
    can start work."""
    async with cori(tmp_path, resonant_home, chat, monkeypatch) as c:
        assert c.daemon.stack is not None and c.daemon.stack.brain is not None
        names = {s.name for s in c.daemon.stack.brain.tools_for("miu")}
        assert "pr" in names and "start_run" not in names and "merge_pr" not in names
        reply = "Only Justin or Brian can start work. Want me to look the ticket up instead?"
        c.claude(calls=[surf("DORI-1173")], answer=reply)
        said = await c.ask(MIU, "robot-surf DORI-1173", thread(30), has(reply), "the reply")
        assert said == [reply]
        (result,) = c.seen()["results"]
        assert "start_run" in result and not result.startswith("done:")
        assert c.runs() == []
        assert (
            c.conn.execute(
                "SELECT 1 FROM audit_log WHERE action = 'executor.start' AND tool = 'start_run'"
            ).fetchone()
            is None
        )
        said = await c.ask(MIU, f"/run {REPO} fix the login bug", thread(31), lambda s: bool(s),
                           "the refusal")  # fmt: skip
        assert said[0].startswith("Only Justin or Brian can start work"), said
        assert c.runs() == []

"""EM-7: fork-mode runs (docs/employee-plan.md D6) and merge per trust rules (D4).

The runner harness of ``test_runner_claude`` (fake ``claude``, fake ``gh``, local bare repos
as remotes, the App behind a fake endpoint), plus an upstream repo the owner can't push to,
the owner's fork of it (a bare clone), the ``public_repo`` token in the fake Keychain and
EM-4's ``TrustChecks`` behind the worker desk, whose approvals the tests answer as the owner.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from test_runner_claude import (
    GH_TOKEN,
    GIT,
    GIT_ENV,
    Chan,
    Harness,
    build,
    git,
    seed_remote,
    until,
)

from resonant.gate.codes import clean_external
from resonant.gateway.channel import ApprovalPrompt, MessageRef
from resonant.principals import Principal, Principals
from resonant.runner.resolve import FakeGitHub, RepoInfo
from resonant.runner.workspace import FORK_NEEDS_PUBLIC_REPO
from resonant.runners.claude import Refused, Started
from resonant.secrets import GITHUB_PUBLIC_TOKEN_REF
from resonant.store import approvals as ap
from resonant.store import questions as qs
from resonant.store import repos as repo_store
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.store import trust as trust_store
from resonant.store.db import kv_set, utcnow
from resonant.trust.checks import CHECK_TOOL, TrustChecks

PUBLIC_TOKEN = "ghp_" + "Pq7Rs8Tu9V" * 3 + "Wx1Y2Z"
UPSTREAM = "up/stream"
FORK = "me/stream"


class AskChan(Chan):
    """``Chan`` that also takes approval prompts (the trust check's ask)."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[ApprovalPrompt] = []

    async def request_approval(self, principal: str, prompt: ApprovalPrompt) -> MessageRef:
        self.prompts.append(prompt)
        return await self.send(principal, prompt.text(), dedupe_key=f"ask:{prompt.approval_id}")


def prompts(h: Harness) -> list[ApprovalPrompt]:
    return cast(AskChan, h.chan).prompts


def answer(h: Harness, *, yes: bool = True) -> None:
    """The owner answers the run's pending trust check (what a bound iMessage reply does)."""
    now = utcnow()
    row = h.conn.execute(
        "SELECT id FROM approvals WHERE tool = ? AND status = 'pending'", (CHECK_TOOL,)
    ).fetchone()
    assert row is not None, "no pending check"
    ap.resolve(h.conn, row["id"], approved=yes, decided_by="owner", now=now)
    done = ap.get_approval(h.conn, row["id"])
    assert done is not None
    ap.wake_resolved(h.conn, ap.resolve_result([done]), now)


def fork_harness(tmp_path: Path, db: sqlite3.Connection, *extra: str, **kw: Any) -> Harness:
    """A push repo ``proj`` (from ``build``) and the fork-mode ``up/stream``."""
    github = FakeGitHub([RepoInfo(UPSTREAM, "main", False, False, True)])
    h = build(tmp_path, db, extra=["--no-pr", *extra], github=github, **kw)
    upstream = seed_remote(h.root, "stream")
    fork = h.root / "fork-stream.git"
    subprocess.run([GIT, "clone", "-q", "--bare", str(upstream), str(fork)], env=GIT_ENV,
                   check=True)  # fmt: skip
    h.remotes["stream"], h.remotes["fork"] = upstream, fork
    urls = {f"github.com/{UPSTREAM}": upstream, **{f"github.com/own/{n}": p
            for n, p in h.remotes.items() if n not in ("stream", "fork")}}  # fmt: skip
    h.ws._url_for = lambda repo: str(urls[repo.remote])  # pyright: ignore[reportPrivateUsage]
    h.ws._fork_url_for = lambda slug: str(fork)  # pyright: ignore[reportPrivateUsage]
    h.ws.fork_wait_s = 0.0
    h.keychain.items[GITHUB_PUBLIC_TOKEN_REF] = PUBLIC_TOKEN
    h.api.account = "me"  # the App's installation is the owner's account, where the fork is
    (h.gh_dir / "fork_remote").write_text(str(fork))
    (h.gh_dir / "forks.json").write_text(json.dumps({UPSTREAM: FORK}))
    (h.gh_dir / "repos.json").write_text(json.dumps({
        FORK: {"full_name": FORK, "fork": True, "private": False,
               "parent": {"full_name": UPSTREAM}},
    }))  # fmt: skip
    chan = AskChan()
    h.chan = chan
    h.runner.channel = cast(Any, chan)
    h.runner.desk.channel = cast(Any, chan)
    principals = Principals(
        principals={"owner": Principal(role="owner", identities=("imessage:+15550001111",))}
    )
    h.runner.desk.trust = TrustChecks(db, cast(Any, chan), principals, approval_ttl_s=1800,
                                      poll_s=0.02)  # fmt: skip
    kv_set(db, "autonomy", "on")
    return h


async def settle(h: Harness, code: str, *statuses: str, limit_s: float = 40.0) -> runs_store.Run:
    """Drive the loop until the run reaches one of ``statuses``."""
    h.loop._stopping = False  # pyright: ignore[reportPrivateUsage]  # run again after a stop
    running = asyncio.ensure_future(h.loop.run())
    try:
        await until(lambda: h.run(code).status in statuses, limit_s, f"{code} {statuses}")
    finally:
        h.loop.stop()
        await running
    return h.run(code)


def upstream_calls(h: Harness) -> list[dict[str, Any]]:
    return [c for c in h.gh_calls("create") if "--repo" in c["argv"]]


# --- fork mode -----------------------------------------------------------------------------


async def test_fork_run_asks_then_opens_the_upstream_pr(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = fork_harness(tmp_path, db)
    res = await h.runner.start_run("github.com/up/stream", "fix the flaky test", "owner")
    assert isinstance(res, Started), res
    assert "up/stream is upstream: the work goes to your fork, and I'll ask before the PR." in (
        res.text
    )
    code = res.code
    run = await settle(h, code, "waiting")
    assert run.wait_reason == "question" and run.step == "publish"
    # the checkout is the fork's, with the original as ``upstream``
    checkout = h.checkout("stream")
    assert git(checkout, "remote", "get-url", "origin") == str(h.remotes["fork"])
    assert git(checkout, "remote", "get-url", "upstream") == str(h.remotes["stream"])
    # the session pushed to the fork only, and opened no PR (its prompt says so)
    branch = run.branch or ""
    assert git(h.remotes["fork"], "rev-parse", f"refs/heads/{branch}") == run.head_sha
    assert branch not in git(h.remotes["stream"], "for-each-ref", "refs/heads")
    [mint] = h.api.mints
    assert mint["repositories"] == ["stream"]  # the fork's name, in the owner's account
    session = h.argv_log(code)[0]["argv"]
    prompt = session[session.index("--append-system-prompt") + 1]
    assert "do not open a pull request" in prompt and FORK in prompt and UPSTREAM in prompt
    # nothing opened upstream before the owner's yes; the ask names the change
    assert upstream_calls(h) == []
    [prompt_sent] = prompts(h)
    shown = clean_external(f"+1/-0, 1 file @ {(run.head_sha or '')[:12]}: Fix the flaky test")
    assert prompt_sent.summary.startswith(f'{code} wants to external_pr on up/stream: "{shown}"'), (
        prompt_sent.summary
    )
    assert h.api.live == set()  # the session's token was revoked before the owner was asked
    assert "irreversible" in prompt_sent.summary
    [check] = qs.for_run(db, code)
    assert check.kind == "check" and check.status == "open"

    answer(h)
    run = await settle(h, code, "done")
    assert run.pr_number == 41 and run.pr_url == "https://github.com/up/stream/pull/41"
    [create] = upstream_calls(h)
    argv = create["argv"]
    assert argv[argv.index("--repo") + 1] == UPSTREAM
    assert argv[argv.index("--head") + 1] == f"me:{branch}"
    assert argv[argv.index("--title") + 1] == "Fix the flaky test"
    assert create["hosts_token"] == PUBLIC_TOKEN  # the public_repo token, and only here
    assert "fix the flaky test" not in create["stdin"]  # quote_ask is off for fork repos
    others = [c for c in json.loads("[" + ",".join(
        (h.gh_dir / "calls.jsonl").read_text().splitlines()) + "]")]  # fmt: skip
    public = [c["argv"][:4] for c in others if c["hosts_token"] == PUBLIC_TOKEN]
    assert all(a[:2] in (["pr", "create"], ["api", "-X"], ["api", f"repos/{FORK}"]) for a in public)
    text = h.chan.texts()[-1]
    assert text.startswith(
        f"{code} done: PR #41 on up/stream · https://github.com/up/stream/pull/41"
    )
    assert "up/stream is upstream; its maintainers merge." in text
    assert qs.get(db, check.id) is not None and qs.get(db, check.id).delivered_at is not None  # type: ignore[union-attr]
    # never merged by Resonant
    assert await h.runner.merge_run(code) == Refused(
        "up/stream is upstream; its maintainers merge."
    )
    assert h.gh_calls("merge") == []


async def test_fork_run_no_means_no_upstream_pr(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = fork_harness(tmp_path, db)
    res = await h.runner.start_run("github.com/up/stream", "fix it", "owner")
    assert isinstance(res, Started)
    await settle(h, res.code, "waiting")
    answer(h, yes=False)
    run = await settle(h, res.code, "done")
    assert run.pr_url is None and run.failure == "not approved"
    assert upstream_calls(h) == []
    assert "no PR on up/stream (not approved)" in h.chan.texts()[-1]


async def test_fork_branch_moved_after_the_ask_opens_nothing(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = fork_harness(tmp_path, db)
    res = await h.runner.start_run("github.com/up/stream", "fix it", "owner")
    assert isinstance(res, Started)
    run = await settle(h, res.code, "waiting")
    # someone pushes to the fork's branch after the owner was asked
    other = h.root / "other"
    git(h.root, "clone", "-q", str(h.remotes["fork"]), str(other))
    git(other, "checkout", "-q", run.branch or "")
    (other / "more.txt").write_text("more\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "more")
    git(other, "push", "-q", "origin", run.branch or "")
    answer(h)
    run = await settle(h, res.code, "failed")
    assert "moved" in (run.failure or "") and "nothing was opened upstream" in (run.failure or "")
    assert upstream_calls(h) == []


async def test_upstream_pr_at_another_head_is_closed(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = fork_harness(tmp_path, db)
    trust_store.add_rule(db, scope=UPSTREAM, action="external_pr", effect="allow",
                         created_from="chat", now=utcnow())  # fmt: skip
    (h.gh_dir / "fork_moved").write_text("1")  # the branch moves while the PR is being opened
    res = await h.runner.start_run("github.com/up/stream", "fix it", "owner")
    assert isinstance(res, Started)
    run = await settle(h, res.code, "failed", "done")
    assert prompts(h) == []  # the owner's allow rule: no ask
    assert run.status == "failed" and "isn't at the commit you approved (closed it)" in (
        run.failure or ""
    )
    [close] = h.gh_calls("close")
    assert close["hosts_token"] == PUBLIC_TOKEN
    assert [p["state"] for p in h.prs()] == ["CLOSED"]


async def test_fork_run_refused_without_the_public_repo_token(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = fork_harness(tmp_path, db)
    del h.keychain.items[GITHUB_PUBLIC_TOKEN_REF]
    assert await h.runner.setup_missing() is None  # optional for the runner as a whole
    res = await h.runner.start_run("github.com/up/stream", "fix it", "owner")
    assert res == Refused(FORK_NEEDS_PUBLIC_REPO)
    assert isinstance(await h.runner.start_run("proj", "fix it", "owner"), Started)


async def test_fork_checks_refuse_a_token_and_open_nothing(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = fork_harness(tmp_path, db, "--leak", GH_TOKEN)
    h.runner._claude_cmd = (*(h.runner._claude_cmd or ()), "--scenario", "tokens")  # pyright: ignore[reportPrivateUsage]
    res = await h.runner.start_run("github.com/up/stream", "fix it", "owner")
    assert isinstance(res, Started)
    run = await settle(h, res.code, "failed")
    assert "nothing was opened upstream" in (run.failure or "")
    assert GH_TOKEN not in (run.failure or "") and prompts(h) == []
    assert upstream_calls(h) == []


# --- merge per trust rules: auto-merge --------------------------------------------------------


def merge_harness(tmp_path: Path, db: sqlite3.Connection, **kw: Any) -> Harness:
    h = build(tmp_path, db, merge_poll_s=0.1, **kw)
    kv_set(db, "autonomy", "on")
    return h


async def start_merging(h: Harness, ask: str = "fix it and merge it") -> str:
    res = await h.runner.start_run("proj", ask, "owner", merge=True)
    assert isinstance(res, Started), res
    assert "I'll merge it when CI is green, unless a rule says to ask." in res.text
    return res.code


async def test_auto_merge_on_green(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = merge_harness(tmp_path, db)
    code = await start_merging(h)
    run = await settle(h, code, "merged")
    [merge] = h.gh_calls("merge")
    assert merge["argv"][merge["argv"].index("--match-head-commit") + 1] == run.head_sha
    assert merge["hosts_token"] == GH_TOKEN  # the daemon's token, never the session's
    [text] = h.chan.texts()
    assert text.startswith(f"{code} merged: PR #41.") and "github.com/own/proj/pull/41" in text
    task = task_store.get_task(db, cast(str, run.task_id))
    assert task is not None and task.status == "done"


async def test_auto_merge_waits_for_pending_ci_then_merges(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = merge_harness(tmp_path, db)
    (h.gh_dir / "checks_rc").write_text("8")  # pending
    code = await start_merging(h)
    running = asyncio.ensure_future(h.loop.run())
    try:
        await until(lambda: any("I'll merge it when CI is green." in t for t in h.chan.texts()),
                    40, "the ready reply")  # fmt: skip
        assert h.run(code).status == "ready" and h.gh_calls("merge") == []
        (h.gh_dir / "checks_rc").write_text("0")
        await until(lambda: h.run(code).status == "merged", 20, "the merge")
    finally:
        h.loop.stop()
        await running
    ready, merged = h.chan.texts()
    assert ready.startswith(f"{code} done: PR #41 (CI pending)")
    assert merged.startswith(f"{code} merged: PR #41.")


async def test_auto_merge_stops_on_red(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = merge_harness(tmp_path, db)
    (h.gh_dir / "checks_rc").write_text("8")
    code = await start_merging(h)
    running = asyncio.ensure_future(h.loop.run())
    try:
        await until(lambda: len(h.chan.texts()) == 1, 40, "the ready reply")
        (h.gh_dir / "checks_rc").write_text("1")  # red
        await until(lambda: len(h.chan.texts()) == 2, 20, "the stop")
    finally:
        h.loop.stop()
        await running
    assert h.chan.texts()[1] == (
        f"{code} not merged automatically: CI is red. /merge {code} when it's ready."
    )
    assert h.run(code).status == "ready" and h.gh_calls("merge") == []


async def test_auto_merge_gives_up_after_the_wait(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = merge_harness(tmp_path, db, merge_wait_s=0.5)
    (h.gh_dir / "checks_rc").write_text("8")
    await start_merging(h)
    running = asyncio.ensure_future(h.loop.run())
    try:
        await until(lambda: len(h.chan.texts()) == 2, 40, "the give-up")
    finally:
        h.loop.stop()
        await running
    assert "not merged automatically: CI didn't finish within 0s" in h.chan.texts()[1]
    assert h.gh_calls("merge") == []


async def test_auto_merge_asks_when_a_rule_says_so(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = merge_harness(tmp_path, db)
    trust_store.add_rule(db, scope="proj", action="merge", effect="ask", created_from="chat",
                         now=utcnow())  # fmt: skip
    code = await start_merging(h)
    run = await settle(h, code, "ready")
    await asyncio.sleep(0.3)
    assert run.status == "ready" and h.gh_calls("merge") == []
    [text] = h.chan.texts()
    assert text.startswith(f"{code} done: PR #41 (CI green)")
    assert text.endswith(
        f"Not merged automatically: a rule says to ask before merging in proj. "
        f"/merge {code} when it's ready."
    )


async def test_auto_merge_never_merges_flagged_paths(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = merge_harness(tmp_path, db, scenario="makefile")
    code = await start_merging(h)
    await settle(h, code, "ready")
    await asyncio.sleep(0.3)
    assert h.gh_calls("merge") == []
    assert (
        "Not merged automatically: it changes .claude/settings.json, Makefile."
        in (h.chan.texts()[0])
    )


async def test_auto_merge_refuses_a_moved_head(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = merge_harness(tmp_path, db)
    (h.gh_dir / "checks_rc").write_text("8")
    code = await start_merging(h)
    running = asyncio.ensure_future(h.loop.run())
    try:
        await until(lambda: len(h.chan.texts()) == 1, 40, "the ready reply")
        prs = h.prs()
        prs[0]["headRefOid"] = "e" * 40  # someone pushed to the PR after the run
        (h.gh_dir / "prs.json").write_text(json.dumps(prs))
        (h.gh_dir / "checks_rc").write_text("0")
        await until(lambda: len(h.chan.texts()) == 2, 20, "the stop")
    finally:
        h.loop.stop()
        await running
    assert "changed since the run finished" in h.chan.texts()[1]
    assert h.gh_calls("merge") == [] and h.run(code).status == "ready"


async def test_owner_merge_during_the_watch_wins_once(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = merge_harness(tmp_path, db)
    (h.gh_dir / "checks_rc").write_text("8")
    code = await start_merging(h)
    running = asyncio.ensure_future(h.loop.run())
    try:
        await until(lambda: len(h.chan.texts()) == 1, 40, "the ready reply")
        (h.gh_dir / "checks_rc").write_text("0")
        merged = await h.runner.merge_run(code)  # /merge R1 while the watcher is waiting
        assert not isinstance(merged, Refused), merged
        await asyncio.sleep(0.5)
    finally:
        h.loop.stop()
        await running
    assert len(h.gh_calls("merge")) == 1 and h.run(code).status == "merged"
    assert len(h.chan.texts()) == 1  # the watcher says nothing more


async def test_merge_flag_without_merge_intent_is_a_plain_run(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = merge_harness(tmp_path, db)
    res = await h.runner.start_run("proj", "fix it", "owner")
    assert isinstance(res, Started) and "merge it when CI" not in res.text
    await settle(h, res.code, "ready")
    await asyncio.sleep(0.3)
    assert h.gh_calls("merge") == [] and f"/merge {res.code} to merge it." in h.chan.texts()[0]


@pytest.mark.parametrize("mode", ["fork"])
async def test_start_run_ignores_merge_for_a_fork_repo(
    tmp_path: Path, db: sqlite3.Connection, mode: str
) -> None:
    h = fork_harness(tmp_path, db)
    res = await h.runner.start_run("github.com/up/stream", "fix it", "owner", merge=True)
    assert isinstance(res, Started) and "merge it when CI" not in res.text
    repo = repo_store.get(db, UPSTREAM)
    assert repo is not None and repo.mode == mode
    run = h.run(res.code)
    task = task_store.get_task(db, cast(str, run.task_id))
    assert task is not None and "merge" not in task.checkpoint


async def test_a_check_the_worker_asked_itself_never_opens_the_pr(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """A worker's own ``resonant check`` with the very text the daemon would show can't stand
    in for the daemon's external_pr ask: its rows never match ``find_check``."""
    h = fork_harness(tmp_path, db)
    res = await h.runner.start_run("github.com/up/stream", "fix it", "owner")
    assert isinstance(res, Started)
    run = await settle(h, res.code, "waiting")
    [daemon_row] = qs.for_run(db, res.code)
    assert "(asked by Resonant)" in daemon_row.question
    forged = daemon_row.question.replace(" (asked by Resonant)", "")
    fake = qs.ask(db, res.code, "check", forged, at=utcnow())
    qs.answer(db, fake.id, "ok", at=utcnow())  # as if the owner had said yes to the worker's
    h.runner._wake(run, "test")  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0.5)
    h.loop._stopping = False  # pyright: ignore[reportPrivateUsage]
    running = asyncio.ensure_future(h.loop.run())
    await asyncio.sleep(1.0)
    h.loop.stop()
    await running
    assert h.run(res.code).status == "waiting" and upstream_calls(h) == []


async def test_cancel_stops_an_auto_merge_watch(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = merge_harness(tmp_path, db)
    (h.gh_dir / "checks_rc").write_text("8")
    code = await start_merging(h)
    running = asyncio.ensure_future(h.loop.run())
    try:
        await until(lambda: len(h.chan.texts()) == 1, 40, "the ready reply")
        res = await h.runner.cancel_run(code)
        assert res == Refused(
            f"{code} won't be merged automatically; its PR stays open. /merge {code} to merge it."
        )
        (h.gh_dir / "checks_rc").write_text("0")
        await asyncio.sleep(0.6)
    finally:
        h.loop.stop()
        await running
    assert h.gh_calls("merge") == [] and h.run(code).status == "ready"

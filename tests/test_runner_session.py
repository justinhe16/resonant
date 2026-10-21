"""The Claude session drives its own run with a per-run GitHub App token (docs/repos-plan.md D10):
minting and scoping, the credentials the session finds, refresh, revoke, PR discovery and the
not-set-up text. The runner harness of ``test_runner_claude`` with a fake GitHub App endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from github_app_fakes import private_pem_one_line
from test_runner_claude import (
    CLAUDE_TOKEN,
    GH_TOKEN,
    Harness,
    build,
    git,
    until,
    with_scenario,
)

from resonant import killswitch
from resonant.runner import github_app
from resonant.runners import claude as claude_mod
from resonant.runners.claude import Refused
from resonant.secrets import GITHUB_APP_KEY_REF, GITHUB_TOKEN_REF
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.store.db import iso, utcnow


@pytest.fixture
def h(tmp_path: Path, db: sqlite3.Connection) -> Harness:
    return build(tmp_path, db)


def creds_seen(h: Harness, name: str = "creds.json", code: str = "R1") -> dict[str, Any]:
    return json.loads((h.tmp(code) / name).read_text())


# --- minting and scoping ---------------------------------------------------------------------


async def test_each_run_gets_a_token_for_its_one_repo(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, repos=("proj", "other"))
    await h.start("proj")
    await h.start("other")
    await h.drain()
    assert sorted(m["repositories"] for m in h.api.mints) == [["other"], ["proj"]]
    assert len(set(h.api.tokens)) == 2
    # the registry says allow_github_changes is off for the seeded repos: no Workflows
    assert all("workflows" not in m["permissions"] for m in h.api.mints)
    assert all(m["permissions"]["contents"] == "write" for m in h.api.mints)
    assert all("administration" not in m["permissions"] for m in h.api.mints)
    assert h.api.live == set() and sorted(h.api.revoked) == sorted(h.api.tokens)


async def test_workflows_permission_follows_the_repo_setting(h: Harness) -> None:
    h.conn.execute("UPDATE repos SET allow_github_changes = 1")
    await h.start()
    await h.drain()
    assert h.api.mints[0]["permissions"] == github_app.permissions(workflows=True)


async def test_registry_default_allows_github_changes_for_new_repos(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    from resonant.runner.resolve import FakeGitHub, RepoInfo

    h = build(tmp_path, db, github=FakeGitHub([RepoInfo("own/fresh", "main", False, True, True)]))
    res = await h.runner.start_run("own/fresh", "x", "owner")
    assert isinstance(res, claude_mod.Started)
    from resonant.store import repos as repo_store

    fresh = repo_store.get(db, "own/fresh")
    assert fresh is not None and fresh.allow_github_changes is True


async def test_a_token_that_cant_be_minted_fails_the_run_before_the_session(h: Harness) -> None:
    h.api.fail_mint = (404, "Not Found")
    await h.start()
    await h.drain()
    run = h.run()
    assert run.status == "failed"
    assert "HTTP 404" in (run.failure or "") and "installed on proj" in (run.failure or "")
    assert not (h.tmp("R1") / "argv.json").exists()  # Claude never started
    assert h.chan.texts()[0].startswith(
        "R1 (fix the flaky test) failed: minting the run's GitHub token failed"
    )


# --- what the session finds ------------------------------------------------------------------


async def test_session_credentials_dir_and_env(h: Harness) -> None:
    await h.start()
    await h.drain()
    seen = creds_seen(h)
    token = h.api.tokens[0]
    assert seen["token"] == token and seen["hosts_token"] == token
    assert seen["modes"] == ["0o700"]
    assert seen["gh_config_dir"] == str(h.code_dir("R1").resolve() / "creds" / "gh")
    # the same files held the token the whole session, and the clone's config points at them
    assert creds_seen(h, "creds-end.json")["token"] == token
    # the session's own gh/git calls carried the run's token by file, the daemon's by its own
    session_calls = [c for c in h.gh_calls() if c["hosts_token"] == token]
    assert [c["argv"][:2] for c in session_calls] == [["pr", "create"], ["pr", "checks"]]
    assert all(c["config_dir"].endswith("/R1/creds/gh") for c in session_calls)
    tools = {c["argv"][0] for c in h.session_tools()}
    assert tools == {"git", "gh"}  # the run-local bin has both
    assert all(c["rc"] == 0 for c in h.session_tools())


async def test_creds_dir_exists_with_the_helper_for_the_session(h: Harness) -> None:
    code = await h.start()
    with_scenario(h, "hang")
    run_task = h.spawn_loop()
    await until(lambda: h.run(code).pgid is not None, what="the session")
    creds = h.code_dir(code) / "creds"
    await until(lambda: (creds / "gh" / "hosts.yml").exists(), what="the credentials")
    assert stat.S_IMODE(creds.stat().st_mode) == 0o700
    assert stat.S_IMODE((creds / "token").stat().st_mode) == 0o600
    assert stat.S_IMODE((creds / "gh" / "hosts.yml").stat().st_mode) == 0o600
    assert stat.S_IMODE((creds / "credential-helper").stat().st_mode) == 0o700
    # the helper reaches the session by env, never by the checkout's config
    config = (h.checkout() / ".git" / "config").read_text()
    assert "credential" not in config and '[remote "origin"]' in config
    await h.runner.cancel_run(code)
    await run_task


async def test_the_prompt_tells_the_session_to_drive_the_pr(h: Harness) -> None:
    await h.start()
    await h.drain()
    argv = h.argv_log()[0]["argv"]
    prompt: str = argv[argv.index("--append-system-prompt") + 1]
    branch = h.run().branch
    assert branch == "resonant/r1-fix-the-flaky-test"
    assert f"on branch {branch}" in prompt
    assert f"git push -u origin {branch}" in prompt
    assert f"gh pr create --base main --head {branch}" in prompt
    assert "gh pr checks --watch" in prompt
    assert f"at most {claude_mod.MAX_FIX_ATTEMPTS} times" in prompt
    assert "Never push to main" in prompt and "Do not push" not in prompt
    assert "Stop when CI is green or you are out of attempts" in prompt
    assert "own/proj" in prompt
    assert claude_mod.MAX_FIX_ATTEMPTS == 3


async def test_a_fixing_session_that_pushes_again_is_still_one_pr(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, extra=["--ci-fixes", "2"])
    await h.start()
    await h.drain()
    run = h.run()
    assert run.status == "ready" and len(h.prs()) == 1
    branch = run.branch or ""
    assert git(h.remotes["proj"], "rev-list", "--count", f"main..{branch}") == "3"
    # the PR's head is the last push, which is what the daemon recorded
    assert run.head_sha == git(h.remotes["proj"], "rev-parse", branch)


# --- no secret where it can be read ----------------------------------------------------------


async def test_no_token_in_env_argv_logs_db_or_transcript(
    tmp_path: Path, db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    h = build(tmp_path, db, extra=["--echo-token"])
    await h.start()
    await h.drain()
    tokens = h.api.tokens
    assert tokens and h.run().status == "ready"
    log = h.argv_log()[0]
    blob = json.dumps(log["argv"]) + json.dumps(log["env"])
    assert not any(t in blob for t in tokens)
    assert "GH_TOKEN" not in log["env"] and "GITHUB_TOKEN" not in log["env"]
    assert log["env"]["GH_CONFIG_DIR"].endswith("/R1/creds/gh")  # a path, not a secret
    # every gh the fake saw: the token only in the hosts file, never in env or argv
    for call in h.gh_calls():
        assert not any(t in json.dumps(call["argv"]) + json.dumps(call["env"]) for t in tokens)
    # the transcript redacts the token the session itself echoed
    transcript = (h.runner.transcript_dir / "R1.jsonl").read_text()
    assert "[redacted]" in transcript and not any(t in transcript for t in tokens)
    assert GH_TOKEN not in transcript and CLAUDE_TOKEN not in transcript
    # not in the log lines, the database, the reply or the run row
    assert not any(t in caplog.text for t in tokens)
    dump = "\n".join(h.conn.iterdump())
    assert not any(t in dump for t in tokens)
    assert not any(t in text for t in tokens for text in h.chan.texts())
    # and not left on disk in the daemon's secret dir
    leftovers = [f for f in h.cfg.secrets_dir.glob("**/*") if f.is_file()]
    assert not any(t in f.read_text() for f in leftovers for t in tokens)
    assert not (h.code_dir("R1") / "creds").exists()  # gone with the session


async def test_the_app_key_never_leaves_the_keychain_call(h: Harness) -> None:
    await h.start()
    await h.drain()
    key = private_pem_one_line()
    assert not any(key[:40] in a for call in h.keychain.argvs() for a in call)
    log = json.dumps(h.argv_log()[0])
    assert key[:40] not in log and "BEGIN" not in log
    assert key[:40] not in "\n".join(h.conn.iterdump())


# --- refresh ---------------------------------------------------------------------------------


async def test_a_long_session_gets_a_fresh_token_in_the_same_files(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, extra=["--sleep", "1.5", "--echo-token"], refresh_s=0.4)
    await h.start()
    await h.drain()
    assert h.run().status == "ready"
    first, *later = h.api.tokens
    assert later, "no refresh happened"
    start, end = creds_seen(h), creds_seen(h, "creds-end.json")
    assert start["token"] == first
    assert end["token"] in later  # rewritten in place (a later refresh may follow the probe)
    assert end["hosts_token"] == end["token"] or end["hosts_token"] in later  # gh's file too
    assert h.api.live == set() and sorted(h.api.revoked) == sorted(h.api.tokens)
    transcript = (h.runner.transcript_dir / "R1.jsonl").read_text()
    assert not any(t in transcript for t in h.api.tokens)  # the refreshed one is redacted too


async def test_a_failed_refresh_is_retried_and_does_not_kill_the_session(
    tmp_path: Path, db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    h = build(tmp_path, db, extra=["--sleep", "1.6"], refresh_s=0.3)
    await h.start()

    async def flap() -> None:
        await until(lambda: len(h.api.mints) >= 1, what="the first token")
        h.api.fail_mint = (403, "nope")  # the refreshes fail for a while ...
        await asyncio.sleep(0.5)
        h.api.fail_mint = None  # ... then work

    await asyncio.gather(h.drain(), flap())
    assert h.run().status == "ready"
    assert "run token refresh failed" in caplog.text
    assert len(h.api.tokens) >= 2  # a later refresh got through
    assert h.api.live == set()


# --- revoke ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "status"), [("happy", "ready"), ("nochanges", "no_changes"), ("error", "failed")]
)
async def test_the_token_is_revoked_when_the_run_ends(
    tmp_path: Path, db: sqlite3.Connection, scenario: str, status: str
) -> None:
    h = build(tmp_path, db, scenario=scenario)
    await h.start()
    await h.drain()
    assert h.run().status == status
    assert h.api.tokens and h.api.live == set() and h.api.revoked == h.api.tokens
    assert not (h.code_dir("R1") / "creds").exists()  # a kept dir has no credentials in it


async def test_the_token_is_revoked_on_a_timeout(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, scenario="hang", max_session_s=1)
    await h.start()
    await h.drain()
    assert h.run().status == "failed" and "timed out" in (h.run().failure or "")
    assert h.api.live == set() and len(h.api.revoked) == 1


async def test_the_token_is_revoked_on_cancel(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, scenario="hang")
    code = await h.start()
    task = h.spawn_loop()
    await until(lambda: h.run(code).pgid is not None, what="the session")
    assert len(h.api.live) == 1
    await h.runner.cancel_run(code)
    await task
    assert h.run(code).status == "cancelled"
    assert h.api.live == set() and h.api.revoked == h.api.tokens
    assert not h.code_dir(code).exists()


async def test_the_token_is_revoked_on_kill(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, scenario="hang")
    code = await h.start()
    await h.runner.start()
    task = h.spawn_loop()
    await until(lambda: h.run(code).pgid is not None, what="the session")
    assert len(h.api.live) == 1
    killswitch.engage(h.conn, actor="owner")
    await task
    assert h.run(code).status == "cancelled"
    assert h.api.live == set() and h.api.revoked == h.api.tokens
    await h.runner.aclose()


async def test_a_revoke_that_fails_is_logged_and_does_not_stop_the_run(
    tmp_path: Path, db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    h = build(tmp_path, db)
    h.api.fail_revoke = True
    await h.start()
    await h.drain()
    assert h.run().status == "ready"
    assert "couldn't revoke a run's token" in caplog.text
    assert not any(t in caplog.text for t in h.api.tokens)
    assert db.execute("SELECT 1 FROM audit_log WHERE action = 'run.token_revoke_failed'").fetchone()
    # three tries, then the owner is told which token is still live and until when
    deletes = [r for r in h.api.requests if r[0] == "DELETE"]
    assert len(deletes) == 3
    reply = h.chan.texts()[0]
    assert reply.startswith("R1 (fix the flaky test) opened PR #41")
    assert "\n⚠ The run's GitHub token wasn't revoked; it expires at " in reply
    assert not any(t in reply for t in h.api.tokens)


async def test_a_resumed_session_gets_a_new_token_and_the_old_one_is_revoked(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="usage_limit", extra=["--reset-in", "3600"])
    code = await h.start()
    await h.drain()
    assert h.run().status == "waiting"
    assert h.api.live == set()  # not held while parked
    task = task_store.get_task(h.conn, cast(str, h.run().task_id))
    assert task is not None
    past = iso(utcnow() - timedelta(seconds=1))
    h.conn.execute("UPDATE tasks SET wake_at = ? WHERE id = ?", (past, task.id))
    runs_store.update(h.conn, code, waiting_since=utcnow() - timedelta(hours=1))
    await h.drain()
    assert h.run().status == "ready", h.run().failure
    assert len(h.api.tokens) == 2 and h.api.live == set()


# --- the run after the session ---------------------------------------------------------------


async def test_pushed_but_no_pr_fails_with_the_branch(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, extra=["--no-pr"])
    await h.start()
    await h.drain()
    run = h.run()
    assert run.status == "failed" and run.pr_number is None
    assert f"pushed {run.branch} but opened no PR" in (run.failure or "")
    assert run.branch in h.branches()


async def test_nothing_pushed_and_no_pr_is_no_changes(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, extra=["--no-push"])  # committed locally, never pushed
    await h.start()
    await h.drain()
    assert h.run().status == "no_changes"
    assert h.chan.texts()[0].startswith("R1 (fix the flaky test) made no changes")
    assert h.branches() == ["main"]


async def test_the_pr_is_found_by_its_branch_not_by_the_summary(h: Harness) -> None:
    await h.start()
    await h.drain()
    [lookup] = [c for c in h.gh_calls("list") if c["hosts_token"] == GH_TOKEN]
    argv = lookup["argv"]
    assert argv[argv.index("--head") + 1] == h.run().branch
    assert argv[argv.index("--repo") + 1] == "own/proj" and "--state" in argv
    assert h.run().pr_url == "https://github.com/own/proj/pull/41"


async def test_a_pr_to_another_base_or_not_open_fails_the_run(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    await h.start()
    real = h.ws.find_pr

    async def other_base(*a: Any, **k: Any) -> Any:
        from dataclasses import replace

        found = await real(*a, **k)
        assert found is not None
        return replace(found, base="develop")

    h.ws.find_pr = other_base  # type: ignore[method-assign]
    await h.drain()
    assert h.run().status == "failed" and "targets develop, not main" in (h.run().failure or "")


@pytest.mark.parametrize(("rc", "word"), [("0", "green"), ("1", "red"), ("8", "pending")])
async def test_the_reply_says_how_ci_stands(
    tmp_path: Path, db: sqlite3.Connection, rc: str, word: str
) -> None:
    h = build(tmp_path, db)
    (h.gh_dir / "checks_rc").write_text(rc)
    await h.start()
    await h.drain()
    assert h.chan.texts()[0].startswith(f"R1 (fix the flaky test) opened PR #41 (CI {word}, ")


async def test_a_pr_without_checks_says_no_ci(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db)
    (h.gh_dir / "checks_rc").write_text("1")
    (h.gh_dir / "checks_err").write_text("no checks reported on the 'x' branch")
    await h.start()
    await h.drain()
    assert h.chan.texts()[0].startswith("R1 (fix the flaky test) opened PR #41 (no CI, ")


# --- the checks before a merge ---------------------------------------------------------------


async def test_a_github_change_is_listed_in_the_merge_prompt_when_the_repo_allows_it(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="github")
    h.conn.execute("UPDATE repos SET allow_github_changes = 1")
    await h.start()
    await h.drain()
    run = h.run()
    assert run.status == "ready" and run.failure is None
    assert run.flagged_paths == (".github/workflows/evil.yml",)
    assert "⚠ changes .github/workflows/evil.yml" in h.chan.texts()[0]
    res = await h.runner.merge_run("R1")
    assert isinstance(res, claude_mod.Merged)
    assert res.text == "R1 merged: PR #41.\n⚠ changes .github/workflows/evil.yml"
    assert h.api.mints[0]["permissions"]["workflows"] == "write"


async def test_merge_refuses_a_pr_that_moved_after_the_run_finished(h: Harness) -> None:
    await h.start()
    await h.drain()
    prs = h.prs()
    prs[0]["headRefOid"] = "d" * 40  # someone pushed to the PR since
    (h.gh_dir / "prs.json").write_text(json.dumps(prs))
    res = await h.runner.merge_run("R1")
    assert isinstance(res, Refused) and "changed since the run finished" in res.reason
    assert h.gh_calls("merge") == [] and h.run().status == "ready"


async def test_every_merge_runs_the_checks_first(h: Harness) -> None:
    await h.start()
    await h.drain()
    # a push after the run, with a token in it, on the PR's own branch
    branch = h.run().branch or ""
    work = h.root / "later"
    git(h.root, "clone", "-q", str(h.remotes["proj"]), str(work))
    git(work, "checkout", "-q", branch)
    (work / "oops.txt").write_text(f"{GH_TOKEN}\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "oops")
    git(work, "push", "-q", "origin", branch)
    head = git(work, "rev-parse", "HEAD")
    prs = h.prs()
    prs[0]["headRefOid"] = head
    (h.gh_dir / "prs.json").write_text(json.dumps(prs))
    h.conn.execute("UPDATE runs SET head_sha = ? WHERE code = 'R1'", (head,))  # as if it was seen
    res = await h.runner.merge_run("R1")
    assert isinstance(res, Refused) and "a token appears" in res.reason
    assert GH_TOKEN not in res.reason and h.gh_calls("merge") == []


# --- not set up ------------------------------------------------------------------------------


async def test_the_github_pieces_are_capability_notes_not_blockers(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """RX-1: the App key, runner.github_app and the daemon's token each unlock something;
    none of them makes the runner "not set up"."""
    h = build(tmp_path, db)
    assert await h.runner.setup_missing() is None
    assert await h.runner.capability_notes() == []
    del h.keychain.items[GITHUB_APP_KEY_REF]
    assert await h.runner.setup_missing() is None
    [note] = await h.runner.capability_notes()
    assert note.startswith("No push or PRs yet: the GitHub App isn't set up")
    assert "secret set runner/github_app_key --from-file" in note
    assert "runner.github_app" not in note
    h.runner.app = None  # runner.github_app isn't in the config
    [note] = await h.runner.capability_notes()
    assert "add runner.github_app with app_id and installation_id" in note
    del h.keychain.items[GITHUB_TOKEN_REF]
    assert await h.runner.setup_missing() is None
    app, token = await h.runner.capability_notes()
    assert app.startswith("No push or PRs yet")
    assert token.startswith("No merges, repo lookups, private clones, new repos or fork runs")
    assert f"resonant secret set {GITHUB_TOKEN_REF}" in token
    # the token alone missing: pushing needs it too (Resonant checks the PR with it)
    h.keychain.items[GITHUB_APP_KEY_REF] = private_pem_one_line()
    h.runner.app = h.app
    [token] = await h.runner.capability_notes()
    assert token.startswith("No push, PRs, merges, repo lookups")


async def test_a_refresh_revokes_the_previous_token_at_once(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="hang", refresh_s=0.3)
    code = await h.start()
    task = h.spawn_loop()
    await until(lambda: len(h.api.mints) >= 2, what="a refresh")
    await until(lambda: len(h.api.revoked) >= 1, what="the old token's revoke")
    assert h.run(code).status == "running"  # the session is still going
    assert h.api.revoked[0] == h.api.tokens[0]
    newest = h.api.tokens[-1]
    assert newest in h.api.live and h.api.tokens[0] not in h.api.live
    token_file = (h.code_dir(code) / "creds" / "token").read_text().strip()
    assert token_file in h.api.live  # what is on disk is always a live token
    await h.runner.cancel_run(code)
    await task
    assert h.api.live == set()


# --- the mint reply is checked --------------------------------------------------------------


@pytest.mark.parametrize(
    ("grant", "why"),
    [
        ({"repository_selection": "all"}, "more than the one repository"),
        ({"repositories": [{"name": "proj"}, {"name": "other"}]}, "different set of repositories"),
        ({"repositories": [{"name": "other"}]}, "different set of repositories"),
        ({"permissions": {"contents": "write", "administration": "write"}}, "administration"),
        ({"permissions": {"contents": "read", "pull_requests": "write"}}, "lacks contents"),
    ],
)
async def test_a_token_that_isnt_what_was_asked_for_is_revoked_and_fails_the_run(
    tmp_path: Path, db: sqlite3.Connection, grant: dict[str, Any], why: str
) -> None:
    h = build(tmp_path, db)
    h.api.grant = grant
    await h.start()
    await h.drain()
    run = h.run()
    assert run.status == "failed" and why in (run.failure or "")
    assert h.api.live == set() and h.api.revoked == h.api.tokens and len(h.api.tokens) == 1
    assert not (h.tmp("R1") / "argv.json").exists()  # no session with that token


async def test_a_transient_mint_failure_is_retried(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db)
    h.api.flaky = 2  # two 503s, then fine
    await h.start()
    await h.drain()
    assert h.run().status == "ready" and len(h.api.tokens) == 1


# --- the base branch ------------------------------------------------------------------------


def push_event(
    sha: str, *, actor: str = "resonant-runner[bot]", minutes_ago: float = 0
) -> dict[str, Any]:
    from datetime import UTC, datetime

    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {
        "after": sha, "before": "0" * 40, "ref": "refs/heads/main", "activity_type": "push",
        "actor": {"login": actor}, "timestamp": when.isoformat().replace("+00:00", "Z"),
    }  # fmt: skip


async def test_an_unprotected_base_is_said_in_the_start_reply(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    res = await h.runner.start_run("proj", "x", "owner")
    assert isinstance(res, claude_mod.Started) and res.warning is None
    assert "protected" not in res.text
    (h.gh_dir / "protected").write_text("false")
    res = await h.runner.start_run("proj", "y", "owner")
    assert isinstance(res, claude_mod.Started)
    assert res.text.endswith("\nmain isn't protected — the session could push to it directly.")


async def test_a_direct_push_to_the_base_fails_the_run_and_tells_the_owner(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    (h.gh_dir / "protected").write_text("false")
    sha = "ab" * 20
    (h.gh_dir / "activity.json").write_text(
        json.dumps(
            [
                push_event(sha),
                push_event("cd" * 20, actor="someone-else"),  # a person's push isn't the run's
                push_event("ef" * 20, minutes_ago=600),  # before the run began
            ]
        )
    )
    await h.start()
    await h.drain()
    run = h.run()
    assert run.status == "failed"
    assert run.failure == "it pushed directly to main (abababa), so it won't be merged"
    # PER-268: one text, the ⚠ on its own line
    assert h.chan.texts() == [
        "R1 (fix the flaky test) failed: it pushed directly to main (abababa), so it won't be "
        "merged\n⚠ R1 pushed abababa straight to main, skipping every check. Look at own/proj now."
    ]
    res = await h.runner.merge_run("R1")
    assert isinstance(res, Refused) and h.gh_calls("merge") == []
    assert h.api.live == set()
    assert h.prs() == [] or h.run().pr_number is None  # nothing was recorded as ready


async def test_the_feed_is_checked_even_when_the_base_is_protected(h: Harness) -> None:
    """A ruleset the app can bypass would let a direct push through while `protected` is true."""
    (h.gh_dir / "activity.json").write_text(json.dumps([push_event("ab" * 20)]))
    await h.start()
    await h.drain()  # protected is the default in the fake
    run = h.run()
    assert run.status == "failed" and "pushed directly to main" in (run.failure or "")
    [text] = h.chan.texts()
    assert "\n⚠ R1 pushed abababa straight to main" in text


async def test_a_pr_merge_onto_the_base_by_the_bot_is_not_a_direct_push(h: Harness) -> None:
    """PER-268: GitHub reports a merge as ``pr_merge``; it is not a push. One the run didn't
    make itself (Resonant merging another run's PR meanwhile) is only audited."""
    event = push_event("cd" * 20) | {"activity_type": "pr_merge"}
    (h.gh_dir / "activity.json").write_text(json.dumps([event]))
    await h.start(report="now")
    await h.drain()
    run = h.run()
    assert run.status == "ready" and run.failure is None
    [text] = h.chan.texts()
    assert "⚠" not in text and "pushed directly" not in text and "merged" not in text
    rows = h.conn.execute("SELECT 1 FROM audit_log WHERE action = 'run.base_merge'").fetchall()
    assert len(rows) == 1


async def test_a_bot_merge_the_run_didnt_make_is_no_changes_not_done(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """Review fix: a merge in the window that isn't this run's own gh pr merge (Resonant's
    merge of another run) never makes a run that pushed nothing claim it."""
    h = build(tmp_path, db, scenario="nochanges")
    event = push_event("12" * 20) | {"activity_type": "pr_merge"}  # an all-digit short sha
    (h.gh_dir / "activity.json").write_text(json.dumps([event]))
    await h.start(report="now")
    await h.drain()
    assert h.run().status == "no_changes"
    [text] = h.chan.texts()
    assert "merged" not in text and "#1212121" not in text


async def test_a_run_asked_to_merge_a_pr_ends_done_with_one_text_naming_it(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """PER-268's incident: R4 reviewed PR #121 and squash-merged it with gh. It pushed nothing
    of its own, so it ends done, with ONE end text that names the merge and no ⚠."""
    h = build(tmp_path, db, scenario="nochanges", extra=["--bash", "gh pr merge 121 --squash"])
    event = push_event("cd" * 20) | {"activity_type": "pr_merge"}
    (h.gh_dir / "activity.json").write_text(json.dumps([event]))
    await h.start(report="digest")  # the merge is said now even when the end is a digest
    await h.drain()
    run = h.run()
    assert run.status == "done" and run.failure is None
    [text] = h.chan.texts()
    assert text.startswith("R1 (fix the flaky test) merged PR #121 into main (")
    assert "⚠" not in text and "Along the way" not in text and len(text.splitlines()) <= 3
    rows = db.execute("SELECT 1 FROM audit_log WHERE action = 'run.done_merged'").fetchall()
    assert len(rows) == 1


async def test_a_pr_the_session_merged_itself_fails_loudly_and_is_never_resonants(
    h: Harness,
) -> None:
    from dataclasses import replace

    await h.start()
    real = h.ws.find_pr

    async def merged(*a: Any, **k: Any) -> Any:
        found = await real(*a, **k)
        assert found is not None
        return replace(found, state="MERGED")

    h.ws.find_pr = merged  # type: ignore[method-assign]
    await h.drain()
    run = h.run()
    assert run.status == "failed" and run.failure == "it merged its own PR #41, not Resonant"
    [text] = h.chan.texts()
    assert text.endswith(
        "\n⚠ R1 merged its own PR #41, so none of Resonant's checks ran on it. Look at own/proj."
    )
    res = await h.runner.merge_run("R1")
    assert isinstance(res, Refused) and h.gh_calls("merge") == []
    assert run.status != "merged"


def _merged_pr(h: Harness) -> None:
    """``find_pr`` reports the session's PR as MERGED (by whom is up to the test)."""
    from dataclasses import replace

    real = h.ws.find_pr

    async def merged(*a: Any, **k: Any) -> Any:
        found = await real(*a, **k)
        assert found is not None
        return replace(found, state="MERGED")

    h.ws.find_pr = merged  # type: ignore[method-assign]


def _resonant_merged(db: sqlite3.Connection, repo: str, number: int) -> str:
    """Record that Resonant's ``github.merge_pr`` merged ``repo#number`` on an approved
    prompt (as the gate and executor write it); returns the approval's code."""
    from resonant.gate.codes import new_code
    from resonant.loop import NewTask
    from resonant.store import approvals as ap
    from resonant.store.audit import audit
    from resonant.store.db import atomic
    from resonant_sdk import Effect, Level

    tid = task_store.insert_task(db, NewTask(kind="message", runner="claude", priority=1))
    code = new_code(ap.pending_codes(db))
    now = utcnow()
    with atomic(db):
        approval_id = ap.insert_approval(
            db,
            ap.NewApproval(
                task_id=tid, intent_hash="h1", tool="merge_pr", effect=Effect.WRITE,
                level=Level.L2, summary=f"merge PR #{number} in {repo}", approvers=("justin",),
                code=code, expires_at=now + timedelta(minutes=30), extension="github",
            ),
            now,
        )  # fmt: skip
        args = {"repo": repo, "number": number, "head_sha": "ab" * 20}
        audit(db, "gate.evaluate", task_id=tid, extension="github", tool="merge_pr",
              intent_hash="h1", approval_id=approval_id, args=args)  # fmt: skip
        audit(db, "executor.result", task_id=tid, extension="github", tool="merge_pr",
              intent_hash="h1", approval_id=approval_id, ok=True)  # fmt: skip
    return code


async def test_a_pr_resonant_merged_ends_the_run_done_with_no_alert(h: Harness) -> None:
    """PER-269's incident: the brain's merge_pr (approved in M6) merged the run's PR while
    the run's own check for it was open. The run is done and merged, not failed, and the
    one end text names the approval, with no ⚠."""
    code = _resonant_merged(h.conn, "Own/Proj", 41)
    await h.start()
    _merged_pr(h)
    await h.drain()
    run = h.run()
    assert run.status == "done" and run.failure is None and run.pr_number == 41
    [text] = h.chan.texts()
    assert text.startswith(f"R1's PR #41 is merged into main (you approved it in {code}).")
    assert "⚠" not in text
    (row,) = h.conn.execute(
        "SELECT detail FROM audit_log WHERE action = 'run.done_merged'"
    ).fetchall()
    assert json.loads(row["detail"])["by"] == "resonant"


async def test_a_pr_the_run_merged_on_an_approved_check_ends_done(h: Harness) -> None:
    """The owner said yes to the run's merge check and the session merged it: a merge
    Resonant has a record of, so no ⚠ either."""
    from resonant.gate.codes import new_code
    from resonant.store import approvals as ap
    from resonant.store.db import atomic
    from resonant_sdk import Effect, Level

    await h.start()
    run = h.run()
    assert run.task_id is not None
    now = utcnow()
    with atomic(h.conn):
        approval_id = ap.insert_approval(
            h.conn,
            ap.NewApproval(
                task_id=run.task_id, intent_hash="c1", tool=ap.CHECK_TOOL, effect=Effect.WRITE,
                level=Level.L2, summary="R1 wants to merge on github", approvers=("justin",),
                code=new_code(ap.pending_codes(h.conn)), expires_at=now + timedelta(minutes=30),
                requested_by="R1", action_key="github merge own/proj#41",
                action_target="own/proj#41",
            ),
            now,
        )  # fmt: skip
        h.conn.execute(
            "UPDATE approvals SET status = 'approved', decided_at = ? WHERE id = ?",
            (iso(now), approval_id),
        )
    _merged_pr(h)
    await h.drain()
    after = h.run()
    assert after.status == "done" and after.failure is None
    [text] = h.chan.texts()
    assert text.startswith("R1 (fix the flaky test) merged PR #41 into main (")
    assert "you approved it in" in text.splitlines()[0] and "⚠" not in text


async def test_a_merged_pr_obsoletes_the_open_asks_for_it(h: Harness) -> None:
    """An ask still open for the merged PR (another task's merge prompt) expires as
    obsolete when the run finds it merged, audited and never texted."""
    from resonant.gate.codes import new_code
    from resonant.loop import NewTask
    from resonant.store import approvals as ap
    from resonant.store.db import atomic
    from resonant_sdk import Effect, Level

    tid = task_store.insert_task(h.conn, NewTask(kind="message", runner="other", priority=1))
    task_store.set_status(h.conn, tid, "running")  # a brain turn waiting on its prompt
    now = utcnow()
    with atomic(h.conn):
        approval_id = ap.insert_approval(
            h.conn,
            ap.NewApproval(
                task_id=tid, intent_hash="m1", tool="merge_pr", effect=Effect.WRITE,
                level=Level.L2, summary="merge PR #41 in own/proj", approvers=("justin",),
                code=new_code(ap.pending_codes(h.conn)), expires_at=now + timedelta(minutes=30),
                extension="github", action_key="github merge own/proj#41",
                action_target="own/proj#41",
            ),
            now,
        )  # fmt: skip
    await h.start()
    _merged_pr(h)
    await h.drain()
    assert h.run().status == "failed"  # no record that Resonant merged it: the alert stays
    approval = ap.get_approval(h.conn, approval_id)
    assert approval is not None and approval.status == "expired"
    (row,) = h.conn.execute(
        "SELECT detail FROM audit_log WHERE action = 'approval.expired'"
    ).fetchall()
    assert json.loads(row["detail"])["reason"] == "obsolete"


async def test_an_unprotected_base_with_a_clean_feed_runs_normally(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    (h.gh_dir / "protected").write_text("false")
    (h.gh_dir / "activity.json").write_text(json.dumps([push_event("cd" * 20, actor="a-person")]))
    await h.start()
    await h.drain()
    assert h.run().status == "ready"
    lookups = [c for c in h.gh_calls("api") if "/activity" in c["argv"][1]]
    assert len(lookups) == 5  # push, force_push, branch_deletion, pr_merge, merge_queue_merge
    assert all("ref=refs/heads/main" in c["argv"][1] for c in lookups)
    assert h.api.requests.count(("GET", "/app")) == 1  # the bot's login, asked once


async def test_a_feed_that_cant_be_read_fails_the_run_closed(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    (h.gh_dir / "protected").write_text("false")
    (h.gh_dir / "activity_rc").write_text("1")
    await h.start()
    await h.drain()
    run = h.run()
    assert run.status == "failed" and "activity" in (run.failure or "")
    assert h.api.live == set()


# --- the startup sweep ------------------------------------------------------------------------


async def test_startup_revokes_a_token_left_by_a_run_that_already_ended(h: Harness) -> None:
    from resonant.runner.workspace import write_session_credentials

    await h.start()
    await h.drain()
    assert h.run().status == "ready" and h.api.live == set()
    # a crash after the row said "ready" but before the token was revoked
    left = (await h.app.mint("proj", workflows=False)).token
    run_dir = h.code_dir("R1")
    write_session_credentials(run_dir, left)
    other = h.cfg.work_dir / "R7"  # a dir with no creds, and a name that isn't a run's
    other.mkdir()
    (h.cfg.work_dir / "notes" / "creds").mkdir(parents=True)
    (h.cfg.work_dir / "notes" / "creds" / "token").write_text("ghs_not_a_run\n")
    assert h.api.live == {left}
    await h.runner.start()
    assert h.api.live == set() and left in h.api.revoked
    assert not (run_dir / "creds").exists()
    assert (h.cfg.work_dir / "notes" / "creds" / "token").exists()  # only R<n> dirs are touched
    await h.runner.aclose()

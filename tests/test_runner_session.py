"""The Claude session drives its own run with a per-run GitHub App token (docs/repos-plan.md D10):
minting and scoping, the credentials the session finds, refresh, revoke, PR discovery and the
not-set-up text. The runner harness of ``test_runner_claude`` with a fake GitHub App endpoint."""

from __future__ import annotations

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
    assert [m["repositories"] for m in h.api.mints] == [["proj"], ["other"]]
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
    assert h.chan.texts()[0].startswith("R1 failed: minting the run's GitHub token failed")


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
    config = (h.code_dir(code) / "repo" / ".git" / "config").read_text()
    assert f"helper = !{creds}/credential-helper" in config
    assert '[remote "origin"]' in config
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
    # the old tokens are not revoked mid-run (a running `gh pr checks --watch` may hold one)
    # but all of them are once the session is over
    assert h.api.live == set() and sorted(h.api.revoked) == sorted(h.api.tokens)
    transcript = (h.runner.transcript_dir / "R1.jsonl").read_text()
    assert not any(t in transcript for t in h.api.tokens)  # the refreshed one is redacted too


async def test_a_failed_refresh_is_retried_and_does_not_kill_the_session(
    tmp_path: Path, db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    h = build(tmp_path, db, extra=["--sleep", "1.2"], refresh_s=0.3)
    await h.start()
    real = h.api.fail_mint
    seen: list[int] = []
    orig = h.runner.app._client  # type: ignore[union-attr]  # pyright: ignore[reportPrivateUsage]

    def client() -> Any:
        seen.append(1)
        if len(seen) == 2:  # the first refresh
            h.api.fail_mint = (500, "down")
        elif len(seen) == 3:
            h.api.fail_mint = real
        return orig()

    h.runner.app._client = client  # type: ignore[union-attr, method-assign]  # pyright: ignore[reportPrivateUsage]
    await h.drain()
    assert h.run().status == "ready"
    assert "run token refresh failed" in caplog.text
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
    assert h.chan.texts()[0].startswith("R1: no changes.")
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
    assert h.chan.texts()[0].startswith(f"R1 done: PR #41 (CI {word}) · ")


async def test_a_pr_without_checks_says_no_ci(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db)
    (h.gh_dir / "checks_rc").write_text("1")
    (h.gh_dir / "checks_err").write_text("no checks reported on the 'x' branch")
    await h.start()
    await h.drain()
    assert h.chan.texts()[0].startswith("R1 done: PR #41 (no CI) · ")


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


async def test_not_set_up_names_the_app_config_and_key(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    assert await h.runner.setup_missing() is None
    del h.keychain.items[GITHUB_APP_KEY_REF]
    text = await h.runner.setup_missing()
    assert text is not None
    assert f"secret {GITHUB_APP_KEY_REF}" in text and "GitHub App's private key" in text
    assert "runner.github_app" not in text
    h.runner.cfg = h.cfg.model_copy(update={"github_app": None})
    text = await h.runner.setup_missing()
    assert text is not None
    assert "runner.github_app isn't set in the config file" in text
    assert "add runner.github_app with app_id and installation_id" in text
    res = await h.runner.start_run("proj", "x", "owner")
    assert isinstance(res, Refused) and res.reason == text
    # the daemon's own token is still part of it
    del h.keychain.items[GITHUB_TOKEN_REF]
    again = await h.runner.setup_missing()
    assert again is not None and f"secret {GITHUB_TOKEN_REF}" in again

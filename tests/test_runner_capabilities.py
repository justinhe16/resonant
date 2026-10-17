"""Graduated capability (RX-1): a worker runs as soon as the Claude credential exists; each
GitHub piece unlocks more. The behaviour matrix:

- no Claude credential: every run is refused, naming only that
- Claude only, no repo: a scratch run, as with everything set up
- Claude only, a repo with a checkout: runs from the checkout's base, commits locally, ends
  ``done`` on its local branch; a public repo with no checkout is cloned anonymously
- Claude only, a private repo with no checkout: refused, saying what to set
- Claude and the daemon's token, no App: runs from the mirror as usual, no session token,
  ends ``done`` on its local branch
- everything: the PR path (``test_runner_claude.py``), with no heads-up

Uses the harness of ``test_runner_claude.py`` (fake Claude CLI, local bare remotes, fake gh).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from test_runner_claude import GH_TOKEN, GIT, GIT_ENV, Harness, build, git

from resonant.runner.workspace import needs_github_token
from resonant.runners import claude as claude_mod
from resonant.runners.claude import Refused, Started, reply_text
from resonant.runners.reports import end_title
from resonant.secrets import (
    ANTHROPIC_KEY_REF,
    CLAUDE_TOKEN_REF,
    GITHUB_APP_KEY_REF,
    GITHUB_TOKEN_REF,
)
from resonant.store import repos as repo_store
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.store.db import atomic, utcnow

CLAUDE_TOKEN = "sk-ant-oat01-" + "Kq7Lm2Np4R" * 9


def claude_only(h: Harness) -> None:
    """Only the Claude credential in the Keychain: no daemon token, no App key."""
    h.keychain.items.clear()
    h.keychain.items[CLAUDE_TOKEN_REF] = CLAUDE_TOKEN


def owner_checkout(h: Harness, repo: str = "proj") -> Path:
    """The owner's own clone at ``<code_dir>/<repo>``, its origin the GitHub URL."""
    path = h.checkout(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([GIT, "clone", "-q", str(h.remotes[repo]), str(path)], env=GIT_ENV, check=True)
    git(path, "remote", "set-url", "origin", f"https://github.com/own/{repo}.git")
    return path


def private(h: Harness, root: Path) -> None:
    """GitHub answers nobody without a token: every repo's URL leads nowhere."""
    h.ws._url_for = lambda repo: str(root / "nowhere" / f"{repo.name}.git")  # pyright: ignore[reportPrivateUsage]


def env_config(env: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    ]


def assert_no_github_in_session(h: Harness, code: str = "R1") -> dict[str, Any]:
    """The session had no GitHub credentials at all: no gh config dir, no credential helper
    (only the reset), nothing minted, no daemon token anywhere in its env."""
    [call] = h.argv_log(code)
    env = call["env"]
    assert "GH_CONFIG_DIR" not in env
    helpers = [v for k, v in env_config(env) if k == "credential.helper"]
    assert helpers == [""]
    assert ("core.excludesFile", f"{h.code_dir(code)}/git-exclude") in env_config(env)
    assert all(GH_TOKEN not in v for v in env.values())
    assert not any(k.startswith("RESONANT_SECRET") for k in env)
    assert h.api.issued == 0
    return call


# --- no Claude credential -------------------------------------------------------------------


async def test_no_claude_credential_refuses_every_run(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    del h.keychain.items[CLAUDE_TOKEN_REF]
    want = (
        f"The runner isn't set up: a Claude login (or secret {CLAUDE_TOKEN_REF} (or "
        f"{ANTHROPIC_KEY_REF})). On the Mini: log in with `claude` in the Mini's terminal, or "
        f"resonant secret set {CLAUDE_TOKEN_REF}."
    )
    assert await h.runner.setup_missing() == want
    for repo in ("proj", None):
        assert await h.runner.start_run(repo, "x", "owner") == Refused(want)
    assert runs_store.recent(db) == []


# --- Claude only ----------------------------------------------------------------------------


async def test_claude_only_scratch_run_works(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, scenario="scratch")
    claude_only(h)
    assert await h.runner.setup_missing() is None
    res = await h.runner.start_run(None, "how full is the disk?", "owner", report="now")
    assert isinstance(res, Started) and res.limits is None
    assert "Heads up" not in res.text  # a scratch run never pushes anyway
    await h.drain()
    run = h.run()
    assert run.status == "done" and run.failure is None
    assert h.chan.texts()[0].startswith("R1 done (")
    assert h.api.issued == 0 and h.gh_calls() == [] and h.gh_calls("api") == []


async def test_claude_only_repo_with_a_checkout_commits_locally(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="happy", extra=["--no-push"])
    claude_only(h)
    checkout = owner_checkout(h)
    private(h, tmp_path)  # a private repo: no anonymous fetch, so the checkout's base is used
    base = git(checkout, "rev-parse", "origin/main")
    res = await h.runner.start_run("proj", "fix the flaky test", "owner", report="now")
    assert isinstance(res, Started)
    assert res.limits == (
        "Heads up: no push yet (GitHub App not set up); the work stays on a local branch."
    )
    assert res.text == (
        "On it: R1 · proj. Up to 45m. /runs to check, /cancel R1 to stop.\n" + res.limits
    )
    task = task_store.get_task(db, cast(str, h.run().task_id))
    assert task is not None and task.checkpoint["local"] == "app"
    await h.drain()

    run = h.run()
    branch = "resonant/r1-fix-the-flaky-test"
    assert run.status == "done" and run.branch == branch and run.base_sha == base
    assert run.failure == (
        "can't push yet: the GitHub App isn't set up "
        "(it started from your checkout's origin/main, not fetched)"
    )
    assert run.pr_number is None and run.head_sha is None
    # the work is on the local branch in the owner's checkout, nowhere else
    assert git(checkout, "log", "-1", "--format=%s", branch) == "Fix the flaky test"
    assert git(checkout, "rev-parse", f"{branch}~1") == base
    assert h.branches() == ["main"]
    call = assert_no_github_in_session(h)
    prompt = call["argv"][call["argv"].index("--append-system-prompt") + 1]
    assert "don't push" in prompt and "the GitHub App isn't set up" in prompt
    assert "gh pr create" not in prompt
    # nothing the daemon does needs the token: no gh at all, no GitHub lookups
    assert h.gh_calls() == [] and h.gh_calls("api") == []
    [text] = h.chan.texts()
    assert text.startswith("R1 done (")
    assert (
        f"): work is on local branch {branch} in {checkout}; "
        "can't push yet: the GitHub App isn't set up (it started from your checkout's "
        "origin/main, not fetched).\nR1: Fixed it." in text
    )
    assert end_title(run) == f"R1 done: local branch {branch} (not pushed)"
    # a resumed session isn't told to push either
    resumed = h.runner._turn_prompt(replace(run, sessions=1), local=True)  # pyright: ignore[reportPrivateUsage]
    assert "don't push" in resumed and "pull request" not in resumed
    # it can't be merged; the cleanup after keep_days keeps the branch (it moved)
    assert await h.runner.merge_run("R1") == Refused("R1 isn't ready to merge (done).")
    runs_store.update(db, "R1", ended_at=utcnow() - timedelta(days=8))
    deps = SimpleNamespace(settings=SimpleNamespace(runner=h.cfg), conn=db)
    job = claude_mod.cleanup_job(cast(Any, deps))
    assert job is not None
    await job.run({}, cast(Any, None))
    assert not h.worktree("R1").exists()
    assert git(checkout, "log", "-1", "--format=%s", branch) == "Fix the flaky test"


async def test_claude_only_merge_ask_is_dropped_with_a_note(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="nochanges")
    claude_only(h)
    owner_checkout(h)
    res = await h.runner.start_run("proj", "fix it and merge", "owner", merge=True)
    assert isinstance(res, Started)
    assert res.limits == "Heads up: no push yet (GitHub App not set up); so no PR and no merge."
    task = task_store.get_task(db, cast(str, h.run().task_id))
    assert task is not None and "merge" not in task.checkpoint
    await h.drain()
    assert h.run().status == "no_changes"  # nothing committed


async def test_claude_only_public_repo_is_cloned_anonymously(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="happy", extra=["--no-push"])
    claude_only(h)
    assert not h.checkout().exists()
    res = await h.runner.start_run("proj", "fix the flaky test", "owner", report="now")
    assert isinstance(res, Started) and res.limits is not None
    await h.drain()
    run = h.run()
    assert run.status == "done" and h.checkout().is_dir()
    assert run.branch is not None
    assert git(h.checkout(), "log", "-1", "--format=%s", run.branch) == "Fix the flaky test"
    assert_no_github_in_session(h)
    assert h.gh_calls() == []


async def test_claude_only_private_repo_without_a_checkout_is_refused(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    claude_only(h)
    private(h, tmp_path)
    res = await h.runner.start_run("proj", "x", "owner")
    assert isinstance(res, Refused)
    assert res.reason == (
        f"I can't work on own/proj yet: there's no checkout at {h.checkout()}, and cloning it "
        "needs the daemon's GitHub token (runner/github_token). On the Mini: resonant secret "
        f"set runner/github_token, or clone it to {h.checkout()} yourself."
    )
    assert runs_store.recent(db) == []
    # a scratch run still goes
    assert isinstance(await h.runner.start_run(None, "x", "owner"), Started)


async def test_claude_only_unknown_repo_and_fork_runs_say_what_they_need(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    with atomic(db):  # registered while the token was there: a fork-mode repo
        repo_store.add(db, "up/stream", base="main", source="auto", mode="fork", ci=True)
    h.runner.desk.trust = cast(Any, object())
    claude_only(h)
    res = await h.runner.start_run("own/elsewhere", "x", "owner")
    assert res == Refused(needs_github_token("Looking up 'own/elsewhere' on GitHub"))
    res = await h.runner.start_run("up/stream", "x", "owner")
    assert res == Refused(needs_github_token("A fork run"))
    h.keychain.items[GITHUB_TOKEN_REF] = GH_TOKEN  # the token, but still no App
    res = await h.runner.start_run("up/stream", "x", "owner")
    assert isinstance(res, Refused) and res.reason.startswith(
        "A fork run pushes to your fork with the GitHub App, which isn't set up yet."
    )
    assert "runner/github_app_key --from-file" in res.reason
    assert runs_store.recent(db) == []


# --- Claude and the daemon's token, no App ---------------------------------------------------


async def test_token_without_the_app_runs_from_the_mirror_and_commits_locally(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="happy", extra=["--no-push"])
    del h.keychain.items[GITHUB_APP_KEY_REF]
    res = await h.runner.start_run("proj", "fix the flaky test", "owner", report="now")
    assert isinstance(res, Started)
    assert res.limits == (
        "Heads up: no push yet (GitHub App not set up); the work stays on a local branch."
    )
    assert res.warning is None  # the base protection only matters to a session that can push
    await h.drain()
    run = h.run()
    assert run.status == "done" and run.failure == "can't push yet: the GitHub App isn't set up"
    assert (h.cfg.mirror_dir / "proj.git" / "HEAD").exists()  # fetched with the daemon's token
    assert run.branch is not None
    assert git(h.checkout(), "log", "-1", "--format=%s", run.branch) == "Fix the flaky test"
    assert h.branches() == ["main"]
    assert_no_github_in_session(h)
    assert h.gh_calls() == []  # no PR lookup: there is none
    # the App's config missing (not just its key) is the same run
    h.keychain.items[GITHUB_APP_KEY_REF] = "x" * 20
    h.runner.app = None
    assert (await h.runner.capabilities()).no_push == "app"


async def test_app_without_the_token_cant_push_either(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """Pushing needs both: the App for the session's token, the daemon's token for finding
    and checking the PR (and the base-push watch)."""
    h = build(tmp_path, db, scenario="happy", extra=["--no-push"])
    del h.keychain.items[GITHUB_TOKEN_REF]
    owner_checkout(h)
    res = await h.runner.start_run("proj", "fix the flaky test", "owner", report="now")
    assert isinstance(res, Started)
    assert res.limits == (
        "Heads up: no push yet (runner/github_token not set); the work stays on a local branch."
    )
    await h.drain()
    assert h.run().failure == "can't push yet: runner/github_token isn't set"
    assert_no_github_in_session(h)


# --- everything -----------------------------------------------------------------------------


async def test_everything_set_up_has_no_heads_up(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db)
    assert await h.runner.capability_notes() == []
    res = await h.runner.start_run("proj", "fix the flaky test", "owner", report="now")
    assert isinstance(res, Started) and res.limits is None and "Heads up" not in res.text
    task = task_store.get_task(db, cast(str, h.run().task_id))
    assert task is not None and "local" not in task.checkpoint
    await h.drain()
    assert h.run().status == "ready" and h.api.issued >= 1


async def test_merge_without_the_token_is_refused_on_its_own(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    code = await h.start()
    await h.drain()
    assert h.run(code).status == "ready"
    del h.keychain.items[GITHUB_TOKEN_REF]
    assert await h.runner.merge_run(code) == Refused(needs_github_token("Merging R1"))
    assert h.gh_calls("merge") == []


def test_local_reply_texts(db: sqlite3.Connection) -> None:
    blank = runs_store.create(
        db, repo="proj", ask="x", principal="owner", session_id="s", max_seconds=60
    )
    run = replace(
        blank,
        status="done",
        branch="resonant/r1-x",
        checkout=str(Path.home() / "code" / "proj"),
        failure="can't push yet: the GitHub App isn't set up",
        session_seconds=125.0,
    )
    assert reply_text(run) == (
        "R1 done (2m): work is on local branch resonant/r1-x in ~/code/proj; "
        "can't push yet: the GitHub App isn't set up."
    )
    assert reply_text(replace(run, checkout="/elsewhere/proj")) is not None


async def test_claude_only_never_falls_back_on_a_network_failure_or_a_local_base(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """The checkout's base is used only when GitHub refuses an anonymous fetch (a private
    repo), never when the network fails, and only its ``origin/<base>``: never the local
    ``<base>``, which may hold unpushed work."""
    h = build(tmp_path, db, scenario="happy", extra=["--no-push"])
    claude_only(h)
    checkout = owner_checkout(h)
    h.ws._url_for = lambda repo: "https://127.0.0.1:1/own/proj.git"  # pyright: ignore[reportPrivateUsage]
    await h.start()
    await h.drain()
    run = h.run("R1")
    assert run.status == "failed" and (run.failure or "").startswith(
        "fetching main of own/proj failed"
    )
    private(h, tmp_path)
    git(checkout, "update-ref", "-d", "refs/remotes/origin/main")
    await h.start()
    await h.drain()
    run = h.run("R2")
    assert run.status == "failed" and "has no origin/main to start from" in (run.failure or "")
    assert h.api.issued == 0


async def test_a_push_run_whose_token_went_before_prepare_fails_clearly(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    code = await h.start()
    del h.keychain.items[GITHUB_TOKEN_REF]  # removed while the run was queued
    await h.drain()
    run = h.run(code)
    assert run.status == "failed"
    assert (run.failure or "").startswith("Pushing needs the daemon's GitHub token")
    assert h.api.issued == 0 and not h.worktree(code).exists()


async def test_a_bad_local_checkpoint_value_is_ignored(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    code = await h.start()
    task = task_store.get_task(db, cast(str, h.run(code).task_id))
    assert task is not None
    db.execute(
        "UPDATE tasks SET checkpoint = ? WHERE id = ?",
        (json.dumps({**task.checkpoint, "local": ["app"]}), task.id),
    )
    await h.drain()
    assert h.run(code).status == "ready"  # a push run, as started

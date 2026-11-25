"""PER-334: the daemon's own GitHub calls for a run (finding the PR, its checks, CI, the
merge) use the GitHub App when the daemon has no token of its own.

Acceptance criteria:

1. The App configured and no ``runner/github_token``: a repo run pushes and opens its PR,
   ``ci_state`` reads the checks with an App token, the merge goes through the App, and the
   capability notes say nothing is missing.
2. Only ``runner/github_token`` (no App, or the App alongside it): as before, the daemon's
   token and no App token minted.
3. Neither: today's refusal.
4. No token in argv, env or logs; every App token revoked after its call, errors included.

Fakes only: ``FakeGitHubApi`` (an ``httpx.MockTransport``), ``FakeKeychain``, a fake ``gh``
and local bare remotes under ``tmp_path``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from github_app_fakes import FakeGitHubApi
from test_app_lookups import app_with
from test_runner_claude import GH_TOKEN as HARNESS_GH_TOKEN
from test_runner_claude import build
from test_runner_workspace import CLAUDE_TOKEN, GH_TOKEN, SECRETS, Env

from resonant.runner import workspace as workspace_mod
from resonant.runner.workspace import (
    APP_CI,
    APP_PRS,
    Exec,
    MergeRefused,
    RunnerError,
    app_merge_perms,
    needs_github_token,
)
from resonant.runners.claude import Capabilities, Merged, Refused, Started
from resonant.secrets import GITHUB_APP_KEY_REF, GITHUB_TOKEN_REF, FakeKeychain, RunnerSecrets

NO_TOKEN = RunnerSecrets(None, CLAUDE_TOKEN, None)


@pytest.fixture
def api() -> FakeGitHubApi:
    return FakeGitHubApi()


@pytest.fixture
def env(tmp_path: Path, api: FakeGitHubApi) -> Env:
    e = Env(tmp_path)
    e.ws.app = app_with(api)
    return e


def with_metadata(perms: dict[str, str]) -> dict[str, str]:
    return {"metadata": "read", **perms}


def assert_no_token_leaked(env: Env, api: FakeGitHubApi) -> None:
    """No App token in any gh argv or env, nor in any git argv or env; all revoked."""
    tokens = api.tokens
    assert tokens
    for call in env.gh_calls():
        assert all(t not in a for t in tokens for a in call["argv"])
        assert all(t not in str(v) for t in tokens for v in call["env"].values())
    logs = env.shim_log.read_text() if env.shim_log.exists() else ""
    logs += env.env_log.read_text() if env.env_log.exists() else ""
    assert all(t not in logs for t in tokens)
    assert api.live == set() and sorted(api.revoked) == sorted(tokens)


# --- the workspace --------------------------------------------------------------------------


async def test_app_only_finds_the_pr_reads_ci_and_merges(env: Env, api: FakeGitHubApi) -> None:
    p, head = await env.open_pr()
    pr = await env.ws.find_pr("proj", p.branch, NO_TOKEN)
    assert pr is not None and pr.number == 41
    assert await env.ws.ci_state("proj", 41, NO_TOKEN) == "green"
    done = await env.ws.merge("proj", 41, head, NO_TOKEN, branch=p.branch)
    assert done.checks.files == 1
    calls = env.gh_calls()
    assert [c["argv"][:2] for c in calls if c["argv"][0] == "pr"][-1] == ["pr", "merge"]
    # every gh call ran as an App token minted for it alone
    assert all(c["hosts_token"] in api.tokens for c in calls)
    assert len({c["hosts_token"] for c in calls}) == len(calls)
    assert all(m["repositories"] == ["proj"] for m in api.mints)
    perms = [m["permissions"] for m in api.mints]
    assert with_metadata(APP_PRS) in perms
    assert with_metadata(APP_CI) in perms
    assert with_metadata(app_merge_perms(workflows=False)) in perms
    assert_no_token_leaked(env, api)


async def test_app_only_ci_state_reads_the_checks_through_the_app(
    env: Env, api: FakeGitHubApi
) -> None:
    await env.open_pr()
    env.gh_set("checks_rc", "8")
    assert await env.ws.ci_state("proj", 41, NO_TOKEN) == "pending"
    [call] = env.gh_calls("checks")
    [mint] = api.mints
    assert call["hosts_token"] == mint["token"]
    assert mint["permissions"] == with_metadata(APP_CI)
    assert_no_token_leaked(env, api)


async def test_merge_with_github_changes_allowed_asks_for_workflows(
    env: Env, api: FakeGitHubApi
) -> None:
    repo = env.ws._repo("proj")  # pyright: ignore[reportPrivateUsage]
    assert not repo.allow_github_changes
    assert "workflows" not in app_merge_perms(workflows=False)
    assert app_merge_perms(workflows=True)["workflows"] == "write"


async def test_the_daemon_token_still_goes_first(env: Env, api: FakeGitHubApi) -> None:
    """With the daemon's token set nothing changes, the App set up or not."""
    p, head = await env.open_pr()
    await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch)
    assert env.gh_calls() and all(c["hosts_token"] == GH_TOKEN for c in env.gh_calls())
    assert api.issued == 0


async def test_neither_credential_is_todays_refusal(env: Env, api: FakeGitHubApi) -> None:
    p, head = await env.open_pr()
    env.ws.app = None
    with pytest.raises(RunnerError) as e:
        await env.ws.ci_state("proj", 41, NO_TOKEN)
    assert str(e.value) == needs_github_token("Asking GitHub")
    with pytest.raises(MergeRefused) as m:
        await env.ws.merge("proj", 41, head, NO_TOKEN, branch=p.branch)
    assert str(m.value) == needs_github_token("Merging")
    assert env.gh_calls() == [] and api.issued == 0


async def test_an_app_without_its_key_is_no_app(env: Env, api: FakeGitHubApi) -> None:
    await env.open_pr()
    assert env.ws.app is not None
    keychain = env.ws.app.secrets.runner
    assert isinstance(keychain, FakeKeychain)
    keychain.items.clear()
    with pytest.raises(RunnerError, match="daemon's GitHub token"):
        await env.ws.ci_state("proj", 41, NO_TOKEN)
    assert api.issued == 0


async def test_a_failed_merge_still_revokes(env: Env, api: FakeGitHubApi) -> None:
    p, head = await env.open_pr()
    env.gh_set("merge_rc", "1")
    with pytest.raises(MergeRefused, match="gh pr merge failed"):
        await env.ws.merge("proj", 41, head, NO_TOKEN, branch=p.branch)
    assert_no_token_leaked(env, api)


async def test_a_call_that_raises_still_revokes(
    env: Env, api: FakeGitHubApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    await env.open_pr()

    async def boom(*_a: Any, **_kw: Any) -> Exec:
        raise OSError("gh vanished")

    monkeypatch.setattr(workspace_mod, "_exec", boom)
    with pytest.raises(OSError):
        await env.ws.ci_state("proj", 41, NO_TOKEN)
    assert api.issued == 1 and api.live == set()


async def test_a_refused_mint_is_said_without_a_token(env: Env, api: FakeGitHubApi) -> None:
    await env.open_pr()
    api.fail_mint = (403, "Resource not accessible by integration")
    with pytest.raises(RunnerError) as e:
        await env.ws.ci_state("proj", 41, NO_TOKEN)
    assert "GitHub App" in str(e.value) and "HTTP 403" in str(e.value)
    assert env.gh_calls() == [] and api.live == set()


async def test_ci_the_daemon_token_cant_read_is_asked_again_through_the_app(
    env: Env, api: FakeGitHubApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fine-grained token can't have Checks (GitHub's 403): the App can read them."""
    await env.open_pr()
    run_gh = env.ws._run_gh  # pyright: ignore[reportPrivateUsage]

    async def forbidden_for_the_pat(
        args: Sequence[str], token: str, secrets: RunnerSecrets, stdin: bytes | None
    ) -> Exec:
        if token == GH_TOKEN:
            return Exec(4, b"", "HTTP 403: Resource not accessible by personal access token")
        return await run_gh(args, token, secrets, stdin)

    monkeypatch.setattr(env.ws, "_run_gh", forbidden_for_the_pat)
    assert await env.ws.ci_state("proj", 41, SECRETS) == "green"
    assert api.issued == 1 and api.live == set()

    # gh's usual shape for it: exit 1 with GitHub's 403 on stderr (not "red")
    async def forbidden_exit_1(
        args: Sequence[str], token: str, secrets: RunnerSecrets, stdin: bytes | None
    ) -> Exec:
        if token == GH_TOKEN:
            return Exec(1, b"", "GraphQL: Resource not accessible by personal access token")
        return await run_gh(args, token, secrets, stdin)

    monkeypatch.setattr(env.ws, "_run_gh", forbidden_exit_1)
    assert await env.ws.ci_state("proj", 41, SECRETS) == "green"
    assert api.issued == 2 and api.live == set()
    env.ws.app = None  # without the App it reads as it always did
    assert await env.ws.ci_state("proj", 41, SECRETS) == "red"


# --- capabilities ---------------------------------------------------------------------------


def test_the_app_alone_is_enough_to_push() -> None:
    app_only = Capabilities(github_token=False, app_key=True, app_config=True)
    assert app_only.no_push is None
    assert app_only.notes() == []
    everything = Capabilities(github_token=True, app_key=True, app_config=True)
    assert everything.no_push is None and everything.notes() == []


def test_without_the_app_the_notes_are_unchanged() -> None:
    token_only = Capabilities(github_token=True, app_key=False, app_config=False)
    assert token_only.no_push == "app"
    [note] = token_only.notes()
    assert note.startswith("No push or PRs yet: the GitHub App isn't set up")
    neither = Capabilities(github_token=False, app_key=False, app_config=False)
    assert neither.no_push == "app"
    assert [n.split(":")[0] for n in neither.notes()] == [
        "No push or PRs yet",
        "No merges, repo lookups, private clones, new repos or fork runs yet",
    ]


# --- a whole run ----------------------------------------------------------------------------


async def test_app_only_run_pushes_opens_its_pr_and_merges(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="happy")
    del h.keychain.items[GITHUB_TOKEN_REF]
    h.api.add_repo("own/proj")
    assert await h.runner.capability_notes() == []
    res = await h.runner.start_run("proj", "fix the flaky test", "owner", report="now")
    assert isinstance(res, Started), res
    assert res.limits is None and "Heads up" not in res.text
    await h.drain()
    run = h.run()
    assert run.status == "ready", run.failure
    assert run.pr_number is not None and h.prs()
    merged = await h.runner.merge_run(run.code)
    assert isinstance(merged, Merged), merged
    assert h.run().status == "merged"
    daemon_calls = [c for c in h.gh_calls() if c["hosts_token"] is not None]
    [merge] = h.gh_calls("merge")
    assert merge["hosts_token"] in h.api.tokens
    assert all(c["hosts_token"] != HARNESS_GH_TOKEN for c in daemon_calls)
    assert h.api.live == set()


async def test_with_neither_a_push_run_and_a_merge_are_refused_as_today(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db)
    code = await h.start()
    await h.drain()
    assert h.run(code).status == "ready"
    del h.keychain.items[GITHUB_TOKEN_REF]
    del h.keychain.items[GITHUB_APP_KEY_REF]
    assert await h.runner.merge_run(code) == Refused(needs_github_token(f"Merging {code}"))
    assert h.gh_calls("merge") == []

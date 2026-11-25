"""PER-327: repo lookups, registration and clones use the GitHub App when the daemon has no
token of its own; setup the owner must finish is said plainly in a shared thread (the owner
gets the command alone); a run's start reply says where its end will be said.

Acceptance criteria, one block each:

1. With only a GitHub App configured, an unregistered private repo that the installation
   can see registers, and the run starts (and clones with the App's read token).
2. A missing credential in a Slack thread: the thread gets a plain line with no command;
   the owner gets the command.
3. A Slack-origin run's acknowledgement never says "text".

Fakes only: the GitHub App API is ``FakeGitHubApi`` (an ``httpx.MockTransport``), the
Keychain is ``FakeKeychain``, the remotes are local bare repos under ``tmp_path``.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from github_app_fakes import FakeGitHubApi, private_pem_one_line
from test_runner_capabilities import assert_no_github_in_session, claude_only, owner_checkout
from test_runner_claude import Harness, build

from resonant.config import GitHubAppConfig
from resonant.runner.github_app import GitHubApp
from resonant.runner.resolve import AppGitHub, NoAccess, Resolved, Resolver
from resonant.runner.workspace import OWNER_SETUP, github_token_gap
from resonant.runners.claude import (
    SLACK_DONE,
    TEXT_DONE,
    Refused,
    Started,
    done_line,
    is_shared,
)
from resonant.secrets import (
    GITHUB_APP_KEY_REF,
    GITHUB_TOKEN_REF,
    FakeKeychain,
    RunnerSecrets,
    SecretStore,
)
from resonant.store import repos as repo_store
from resonant.store import runs as runs_store
from resonant.store.db import atomic

READ = {"contents": "read", "metadata": "read"}
CI = "on:\n  pull_request:\njobs: {}\n"
SLACK_THREAD = {
    "channel": "slack",
    "principal": "member",
    "identity": "slack:U2",
    "thread": "C1:1700000000.000100",
    "ref": "C1:1700000000.000100",
}


def app_with(api: FakeGitHubApi) -> GitHubApp:
    keychain = FakeKeychain()
    keychain.items[GITHUB_APP_KEY_REF] = private_pem_one_line()
    cfg = GitHubAppConfig(app_id=api.app_id, installation_id=api.installation_id)
    return GitHubApp(cfg, SecretStore(keychain), transport=api.transport(), backoff_s=0)


# --- AppGitHub ------------------------------------------------------------------------------


async def test_app_lookup_sees_a_private_repo_with_a_read_only_token() -> None:
    api = FakeGitHubApi()
    api.add_repo("own/secret", base="trunk", size=345, workflows={"ci.yml": CI})
    gh = AppGitHub(app_with(api))
    info = await gh.repo_info("own/secret")
    assert info is not None
    assert (info.slug, info.base, info.private, info.can_push, info.has_ci) == (
        "own/secret", "trunk", True, True, True,
    )  # fmt: skip
    assert not info.anonymous
    assert await gh.size_kb("own/secret") == 345  # from the lookup, no second mint
    assert [m["repositories"] for m in api.mints] == [["secret"]]
    assert [m["permissions"] for m in api.mints] == [READ]
    assert api.revoked == api.tokens  # every token revoked once used
    assert await gh.is_protected("own/secret", "trunk") is True
    assert api.revoked == api.tokens


async def test_app_lookup_without_ci_or_access() -> None:
    api = FakeGitHubApi()
    api.add_repo("own/plain")
    gh = AppGitHub(app_with(api))
    info = await gh.repo_info("own/plain")
    assert info is not None and info.has_ci is False
    # not installed there: GitHub's 422 on the mint is "can't see it", not an error
    assert await gh.repo_info("own/hidden") is None
    # a same-named repo of another account is never mistaken for it
    api.add_repo("own/shared-name")
    assert await gh.repo_info("someone/shared-name") is None
    assert await gh.is_protected("own/hidden", "main") is None
    assert await gh.size_kb("own/hidden") is None
    assert sorted(api.revoked) == sorted(api.tokens)


async def test_app_lists_the_installation_repos() -> None:
    api = FakeGitHubApi()
    api.add_repo("own/one")
    api.add_repo("own/two")
    assert sorted(await AppGitHub(app_with(api)).my_repos()) == ["own/one", "own/two"]
    [mint] = api.mints
    assert mint["repositories"] is None and mint["permissions"] == {"metadata": "read"}
    assert api.revoked == api.tokens


async def test_resolver_registers_an_app_visible_private_repo_as_push(
    db: sqlite3.Connection,
) -> None:
    api = FakeGitHubApi()
    api.add_repo("own/secret", workflows={"ci.yml": CI})
    got = await Resolver(db).resolve("own/secret", AppGitHub(app_with(api)))
    assert isinstance(got, Resolved) and got.new
    assert (got.repo.mode, got.repo.ci, got.repo.base) == ("push", True, "main")
    assert isinstance(await Resolver(db).resolve("own/hidden", AppGitHub(app_with(api))), NoAccess)


# --- 1. App only: an unregistered private repo registers and the run starts ------------------


def app_only(h: Harness) -> None:
    """No daemon token: the GitHub App (config and key) is the only GitHub credential."""
    del h.keychain.items[GITHUB_TOKEN_REF]
    assert GITHUB_APP_KEY_REF in h.keychain.items


def unregistered_private(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """``own/proj`` isn't in the registry and can't be fetched anonymously (private)."""
    with atomic(h.conn):
        repo_store.forget(h.conn, "own/proj")
    h.api.add_repo("own/proj", workflows={"ci.yml": CI})

    async def private(_name: str) -> bool:
        return False

    monkeypatch.setattr(h.ws, "can_start_tokenless", private)


async def test_app_only_registers_an_unregistered_private_repo_and_the_run_starts(
    tmp_path: Path, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = build(tmp_path, db, scenario="happy", extra=["--no-push"])
    app_only(h)
    unregistered_private(h, monkeypatch)
    tokens: list[str | None] = []
    prepare = h.ws.prepare

    async def recording(*args: Any, **kw: Any) -> Any:
        tokens.append(kw.get("github_token"))
        return await prepare(*args, **kw)

    monkeypatch.setattr(h.ws, "prepare", recording)
    res = await h.runner.start_run("own/proj", "fix the flaky test", "owner", report="now")
    assert isinstance(res, Started), res
    assert res.new_repo == "own/proj"
    repo = repo_store.get(db, "own/proj")
    assert repo is not None and (repo.mode, repo.ci) == ("push", True)
    await h.drain()
    run = h.run()
    assert run.status == "done" and run.branch is not None  # a local branch: no daemon token
    assert h.checkout().is_dir()  # cloned with the App's read token
    assert tokens and all(t is not None and t.startswith("ghs_") for t in tokens)
    assert_no_github_in_session(h, app_reads=True)
    assert all(m["repositories"] == ["proj"] for m in h.api.mints)


async def test_app_only_add_repo_tool_path_uses_the_app(
    tmp_path: Path, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repo tools' lookup and clone: ``lookup_github`` and ``clone_token``."""
    h = build(tmp_path, db)
    app_only(h)
    unregistered_private(h, monkeypatch)
    secrets = RunnerSecrets(None, None, None)
    gh = await h.runner.lookup_github(secrets)
    assert isinstance(gh, AppGitHub)
    got = await h.runner.resolver.resolve("own/proj", gh)
    assert isinstance(got, Resolved) and got.new and got.repo.mode == "push"
    async with h.runner.clone_token(got.repo, secrets) as token:
        assert token is not None and token.startswith("ghs_")
        out = await h.ws.owner_checkout(got.repo, token)
    assert out.state == "cloned"
    assert sorted(h.api.revoked) == sorted(h.api.tokens)
    # with the daemon's token, that wins and the App isn't asked
    with_token = RunnerSecrets("ghp_" + "x" * 36, None, None)
    assert not isinstance(await h.runner.lookup_github(with_token), AppGitHub)
    async with h.runner.clone_token(got.repo, with_token) as token:
        assert token == with_token.github_token
    # no App key: neither
    del h.keychain.items[GITHUB_APP_KEY_REF]
    assert await h.runner.lookup_github(secrets) is None
    async with h.runner.clone_token(got.repo, secrets) as token:
        assert token is None


async def test_a_repo_the_app_cant_see_is_said_without_a_command(
    tmp_path: Path, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = build(tmp_path, db)
    app_only(h)
    unregistered_private(h, monkeypatch)
    res = await h.runner.start_run("own/elsewhere", "x", "owner")
    assert isinstance(res, Refused)
    assert res.reason.startswith("The GitHub App can't see own/elsewhere")
    assert "secret set" not in res.reason
    assert runs_store.recent(db) == []


# --- 2. a missing credential in a Slack thread ---------------------------------------------


async def test_missing_credential_in_a_slack_thread_tells_the_owner_alone(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, owner="owner")
    claude_only(h)  # no daemon token, no App key
    res = await h.runner.start_run(
        "own/elsewhere", "x", "owner", channel_ref=SLACK_THREAD, requested_by="member"
    )
    assert isinstance(res, Refused)
    assert "secret set" not in res.reason and "resonant " not in res.reason
    assert "runner/github_token" not in res.reason
    assert res.reason == (
        "Looking up 'own/elsewhere' on GitHub needs GitHub access that isn't set up here yet. "
        f"{OWNER_SETUP}; I've sent them the details."
    )
    [(who, text)] = h.chan.sent
    assert who == "owner"
    assert "resonant secret set runner/github_token" in text
    # asked again the same day: the owner isn't told twice
    again = await h.runner.start_run(
        "own/other", "x", "owner", channel_ref=SLACK_THREAD, requested_by="member"
    )
    assert isinstance(again, Refused) and "secret set" not in again.reason
    assert len(h.chan.sent) == 1


async def test_the_owner_in_a_thread_also_gets_the_plain_line(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """A thread is shared even when the owner asked: the command goes to their DM."""
    h = build(tmp_path, db, owner="owner")
    claude_only(h)
    ref = {**SLACK_THREAD, "principal": "owner", "identity": "slack:U1"}
    res = await h.runner.start_run("own/elsewhere", "x", "owner", channel_ref=ref)
    assert isinstance(res, Refused) and "secret set" not in res.reason
    assert [who for who, _ in h.chan.sent] == ["owner"]


async def test_the_owner_alone_still_gets_the_command(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, owner="owner")
    claude_only(h)
    imessage = {"channel": "imessage", "principal": "owner", "thread": None, "ref": "g1"}
    res = await h.runner.start_run("own/elsewhere", "x", "owner", channel_ref=imessage)
    assert isinstance(res, Refused)
    assert "On the Mini: resonant secret set runner/github_token." in res.reason
    assert h.chan.sent == []


async def test_a_failed_owner_dm_still_keeps_the_command_out_of_the_thread(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, owner="owner")
    claude_only(h)
    h.chan.fail = 5
    res = await h.runner.start_run(
        "own/elsewhere", "x", "owner", channel_ref=SLACK_THREAD, requested_by="member"
    )
    assert isinstance(res, Refused)
    assert res.reason.endswith(f"{OWNER_SETUP}.") and "secret set" not in res.reason


async def test_no_clone_refusal_in_a_thread_is_plain(
    tmp_path: Path, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = build(tmp_path, db, owner="owner")
    claude_only(h)

    async def private(_name: str) -> bool:
        return False

    monkeypatch.setattr(h.ws, "can_start_tokenless", private)
    res = await h.runner.start_run(
        "proj", "x", "owner", channel_ref=SLACK_THREAD, requested_by="member"
    )
    assert isinstance(res, Refused)
    assert res.reason.startswith("I can't work on own/proj yet: there's no copy of it here")
    assert "secret set" not in res.reason and str(h.checkout()) not in res.reason
    [(who, text)] = h.chan.sent
    assert who == "owner" and "resonant secret set runner/github_token" in text


async def test_a_run_whose_checkout_went_fails_plainly_in_its_thread(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    """``SetupFailed`` at prepare: the run's failure (said in its thread) has no command."""
    h = build(tmp_path, db, scenario="happy", extra=["--no-push"], owner="owner")
    claude_only(h)
    checkout = owner_checkout(h)
    h.ws._url_for = lambda repo: str(tmp_path / "nowhere" / f"{repo.name}.git")  # pyright: ignore[reportPrivateUsage]
    res = await h.runner.start_run(
        "proj", "x", "owner", channel_ref=SLACK_THREAD, requested_by="member"
    )
    assert isinstance(res, Started)
    shutil.rmtree(checkout)  # gone before the run prepares
    await h.drain()
    run = h.run()
    assert run.status == "failed" and run.failure is not None
    assert "secret set" not in run.failure and OWNER_SETUP in run.failure
    with_cmd = [who for who, t in h.chan.sent if "resonant secret set runner/github_token" in t]
    assert with_cmd == ["owner"]  # the command went to the owner alone, once


async def test_repo_tools_in_a_thread_say_it_plainly(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, owner="owner")
    gap = github_token_gap("Adding a repo by name")
    plain = await h.runner.setup_text(gap, channel_ref=SLACK_THREAD, requester="member")
    assert "secret set" not in plain and OWNER_SETUP in plain
    assert h.chan.sent == [("owner", gap.owner_text)]
    assert await h.runner.setup_text(gap, channel_ref=None, requester="owner") == gap.owner_text


def test_is_shared() -> None:
    assert is_shared(SLACK_THREAD, "member", "owner")
    assert is_shared(None, "member", "owner")  # a member's reply is never the owner's alone
    assert is_shared({**SLACK_THREAD, "principal": "owner"}, "owner", "owner")  # a thread
    assert not is_shared({"channel": "slack", "thread": None}, "owner", "owner")  # a DM
    assert not is_shared({"channel": "imessage", "thread": None}, "owner", "owner")
    assert not is_shared(None, None, None)


# --- 3. a Slack-origin run's ack never says "text" -----------------------------------------


def test_done_line_by_channel() -> None:
    assert done_line(SLACK_THREAD, "digest") == SLACK_DONE
    assert done_line(SLACK_THREAD, "now") == SLACK_DONE
    assert done_line({"channel": "imessage"}, "now") == TEXT_DONE
    assert done_line({"channel": "imessage"}, "digest") is None  # the end goes to the digest
    assert done_line(None, "now") is None
    assert "text" not in SLACK_DONE.lower()


async def test_a_slack_run_ack_says_it_posts_here(tmp_path: Path, db: sqlite3.Connection) -> None:
    h = build(tmp_path, db, scenario="scratch", owner="owner")
    res = await h.runner.start_run(
        None, "how full is the disk?", "owner", channel_ref=SLACK_THREAD,
        requested_by="member", report="now",
    )  # fmt: skip
    assert isinstance(res, Started)
    assert SLACK_DONE in res.text
    assert "text" not in res.text.lower()
    await h.drain()


async def test_an_imessage_run_ack_says_a_text_comes(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    h = build(tmp_path, db, scenario="scratch")
    imessage = {"channel": "imessage", "principal": "owner", "thread": None, "ref": "g1"}
    res = await h.runner.start_run(
        None, "how full is the disk?", "owner", channel_ref=imessage, report="now"
    )
    assert isinstance(res, Started) and res.text.endswith(f"to stop. {TEXT_DONE}")
    await h.drain()

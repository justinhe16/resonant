"""PER-336: extension secrets minted by the instance's GitHub App.

An instance maps ``github/token`` to its App (``extensions.secret_sources``); every call that
declares the secret gets an installation token scoped by the tool's ``github_app`` block and the
call's ``repo`` arg, revoked when the call ends. Personal Resonant, with no mapping, reads the
Keychain as before. No network: the App API is ``FakeGitHubApi`` and ``gh`` is the fake shim.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from github_app_fakes import APP_ID, INSTALLATION_ID, FakeGitHubApi, private_pem_one_line
from pydantic import ValidationError

from resonant.api import DaemonState
from resonant.components import Components, app_minter, build_components
from resonant.config import GitHubAppConfig, Settings, load_settings
from resonant.e2e.extensions import install_fixture
from resonant.executor import ToolResult
from resonant.extensions.app_secrets import GitHubAppMinter
from resonant.extensions.host import ExtensionHealth
from resonant.gate import Allow, Intent
from resonant.loop import Loop, NewTask, SlotPool
from resonant.ops.fakes import FakeOpsRunner
from resonant.runner.github_app import GitHubApp
from resonant.secrets import (
    GITHUB_APP_KEY_REF,
    FakeKeychain,
    MintScope,
    SecretError,
    SecretStore,
    SecretUnavailable,
    mint_scope,
)
from resonant.store.db import utcnow
from resonant.store.tasks import insert_task
from resonant_sdk import Manifest
from resonant_sdk.manifest import parse_yaml
from resonant_sdk.tool import APP_READ_DEFAULT, AppToken

REPO = Path(__file__).resolve().parents[1]
GITHUB = REPO / "extensions/github"
FAKE_GH = REPO / "tests/fixtures/fake_gh.py"
DORI = "dori-app-code/voyager-web"
STATIC = "static-pat-not-real-0123456789"
HEAD = "a1b2c3d" + "0" * 33
APP_CFG = {"app_id": APP_ID, "installation_id": INSTALLATION_ID}
PRINCIPALS = """\
principals:
  justin:
    role: owner
    identities: ["imessage:+15551234567"]
"""


def minted_token(n: int) -> str:
    """The ``n``th token ``FakeGitHubApi`` issues (1-based)."""
    return f"ghs_{n:04d}" + "Tk" * 17


# --- SecretStore.lease and env_for --------------------------------------------------------


class FakeMinter:
    def __init__(self, refs: set[str], *, fail_on: str | None = None) -> None:
        self.refs = refs
        self.fail_on = fail_on
        self.minted: list[tuple[str, MintScope]] = []
        self.released: list[str] = []

    def mints(self, ref: str) -> bool:
        return ref in self.refs

    async def mint(self, ref: str, scope: MintScope) -> str:
        if ref == self.fail_on:
            raise SecretUnavailable(ref, "unavailable: no app")
        self.minted.append((ref, scope))
        return f"minted-{len(self.minted)}-value"

    async def release(self, value: str) -> None:
        self.released.append(value)


async def test_lease_reads_the_keychain_when_nothing_is_minted() -> None:
    kc = FakeKeychain(items={"gh/token": STATIC})
    store = SecretStore(kc)
    async with store.lease("gh", ["token"], MintScope("o/r", {"contents": "read"})) as env:
        assert env == {"RESONANT_SECRET_TOKEN": STATIC}
    assert await store.env_for("gh", ["token"]) == {"RESONANT_SECRET_TOKEN": STATIC}


async def test_lease_mints_a_mapped_ref_and_releases_it_however_the_block_ends() -> None:
    minter = FakeMinter({"gh/token"})
    store = SecretStore(FakeKeychain(items={"gh/other": "other-value-1"}), minter=minter)
    scope = MintScope(DORI, {"pull_requests": "write"})
    with pytest.raises(RuntimeError):
        async with store.lease("gh", ["token", "other"], scope) as env:
            assert env == {
                "RESONANT_SECRET_TOKEN": "minted-1-value",
                "RESONANT_SECRET_OTHER": "other-value-1",
            }
            assert minter.released == []  # alive while the call runs
            raise RuntimeError("the call blew up")
    assert minter.minted == [("gh/token", scope)]
    assert minter.released == ["minted-1-value"]


async def test_lease_releases_what_it_minted_when_a_later_secret_is_missing() -> None:
    minter = FakeMinter({"gh/token"})
    store = SecretStore(FakeKeychain(), minter=minter)
    with pytest.raises(SecretError, match="gh/other is missing"):
        async with store.lease("gh", ["token", "other"]):
            pytest.fail("the block never runs without every secret")
    assert minter.released == ["minted-1-value"]
    assert minter.minted[0][1] == MintScope()  # no scope given: the read-only default


async def test_env_for_refuses_a_minted_ref() -> None:
    """A long-running MCP server can't have its token revoked per call, so it gets none."""
    store = SecretStore(FakeKeychain(items={"gh/token": STATIC}), minter=FakeMinter({"gh/token"}))
    with pytest.raises(SecretUnavailable, match="minted per call"):
        await store.env_for("gh", ["token"])
    assert store.minted("gh/token") and not store.minted("gh/other")


def test_mint_scope_from_the_spec_and_args() -> None:
    assert mint_scope(None, {"repo": DORI}) == MintScope()
    app = AppToken(repo="repo", permissions={"contents": "write"})
    assert mint_scope(app, {"repo": DORI}) == MintScope(DORI, {"contents": "write"})
    assert mint_scope(app, {"repo": 7}).repo == ""  # never widened to every repo
    wide = mint_scope(AppToken(), {})
    assert wide.repo is None and dict(wide.permissions) == dict(APP_READ_DEFAULT)


# --- GitHubAppMinter -----------------------------------------------------------------------


def app_store(api: FakeGitHubApi) -> tuple[SecretStore, GitHubAppMinter]:
    store = SecretStore(FakeKeychain(items={GITHUB_APP_KEY_REF: private_pem_one_line()}))
    app = GitHubApp(GitHubAppConfig(**APP_CFG), store, transport=api.transport(), backoff_s=0)
    minter = GitHubAppMinter(app, ["github/token"])
    store.minter = minter
    return store, minter


async def test_minter_scopes_to_the_calls_repo_and_revokes_after() -> None:
    api = FakeGitHubApi()
    api.account = "dori-app-code"
    store, _ = app_store(api)
    scope = MintScope(DORI, {"contents": "write", "pull_requests": "write"})
    async with store.lease("github", ["token"], scope) as env:
        assert env == {"RESONANT_SECRET_TOKEN": minted_token(1)}
        assert api.revoked == []
    (mint,) = api.mints
    assert mint["repositories"] == ["voyager-web"]
    assert mint["permissions"] == {
        "metadata": "read", "contents": "write", "pull_requests": "write",
    }  # fmt: skip
    assert api.revoked == [minted_token(1)]


async def test_minter_without_a_repo_mints_for_the_installation_read_only() -> None:
    api = FakeGitHubApi()
    store, _ = app_store(api)
    async with store.lease("github", ["token"]) as env:
        assert env["RESONANT_SECRET_TOKEN"] == minted_token(1)
    (mint,) = api.mints
    assert mint["repositories"] is None  # every repo the installation sees
    assert mint["permissions"] == {"metadata": "read", **APP_READ_DEFAULT}
    assert api.revoked == [minted_token(1)]


async def test_minter_refuses_a_grant_beyond_the_request_and_revokes_it() -> None:
    api = FakeGitHubApi()
    api.grant = {"permissions": {"metadata": "read", "administration": "write"}}
    store, _ = app_store(api)
    with pytest.raises(SecretUnavailable, match="administration: write, which was not asked"):
        async with store.lease("github", ["token"]):
            pytest.fail("never runs")
    assert api.revoked == [minted_token(1)]


@pytest.mark.parametrize("repo", ["", "voyager-web", "a/b/c", "dori/..", "-x/y z"])
async def test_minter_refuses_a_repo_that_isnt_owner_name_without_asking(repo: str) -> None:
    api = FakeGitHubApi()
    store, _ = app_store(api)
    with pytest.raises(SecretUnavailable, match="one owner/name repo"):
        async with store.lease("github", ["token"], MintScope(repo, {"contents": "read"})):
            pytest.fail("never runs")
    assert api.requests == []


async def test_minter_failure_names_githubs_reason_and_no_value() -> None:
    api = FakeGitHubApi()
    api.fail_mint = (404, "Not Found")
    store, _ = app_store(api)
    with pytest.raises(SecretUnavailable) as caught:
        async with store.lease("github", ["token"], MintScope(DORI, {"contents": "read"})):
            pytest.fail("never runs")
    assert "secret github/token unavailable: asking GitHub as the App failed (HTTP 404)" in str(
        caught.value
    )
    assert "BEGIN" not in str(caught.value)


async def test_a_failed_revoke_is_logged_and_never_raised(caplog: pytest.LogCaptureFixture) -> None:
    api = FakeGitHubApi()
    api.account = "dori-app-code"
    api.fail_revoke = True
    store, _ = app_store(api)
    async with store.lease("github", ["token"], MintScope(DORI, {"pull_requests": "read"})):
        pass
    assert "extension token not revoked" in caplog.text
    assert minted_token(1) not in caplog.text


# --- manifest and config -------------------------------------------------------------------

BASE = """\
name: hub
version: 1
secrets: [token]
"""


def manifest(body: str) -> Manifest:
    return Manifest.model_validate(parse_yaml(BASE + body))


def test_manifest_default_reach_is_read_on_the_repo_arg_or_everything() -> None:
    m = manifest(
        "tools:\n"
        "  pr: {run: './cli.py pr {repo}', effect: read, args: {repo: string}, secrets: [token]}\n"
        "  all: {run: ./cli.py all, effect: read, secrets: [token]}\n"
        "  plain: {run: ./cli.py plain, effect: read}\n"
        "jobs:\n"
        "  - {id: watch, schedule: '0 * * * *', run: ./cli.py w, effect: read, secrets: [token],\n"
        "     github_app: {permissions: {checks: read}}}\n"
    )
    specs = {s.name: s for s in m.tool_specs() + m.job_specs()}
    assert specs["pr"].github_app == AppToken(repo="repo")
    assert specs["all"].github_app == AppToken()
    assert specs["plain"].github_app is None  # no secret to back
    assert specs["job_watch"].github_app == AppToken(permissions={"checks": "read"})


@pytest.mark.parametrize(
    ("body", "err"),
    [
        (
            "tools:\n  t: {run: './cli.py {n}', effect: read, args: {n: integer}, secrets: [token],"
            " github_app: {repo: n}}\n",
            "github_app.repo must name a string arg",
        ),
        (
            "tools:\n  t: {run: ./cli.py, effect: read, secrets: [token],"
            " github_app: {repo: repo}}\n",
            "github_app.repo must name a string arg",
        ),
        (
            "tools:\n  t: {run: ./cli.py, effect: write, github_app: {}}\n",
            "github_app needs a declared secret",
        ),
        (
            "tools:\n  t: {run: ./cli.py, effect: write, secrets: [token],"
            " github_app: {permissions: {administration: write}}}\n",
            "administration",
        ),
        (
            "tools:\n  t: {run: ./cli.py, effect: write, secrets: [token],"
            " github_app: {permissions: {contents: admin}}}\n",
            "contents",
        ),
        (
            "tools:\n  t: {run: ./cli.py, effect: write, secrets: [token],"
            " github_app: {permissions: {}}}\n",
            "permissions",
        ),
        (
            "jobs:\n  - {id: j, schedule: '0 * * * *', run: ./cli.py, effect: read,"
            " secrets: [token], github_app: {repo: repo}}\n",
            "jobs take no args",
        ),
    ],
)
def test_manifest_refuses_a_bad_github_app_block(body: str, err: str) -> None:
    with pytest.raises(ValidationError, match=err):
        manifest(body)


def test_config_secret_sources_need_the_app_and_a_ref() -> None:
    with pytest.raises(ValidationError, match=r"runner\.github_app isn't set"):
        load_settings(extensions={"secret_sources": {"github/token": "github_app"}})
    with pytest.raises(ValidationError, match="must be an extension secret"):
        load_settings(
            runner={"github_app": APP_CFG}, extensions={"secret_sources": {"token": "github_app"}}
        )
    with pytest.raises(ValidationError):
        load_settings(
            runner={"github_app": APP_CFG},
            extensions={"secret_sources": {"github/token": "keychain"}},
        )
    ok = load_settings(
        runner={"github_app": APP_CFG},
        extensions={"secret_sources": {"github/token": "github_app"}},
    )
    minter = app_minter(ok, SecretStore(FakeKeychain()))
    assert minter is not None and minter.mints("github/token") and not minter.mints("linear/key")
    assert app_minter(load_settings(runner={"github_app": APP_CFG}), SecretStore()) is None


# --- the github extension end to end (the ticket's "done when") ---------------------------


class Instance:
    """The real ``extensions/github`` installed with a fake ``gh`` pinned, and the daemon's
    own components (registry, gate, executor, secrets) built from ``settings``."""

    def __init__(self, settings: Settings, db: sqlite3.Connection, tmp: Path, kc: FakeKeychain):
        settings.principals_path.write_text(PRINCIPALS)
        self.state_path = tmp / "gh" / "state.json"
        self.state_path.parent.mkdir()
        shim = tmp / "bin" / "gh"
        shim.parent.mkdir()
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" -I -S "{FAKE_GH}" "{self.state_path}" "$@"\n'
        )
        shim.chmod(0o755)
        files = {rel: (GITHUB / rel).read_text() for rel in ("cli.py", "README.md")}
        install_fixture(
            settings,
            db,
            (GITHUB / "resonant.yaml").read_text(),
            files,
            argv0_paths={"bin:gh": str(shim)},
        )
        loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
        state = DaemonState(settings, db, loop, started_at=utcnow())
        self.store = SecretStore(kc)
        self.c: Components = build_components(
            settings, db, state, ops_runner=FakeOpsRunner(), secrets=self.store
        )
        self.db = db
        self.task_id = insert_task(db, NewTask(kind="k", runner="echo", priority=1))
        self.step = 0

    def gh_state(self, **state: Any) -> None:
        self.state_path.write_text(json.dumps(state))

    def gh_calls(self) -> list[dict[str, Any]]:
        path = self.state_path.parent / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    async def call(self, tool: str, args: Mapping[str, Any]) -> ToolResult:
        spec = next(s for s in self.c.registry.specs("github") if s.name == tool)
        intent = Intent.for_tool(
            spec, task_id=self.task_id, principal="justin", args=dict(args), origin="explicit"
        )
        decision = self.c.gate.evaluate(intent)
        assert isinstance(decision, Allow), decision
        self.step += 1
        return await self.c.executor.execute(intent, decision, step_no=self.step)


def gh_world(token: str, *tokens: str) -> dict[str, Any]:
    pr = {
        "number": 159, "title": "Voyager onboarding", "url": f"https://github.com/{DORI}/pull/159",
        "state": "OPEN", "isDraft": False, "author": {"login": "brian"},
        "headRefName": "onboarding", "headRefOid": HEAD, "baseRefName": "main",
        "reviewDecision": "APPROVED", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"__typename": "CheckRun", "name": "ci", "status": "COMPLETED",
                               "conclusion": "SUCCESS"}],
    }  # fmt: skip
    return {
        "token": token,
        "tokens": list(tokens),
        "prs": {f"{DORI}#159": pr},
        "api": {
            f"repos/{DORI}/pulls/159/files": [
                {"filename": "a.ts", "status": "modified", "additions": 1, "deletions": 0,
                 "patch": "+a"}
            ],
            f"repos/{DORI}/pulls/159/reviews": [
                {"user": {"login": "ana"}, "state": "APPROVED", "body": "ship it",
                 "submitted_at": "2026-11-24T10:00:00Z", "commit_id": HEAD}
            ],
            f"repos/{DORI}/pulls/159/comments": [],
            f"repos/{DORI}/issues/159/comments": [],
            "installation/repositories": {"total_count": 3, "repositories": []},
        },
    }  # fmt: skip


async def read_review_merge(inst: Instance) -> list[ToolResult]:
    """Read PR 159's checks, reviews and diff, then merge it: what Brian asked Cori for."""
    out = [
        await inst.call("pr", {"repo": DORI, "number": 159}),
        await inst.call("pr_comments", {"repo": DORI, "number": 159}),
        await inst.call("pr_diff", {"repo": DORI, "number": 159}),
        await inst.call("merge_pr", {"repo": DORI, "number": 159, "head_sha": HEAD}),
    ]
    for r in out:
        assert r.ok, (r.error, r.notice, r.reason)
    pr, comments, diff, merged = (r.data or {} for r in out)
    assert pr["author"] == "brian" and pr["ci"] == "passing" and pr["checks"][0]["name"] == "ci"
    assert comments["review_states"] == {"ana": "approved"}
    assert diff["files"][0]["patch"] == "+a"
    assert merged["summary"] == "merged voyager-web#159 (squash) at a1b2c3d"
    return out


async def test_an_instance_with_the_app_reads_reviews_checks_diff_and_merges(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    settings = load_settings(
        dry_run=False,
        runner={"github_app": APP_CFG},
        extensions={"secret_sources": {"github/token": "github_app"}},
    )
    kc = FakeKeychain(items={GITHUB_APP_KEY_REF: private_pem_one_line()})  # no github/token
    inst = Instance(settings, db, tmp_path, kc)
    api = FakeGitHubApi()
    api.account = "dori-app-code"
    minter = inst.store.minter
    assert isinstance(minter, GitHubAppMinter) and minter.mints("github/token")
    # The wiring's App, pointed at the fake GitHub API.
    minter.app = GitHubApp(GitHubAppConfig(**APP_CFG), inst.store, transport=api.transport())
    inst.gh_state(**gh_world("unused", *(minted_token(n) for n in range(1, 8))))

    results = await read_review_merge(inst)

    # One token per call, each for voyager-web only, each revoked once its call returned.
    assert [m["repositories"] for m in api.mints] == [["voyager-web"]] * 4
    assert api.revoked == api.tokens == [minted_token(n) for n in range(1, 5)]
    reads = [m["permissions"] for m in api.mints[:3]]
    assert all(set(p.values()) == {"read"} for p in reads)
    assert api.mints[3]["permissions"] == {
        "metadata": "read", "actions": "read", "contents": "write", "pull_requests": "write",
        "checks": "read", "statuses": "read",
    }  # fmt: skip
    calls = inst.gh_calls()
    assert all(c["token_ok"] for c in calls)
    assert calls[-1]["argv"][:3] == ["pr", "merge", "159"]
    # The minted token never reaches a stored result or the audit log.
    dumped = json.dumps([r.data for r in results]) + json.dumps(
        [list(row) for row in db.execute("SELECT * FROM audit_log")]
    )
    assert "ghs_" not in dumped
    assert [c.argv[-2] for c in kc.calls if "github/token" in c.argv] == []  # never read

    # The health check mints for the installation (read only) and lists its repos.
    assert inst.c.extensions is not None
    health = ExtensionHealth.from_settings(settings, inst.c.extensions, inst.store)
    got = await health.evaluate(utcnow())
    result = got["ext:github"]
    assert result is not None and result[0].ok, result
    assert api.mints[-1]["repositories"] is None and api.revoked[-1] == api.tokens[-1]


async def test_personal_resonant_with_a_static_token_is_unchanged(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    settings = load_settings(dry_run=False)  # no secret_sources, no App
    kc = FakeKeychain(items={"github/token": STATIC})
    inst = Instance(settings, db, tmp_path, kc)
    assert inst.store.minter is None
    inst.gh_state(**gh_world(STATIC))

    await read_review_merge(inst)

    assert all(c["token_ok"] for c in inst.gh_calls())
    assert [c.argv[-2] for c in kc.calls] == ["github/token"] * 4  # the Keychain, every call

"""The runner workspace: config, secrets, checkouts, worktrees and scratch dirs, the mirror, PR
checks, merge and cleanup. Local bare repos for the remote and a fake ``gh``; no network, no
Keychain, and a temp dir as ``runner.code_dir`` (never the real ~/code)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from resonant import secrets as secrets_mod
from resonant.config import RunnerConfig, RunnerRepo, load_settings
from resonant.runner import cleanup as cleanup_mod
from resonant.runner import workspace as workspace_mod
from resonant.runner.checks import CheckFailed
from resonant.runner.workspace import (
    CiPending,
    MergeRefused,
    Prepared,
    RunnerError,
    Workspace,
    branch_name,
    git_env,
    link_local_config,
    local_config_name,
    remote_slug,
    session_git_env,
    slugify,
)
from resonant.secrets import FakeKeychain, RunnerSecrets, SecretStore
from resonant.store import repos as repo_store
from resonant.store.db import connect, migrate

GIT = "/usr/bin/git"
GH_TOKEN = "github_pat_" + "A1b2C3d4E5" * 6
CLAUDE_TOKEN = "sk-ant-oat01-" + "Zy9Xw8Vu7T" * 9
SECRETS = RunnerSecrets(GH_TOKEN, CLAUDE_TOKEN, None)
ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}

FAKE_GH = """#!@PY@
import json, os, sys
d = "@STATE@"
args = sys.argv[1:]
stdin = sys.stdin.read() if "--body-file" in args else ""
conf = os.environ.get("GH_CONFIG_DIR", "")
hosts = os.path.join(conf, "hosts.yml")
hosts_token = None
if os.path.exists(hosts):
    for line in open(hosts):
        if "oauth_token:" in line:
            hosts_token = line.split(":", 1)[1].strip()
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    call = {
        "argv": args,
        "env": dict(os.environ),
        "hosts_token": hosts_token,
        "config_dir": conf,
        "stdin": stdin,
    }
    f.write(json.dumps(call) + "\\n")
def rc(name):
    p = os.path.join(d, name)
    return int(open(p).read()) if os.path.exists(p) else 0
prs = os.path.join(d, "prs.json")
if args[:2] == ["pr", "list"]:
    print(open(prs).read() if os.path.exists(prs) else "[]")
elif args[:2] == ["pr", "create"]:
    url = "https://github.com/own/proj/pull/41"
    open(prs, "w").write(json.dumps([{"number": 41, "url": url}]))
    print("Creating pull request...", file=sys.stderr)
    print(url)
elif args[:1] == ["api"]:
    def text(name, default):
        p = os.path.join(d, name)
        return open(p).read().strip() if os.path.exists(p) else default
    if "/branches/" in args[1]:
        print(json.dumps({"protected": text("protected", "true") == "true"}))
    elif "/activity" in args[1]:
        if rc("activity_rc"):
            sys.exit(rc("activity_rc"))
        print(text("activity.json", "[]"))
elif args[:2] == ["pr", "view"]:
    view = os.path.join(d, "view.json")
    if not os.path.exists(view):
        sys.exit(1)
    print(open(view).read())
elif args[:2] == ["pr", "checks"]:
    err = os.path.join(d, "checks_err")
    if os.path.exists(err):
        print(open(err).read(), file=sys.stderr)
    sys.exit(rc("checks_rc"))
elif args[:2] == ["pr", "merge"]:
    sys.exit(rc("merge_rc"))
"""

# Logs every git argv and env, then runs the real git.
GIT_SHIM = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
env >> "{envlog}"
exec {git} "$@"
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, "-C", str(cwd), *args], env=ENV, capture_output=True, text=True, check=True
    ).stdout.strip()


class Env:
    def __init__(self, root: Path, *, ci: bool = True) -> None:
        self.root = root
        self.remote = root / "remote.git"
        subprocess.run(
            [GIT, "init", "-q", "--bare", "-b", "main", str(self.remote)], env=ENV, check=True
        )
        seed = root / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        (seed / "README.md").write_text("hello\n")
        if ci:
            (seed / ".github" / "workflows").mkdir(parents=True)
            (seed / ".github" / "workflows" / "ci.yml").write_text("on:\n  pull_request:\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "base")
        git(seed, "push", "-q", str(self.remote), "main")
        self.gh = root / "bin" / "gh"
        self.gh.parent.mkdir()
        self.gh_dir = root / "gh-state"
        self.gh_dir.mkdir()
        self.gh.write_text(
            FAKE_GH.replace("@PY@", sys.executable).replace("@STATE@", str(self.gh_dir))
        )
        self.gh.chmod(0o755)
        self.shim = root / "bin" / "git"
        self.shim_log = root / "git-calls.log"
        self.env_log = root / "git-env.log"
        self.shim.write_text(GIT_SHIM.format(log=self.shim_log, envlog=self.env_log, git=GIT))
        self.shim.chmod(0o755)
        self.cfg = RunnerConfig(
            repos={"proj": RunnerRepo(remote="github.com/own/proj", allow_github_changes=False)},
            work_dir=root / "runs",
            code_dir=root / "code",
            mirror_dir=root / "state" / "mirrors",
            cache_dir=root / "runs" / ".cache",
            secrets_dir=root / "state" / "secrets",
            git_path=self.shim,
            gh_path=self.gh,
        )
        self.conn = connect(":memory:")
        migrate(self.conn)
        repo_store.import_config(self.conn, self.cfg.repos)
        self.ws = Workspace(self.cfg, self.conn, url_for=lambda repo: str(self.remote))

    def gh_calls(self, verb: str | None = None) -> list[dict[str, Any]]:
        """The fake gh's calls; with ``verb`` ("create"), only ``gh pr <verb>``."""
        path = self.gh_dir / "calls.jsonl"
        calls: list[dict[str, Any]] = (
            [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        )
        return [c for c in calls if verb is None or c["argv"][:2] == ["pr", verb]]

    def gh_set(self, name: str, text: str) -> None:
        self.gh_dir.mkdir(parents=True, exist_ok=True)
        (self.gh_dir / name).write_text(text)

    def set_pr(self, branch: str, head: str, **over: Any) -> None:
        """What ``gh pr view``/``list`` say about PR 41."""
        pr = {
            "state": "OPEN", "headRefName": branch, "headRefOid": head, "baseRefName": "main",
            "isCrossRepository": False, "title": "Fix the flaky test", "body": "Fixed.", **over,
        }  # fmt: skip
        self.gh_set("view.json", json.dumps(pr))
        url = "https://github.com/own/proj/pull/41"
        self.gh_set("prs.json", json.dumps([{"number": 41, "url": url, **pr}]))

    async def open_pr(
        self, files: dict[str, str] | None = None, *, message: str = "Fix it", **over: Any
    ) -> tuple[Prepared, str]:
        """A run whose session pushed its branch and opened PR 41 (plain git, the fake gh)."""
        p = await self.prepare()
        self.session(p, files or {"a.txt": "a\n"}, message)
        git(p.workspace, "push", "-q", str(self.remote), p.branch)
        head = git(p.workspace, "rev-parse", "HEAD")
        self.set_pr(p.branch, head, **over)
        return p, head

    @property
    def checkout(self) -> Path:
        return self.cfg.code_dir / "proj"

    def worktree(self, code: str = "R1") -> Path:
        return self.cfg.code_dir / ".worktrees" / "proj" / code

    def push_base(self, name: str, text: str = "n\n") -> None:
        """A new commit on the remote's main (someone else pushed)."""
        seed = self.root / "seed"
        (seed / name).write_text(text)
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", f"add {name}")
        git(seed, "push", "-q", str(self.remote), "main")

    def remote_branches(self) -> list[str]:
        out = git(self.remote, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        return out.split()

    async def prepare(self, code: str = "R1", ask: str = "Fix the flaky test") -> Prepared:
        return await self.ws.prepare(code, "proj", ask, github_token=GH_TOKEN)

    async def prepare_scratch(self, code: str = "R7") -> Prepared:
        return await self.ws.prepare_scratch(code)

    def session(self, p: Prepared, files: dict[str, str], message: str = "Fix it") -> None:
        """What Claude does in the worktree, with plain git."""
        for name, text in files.items():
            f = p.workspace / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        git(p.workspace, "add", "-A")
        git(p.workspace, "commit", "-q", "-m", message)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# --- config and secrets --------------------------------------------------------------------


def test_runner_repos_default_empty(resonant_home: Path) -> None:
    s = load_settings()
    assert s.runner.repos == {}
    assert s.runner.max_run_dir_mb == 5000
    assert s.runner.max_minutes == 45
    assert s.runner.work_dir == Path("~/resonant-runs").expanduser()
    assert s.runner.mirror_dir == Path("~/.resonant/runner/mirrors").expanduser()


def test_runner_yaml(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(
        "runner:\n  repos:\n    resonant:\n      remote: github.com/me/resonant.git\n"
        "      base: develop\n      quote_ask: false\n"
    )
    repo = load_settings().runner.repos["resonant"]
    assert repo.remote == "github.com/me/resonant"
    assert (repo.base, repo.quote_ask) == ("develop", False)
    assert repo.model_fields_set == {"remote", "base", "quote_ask"}  # what import_config writes
    assert repo.slug == "me/resonant"


@pytest.mark.parametrize(
    "bad",
    [
        {"remote": "https://github.com/me/x"},
        {"remote": "user:pw@github.com/me/x"},
        {"remote": "github.com/me"},
        {"remote": "github.com/me/x", "base": "-evil"},
        {"remote": "github.com/me/x", "base": "a..b"},
        {"remote": "github.com/me/x", "base": "main branch"},
    ],
)
def test_runner_repo_validation(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RunnerRepo(**bad)


def test_runner_repo_name_and_paths_validated() -> None:
    with pytest.raises(ValidationError):
        RunnerConfig(repos={"../x": RunnerRepo(remote="github.com/me/x")})
    with pytest.raises(ValidationError):
        RunnerConfig(git_path=Path("git"))
    with pytest.raises(ValidationError):
        RunnerConfig(unknown=1)  # type: ignore[call-arg]


async def test_runner_secret_names_and_size_cap() -> None:
    assert secrets_mod.GITHUB_TOKEN_REF == "runner/github_token"
    assert secrets_mod.CLAUDE_TOKEN_REF == "runner/claude_token"
    assert secrets_mod.ANTHROPIC_KEY_REF == "runner/anthropic_key"
    # A `claude setup-token` value is about 108 characters; the store's cap fits it.
    secrets_mod.validate_value(CLAUDE_TOKEN + "x" * (108 - len(CLAUDE_TOKEN)))
    assert secrets_mod.MAX_LEN >= 128
    # ``secret set`` takes the core prefix: the ref validates like any other.
    secrets_mod.validate_ref("runner/github_token")


async def test_missing_and_loaded_runner_secrets() -> None:
    kc = FakeKeychain()
    store = SecretStore(kc)
    # RX-1: only the Claude credential is required; the GitHub pieces are capabilities
    assert await secrets_mod.missing_runner_secrets(store) == [
        "runner/claude_token (or runner/anthropic_key)",
    ]
    with pytest.raises(secrets_mod.SecretMissing):
        await secrets_mod.load_runner_secrets(store)
    bare = await secrets_mod.load_runner_secrets(store, claude=False)
    assert bare.values() == [] and bare.github_token is None
    kc.items["runner/anthropic_key"] = "sk-ant-api03-abcdefgh"
    assert await secrets_mod.missing_runner_secrets(store) == []
    no_github = await secrets_mod.load_runner_secrets(store)
    assert no_github.github_token is None and no_github.github_public_token is None
    kc.items["runner/github_token"] = GH_TOKEN
    kc.items["runner/github_app_key"] = "LS0tLS1CRUdJTiBmYWtl"
    assert await secrets_mod.missing_runner_secrets(store) == []
    loaded = await secrets_mod.load_runner_secrets(store)
    assert (loaded.claude_token, loaded.anthropic_key) == (None, "sk-ant-api03-abcdefgh")
    kc.items["runner/claude_token"] = CLAUDE_TOKEN
    loaded = await secrets_mod.load_runner_secrets(store)
    assert loaded.claude_token == CLAUDE_TOKEN
    assert set(loaded.values()) == {GH_TOKEN, CLAUDE_TOKEN}


# --- helpers ---------------------------------------------------------------------------------


def test_branch_names() -> None:
    assert slugify("Fix the flaky pulse test!!") == "fix-the-flaky-pulse-test"
    assert slugify("???") == "run"
    assert branch_name("R12", "Fix: X/Y") == "resonant/r12-fix-x-y"
    assert len(slugify("a" * 100)) == 30


def test_git_env_is_hardened() -> None:
    e = git_env(Path("/x/credential-helper"))
    assert e["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert e["GIT_CONFIG_NOSYSTEM"] == "1"
    pairs = [
        (e[f"GIT_CONFIG_KEY_{i}"], e[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(e["GIT_CONFIG_COUNT"]))
    ]
    assert ("core.hooksPath", "/dev/null") in pairs
    assert pairs.index(("credential.helper", "")) < pairs.index(
        ("credential.helper", "!/x/credential-helper")
    )
    assert not any("extraHeader" in k for k, _ in pairs)
    assert e["GIT_COMMITTER_NAME"] == "Resonant runner"
    assert ("credential.helper", "!/x/credential-helper") not in [
        (git_env()[f"GIT_CONFIG_KEY_{i}"], git_env()[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(git_env()["GIT_CONFIG_COUNT"]))
    ]


# --- prepare: checkouts, worktrees, scratch dirs (docs/employee-plan.md D1) -------------------


def test_remote_slug_and_local_config_names() -> None:
    for url in (
        "https://github.com/own/proj",
        "https://github.com/own/proj.git",
        "https://x-access-token@github.com/own/proj/",
        "git@github.com:own/proj.git",
        "ssh://git@github.com/own/proj.git",
    ):
        assert remote_slug(url) == "own/proj", url
    assert remote_slug("https://gitlab.com/own/proj") is None
    assert remote_slug("/tmp/remote.git") is None
    for name in (".env", ".env.local", ".env.production", ".envrc", ".tool-versions",
                 ".python-version", ".node-version", ".mcp.json"):  # fmt: skip
        assert local_config_name(name), name
    for name in (".env.example", ".env.sample", ".env.template", ".ENV.EXAMPLE", "env", ".git",
                 ".environment", "README.md"):  # fmt: skip
        assert not local_config_name(name), name


async def test_prepare_clones_the_checkout_and_adds_a_worktree(env: Env) -> None:
    p = await env.prepare("R3", "Fix the flaky pulse test")
    assert p.branch == "resonant/r3-fix-the-flaky-pulse-test"
    assert p.workspace == env.worktree("R3") and p.checkout == env.checkout
    assert git(p.workspace, "rev-parse", "--abbrev-ref", "HEAD") == p.branch
    assert git(env.remote, "rev-parse", "main") == p.base_sha
    assert git(p.workspace, "rev-parse", "HEAD") == p.base_sha
    assert (p.workspace / "README.md").read_text() == "hello\n"
    # the checkout: cloned on first use from the plain URL, on its default branch
    assert git(env.checkout, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(env.checkout, "remote", "get-url", "origin") == str(env.remote)
    assert git(env.checkout, "status", "--porcelain") == ""
    assert str(p.workspace) in git(env.checkout, "worktree", "list", "--porcelain")
    assert stat.S_IMODE(p.run_dir.stat().st_mode) == 0o700
    assert (p.run_dir / "home").is_dir() and (p.run_dir / "tmp").is_dir()
    assert p.run_dir == env.cfg.work_dir / "R3" and not (p.run_dir / "repo").exists()
    assert env.conn.execute("SELECT ci FROM repos").fetchone()["ci"] == 1


async def test_prepare_leaves_no_token_anywhere(env: Env) -> None:
    p = await env.prepare()
    for path in [env.checkout / ".git" / "config", p.workspace / ".git"]:
        assert GH_TOKEN not in path.read_text()
    log = env.shim_log.read_text()
    assert GH_TOKEN not in log
    assert "x-access-token" not in log
    assert "clone" in log and "worktree add" in log
    assert _leftovers(env) == []


async def test_prepare_unknown_repo_and_bad_code(env: Env) -> None:
    with pytest.raises(RunnerError, match="isn't a registered repo"):
        await env.ws.prepare("R1", "nope", "x", github_token=GH_TOKEN)
    with pytest.raises(RunnerError, match="bad run code"):
        await env.ws.prepare("../R1", "proj", "x", github_token=GH_TOKEN)
    with pytest.raises(RunnerError, match="bad run code"):
        await env.ws.prepare_scratch("R1/..")


async def test_prepare_rebuilds_leftovers_and_failure_cleans_up(env: Env) -> None:
    first = await env.prepare("R1")
    (first.run_dir / "stale.txt").write_text("from a crashed attempt")
    (first.run_dir / "link").symlink_to(env.root)
    (first.workspace / "half-done.txt").write_text("from a crashed attempt")
    second = await env.prepare("R1")
    assert not (second.run_dir / "stale.txt").exists()
    assert not (second.workspace / "half-done.txt").exists()
    assert (second.workspace / "README.md").exists()
    assert (env.root / "seed").exists()  # the symlink target was not followed
    assert git(env.checkout, "worktree", "list", "--porcelain").count("worktree ") == 2
    env.conn.execute("UPDATE repos SET base = 'nope'")
    with pytest.raises(RunnerError, match=r"nope isn.t a branch of proj"):
        await env.prepare("R2")
    assert not (env.cfg.work_dir / "R2").exists() and not env.worktree("R2").exists()


async def test_prepare_fetches_without_touching_the_owners_checkout(env: Env) -> None:
    await env.prepare("R1")
    # the owner works in their checkout: another branch, a local commit, uncommitted edits
    git(env.checkout, "checkout", "-q", "-b", "mine")
    (env.checkout / "mine.txt").write_text("mine\n")
    git(env.checkout, "add", "mine.txt")
    git(env.checkout, "commit", "-q", "-m", "mine")
    (env.checkout / "README.md").write_text("owner's edit\n")
    (env.checkout / "untracked.txt").write_text("u\n")
    head = git(env.checkout, "rev-parse", "HEAD")
    env.push_base("new.txt")
    p = await env.prepare("R2")
    assert (p.workspace / "new.txt").exists() and not (p.workspace / "mine.txt").exists()
    assert git(env.checkout, "rev-parse", "origin/main") == p.base_sha
    assert git(env.checkout, "rev-parse", "--abbrev-ref", "HEAD") == "mine"
    assert git(env.checkout, "rev-parse", "HEAD") == head
    assert (env.checkout / "README.md").read_text() == "owner's edit\n"
    assert (env.checkout / "untracked.txt").exists() and not (env.checkout / "new.txt").exists()
    assert git(env.checkout, "rev-parse", "main") != p.base_sha  # their main isn't moved either


async def test_an_existing_checkout_is_used_and_must_be_the_repo(env: Env) -> None:
    owner = env.root / "elsewhere" / "proj"
    git(env.root, "clone", "-q", str(env.remote), str(owner))
    git(owner, "remote", "set-url", "origin", "git@github.com:own/proj.git")  # an ssh remote
    repo_store.set_path(env.conn, "own/proj", owner)
    p = await env.prepare("R1")
    assert p.checkout == owner and not env.checkout.exists()
    assert git(owner, "remote", "get-url", "origin") == "git@github.com:own/proj.git"
    assert git(owner, "rev-parse", "origin/main") == p.base_sha
    # a checkout of another repo, or not a checkout at all, is in the way
    git(owner, "remote", "set-url", "origin", "git@github.com:own/other.git")
    with pytest.raises(RunnerError, match="origin isn't own/proj"):
        await env.prepare("R2")
    plain = env.root / "plain"
    (plain / "sub").mkdir(parents=True)
    repo_store.set_path(env.conn, "own/proj", plain)
    with pytest.raises(RunnerError, match="isn't a git checkout of own/proj"):
        await env.prepare("R3")
    repo_store.set_path(env.conn, "own/proj", owner / ".github")  # a dir inside a checkout
    with pytest.raises(RunnerError, match="isn't a git checkout"):
        await env.prepare("R4")
    assert not any((env.cfg.work_dir / c).exists() for c in ("R2", "R3", "R4"))


async def test_local_config_is_linked_when_untracked(env: Env) -> None:
    seed = env.root / "seed"
    (seed / ".tool-versions").write_text("python 3.12\n")
    (seed / ".gitignore").write_text(".env\n.env.*\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "tool versions")
    git(seed, "push", "-q", str(env.remote), "main")
    await env.prepare("R1")  # clones the checkout
    for name in (".env", ".env.local", ".env.example", ".envrc", ".mcp.json", ".tool-versions",
                 "notes.txt"):  # fmt: skip
        (env.checkout / name).write_text(f"{name} of the owner\n")
    (env.checkout / ".env.d").mkdir()
    p = await env.prepare("R2")
    linked = sorted(n for n in os.listdir(p.workspace) if (p.workspace / n).is_symlink())
    assert linked == [".env", ".env.local", ".envrc", ".mcp.json"]
    assert os.readlink(p.workspace / ".env") == str(env.checkout / ".env")
    assert (p.workspace / ".env").read_text() == ".env of the owner\n"
    assert (p.workspace / ".tool-versions").read_text() == "python 3.12\n"  # tracked: git's copy
    assert not (p.workspace / ".env.example").exists() and not (p.workspace / "notes.txt").exists()
    assert p.exclude_file.read_text() == "/.env\n/.env.local\n/.envrc\n/.mcp.json\n"
    # with the session's git config, none of it shows up to be committed
    status = await asyncio.to_thread(
        subprocess.run,
        [GIT, "-C", str(p.workspace), "status", "--porcelain"],
        env={**ENV, **session_git_env(None, p.exclude_file)},
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert status.stdout == ""


def test_link_local_config_skips_what_is_there(tmp_path: Path) -> None:
    checkout, worktree = tmp_path / "co", tmp_path / "wt"
    checkout.mkdir()
    worktree.mkdir()
    (checkout / ".env").write_text("a\n")
    (checkout / ".envrc").write_text("b\n")
    (checkout / ".mcp.json").symlink_to(checkout / ".env")
    (worktree / ".envrc").write_text("already\n")
    assert link_local_config(checkout, worktree, tracked=[".env"]) == [".mcp.json"]
    assert (worktree / ".envrc").read_text() == "already\n"


async def test_two_runs_on_one_repo_each_get_a_worktree(env: Env) -> None:
    active = peak = 0
    real = env.ws.ensure_checkout

    async def slow(repo: repo_store.Repo, token: str | None, *, fork: str | None = None) -> Path:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        try:
            return await real(repo, token, fork=fork)
        finally:
            active -= 1

    env.ws.ensure_checkout = slow  # type: ignore[method-assign]
    a, b = await asyncio.gather(env.prepare("R1", "one"), env.prepare("R2", "two"))
    assert peak == 1  # fetch and worktree add are serialized per repo
    assert a.workspace != b.workspace and a.branch != b.branch
    assert git(a.workspace, "rev-parse", "--abbrev-ref", "HEAD") == a.branch
    assert git(b.workspace, "rev-parse", "--abbrev-ref", "HEAD") == b.branch
    assert env.ws.checkout_lock("proj") is env.ws.checkout_lock("PROJ")
    assert env.ws.checkout_lock("proj") is not env.ws.checkout_lock("other")


async def test_remove_workspace_removes_the_worktree_and_its_branch_only(env: Env) -> None:
    (env.root / "keep").mkdir()
    p = await env.prepare("R1")
    other = await env.prepare("R2")
    (env.checkout / ".env").write_text("SECRET=1\n")
    (p.workspace / ".env").symlink_to(env.checkout / ".env")
    (p.workspace / "out").symlink_to(env.root / "keep")
    env.session(p, {"a.txt": "a\n"})
    head = git(p.workspace, "rev-parse", "HEAD")
    assert await env.ws.remove_workspace(
        "R1", workspace=p.workspace, checkout=p.checkout, branch=p.branch, expect=head
    )
    assert not p.workspace.exists()
    assert (env.checkout / ".env").read_text() == "SECRET=1\n"  # links unlinked, not followed
    assert (env.root / "keep").is_dir()
    assert p.branch not in git(env.checkout, "branch", "--format=%(refname:short)")
    assert other.branch in git(env.checkout, "branch", "--format=%(refname:short)")
    assert other.workspace.is_dir() and (env.checkout / "README.md").exists()
    assert str(p.workspace) not in git(env.checkout, "worktree", "list", "--porcelain")
    # again: nothing there
    assert not await env.ws.remove_workspace(
        "R1", workspace=p.workspace, checkout=p.checkout, branch=p.branch
    )


async def test_remove_workspace_never_touches_the_owners_branch(env: Env) -> None:
    p = await env.prepare("R1")
    await env.ws.remove_workspace("R1", workspace=p.workspace, checkout=p.checkout,
                                  branch="main", expect=p.base_sha)  # fmt: skip
    assert "main" in git(env.checkout, "branch", "--format=%(refname:short)")
    assert git(env.checkout, "rev-parse", "--abbrev-ref", "HEAD") == "main"


async def test_a_run_branch_the_owner_moved_or_checked_out_is_kept(env: Env) -> None:
    def branches() -> str:
        return git(env.checkout, "branch", "--format=%(refname:short)")

    # moved: the owner committed to it after the run (the run recorded the base)
    p = await env.prepare("R1")
    env.session(p, {"owner.txt": "the owner's review fix\n"})
    await env.ws.remove_workspace("R1", workspace=p.workspace, checkout=p.checkout,
                                  branch=p.branch, expect=p.base_sha)  # fmt: skip
    assert not p.workspace.exists() and p.branch in branches()
    # checked out: the owner is on it in their checkout
    q = await env.prepare("R2")
    git(q.workspace, "checkout", "-q", "--detach")
    git(env.checkout, "checkout", "-q", q.branch)
    await env.ws.remove_workspace("R2", workspace=q.workspace, checkout=q.checkout,
                                  branch=q.branch, expect=q.base_sha)  # fmt: skip
    assert q.branch in branches()
    assert git(env.checkout, "rev-parse", "--abbrev-ref", "HEAD") == q.branch
    # no sha to compare against: kept
    r = await env.prepare("R3")
    await env.ws.remove_workspace("R3", workspace=r.workspace, checkout=r.checkout,
                                  branch=r.branch)  # fmt: skip
    assert r.branch in branches()


async def test_a_failed_clone_leaves_nothing_in_the_way(env: Env) -> None:
    real = env.remote
    env.remote = env.root / "missing.git"
    with pytest.raises(RunnerError, match="of own/proj failed"):
        await env.prepare("R1")
    assert not env.checkout.exists()
    code = env.cfg.code_dir
    assert not code.exists() or [n for n in os.listdir(code) if n.startswith(".proj")] == []
    env.remote = real
    p = await env.prepare("R2")
    assert (p.workspace / "README.md").exists()
    assert stat.S_IMODE(env.checkout.stat().st_mode) == 0o755


async def test_a_checkout_path_must_be_a_plain_checkout_apart_from_resonants_dirs(
    env: Env,
) -> None:
    for bad in (env.cfg.work_dir / "x", env.cfg.mirror_dir, env.cfg.code_dir / ".worktrees",
                env.cfg.code_dir / ".scratch" / "x", env.cfg.secrets_dir.parent):  # fmt: skip
        repo_store.set_path(env.conn, "own/proj", bad)
        with pytest.raises(RunnerError, match="overlaps Resonant's own dirs"):
            await env.prepare("R1")
    owner = env.root / "elsewhere" / "proj"
    git(env.root, "clone", "-q", str(env.remote), str(owner))
    linked = env.root / "linked"
    git(owner, "worktree", "add", "-q", str(linked))
    repo_store.set_path(env.conn, "own/proj", linked)  # a linked worktree: .git is a file
    with pytest.raises(RunnerError, match="isn't a plain checkout"):
        await env.prepare("R1")


async def test_the_checkout_never_raises_the_recorded_ci(tmp_path: Path) -> None:
    env = Env(tmp_path, ci=False)
    await env.prepare("R1")
    assert env.conn.execute("SELECT ci FROM repos").fetchone()["ci"] == 0
    # CI arrives on the base: the (worker-writable) checkout says so, but only the daemon-only
    # mirror may raise a repo recorded without CI (a merge's recheck does)
    seed = env.root / "seed"
    (seed / ".github" / "workflows").mkdir(parents=True)
    (seed / ".github" / "workflows" / "ci.yml").write_text("on:\n  pull_request:\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "ci")
    git(seed, "push", "-q", str(env.remote), "main")
    await env.prepare("R2")
    assert env.conn.execute("SELECT ci FROM repos").fetchone()["ci"] == 0
    env.conn.execute("UPDATE repos SET ci = NULL")
    await env.prepare("R3")  # unknown: the checkout's word is taken
    assert env.conn.execute("SELECT ci FROM repos").fetchone()["ci"] == 1


async def test_remove_workspace_refuses_anything_but_a_run_workspace(env: Env) -> None:
    p = await env.prepare("R1")
    root = env.cfg.code_dir
    for bad in (env.checkout, root, root / ".worktrees", root / ".worktrees" / "proj",
                root / ".worktrees" / "proj" / "R2", root / ".scratch" / "R2", Path("/"),
                env.root / "runs" / "R1", root / ".worktrees" / "../.." / "R1"):  # fmt: skip
        with pytest.raises(RunnerError, match="refusing"):
            await env.ws.remove_workspace("R1", workspace=bad, checkout=env.checkout)
    with pytest.raises(RunnerError, match="refusing"):
        await env.ws.remove_workspace("R1/..", workspace=p.workspace, checkout=env.checkout)
    assert p.workspace.is_dir() and (env.checkout / "README.md").exists()
    # a .worktrees/<name> swapped for a link to somewhere else is refused, not followed
    victim = env.root / "victim"
    (victim / "R5").mkdir(parents=True)
    (root / ".worktrees" / "evil").symlink_to(victim)
    with pytest.raises(RunnerError, match="refusing"):
        await env.ws.remove_workspace("R5", workspace=root / ".worktrees" / "evil" / "R5")
    assert (victim / "R5").is_dir()


async def test_remove_workspace_without_a_usable_checkout(env: Env) -> None:
    p = await env.prepare("R1")
    moved = env.root / "moved-away"
    env.checkout.rename(moved)  # the owner moved their checkout
    assert await env.ws.remove_workspace(
        "R1", workspace=p.workspace, checkout=p.checkout, branch=p.branch
    )
    assert not p.workspace.exists() and (moved / "README.md").exists()


async def test_scratch_dir(env: Env) -> None:
    p = await env.prepare_scratch()
    assert p.scratch and p.repo_name == "" and p.checkout is None and p.branch == ""
    assert p.workspace == env.cfg.code_dir / ".scratch" / "R7"
    assert p.workspace.is_dir() and list(p.workspace.iterdir()) == []
    assert stat.S_IMODE(p.workspace.stat().st_mode) == 0o700
    (p.workspace / "notes.md").write_text("n\n")
    again = await env.prepare_scratch()  # a crash before the session: rebuilt empty
    assert list(again.workspace.iterdir()) == []
    assert await env.ws.remove_workspace("R7", workspace=again.workspace)
    assert not again.workspace.exists() and (env.cfg.code_dir / ".scratch").is_dir()


async def test_planted_checkout_config_and_hooks_never_run_as_the_daemon(env: Env) -> None:
    p = await env.prepare("R1")
    marker = env.root / "pwned"
    git(env.checkout, "config", "core.fsmonitor", f"touch {marker}; echo")
    git(env.checkout, "config", "credential.helper", f"!touch {marker}")
    git(env.checkout, "config", "core.sshCommand", f"touch {marker}")
    for hook in ("post-checkout", "reference-transaction", "pre-auto-gc"):
        path = env.checkout / ".git" / "hooks" / hook
        path.write_text(f"#!/bin/sh\ntouch {marker}\n")
        path.chmod(0o755)
    second = await env.prepare("R2")
    await env.ws.remove_workspace("R1", workspace=p.workspace, checkout=p.checkout,
                                  branch=p.branch)  # fmt: skip
    await env.ws.remove_workspace("R2", workspace=second.workspace, checkout=second.checkout,
                                  branch=second.branch)  # fmt: skip
    assert not marker.exists()


async def test_a_planted_smudge_filter_runs_only_under_the_guard(
    env: Env, resonant_home: Path
) -> None:
    """EM-3 (S2): a worker can write its checkout's config. A filter there runs only inside
    the thin guard (as the worker could run it anyway), never as the daemon: it can't write
    Resonant's state. Only with ``runner.guard: thin``: with the guard off (GD-1) the daemon's
    git in a checkout is unwrapped, as the worker itself is."""
    env.cfg = env.cfg.model_copy(update={"guard": "thin"})
    env.ws = Workspace(env.cfg, env.conn, url_for=lambda repo: str(env.remote))
    seed = env.root / "seed"
    (seed / ".gitattributes").write_text("*.txt filter=evil\n")
    (seed / "a.txt").write_text("a\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "attributes")
    git(seed, "push", "-q", str(env.remote), "main")
    await env.prepare("R1")
    ran, daemon_only = env.root / "filter-ran", resonant_home / "filter-pwned"
    ctl = env.cfg.work_dir / ".ctl" / "R9"  # another run's socket and token
    ctl.mkdir(parents=True)
    (ctl / "token").write_text("theirs")
    forge = f"echo forged > {ctl}/token; touch {ctl}/sock; cat {ctl}/token > {ran}.read"
    git(env.checkout, "config", "filter.evil.smudge",
        f"touch {ran}; touch {daemon_only}; {forge}; cat")  # fmt: skip
    git(env.checkout, "config", "core.alternateRefsCommand", f"touch {daemon_only}; true")
    git(env.checkout, "config", "core.askPass", f"touch {daemon_only}")
    p = await env.prepare("R2")
    assert (p.workspace / "a.txt").read_text() == "a\n"
    assert ran.exists()  # it ran, in the guard
    assert not daemon_only.exists()  # never as the daemon
    assert (ctl / "token").read_text() == "theirs" and not (ctl / "sock").exists()
    leaked = env.root / "filter-ran.read"
    assert not leaked.exists() or leaked.read_text() == ""


async def test_every_git_in_a_checkout_goes_through_the_guard(env: Env) -> None:
    guarded: list[list[str]] = []

    def guard(argv: Sequence[str]) -> Sequence[str]:
        guarded.append(list(argv))
        return argv

    env.ws = Workspace(env.cfg, env.conn, url_for=lambda repo: str(env.remote), guard=guard)
    p = await env.prepare("R1")
    await env.ws.remove_workspace("R1", workspace=p.workspace, checkout=p.checkout,
                                  branch=p.branch, expect=p.base_sha)  # fmt: skip
    seen = {" ".join(c[1:]) for c in guarded}
    places = (str(env.checkout), str(p.workspace))
    for line in env.shim_log.read_text().splitlines():
        if any(line.startswith(f"-C {place} ") for place in places):
            assert line in seen, f"unguarded git in a checkout: {line}"
    assert any("worktree add" in s and "--no-checkout" in s for s in seen)
    assert any(s.startswith(f"-C {p.workspace} reset -q --hard") for s in seen)
    assert not any("worktree remove" in s for s in seen)
    assert all(GH_TOKEN not in s for s in seen)


async def test_mirror_picks_up_new_remote_commits(env: Env) -> None:
    await env.ws.refresh_mirror("proj", github_token=GH_TOKEN)
    env.push_base("new.txt")
    await env.ws.refresh_mirror("proj", github_token=GH_TOKEN)
    mirror = env.ws.mirror_path("proj")
    assert git(mirror, "rev-parse", "main") == git(env.remote, "rev-parse", "main")


async def test_mirror_lock_serializes_refreshes(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    peak = 0
    real = env.ws._refresh  # pyright: ignore[reportPrivateUsage]

    async def slow(repo_name: str, token: str) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        await real(repo_name, token)
        active -= 1

    monkeypatch.setattr(env.ws, "_refresh", slow)
    await asyncio.gather(*(env.ws.refresh_mirror("proj", github_token=GH_TOKEN) for _ in range(3)))
    assert peak == 1
    assert env.ws.mirror_lock("proj") is env.ws.mirror_lock("proj")
    assert env.ws.mirror_lock("proj") is not env.ws.mirror_lock("other")


# --- checks wired through the workspace ------------------------------------------------------


# --- merge -----------------------------------------------------------------------------------


async def test_merge_needs_green_ci_and_matches_head(env: Env) -> None:
    p, sha = await env.open_pr()
    env.gh_set("checks_rc", "1")
    with pytest.raises(MergeRefused, match="isn't green"):
        await env.ws.merge("proj", 41, sha, SECRETS, branch=p.branch)
    env.gh_set("checks_rc", "8")
    with pytest.raises(CiPending, match="still running"):  # an owner's /merge waits on it
        await env.ws.merge("proj", 41, sha, SECRETS, branch=p.branch)
    assert not env.gh_calls("merge")

    env.gh_set("checks_rc", "0")
    done = await env.ws.merge("proj", 41, sha, SECRETS, branch=p.branch)
    result = done.checks
    assert result.head_sha == sha and result.files == 1 and result.flagged == ()
    assert result.deps == () and not done.no_ci
    merge = env.gh_calls("merge")[0]
    argv: list[str] = merge["argv"]  # type: ignore[assignment]
    assert argv[:3] == ["pr", "merge", "41"]
    assert "--squash" in argv
    assert argv[argv.index("--match-head-commit") + 1] == sha
    assert merge["hosts_token"] == GH_TOKEN


async def test_merge_failure_and_bad_input(env: Env) -> None:
    p, sha = await env.open_pr()
    env.gh_set("merge_rc", "1")
    with pytest.raises(MergeRefused, match="gh pr merge failed"):
        await env.ws.merge("proj", 41, sha, SECRETS, branch=p.branch)
    with pytest.raises(MergeRefused, match="bad PR"):
        await env.ws.merge("proj", 41, "--admin", SECRETS, branch=p.branch)
    with pytest.raises(MergeRefused, match="bad PR"):
        await env.ws.merge("proj", 0, sha, SECRETS, branch=p.branch)
    with pytest.raises(MergeRefused, match="bad PR"):
        await env.ws.merge("proj", 41, sha, SECRETS, branch="--upload-pack=x")


# --- the checks before a merge (docs/repos-plan.md D10) ---------------------------------------


async def _refused(env: Env, p: Prepared, head: str, match: str) -> None:
    env.gh_set("checks_rc", "0")
    with pytest.raises(MergeRefused, match=match):
        await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch)
    assert env.gh_calls("merge") == []  # never reached the merge


async def test_merge_refuses_a_github_change(env: Env) -> None:
    p, head = await env.open_pr({".github/workflows/x.yml": "on: push\n"})
    await _refused(env, p, head, r"the checks refuse it: it changes \.github/")


async def test_merge_lists_a_github_change_when_the_repo_allows_it(env: Env) -> None:
    env.conn.execute("UPDATE repos SET allow_github_changes = 1")
    files = {".github/workflows/x.yml": "on: push\n", "Makefile": "all:\n", "uv.lock": "x\n"}
    p, head = await env.open_pr(files)
    env.gh_set("checks_rc", "0")
    done = await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch)
    assert done.checks.flagged == (".github/workflows/x.yml",)
    assert done.checks.deps == ("uv.lock",) and not done.no_ci
    assert len(env.gh_calls("merge")) == 1


async def test_merge_refuses_a_token_in_the_history_even_if_removed(env: Env) -> None:
    p = await env.prepare()
    env.session(p, {"leak.txt": f"{GH_TOKEN}\n"}, "add")
    git(p.workspace, "rm", "-q", "leak.txt")
    git(p.workspace, "commit", "-q", "-m", "remove it again")
    git(p.workspace, "push", "-q", str(env.remote), p.branch)
    head = git(p.workspace, "rev-parse", "HEAD")
    env.set_pr(p.branch, head)
    env.gh_set("checks_rc", "0")
    with pytest.raises(MergeRefused, match="a token appears") as e:
        await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch)
    assert GH_TOKEN not in str(e.value) and env.gh_calls("merge") == []


async def test_merge_refuses_a_token_shaped_string_in_a_commit_message(env: Env) -> None:
    p, head = await env.open_pr(message="use ghs_" + "A" * 36)
    await _refused(env, p, head, "a token appears in the commit object")


async def test_merge_refuses_a_token_in_the_pr_text(env: Env) -> None:
    p, _head = await env.open_pr(body=f"see {CLAUDE_TOKEN}")
    env.gh_set("checks_rc", "0")
    with pytest.raises(CheckFailed, match="pr body"):
        await env.ws.check_pr("proj", 41, p.branch, SECRETS)


async def test_merge_refuses_a_merge_commit_and_a_submodule(env: Env) -> None:
    p = await env.prepare()
    env.session(p, {"a.txt": "a\n"})
    git(p.workspace, "update-index", "--add", "--cacheinfo", f"160000,{p.base_sha},sub")
    git(p.workspace, "commit", "-q", "-m", "sub")
    git(p.workspace, "push", "-q", str(env.remote), p.branch)
    head = git(p.workspace, "rev-parse", "HEAD")
    env.set_pr(p.branch, head)
    await _refused(env, p, head, "submodule")


async def test_merge_refuses_a_pr_that_changed_or_isnt_the_runs(env: Env) -> None:
    p, head = await env.open_pr()
    env.gh_set("checks_rc", "0")
    with pytest.raises(MergeRefused, match="changed since the run finished"):
        await env.ws.merge("proj", 41, "c" * 40, SECRETS, branch=p.branch)
    for over, match in (
        ({"state": "MERGED"}, "is merged, not open"),
        ({"state": "CLOSED"}, "is closed, not open"),
        ({"baseRefName": "develop"}, "targets develop"),
        ({"isCrossRepository": True}, "isn't the run's branch"),
        ({"headRefName": "other"}, "isn't the run's branch"),
    ):
        env.set_pr(p.branch, head, **over)
        await _refused(env, p, head, match)
    env.gh_dir.joinpath("view.json").unlink()
    await _refused(env, p, head, "couldn't read the PR")


async def test_merge_notices_a_branch_that_moved_after_the_view(env: Env) -> None:
    p, head = await env.open_pr()
    env.session(p, {"b.txt": "b\n"}, "later")
    git(p.workspace, "push", "-q", str(env.remote), p.branch)
    await _refused(env, p, head, "moved while it was being checked")


async def test_pr_branch_is_fetched_with_fsck(env: Env) -> None:
    p, _head = await env.open_pr()
    env.gh_set("checks_rc", "0")
    await env.ws.check_pr("proj", 41, p.branch, SECRETS)
    fetches = [line for line in env.shim_log.read_text().splitlines() if " fetch " in f" {line} "]
    pr_fetch = next(line for line in fetches if "refs/runs/pr-41" in line)
    assert "transfer.fsckObjects=true" in pr_fetch and "fetch.fsckObjects=true" in pr_fetch
    assert git(env.ws.mirror_path("proj"), "rev-parse", "refs/runs/pr-41") == _head


async def test_find_pr_ignores_forks_and_prefers_an_open_pr(env: Env) -> None:
    url = "https://github.com/own/proj/pull/%d"
    sha = "a" * 40

    def pr(n: int, **over: Any) -> dict[str, Any]:
        return {
            "number": n, "url": url % n, "state": "OPEN", "baseRefName": "main", "headRefOid": sha,
            "isCrossRepository": False, "changedFiles": 2, **over,
        }  # fmt: skip

    assert await env.ws.find_pr("proj", "b", SECRETS) is None  # nothing listed
    env.gh_set("prs.json", json.dumps([pr(7, state="CLOSED"), pr(9, isCrossRepository=True)]))
    found = await env.ws.find_pr("proj", "b", SECRETS)
    assert found is not None and (found.number, found.state, found.files) == (7, "CLOSED", 2)
    env.gh_set("prs.json", json.dumps([pr(7, state="CLOSED"), pr(8), pr(9, state="MERGED")]))
    found = await env.ws.find_pr("proj", "b", SECRETS)
    assert found is not None and found.number == 8
    env.gh_set("prs.json", json.dumps([{"number": 1, "url": "http://evil/x"}]))
    assert await env.ws.find_pr("proj", "b", SECRETS) is None  # a bad url is no PR
    listed = env.gh_calls("list")[-1]["argv"]
    assert "--head" in listed and "--state" in listed


async def test_pushed_commits_counts_the_branch_beyond_the_base(env: Env) -> None:
    p = await env.prepare()
    assert await env.ws.pushed_commits(p, SECRETS) == 0  # never pushed
    env.session(p, {"a.txt": "a\n"})
    env.session(p, {"b.txt": "b\n"})
    git(p.workspace, "push", "-q", str(env.remote), p.branch)
    assert await env.ws.pushed_commits(p, SECRETS) == 2


async def test_ci_state(env: Env) -> None:
    for rc, err, want in (("0", "", "green"), ("8", "", "pending"), ("1", "", "red"),
                          ("1", "no checks reported on the 'x' branch", "none"),
                          ("4", "", "error")):  # fmt: skip
        env.gh_set("checks_rc", rc)
        env.gh_set("checks_err", err)
        assert await env.ws.ci_state("proj", 41, SECRETS) == want


# --- cleanup ---------------------------------------------------------------------------------


def test_rmtree_nofollow_never_follows_links(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    run = tmp_path / "R1"
    (run / "repo" / "sub").mkdir(parents=True)
    (run / "repo" / "sub" / "f.txt").write_text("x")
    (run / "link-dir").symlink_to(outside)
    (run / "repo" / "link-file").symlink_to(victim)
    (run / "repo" / "sub" / "dangling").symlink_to(tmp_path / "nowhere")
    locked = run / "locked"
    locked.mkdir()
    (locked / "g.txt").write_text("g")
    locked.chmod(0)
    readonly = run / "ro"
    readonly.mkdir()
    (readonly / "h.txt").write_text("h")
    readonly.chmod(0o500)

    assert cleanup_mod.rmtree_nofollow(run) is True
    assert not run.exists()
    assert (outside / "keep.txt").read_text() == "keep"
    assert victim.read_text() == "keep"
    assert cleanup_mod.rmtree_nofollow(run) is False


def test_rmtree_nofollow_on_a_symlink_root_only_unlinks_it(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "f").write_text("f")
    link = tmp_path / "R1"
    link.symlink_to(target)
    assert cleanup_mod.rmtree_nofollow(link) is True
    assert not link.is_symlink()
    assert (target / "f").read_text() == "f"


def test_rmtree_nofollow_refuses_a_file(tmp_path: Path) -> None:
    f = tmp_path / "R1"
    f.write_text("x")
    with pytest.raises(NotADirectoryError):
        cleanup_mod.rmtree_nofollow(f)


def test_delete_run_dir_validates_code(tmp_path: Path) -> None:
    (tmp_path / "R1").mkdir()
    with pytest.raises(ValueError):
        cleanup_mod.delete_run_dir(tmp_path, "../x")
    assert cleanup_mod.delete_run_dir(tmp_path, "R1") is True
    assert cleanup_mod.delete_run_dir(tmp_path, "R1") is False


def test_dir_size_counts_and_ignores_links(tmp_path: Path) -> None:
    big = tmp_path / "big"
    big.write_bytes(b"x" * 1_000_000)
    run = tmp_path / "run"
    run.mkdir()
    (run / "a").write_bytes(b"x" * 100_000)
    (run / "link").symlink_to(big)
    size = cleanup_mod.dir_size(run)
    assert 100_000 <= size < 500_000
    assert cleanup_mod.dir_size(tmp_path / "missing") == 0


async def test_watch_disk_fires_once_over_the_cap(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"x" * 200_000)
    seen: list[int] = []

    async def exceeded(size: int) -> None:
        seen.append(size)

    await asyncio.wait_for(cleanup_mod.watch_disk(tmp_path, 100_000, exceeded, interval_s=0.01), 5)
    assert len(seen) == 1 and seen[0] > 100_000

    task = asyncio.create_task(cleanup_mod.watch_disk(tmp_path, 10**9, exceeded, interval_s=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    assert len(seen) == 1


def test_prune_cache_removes_least_recently_used(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / "uv").mkdir(parents=True)
    now = time.time()
    for i, name in enumerate(["uv/old", "uv/mid", "new"]):
        f = cache / name
        f.write_bytes(b"x" * 100_000)
        os.utime(f, (now - 1000 + i * 100, now - 1000 + i * 100))
    outside = tmp_path / "outside"
    outside.write_text("keep")
    (cache / "link").symlink_to(outside)

    freed = cleanup_mod.prune_cache(cache, 150_000)
    assert freed >= 100_000
    assert not (cache / "uv" / "old").exists()
    assert (cache / "new").exists()
    assert outside.read_text() == "keep"
    assert cleanup_mod.prune_cache(tmp_path / "missing", 0) == 0


def test_cleanup_job_keeps_recent_and_running(tmp_path: Path) -> None:
    work = tmp_path / "runs"
    for code in ("R1", "R2", "R3", "R4"):
        (work / code / "repo").mkdir(parents=True)
    (work / ".cache").mkdir()
    now = 10_000_000.0
    day = cleanup_mod.DAY_S
    result = cleanup_mod.cleanup(
        work,
        work / ".cache",
        ended_at={"R1": now - 8 * day, "R2": now - 6 * day, "R3": now - 30 * day, "../x": 0},
        keep_days=7,
        max_cache_bytes=10**9,
        now=now,
    )
    assert result.removed_runs == ["R1", "R3"]
    assert result.errors == []
    assert (work / "R2").exists() and (work / "R4").exists()  # R4 is still running
    assert (work / ".cache").exists()


# --- review follow-ups -----------------------------------------------------------------------


def test_runner_secrets_repr_hides_values() -> None:
    text = repr(SECRETS) + str(SECRETS)
    assert GH_TOKEN not in text and CLAUDE_TOKEN not in text


def test_runner_dirs_validated(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    assert RunnerConfig(work_dir=tmp_path / "link" / "w").work_dir == real / "w"
    with pytest.raises(ValidationError, match="absolute"):
        RunnerConfig(work_dir=Path("relative/runs"))
    for mirror, work, cache in [
        (tmp_path / "w" / "m", tmp_path / "w", tmp_path / "c"),  # mirror inside work
        (tmp_path / "m", tmp_path / "m" / "w", tmp_path / "c"),  # work inside mirror
        (tmp_path / "c" / "m", tmp_path / "w", tmp_path / "c"),  # mirror inside cache
        (tmp_path / "m", tmp_path / "w", tmp_path / "m" / "c"),  # cache inside mirror
        (tmp_path / "m", tmp_path / "m", tmp_path / "c"),  # same
    ]:
        with pytest.raises(ValidationError, match="overlap"):
            RunnerConfig(mirror_dir=mirror, work_dir=work, cache_dir=cache)
    cfg = RunnerConfig(
        work_dir=tmp_path / "w", cache_dir=tmp_path / "c", deny_read=[tmp_path / "link" / "k"]
    )
    assert cfg.deny_read == [real / "k"]
    assert RunnerConfig().deny_read == []
    with pytest.raises(ValidationError, match="absolute"):
        RunnerConfig(deny_read=[Path("relative")])
    for denied in (tmp_path / "w", tmp_path, tmp_path / "w" / "x", tmp_path / "c"):
        with pytest.raises(ValidationError, match="deny_read"):
            RunnerConfig(work_dir=tmp_path / "w", cache_dir=tmp_path / "c", deny_read=[denied])
    with pytest.raises(ValidationError):
        RunnerRepo(remote="github.com/own/..")
    with pytest.raises(ValidationError):
        RunnerRepo(remote="github.com/./repo")


def test_code_dir_and_repo_path_validated(tmp_path: Path) -> None:
    assert RunnerConfig().code_dir == (Path.home() / "code").resolve()
    assert RunnerConfig(code_dir=tmp_path / "c").code_dir == (tmp_path / "c").resolve()
    with pytest.raises(ValidationError, match="absolute"):
        RunnerConfig(code_dir=Path("code"))
    for code, work, mirror in [
        (tmp_path / "w" / "code", tmp_path / "w", tmp_path / "m"),  # code inside work
        (tmp_path / "code", tmp_path / "code" / "w", tmp_path / "m"),  # work inside code
        (tmp_path / "code", tmp_path / "w", tmp_path / "code" / "m"),  # mirror inside code
    ]:
        with pytest.raises(ValidationError, match="overlap"):
            RunnerConfig(code_dir=code, work_dir=work, mirror_dir=mirror,
                         cache_dir=tmp_path / "cache")  # fmt: skip
    assert RunnerRepo(remote="github.com/o/r").path is None
    assert RunnerRepo(remote="github.com/o/r", path=Path("~/src/r")).path == Path.home() / "src/r"
    with pytest.raises(ValidationError, match="absolute"):
        RunnerRepo(remote="github.com/o/r", path=Path("src/r"))
    conn = connect(":memory:")
    migrate(conn)
    repo_store.import_config(conn, {"r": RunnerRepo(remote="github.com/o/r", path=tmp_path)})
    found = repo_store.find(conn, "r")
    assert found is not None and found.path == tmp_path
    repo_store.import_config(conn, {"r": RunnerRepo(remote="github.com/o/r")})
    found = repo_store.find(conn, "r")
    assert found is not None and found.path is None


def test_git_env_does_not_follow_redirects() -> None:
    e = git_env()
    pairs = {
        e[f"GIT_CONFIG_KEY_{i}"]: e[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(e["GIT_CONFIG_COUNT"]))
    }
    assert pairs["http.followRedirects"] == "false"


async def test_repo_without_ci_is_prepared_and_recorded(tmp_path: Path) -> None:
    env = Env(tmp_path, ci=False)
    await env.prepare()
    repo = repo_store.find(env.conn, "proj")
    assert repo is not None and repo.ci is False
    assert await env.ws.repo_has_ci("proj", github_token=GH_TOKEN) is False


async def test_repo_with_push_only_ci_counts_as_no_ci(tmp_path: Path) -> None:
    env = Env(tmp_path, ci=False)
    seed = tmp_path / "seed"
    (seed / ".github" / "workflows").mkdir(parents=True)
    (seed / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "ci on push only")
    git(seed, "push", "-q", str(env.remote), "main")
    await env.prepare()
    repo = repo_store.find(env.conn, "proj")
    assert repo is not None and repo.ci is False


async def test_merge_is_refused_without_ci_after_a_live_recheck(tmp_path: Path) -> None:
    env = Env(tmp_path, ci=False)
    p, head = await env.open_pr()
    with pytest.raises(MergeRefused, match="has no CI; add a pull_request workflow first"):
        await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch)
    assert env.gh_calls("merge") == []
    # CI appears on the remote: the recheck finds it, records it and the merge goes on.
    seed = tmp_path / "seed"
    (seed / ".github" / "workflows").mkdir(parents=True)
    (seed / ".github" / "workflows" / "ci.yml").write_text("on:\n  pull_request:\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "ci")
    git(seed, "push", "-q", str(env.remote), "main")
    await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch)
    repo = repo_store.find(env.conn, "proj")
    assert repo is not None and repo.ci is True


async def test_an_owners_merge_goes_in_without_ci_with_a_flag(tmp_path: Path) -> None:
    """GT-1: a repo with no CI at all merges on the owner's word (``allow_no_ci``), and
    says so; the auto-merge (no ``allow_no_ci``) is still refused."""
    env = Env(tmp_path, ci=False)
    p, head = await env.open_pr()
    env.gh_set("checks_rc", "1")
    env.gh_set("checks_err", "no checks reported on the 'x' branch")
    with pytest.raises(MergeRefused, match="has no CI"):
        await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch)
    assert env.gh_calls("merge") == []
    done = await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch, allow_no_ci=True)
    assert done.no_ci and len(env.gh_calls("merge")) == 1


async def test_an_owners_merge_still_refuses_red_ci_and_waits_on_pending(tmp_path: Path) -> None:
    env = Env(tmp_path, ci=False)
    p, head = await env.open_pr()
    env.gh_set("checks_rc", "1")  # red: some check (an app's) failed
    with pytest.raises(MergeRefused, match="isn't green") as e:
        await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch, allow_no_ci=True)
    assert not isinstance(e.value, CiPending)
    env.gh_set("checks_rc", "8")
    with pytest.raises(CiPending):
        await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch, allow_no_ci=True)
    assert env.gh_calls("merge") == []


async def test_an_owners_merge_waits_for_checks_not_reported_yet_in_a_repo_with_ci(
    env: Env,
) -> None:
    p, sha = await env.open_pr()
    env.gh_set("checks_rc", "1")
    env.gh_set("checks_err", "no checks reported on the 'x' branch")
    with pytest.raises(CiPending, match="no CI checks reported for this PR yet"):
        await env.ws.merge("proj", 41, sha, SECRETS, branch=p.branch, allow_no_ci=True)
    assert env.gh_calls("merge") == []


async def test_merge_allows_the_starter_ci_pr_in_a_repo_without_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from resonant.runner import create

    env = Env(tmp_path, ci=False)
    p, head = await env.open_pr()
    verdict = {"ci_only": True}

    async def fake(run: Any, slug: str, number: int, sha: str) -> bool:
        assert (slug, number, sha) == ("own/proj", 41, head)
        return verdict["ci_only"]

    monkeypatch.setattr(create, "adds_only_starter_ci", fake)
    await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch)
    assert len(env.gh_calls("merge")) == 1
    verdict["ci_only"] = False
    with pytest.raises(MergeRefused, match="has no CI"):
        await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch)


async def test_repo_has_ci(env: Env) -> None:
    assert await env.ws.repo_has_ci("proj", github_token=GH_TOKEN) is True
    await env.ws.refresh_mirror("proj", github_token=GH_TOKEN)
    assert await env.ws.repo_has_ci("proj", refresh=False) is True
    with pytest.raises(ValueError):
        await env.ws.repo_has_ci("proj")


async def test_merge_distinguishes_no_ci_checks(env: Env) -> None:
    p, sha = await env.open_pr()
    env.gh_set("checks_rc", "1")
    env.gh_set("checks_err", "no checks reported on the 'x' branch")
    with pytest.raises(MergeRefused, match="no CI checks reported"):
        await env.ws.merge("proj", 41, sha, SECRETS, branch=p.branch)
    env.gh_set("checks_err", "")
    with pytest.raises(MergeRefused, match="isn't green"):
        await env.ws.merge("proj", 41, sha, SECRETS, branch=p.branch)
    env.gh_set("checks_rc", "4")
    with pytest.raises(MergeRefused, match="errored"):
        await env.ws.merge("proj", 41, sha, SECRETS, branch=p.branch)


def _deep(root: Path, depth: int) -> None:
    """A chain of directories ``depth`` deep, built with fds (paths would be too long)."""
    root.mkdir()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    for _ in range(depth):
        os.mkdir("d", dir_fd=fd)
        nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = nxt
    os.close(fd)


def test_deep_tree_is_unmeasurable_and_not_deleted(tmp_path: Path) -> None:
    run = tmp_path / "R1"
    _deep(run, cleanup_mod.MAX_DEPTH + 20)
    assert cleanup_mod.dir_size(run) == cleanup_mod.UNMEASURABLE
    with pytest.raises(OSError, match="too deep"):
        cleanup_mod.rmtree_nofollow(run)


def test_moderately_deep_tree_is_measured_and_deleted(tmp_path: Path) -> None:
    run = tmp_path / "R1"
    _deep(run, 100)
    assert cleanup_mod.dir_size(run) < cleanup_mod.UNMEASURABLE
    assert cleanup_mod.rmtree_nofollow(run) is True
    assert not run.exists()


def test_unopenable_dir_counts_as_over_the_limit(tmp_path: Path) -> None:
    run = tmp_path / "R1"
    (run / "hidden").mkdir(parents=True)
    (run / "hidden" / "big").write_bytes(b"x" * 10_000)
    (run / "hidden").chmod(0)
    try:
        assert cleanup_mod.dir_size(run) == cleanup_mod.UNMEASURABLE
    finally:
        (run / "hidden").chmod(0o700)


def test_rmtree_clears_uchg(tmp_path: Path) -> None:
    run = tmp_path / "R1"
    (run / "sub").mkdir(parents=True)
    f = run / "sub" / "locked.txt"
    f.write_text("x")
    os.chflags(f, stat.UF_IMMUTABLE)
    assert cleanup_mod.rmtree_nofollow(run) is True
    assert not run.exists()


def test_cleanup_continues_after_a_failing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "runs"
    for code in ("R1", "R2"):
        (work / code).mkdir(parents=True)
    real = cleanup_mod.delete_run_dir

    def flaky(work_dir: Path, code: str) -> bool:
        if code == "R1":
            raise RuntimeError("boom")
        return real(work_dir, code)

    monkeypatch.setattr(cleanup_mod, "delete_run_dir", flaky)
    result = cleanup_mod.cleanup(
        work,
        work / ".cache",
        ended_at={"R1": 0.0, "R2": 0.0},
        keep_days=7,
        max_cache_bytes=10**9,
        now=10**9,
    )
    assert result.removed_runs == ["R2"]
    assert result.errors == ["R1: RuntimeError"]


async def test_exec_bounds_output(tmp_path: Path) -> None:
    from resonant.runner.checks import OutputTooLarge
    from resonant.runner.workspace import _exec  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(OutputTooLarge):
        await _exec(["/usr/bin/yes", "x" * 100], env={}, max_out=1000)


# --- the token never reaches a child's env or argv ---------------------------------------------


def test_credential_helper_hands_git_the_token(tmp_path: Path) -> None:
    with workspace_mod.secret_dir(tmp_path / "secrets") as d:
        helper = workspace_mod.write_git_credentials(d, GH_TOKEN)
        assert stat.S_IMODE((d / "token").stat().st_mode) == 0o600
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "secrets").stat().st_mode) == 0o700
        out = subprocess.run(
            [GIT, "credential", "fill"],
            env=git_env(helper),
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "username=x-access-token" in out
        assert f"password={GH_TOKEN}" in out
    assert not d.exists()


def test_gh_config_holds_the_token_in_hosts_yml(tmp_path: Path) -> None:
    with workspace_mod.secret_dir(tmp_path / "secrets") as d:
        config = workspace_mod.write_gh_config(d, GH_TOKEN)
        hosts = config / "hosts.yml"
        assert stat.S_IMODE(hosts.stat().st_mode) == 0o600
        assert f"oauth_token: {GH_TOKEN}" in hosts.read_text()
    assert not d.exists()


def _exists(path: str) -> bool:
    return Path(path).exists()


def _leftovers(env: Env) -> list[Path]:
    root = env.cfg.secrets_dir
    return list(root.iterdir()) if root.exists() else []


async def test_no_token_in_any_child_env_or_argv(env: Env) -> None:
    p, head = await env.open_pr()
    await env.ws.find_pr("proj", p.branch, SECRETS)
    env.gh_set("checks_rc", "0")
    await env.ws.merge("proj", 41, head, SECRETS, branch=p.branch)

    git_seen = env.shim_log.read_text() + env.env_log.read_text()
    assert "credential.helper" in env.env_log.read_text()  # the helper path was passed
    gh_calls = env.gh_calls()
    assert len(gh_calls) >= 3
    for text in [git_seen, json.dumps([[c["argv"], c["env"]] for c in gh_calls])]:
        for token in (GH_TOKEN, CLAUDE_TOKEN):
            assert token not in text
    for call in gh_calls:
        assert "GH_TOKEN" not in call["env"] and "GITHUB_TOKEN" not in call["env"]
        assert call["hosts_token"] == GH_TOKEN  # the file was there while gh ran
        assert not _exists(call["config_dir"])  # and gone afterwards
    assert _leftovers(env) == []


async def test_secret_files_removed_after_failure(env: Env) -> None:
    await env.prepare("R1")
    # git failure: the remote vanishes
    env.remote.rename(env.root / "gone.git")
    with pytest.raises(RunnerError, match="fetching main of own/proj failed"):
        await env.prepare("R2")
    assert _leftovers(env) == []
    env.remote = env.root / "gone.git"
    # gh failure
    env.gh.write_text("#!/bin/sh\nexit 1\n")
    with pytest.raises(RunnerError, match="gh pr list failed"):
        await env.ws.find_pr("proj", "resonant/r1-x", SECRETS)
    assert _leftovers(env) == []
    # cancelled mid-call
    with pytest.raises(asyncio.TimeoutError):
        slow = env.root / "bin" / "slowgh"
        slow.write_text("#!/bin/sh\nsleep 30\n")
        slow.chmod(0o755)
        env.cfg.gh_path = slow
        await asyncio.wait_for(env.ws.merge("proj", 41, "a" * 40, SECRETS, branch="b"), 1)
    assert _leftovers(env) == []


# --- startup wipe, deep cache, path scan ------------------------------------------------------


def test_wipe_secret_dirs_removes_leftovers_and_never_raises(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    (secrets / "call-1").mkdir(parents=True)
    (secrets / "call-1" / "token").write_text(GH_TOKEN)
    (secrets / "stray-file").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (secrets / "link").symlink_to(outside)

    assert cleanup_mod.wipe_secret_dirs(secrets) == 3
    assert list(secrets.iterdir()) == []
    assert (outside / "keep").read_text() == "keep"
    assert cleanup_mod.wipe_secret_dirs(tmp_path / "missing") == 0


def test_cleanup_wipes_secret_dirs(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    (secrets / "call-1").mkdir(parents=True)
    result = cleanup_mod.cleanup(
        tmp_path / "runs",
        tmp_path / "cache",
        secrets_dir=secrets,
        ended_at={},
        keep_days=7,
        max_cache_bytes=10**9,
    )
    assert result.errors == []
    assert list(secrets.iterdir()) == []


def test_prune_cache_survives_a_deep_tree(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _deep(cache, cleanup_mod.MAX_DEPTH + 50)
    (cache / "f").write_bytes(b"x" * 100_000)
    assert cleanup_mod.prune_cache(cache, 0) >= 100_000
    assert not (cache / "f").exists()
    result = cleanup_mod.cleanup(
        tmp_path / "runs", cache, ended_at={}, keep_days=7, max_cache_bytes=0
    )
    assert result.errors == []


def test_cleanup_records_a_prune_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cache_dir: Path, max_bytes: int) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(cleanup_mod, "prune_cache", boom)
    result = cleanup_mod.cleanup(
        tmp_path, tmp_path / "c", ended_at={}, keep_days=7, max_cache_bytes=0
    )
    assert result.errors == ["cache: RuntimeError"]


def test_secret_dir_base_is_tightened(tmp_path: Path) -> None:
    base = tmp_path / "secrets"
    base.mkdir(mode=0o755)
    with workspace_mod.secret_dir(base):
        assert stat.S_IMODE(base.stat().st_mode) == 0o700


async def test_workspace_takes_only_the_registry_name(
    env: Env,
) -> None:
    with pytest.raises(RunnerError, match="isn't a registered repo"):
        await env.ws.prepare("R1", "own/proj", "x", github_token=GH_TOKEN)
    assert not env.checkout.exists() and env.remote_branches() == ["main"]


# --- the session's credentials (docs/repos-plan.md D10) ----------------------------------------

SESSION_TOKEN = "ghs_" + "Sess10nT0ken" * 3


async def test_the_session_helper_comes_by_env_never_the_checkout_config(env: Env) -> None:
    p = await env.prepare()
    assert p.creds_dir == p.run_dir / "creds"
    assert stat.S_IMODE(p.creds_dir.stat().st_mode) == 0o700 and list(p.creds_dir.iterdir()) == []
    # nothing about the run's credentials is in the checkout's config (the owner's pushes)
    config = (env.checkout / ".git" / "config").read_text()
    assert "credential" not in config and "creds" not in config and "worktreeConfig" not in config
    assert not (env.checkout / ".git" / "worktrees" / "R1" / "config.worktree").exists()
    assert git(p.workspace, "remote", "get-url", "origin") == str(env.remote)
    e = session_git_env(p.creds_dir / "credential-helper", p.exclude_file)
    pairs = [
        (e[f"GIT_CONFIG_KEY_{i}"], e[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(e["GIT_CONFIG_COUNT"]))
    ]
    assert pairs[:2] == [
        ("credential.helper", ""),
        ("credential.helper", f"!{p.creds_dir}/credential-helper"),
    ]
    assert ("url.https://github.com/.insteadOf", "git@github.com:") in pairs
    assert ("core.excludesFile", str(p.exclude_file)) in pairs
    none = session_git_env(None)
    assert ("credential.helper", "") in [
        (none[f"GIT_CONFIG_KEY_{i}"], none[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(none["GIT_CONFIG_COUNT"]))
    ]
    assert not any(v.startswith("!") for k, v in none.items() if k.startswith("GIT_CONFIG_V"))


def test_session_credentials_files_and_modes(tmp_path: Path) -> None:
    run = tmp_path / "R1"
    run.mkdir(mode=0o700)
    creds = workspace_mod.write_session_credentials(run, SESSION_TOKEN)
    assert creds == run / "creds"
    modes = {str(f.relative_to(creds)): stat.S_IMODE(f.stat().st_mode) for f in creds.rglob("*")}
    assert modes == {
        "token": 0o600,
        "credential-helper": 0o700,
        "gh": 0o700,
        "gh/hosts.yml": 0o600,
    }
    assert stat.S_IMODE(creds.stat().st_mode) == 0o700
    assert (creds / "token").read_text() == SESSION_TOKEN + "\n"
    assert f"oauth_token: {SESSION_TOKEN}" in (creds / "gh" / "hosts.yml").read_text()
    # git gets it through the helper, exactly like the daemon's own calls do
    out = subprocess.run(
        [GIT, "credential", "fill"],
        env=git_env(creds / "credential-helper"),
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"password={SESSION_TOKEN}" in out
    assert workspace_mod.read_session_token(run) == SESSION_TOKEN


def test_session_credentials_are_refreshed_in_place(tmp_path: Path) -> None:
    run = tmp_path / "R1"
    run.mkdir(mode=0o700)
    creds = workspace_mod.write_session_credentials(run, SESSION_TOKEN)
    names = sorted(str(f.relative_to(creds)) for f in creds.rglob("*"))
    again = workspace_mod.write_session_credentials(run, "ghs_" + "N3wT0ken" * 5)
    assert again == creds and sorted(str(f.relative_to(creds)) for f in creds.rglob("*")) == names
    assert (creds / "token").read_text() == "ghs_" + "N3wT0ken" * 5 + "\n"
    assert "N3wT0ken" in (creds / "gh" / "hosts.yml").read_text()
    assert SESSION_TOKEN not in (creds / "gh" / "hosts.yml").read_text()
    assert stat.S_IMODE((creds / "token").stat().st_mode) == 0o600


def test_refresh_never_writes_through_a_planted_link(tmp_path: Path) -> None:
    run = tmp_path / "R1"
    run.mkdir(mode=0o700)
    creds = workspace_mod.write_session_credentials(run, SESSION_TOKEN)
    target = tmp_path / "victim.txt"
    target.write_text("keep me")
    (creds / "token").unlink()
    (creds / "token").symlink_to(target)  # the session replaces the file with a link
    (creds / "gh" / "hosts.yml").unlink()
    os.link(target, creds / "gh" / "hosts.yml")  # ... and the other with a hard link
    workspace_mod.write_session_credentials(run, "ghs_" + "N3wT0ken" * 5)
    assert target.read_text() == "keep me"  # nothing was written through
    assert not (creds / "token").is_symlink()
    assert (creds / "token").read_text().startswith("ghs_N3w")
    assert "N3wT0ken" in (creds / "gh" / "hosts.yml").read_text()
    # a planted symlink where a directory belongs is refused, not followed
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.rmtree(creds / "gh")
    (creds / "gh").symlink_to(elsewhere)
    with pytest.raises(RunnerError, match="plain directory"):
        workspace_mod.write_session_credentials(run, SESSION_TOKEN)
    assert list(elsewhere.iterdir()) == []
    shutil.rmtree(creds)
    creds.symlink_to(elsewhere)
    with pytest.raises(RunnerError, match="plain directory"):
        workspace_mod.write_session_credentials(run, SESSION_TOKEN)
    assert list(elsewhere.iterdir()) == []
    # and a run dir that is itself a link
    link = tmp_path / "R2"
    link.symlink_to(elsewhere)
    with pytest.raises(RunnerError, match="plain directory"):
        workspace_mod.write_session_credentials(link, SESSION_TOKEN)
    assert list(elsewhere.iterdir()) == []


def test_read_session_token_only_reads_a_plain_token_file(tmp_path: Path) -> None:
    run = tmp_path / "R1"
    run.mkdir(mode=0o700)
    assert workspace_mod.read_session_token(run) is None  # no creds yet
    creds = workspace_mod.write_session_credentials(run, SESSION_TOKEN)
    (creds / "token").unlink()
    (creds / "token").symlink_to(tmp_path / "other")
    (tmp_path / "other").write_text("ghs_nottheone\n")
    assert workspace_mod.read_session_token(run) is None
    (creds / "token").unlink()
    os.mkfifo(creds / "token")
    assert workspace_mod.read_session_token(run) is None  # a FIFO can't hang it
    (creds / "token").unlink()
    (creds / "token").write_text("two\nlines\n")
    assert workspace_mod.read_session_token(run) is None


def test_the_session_helper_answers_for_github_com_only(tmp_path: Path) -> None:
    run = tmp_path / "R1"
    run.mkdir(mode=0o700)
    creds = workspace_mod.write_session_credentials(run, SESSION_TOKEN)

    def ask(host: str) -> str:
        return subprocess.run(
            [str(creds / "credential-helper"), "get"],
            input=f"protocol=https\nhost={host}\n\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    assert f"password={SESSION_TOKEN}" in ask("github.com")
    for other in ("evil.example.com", "github.com.evil.example", "gist.github.com", ""):
        assert ask(other) == ""
    assert subprocess.run(
        [str(creds / "credential-helper"), "store"], input="host=github.com\n\n",
        capture_output=True, text=True, check=True,
    ).stdout == ""  # fmt: skip


def test_a_planted_temp_name_is_a_clean_error_not_an_oserror(tmp_path: Path) -> None:
    run = tmp_path / "R1"
    run.mkdir(mode=0o700)
    creds = workspace_mod.write_session_credentials(run, SESSION_TOKEN)
    (creds / ".token.new").mkdir()  # a directory where the next temp file goes
    with pytest.raises(RunnerError, match="credential files can't be updated"):
        workspace_mod.write_session_credentials(run, SESSION_TOKEN)
    (creds / ".token.new").rmdir()
    (creds / "token").unlink()
    (creds / "token").mkdir()  # ... or where the token file goes
    with pytest.raises(RunnerError, match="credential files can't be updated"):
        workspace_mod.write_session_credentials(run, SESSION_TOKEN)
    assert not (creds / ".token.new").exists()  # and no temp file is left behind


async def test_base_protected_and_base_pushes(env: Env) -> None:
    from datetime import UTC, datetime, timedelta

    env.gh_set("protected", "false")
    assert await env.ws.base_protected("proj", SECRETS) is False
    env.gh_set("protected", "true")
    assert await env.ws.base_protected("proj", SECRETS) is True

    now = datetime.now(UTC)

    def ev(sha: str, kind: str, actor: str, when: datetime) -> dict[str, object]:
        return {"after": sha, "activity_type": kind, "actor": {"login": actor},
                "timestamp": when.isoformat().replace("+00:00", "Z")}  # fmt: skip

    env.gh_set("activity.json", json.dumps([
        ev("a" * 40, "push", "bot[bot]", now),
        ev("b" * 40, "force_push", "BOT[bot]", now),
        ev("a" * 40, "push", "bot[bot]", now),  # a repeat
        ev("c" * 40, "push", "someone", now),
        ev("d" * 40, "push", "bot[bot]", now - timedelta(hours=2)),
        ev("e" * 40, "pr_merge", "bot[bot]", now),  # a merge onto base counts too
        ev("f" * 40, "branch_creation", "bot[bot]", now),
    ]))  # fmt: skip
    found = await env.ws.base_pushes(
        "proj", SECRETS, actor="bot[bot]", since=now - timedelta(hours=1)
    )
    assert found == ["a" * 40, "b" * 40, "e" * 40]
    # one query per type, filtered by actor on GitHub's side (a busy branch can't hide it)
    queries = [
        c["argv"][1]
        for c in env.gh_calls()
        if c["argv"][0] == "api" and "/activity" in c["argv"][1]
    ]
    assert len(queries) == 4
    assert all("actor=bot%5Bbot%5D" in q and "ref=refs/heads/main" in q for q in queries)
    assert {q.split("activity_type=")[1].split("&")[0] for q in queries} == {
        "push",
        "force_push",
        "branch_deletion",
        "pr_merge",
    }
    # anything it can't read fails closed
    odd = ev("a" * 40, "push", "bot[bot]", now) | {"timestamp": "yesterday"}
    for bad in ('{"message": "nope"}', "not json", json.dumps([{"junk": True}]), json.dumps([odd])):
        env.gh_set("activity.json", bad)
        with pytest.raises(RunnerError, match="activity couldn't be read"):
            await env.ws.base_pushes("proj", SECRETS, actor="bot[bot]", since=now)
    env.gh_set("activity_rc", "1")
    with pytest.raises(RunnerError, match="activity"):
        await env.ws.base_pushes("proj", SECRETS, actor="bot[bot]", since=now)


# --- the add-CI PR (EM-8: merge_ci_pr) ---------------------------------------------------------


async def _ci_pr(env: Env, extra: dict[str, str] | None = None) -> tuple[str, str]:
    """An add-CI branch on the remote with the starter workflow (plus ``extra``), as PR 41."""
    from resonant.runner.create import CI_PATH, ci_template

    p = await env.prepare("R9")
    branch = "resonant/add-ci-0badc0de"
    git(p.workspace, "checkout", "-q", "-b", branch)
    f = p.workspace / CI_PATH
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(ci_template())
    env.session(p, extra or {}, "Add starter CI")
    git(p.workspace, "push", "-q", str(env.remote), branch)
    head = git(p.workspace, "rev-parse", "HEAD")
    env.set_pr(branch, head, title="Add starter CI", body="Adds a pull_request workflow.")
    return branch, head


async def test_starter_ci_merge_allows_its_one_github_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from resonant.runner import create

    env = Env(tmp_path, ci=False)
    branch, head = await _ci_pr(env)
    verdict = {"ci_only": True}

    async def fake(run: Any, slug: str, number: int, sha: str) -> bool:
        return verdict["ci_only"]

    monkeypatch.setattr(create, "adds_only_starter_ci", fake)
    env.gh_set("checks_rc", "0")
    # The ordinary merge refuses it: allow_github_changes is off for this repo.
    with pytest.raises(MergeRefused, match=r"it changes \.github/"):
        await env.ws.merge("proj", 41, head, SECRETS, branch=branch)
    result = await env.ws.merge("proj", 41, head, SECRETS, branch=branch, starter_ci=True)
    assert result.checks.flagged == (create.CI_PATH,)
    assert len(env.gh_calls("merge")) == 1
    verdict["ci_only"] = False
    with pytest.raises(MergeRefused, match="isn't only the starter CI"):
        await env.ws.merge("proj", 41, head, SECRETS, branch=branch, starter_ci=True)


async def test_starter_ci_merge_is_stricter_not_looser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from resonant.runner import create

    async def yes(run: Any, slug: str, number: int, sha: str) -> bool:
        return True

    monkeypatch.setattr(create, "adds_only_starter_ci", yes)
    env = Env(tmp_path, ci=False)
    branch, head = await _ci_pr(env, {"Makefile": "all:\n"})
    env.gh_set("checks_rc", "0")
    with pytest.raises(MergeRefused, match="isn't only the starter CI"):  # anything else too
        await env.ws.merge("proj", 41, head, SECRETS, branch=branch, starter_ci=True)
    p, run_head = await env.open_pr()  # a run's branch is never the add-CI PR
    with pytest.raises(MergeRefused, match="isn't only the starter CI"):
        await env.ws.merge("proj", 41, run_head, SECRETS, branch=p.branch, starter_ci=True)
    assert env.gh_calls("merge") == []


def test_the_daemons_git_is_unwrapped_with_the_guard_off(env: Env) -> None:
    """GD-1: the default guard is off, so nothing wraps the daemon's git in a checkout."""
    assert env.cfg.guard == "off"
    ws = Workspace(env.cfg, env.conn)
    assert ws._guard(["git", "status"]) == ["git", "status"]  # pyright: ignore[reportPrivateUsage]

"""The runner workspace: config, secrets, mirror, clone, bundle handback, publish, merge and
cleanup. Local bare repos for the remote and a fake ``gh``; no network, no Keychain."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from resonant import secrets as secrets_mod
from resonant.config import RunnerConfig, RunnerRepo, load_settings
from resonant.runner import cleanup as cleanup_mod
from resonant.runner.checks import CheckFailed
from resonant.runner.workspace import (
    BundleRefused,
    MergeRefused,
    NoChanges,
    Prepared,
    PublishAborted,
    RunnerError,
    Workspace,
    branch_name,
    committer_env,
    git_env,
    slugify,
)
from resonant.secrets import FakeKeychain, RunnerSecrets, SecretStore

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

FAKE_GH = f"""#!{sys.executable}
import json, os, sys
d = os.environ["GH_CONFIG_DIR"]
args = sys.argv[1:]
stdin = sys.stdin.read() if "--body-file" in args else ""
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    call = {{"argv": args, "token": os.environ.get("GH_TOKEN"), "stdin": stdin}}
    f.write(json.dumps(call) + "\\n")
def rc(name):
    p = os.path.join(d, name)
    return int(open(p).read()) if os.path.exists(p) else 0
prs = os.path.join(d, "prs.json")
if args[:2] == ["pr", "list"]:
    print(open(prs).read() if os.path.exists(prs) else "[]")
elif args[:2] == ["pr", "create"]:
    url = "https://github.com/own/proj/pull/41"
    open(prs, "w").write(json.dumps([{{"number": 41, "url": url}}]))
    print("Creating pull request...", file=sys.stderr)
    print(url)
elif args[:2] == ["pr", "checks"]:
    sys.exit(rc("checks_rc"))
elif args[:2] == ["pr", "merge"]:
    sys.exit(rc("merge_rc"))
"""

# Logs every git argv, then runs the real git.
GIT_SHIM = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
exec {git} "$@"
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, "-C", str(cwd), *args], env=ENV, capture_output=True, text=True, check=True
    ).stdout.strip()


def _ps() -> str:
    return subprocess.run(["ps", "-axww", "-o", "command"], capture_output=True, text=True).stdout


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote = root / "remote.git"
        subprocess.run(
            [GIT, "init", "-q", "--bare", "-b", "main", str(self.remote)], env=ENV, check=True
        )
        seed = root / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        (seed / "README.md").write_text("hello\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "base")
        git(seed, "push", "-q", str(self.remote), "main")
        self.gh = root / "bin" / "gh"
        self.gh.parent.mkdir()
        self.gh.write_text(FAKE_GH)
        self.gh.chmod(0o755)
        self.shim = root / "bin" / "git"
        self.shim_log = root / "git-calls.log"
        self.shim.write_text(GIT_SHIM.format(log=self.shim_log, git=GIT))
        self.shim.chmod(0o755)
        self.cfg = RunnerConfig(
            repos={"proj": RunnerRepo(remote="github.com/own/proj")},
            work_dir=root / "runs",
            mirror_dir=root / "state" / "mirrors",
            cache_dir=root / "runs" / ".cache",
            git_path=self.shim,
            gh_path=self.gh,
        )
        self.ws = Workspace(self.cfg, url_for=lambda repo: str(self.remote))

    @property
    def gh_dir(self) -> Path:
        return self.cfg.mirror_dir.parent / "gh"

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

    def remote_branches(self) -> list[str]:
        out = git(self.remote, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        return out.split()

    async def prepare(self, code: str = "R1", ask: str = "Fix the flaky test") -> Prepared:
        return await self.ws.prepare(code, "proj", ask, github_token=GH_TOKEN)

    def session(self, p: Prepared, files: dict[str, str], message: str = "Fix it") -> None:
        """What Claude does in the clone, with plain git."""
        for name, text in files.items():
            f = p.repo_dir / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        git(p.repo_dir, "add", "-A")
        git(p.repo_dir, "commit", "-q", "-m", message)

    async def bundle(self, p: Prepared, *, env: dict[str, str] | None = None) -> None:
        await self.ws.harvest(
            p, lambda argv: list(argv), env or {"PATH": "/usr/bin:/bin", **committer_env()}
        )

    async def deliver(self, p: Prepared, files: dict[str, str], message: str = "Fix it") -> str:
        self.session(p, files, message)
        await self.bundle(p)
        return await self.ws.import_bundle(p)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


async def always() -> bool:
    return True


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
    assert (repo.base, repo.quote_ask, repo.allow_github_changes) == ("develop", False, False)
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
    assert await secrets_mod.missing_runner_secrets(store) == [
        "runner/github_token",
        "runner/claude_token (or runner/anthropic_key)",
    ]
    kc.items["runner/github_token"] = GH_TOKEN
    kc.items["runner/anthropic_key"] = "sk-ant-api03-abcdefgh"
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
    e = git_env(GH_TOKEN)
    assert e["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert e["GIT_CONFIG_NOSYSTEM"] == "1"
    pairs = {
        e[f"GIT_CONFIG_KEY_{i}"]: e[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(e["GIT_CONFIG_COUNT"]))
    }
    assert pairs["core.hooksPath"] == "/dev/null"
    assert pairs["credential.helper"] == ""
    assert pairs["http.https://github.com/.extraHeader"].startswith("Authorization: Basic ")
    assert e["GIT_COMMITTER_NAME"] == "Resonant runner"
    assert "http.https://github.com/.extraHeader" not in {
        e[f"GIT_CONFIG_KEY_{i}"] for i in range(int(git_env()["GIT_CONFIG_COUNT"]))
    }


# --- prepare ---------------------------------------------------------------------------------


async def test_prepare_clones_and_branches(env: Env) -> None:
    p = await env.prepare("R3", "Fix the flaky pulse test")
    assert p.branch == "resonant/r3-fix-the-flaky-pulse-test"
    assert p.repo_dir == env.cfg.work_dir / "R3" / "repo"
    assert git(p.repo_dir, "rev-parse", "--abbrev-ref", "HEAD") == p.branch
    assert git(p.repo_dir, "rev-parse", "main") == p.base_sha
    assert (p.repo_dir / "README.md").read_text() == "hello\n"
    assert git(p.repo_dir, "remote", "get-url", "origin") == str(env.remote)
    assert stat.S_IMODE(p.run_dir.stat().st_mode) == 0o700
    assert (p.run_dir / "home").is_dir() and (p.run_dir / "tmp").is_dir()
    # no hardlinks into the mirror
    mirror_obj = next((env.ws.mirror_path("proj") / "objects").glob("??/*"))
    clone_obj = p.repo_dir / ".git" / "objects" / mirror_obj.parent.name / mirror_obj.name
    if clone_obj.exists():
        assert clone_obj.stat().st_ino != mirror_obj.stat().st_ino


async def test_prepare_leaves_no_token_anywhere(env: Env) -> None:
    p = await env.prepare()
    for path in [p.repo_dir / ".git" / "config", env.ws.mirror_path("proj") / "config"]:
        assert GH_TOKEN not in path.read_text()
    log = env.shim_log.read_text()
    assert GH_TOKEN not in log
    assert "x-access-token" not in log
    assert "clone" in log and "checkout -q -b" in log


async def test_prepare_unknown_repo_and_bad_code(env: Env) -> None:
    with pytest.raises(RunnerError, match=r"isn't in runner\.repos"):
        await env.ws.prepare("R1", "nope", "x", github_token=GH_TOKEN)
    with pytest.raises(RunnerError, match="bad run code"):
        await env.ws.prepare("../R1", "proj", "x", github_token=GH_TOKEN)


async def test_prepare_twice_refuses_and_failure_cleans_up(env: Env) -> None:
    await env.prepare("R1")
    with pytest.raises(RunnerError, match="already exists"):
        await env.prepare("R1")
    env.cfg.repos["proj"] = RunnerRepo(remote="github.com/own/proj", base="nope")
    with pytest.raises(RunnerError, match="isn't a branch"):
        await env.prepare("R2")
    assert not (env.cfg.work_dir / "R2").exists()


async def test_mirror_picks_up_new_remote_commits(env: Env) -> None:
    await env.prepare("R1")
    seed = env.root / "seed"
    (seed / "new.txt").write_text("n\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "second")
    git(seed, "push", "-q", str(env.remote), "main")
    p = await env.prepare("R2")
    assert (p.repo_dir / "new.txt").exists()


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


# --- harvest and bundle ----------------------------------------------------------------------


async def test_full_flow_to_a_pr(env: Env) -> None:
    p = await env.prepare("R1", "Fix the flaky test")
    head = await env.deliver(p, {"src/fix.py": "x = 1\n"}, "Fix the flaky test")
    assert git(env.ws.mirror_path("proj"), "rev-parse", p.run_ref) == head

    result = await env.ws.check(p, head, SECRETS)
    assert result.files == 1 and result.title == "Fix the flaky test"

    pr = await env.ws.publish(
        p, result, SECRETS, ask="Fix the flaky test", summary="Done.", still_ok=always
    )
    assert (pr.number, pr.url) == (41, "https://github.com/own/proj/pull/41")
    assert p.branch in env.remote_branches()
    assert git(env.remote, "rev-parse", p.branch) == head

    create = env.gh_calls("create")[0]
    argv: list[str] = create["argv"]  # type: ignore[assignment]
    assert argv[argv.index("--head") + 1] == p.branch
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--repo") + 1] == "own/proj"
    assert argv[argv.index("--title") + 1] == "Fix the flaky test"
    assert "Fix the flaky test" in str(create["stdin"]) and "Done." in str(create["stdin"])
    assert "R1" in str(create["stdin"])


async def test_token_never_on_argv_or_in_urls(env: Env) -> None:
    p = await env.prepare()
    head = await env.deliver(p, {"a.txt": "a\n"})
    result = await env.ws.check(p, head, SECRETS)
    await env.ws.publish(p, result, SECRETS, ask="x", summary="s", still_ok=always)
    assert GH_TOKEN not in env.shim_log.read_text()
    assert CLAUDE_TOKEN not in env.shim_log.read_text()
    for call in env.gh_calls():
        assert GH_TOKEN not in json.dumps(call["argv"])
        assert call["token"] == GH_TOKEN  # by env only
    for config in [env.ws.mirror_path("proj") / "config", p.repo_dir / ".git" / "config"]:
        assert GH_TOKEN not in config.read_text()
    assert GH_TOKEN not in await asyncio.to_thread(_ps)


async def test_uncommitted_work_is_committed_by_harvest(env: Env) -> None:
    p = await env.prepare()
    (p.repo_dir / "wip.txt").write_text("wip\n")
    await env.bundle(p)
    head = await env.ws.import_bundle(p)
    assert (
        git(env.ws.mirror_path("proj"), "log", "-1", "--format=%s", head) == "R1: uncommitted work"
    )
    assert git(env.ws.mirror_path("proj"), "log", "-1", "--format=%an <%ae>", head) == (
        "Resonant runner <resonant-runner@users.noreply.github.com>"
    )


async def test_no_commits_means_no_bundle(env: Env) -> None:
    p = await env.prepare()
    await env.bundle(p)
    assert not p.bundle_path.exists()
    with pytest.raises(NoChanges):
        await env.ws.import_bundle(p)


async def test_bundle_symlink_refused(env: Env) -> None:
    p = await env.prepare()
    secret = env.root / "resonant.db"
    secret.write_bytes(b"SQLite format 3\0" + b"x" * 100)
    p.bundle_path.symlink_to(secret)
    with pytest.raises(BundleRefused, match="regular file"):
        await env.ws.import_bundle(p)
    assert not any(env.ws.cfg.mirror_dir.joinpath(".incoming").glob("*"))


async def test_bundle_fifo_refused_without_hanging(env: Env) -> None:
    p = await env.prepare()
    os.mkfifo(p.bundle_path)
    with pytest.raises(BundleRefused, match="regular file"):
        await asyncio.wait_for(env.ws.import_bundle(p), 10)


async def test_run_dir_symlink_refused(env: Env) -> None:
    p = await env.prepare()
    real = env.root / "elsewhere"
    real.mkdir()
    (real / "out.bundle").write_bytes(b"junk")
    moved = p.run_dir.with_name("moved")
    p.run_dir.rename(moved)
    p.run_dir.symlink_to(real)
    with pytest.raises(BundleRefused, match="plain directory"):
        await env.ws.import_bundle(p)


async def test_oversize_bundle_refused(env: Env) -> None:
    env.cfg.max_bundle_mb = 1
    p = await env.prepare()
    p.bundle_path.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(BundleRefused, match="over 1 MB"):
        await env.ws.import_bundle(p)
    assert not list((env.cfg.mirror_dir / ".incoming").glob("*"))
    # exactly at the cap is read through (and then rejected as not a bundle)
    p.bundle_path.write_bytes(b"x" * (1024 * 1024))
    with pytest.raises(BundleRefused, match="valid git bundle"):
        await env.ws.import_bundle(p)


async def test_junk_bundle_refused(env: Env) -> None:
    p = await env.prepare()
    p.bundle_path.write_bytes(b"not a bundle")
    with pytest.raises(BundleRefused, match="valid git bundle"):
        await env.ws.import_bundle(p)


async def test_bundle_with_extra_ref_refused(env: Env) -> None:
    p = await env.prepare()
    env.session(p, {"a.txt": "a\n"})
    git(p.repo_dir, "branch", "extra")
    git(
        p.repo_dir,
        "bundle",
        "create",
        "-q",
        str(p.bundle_path),
        f"{p.base_sha}..{p.branch}",
        f"{p.base_sha}..extra",
    )
    with pytest.raises(BundleRefused, match="other than the run's branch"):
        await env.ws.import_bundle(p)
    assert git(env.ws.mirror_path("proj"), "for-each-ref", "refs/runs") == ""


async def test_bundle_with_wrong_ref_refused(env: Env) -> None:
    p = await env.prepare()
    env.session(p, {"a.txt": "a\n"})
    git(p.repo_dir, "branch", "-m", "evil")
    git(p.repo_dir, "bundle", "create", "-q", str(p.bundle_path), f"{p.base_sha}..evil")
    with pytest.raises(BundleRefused, match="other than the run's branch"):
        await env.ws.import_bundle(p)


async def test_bundle_failing_fsck_refused(env: Env) -> None:
    p = await env.prepare()

    def plumbing(*args: str, stdin: str) -> str:
        res = subprocess.run(
            [GIT, "-C", str(p.repo_dir), *args],
            env=ENV,
            input=stdin,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        return res.stdout.strip()

    # A tree entry named ".git" packs fine but fails fsckObjects (hasDotgit) on fetch.
    blob = plumbing("hash-object", "-w", "--stdin", stdin="x\n")
    tree = plumbing("mktree", stdin=f"100644 blob {blob}\t.git\n")
    commit = plumbing("commit-tree", tree, "-p", p.base_sha, "-m", "bad", stdin="")
    git(p.repo_dir, "update-ref", f"refs/heads/{p.branch}", commit)
    git(p.repo_dir, "bundle", "create", "-q", str(p.bundle_path), f"{p.base_sha}..{p.branch}")
    with pytest.raises(BundleRefused, match="git's checks"):
        await env.ws.import_bundle(p)
    assert git(env.ws.mirror_path("proj"), "for-each-ref", "refs/runs") == ""


async def test_planted_clone_config_and_hooks_never_run(env: Env) -> None:
    p = await env.prepare()
    env.session(p, {"a.txt": "a\n"})
    git(p.repo_dir, "bundle", "create", "-q", str(p.bundle_path), f"{p.base_sha}..{p.branch}")
    marker = env.root / "pwned"
    git(p.repo_dir, "config", "core.fsmonitor", f"touch {marker}; echo")
    git(p.repo_dir, "config", "credential.helper", f"!touch {marker}")
    git(p.repo_dir, "config", "core.sshCommand", f"touch {marker}")
    hook = p.repo_dir / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    head = await env.ws.import_bundle(p)
    result = await env.ws.check(p, head, SECRETS)
    await env.ws.publish(p, result, SECRETS, ask="x", summary="", still_ok=always)
    assert not marker.exists()
    # the only git run against the clone was in prepare
    for line in env.shim_log.read_text().splitlines():
        if str(p.repo_dir) in line:
            assert "remote set-url" in line or "checkout" in line or "clone" in line, line


# --- checks wired through the workspace ------------------------------------------------------


async def test_token_in_summary_refused_before_push(env: Env) -> None:
    p = await env.prepare()
    head = await env.deliver(p, {"a.txt": "a\n"})
    result = await env.ws.check(p, head, SECRETS)
    with pytest.raises(CheckFailed, match="pr body"):
        await env.ws.publish(
            p, result, SECRETS, ask="x", summary=f"key {CLAUDE_TOKEN}", still_ok=always
        )
    assert p.branch not in env.remote_branches()
    assert env.gh_calls() == []


async def test_github_change_refused_via_workspace(env: Env) -> None:
    p = await env.prepare()
    head = await env.deliver(p, {".github/workflows/x.yml": "on: push\n"})
    with pytest.raises(CheckFailed, match=r"\.github/"):
        await env.ws.check(p, head, SECRETS)
    assert p.branch not in env.remote_branches()


async def test_flagged_paths_reach_the_pr_body(env: Env) -> None:
    p = await env.prepare()
    head = await env.deliver(p, {"Makefile": "all:\n", ".claude/settings.json": "{}\n"})
    result = await env.ws.check(p, head, SECRETS)
    assert result.flag_line == "⚠ changes .claude/settings.json, Makefile"
    await env.ws.publish(p, result, SECRETS, ask="x", summary="", still_ok=always)
    create = env.gh_calls("create")[0]
    assert "⚠ changes .claude/settings.json, Makefile" in str(create["stdin"])


async def test_quote_ask_false_leaves_the_ask_out(env: Env) -> None:
    env.cfg.repos["proj"] = RunnerRepo(remote="github.com/own/proj", quote_ask=False)
    p = await env.prepare()
    head = await env.deliver(p, {"a.txt": "a\n"})
    result = await env.ws.check(p, head, SECRETS)
    await env.ws.publish(p, result, SECRETS, ask="private ask text", summary="", still_ok=always)
    create = env.gh_calls("create")[0]
    assert "private ask text" not in str(create["stdin"])


# --- publish: cancel and idempotence ---------------------------------------------------------


async def _published(env: Env) -> tuple[Prepared, object]:
    p = await env.prepare()
    head = await env.deliver(p, {"a.txt": "a\n"})
    return p, await env.ws.check(p, head, SECRETS)


async def test_cancelled_before_push(env: Env) -> None:
    p, result = await _published(env)

    async def no() -> bool:
        return False

    with pytest.raises(PublishAborted):
        await env.ws.publish(p, result, SECRETS, ask="x", summary="", still_ok=no)  # type: ignore[arg-type]
    assert p.branch not in env.remote_branches()
    assert env.gh_calls() == []


@pytest.mark.parametrize(("flips_at", "pushed", "gh_calls"), [(2, True, 0), (3, True, 1)])
async def test_cancelled_between_push_and_pr(
    env: Env, flips_at: int, pushed: bool, gh_calls: int
) -> None:
    p, result = await _published(env)
    calls = 0

    async def flip() -> bool:
        nonlocal calls
        calls += 1
        return calls < flips_at

    with pytest.raises(PublishAborted):
        await env.ws.publish(p, result, SECRETS, ask="x", summary="", still_ok=flip)  # type: ignore[arg-type]
    assert (p.branch in env.remote_branches()) is pushed
    created = env.gh_calls("create")
    assert created == []
    assert len(env.gh_calls()) == gh_calls  # the list lookup happens before the third check


async def test_publish_is_idempotent(env: Env) -> None:
    p, result = await _published(env)
    first = await env.ws.publish(p, result, SECRETS, ask="x", summary="", still_ok=always)  # type: ignore[arg-type]
    second = await env.ws.publish(p, result, SECRETS, ask="x", summary="", still_ok=always)  # type: ignore[arg-type]
    assert first == second
    creates = env.gh_calls("create")
    assert len(creates) == 1


async def test_gh_create_failure_is_a_clean_error(env: Env) -> None:
    p, result = await _published(env)
    env.gh.write_text(f"#!/bin/sh\necho 'boom {GH_TOKEN}' >&2\nexit 1\n")
    with pytest.raises(RunnerError, match="gh pr create failed") as e:
        await env.ws.publish(p, result, SECRETS, ask="x", summary="", still_ok=always)  # type: ignore[arg-type]
    assert GH_TOKEN not in str(e.value)


# --- merge -----------------------------------------------------------------------------------


async def test_merge_needs_green_ci_and_matches_head(env: Env) -> None:
    sha = "a" * 40
    env.gh_set("checks_rc", "1")
    with pytest.raises(MergeRefused, match="isn't green"):
        await env.ws.merge("proj", 41, sha, SECRETS)
    env.gh_set("checks_rc", "8")
    with pytest.raises(MergeRefused, match="still running"):
        await env.ws.merge("proj", 41, sha, SECRETS)
    assert not env.gh_calls("merge")

    env.gh_set("checks_rc", "0")
    await env.ws.merge("proj", 41, sha, SECRETS)
    merge = env.gh_calls("merge")[0]
    argv: list[str] = merge["argv"]  # type: ignore[assignment]
    assert argv[:3] == ["pr", "merge", "41"]
    assert "--squash" in argv
    assert argv[argv.index("--match-head-commit") + 1] == sha
    assert merge["token"] == GH_TOKEN


async def test_merge_failure_and_bad_input(env: Env) -> None:
    env.gh_set("merge_rc", "1")
    with pytest.raises(MergeRefused, match="gh pr merge failed"):
        await env.ws.merge("proj", 41, "b" * 40, SECRETS)
    with pytest.raises(MergeRefused, match="bad PR"):
        await env.ws.merge("proj", 41, "--admin", SECRETS)
    with pytest.raises(MergeRefused, match="bad PR"):
        await env.ws.merge("proj", 0, "b" * 40, SECRETS)


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

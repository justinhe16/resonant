"""Pre-push checks on a run's commits (resonant.runner.checks), on real local repos."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from resonant.config import RunnerRepo
from resonant.runner import checks
from resonant.runner.checks import (
    CheckFailed,
    GitError,
    check_texts,
    classify_path,
    has_secret,
    run_checks,
)

GIT = "/usr/bin/git"
GH_TOKEN = "github_pat_" + "A1b2C3d4E5" * 6
CLAUDE_TOKEN = "sk-ant-oat01-" + "Zy9Xw8Vu7T" * 9
ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
REPO = RunnerRepo(remote="github.com/own/proj")


def git(cwd: Path, *args: str, stdin: str | None = None, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        [GIT, "-C", str(cwd), *args],
        env={**ENV, **(env or {})},
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("hello\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


def runner_for(path: Path) -> checks.GitRunner:
    async def run(*args: str, stdin: bytes | None = None, max_out: int | None = None) -> bytes:
        res = await asyncio.to_thread(
            subprocess.run,
            [GIT, "-C", str(path), *args],
            env=ENV,
            input=stdin,
            capture_output=True,
        )
        if res.returncode != 0:
            raise GitError(args, res.returncode, res.stderr.decode())
        if max_out is not None and len(res.stdout) > max_out:
            raise checks.OutputTooLarge
        return res.stdout

    return run


def commit(path: Path, files: dict[str, str], message: str = "change") -> str:
    for name, text in files.items():
        f = path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD")


async def check(
    path: Path, base: str, head: str, *, repo: RunnerRepo = REPO, branch: str = "resonant/r1-x"
) -> checks.CheckResult:
    return await run_checks(
        runner_for(path),
        base_sha=base,
        head_sha=head,
        branch=branch,
        repo=repo,
        secrets=[GH_TOKEN, CLAUDE_TOKEN],
    )


@pytest.mark.parametrize(
    ("path", "refused", "flagged"),
    [
        ("src/app.py", False, False),
        (".github/workflows/ci.yml", True, False),
        (".GitHub/actions/x/action.yml", True, False),
        ("sub/.github/x.yml", False, False),
        (".gitmodules", True, False),
        ("vendor/.gitmodules", True, False),
        (".claude/settings.json", False, True),
        ("pkg/.claude/hooks/x.sh", False, True),
        (".mcp.json", False, True),
        (".devcontainer/devcontainer.json", False, True),
        (".devcontainer.json", False, True),
        (".vscode/tasks.json", False, True),
        (".vscode/settings.json", False, True),
        (".vscode/launch.json", False, False),
        ("Makefile", False, True),
        ("services/api/package.json", False, True),
        ("pyproject.toml", False, True),
        ("Dockerfile.dev", False, True),
        ("docker/dockerfile", False, True),
        ("tests/conftest.py", False, True),
        (".pre-commit-config.yaml", False, True),
        ("docs/makefile.md", False, False),
    ],
)
def test_classify_path(path: str, refused: bool, flagged: bool) -> None:
    refusal, is_flagged = classify_path(path)
    assert (refusal is not None) == refused
    assert is_flagged == flagged


def test_github_allowed_when_the_repo_says_so() -> None:
    assert classify_path(".github/workflows/ci.yml", allow_github=True) == (None, True)
    # .gitmodules is refused regardless
    assert classify_path(".gitmodules", allow_github=True)[0] is not None


def test_has_secret_sees_encodings_and_shapes() -> None:
    assert has_secret([GH_TOKEN], f"x {GH_TOKEN} y")
    assert has_secret([GH_TOKEN], GH_TOKEN.replace("_", "%5F"))
    assert has_secret([], "ghp_" + "a" * 36)
    assert not has_secret([GH_TOKEN], "nothing to see here")
    assert not has_secret([], "nothing to see here")


def test_check_texts_names_the_place_not_the_text() -> None:
    with pytest.raises(CheckFailed) as e:
        check_texts([GH_TOKEN], pr_body=f"hi {GH_TOKEN}")
    assert str(e.value) == "a token appears in the pr body"
    assert GH_TOKEN not in str(e.value)


async def test_clean_change(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(
        repo, {"src/a.py": "x = 1\n", "Makefile": "all:\n", "package.json": "{}\n"}, "Add a"
    )
    r = await check(repo, base, head)
    assert r.files == 3
    assert r.title == "Add a"
    assert r.flagged == ("Makefile", "package.json")
    assert r.flag_line == "⚠ changes Makefile, package.json"
    assert r.head_sha == head
    assert r.changed_bytes > 0


async def test_no_flags_means_empty_flag_line(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    r = await check(repo, base, commit(repo, {"a.txt": "a\n"}))
    assert r.flagged == ()
    assert r.flag_line == ""


async def test_first_commit_subject_is_the_title(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {"a": "1"}, "First subject\n\nbody text")
    head = commit(repo, {"b": "2"}, "Second subject")
    assert (await check(repo, base, head)).title == "First subject"


async def test_github_change_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {".github/workflows/ci.yml": "on: push\n"})
    with pytest.raises(CheckFailed, match=r"\.github/"):
        await check(repo, base, head)
    allowed = RunnerRepo(remote="github.com/own/proj", allow_github_changes=True)
    res = await check(repo, base, head, repo=allowed)
    assert res.files == 1 and res.flagged == (".github/workflows/ci.yml",)  # allowed, listed


async def test_github_rename_out_of_dir_still_seen(repo: Path) -> None:
    commit(repo, {".github/workflows/ci.yml": "on: push\n"})
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "mv", ".github/workflows/ci.yml", "ci.yml")
    git(repo, "commit", "-q", "-m", "mv")
    with pytest.raises(CheckFailed, match=r"\.github/"):
        await check(repo, base, git(repo, "rev-parse", "HEAD"))


async def test_gitmodules_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {".gitmodules": '[submodule "x"]\n\tpath = x\n\turl = https://e.com/x\n'})
    with pytest.raises(CheckFailed, match=r"\.gitmodules"):
        await check(repo, base, head)


async def test_submodule_entry_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},sub")
    git(repo, "commit", "-q", "-m", "sub")
    with pytest.raises(CheckFailed, match="submodule"):
        await check(repo, base, git(repo, "rev-parse", "HEAD"))


async def test_token_in_diff_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"notes.txt": f"key={GH_TOKEN}\n"})
    with pytest.raises(CheckFailed, match="diff") as e:
        await check(repo, base, head)
    assert GH_TOKEN not in str(e.value)


async def test_claude_token_in_diff_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"notes.txt": f"{CLAUDE_TOKEN}\n"})
    with pytest.raises(CheckFailed, match="diff"):
        await check(repo, base, head)


async def test_token_in_commit_message_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"a": "1"}, f"oops {CLAUDE_TOKEN}")
    with pytest.raises(CheckFailed, match="commit object"):
        await check(repo, base, head)


async def test_token_in_branch_name_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"a": "1"})
    with pytest.raises(CheckFailed, match="branch name"):
        await check(repo, base, head, branch=f"resonant/r1-{GH_TOKEN}")


async def test_token_in_binary_file_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "blob.bin").write_bytes(b"\0\0" + GH_TOKEN.encode() + b"\0")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "bin")
    with pytest.raises(CheckFailed, match="diff"):
        await check(repo, base, git(repo, "rev-parse", "HEAD"))


async def test_removing_a_line_is_not_a_leak(repo: Path) -> None:
    commit(repo, {"old.txt": f"{GH_TOKEN}\n"})
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"old.txt": "clean\n"})
    assert (await check(repo, base, head)).files == 1


async def test_too_many_files_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {f"f/{i}.txt": str(i) for i in range(checks.MAX_FILES + 1)})
    with pytest.raises(CheckFailed, match="201 files"):
        await check(repo, base, head)


async def test_too_many_bytes_refused(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "MAX_CHANGED_BYTES", 1000)
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"big.txt": "x" * 2000})
    with pytest.raises(CheckFailed, match="limit is 20 MB"):
        await check(repo, base, head)


async def test_head_must_descend_from_base(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--orphan", "other")
    git(repo, "rm", "-rfq", ".")
    head = commit(repo, {"x": "1"}, "unrelated")
    with pytest.raises(CheckFailed, match="descend"):
        await check(repo, base, head)


async def test_other_git_failures_propagate(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    with pytest.raises(GitError):
        await check(repo, base, "f" * 40)


# --- history, not just the net diff ------------------------------------------------------------


async def test_plus_plus_lines_are_scanned(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"notes.txt": f"+{GH_TOKEN}\n"})  # the patch line starts with "++"
    with pytest.raises(CheckFailed, match="diff"):
        await check(repo, base, head)
    patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+++b\n"
    assert checks.added_lines(patch) == "++b"


async def test_token_added_then_removed_is_still_caught(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {"a.txt": f"{GH_TOKEN}\n"}, "add")
    head = commit(repo, {"a.txt": "clean\n"}, "remove")
    with pytest.raises(CheckFailed, match="diff"):
        await check(repo, base, head)


async def test_github_added_then_removed_is_still_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {".github/workflows/x.yml": "on: push\n"}, "add")
    git(repo, "rm", "-rq", ".github")
    git(repo, "commit", "-q", "-m", "remove")
    with pytest.raises(CheckFailed, match=r"\.github/"):
        await check(repo, base, git(repo, "rev-parse", "HEAD"))


async def test_merge_commit_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"s.txt": "s"}, "side")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"m.txt": "m"}, "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", "side")
    with pytest.raises(CheckFailed, match="merge commit"):
        await check(repo, base, git(repo, "rev-parse", "HEAD"))


async def test_token_in_author_line_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("a")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x", env={"GIT_AUTHOR_NAME": GH_TOKEN})
    with pytest.raises(CheckFailed, match="commit object"):
        await check(repo, base, git(repo, "rev-parse", "HEAD"))


async def test_deleted_blob_sizes_count(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commit(repo, {"big.txt": "x" * 5000})
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "-q", "big.txt")
    git(repo, "commit", "-q", "-m", "rm")
    monkeypatch.setattr(checks, "MAX_CHANGED_BYTES", 1000)
    with pytest.raises(CheckFailed, match="limit is 20 MB"):
        await check(repo, base, git(repo, "rev-parse", "HEAD"))


async def test_oversize_history_fails_closed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "MAX_LOG_BYTES", 100)
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"a.txt": "x" * 500})
    with pytest.raises(CheckFailed, match="too large"):
        await check(repo, base, head)


async def test_symlink_and_mode_changes_flagged(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "link").symlink_to("README.md")
    (repo / "run.sh").write_text("#!/bin/sh\n")
    (repo / "run.sh").chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")
    r = await check(repo, base, git(repo, "rev-parse", "HEAD"))
    assert r.flagged == ("link (symlink)", "run.sh (executable)")
    mid = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").chmod(0o755)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chmod")
    r = await check(repo, mid, git(repo, "rev-parse", "HEAD"))
    assert r.flagged == ("README.md (mode change)",)


@pytest.mark.parametrize(
    "path",
    [
        ".envrc",
        ".npmrc",
        ".yarnrc.yml",
        ".husky/pre-commit",
        ".gitattributes",
        "Jenkinsfile",
        ".gitlab-ci.yml",
        ".circleci/config.yml",
    ],
)
def test_more_flagged_paths(path: str) -> None:
    assert classify_path(path) == (None, True)


def test_parse_raw_fails_closed() -> None:
    with pytest.raises(CheckFailed):
        checks.parse_raw(b"garbage\0path\0")
    with pytest.raises(CheckFailed):
        checks.parse_raw(b":100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" M\0")  # no path
    assert checks.parse_raw(b"") == []


def test_git_error_names_the_subcommand() -> None:
    e = GitError(["-C", "/x", "-c", "a=b", "fetch", "-q"], 128, "nope")
    assert str(e) == "git fetch failed (128): nope"


# --- CI presence -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("on: pull_request\n", True),
        ("on: [push, pull_request]\n", True),
        ("on:\n  push:\n  pull_request:\n    branches: [main]\n", True),
        ("on:\n  pull_request:\n", True),
        ("on: push\n", False),
        ("on:\n  push:\n  pull_request_target:\n", False),
        ("on:\n  workflow_dispatch:\n", False),
        ("name: x\n", False),
        ("on: [unclosed\n", False),
        ("- just\n- a list\n", False),
        ("", False),
    ],
)
def test_runs_on_pull_request(text: str, expected: bool) -> None:
    assert checks.runs_on_pull_request(text) is expected


async def test_ci_present(repo: Path) -> None:
    assert await checks.ci_present(runner_for(repo), "main") is False
    commit(repo, {".github/workflows/deploy.yml": "on: push\n"})
    assert await checks.ci_present(runner_for(repo), "main") is False
    commit(repo, {".github/workflows/ci.yaml": "on:\n  pull_request:\n"})
    assert await checks.ci_present(runner_for(repo), "main") is True
    with pytest.raises(GitError):
        await checks.ci_present(runner_for(repo), "nope")


async def test_token_in_a_file_path_refused(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"ghp_" + "a" * 36 + ".txt": "x\n"})
    with pytest.raises(CheckFailed, match="file paths"):
        await check(repo, base, head)

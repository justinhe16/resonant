"""BR-9: ``recent_changes`` (``tools/changes.py``): the newest commits of a repo's local
checkout, with PR numbers, from the registry, Resonant's own checkout or a path; the read
scope; hardened git (a repo's config can't run a program); redaction; the spec flags."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from resonant.config import OpsConfig, RunnerConfig, Settings
from resonant.e2e.bench import IsolatedSettings
from resonant.executor import ToolFailed
from resonant.store import repos as repo_store
from resonant.tools import changes
from resonant.tools.changes import RECENT_CHANGES, parse_log, recent_changes
from resonant.tools.files import GRANT, ReadScope
from resonant_sdk import Effect, Level

GIT = Path("/usr/bin/git")
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

pytestmark = pytest.mark.skipif(not os.access(GIT, os.X_OK), reason="needs /usr/bin/git")


def git(cwd: Path, *args: str, date: str = "2026-10-18T12:00:00+00:00") -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Resonant",
        "GIT_AUTHOR_EMAIL": "r@example.com",
        "GIT_COMMITTER_NAME": "Resonant",
        "GIT_COMMITTER_EMAIL": "r@example.com",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }
    subprocess.run([str(GIT), *args], cwd=cwd, env=env, check=True, capture_output=True)


def make_repo(path: Path, subjects: list[str]) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for i, subject in enumerate(subjects):
        (path / "f.txt").write_text(f"{i}\n")
        git(path, "add", "f.txt")
        git(path, "commit", "-q", "-m", subject, date=f"2026-10-{10 + i:02d}T12:00:00+00:00")
    return path


@pytest.fixture
def world(tmp_path: Path) -> tuple[Settings, ReadScope, Path]:
    code = tmp_path / "code"
    own = make_repo(
        tmp_path / "resonant",
        ["BR-1: the brain loop (#100)", "BR-8: brain cut-over (#104)"],
    )
    settings = IsolatedSettings(
        home=tmp_path / "home",
        runner=RunnerConfig(code_dir=code, git_path=GIT),
        ops=OpsConfig(repo_path=own),
    )
    scope = ReadScope.build([code, own], [".env", "*.pem"])
    return settings, scope, own


def run(db: sqlite3.Connection, w: tuple[Settings, ReadScope, Path], **args: Any) -> Any:
    settings, scope, _ = w
    return asyncio.run(recent_changes(db, settings, scope, args))


def test_spec_is_an_untrusted_l0_read_behind_the_files_grant() -> None:
    assert RECENT_CHANGES.effect is Effect.READ and RECENT_CHANGES.effective_level is Level.L0
    assert RECENT_CHANGES.untrusted and RECENT_CHANGES.grant == GRANT
    assert changes.SPECS == (RECENT_CHANGES,)
    assert "required" not in RECENT_CHANGES.args_schema  # repo and limit are optional


def test_resonant_itself_by_default_newest_first_with_pr_numbers(
    db: sqlite3.Connection, world: tuple[Settings, ReadScope, Path]
) -> None:
    out = run(db, world)
    assert out["repo"] == "resonant" and out["path"] == str(world[2].resolve())
    assert [c["subject"] for c in out["commits"]] == [
        "BR-8: brain cut-over (#104)",
        "BR-1: the brain loop (#100)",
    ]
    assert [c.get("pr") for c in out["commits"]] == [104, 100]
    assert out["commits"][0]["date"].startswith("2026-10-11")
    assert out["branch"] == "main" and out["untrusted"] is True
    assert "not fetched" in out["note"] and "fetched_at" not in out  # never fetched
    assert run(db, world, repo="resonant")["repo"] == "resonant"
    assert len(run(db, world, limit=1)["commits"]) == 1


def test_a_registered_repo_by_name_slug_or_alias(
    db: sqlite3.Connection, world: tuple[Settings, ReadScope, Path], tmp_path: Path
) -> None:
    make_repo(tmp_path / "code" / "dori", ["Merge pull request #17 from x/y", "fix login"])
    repo_store.add(db, "justinhe16/dori", base="main", source="test", name="dori")
    for key in ("dori", "justinhe16/dori", "DORI"):
        out = run(db, world, repo=key)
        assert out["repo"] == "justinhe16/dori"
        assert [c.get("pr") for c in out["commits"]] == [None, 17]
    by_path = run(db, world, repo=str(tmp_path / "code" / "dori"))
    assert len(by_path["commits"]) == 2


def test_upstream_ahead_behind_and_last_fetch(
    db: sqlite3.Connection, world: tuple[Settings, ReadScope, Path], tmp_path: Path
) -> None:
    origin = world[2]
    clone = tmp_path / "code" / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    (origin / "f.txt").write_text("new\n")
    git(origin, "commit", "-qam", "BR-9: recent changes (#105)")
    git(clone, "fetch", "-q")
    out = run(db, world, repo=str(clone))
    assert (out["ahead"], out["behind"]) == (0, 1)  # GitHub has one more than the checkout
    assert out["commits"][0]["pr"] == 104 and "fetched_at" in out


def test_errors_say_why(
    db: sqlite3.Connection, world: tuple[Settings, ReadScope, Path], tmp_path: Path
) -> None:
    with pytest.raises(ToolFailed, match="no repo named 'nope'"):
        run(db, world, repo="nope")
    (tmp_path / "code" / "plain").mkdir(parents=True)
    with pytest.raises(ToolFailed, match="isn't a git checkout"):
        run(db, world, repo=str(tmp_path / "code" / "plain"))
    with pytest.raises(ToolFailed, match="outside the read roots"):
        run(db, world, repo=str(tmp_path / "home"))
    with pytest.raises(ToolFailed, match="limit"):
        run(db, world, limit=0)
    with pytest.raises(ToolFailed, match="unknown arguments"):
        run(db, world, number=1)
    settings, scope, _ = world
    alone = settings.model_copy(update={"ops": OpsConfig(repo_path=None)})
    with pytest.raises(ToolFailed, match="say which repo"):
        asyncio.run(recent_changes(db, alone, scope, {}))


def test_a_repos_own_config_runs_nothing_and_text_is_redacted(
    db: sqlite3.Connection, world: tuple[Settings, ReadScope, Path], tmp_path: Path
) -> None:
    """Workers are unguarded and can write a checkout's .git/config: nothing in it may run."""
    repo = make_repo(tmp_path / "code" / "evil", [f"leak {TOKEN}"])
    marker = tmp_path / "ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    for key in ("core.pager", "core.fsmonitor", "gpg.program"):
        git(repo, "config", key, str(hook))
    git(repo, "config", "log.showSignature", "true")
    out = run(db, world, repo=str(repo))
    assert not marker.exists()
    assert TOKEN not in out["commits"][0]["subject"]


def test_the_hardening_is_what_stops_the_repo_config(tmp_path: Path) -> None:
    """Control for the test above: a plain git with the repo's pager does run it."""
    repo = make_repo(tmp_path / "plain", ["x"])
    marker = tmp_path / "ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    hook.chmod(0o755)
    git(repo, "config", "core.pager", str(hook))
    script = f"cd {repo} && {GIT} log -n1"  # stdout is the terminal: git pages
    subprocess.run(["/usr/bin/script", "-q", "/dev/null", "/bin/sh", "-c", script],
                   check=False, capture_output=True, timeout=30,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(repo), "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": "/dev/null", "TERM": "dumb"})  # fmt: skip
    assert marker.exists()


@pytest.mark.parametrize(
    "how", ["gitfile", "symlink", "commondir", "alternates", "padded", "objects", "pack"]
)
def test_a_dot_git_pointing_outside_the_roots_is_refused(
    db: sqlite3.Connection, world: tuple[Settings, ReadScope, Path], tmp_path: Path, how: str
) -> None:
    """A worker-written .git can't make git read a repo the read tools can't reach (review
    of BR-9): a gitdir file, a symlinked .git, a linked worktree, alternates or objects."""
    outside = make_repo(tmp_path / "outside", ["secret subject"])
    repo = make_repo(tmp_path / "code" / "victim", ["fine"])
    dot = repo / ".git"
    if how in ("gitfile", "symlink"):
        shutil.rmtree(dot)
        if how == "gitfile":
            dot.write_text(f"gitdir: {outside / '.git'}\n")
        else:
            dot.symlink_to(outside / ".git")
    elif how == "commondir":
        (dot / "commondir").write_text(str(outside / ".git"))
    elif how in ("alternates", "padded"):
        pad = "#\n\n" * 25 if how == "padded" else ""  # git reads every line, so must we
        (dot / "objects" / "info").mkdir(exist_ok=True)
        (dot / "objects" / "info" / "alternates").write_text(
            f"{pad}{outside / '.git' / 'objects'}\n"
        )
    elif how == "pack":
        shutil.rmtree(dot / "objects" / "pack")
        (dot / "objects" / "pack").symlink_to(outside / ".git" / "objects" / "pack")
    else:
        shutil.rmtree(dot / "objects")
        (dot / "objects").symlink_to(outside / ".git" / "objects")
    with pytest.raises(ToolFailed) as e:
        run(db, world, repo=str(repo))
    assert "secret" not in str(e.value)


def test_parse_log_skips_malformed_lines() -> None:
    line = "\x1f".join(("abc1234", "2026-10-18T12:00:00+00:00", "sam", "Fix (#7)"))
    assert parse_log(f"garbage\n{line}\n") == [
        {"sha": "abc1234", "date": "2026-10-18T12:00:00+00:00", "author": "sam",
         "subject": "Fix (#7)", "pr": 7}
    ]  # fmt: skip

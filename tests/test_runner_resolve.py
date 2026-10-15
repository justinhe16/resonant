"""The repo resolver (resonant.runner.resolve) with a fake GitHub."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from resonant.config import RunnerConfig
from resonant.runner.resolve import (
    Ambiguous,
    FakeGitHub,
    GhGitHub,
    GitHubError,
    NoAccess,
    NotFound,
    RepoInfo,
    Resolved,
    Resolver,
    parse_slug,
)
from resonant.runner.workspace import Workspace
from resonant.secrets import RunnerSecrets
from resonant.store import repos


def info(slug: str, *, push: bool = True, private: bool = False, ci: bool = True) -> RepoInfo:
    return RepoInfo(slug, "main", private, push, ci)


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("me/proj", "me/proj"),
        ("https://github.com/me/proj", "me/proj"),
        ("http://www.github.com/me/proj.git", "me/proj"),
        ("github.com/me/proj/tree/main/src", "me/proj"),
        ("github.com/me/proj?tab=readme", "me/proj"),
        ("proj", None),
        ("a/b/c", None),
        ("../x", None),
        ("trading bot", None),
    ],
)
def test_parse_slug(text: str, slug: str | None) -> None:
    assert parse_slug(text) == slug


async def test_slug_registers_a_pushable_repo_once(db: sqlite3.Connection) -> None:
    gh = FakeGitHub([info("Me/Proj", ci=False)])
    resolver = Resolver(db)
    got = await resolver.resolve("me/proj", gh)
    assert isinstance(got, Resolved) and got.new
    r = got.repo
    assert (r.slug, r.name, r.source, r.mode, r.ci) == ("Me/Proj", "proj", "auto", "push", False)
    again = await resolver.resolve("https://github.com/ME/proj", gh)
    assert again == Resolved(r, new=False)
    assert gh.calls == ["repo_info me/proj"]  # the second time needs no GitHub


async def test_public_repo_without_push_is_fork_mode(db: sqlite3.Connection) -> None:
    gh = FakeGitHub([info("up/stream", push=False)])
    got = await Resolver(db).resolve("up/stream", gh)
    assert isinstance(got, Resolved) and got.repo.mode == "fork" and got.new


async def test_private_or_missing_repo_is_no_access(db: sqlite3.Connection) -> None:
    gh = FakeGitHub([info("them/secret", push=False, private=True)])
    resolver = Resolver(db)
    assert await resolver.resolve("them/secret", gh) == NoAccess("them/secret")
    assert await resolver.resolve("them/gone", gh) == NoAccess("them/gone")
    assert NoAccess("a/b").text == "No access to a/b."
    assert repos.list_all(db) == []


async def test_registry_name_and_alias_win_before_github(db: sqlite3.Connection) -> None:
    r = repos.add(db, "me/dori-oncall", base="main", source="owner", aliases=["pager"])
    gh = FakeGitHub()
    resolver = Resolver(db)
    assert await resolver.resolve("dori-oncall", gh) == Resolved(r, new=False)
    assert await resolver.resolve("Pager", gh) == Resolved(r, new=False)
    assert gh.calls == []


async def test_extension_link_by_extension_name(db: sqlite3.Connection) -> None:
    r = repos.add(db, "me/bot", base="main", source="extension:trading")
    gh = FakeGitHub()
    resolver = Resolver(db)
    assert await resolver.resolve("trading", gh) == Resolved(r, new=False)
    repos.add(db, "me/bot2", base="main", source="extension:trading")
    assert await resolver.resolve("trading", gh) == Ambiguous(("bot", "bot2"))
    assert gh.calls == []


async def test_fuzzy_match_on_the_owners_repo_list(db: sqlite3.Connection) -> None:
    gh = FakeGitHub([info("me/dori-oncall"), info("me/resonant"), info("me/blog")])
    got = await Resolver(db).resolve("dori oncall", gh)
    assert isinstance(got, Resolved) and got.new and got.repo.slug == "me/dori-oncall"
    near = await Resolver(db).resolve("resonat", gh)
    assert isinstance(near, Resolved) and near.repo.slug == "me/resonant"


async def test_ambiguous_is_capped_at_three_and_exact_wins(db: sqlite3.Connection) -> None:
    names = ["me/trading-bot", "me/trading-notes", "me/trading-ui", "me/trading-api"]
    got = await Resolver(db).resolve("trading", FakeGitHub([info(n) for n in names]))
    assert isinstance(got, Ambiguous) and len(got.candidates) == 3
    assert got.text.startswith("Which repo? ")
    exact = FakeGitHub([info("me/trading"), info("me/trading-bot")])
    won = await Resolver(db).resolve("trading", exact)
    assert isinstance(won, Resolved) and won.repo.slug == "me/trading"


async def test_not_found(db: sqlite3.Connection) -> None:
    gh = FakeGitHub([info("me/blog")])
    assert await Resolver(db).resolve("quantum", gh) == NotFound("quantum")
    assert await Resolver(db).resolve("  ", gh) == NotFound("")


async def test_owner_repo_list_is_cached_for_an_hour(db: sqlite3.Connection) -> None:
    now = [0.0]
    gh = FakeGitHub([info("me/blog")])
    resolver = Resolver(db, clock=lambda: now[0])
    await resolver.resolve("zzz", gh)
    now[0] = 3599
    await resolver.resolve("yyy", gh)
    assert gh.calls.count("my_repos") == 1
    now[0] = 3601
    await resolver.resolve("xxx", gh)
    assert gh.calls.count("my_repos") == 2


async def test_github_errors_propagate(db: sqlite3.Connection) -> None:
    with pytest.raises(GitHubError, match="down"):
        await Resolver(db).resolve("me/x", FakeGitHub(error="down"))


async def test_name_collision_gets_a_distinct_handle(db: sqlite3.Connection) -> None:
    repos.add(db, "a/proj", base="main", source="owner")
    got = await Resolver(db).resolve("b/proj", FakeGitHub([info("b/proj")]))
    assert isinstance(got, Resolved) and got.repo.name == "b-proj"


# --- the real client, against a fake gh script ------------------------------------------------

FAKE_GH = """#!@PY@
import json, os, sys
argv = sys.argv[1:]
open("@LOG@", "a").write(json.dumps({"argv": argv, "env": sorted(os.environ)}) + "\\n")
path = argv[1].lower()
hosts = open(os.path.join(os.environ["GH_CONFIG_DIR"], "hosts.yml")).read()
assert "ghp_secret" in hosts
if path == "repos/me/proj":
    print(json.dumps({"full_name": "Me/Proj", "default_branch": "dev", "private": True,
                      "permissions": {"push": True}}))
elif path == "repos/me/proj/contents/.github/workflows":
    print(json.dumps([{"name": "a.yml", "type": "file"}, {"name": "notes.md", "type": "file"}]))
elif path == "repos/me/proj/contents/.github/workflows/a.yml":
    print("on:\\n  pull_request:\\n")
elif path == "user/repos":
    print("me/proj\\nme/other")
else:
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(1)
"""


async def test_gh_client_reads_access_and_ci_without_the_token_in_argv(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH.replace("@PY@", sys.executable).replace("@LOG@", str(log)))
    gh.chmod(0o755)
    cfg = RunnerConfig(
        work_dir=tmp_path / "runs",
        mirror_dir=tmp_path / "state" / "mirrors",
        cache_dir=tmp_path / "runs" / ".cache",
        secrets_dir=tmp_path / "state" / "secrets",
        gh_path=gh,
    )
    secrets = RunnerSecrets(github_token="ghp_secret", claude_token="c", anthropic_key=None)
    client = GhGitHub(Workspace(cfg, sqlite3.connect(":memory:")), secrets)
    assert await client.repo_info("me/proj") == RepoInfo("Me/Proj", "dev", True, True, True)
    assert await client.repo_info("me/missing") is None
    assert await client.my_repos() == ["me/proj", "me/other"]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert "ghp_secret" not in json.dumps(calls)
    assert all("GH_CONFIG_DIR" in c["env"] and "GH_TOKEN" not in c["env"] for c in calls)
    assert list((tmp_path / "state" / "secrets").iterdir()) == []  # the throwaway dir is gone

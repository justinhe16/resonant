"""The repo resolver (resonant.runner.resolve) with a fake GitHub."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from github_public_fakes import FakePublicApi, public

from resonant.config import RunnerConfig
from resonant.runner.resolve import (
    Ambiguous,
    FakeGitHub,
    GhGitHub,
    GitHubError,
    NeedsToken,
    NoAccess,
    NotFound,
    NotPushable,
    OfflineGitHub,
    RepoInfo,
    Resolved,
    Resolver,
    parse_slug,
)
from resonant.runner.workspace import Workspace
from resonant.secrets import RunnerSecrets
from resonant.store import extensions as ext_store
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
    assert gh.calls == ["repo_info me/proj", "is_protected Me/Proj main"]  # then no GitHub


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


def _install(db: sqlite3.Connection, name: str, status: str = "enabled") -> None:
    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name=name,
            source=f"repo:{name}",
            commit=None,
            tree_digest="d",
            manifest_sha="m",
            manifest={},
            installed_at=datetime(2026, 1, 1, tzinfo=UTC),
            installed_by="owner",
            status=status,  # type: ignore[arg-type]
        ),
    )


async def test_extension_name_resolves_its_single_link(db: sqlite3.Connection) -> None:
    _install(db, "trading")
    repos.set_links(db, "trading", ["me/bot"])
    gh = FakeGitHub([info("me/bot")])
    got = await Resolver(db).resolve("Trading", gh)
    assert isinstance(got, Resolved) and got.new and got.repo.slug == "me/bot"
    assert got.repo.source == "auto" and got.note == "(new repo: me/bot via trading)"
    again = await Resolver(db).resolve("trading", gh)  # registered now: no GitHub call
    assert again == Resolved(got.repo, new=False, via="trading")
    assert isinstance(again, Resolved) and again.note == ""
    assert gh.calls == ["repo_info me/bot", "is_protected me/bot main"]


async def test_extension_name_with_several_links_is_ambiguous(db: sqlite3.Connection) -> None:
    _install(db, "trading")
    repos.set_links(db, "trading", ["me/bot2", "me/bot"])
    gh = FakeGitHub()
    assert await Resolver(db).resolve("trading", gh) == Ambiguous(("me/bot", "me/bot2"))
    assert gh.calls == []


async def test_extension_link_without_access_and_registry_first(db: sqlite3.Connection) -> None:
    _install(db, "trading")
    repos.set_links(db, "trading", ["them/gone"])
    assert await Resolver(db).resolve("trading", FakeGitHub()) == NoAccess("them/gone")
    r = repos.add(db, "me/other", base="main", source="owner", aliases=["trading"])
    assert await Resolver(db).resolve("trading", FakeGitHub()) == Resolved(r, new=False)


async def test_disabled_or_missing_extension_links_are_ignored(db: sqlite3.Connection) -> None:
    repos.set_links(db, "ghost", ["me/bot"])  # no extension row at all
    _install(db, "trading", status="disabled")
    repos.set_links(db, "trading", ["me/bot"])
    gh = FakeGitHub([info("me/bot")], mine=[])
    for name in ("ghost", "trading"):
        assert await Resolver(db).resolve(name, gh) == NotFound(name)
    assert gh.calls == ["my_repos"] * 2 or gh.calls == ["my_repos"]
    assert repos.list_all(db) == []


async def test_link_to_a_repo_you_cant_push_to_is_not_registered(db: sqlite3.Connection) -> None:
    _install(db, "trading")
    repos.set_links(db, "trading", ["them/pub"])
    gh = FakeGitHub([info("them/pub", push=False)])
    got = await Resolver(db).resolve("trading", gh)
    assert got == NotPushable("them/pub", "trading")
    assert isinstance(got, NotPushable) and "them/pub" in got.text
    assert repos.list_all(db) == []


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
if path == "repos/me/proj" and argv[2:] == ["--jq", ".size"]:
    print("2048")
elif path == "repos/me/proj":
    print(json.dumps({"full_name": "Me/Proj", "default_branch": "dev", "private": True,
                      "permissions": {"push": True}}))
elif path == "repos/me/proj/contents/.github/workflows":
    print(json.dumps([{"name": "a.yml", "type": "file"}, {"name": "notes.md", "type": "file"}]))
elif path == "repos/me/proj/contents/.github/workflows/a.yml":
    print("on:\\n  pull_request:\\n")
elif path == "repos/me/weird":
    print(json.dumps({"full_name": "me/weird", "default_branch": "--upload-pack=x/..",
                      "private": False, "permissions": {"push": True}}))
elif path == "repos/me/boom":
    print("HTTP 403: bad credentials ghp_secret", file=sys.stderr)
    sys.exit(1)
elif path == "user/repos":
    print("me/proj\\nme/other")
else:
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(1)
"""


def make_client(tmp_path: Path, _case: str = "") -> GhGitHub:
    gh = tmp_path / "gh"
    gh.write_text(
        FAKE_GH.replace("@PY@", sys.executable).replace("@LOG@", str(tmp_path / "calls.jsonl"))
    )
    gh.chmod(0o755)
    cfg = RunnerConfig(
        work_dir=tmp_path / "runs",
        mirror_dir=tmp_path / "state" / "mirrors",
        cache_dir=tmp_path / "runs" / ".cache",
        secrets_dir=tmp_path / "state" / "secrets",
        gh_path=gh,
    )
    secrets = RunnerSecrets(github_token="ghp_secret", claude_token="c", anthropic_key=None)
    return GhGitHub(Workspace(cfg, sqlite3.connect(":memory:")), secrets)


async def test_gh_client_reads_access_and_ci_without_the_token_in_argv(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    client = make_client(tmp_path)
    assert await client.repo_info("me/proj") == RepoInfo("Me/Proj", "dev", True, True, True)
    assert await client.repo_info("me/missing") is None
    assert await client.my_repos() == ["me/proj", "me/other"]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert "ghp_secret" not in json.dumps(calls)
    assert all("GH_CONFIG_DIR" in c["env"] and "GH_TOKEN" not in c["env"] for c in calls)
    assert list((tmp_path / "state" / "secrets").iterdir()) == []  # the throwaway dir is gone


async def test_size_kb_from_each_client_never_raises(tmp_path: Path) -> None:
    """RP-1: how big a repo is, so an add can leave a huge clone to the first run."""
    client = make_client(tmp_path)
    assert await client.size_kb("me/proj") == 2048
    assert await client.size_kb("me/missing") is None  # a gh failure is unknown
    assert await client.size_kb("me/boom") is None
    api = FakePublicApi({"octo/lib": {**public("octo/lib"), "size": 77}})
    assert await api.github().size_kb("octo/lib") == 77
    assert await api.github().size_kb("octo/none") is None
    api.repos["octo/odd"] = {**public("octo/odd"), "size": "big"}
    assert await api.github().size_kb("octo/odd") is None
    api.status = 403
    assert await api.github().size_kb("octo/lib") is None
    assert await OfflineGitHub().size_kb("me/proj") is None
    fake = FakeGitHub()
    fake.sizes["me/proj"] = 5
    assert await fake.size_kb("Me/Proj") == 5 and await fake.size_kb("me/x") is None


class SlowGitHub(FakeGitHub):
    async def repo_info(self, slug: str) -> RepoInfo | None:
        await asyncio.sleep(0.01)  # both requests are past the registry check
        return await super().repo_info(slug)


async def test_concurrent_resolves_of_one_new_repo(db: sqlite3.Connection) -> None:
    gh = SlowGitHub([info("me/fresh")])
    resolver = Resolver(db)
    a, b = await asyncio.gather(resolver.resolve("me/fresh", gh), resolver.resolve("ME/fresh", gh))
    assert isinstance(a, Resolved) and isinstance(b, Resolved)
    assert a.repo == b.repo and sorted([a.new, b.new]) == [False, True]
    assert len(repos.list_all(db)) == 1


async def test_a_bad_default_branch_from_github_is_refused(tmp_path: Path) -> None:
    client = make_client(tmp_path, "weird")
    with pytest.raises(GitHubError, match="wasn't readable"):
        await client.repo_info("me/weird")


async def test_a_token_in_gh_stderr_is_redacted(tmp_path: Path) -> None:
    client = make_client(tmp_path, "boom")
    with pytest.raises(GitHubError) as e:
        await client.repo_info("me/boom")
    assert "ghp_secret" not in str(e.value) and "403" in str(e.value)


# --- GT-1: an explicit public repo with no token ------------------------------------------


async def test_public_github_registers_a_public_repo_without_a_token(
    db: sqlite3.Connection,
) -> None:
    api = FakePublicApi({"me/proj": public("Me/Proj", "trunk")}, protected=False)
    got = await Resolver(db).resolve("https://github.com/me/proj", api.github())
    assert isinstance(got, Resolved) and got.new
    assert (got.repo.slug, got.repo.base, got.repo.mode, got.repo.ci) == (
        "Me/Proj",
        "trunk",
        "push",
        None,  # unknown: the first run's mirror check decides
    )
    assert got.warning == "trunk isn't protected"
    assert [r.url.path for r in api.requests] == [
        "/repos/me/proj",
        "/repos/Me/Proj/branches/trunk",
    ]
    for r in api.requests:  # anonymous: no credential of any kind
        assert "authorization" not in r.headers and "cookie" not in r.headers


async def test_public_github_sees_nothing_private_or_missing(db: sqlite3.Connection) -> None:
    api = FakePublicApi({"me/secret": public("me/secret", private=True)})
    gh = api.github()
    assert await gh.repo_info("me/secret") is None
    assert await Resolver(db).resolve("me/secret", gh) == NoAccess("me/secret")
    assert await Resolver(db).resolve("me/none", gh) == NoAccess("me/none")
    assert repos.list_all(db) == []


async def test_public_github_errors_and_names(db: sqlite3.Connection) -> None:
    api = FakePublicApi({"me/proj": public("me/proj")})
    gh = api.github()
    api.status = 403
    with pytest.raises(GitHubError, match="without a token is used up"):
        await gh.repo_info("me/proj")
    api.status = 500
    with pytest.raises(GitHubError, match="HTTP 500"):
        await gh.repo_info("me/proj")
    api.status = None
    api.repos["me/odd"] = public("me/odd", "bad branch name~")
    with pytest.raises(GitHubError, match="wasn't readable"):
        await gh.repo_info("me/odd")
    with pytest.raises(NeedsToken):
        await Resolver(db).resolve("proj", gh)  # a bare name lists your repos: needs the token

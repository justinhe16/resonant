"""The repo registry (migration 007, resonant.store.repos)."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from resonant.config import RunnerRepo
from resonant.store import repos
from resonant.store.db import connect, latest_version, migrate, open_db, schema_version
from resonant.store.repos import RepoConflict


def _through(version: int) -> list[tuple[int, str]]:
    found = [
        (int(f.name[:3]), f.read_text())
        for f in resources.files("resonant.store.migrations").iterdir()
        if f.name.endswith(".sql")
    ]
    return sorted(m for m in found if m[0] <= version)


def test_migration_007_over_populated_006(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = connect(path)
    assert migrate(conn, _through(6)) == 6
    conn.close()
    conn = open_db(path)
    assert schema_version(conn) == latest_version() == 7
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
    assert {"slug", "name", "aliases", "base", "mode", "ci", "source", "last_used_at"} <= cols
    assert repos.list_all(conn) == []
    conn.close()


def test_add_get_and_defaults(db: sqlite3.Connection) -> None:
    r = repos.add(db, "Me/Trading-Bot", base="main", source="auto", ci=True)
    assert (r.name, r.mode, r.ci, r.quote_ask, r.aliases) == ("trading-bot", "push", True, True, ())
    assert r.remote == "github.com/Me/Trading-Bot" and r.last_used_at is None
    assert repos.get(db, "me/trading-bot") == r  # the slug is case-insensitive
    fork = repos.add(db, "up/stream", base="dev", source="auto", mode="fork")
    assert fork.quote_ask is False and fork.ci is None


def test_names_and_aliases_share_one_namespace(db: sqlite3.Connection) -> None:
    repos.add(db, "a/proj", base="main", source="owner", aliases=["p"])
    assert repos.add(db, "b/proj", base="main", source="auto").name == "b-proj"
    assert repos.add(db, "c/proj", base="main", source="auto").name == "c-proj"
    with pytest.raises(RepoConflict):
        repos.add(db, "d/x", base="main", source="owner", name="proj")
    with pytest.raises(RepoConflict):
        repos.add(db, "d/x", base="main", source="owner", name="x", aliases=["p"])
    with pytest.raises(RepoConflict):
        repos.add(db, "a/proj", base="main", source="owner")
    with pytest.raises(ValueError):
        repos.add(db, "d/x", base="main", source="owner", name="Bad Name")
    with pytest.raises(ValueError):
        repos.add(db, "not-a-slug", base="main", source="owner")


def test_odd_repo_names_get_valid_handles(db: sqlite3.Connection) -> None:
    assert repos.add(db, "me/.github", base="main", source="auto").name == "github"
    assert repos.add(db, "me/2048", base="main", source="auto").name == "r-2048"
    assert repos.add(db, "me/My.Repo_X", base="main", source="auto").name == "my-repo_x"


def test_find_by_slug_name_or_alias(db: sqlite3.Connection) -> None:
    r = repos.add(db, "me/dori-oncall", base="main", source="owner", aliases=["oncall"])
    for key in ("me/dori-oncall", "DORI-ONCALL", " oncall "):
        assert repos.find(db, key) == r
    assert repos.find(db, "dori") is None


def test_list_is_most_recently_used_first_then_newest(db: sqlite3.Connection) -> None:
    for slug in ("o/a", "o/b", "o/c"):
        repos.add(db, slug, base="main", source="auto")
    assert [r.name for r in repos.list_all(db)] == ["c", "b", "a"]
    assert repos.touch(db, "o/a") and not repos.touch(db, "o/zzz")
    assert [r.name for r in repos.list_all(db)] == ["a", "c", "b"]
    db.execute("UPDATE repos SET last_used_at = '2999-01-01T00:00:00.000+00:00' WHERE name = 'b'")
    assert [r.name for r in repos.list_all(db)] == ["b", "a", "c"]


def test_forget_and_set_ci(db: sqlite3.Connection) -> None:
    repos.add(db, "o/a", base="main", source="auto")
    assert repos.set_ci(db, "o/a", False)
    got = repos.get(db, "o/a")
    assert got is not None and got.ci is False and got.ci_checked_at is not None
    assert repos.forget(db, "a") and not repos.forget(db, "a")
    assert repos.get(db, "o/a") is None


def test_by_extension(db: sqlite3.Connection) -> None:
    repos.add(db, "me/bot", base="main", source="extension:trading", aliases=["trading"])
    repos.add(db, "me/other", base="main", source="auto")
    assert [r.slug for r in repos.by_extension(db, "Trading")] == ["me/bot"]
    assert repos.by_extension(db, "nope") == []


def test_import_config_is_idempotent_and_owner_sourced(db: sqlite3.Connection) -> None:
    cfg = {
        "proj": RunnerRepo(remote="github.com/own/proj", base="trunk", quote_ask=False),
        "tool": RunnerRepo(remote="github.com/own/tool", allow_github_changes=True),
    }
    assert repos.import_config(db, cfg) == 2
    first = repos.list_all(db)
    assert repos.import_config(db, cfg) == 2
    assert repos.list_all(db) == first
    proj = repos.find(db, "proj")
    assert proj is not None and (proj.source, proj.base, proj.quote_ask) == (
        "owner",
        "trunk",
        False,
    )
    tool = repos.find(db, "tool")
    assert tool is not None and tool.allow_github_changes and tool.mode == "push"


def test_import_config_adopts_an_auto_registered_repo(db: sqlite3.Connection) -> None:
    repos.add(db, "own/proj", base="main", source="auto")
    repos.touch(db, "own/proj")
    assert repos.add(db, "other/web", base="main", source="auto").name == "web"
    cfg = {
        "proj": RunnerRepo(remote="github.com/own/proj"),
        "web": RunnerRepo(remote="github.com/own/web"),
    }
    repos.import_config(db, cfg)
    web = repos.find(db, "web")
    assert web is not None and web.slug == "own/web"
    moved = repos.get(db, "other/web")
    assert moved is not None and moved.name == "other-web"
    proj = repos.get(db, "own/proj")
    assert proj is not None and proj.source == "owner" and proj.last_used_at is not None

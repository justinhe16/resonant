"""The repo registry (migration 007, resonant.store.repos)."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from resonant.config import RunnerRepo
from resonant.store import repos, runs
from resonant.store.db import connect, latest_version, migrate, open_db, schema_version
from resonant.store.repos import RepoConflict


def _through(version: int) -> list[tuple[int, str]]:
    found = [
        (int(f.name[:3]), f.read_text())
        for f in resources.files("resonant.store.migrations").iterdir()
        if f.name.endswith(".sql")
    ]
    return sorted(m for m in found if m[0] <= version)


def test_migration_008_over_populated_007(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = connect(path)
    assert migrate(conn, _through(7)) == 7
    repos.add(conn, "me/a", base="main", source="auto")
    conn.close()
    conn = open_db(path)
    assert schema_version(conn) == latest_version() == 12
    assert [r.slug for r in repos.list_all(conn)] == ["me/a"]
    assert repos.links(conn, "x") == []
    conn.close()


def test_migration_009_over_populated_008(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = connect(path)
    assert migrate(conn, _through(8)) == 8
    repos.add(conn, "me/a", base="main", source="auto")
    for _ in range(3):
        runs.create(conn, repo="a", ask="x", principal="owner", session_id="s", max_seconds=60)
    runs.update(conn, "R1", status="ready", pr_number=4, branch="resonant/r1-x")
    conn.execute("DELETE FROM runs WHERE code = 'R3'")  # the high-water mark is R3
    conn.close()
    conn = open_db(path)
    assert schema_version(conn) == 12
    old = runs.get(conn, "R1")
    assert old is not None and old.status == "ready" and old.pr_number == 4
    assert old.branch == "resonant/r1-x" and old.workspace is None and old.checkout is None
    new = runs.create(conn, repo="", ask="y", principal="owner", session_id="t", max_seconds=60)
    assert new.code == "R4" and new.scratch  # codes are never reused
    assert runs.finish(conn, "R4", "done", summary="ok")
    assert runs.get(conn, "R4").status == "done"  # type: ignore[union-attr]
    assert repos.get(conn, "me/a").path is None  # type: ignore[union-attr]
    assert repos.set_path(conn, "me/a", tmp_path)
    assert repos.get(conn, "me/a").path == tmp_path  # type: ignore[union-attr]
    names = {r["name"] for r in conn.execute("PRAGMA index_list(runs)")}
    assert {"runs_status", "runs_repo"} <= names
    conn.close()


def test_migration_007_over_populated_006(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = connect(path)
    assert migrate(conn, _through(6)) == 6
    conn.close()
    conn = open_db(path)
    assert schema_version(conn) == latest_version() == 12
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


def test_links_are_replaced_and_dropped_per_extension(db: sqlite3.Connection) -> None:
    repos.set_links(db, "trading", ["me/b", "me/a"])
    repos.set_links(db, "other", ["me/c"])
    assert repos.links(db, "Trading") == ["me/a", "me/b"]
    repos.set_links(db, "trading", ["me/d", "me/D"])
    assert repos.links(db, "trading") == ["me/d"]
    repos.drop_links(db, "trading")
    assert repos.links(db, "trading") == []
    assert repos.links(db, "other") == ["me/c"]
    assert repos.list_all(db) == []  # links never register a repo


def test_import_config_is_idempotent_and_owner_sourced(db: sqlite3.Connection) -> None:
    cfg = {
        "proj": RunnerRepo(remote="github.com/own/proj", base="trunk", quote_ask=False),
        "tool": RunnerRepo(remote="github.com/own/tool", allow_github_changes=True),
    }
    assert repos.import_config(db, cfg).imported == 2
    first = repos.list_all(db)
    assert repos.import_config(db, cfg).imported == 2
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


def test_import_config_skips_an_owner_clash_and_imports_the_rest(db: sqlite3.Connection) -> None:
    first = repos.import_config(db, {"web": RunnerRepo(remote="github.com/a/web")})
    assert first.imported == 1
    again = repos.import_config(db, {"web": RunnerRepo(remote="github.com/b/web")})
    assert [n for n, _ in again.skipped] == ["web"]  # an owner entry already has that name
    other = repos.import_config(
        db,
        {"web": RunnerRepo(remote="github.com/b/web"), "ok": RunnerRepo(remote="github.com/b/ok")},
    )
    assert other.imported == 1 and repos.find(db, "ok") is not None
    assert repos.find(db, "web").slug == "a/web"  # pyright: ignore[reportOptionalMemberAccess]


def test_a_path_only_entry_keeps_the_registrys_values(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """An entry added for a checkout path doesn't reset what the registry knows (EM-11)."""
    repos.add(db, "own/pub", base="develop", source="auto", mode="fork")
    before = repos.get(db, "own/pub")
    assert before is not None and before.allow_github_changes and not before.quote_ask
    cfg = {"pub": RunnerRepo(remote="github.com/own/pub", path=tmp_path)}
    assert repos.import_config(db, cfg).imported == 1
    after = repos.get(db, "own/pub")
    assert after is not None
    assert (after.base, after.mode, after.allow_github_changes, after.quote_ask) == (
        "develop",
        "fork",
        True,
        False,
    )
    assert (after.source, after.path) == ("owner", tmp_path)
    assert repos.import_config(db, cfg).imported == 1 and repos.get(db, "own/pub") == after


def test_a_new_entry_gets_the_registry_defaults_and_set_fields_win(
    db: sqlite3.Connection,
) -> None:
    repos.import_config(db, {"proj": RunnerRepo(remote="github.com/own/proj")})
    proj = repos.get(db, "own/proj")
    assert proj is not None
    assert (proj.base, proj.mode, proj.allow_github_changes, proj.quote_ask) == (
        "main",
        "push",
        True,
        True,
    )
    # Fields an entry does set are written, over a new row and an existing one alike.
    cfg = {
        n: RunnerRepo(remote=f"github.com/own/{n}", allow_github_changes=False, base="trunk")
        for n in ("proj", "new")
    }
    repos.import_config(db, cfg)
    for slug in ("own/proj", "own/new"):
        r = repos.get(db, slug)
        assert r is not None and (r.base, r.allow_github_changes, r.quote_ask) == (
            "trunk",
            False,
            True,
        )
    # A later entry that no longer sets it leaves the stricter value alone.
    repos.import_config(db, {"proj": RunnerRepo(remote="github.com/own/proj")})
    again = repos.get(db, "own/proj")
    assert again is not None and (again.base, again.allow_github_changes) == ("trunk", False)


def test_import_config_rejects_an_invalid_name_on_the_update_path(db: sqlite3.Connection) -> None:
    repos.add(db, "own/proj", base="main", source="auto")
    bad = RunnerRepo.model_construct(remote="github.com/own/proj", base="main")
    res = repos.import_config(db, {"../x": bad, "Bad Name": bad})
    assert res.imported == 0 and len(res.skipped) == 2
    proj = repos.get(db, "own/proj")
    assert proj is not None and proj.name == "proj" and proj.source == "auto"


def _run(db: sqlite3.Connection, repo: str, status: str) -> None:
    db.execute(
        "INSERT INTO runs (code, principal, repo, ask, status, session_id, max_seconds,"
        " created_at, updated_at) VALUES (?, 'o', ?, 'x', ?, 's', 60, 't', 't')",
        (f"R{db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] + 1}", repo, status),
    )


def test_rename_on_clash_is_refused_while_a_run_is_in_progress(db: sqlite3.Connection) -> None:
    repos.add(db, "other/web", base="main", source="auto")
    _run(db, "web", "ready")
    cfg = {"web": RunnerRepo(remote="github.com/own/web")}
    res = repos.import_config(db, cfg)
    assert res.imported == 0 and "run in progress" in res.skipped[0][1]
    assert repos.get(db, "other/web").name == "web"  # pyright: ignore[reportOptionalMemberAccess]
    assert repos.get(db, "own/web") is None
    db.execute("UPDATE runs SET status = 'merged'")
    res = repos.import_config(db, cfg)
    assert res.renamed == (("web", "other-web"),) and res.skipped == ()
    assert db.execute("SELECT repo FROM runs").fetchone()[0] == "other-web"  # history follows


def test_renaming_an_existing_repo_keeps_the_old_name_as_an_alias(db: sqlite3.Connection) -> None:
    repos.add(db, "own/proj", base="main", source="auto")
    res = repos.import_config(db, {"mine": RunnerRepo(remote="github.com/own/proj")})
    assert res.renamed == (("proj", "mine"),)
    r = repos.get(db, "own/proj")
    assert r is not None and r.name == "mine" and r.aliases == ("proj",)
    assert repos.find(db, "proj") == r


def test_slugs_are_case_insensitive_and_dots_are_rejected(db: sqlite3.Connection) -> None:
    repos.add(db, "Own/Proj", base="main", source="auto")
    with pytest.raises(RepoConflict):
        repos.add(db, "own/proj", base="main", source="auto")
    for bad in ("../..", "own/..", "./x", "own/."):
        with pytest.raises(ValueError):
            repos.add(db, bad, base="main", source="auto")

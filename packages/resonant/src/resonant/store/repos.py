"""The repo registry (migration 007) and extension repo links (008). Plain functions over a
connection.

A repo is a GitHub ``owner/name`` the Claude runner may work in (docs/repos-plan.md D1). It has
a short unique ``name`` (the handle in ``runs.repo`` and the mirror's dir name), optional
aliases, and the settings the runner needs: the base branch, ``mode`` (``push``, or ``fork``
for a public repo the token can't push to), whether CI was seen, and the PR-body switches.

Names and aliases share one namespace, compared case-insensitively, so a handle never means
two repos. Callers own transactions; every function here is one statement or joins the
caller's.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from resonant.store.db import atomic, now_iso

if TYPE_CHECKING:
    from resonant.config import RunnerRepo

Mode = Literal["push", "fork"]

NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class RepoConflict(ValueError):
    """The slug, name or an alias is already taken (by another repo)."""


@dataclass(frozen=True)
class Repo:
    slug: str
    name: str
    aliases: tuple[str, ...]
    base: str
    mode: Mode
    ci: bool | None
    ci_checked_at: datetime | None
    allow_github_changes: bool
    quote_ask: bool
    source: str
    added_at: datetime
    last_used_at: datetime | None
    path: Path | None = None  # the owner's checkout, when not <runner.code_dir>/<name> (009)

    @property
    def remote(self) -> str:
        """``github.com/owner/name``, the form the workspace builds git URLs from."""
        return f"github.com/{self.slug}"


def _dt(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def _row(r: sqlite3.Row) -> Repo:
    added = _dt(r["added_at"])
    assert added is not None
    return Repo(
        slug=r["slug"],
        name=r["name"],
        aliases=tuple(cast(list[str], json.loads(r["aliases"]))),
        base=r["base"],
        mode=r["mode"],
        ci=None if r["ci"] is None else bool(r["ci"]),
        ci_checked_at=_dt(r["ci_checked_at"]),
        allow_github_changes=bool(r["allow_github_changes"]),
        quote_ask=bool(r["quote_ask"]),
        source=r["source"],
        added_at=added,
        last_used_at=_dt(r["last_used_at"]),
        # no column before migration 009 (a migration reading the registry part way)
        path=Path(r["path"]) if "path" in r.keys() and r["path"] is not None else None,  # noqa: SIM118
    )


def sanitize_name(text: str) -> str:
    """A valid handle from free text: lowercase, ``[a-z0-9_-]``, starting with a letter."""
    name = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    if not name or not name[0].isalpha():
        name = f"r-{name}".rstrip("-")
    return name[:32].rstrip("-_") or "repo"


def _taken(conn: sqlite3.Connection, handle: str, *, except_slug: str | None = None) -> bool:
    """Whether ``handle`` is the name or an alias of a repo other than ``except_slug``."""
    for r in conn.execute("SELECT slug, name, aliases FROM repos"):
        if except_slug is not None and r["slug"].lower() == except_slug.lower():
            continue
        if handle.lower() == r["name"].lower():
            return True
        if handle.lower() in (a.lower() for a in json.loads(r["aliases"])):
            return True
    return False


def unique_name(
    conn: sqlite3.Connection,
    slug: str,
    *,
    except_slug: str | None = None,
    avoid: str | None = None,
) -> str:
    """A free handle for ``owner/name``: the repo name, else ``owner-name``, else numbered.
    ``except_slug`` is a repo whose own handles don't count as taken; ``avoid`` is a handle
    not to return."""
    owner, _, repo = slug.partition("/")

    def free(handle: str) -> bool:
        return handle != (avoid or "").lower() and not _taken(conn, handle, except_slug=except_slug)

    for candidate in (sanitize_name(repo), sanitize_name(f"{owner}-{repo}")):
        if free(candidate):
            return candidate
    base = sanitize_name(f"{owner}-{repo}")[:29]
    n = 2
    while not free(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def _check_handle(handle: str) -> None:
    if not NAME.fullmatch(handle):
        raise ValueError(f"repo handle {handle!r} must match [a-z][a-z0-9_-]{{0,31}}")


def add(
    conn: sqlite3.Connection,
    slug: str,
    *,
    base: str,
    source: str,
    name: str | None = None,
    aliases: Iterable[str] = (),
    mode: Mode = "push",
    ci: bool | None = None,
    allow_github_changes: bool = True,
    quote_ask: bool | None = None,
) -> Repo:
    """Register ``owner/name``. ``name`` defaults to a free handle derived from the slug;
    ``quote_ask`` defaults to off for ``fork`` repos (a public ask shouldn't show).
    ``allow_github_changes`` defaults to on (docs/repos-plan.md D10): the session's token has
    Workflows, and the changes are listed in the merge prompt instead of refused. Raises
    :class:`RepoConflict` when the slug, name or an alias is taken."""
    if not SLUG.fullmatch(slug) or {".", ".."} & set(slug.split("/")):
        raise ValueError(f"{slug!r} isn't owner/name")
    ts = now_iso()
    with atomic(conn):
        if conn.execute("SELECT 1 FROM repos WHERE slug = ?", (slug,)).fetchone():
            raise RepoConflict(f"{slug} is already registered")
        name = name if name is not None else unique_name(conn, slug)
        handles = [name, *aliases]
        for handle in handles:
            _check_handle(handle)
            if _taken(conn, handle) or handles.count(handle) > 1:
                raise RepoConflict(f"{handle} is already a repo name or alias")
        conn.execute(
            "INSERT INTO repos (slug, name, aliases, base, mode, ci, ci_checked_at,"
            " allow_github_changes, quote_ask, source, added_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                slug,
                name,
                json.dumps(list(handles[1:])),
                base,
                mode,
                None if ci is None else int(ci),
                None if ci is None else ts,
                int(allow_github_changes),
                int(mode == "push" if quote_ask is None else quote_ask),
                source,
                ts,
            ),
        )
    repo = get(conn, slug)
    assert repo is not None
    return repo


def get(conn: sqlite3.Connection, slug: str) -> Repo | None:
    r = conn.execute("SELECT * FROM repos WHERE slug = ?", (slug,)).fetchone()
    return None if r is None else _row(r)


def find(conn: sqlite3.Connection, key: str) -> Repo | None:
    """The repo whose slug, name or alias is ``key`` (case-insensitive)."""
    key = key.strip().lower()
    for repo in list_all(conn):
        if key in (repo.slug.lower(), repo.name.lower(), *(a.lower() for a in repo.aliases)):
            return repo
    return None


def set_links(conn: sqlite3.Connection, extension: str, slugs: Iterable[str]) -> None:
    """Replace the repos ``extension`` links (migration 008). Links are kept apart from the
    registry, so they can name a repo that hasn't been resolved against GitHub yet."""
    with atomic(conn):
        conn.execute("DELETE FROM repo_links WHERE extension = ?", (extension,))
        conn.executemany(
            "INSERT OR IGNORE INTO repo_links (extension, slug) VALUES (?, ?)",
            [(extension, slug) for slug in slugs],
        )


def drop_links(conn: sqlite3.Connection, extension: str) -> None:
    with atomic(conn):
        conn.execute("DELETE FROM repo_links WHERE extension = ?", (extension,))


def enabled_links(conn: sqlite3.Connection, extension: str) -> list[str]:
    """Like :func:`links`, but only while the extension is installed and enabled: a disabled
    or tampered extension's name stops meaning anything to the resolver."""
    rows = conn.execute(
        "SELECT l.slug FROM repo_links l JOIN extensions e ON e.name = l.extension COLLATE NOCASE"
        " WHERE l.extension = ? AND e.status = 'enabled' ORDER BY l.slug",
        (extension,),
    )
    return [r["slug"] for r in rows]


def linking_extensions(conn: sqlite3.Connection) -> list[str]:
    """Enabled extensions that link at least one repo, by name (``trading`` names its repo)."""
    rows = conn.execute(
        "SELECT DISTINCT e.name FROM repo_links l JOIN extensions e"
        " ON e.name = l.extension COLLATE NOCASE WHERE e.status = 'enabled' ORDER BY e.name"
    )
    return [r["name"] for r in rows]


def links(conn: sqlite3.Connection, extension: str) -> list[str]:
    """``owner/name`` slugs ``extension`` links (name compared case-insensitively), sorted."""
    rows = conn.execute(
        "SELECT slug FROM repo_links WHERE extension = ? ORDER BY slug", (extension,)
    )
    return [r["slug"] for r in rows]


def list_all(conn: sqlite3.Connection) -> list[Repo]:
    """Most recently used first; never-used repos last, newest registered first."""
    rows = conn.execute(
        "SELECT * FROM repos ORDER BY last_used_at IS NULL, last_used_at DESC, added_at DESC,"
        " rowid DESC"
    )
    return [_row(r) for r in rows]


def touch(conn: sqlite3.Connection, slug: str) -> bool:
    return (
        conn.execute("UPDATE repos SET last_used_at = ? WHERE slug = ?", (now_iso(), slug)).rowcount
        == 1
    )


_TERMINAL = ("merged", "failed", "cancelled", "no_changes", "done")


def add_alias(conn: sqlite3.Connection, slug: str, alias: str) -> Repo:
    """Give a repo another handle. Raises :class:`RepoConflict` if it is taken elsewhere."""
    repo = get(conn, slug)
    if repo is None:
        raise ValueError(f"{slug} isn't registered")
    _check_handle(alias)
    if alias.lower() == repo.name.lower() or alias.lower() in (a.lower() for a in repo.aliases):
        return repo
    with atomic(conn):
        if _taken(conn, alias):
            raise RepoConflict(f"{alias} is already a repo name or alias")
        conn.execute(
            "UPDATE repos SET aliases = ? WHERE slug = ?",
            (json.dumps([*repo.aliases, alias]), slug),
        )
    updated = get(conn, slug)
    assert updated is not None
    return updated


def set_ci(conn: sqlite3.Connection, slug: str, ci: bool) -> bool:
    """Record whether a ``pull_request`` workflow exists, and when that was looked at."""
    return (
        conn.execute(
            "UPDATE repos SET ci = ?, ci_checked_at = ? WHERE slug = ?", (int(ci), now_iso(), slug)
        ).rowcount
        == 1
    )


def set_path(conn: sqlite3.Connection, slug: str, path: Path | None) -> bool:
    """Where the owner's checkout of the repo lives (None: ``<runner.code_dir>/<name>``)."""
    if path is not None and not path.is_absolute():
        raise ValueError("a checkout path must be absolute")
    value = None if path is None else str(path)
    return conn.execute("UPDATE repos SET path = ? WHERE slug = ?", (value, slug)).rowcount == 1


def open_runs(conn: sqlite3.Connection, name: str) -> list[str]:
    """Codes of the repo's runs that aren't over (including one ready to merge)."""
    rows = conn.execute("SELECT code, status FROM runs WHERE repo = ? COLLATE NOCASE", (name,))
    return [r["code"] for r in rows if r["status"] not in _TERMINAL]


def forget(conn: sqlite3.Connection, key: str) -> bool:
    """Drop a repo from the registry (by slug, name or alias). Nothing on GitHub is touched."""
    repo = find(conn, key)
    if repo is None:
        return False
    return conn.execute("DELETE FROM repos WHERE slug = ?", (repo.slug,)).rowcount == 1


@dataclass(frozen=True)
class ImportResult:
    imported: int
    renamed: tuple[tuple[str, str], ...]  # (old name, new name): the caller moves mirrors
    skipped: tuple[tuple[str, str], ...]  # (config name, reason)


def _rename(conn: sqlite3.Connection, repo: Repo, new_name: str, *, keep_old: bool) -> None:
    """Give ``repo`` a new handle. Runs name their repo by handle, so a repo with a run that
    isn't over (including one ready to merge) can't be renamed; finished runs follow it.
    ``keep_old`` leaves the old handle as an alias."""
    _check_handle(new_name)
    busy = conn.execute(
        "SELECT 1 FROM runs WHERE repo = ? COLLATE NOCASE AND status NOT IN (?, ?, ?, ?, ?)"
        " LIMIT 1",
        (repo.name, *_TERMINAL),
    ).fetchone()
    if busy:
        raise RepoConflict(f"{repo.slug} has a run in progress, so it can't be renamed")
    aliases = [a for a in repo.aliases if a.lower() != new_name.lower()]
    if keep_old:
        aliases.append(repo.name)
    conn.execute(
        "UPDATE repos SET name = ?, aliases = ? WHERE slug = ?",
        (new_name, json.dumps(aliases), repo.slug),
    )
    conn.execute("UPDATE runs SET repo = ? WHERE repo = ? COLLATE NOCASE", (new_name, repo.name))


def _import_one(
    conn: sqlite3.Connection, name: str, cfg: RunnerRepo, renamed: list[tuple[str, str]]
) -> None:
    _check_handle(name)
    existing = get(conn, cfg.slug)
    squatter = find(conn, name)
    if squatter is not None and (existing is None or squatter.slug != existing.slug):
        if squatter.source == "owner":
            raise RepoConflict(f"runner.repos name {name!r} is used by {squatter.slug}")
        if squatter.name.lower() == name.lower():
            # The old handle now belongs to the owner's entry, so it can't be kept as an alias.
            new = unique_name(conn, squatter.slug, except_slug=squatter.slug, avoid=name.lower())
            _rename(conn, squatter, new, keep_old=False)
            renamed.append((squatter.name, new))
        else:  # only an alias of the other repo
            aliases = [a for a in squatter.aliases if a.lower() != name.lower()]
            conn.execute(
                "UPDATE repos SET aliases = ? WHERE slug = ?",
                (json.dumps(aliases), squatter.slug),
            )
    if existing is None:
        add(
            conn,
            cfg.slug,
            name=name,
            base=cfg.base,
            source="owner",
            allow_github_changes=cfg.allow_github_changes,
            quote_ask=cfg.quote_ask,
        )
        set_path(conn, cfg.slug, cfg.path)
        return
    if existing.name.lower() != name.lower():
        _rename(conn, existing, name, keep_old=True)
        renamed.append((existing.name, name))
    conn.execute(
        "UPDATE repos SET base = ?, allow_github_changes = ?, quote_ask = ?, source = 'owner'"
        " WHERE slug = ?",
        (cfg.base, int(cfg.allow_github_changes), int(cfg.quote_ask), existing.slug),
    )
    set_path(conn, existing.slug, cfg.path)


def import_config(conn: sqlite3.Connection, configured: Mapping[str, RunnerRepo]) -> ImportResult:
    """Upsert the ``runner.repos`` config entries as ``source=owner`` (idempotent: a second
    call changes nothing). The config is only a seed after this. The configured name wins: a
    repo that auto-registered under it is renamed (unless it has a run in progress), and a
    repo already registered under another handle keeps that handle as an alias.

    An entry that can't be imported (a bad handle, a clash with another owner entry, a rename
    blocked by a run) is skipped with a reason; the rest still import."""
    renamed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    done = 0
    with atomic(conn):
        for name, cfg in configured.items():
            mark = len(renamed)
            try:
                with atomic(conn):  # a skipped entry leaves nothing half done
                    _import_one(conn, name, cfg, renamed)
            except (RepoConflict, ValueError) as e:
                del renamed[mark:]
                skipped.append((name, str(e)))
            else:
                done += 1
    return ImportResult(done, tuple(renamed), tuple(skipped))

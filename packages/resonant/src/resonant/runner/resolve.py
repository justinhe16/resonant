"""Turn the words in a request into a repo (docs/repos-plan.md D2).

Tried in order, the way a person would:

1. ``owner/name`` or a ``github.com`` URL.
2. A registry name or alias.
3. An extension's linked repos (its manifest ``repos``), by extension name. One link resolves
   like that slug; several are :class:`Ambiguous`.
4. The owner's GitHub repos (listed through the API, cached for an hour), fuzzy-matched by name.

The first time a repo is resolved, GitHub is asked what access the token has: push access
registers it ``mode=push``, a public repo it can't push to ``mode=fork``, anything else is
:class:`NoAccess`. It is registered ``source=auto`` and the result says it is new. A name that
matches several repos is :class:`Ambiguous` (at most three candidates), and no match is
:class:`NotFound`.

Every GitHub call goes through the :class:`GitHub` protocol. The real one runs ``gh`` through
the workspace's per-call ``GH_CONFIG_DIR``, so the token is never in env or argv; tests use a
fake.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from resonant.config import RunnerRepo
from resonant.runner.checks import MAX_WORKFLOW_BYTES, runs_on_pull_request
from resonant.runner.workspace import Workspace
from resonant.secrets import RunnerSecrets
from resonant.store import repos
from resonant.store.db import atomic
from resonant.store.repos import Repo, RepoConflict

LIST_TTL_S = 3600.0
MAX_CANDIDATES = 3
MAX_LISTED = 1000  # GitHub repos remembered from the owner's list
MAX_WORKFLOWS = 20  # workflow files read to look for a pull_request trigger
FUZZY_CUTOFF = 0.7
EXACT = 2.0  # a score above any fuzzy ratio

_URL = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:[/?#].*)?",
    re.IGNORECASE,
)
_PLAIN = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?")
_WORKFLOW_FILE = re.compile(r"[\w.-]+\.ya?ml")


class GitHubError(Exception):
    """GitHub couldn't be asked (not reachable, bad token, rate limit). Never carries a
    token: the workspace redacts ``gh``'s stderr."""


class NeedsToken(GitHubError):
    """The answer needs GitHub and the daemon has no token to ask with (RX-1)."""


@dataclass(frozen=True)
class RepoInfo:
    slug: str  # as GitHub spells it
    base: str  # the default branch
    private: bool
    can_push: bool
    has_ci: bool  # a workflow on the base branch runs on pull_request


class GitHub(Protocol):
    async def repo_info(self, slug: str) -> RepoInfo | None:
        """The repo and the token's access to it, or None if it can't be seen (missing, or
        private with no access)."""
        ...

    async def my_repos(self) -> list[str]:
        """``owner/name`` of the repos the token can see (the owner's, collaborations, orgs)."""
        ...

    async def is_protected(self, slug: str, branch: str) -> bool | None:
        """Whether ``branch`` has branch protection, or None if that can't be told."""
        ...


@dataclass(frozen=True)
class Resolved:
    repo: Repo
    new: bool  # registered by this call
    via: str | None = None  # the extension whose link led here
    warning: str | None = None  # for the reply to append (a new push repo's base is unprotected)

    @property
    def note(self) -> str:
        """``(new repo: owner/name)`` for a repo this call registered, else empty."""
        if not self.new:
            return ""
        by = f" via {self.via}" if self.via else ""
        return f"(new repo: {self.repo.slug}{by})"


@dataclass(frozen=True)
class Ambiguous:
    candidates: tuple[str, ...]  # at most MAX_CANDIDATES

    @property
    def text(self) -> str:
        return f"Which repo? {', '.join(self.candidates)}"


@dataclass(frozen=True)
class NotFound:
    query: str


@dataclass(frozen=True)
class NoAccess:
    slug: str

    @property
    def text(self) -> str:
        return f"No access to {self.slug}."


@dataclass(frozen=True)
class NotPushable(NoAccess):
    """An extension links a repo the token can't push to. Links don't register those (a
    :class:`NoAccess`, so callers refuse it like one)."""

    extension: str = ""

    @property
    def text(self) -> str:
        return (
            f"{self.slug} (linked by {self.extension}) isn't one you can push to; "
            f"add it yourself with its owner/name."
        )


Resolution = Resolved | Ambiguous | NotFound | NoAccess


def parse_slug(text: str) -> str | None:
    """``owner/name`` from ``owner/name``, ``github.com/owner/name`` or a URL on it."""
    text = text.strip()
    m = _URL.fullmatch(text) or _PLAIN.fullmatch(text)
    if m is None or {".", ".."} & {m[1], m[2]}:
        return None
    return f"{m[1]}/{m[2]}"


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


class Resolver:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        ttl_s: float = LIST_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.conn = conn
        self.ttl_s = ttl_s
        self._clock = clock
        self._mine: tuple[float, list[str]] | None = None  # (fetched at, slugs)

    async def resolve(self, text: str, github: GitHub) -> Resolution:
        """Raises :class:`GitHubError` when GitHub is needed and can't be reached."""
        text = text.strip()
        if not text:
            return NotFound(text)
        if (slug := parse_slug(text)) is not None:
            return await self._by_slug(slug, github)
        if (known := repos.find(self.conn, text)) is not None:
            return Resolved(known, new=False)
        linked = repos.enabled_links(self.conn, text)
        if len(linked) == 1:
            return await self._by_slug(linked[0], github, via=text.lower())
        if linked:
            return Ambiguous(tuple(linked[:MAX_CANDIDATES]))
        return await self._fuzzy(text, github)

    async def _by_slug(self, slug: str, github: GitHub, via: str | None = None) -> Resolution:
        if (known := repos.get(self.conn, slug)) is not None:
            return Resolved(known, new=False, via=via)
        info = await github.repo_info(slug)
        if info is None or not (info.can_push or not info.private):
            return NoAccess(slug)
        if (known := repos.get(self.conn, info.slug)) is not None:  # GitHub spells it otherwise
            return Resolved(known, new=False, via=via)
        if via is not None and not info.can_push:
            return NotPushable(slug, via)  # a link never writes a fork-mode row
        try:
            with atomic(self.conn):
                repo = repos.add(
                    self.conn,
                    info.slug,
                    base=info.base,
                    source="auto",
                    mode="push" if info.can_push else "fork",
                    ci=info.has_ci,
                )
        except (RepoConflict, ValueError):
            # Another request registered it while GitHub was being asked.
            if (known := repos.get(self.conn, info.slug)) is None:
                raise
            return Resolved(known, new=False, via=via)
        return Resolved(
            repo, new=True, via=via, warning=await self._protection_warning(repo, github)
        )

    @staticmethod
    async def _protection_warning(repo: Repo, github: GitHub) -> str | None:
        """Only for a push repo (a fork repo's branches aren't ours to protect). Never blocks,
        and an answer that can't be had is no warning."""
        if repo.mode != "push":
            return None
        try:
            protected = await github.is_protected(repo.slug, repo.base)
        except GitHubError:
            return None
        return f"{repo.base} isn't protected" if protected is False else None

    async def _list(self, github: GitHub) -> list[str]:
        now = self._clock()
        if self._mine is None or now - self._mine[0] >= self.ttl_s:
            self._mine = (now, (await github.my_repos())[:MAX_LISTED])
        return self._mine[1]

    async def _fuzzy(self, text: str, github: GitHub) -> Resolution:
        query = _squash(text)
        if not query:
            return NotFound(text)
        scored: list[tuple[float, str]] = []
        for slug in await self._list(github):
            name = _squash(slug.partition("/")[2])
            if not name:
                continue
            if name == query:
                scored.append((EXACT, slug))
                continue
            ratio = difflib.SequenceMatcher(None, query, name).ratio()
            contained = len(query) >= 3 and len(name) >= 3 and (query in name or name in query)
            if ratio >= FUZZY_CUTOFF or contained:
                scored.append((ratio, slug))
        scored.sort(key=lambda s: (-s[0], s[1].lower()))
        if not scored:
            return NotFound(text)
        exact = [slug for score, slug in scored if score == EXACT]
        if len(exact) == 1 or len(scored) == 1:
            return await self._by_slug(exact[0] if exact else scored[0][1], github)
        return Ambiguous(tuple((exact or [slug for _, slug in scored])[:MAX_CANDIDATES]))


class GhGitHub:
    """The real :class:`GitHub`: ``gh api`` through the workspace's throwaway config dir."""

    def __init__(self, workspace: Workspace, secrets: RunnerSecrets) -> None:
        self._ws = workspace
        self._secrets = secrets

    async def _api(self, *args: str) -> tuple[int, str, str]:
        res = await self._ws.gh(["api", *args], self._secrets)
        return res.returncode, res.stdout.decode(errors="replace"), res.stderr

    async def repo_info(self, slug: str) -> RepoInfo | None:
        code, out, err = await self._api(f"repos/{slug}")
        if code != 0:
            if "HTTP 404" in err:
                return None
            raise GitHubError(f"couldn't read {slug} from GitHub: {err[-120:]}")
        try:
            data: Any = json.loads(out)
            name, base = str(data["full_name"]), str(data["default_branch"])
            private = bool(data["private"])
            permissions = cast("dict[str, Any]", data.get("permissions") or {})
            can_push = bool(permissions.get("push"))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise GitHubError(f"GitHub's answer about {slug} wasn't readable") from None
        try:
            RunnerRepo(remote=f"github.com/{name}", base=base)  # the config's own checks
        except ValueError:
            raise GitHubError(f"GitHub's answer about {slug} wasn't readable") from None
        if parse_slug(name) != name:
            raise GitHubError(f"GitHub's answer about {slug} wasn't readable")
        return RepoInfo(name, base, private, can_push, await self._has_ci(name, base))

    async def _has_ci(self, slug: str, base: str) -> bool:
        """Whether a workflow on the base branch runs on ``pull_request``. A failure to read
        is "no": the run's own check in the mirror is what decides."""
        ref = ["-X", "GET", "-f", f"ref={base}"]
        code, out, _ = await self._api(f"repos/{slug}/contents/.github/workflows", *ref)
        if code != 0:
            return False
        try:
            names = [str(e["name"]) for e in json.loads(out) if e.get("type") == "file"]
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
        for name in [n for n in names if _WORKFLOW_FILE.fullmatch(n)][:MAX_WORKFLOWS]:
            path = f"repos/{slug}/contents/.github/workflows/{name}"
            code, text, _ = await self._api(path, *ref, "-H", "Accept: application/vnd.github.raw")
            if code == 0 and len(text) <= MAX_WORKFLOW_BYTES and runs_on_pull_request(text):
                return True
        return False

    async def is_protected(self, slug: str, branch: str) -> bool | None:
        code, out, _ = await self._api(f"repos/{slug}/branches/{branch}")
        if code != 0:
            return None
        try:
            return bool(json.loads(out)["protected"])
        except (ValueError, KeyError, TypeError):
            return None

    async def my_repos(self) -> list[str]:
        code, out, err = await self._api(
            "user/repos", "-X", "GET", "-f", "per_page=100", "-f", "sort=pushed",
            "-f", "affiliation=owner,collaborator,organization_member",
            "--paginate", "--jq", ".[].full_name",
        )  # fmt: skip
        if code != 0:
            raise GitHubError(f"couldn't list your GitHub repos: {err[-120:]}")
        return [line.strip() for line in out.splitlines() if parse_slug(line.strip())]


class OfflineGitHub:
    """The :class:`GitHub` when the daemon's token isn't set (RX-1): registered repos still
    resolve (the registry is asked first), and anything that needs GitHub raises
    :class:`NeedsToken` instead of asking."""

    async def repo_info(self, slug: str) -> RepoInfo | None:
        raise NeedsToken(f"looking up {slug} needs the daemon's GitHub token")

    async def my_repos(self) -> list[str]:
        raise NeedsToken("listing your GitHub repos needs the daemon's GitHub token")

    async def is_protected(self, slug: str, branch: str) -> bool | None:
        return None


class FakeGitHub:
    """A :class:`GitHub` for tests: ``known`` maps ``owner/name`` (any case) to its info,
    ``mine`` is the owner's repo list, ``protected`` what ``is_protected`` answers. ``calls``
    records what was asked; ``error`` makes every
    call raise :class:`GitHubError`."""

    def __init__(
        self,
        known: list[RepoInfo] | None = None,
        mine: list[str] | None = None,
        error: str | None = None,
        protected: bool | None = True,
    ) -> None:
        self.protected = protected
        self.known = {i.slug.lower(): i for i in known or []}
        self.mine = [i.slug for i in known or []] if mine is None else mine
        self.error = error
        self.calls: list[str] = []

    async def repo_info(self, slug: str) -> RepoInfo | None:
        self.calls.append(f"repo_info {slug}")
        if self.error:
            raise GitHubError(self.error)
        return self.known.get(slug.lower())

    async def is_protected(self, slug: str, branch: str) -> bool | None:
        self.calls.append(f"is_protected {slug} {branch}")
        return self.protected

    async def my_repos(self) -> list[str]:
        self.calls.append("my_repos")
        if self.error:
            raise GitHubError(self.error)
        return list(self.mine)

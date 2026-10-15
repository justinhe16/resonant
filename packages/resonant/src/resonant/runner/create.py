"""Create repos and add starter CI (docs/repos-plan.md D5, D6; owner answer to Q2: option a).

Everything runs as the daemon, with the runner's GitHub token in ``gh``'s throwaway
``GH_CONFIG_DIR`` (never env or argv), and never inside the sandbox.

- :meth:`Creator.create_repo` makes a repo under the token's account, commits a README and the
  starter CI workflow through the contents API, protects the default branch, and registers it
  ``source=created, mode=push, ci=true``. If GitHub refuses the protection (a private repo on a
  Free plan answers 403) the repo is still created and the result says so.
- :meth:`Creator.add_ci` opens a PR with the starter workflow on a repo that has none.
- The starter CI is one fixed file shipped in this package (``templates/ci.yml``). Its SHA-256
  is :data:`CI_TEMPLATE_SHA256`, checked before every write of it, and it is only ever written
  to :data:`CI_PATH`. Nothing else under ``.github/`` is written by this module.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from resonant.runner.resolve import GitHub, GitHubError, parse_slug
from resonant.runner.workspace import Exec
from resonant.secrets import RunnerSecrets
from resonant.store import repos
from resonant.store.db import atomic
from resonant.store.repos import Repo, RepoConflict

CI_TEMPLATE_PATH = Path(__file__).parent / "templates" / "ci.yml"
CI_TEMPLATE_SHA256 = "795f9f3bffdd066a9c8e24dd133d0dc169dd96c5d69913aa750c642bad2abd60"
CI_PATH = ".github/workflows/resonant-ci.yml"
CI_CHECK = "ci"  # the starter workflow's job name: the required status check
CI_BRANCH = "resonant/add-ci"
PR_BODY = "Adds a pull_request workflow that runs ./ci.sh when present. Added by Resonant."
NAME_MAX = 100
DESCRIPTION_MAX = 350  # GitHub's limit
PROTECTION_UNAVAILABLE = "protection not available on this plan"

_NAME = re.compile(r"[A-Za-z0-9._-]+")
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")


class CreateError(Exception):
    """A sentence fit for the owner. Never carries a token (the workspace redacts stderr)."""


class Gh(Protocol):
    """What this module uses of the workspace: ``gh`` with the token in a throwaway config."""

    async def gh(
        self, args: Sequence[str], secrets: RunnerSecrets, *, stdin: bytes | None = None
    ) -> Exec: ...


def valid_repo_name(name: str) -> str | None:
    """Why ``name`` isn't a usable GitHub repo name, or None if it is."""
    if not name or len(name) > NAME_MAX:
        return f"a repo name is 1 to {NAME_MAX} characters"
    if not _NAME.fullmatch(name):
        return "a repo name may only use letters, digits, '.', '_' and '-'"
    if name in (".", "..") or name.startswith("-") or name.lower().endswith(".git"):
        return f"{name!r} isn't a usable repo name"
    return None


def ci_template() -> bytes:
    """The starter workflow, verified against :data:`CI_TEMPLATE_SHA256`. Called before every
    push of it."""
    data = CI_TEMPLATE_PATH.read_bytes()
    if hashlib.sha256(data).hexdigest() != CI_TEMPLATE_SHA256:
        raise CreateError("the starter CI file doesn't match its checksum; not writing it")
    return data


@dataclass(frozen=True)
class Created:
    repo: Repo
    url: str
    protection: str  # whether the default branch is protected, and why not

    @property
    def text(self) -> str:
        return f"Created {self.repo.slug} ({self.url}). {self.protection}"


@dataclass(frozen=True)
class CiPr:
    number: int | None
    url: str | None
    already: bool  # the repo already had CI, so no PR was opened

    @property
    def text(self) -> str:
        if self.already:
            return "That repo already has CI."
        return f"Opened a PR to add CI: {self.url}"


class Creator:
    def __init__(self, conn: sqlite3.Connection, workspace: Gh, secrets: RunnerSecrets) -> None:
        self.conn = conn
        self._ws = workspace
        self._secrets = secrets

    async def _gh(self, *args: str, stdin: bytes | None = None) -> Exec:
        return await self._ws.gh(args, self._secrets, stdin=stdin)

    async def _ok(self, what: str, *args: str, stdin: bytes | None = None) -> str:
        res = await self._gh(*args, stdin=stdin)
        if res.returncode != 0:
            raise CreateError(f"{what} failed: {res.stderr[-160:]}")
        return res.stdout.decode(errors="replace")

    async def _put_file(self, slug: str, path: str, data: bytes, branch: str, message: str) -> None:
        if path == CI_PATH and data != ci_template():
            # The one workflow file ever written, and only this exact content.
            raise CreateError("refusing to write a workflow other than the starter CI")
        content = base64.b64encode(data).decode()
        await self._ok(
            f"writing {path}",
            "api", "-X", "PUT", f"repos/{slug}/contents/{path}",
            "-f", f"message={message}", "-f", f"content={content}", "-f", f"branch={branch}",
        )  # fmt: skip

    # -- create --

    async def create_repo(
        self, name: str, description: str | None = None, *, private: bool = True
    ) -> Created:
        if (why := valid_repo_name(name)) is not None:
            raise CreateError(why[0].upper() + why[1:] + ".")
        description = (description or "").strip()
        if len(description) > DESCRIPTION_MAX:
            raise CreateError(f"The description is too long (at most {DESCRIPTION_MAX}).")
        ci_template()  # fail before creating anything if the file doesn't check out
        login = await self._ok("reading your GitHub account", "api", "user", "--jq", ".login")
        login = login.strip()
        if not login or parse_slug(f"{login}/{name}") is None:
            raise CreateError("Couldn't read your GitHub account name.")
        slug = f"{login}/{name}"
        if repos.get(self.conn, slug) is not None:
            raise CreateError(f"{slug} is already registered.")
        created = await self._gh(
            "repo", "create", name, "--private" if private else "--public",
            f"--description={description}",
        )  # fmt: skip
        if created.returncode != 0:
            raise CreateError(f"Couldn't create {slug}: {created.stderr[-160:]}")
        url = f"https://github.com/{slug}"
        try:
            info = json.loads(await self._ok("reading the new repo", "api", f"repos/{slug}"))
            slug, base = str(info["full_name"]), str(info["default_branch"])
            readme = f"# {name}\n\n{description}\n" if description else f"# {name}\n"
            await self._put_file(slug, "README.md", readme.encode(), base, "Initial commit")
            await self._put_file(slug, CI_PATH, ci_template(), base, "Add starter CI")
        except (CreateError, ValueError, KeyError, TypeError) as e:
            raise CreateError(
                f"Created {slug} but couldn't finish setting it up ({e}). "
                "Add it with add_repo once it's fixed."
            ) from None
        protection = await self.protect(slug, base)
        try:
            with atomic(self.conn):
                repo = repos.add(self.conn, slug, base=base, source="created", mode="push", ci=True)
        except RepoConflict:
            existing = repos.get(self.conn, slug)
            if existing is None:
                raise
            repo = existing
        return Created(repo, url, protection)

    async def protect(self, slug: str, base: str) -> str:
        """Require PRs and the CI check on ``base``; no force push or deletion. Returns a
        sentence for the owner. A refusal is not an error."""
        body = {
            "required_status_checks": {"strict": False, "contexts": [CI_CHECK]},
            "enforce_admins": True,
            "required_pull_request_reviews": {"required_approving_review_count": 0},
            "restrictions": None,
            "allow_force_pushes": False,
            "allow_deletions": False,
        }
        res = await self._gh(
            "api", "-X", "PUT", f"repos/{slug}/branches/{base}/protection", "--input", "-",
            stdin=json.dumps(body).encode(),
        )  # fmt: skip
        if res.returncode == 0:
            return f"{base} is protected: PRs and the CI check are required."
        if "HTTP 403" in res.stderr or "HTTP 404" in res.stderr or "Upgrade" in res.stderr:
            return f"{base} isn't protected: {PROTECTION_UNAVAILABLE}."
        return f"{base} isn't protected: GitHub refused ({res.stderr[-80:]})."

    # -- add CI --

    async def add_ci(self, key: str, github: GitHub) -> CiPr:
        repo = repos.find(self.conn, key)
        if repo is None:
            raise CreateError(f"No registered repo {key[:60]!r}.")
        if repo.mode != "push":
            raise CreateError(f"{repo.slug} is upstream; its maintainers own its CI.")
        if await self.refresh_ci(repo, github):
            return CiPr(None, None, already=True)
        template = ci_template()
        listed = await self._gh(
            "pr", "list", "--repo", repo.slug, "--head", CI_BRANCH, "--state", "open",
            "--json", "number,url",
        )  # fmt: skip
        if listed.returncode == 0:
            try:
                for item in json.loads(listed.stdout):
                    if _PR_URL.fullmatch(str(item["url"])):
                        return CiPr(int(item["number"]), str(item["url"]), already=False)
            except (ValueError, KeyError, TypeError):
                pass
        ref = f"repos/{repo.slug}/git/ref/heads/{repo.base}"
        sha = (await self._ok("reading the base branch", "api", ref, "--jq", ".object.sha")).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise CreateError("Couldn't read the base branch.")
        made = await self._gh(
            "api", "-X", "POST", f"repos/{repo.slug}/git/refs",
            "-f", f"ref=refs/heads/{CI_BRANCH}", "-f", f"sha={sha}",
        )  # fmt: skip
        if made.returncode != 0 and "already exists" not in made.stderr:
            raise CreateError(f"creating the branch failed: {made.stderr[-160:]}")
        await self._put_file(repo.slug, CI_PATH, template, CI_BRANCH, "Add starter CI")
        out = await self._ok(
            "opening the PR",
            "pr", "create", "--repo", repo.slug, "--head", CI_BRANCH, "--base", repo.base,
            "--title", "Add starter CI",
            "--body", PR_BODY,
        )  # fmt: skip
        m = _PR_URL.search(out)
        if m is None:
            raise CreateError("GitHub didn't print a PR URL.")
        return CiPr(int(m[1]), m[0], already=False)

    async def refresh_ci(self, repo: Repo, github: GitHub) -> bool:
        """Look at GitHub again and record whether the repo has a pull_request workflow now
        (so a merged add-CI PR is noticed). Returns the live answer."""
        try:
            info = await github.repo_info(repo.slug)
        except GitHubError as e:
            raise CreateError(f"Couldn't reach GitHub: {e}") from None
        if info is None:
            raise CreateError(f"No access to {repo.slug}.")
        with atomic(self.conn):
            repos.set_ci(self.conn, repo.slug, info.has_ci)
        return info.has_ci

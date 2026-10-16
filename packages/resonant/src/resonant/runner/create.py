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
import secrets as stdlib_secrets
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
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
README_PATH = "README.md"
CI_CHECK = "ci"  # the starter workflow's job name: the required status check
CI_BRANCH_PREFIX = "resonant/add-ci-"  # plus a short random id: never a reused branch
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


@dataclass(frozen=True)
class OpenCiPr:
    """An open add-CI PR as GitHub lists it (its diff is checked again at merge)."""

    number: int
    url: str
    branch: str
    head_sha: str


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

    async def put_file(self, slug: str, path: str, data: bytes, branch: str, message: str) -> None:
        # An allowlist, exact and case-sensitive: the README (any text) and the starter CI
        # workflow (exactly the template). Nothing else is ever written.
        if path == README_PATH:
            pass
        elif path == CI_PATH:
            if data != ci_template():
                raise CreateError("refusing to write a workflow other than the starter CI")
        else:
            raise CreateError(f"refusing to write {path!r}")
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
            "repo", "create", name, "--private" if private else "--public", "--add-readme",
            f"--description={description}",
        )  # fmt: skip
        if created.returncode != 0:
            raise CreateError(f"Couldn't create {slug}: {created.stderr[-160:]}")
        url = f"https://github.com/{slug}"
        try:
            info = json.loads(await self._ok("reading the new repo", "api", f"repos/{slug}"))
            slug, base = str(info["full_name"]), str(info["default_branch"])
            await self.put_file(slug, CI_PATH, ci_template(), base, "Add starter CI")
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
        err = res.stderr.lower()
        if "upgrade to github pro" in err or "not available" in err:
            return f"{base} isn't protected: {PROTECTION_UNAVAILABLE}."
        if "HTTP 403" in res.stderr or "HTTP 404" in res.stderr:
            return f"{base} isn't protected: the token lacks Administration permission."
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
            "pr", "list", "--repo", repo.slug, "--state", "open",
            "--json", "number,url,headRefName",
        )  # fmt: skip
        if listed.returncode == 0:
            try:
                for item in json.loads(listed.stdout):
                    if str(item["headRefName"]).startswith(CI_BRANCH_PREFIX) and _PR_URL.fullmatch(
                        str(item["url"])
                    ):
                        return CiPr(int(item["number"]), str(item["url"]), already=False)
            except (ValueError, KeyError, TypeError):
                pass
        ref = f"repos/{repo.slug}/git/ref/heads/{repo.base}"
        sha = (await self._ok("reading the base branch", "api", ref, "--jq", ".object.sha")).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise CreateError("Couldn't read the base branch.")
        branch = CI_BRANCH_PREFIX + stdlib_secrets.token_hex(4)
        await self._ok(
            "creating the branch",
            "api", "-X", "POST", f"repos/{repo.slug}/git/refs",
            "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}",
        )  # fmt: skip
        await self.put_file(repo.slug, CI_PATH, template, branch, "Add starter CI")
        out = await self._ok(
            "opening the PR",
            "pr", "create", "--repo", repo.slug, "--head", branch, "--base", repo.base,
            "--title", "Add starter CI",
            "--body", PR_BODY,
        )  # fmt: skip
        m = _PR_URL.search(out)
        if m is None:
            raise CreateError("GitHub didn't print a PR URL.")
        return CiPr(int(m[1]), m[0], already=False)

    async def find_ci_pr(self, key: str) -> tuple[Repo, OpenCiPr]:
        """The registered push repo ``key`` and its one open add-CI PR (head branch
        ``resonant/add-ci-…``, from the repo itself), for ``merge_ci_pr``. Raises
        ``CreateError`` with a sentence for the owner."""
        repo = repos.find(self.conn, key)
        if repo is None:
            raise CreateError(f"No registered repo {key[:60]!r}.")
        if repo.mode != "push":
            raise CreateError(f"{repo.slug} is upstream; its maintainers own its CI.")
        listed = await self._gh(
            "pr", "list", "--repo", repo.slug, "--state", "open",
            "--json", "number,url,headRefName,headRefOid,isCrossRepository",
        )  # fmt: skip
        if listed.returncode != 0:
            raise CreateError(f"Couldn't list {repo.slug}'s PRs: {listed.stderr[-120:]}")
        found: list[OpenCiPr] = []
        try:
            for item in json.loads(listed.stdout):
                branch, url = str(item["headRefName"]), str(item["url"])
                head, number = str(item["headRefOid"]), int(item["number"])
                if (
                    branch.startswith(CI_BRANCH_PREFIX)
                    and not item.get("isCrossRepository")
                    and _PR_URL.fullmatch(url)
                    and re.fullmatch(r"[0-9a-f]{40}", head)
                ):
                    found.append(OpenCiPr(number, url, branch, head))
        except (ValueError, KeyError, TypeError):
            raise CreateError(f"{repo.slug}'s PR list couldn't be read.") from None
        if not found:
            raise CreateError(f"{repo.slug} has no open add-CI PR. add_ci opens one.")
        if len(found) > 1:
            numbers = ", ".join(f"#{p.number}" for p in found[:3])
            raise CreateError(f"{repo.slug} has several add-CI PRs ({numbers}); merge one by hand.")
        return repo, found[0]

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


Run = Callable[[Sequence[str]], Awaitable[tuple[int, bytes]]]
"""Runs ``gh`` with the token and returns (exit code, stdout)."""


async def adds_only_starter_ci(run: Run, slug: str, pr_number: int, head_sha: str) -> bool:
    """Whether PR ``pr_number``'s whole diff is the starter CI file: exactly one changed file,
    :data:`CI_PATH`, whose content at ``head_sha`` hashes to :data:`CI_TEMPLATE_SHA256`. Fails
    closed on any doubt. Used to let that PR merge in a repo that has no CI yet."""
    if pr_number < 1 or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        return False
    code, out = await run(["api", f"repos/{slug}/pulls/{pr_number}", "--jq", ".head.sha"])
    if code != 0 or out.decode(errors="replace").strip() != head_sha:
        return False
    code, out = await run(["api", f"repos/{slug}/pulls/{pr_number}/files", "--paginate"])
    if code != 0:
        return False
    try:
        files = json.loads(out)
        if [(f["filename"], f["status"]) for f in files] not in (
            [(CI_PATH, "added")],
            [(CI_PATH, "modified")],
        ):
            return False
    except (ValueError, KeyError, TypeError):
        return False
    code, out = await run(
        ["api", f"repos/{slug}/contents/{CI_PATH}", "-X", "GET", "-f", f"ref={head_sha}",
         "-H", "Accept: application/vnd.github.raw"]
    )  # fmt: skip
    return code == 0 and hashlib.sha256(out).hexdigest() == CI_TEMPLATE_SHA256

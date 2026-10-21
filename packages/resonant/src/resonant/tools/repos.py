"""Repo tools (docs/repos-plan.md D6, D8): ``create_repo`` (L2), ``add_ci`` (L2),
``merge_ci_pr`` (L2), ``list_repos`` (L0), ``add_repo`` (L3) and ``forget_repo`` (L3).

Global builtins like the run tools: the gate lets only the owner call them, and the handlers
find ``ClaudeRunner`` (``components.runners["claude"]``) at call time. Replies are code-built
``text``. The ``run_project`` intent offers them all (EM-8), and the slash commands are
``/repos``, ``/repo add|forget|new|ci``. An L2 tool runs from its slash command and asks a
plain yes when the model chose it.

- ``create_repo{name, description?, private?}``: a repo under the owner's account (private by
  default) with a README, the starter CI workflow and branch protection, registered
  ``source=created`` and cloned for the owner like an add. L2: a model-chosen call asks a
  plain yes.
- ``add_ci{repo}``: a PR adding the starter CI workflow to a registered push repo without CI.
- ``merge_ci_pr{repo}``: squash-merge that repo's open add-CI PR. It isn't a run, so
  ``merge_run`` can't reach it. ``Workspace.merge(starter_ci=True)`` checks that the PR's
  whole diff is the hash-checked starter workflow, runs the pre-merge checks, and needs green
  CI on the PR (the starter workflow runs on the PR itself) and ``--match-head-commit``.
  Then the repo is recorded as having CI.
- ``list_repos``: the registry, most recently used first, with where each one's checkout is
  (``~/code/blog``) or ``not cloned``.
- ``add_repo{repo, name?}``: resolve (``owner/name``, URL, name, fuzzy) and register; ``name``
  is an extra handle for it. A new push repo whose base branch is unprotected comes with a
  warning. Without the daemon's GitHub token (GT-1) an explicit ``owner/name`` or URL is
  still registered when GitHub shows it to anyone (``PublicGitHub``, no credentials): as a
  push repo with its CI unknown. A private or missing one says the token is needed for it;
  a bare name (registry, extension link, fuzzy) needs the token unless it's registered.
  Then the owner gets a checkout (``Workspace.owner_checkout``): cloned to
  ``<runner.code_dir>/<name>`` (the daemon's token by credential helper, never in the clone's
  config), an existing checkout of the repo used as is, anything else there left alone. Adding a
  registered repo with no checkout again clones it. A fork repo isn't cloned (its checkout is
  the owner's fork, made by the first run), nor one GitHub says is over about 2 GB; a clone
  that fails or takes over 2 minutes leaves the repo registered, and the first run tries again.
  The reply says which, with the path.
- ``forget_repo{repo}``: drop the registry entry. Nothing on GitHub changes; refused while the
  repo has an active run.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Protocol, cast

from resonant.executor import ToolFailed, ToolHandler
from resonant.gate.types import Intent
from resonant.redact import Redactor
from resonant.runner.checks import GitError
from resonant.runner.create import (
    DESCRIPTION_MAX,
    NAME_MAX,
    CreateError,
    Creator,
    valid_repo_name,
)
from resonant.runner.resolve import (
    Ambiguous,
    GitHub,
    GitHubError,
    NeedsToken,
    NoAccess,
    NotFound,
    OfflineGitHub,
    PublicGitHub,
    Resolved,
    Resolver,
    parse_slug,
)
from resonant.runner.workspace import (
    CLONE_TIMEOUT_S,
    GITHUB_TOKEN_HOW,
    MergeRefused,
    OwnerCheckout,
    RunnerError,
    Workspace,
    needs_github_token,
    remote_slug,
)
from resonant.secrets import RunnerSecrets, SecretError, SecretStore, load_runner_secrets
from resonant.store import repos as repos_store
from resonant.store.audit import audit
from resonant.store.db import atomic
from resonant.store.repos import Repo, RepoConflict
from resonant.tools.base import BuiltinContext
from resonant_sdk import Effect, Level, ToolSpec

RUNNER = "claude"
NOT_CLONED = "not cloned"
REPO_MAX = 200
REPOS_MAX = 100  # repos listed
SIZE_LOOKUP_S = 10.0  # GitHub's answer on a repo's size; unknown after this


class RunnerApi(Protocol):
    """What these tools use of ``ClaudeRunner``."""

    workspace: Workspace
    resolver: Resolver

    async def setup_missing(self) -> str | None: ...

    secrets: SecretStore

    def github_for(self, secrets: RunnerSecrets) -> GitHub: ...


def not_public_text(slug: str) -> str:
    """An explicit repo GitHub doesn't show without a token, when the daemon has none."""
    return (
        f"{slug} isn't public (or doesn't exist). Registering a private repo needs the "
        f"daemon's GitHub token (runner/github_token), which isn't set yet. "
        f"On the Mini: {GITHUB_TOKEN_HOW}."
    )


def _repo_schema() -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": REPO_MAX}


CREATE_REPO = ToolSpec(
    name="create_repo",
    description=(
        "Create a new GitHub repo under the owner's account (private unless told otherwise) "
        "with a README, starter CI and branch protection, register it for Claude runs and "
        "clone it to the owner's code dir. Asks the owner first when chosen from chat."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": NAME_MAX},
            "description": {"type": "string", "maxLength": DESCRIPTION_MAX},
            "private": {"type": "boolean"},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=120.0 + CLONE_TIMEOUT_S + 20.0,  # create, then the clone
    approval_summary="create a GitHub repo named {name}",
)
ADD_CI = ToolSpec(
    name="add_ci",
    description=(
        "Open a pull request that adds starter CI to a registered repo that has none "
        "(needed before its runs can be merged). Asks the owner first when chosen from chat."
    ),
    args_schema={
        "type": "object",
        "properties": {"repo": _repo_schema()},
        "required": ["repo"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=120.0,
    approval_summary="add CI to {repo}",
)
MERGE_CI_PR = ToolSpec(
    name="merge_ci_pr",
    description=(
        "Squash-merge the open add-CI pull request (from add_ci) of a registered repo, once "
        "its CI is green. Asks the owner first when chosen from chat."
    ),
    args_schema={
        "type": "object",
        "properties": {"repo": _repo_schema()},
        "required": ["repo"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=180.0,
)
LIST_REPOS = ToolSpec(
    name="list_repos",
    description=(
        "The repos Claude runs can work in: name, owner/name, mode, whether it has CI, where "
        "it came from and where its checkout is on this Mac (path, or 'not cloned'). Read only."
    ),
    args_schema={"type": "object", "properties": {}, "additionalProperties": False},
    effect=Effect.READ,
    timeout_s=15.0,
)
ADD_REPO = ToolSpec(
    name="add_repo",
    description=(
        "Register a GitHub repo (owner/name, URL or a name to look up) for Claude runs and "
        "clone it to the owner's code dir (again for a registered repo with no checkout). "
        "`name` is an optional short handle for it."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "repo": _repo_schema(),
            "name": {"type": "string", "minLength": 1, "maxLength": 32},
        },
        "required": ["repo"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=60.0 + SIZE_LOOKUP_S + CLONE_TIMEOUT_S + 10.0,  # resolve, size, then the clone
    approval_summary="add the repo {repo} for runs",
)
FORGET_REPO = ToolSpec(
    name="forget_repo",
    description=(
        "Remove a repo from the list Claude runs can use. Nothing on GitHub changes; refused "
        "while the repo has an active run."
    ),
    args_schema={
        "type": "object",
        "properties": {"repo": _repo_schema()},
        "required": ["repo"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=15.0,
    approval_summary="forget the repo {repo}",
)

SPECS: tuple[ToolSpec, ...] = (
    CREATE_REPO, ADD_CI, MERGE_CI_PR, LIST_REPOS, ADD_REPO, FORGET_REPO,
)  # fmt: skip


def summary_action(intent: Intent) -> str | None:
    """The approval heading for a model-chosen ``merge_ci_pr``: ``merge the add-CI PR in
    blog``, from code (the repo word is cleaned like any arg)."""
    if intent.extension is not None or intent.tool != MERGE_CI_PR.name:
        return None
    repo = intent.args.get("repo")
    return f"merge the add-CI PR in {repo}" if isinstance(repo, str) and repo.strip() else None


def _unknown(tool: str, args: dict[str, Any], allowed: set[str]) -> None:
    if unknown := set(args) - allowed:
        raise ToolFailed(f"{tool}: unknown arguments: {sorted(unknown)}")


def _need_str(tool: str, args: dict[str, Any], key: str, limit: int) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolFailed(f"{tool}: {key} is a required string")
    value = value.strip()
    if len(value) > limit:
        raise ToolFailed(f"{tool}: {key} is too long (at most {limit} characters)")
    return value


def tilde(path: Path) -> str:
    """``~/code/blog`` for a path under the home dir, else the path."""
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def checkout_text(got: OwnerCheckout) -> str:
    """The reply's clause for what an add did about the owner's checkout."""
    where = tilde(got.path)
    match got.state:
        case "cloned":
            return f"cloned to {where}"
        case "present":
            return f"checked out at {where}"
        case "in_the_way":
            return f"not cloned: {got.reason}. I left it alone; runs can't use it until it moves"
        case "skipped":
            return f"not cloned yet: {got.reason} to {where}"
        case _:
            return f"the clone to {where} failed ({got.reason}); the first run will try again"


async def owner_checkout(
    claude: RunnerApi, repo: Repo, secrets: RunnerSecrets, github: GitHub | None
) -> OwnerCheckout:
    """Clone ``repo`` for the owner (``Workspace.owner_checkout``). With ``github``, first ask
    how big it is (at most ``SIZE_LOOKUP_S``; no answer is unknown). A repo just created is
    tiny, so ``create_repo`` passes None."""
    size = None
    if github is not None and repo.mode == "push":
        try:
            size = await asyncio.wait_for(github.size_kb(repo.slug), SIZE_LOOKUP_S)
        except Exception:  # never stops an add (a timeout included)
            size = None
    return await claude.workspace.owner_checkout(repo, secrets.github_token, size_kb=size)


def _checkout_fields(got: OwnerCheckout) -> dict[str, Any]:
    cloned = got.state in ("cloned", "present")
    return {"path": tilde(got.path) if cloned else None, "checkout": got.state}


def _origin_slug(git_dir: Path) -> str | None:
    """``owner/name`` of the ``origin`` in ``<git_dir>/config``, read as text: nothing in a
    checkout's config is run (a worker can write it), and a failure is None."""
    try:
        text = (git_dir / "config").read_text(errors="replace")[:65536]
    except OSError:
        return None
    section = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line.replace(" ", "").lower() == '[remote"origin"]'
        elif section and line.lower().startswith("url") and "=" in line:
            return remote_slug(line.split("=", 1)[1].strip())
    return None


def _listed_path(repo: Repo, code_dir: Path) -> str:
    """Where the repo's checkout is, or ``not cloned``. A recorded ``path`` (an add's or a run's
    clone, or the owner's own) counts when it has a ``.git``; the default place only when its
    ``origin`` is this repo, so something else in the way isn't shown as the checkout."""
    if repo.path is not None:
        return tilde(repo.path) if os.path.lexists(repo.path / ".git") else NOT_CLONED
    path = code_dir / repo.name
    origin = _origin_slug(path / ".git")
    return tilde(path) if origin is not None and origin.lower() == repo.slug.lower() else NOT_CLONED


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    def runner() -> RunnerApi | None:
        components = ctx.state.components
        found = components.runners.get(RUNNER) if components is not None else None
        return cast("RunnerApi | None", found)

    async def ready(what: str, *, token: bool = True) -> tuple[RunnerApi, RunnerSecrets] | str:
        """The runner and its secrets, or the sentence for why not. These tools never start a
        session, so they need the daemon's GitHub token (unless ``token`` is False: the caller
        copes without it) and not the Claude credential."""
        from resonant.runners.claude import not_set_up_text

        claude = runner()
        if claude is None:
            return not_set_up_text(
                ["the Claude runner isn't running"],
                ["set channels.imessage.enabled in ~/.resonant/config.yaml and restart Resonant"],
            )
        try:
            secrets = await load_runner_secrets(claude.secrets, claude=False)
        except SecretError:
            return "The runner's secrets can't be read."
        if token and secrets.github_token is None:
            return needs_github_token(what)
        return claude, secrets

    async def _create_repo(args: dict[str, Any]) -> dict[str, Any]:
        _unknown("create_repo", args, {"name", "description", "private"})
        name = _need_str("create_repo", args, "name", NAME_MAX)
        description = args.get("description")
        private = args.get("private", True)
        if description is not None and not isinstance(description, str):
            raise ToolFailed("create_repo: description must be a string")
        if not isinstance(private, bool):
            raise ToolFailed("create_repo: private must be true or false")
        if (why := valid_repo_name(name)) is not None:
            return {"created": False, "text": why[0].upper() + why[1:] + "."}
        got = await ready("Creating a repo")
        if isinstance(got, str):
            return {"created": False, "text": got}
        claude, secrets = got
        try:
            made = await Creator(ctx.conn, claude.workspace, secrets).create_repo(
                name, description, private=private
            )
        except CreateError as e:
            return {"created": False, "text": str(e)}
        got = await owner_checkout(claude, made.repo, secrets, None)
        clause = checkout_text(got)
        return {
            "created": True,
            "slug": made.repo.slug,
            "name": made.repo.name,
            "private": private,
            **_checkout_fields(got),
            "text": f"{made.text} {clause[0].upper()}{clause[1:]}.",
        }

    async def _add_ci(args: dict[str, Any]) -> dict[str, Any]:
        _unknown("add_ci", args, {"repo"})
        repo = _need_str("add_ci", args, "repo", REPO_MAX)
        got = await ready("Adding CI")
        if isinstance(got, str):
            return {"opened": False, "text": got}
        claude, secrets = got
        try:
            pr = await Creator(ctx.conn, claude.workspace, secrets).add_ci(
                repo, claude.github_for(secrets)
            )
        except CreateError as e:
            return {"opened": False, "text": str(e)}
        return {"opened": not pr.already, "pr_url": pr.url, "text": pr.text}

    async def _merge_ci_pr(args: dict[str, Any]) -> dict[str, Any]:
        _unknown("merge_ci_pr", args, {"repo"})
        key = _need_str("merge_ci_pr", args, "repo", REPO_MAX)
        got = await ready("Merging the add-CI PR")
        if isinstance(got, str):
            return {"merged": False, "text": got}
        claude, secrets = got
        try:
            repo, pr = await Creator(ctx.conn, claude.workspace, secrets).find_ci_pr(key)
        except CreateError as e:
            return {"merged": False, "text": str(e)}
        try:
            await claude.workspace.merge(
                repo.name, pr.number, pr.head_sha, secrets, branch=pr.branch, starter_ci=True
            )
        except (MergeRefused, RunnerError, GitError) as e:
            reason = e.message if isinstance(e, GitError) else str(e)
            reason = Redactor(secrets.values()).text(reason)
            return {
                "merged": False,
                "pr_url": pr.url,
                "text": f"Add-CI PR #{pr.number} in {repo.name} not merged: {reason}.",
            }
        with atomic(ctx.conn):
            repos_store.set_ci(ctx.conn, repo.slug, True)
            audit(ctx.conn, "repo.ci_merged", repo=repo.slug, pr=pr.number)
        return {
            "merged": True,
            "pr_url": pr.url,
            "text": f"Merged the add-CI PR #{pr.number} in {repo.name}: its runs can merge now."
            f"\n{pr.url}",
        }

    async def _list_repos(args: dict[str, Any]) -> dict[str, Any]:
        _unknown("list_repos", args, set())
        found = repos_store.list_all(ctx.conn)
        code_dir = ctx.state.settings.runner.code_dir
        shown = found[:REPOS_MAX]
        paths = await asyncio.to_thread(lambda: [_listed_path(r, code_dir) for r in shown])
        return {
            "repos": [
                {
                    "name": r.name,
                    "slug": r.slug,
                    "mode": r.mode,
                    "ci": r.ci,
                    "source": r.source,
                    "path": path,
                }
                for r, path in zip(shown, paths, strict=True)
            ],
            "count": len(found),
            "truncated": len(found) > REPOS_MAX,
        }

    async def _add_repo(args: dict[str, Any]) -> dict[str, Any]:
        _unknown("add_repo", args, {"repo", "name"})
        text = _need_str("add_repo", args, "repo", REPO_MAX)
        alias = args.get("name")
        if alias is not None and (not isinstance(alias, str) or not alias.strip()):
            raise ToolFailed("add_repo: name must be a non-empty string")
        if isinstance(alias, str):
            alias = alias.strip().lower()
            if not repos_store.NAME.fullmatch(alias):
                return {
                    "added": False,
                    "text": f"{alias[:40]!r} isn't a usable name (start with a letter; "
                    "letters, digits, '_' and '-', at most 32).",
                }
        got = await ready("Adding a repo", token=False)
        if isinstance(got, str):
            return {"added": False, "text": got}
        claude, secrets = got
        slug = parse_slug(text)
        anonymous = secrets.github_token is None
        github: GitHub
        if not anonymous:
            github = claude.github_for(secrets)
        elif slug is not None:
            github = PublicGitHub()  # GT-1: an explicit public repo needs no token
        else:
            github = OfflineGitHub()  # the registry still answers; a lookup needs the token
        try:
            resolution = await claude.resolver.resolve(text, github)
        except NeedsToken:
            return {"added": False, "text": needs_github_token("Adding a repo by name")}
        except GitHubError as e:
            return {"added": False, "text": f"Couldn't reach GitHub: {e}"}
        if anonymous and isinstance(resolution, NoAccess):
            return {"added": False, "text": not_public_text(resolution.slug)}
        if isinstance(resolution, (Ambiguous, NoAccess)):
            return {"added": False, "text": resolution.text}
        if isinstance(resolution, NotFound):
            return {"added": False, "text": f"No repo matches {text[:60]!r}."}
        assert isinstance(resolution, Resolved)
        repo = resolution.repo
        alias_error = None
        if isinstance(alias, str):
            try:
                repo = repos_store.add_alias(ctx.conn, repo.slug, alias)
            except (RepoConflict, ValueError) as e:
                alias_error = str(e)  # the repo is registered either way: say both
        got = await owner_checkout(claude, repo, secrets, github)
        repo = repos_store.get(ctx.conn, repo.slug) or repo  # its path, when just set
        via = f" via {resolution.via}" if resolution.via else ""
        ci = "" if repo.ci is None else ", CI" if repo.ci else ", no CI"
        lines = [
            f"{'Added' if resolution.new else 'Already have'} {repo.name} ({repo.slug}{via}, "
            f"base {repo.base}, {repo.mode}{ci}): {checkout_text(got)}."
        ]
        if alias_error:
            lines.append(f"Couldn't add the name: {alias_error}.")
        if resolution.warning:
            lines.append(f"Heads up: {resolution.warning}.")
        if anonymous and resolution.new:
            lines.append(
                "Registered without the daemon's GitHub token, so push access and CI weren't "
                "checked; runs will find out."
            )
        if repo.mode == "fork":
            lines.append("It's upstream: runs work in your fork and I ask before the PR there.")
        return {
            "added": True,
            "new": resolution.new,
            "slug": repo.slug,
            "name": repo.name,
            **_checkout_fields(got),
            "warning": resolution.warning,
            "via": resolution.via,
            "alias_error": alias_error,
            "text": " ".join(lines),
        }

    async def _forget_repo(args: dict[str, Any]) -> dict[str, Any]:
        _unknown("forget_repo", args, {"repo"})
        key = _need_str("forget_repo", args, "repo", REPO_MAX)
        repo = repos_store.find(ctx.conn, key)
        if repo is None:
            return {"forgot": False, "text": f"No registered repo {key[:60]!r}."}
        if open_runs := repos_store.open_runs(ctx.conn, repo.name):
            codes = ", ".join(open_runs[:3])
            return {"forgot": False, "text": f"{repo.slug} has active runs ({codes}); not removed."}
        with atomic(ctx.conn):
            repos_store.forget(ctx.conn, repo.slug)
        return {
            "forgot": True,
            "slug": repo.slug,
            "text": f"Forgot {repo.slug}. Nothing on GitHub changed.",
        }

    return {
        CREATE_REPO.name: _create_repo,
        ADD_CI.name: _add_ci,
        MERGE_CI_PR.name: _merge_ci_pr,
        LIST_REPOS.name: _list_repos,
        ADD_REPO.name: _add_repo,
        FORGET_REPO.name: _forget_repo,
    }

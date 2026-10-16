"""Repo tools (docs/repos-plan.md D6, D8): ``create_repo`` (L2), ``add_ci`` (L2),
``merge_ci_pr`` (L2), ``list_repos`` (L0), ``add_repo`` (L3) and ``forget_repo`` (L3).

Global builtins like the run tools: the gate lets only the owner call them, and the handlers
find ``ClaudeRunner`` (``components.runners["claude"]``) at call time. Replies are code-built
``text``. The ``run_project`` intent offers them all (EM-8), and the slash commands are
``/repos``, ``/repo add|forget|new|ci``. An L2 tool runs from its slash command and asks a
plain yes when the model chose it.

- ``create_repo{name, description?, private?}``: a repo under the owner's account (private by
  default) with a README, the starter CI workflow and branch protection, registered
  ``source=created``. L2: a model-chosen call asks a plain yes.
- ``add_ci{repo}``: a PR adding the starter CI workflow to a registered push repo without CI.
- ``merge_ci_pr{repo}``: squash-merge that repo's open add-CI PR. It isn't a run, so
  ``merge_run`` can't reach it. ``Workspace.merge(starter_ci=True)`` checks that the PR's
  whole diff is the hash-checked starter workflow, runs the pre-merge checks, and needs green
  CI on the PR (the starter workflow runs on the PR itself) and ``--match-head-commit``.
  Then the repo is recorded as having CI.
- ``list_repos``: the registry, most recently used first.
- ``add_repo{repo, name?}``: resolve (``owner/name``, URL, name, fuzzy) and register; ``name``
  is an extra handle for it. A new push repo whose base branch is unprotected comes with a
  warning.
- ``forget_repo{repo}``: drop the registry entry. Nothing on GitHub changes; refused while the
  repo has an active run.
"""

from __future__ import annotations

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
    NoAccess,
    NotFound,
    Resolved,
    Resolver,
)
from resonant.runner.workspace import MergeRefused, RunnerError, Workspace
from resonant.secrets import RunnerSecrets, SecretError, SecretStore, load_runner_secrets
from resonant.store import repos as repos_store
from resonant.store.audit import audit
from resonant.store.db import atomic
from resonant.store.repos import RepoConflict
from resonant.tools.base import BuiltinContext
from resonant_sdk import Effect, Level, ToolSpec

RUNNER = "claude"
REPO_MAX = 200
REPOS_MAX = 100  # repos listed


class RunnerApi(Protocol):
    """What these tools use of ``ClaudeRunner``."""

    workspace: Workspace
    resolver: Resolver

    async def setup_missing(self) -> str | None: ...

    secrets: SecretStore

    def github_for(self, secrets: RunnerSecrets) -> GitHub: ...


def _repo_schema() -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": REPO_MAX}


CREATE_REPO = ToolSpec(
    name="create_repo",
    description=(
        "Create a new GitHub repo under the owner's account (private unless told otherwise) "
        "with a README, starter CI and branch protection, and register it for Claude runs. "
        "Asks the owner first when chosen from chat."
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
    timeout_s=120.0,
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
        "The repos Claude runs can work in: name, owner/name, mode, whether it has CI and "
        "where it came from. Read only."
    ),
    args_schema={"type": "object", "properties": {}, "additionalProperties": False},
    effect=Effect.READ,
    timeout_s=15.0,
)
ADD_REPO = ToolSpec(
    name="add_repo",
    description=(
        "Register a GitHub repo (owner/name, URL or a name to look up) for Claude runs. "
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
    timeout_s=60.0,
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


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    def runner() -> RunnerApi | None:
        components = ctx.state.components
        found = components.runners.get(RUNNER) if components is not None else None
        return cast("RunnerApi | None", found)

    async def ready() -> tuple[RunnerApi, RunnerSecrets] | str:
        """The runner and its secrets, or the sentence for why not."""
        from resonant.runners.claude import not_set_up_text

        claude = runner()
        if claude is None:
            return not_set_up_text(
                ["the Claude runner isn't running"],
                ["set channels.imessage.enabled in ~/.resonant/config.yaml and restart Resonant"],
            )
        try:
            return claude, await load_runner_secrets(claude.secrets)
        except SecretError:
            return (await claude.setup_missing()) or "The runner's secrets can't be read."

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
        got = await ready()
        if isinstance(got, str):
            return {"created": False, "text": got}
        claude, secrets = got
        try:
            made = await Creator(ctx.conn, claude.workspace, secrets).create_repo(
                name, description, private=private
            )
        except CreateError as e:
            return {"created": False, "text": str(e)}
        return {
            "created": True,
            "slug": made.repo.slug,
            "name": made.repo.name,
            "private": private,
            "text": made.text,
        }

    async def _add_ci(args: dict[str, Any]) -> dict[str, Any]:
        _unknown("add_ci", args, {"repo"})
        repo = _need_str("add_ci", args, "repo", REPO_MAX)
        got = await ready()
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
        got = await ready()
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
        return {
            "repos": [
                {
                    "name": r.name,
                    "slug": r.slug,
                    "mode": r.mode,
                    "ci": r.ci,
                    "source": r.source,
                }
                for r in found[:REPOS_MAX]
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
        got = await ready()
        if isinstance(got, str):
            return {"added": False, "text": got}
        claude, secrets = got
        try:
            resolution = await claude.resolver.resolve(text, claude.github_for(secrets))
        except GitHubError as e:
            return {"added": False, "text": f"Couldn't reach GitHub: {e}"}
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
        via = f" via {resolution.via}" if resolution.via else ""
        lines = [
            f"{'Added' if resolution.new else 'Already have'} {repo.slug} as {repo.name}{via}"
            f" ({repo.mode}{'' if repo.ci is None else ', CI' if repo.ci else ', no CI'})."
        ]
        if alias_error:
            lines.append(f"Couldn't add the name: {alias_error}.")
        if resolution.warning:
            lines.append(f"Heads up: {resolution.warning}.")
        if repo.mode == "fork":
            lines.append("It's upstream: runs work in your fork and I ask before the PR there.")
        return {
            "added": True,
            "new": resolution.new,
            "slug": repo.slug,
            "name": repo.name,
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

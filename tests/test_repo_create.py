"""Creating repos, starter CI and the repo tools (RP-4), with a fake ``gh`` and fake GitHub."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from resonant.api import DaemonState
from resonant.components import Components, build_components
from resonant.config import RunnerConfig, Settings, load_settings
from resonant.loop import Loop, SlotPool
from resonant.runner import create
from resonant.runner.checks import runs_on_pull_request
from resonant.runner.create import CreateError, Creator, valid_repo_name
from resonant.runner.resolve import FakeGitHub, RepoInfo, Resolved, Resolver
from resonant.runner.workspace import Exec
from resonant.secrets import RunnerSecrets
from resonant.store import repos
from resonant.store import runs as runs_store
from resonant.store.db import utcnow

REPO = Path(__file__).resolve().parents[1]
SECRETS = RunnerSecrets("ghp_" + "a" * 36, "claude", None)
Reply = tuple[int, str, str]
Rule = Reply | Callable[[Sequence[str]], Reply]
SCRATCH = json.dumps({"full_name": "me/scratch", "default_branch": "main"})


class FakeGh:
    """``Workspace.gh``: records each call and answers from ``rules`` (longest prefix wins)."""

    def __init__(self, rules: dict[str, Rule] | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = []
        base: dict[str, Rule] = {
            "api user": (0, "me\n", ""),
            "repo create": (0, "https://github.com/me/scratch\n", ""),
            "api repos/me/scratch": (0, SCRATCH, ""),
            "api -X PUT repos/me/scratch/contents/": (0, "{}", ""),
            "api -X PUT repos/me/scratch/branches/main/protection": (0, "{}", ""),
        }
        self.rules = {**base, **(rules or {})}

    async def gh(
        self, args: Sequence[str], secrets: RunnerSecrets, *, stdin: bytes | None = None
    ) -> Exec:
        self.calls.append((tuple(args), stdin))
        line = " ".join(args)
        for prefix, reply in sorted(self.rules.items(), key=lambda kv: -len(kv[0])):
            if line.startswith(prefix):
                code, out, err = reply(args) if callable(reply) else reply
                return Exec(code, out.encode(), err)
        return Exec(1, b"", "HTTP 404 unexpected call")

    def lines(self) -> list[str]:
        return [" ".join(a) for a, _ in self.calls]


def puts(gh: FakeGh) -> list[dict[str, str]]:
    """The ``-f key=value`` fields and path of each contents PUT."""
    out: list[dict[str, str]] = []
    for args, _ in gh.calls:
        if "PUT" in args and any("/contents/" in a for a in args):
            fields = dict(a.split("=", 1) for a in args if "=" in a and not a.startswith("repos"))
            fields["path"] = next(a for a in args if "/contents/" in a).split("/contents/")[1]
            out.append(fields)
    return out


# --- the template ------------------------------------------------------------------------


def test_template_matches_its_checksum_and_runs_on_pull_request() -> None:
    data = create.CI_TEMPLATE_PATH.read_bytes()
    assert hashlib.sha256(data).hexdigest() == create.CI_TEMPLATE_SHA256
    text = data.decode()
    assert runs_on_pull_request(text)
    assert "./ci.sh" in text and "push:" in text
    for line in text.splitlines():
        if "uses:" in line:
            assert len(line.split("@")[1].split()[0]) == 40  # a full commit sha


def test_a_changed_template_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    changed = tmp_path / "ci.yml"
    changed.write_text(create.CI_TEMPLATE_PATH.read_text() + "# extra\n")
    monkeypatch.setattr(create, "CI_TEMPLATE_PATH", changed)
    with pytest.raises(CreateError, match="checksum"):
        create.ci_template()


@pytest.mark.parametrize("name", ["scratch", "a.b_c-d", "x" * 100, "..a", "a.git.b"])
def test_valid_names(name: str) -> None:
    assert valid_repo_name(name) is None


@pytest.mark.parametrize("name", ["", ".", "..", "x" * 101, "a b", "a/b", "-a", "a.git", "é"])
def test_invalid_names(name: str) -> None:
    assert valid_repo_name(name) is not None


# --- create_repo -------------------------------------------------------------------------


async def test_create_repo_pushes_readme_and_ci_protects_and_registers(
    db: sqlite3.Connection,
) -> None:
    gh = FakeGh()
    made = await Creator(db, gh, SECRETS).create_repo("scratch", "My scratch pad")
    assert made.repo.slug == "me/scratch"
    r = repos.get(db, "me/scratch")
    assert r is not None
    assert (r.source, r.mode, r.ci, r.base) == ("created", "push", True, "main")
    create_call = next(a for a, _ in gh.calls if a[:2] == ("repo", "create"))
    assert create_call == (
        "repo", "create", "scratch", "--private", "--add-readme", "--description=My scratch pad"
    )  # fmt: skip
    files = puts(gh)
    assert [f["path"] for f in files] == [create.CI_PATH]  # the README comes from gh itself
    assert base64.b64decode(files[0]["content"]) == create.CI_TEMPLATE_PATH.read_bytes()
    assert files[0]["branch"] == "main"
    protect = next((a, s) for a, s in gh.calls if a[-2:] == ("--input", "-"))
    body = json.loads(protect[1] or b"")
    assert body["required_status_checks"]["contexts"] == ["ci"]
    assert body["required_pull_request_reviews"] is not None
    assert body["allow_force_pushes"] is False and body["allow_deletions"] is False
    assert made.protection.startswith("main is protected")
    token = SECRETS.github_token
    assert token is not None
    assert not any(token in " ".join(a) for a, _ in gh.calls)  # never in argv


async def test_public_when_asked(db: sqlite3.Connection) -> None:
    gh = FakeGh()
    await Creator(db, gh, SECRETS).create_repo("scratch", private=False)
    assert any(line.startswith("repo create scratch --public") for line in gh.lines())


async def test_protection_refused_on_a_free_plan_is_reported_not_fatal(
    db: sqlite3.Connection,
) -> None:
    refusal: Reply = (1, "", "gh: Upgrade to GitHub Pro (HTTP 403)")
    gh = FakeGh({"api -X PUT repos/me/scratch/branches/main/protection": refusal})
    made = await Creator(db, gh, SECRETS).create_repo("scratch")
    assert "protection not available on this plan" in made.protection
    assert repos.get(db, "me/scratch") is not None


async def test_invalid_name_creates_nothing(db: sqlite3.Connection) -> None:
    gh = FakeGh()
    with pytest.raises(CreateError):
        await Creator(db, gh, SECRETS).create_repo("../evil")
    assert gh.calls == []


async def test_create_failure_and_partial_failure(db: sqlite3.Connection) -> None:
    exists = FakeGh({"repo create": (1, "", "Name already exists on this account")})
    with pytest.raises(CreateError, match="already exists"):
        await Creator(db, exists, SECRETS).create_repo("scratch")
    broken = FakeGh({"api -X PUT repos/me/scratch/contents/": (1, "", "HTTP 403 workflows")})
    with pytest.raises(CreateError, match="Created me/scratch but"):
        await Creator(db, broken, SECRETS).create_repo("scratch")
    assert repos.get(db, "me/scratch") is None  # not registered half-made


# --- add_ci ------------------------------------------------------------------------------

SHA = "a" * 40
PR = "https://github.com/me/proj/pull/7"


def add_proj(db: sqlite3.Connection) -> repos.Repo:
    return repos.add(db, "me/proj", base="main", source="owner", ci=False)


def ci_gh() -> FakeGh:
    return FakeGh(
        {
            "pr list": (0, "[]", ""),
            "api repos/me/proj/git/ref/heads/main": (0, SHA + "\n", ""),
            "api -X POST repos/me/proj/git/refs": (0, "{}", ""),
            "api -X PUT repos/me/proj/contents/": (0, "{}", ""),
            "pr create": (0, PR + "\n", ""),
        }
    )


def no_ci_github(has_ci: bool = False) -> FakeGitHub:
    return FakeGitHub([RepoInfo("me/proj", "main", False, True, has_ci)])


async def test_add_ci_opens_a_pr_with_only_the_template(db: sqlite3.Connection) -> None:
    add_proj(db)
    gh = ci_gh()
    pr = await Creator(db, gh, SECRETS).add_ci("proj", no_ci_github())
    assert (pr.number, pr.url, pr.already) == (7, PR, False)
    files = puts(gh)
    [file] = files
    assert file["path"] == create.CI_PATH and re.fullmatch(
        r"resonant/add-ci-[0-9a-f]{8}", file["branch"]
    )
    assert base64.b64decode(file["content"]) == create.CI_TEMPLATE_PATH.read_bytes()
    pr_create = next(a for a, _ in gh.calls if a[:2] == ("pr", "create"))
    assert file["branch"] in pr_create and "main" in pr_create
    again = ci_gh()
    await Creator(db, again, SECRETS).add_ci("proj", no_ci_github())
    assert puts(again)[0]["branch"] != file["branch"]  # never a reused branch


async def test_add_ci_reuses_an_open_pr(db: sqlite3.Connection) -> None:
    add_proj(db)
    gh = ci_gh()
    gh.rules["pr list"] = (
        0,
        json.dumps([{"number": 7, "url": PR, "headRefName": "resonant/add-ci-ab12cd34"}]),
        "",
    )
    pr = await Creator(db, gh, SECRETS).add_ci("proj", no_ci_github())
    assert pr.number == 7 and puts(gh) == []


async def test_add_ci_notices_ci_that_has_appeared(db: sqlite3.Connection) -> None:
    add_proj(db)
    gh = ci_gh()
    pr = await Creator(db, gh, SECRETS).add_ci("proj", no_ci_github(True))
    assert pr.already and gh.calls == []
    r = repos.get(db, "me/proj")
    assert r is not None and r.ci is True


async def test_add_ci_refuses_fork_and_unknown_repos(db: sqlite3.Connection) -> None:
    repos.add(db, "up/stream", base="main", source="auto", mode="fork")
    creator = Creator(db, ci_gh(), SECRETS)
    with pytest.raises(CreateError, match="upstream"):
        await creator.add_ci("stream", FakeGitHub())
    with pytest.raises(CreateError, match="No registered repo"):
        await creator.add_ci("nope", FakeGitHub())


# --- the protection warning on registration ----------------------------------------------


async def test_registering_an_unprotected_push_repo_warns(db: sqlite3.Connection) -> None:
    gh = FakeGitHub([RepoInfo("me/proj", "main", False, True, True)], protected=False)
    got = await Resolver(db).resolve("me/proj", gh)
    assert isinstance(got, Resolved) and got.warning == "main isn't protected"
    assert "is_protected me/proj main" in gh.calls
    again = await Resolver(db).resolve("me/proj", gh)
    assert isinstance(again, Resolved) and again.warning is None


@pytest.mark.parametrize(("protected", "push"), [(True, True), (None, True), (False, False)])
async def test_protected_unknown_and_fork_repos_do_not_warn(
    db: sqlite3.Connection, protected: bool | None, push: bool
) -> None:
    gh = FakeGitHub([RepoInfo("me/a", "main", False, push, True)], protected=protected)
    got = await Resolver(db).resolve("me/a", gh)
    assert isinstance(got, Resolved) and got.warning is None


# --- tools -------------------------------------------------------------------------------


class FakeClaude:
    def __init__(self, ws: FakeGh, github: FakeGitHub, resolver: Resolver) -> None:
        self.workspace = ws
        self.secrets = object()
        self.github = github
        self.resolver = resolver
        self.secrets_value = SECRETS

    async def setup_missing(self) -> str | None:
        return None

    def github_for(self, secrets: RunnerSecrets) -> FakeGitHub:
        return self.github


@pytest.fixture
def settings() -> Settings:
    base = load_settings()
    return base.model_copy(update={"runner": RunnerConfig(), "dry_run": False})


@pytest.fixture
def components(settings: Settings, db: sqlite3.Connection) -> Iterator[Components]:
    shutil.copy(REPO / "config/principals.example.yaml", settings.principals_path)
    state = DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)), utcnow())
    state.components = build_components(settings, db, state)
    yield state.components


@pytest.fixture
def claude(
    components: Components, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> FakeClaude:
    async def fake_secrets(store: object, *, claude: bool = True) -> RunnerSecrets:
        assert claude is False  # the repo tools never start a session
        return fake.secrets_value

    monkeypatch.setattr("resonant.tools.repos.load_runner_secrets", fake_secrets)
    github = FakeGitHub([RepoInfo("me/proj", "main", False, True, True)])
    fake = FakeClaude(FakeGh(), github, Resolver(db))
    components.runners["claude"] = cast(Any, fake)
    return fake


async def call(components: Components, name: str, args: dict[str, Any]) -> dict[str, Any]:
    data = await components.handlers[(None, name)](args)
    json.dumps(data, allow_nan=False)
    return data


def test_levels(components: Components) -> None:
    expected = {
        "create_repo": "L2",
        "add_ci": "L2",
        "merge_ci_pr": "L2",
        "list_repos": "L0",
        "add_repo": "L3",
        "forget_repo": "L3",
    }
    for name, level in expected.items():
        spec = components.registry.get(None, name)
        assert spec is not None and spec.effective_level.name == level


async def test_create_repo_tool(components: Components, claude: FakeClaude) -> None:
    out = await call(components, "create_repo", {"name": "scratch", "description": "d"})
    assert out["created"] is True and out["slug"] == "me/scratch" and out["private"] is True
    assert out["text"].startswith("Created me/scratch")
    bad = await call(components, "create_repo", {"name": "a b"})
    assert bad["created"] is False and "letters" in bad["text"]


async def test_repo_tools_without_the_daemon_token_say_what_they_need(
    components: Components, claude: FakeClaude
) -> None:
    """RX-1: the GitHub repo tools need runner/github_token (not the Claude credential), and
    each one says so on its own instead of calling the whole runner not set up."""
    claude.secrets_value = RunnerSecrets(None, None, None)
    for tool, args, key, what in (
        ("create_repo", {"name": "scratch"}, "created", "Creating a repo"),
        ("add_ci", {"repo": "proj"}, "opened", "Adding CI"),
        ("merge_ci_pr", {"repo": "proj"}, "merged", "Merging the add-CI PR"),
        ("add_repo", {"repo": "me/proj"}, "added", "Adding a repo"),
    ):
        out = await call(components, tool, args)
        assert out[key] is False, tool
        assert out["text"].startswith(f"{what} needs the daemon's GitHub token"), out
        assert "resonant secret set runner/github_token" in out["text"]
        assert "isn't set up" not in out["text"]
    assert claude.workspace.calls == []  # nothing reached gh


async def test_tools_without_a_runner(components: Components) -> None:
    out = await call(components, "create_repo", {"name": "x"})
    assert out["created"] is False and "isn't running" in out["text"]


async def test_add_repo_list_and_forget(
    components: Components, claude: FakeClaude, db: sqlite3.Connection
) -> None:
    claude.github.protected = False
    out = await call(components, "add_repo", {"repo": "me/proj", "name": "p"})
    assert out["added"] and out["new"] and out["name"] == "proj"
    assert out["warning"] == "main isn't protected" and "Heads up" in out["text"]
    listed = await call(components, "list_repos", {})
    assert listed["repos"] == [
        {"name": "proj", "slug": "me/proj", "mode": "push", "ci": True, "source": "auto"}
    ]
    assert repos.find(db, "p") is not None  # the alias works
    missing = await call(components, "add_repo", {"repo": "me/none"})
    assert missing == {"added": False, "text": "No access to me/none."}

    run = runs_store.create(
        db, repo="proj", ask="x", principal="owner", session_id="s", max_seconds=60
    )
    refused = await call(components, "forget_repo", {"repo": "p"})
    assert refused["forgot"] is False and run.code in refused["text"]
    runs_store.update(db, run.code, status="failed")
    done = await call(components, "forget_repo", {"repo": "p"})
    assert done["forgot"] is True and repos.list_all(db) == []
    assert (await call(components, "forget_repo", {"repo": "p"}))["forgot"] is False


async def test_add_ci_tool(
    components: Components, claude: FakeClaude, db: sqlite3.Connection
) -> None:
    add_proj(db)
    claude.workspace.rules.update(ci_gh().rules)
    claude.github.known["me/proj"] = RepoInfo("me/proj", "main", False, True, False)
    out = await call(components, "add_ci", {"repo": "proj"})
    assert out["opened"] is True and out["pr_url"] == PR
    again = await call(components, "add_ci", {"repo": "nope"})
    assert again["opened"] is False


# --- hardening (review of #68) -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/x.yml",
        ".github/workflows/Resonant-CI.yml",
        ".GitHub/workflows/resonant-ci.yml",
        "readme.md",
        "src/README.md",
        "",
    ],
)
async def testput_file_is_an_allowlist(db: sqlite3.Connection, path: str) -> None:
    gh = FakeGh()
    with pytest.raises(CreateError, match="refusing to write"):
        await Creator(db, gh, SECRETS).put_file("me/x", path, b"on: push\n", "main", "m")
    assert gh.calls == []


async def testput_file_allows_only_the_exact_template_and_a_readme(db: sqlite3.Connection) -> None:
    gh = FakeGh({"api -X PUT repos/me/x/contents/": (0, "{}", "")})
    creator = Creator(db, gh, SECRETS)
    with pytest.raises(CreateError, match="other than the starter CI"):
        await creator.put_file("me/x", create.CI_PATH, b"on: push\n", "main", "m")
    assert gh.calls == []
    await creator.put_file("me/x", "README.md", b"# any text\n", "main", "m")
    await creator.put_file("me/x", create.CI_PATH, create.ci_template(), "main", "m")
    assert [f["path"] for f in puts(gh)] == ["README.md", create.CI_PATH]


async def test_a_description_starting_with_a_dash_is_inert(db: sqlite3.Connection) -> None:
    gh = FakeGh()
    await Creator(db, gh, SECRETS).create_repo("scratch", "--delete-me")
    call = next(a for a, _ in gh.calls if a[:2] == ("repo", "create"))
    assert "--description=--delete-me" in call and "--delete-me" not in call


@pytest.mark.parametrize(
    ("stderr", "expect"),
    [
        ("gh: Upgrade to GitHub Pro or make this repository public (HTTP 403)", "this plan"),
        ("Branch protection is not available for this repository (HTTP 403)", "this plan"),
        ("Resource not accessible by personal access token (HTTP 403)", "lacks Administration"),
    ],
)
async def test_protection_403_says_plan_or_permission(
    db: sqlite3.Connection, stderr: str, expect: str
) -> None:
    gh = FakeGh({"api -X PUT repos/me/scratch/branches/main/protection": (1, "", stderr)})
    made = await Creator(db, gh, SECRETS).create_repo("scratch")
    assert expect in made.protection


async def test_add_repo_alias_is_validated_first_and_failure_is_not_added_false(
    components: Components, claude: FakeClaude, db: sqlite3.Connection
) -> None:
    bad = await call(components, "add_repo", {"repo": "me/proj", "name": "9 bad!"})
    assert bad["added"] is False and repos.list_all(db) == []  # nothing registered
    repos.add(db, "me/other", base="main", source="owner", name="taken")
    out = await call(components, "add_repo", {"repo": "me/proj", "name": "taken"})
    assert out["added"] is True and out["alias_error"] and "Couldn't add the name" in out["text"]
    assert repos.get(db, "me/proj") is not None


# --- merging the add-CI PR ---------------------------------------------------------------

HEAD = "b" * 40


def pr_run(
    *, files: list[tuple[str, str]] | None = None, content: bytes | None = None, head: str = HEAD
) -> Callable[[Sequence[str]], Any]:
    files = files if files is not None else [(create.CI_PATH, "added")]
    body = content if content is not None else create.CI_TEMPLATE_PATH.read_bytes()

    async def run(args: Sequence[str]) -> tuple[int, bytes]:
        line = " ".join(args)
        if line.endswith(".head.sha"):
            return 0, head.encode() + b"\n"
        if "/files" in line:
            return 0, json.dumps([{"filename": n, "status": st} for n, st in files]).encode()
        if "/contents/" in line:
            assert f"ref={HEAD}" in line  # the content is read at the PR head
            return 0, body
        return 1, b""

    return run


async def test_only_the_starter_ci_pr_qualifies() -> None:
    ok = create.adds_only_starter_ci
    assert await ok(pr_run(), "me/proj", 7, HEAD) is True
    extra = pr_run(files=[(create.CI_PATH, "added"), ("src/app.py", "added")])
    assert await ok(extra, "me/proj", 7, HEAD) is False
    other = pr_run(files=[(".github/workflows/other.yml", "added")])
    assert await ok(other, "me/proj", 7, HEAD) is False
    assert await ok(pr_run(content=b"on: push\n"), "me/proj", 7, HEAD) is False
    assert await ok(pr_run(head="c" * 40), "me/proj", 7, HEAD) is False  # head moved
    assert await ok(pr_run(), "me/proj", 7, "short") is False


# --- merge_ci_pr (EM-8) --------------------------------------------------------------------

CI_BRANCH = "resonant/add-ci-0badc0de"


class MergingGh(FakeGh):
    """``FakeGh`` plus ``Workspace.merge``, recording the call (its checks are tested in
    ``test_runner_workspace.py``)."""

    def __init__(self, rules: dict[str, Rule] | None = None) -> None:
        super().__init__(rules)
        self.merges: list[dict[str, Any]] = []
        self.refuse: str | None = None

    async def merge(self, repo_name: str, pr_number: int, head_sha: str, secrets: Any,
                    **kw: Any) -> object:  # fmt: skip
        from resonant.runner.workspace import MergeRefused

        self.merges.append({"repo": repo_name, "pr": pr_number, "head": head_sha, **kw})
        if self.refuse:
            raise MergeRefused(self.refuse)
        return object()


def ci_prs(*branches: str) -> Reply:
    items = [
        {"number": 7 + i, "url": f"https://github.com/me/proj/pull/{7 + i}", "headRefName": b,
         "headRefOid": SHA, "isCrossRepository": False}
        for i, b in enumerate(branches)
    ]  # fmt: skip
    return (0, json.dumps(items), "")


async def test_merge_ci_pr_tool(
    components: Components, claude: FakeClaude, db: sqlite3.Connection
) -> None:
    add_proj(db)
    ws = MergingGh({"pr list": ci_prs("resonant/r3-fix", CI_BRANCH)})
    claude.workspace = cast(Any, ws)
    out = await call(components, "merge_ci_pr", {"repo": "proj"})
    assert out["merged"] is True and "PR #8" in out["text"] and out["pr_url"].endswith("/8")
    assert ws.merges == [
        {"repo": "proj", "pr": 8, "head": SHA, "branch": CI_BRANCH, "starter_ci": True}
    ]
    repo = repos.find(db, "proj")
    assert repo is not None and repo.ci is True

    repos.set_ci(db, "me/proj", False)
    ws.refuse = "CI is still running"
    out = await call(components, "merge_ci_pr", {"repo": "proj"})
    assert out == {
        "merged": False,
        "pr_url": "https://github.com/me/proj/pull/8",
        "text": "Add-CI PR #8 in proj not merged: CI is still running.",
    }
    repo = repos.find(db, "proj")
    assert repo is not None and repo.ci is False


async def test_merge_ci_pr_needs_exactly_one_own_add_ci_pr(
    components: Components, claude: FakeClaude, db: sqlite3.Connection
) -> None:
    add_proj(db)
    repos.add(db, "up/lib", base="main", source="auto", mode="fork")
    ws = MergingGh({"pr list": ci_prs("resonant/r3-fix")})
    claude.workspace = cast(Any, ws)
    none = await call(components, "merge_ci_pr", {"repo": "proj"})
    assert none["merged"] is False and "no open add-CI PR" in none["text"]
    ws.rules["pr list"] = ci_prs(CI_BRANCH, CI_BRANCH + "x")
    two = await call(components, "merge_ci_pr", {"repo": "proj"})
    assert "several add-CI PRs (#7, #8)" in two["text"]
    cross = json.dumps([{"number": 9, "url": "https://github.com/me/proj/pull/9",
                         "headRefName": CI_BRANCH, "headRefOid": SHA,
                         "isCrossRepository": True}])  # fmt: skip
    ws.rules["pr list"] = (0, cross, "")
    assert "no open add-CI PR" in (await call(components, "merge_ci_pr", {"repo": "proj"}))["text"]
    fork = await call(components, "merge_ci_pr", {"repo": "lib"})
    assert fork["merged"] is False and "upstream" in fork["text"]
    unknown = await call(components, "merge_ci_pr", {"repo": "nope"})
    assert unknown["merged"] is False and "No registered repo" in unknown["text"]
    assert ws.merges == []


def test_merge_ci_pr_approval_heading(components: Components) -> None:
    from resonant.gate.types import Intent
    from resonant.tools import repos as repos_tools

    spec = components.registry.get(None, "merge_ci_pr")
    assert spec is not None
    intent = Intent.for_tool(spec, task_id="t", principal="owner", args={"repo": "blog"})
    assert repos_tools.summary_action(intent) == "merge the add-CI PR in blog"
    other = Intent.for_tool(spec.model_copy(update={"name": "add_ci"}), task_id="t",
                            principal="owner", args={"repo": "blog"})  # fmt: skip
    assert repos_tools.summary_action(other) is None


async def test_add_repo_says_which_extension_linked_it(
    components: Components, claude: FakeClaude, db: sqlite3.Connection
) -> None:
    from datetime import UTC, datetime

    from resonant.store import extensions as ext_store

    ext_store.save_extension(
        db,
        ext_store.ExtensionRecord(
            name="trading", source="repo:trading", commit=None, tree_digest="d",
            manifest_sha="m", manifest={}, installed_at=datetime(2026, 1, 1, tzinfo=UTC),
            installed_by="owner", status="enabled",
        ),
    )  # fmt: skip
    repos.set_links(db, "trading", ["me/proj"])
    out = await call(components, "add_repo", {"repo": "trading"})
    assert out["added"] and out["via"] == "trading"
    assert out["text"].startswith("Added me/proj as proj via trading (push, CI).")

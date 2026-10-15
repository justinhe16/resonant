"""The github extension (P3-9, P3-15) against a fake ``gh``: never the real one, never GitHub.

Each test runs ``extensions/github/cli.py`` the way Resonant does (``python -I -S``, an env
built from scratch) with a ``gh`` shim on a temp PATH that answers from a JSON state file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from resonant.cli import app
from resonant.extensions.checks import dir_errors, name_errors, resolve_argv0
from resonant_sdk import Effect, Level, load_manifest

REPO = Path(__file__).resolve().parents[1]
EXT = REPO / "extensions/github"
CLI = EXT / "cli.py"
FAKE_GH = REPO / "tests/fixtures/fake_gh.py"
TOKEN = "sentinel-token-not-real-0123456789"
RESONANT = "justinhe16/resonant"


@dataclass
class Result:
    code: int
    out: dict[str, Any]
    err: str
    stdout: str
    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


class FakeGh:
    """A temp PATH with a ``gh`` shim, its state file, and the extension's data dir."""

    def __init__(self, root: Path) -> None:
        self.bin = root / "bin"
        self.bin.mkdir()
        self.data = root / "data"
        self.data.mkdir()
        self.state_path = root / "gh" / "state.json"
        self.state_path.parent.mkdir()
        self.state: dict[str, Any] = {"token": TOKEN, "mine": [], "review": [], "runs": {}}
        shim = self.bin / "gh"
        shim.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" -I -S "{FAKE_GH}" "{self.state_path}" "$@"\n'
        )
        shim.chmod(0o755)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": str(self.bin),
            "HOME": str(self.data),
            "LANG": "en_US.UTF-8",
            "RESONANT_EXT": "github",
            "RESONANT_EXT_DATA": str(self.data),
            "PARENT_SENTINEL": "must-not-reach-gh",
        }
        return {**env, **extra}

    def run(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        deliver: bool = True,
    ) -> Result:
        """Run the CLI like Resonant does: secrets on a pipe at fd 4, never in env or argv."""
        self.state_path.write_text(json.dumps(self.state))
        env = dict(self.env() if env is None else env)
        r, w = os.pipe()
        if deliver:
            os.write(w, json.dumps({"token": TOKEN} if secrets is None else secrets).encode())
            env["RESONANT_SECRETS_FD"] = str(r)
        os.close(w)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-S", str(CLI), *args],
                env=env,
                cwd=EXT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                pass_fds=(r,),
            )
        finally:
            os.close(r)
        out = cast(dict[str, Any], json.loads(proc.stdout)) if proc.returncode == 0 else {}
        events = cast(list[dict[str, Any]], out.get("events", []))
        return Result(proc.returncode, out, proc.stderr, proc.stdout, events)

    def calls(self) -> list[dict[str, Any]]:
        path = self.state_path.parent / "calls.jsonl"
        if not path.exists():
            return []
        return [cast(dict[str, Any], json.loads(line)) for line in path.read_text().splitlines()]


@pytest.fixture
def gh(tmp_path: Path) -> FakeGh:
    return FakeGh(tmp_path)


def check(name: str, *, conclusion: str | None = "SUCCESS", status: str = "COMPLETED") -> Any:
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def pr_node(
    number: int,
    sha: str,
    *,
    repo: str = RESONANT,
    title: str = "A change",
    contexts: list[Any] | None = None,
    state: str | None = "SUCCESS",
) -> dict[str, Any]:
    rollup = None if contexts is None else {"state": state, "contexts": {"nodes": contexts}}
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/{repo}/pull/{number}",
        "isDraft": False,
        "createdAt": "2026-10-10T00:00:00Z",
        "updatedAt": "2026-10-13T00:00:00Z",
        "headRefName": f"branch-{number}",
        "headRefOid": sha,
        "reviewDecision": "REVIEW_REQUIRED",
        "repository": {"nameWithOwner": repo},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]},
    }


def run_row(
    run_id: int,
    sha: str,
    conclusion: str | None,
    *,
    workflow: str = "ci",
    status: str = "completed",
) -> dict[str, Any]:
    return {
        "databaseId": run_id,
        "workflowName": workflow,
        "status": status,
        "conclusion": conclusion or "",
        "headSha": sha,
        "createdAt": f"2026-10-14T00:{run_id:02d}:00Z",
    }


def keys(events: list[dict[str, Any]]) -> list[str]:
    return [e["dedupe_key"] for e in events]


# --- manifest ---------------------------------------------------------------------------


def test_manifest_passes_every_check() -> None:
    m = load_manifest(EXT / "resonant.yaml")
    assert dir_errors(EXT, m) == [] and name_errors(m) == []
    specs = {s.name: s for s in m.tool_specs() + m.job_specs()}
    assert set(specs) == {"prs", "pr", "rerun_failed", "merge_pr", "job_watch", "job_digest"}
    for s in specs.values():
        assert s.secrets == ("token",)
        if s.name not in ("rerun_failed", "merge_pr"):
            assert s.effect is Effect.READ and s.effective_level is Level.L0 and not s.commit
    rerun, merge = specs["rerun_failed"], specs["merge_pr"]
    assert rerun.effect is Effect.WRITE and rerun.effective_level is Level.L3
    assert not rerun.commit
    # Destructive: L1 and commit, so only "yes <code>" approves it.
    assert merge.effect is Effect.WRITE and merge.effective_level is Level.L1 and merge.commit
    # The approval binds the head SHA: it's a required arg, not something looked up later.
    assert merge.args_schema["required"] == ["head_sha", "number", "repo"]
    assert rerun.args_schema["required"] == ["number", "repo"]
    assert {j.id: j.schedule for j in m.jobs} == {"watch": "*/10 * * * *", "digest": "50 7 * * *"}
    assert m.notify is not None and m.notify.on == ("ci_failed", "ci_fixed", "review_requested")
    assert m.health is not None and m.health.check == "python3 cli.py health"
    assert m.secrets == ("token",) and m.description and m.examples
    # The digest reports an item for the briefing: low, so it can never text on its own.
    assert m.report is not None and m.report.max_urgency == "low"
    # Every command runs cli.py under the daemon interpreter.
    assert resolve_argv0(EXT, m.tools["prs"].run) == sys.executable


def test_ext_validate_cli_passes() -> None:
    res = CliRunner().invoke(app, ["ext", "validate", str(EXT)])
    assert res.exit_code == 0, res.output
    assert "github v1" in res.output and "job  watch" in res.output


# --- prs and pr -------------------------------------------------------------------------


def test_prs_shape_ci_states_and_order(gh: FakeGh) -> None:
    gh.state["mine"] = [
        pr_node(41, "b" * 40, contexts=[check("lint", conclusion="FAILURE"), check("test")]),
        pr_node(7, "a" * 40, repo="justinhe16/other", contexts=[check("ci")]),
        pr_node(40, "c" * 40, contexts=[check("test", conclusion=None, status="IN_PROGRESS")]),
        pr_node(
            42,
            "d" * 40,
            contexts=[{"__typename": "StatusContext", "context": "deploy", "state": "ERROR"}],
        ),
        pr_node(43, "e" * 40, contexts=None, title="x" * 200),
    ]
    gh.state["review"] = [pr_node(9, "f" * 40, repo="someone/else")]
    r = gh.run("prs")
    assert r.code == 0, r.err
    prs = r.out["prs"]
    assert [(p["repo"], p["number"]) for p in prs] == [
        ("justinhe16/other", 7),
        (RESONANT, 40),
        (RESONANT, 41),
        (RESONANT, 42),
        (RESONANT, 43),
    ]
    by_n = {p["number"]: p for p in prs}
    assert by_n[7]["ci"] == "passing" and by_n[7]["failing"] == []
    assert by_n[40]["ci"] == "pending"
    assert by_n[41]["ci"] == "failing" and by_n[41]["failing"] == ["lint"]
    assert by_n[42]["ci"] == "failing" and by_n[42]["failing"] == ["deploy"]
    assert by_n[43]["ci"] == "none" and len(by_n[43]["title"]) == 80
    assert by_n[41]["review"] == "review_required" and by_n[41]["head_sha"] == "b" * 40
    assert set(by_n[41]) == {
        "repo", "number", "title", "url", "draft", "branch", "head_sha", "ci", "failing",
        "review", "created_at", "updated_at",
    }  # fmt: skip
    assert [p["number"] for p in r.out["review_requested"]] == [9]
    assert r.out["summary"] == "5 open PRs, 2 failing CI, 1 awaiting your review"
    assert r.out["truncated"] is False
    assert "events" not in r.out  # a read tool emits nothing


def test_search_uses_config_author_and_repos(gh: FakeGh) -> None:
    gh.run("prs")
    (call,) = gh.calls()
    assert call["argv"][:2] == ["api", "graphql"]
    assert "mine=is:pr is:open archived:false author:@me" in call["argv"]
    assert "review=is:pr is:open archived:false review-requested:@me" in call["argv"]

    (gh.data / "config.json").write_text(
        json.dumps({"author": "justinhe16", "repos": ["b/two", "a/one"]})
    )
    gh.run("prs")
    argv = gh.calls()[-1]["argv"]
    assert "mine=is:pr is:open archived:false repo:a/one repo:b/two author:justinhe16" in argv


@pytest.mark.parametrize(
    "config",
    ["not json", '["a"]', '{"author": "-rm"}', '{"repos": ["no-slash"]}', '{"extra": 1}'],
)
def test_bad_config_fails_cleanly(gh: FakeGh, config: str) -> None:
    (gh.data / "config.json").write_text(config)
    r = gh.run("prs")
    assert r.code == 1 and r.stdout == "" and r.err.startswith("github: config.json")
    assert gh.calls() == []


def test_pr_shape(gh: FakeGh) -> None:
    gh.state["prs"] = {
        f"{RESONANT}#40": {
            "number": 40,
            "title": "Add github",
            "url": "https://github.com/justinhe16/resonant/pull/40",
            "state": "OPEN",
            "isDraft": False,
            "author": {"login": "justinhe16"},
            "headRefName": "p3-9",
            "headRefOid": "a" * 40,
            "baseRefName": "main",
            "reviewDecision": "APPROVED",
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [check("test"), check("lint", conclusion="FAILURE")],
            "additions": 10,
            "deletions": 2,
            "changedFiles": 3,
            "createdAt": "2026-10-10T00:00:00Z",
            "updatedAt": "2026-10-13T00:00:00Z",
        }
    }
    r = gh.run("pr", "--repo", RESONANT, "--number", "40")
    assert r.code == 0, r.err
    assert r.out["state"] == "open" and r.out["review"] == "approved"
    assert r.out["mergeable"] == "mergeable" and r.out["author"] == "justinhe16"
    assert r.out["ci"] == "failing" and r.out["failing"] == ["lint"]
    assert r.out["checks"] == [
        {"name": "test", "state": "success"},
        {"name": "lint", "state": "failure"},
    ]
    argv = gh.calls()[0]["argv"]
    assert argv[:5] == ["pr", "view", "40", "--repo", RESONANT]


def test_pr_bad_args_and_not_found(gh: FakeGh) -> None:
    bad = gh.run("pr", "--repo", "not a repo", "--number", "1")
    assert bad.code == 1 and "owner/name" in bad.err and gh.calls() == []
    neg = gh.run("pr", "--repo", RESONANT, "--number", "0")
    assert neg.code == 1 and gh.calls() == []
    missing = gh.run("pr", "--repo", RESONANT, "--number", "99")
    assert missing.code == 1 and missing.stdout == ""
    assert missing.err.startswith("github: gh pr failed (exit 1): GraphQL: Could not resolve")


# --- watch ------------------------------------------------------------------------------


def test_watch_red_green_red_emits_exactly_three_stable_events(gh: FakeGh) -> None:
    """E12's extension half: the dedupe keys alone make the stateless watch exactly-once."""
    branch = f"{RESONANT}:branch-40"
    stored: dict[str, dict[str, Any]] = {}  # the events table: UNIQUE dedupe_key

    def step(sha: str, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        gh.state["mine"] = [pr_node(40, sha)]
        gh.state["runs"] = {branch: runs}
        first, again = gh.run("watch"), gh.run("watch")
        assert first.code == 0, first.err
        assert first.events == again.events  # unchanged state, identical keys
        for e in first.events:
            stored.setdefault(e["dedupe_key"], e)
        return first.events

    red1 = [run_row(1, "s1", "failure", workflow="lint")]
    assert keys(step("s1", red1)) == [f"{RESONANT}#40:run:1:failed"]
    # A new push, still running: the old failure is already stored, nothing new.
    pushed = [run_row(2, "s2", None, workflow="lint", status="in_progress"), *red1]
    assert keys(step("s2", pushed)) == []
    green = [run_row(2, "s2", "success", workflow="lint"), *red1]
    assert keys(step("s2", green)) == [f"{RESONANT}#40:run:2:fixed"]
    red2 = [run_row(3, "s3", "failure", workflow="lint"), *green]
    assert keys(step("s3", red2)) == [f"{RESONANT}#40:run:3:failed"]

    assert [(e["type"], e["text"]) for e in stored.values()] == [
        ("ci_failed", "✗ resonant#40 CI failed (lint)"),
        ("ci_fixed", "✓ resonant#40 CI fixed"),
        ("ci_failed", "✗ resonant#40 CI failed (lint)"),
    ]
    first = next(iter(stored.values()))
    assert first["data"] == {
        "repo": RESONANT,
        "number": 40,
        "url": f"https://github.com/{RESONANT}/pull/40",
        "workflow": "lint",
        "run_id": 1,
        "head_sha": "s1",
    }


def test_watch_green_from_the_start_is_silent(gh: FakeGh) -> None:
    gh.state["mine"] = [pr_node(40, "s2"), pr_node(41, "t1")]
    gh.state["runs"] = {
        f"{RESONANT}:branch-40": [run_row(2, "s2", "success"), run_row(1, "s1", "success")],
        f"{RESONANT}:branch-41": [run_row(5, "t1", "success")],
    }
    r = gh.run("watch")
    assert r.code == 0 and r.events == []
    assert r.out["summary"] == "2 PRs watched, 0 events"


def test_watch_ignores_cancelled_and_reports_per_workflow(gh: FakeGh) -> None:
    gh.state["mine"] = [pr_node(40, "s3")]
    gh.state["runs"] = {
        f"{RESONANT}:branch-40": [
            run_row(9, "s3", "success", workflow="test"),
            run_row(8, "s3", "cancelled", workflow="lint"),
            run_row(7, "s3", "success", workflow="lint"),
            run_row(6, "s2", "cancelled", workflow="lint"),
            run_row(5, "s1", "timed_out", workflow="lint"),
            run_row(4, "s1", "success", workflow="test"),
        ]
    }
    r = gh.run("watch")
    # lint: latest counted run (7) succeeded after a timed-out one (5): fixed. test: green.
    assert keys(r.events) == [f"{RESONANT}#40:run:7:fixed"]


def test_watch_rerun_keeps_its_run_id(gh: FakeGh) -> None:
    """A re-run reuses the run id: failing again repeats the key; passing sends no fixed."""
    gh.state["mine"] = [pr_node(40, "s1")]
    branch = f"{RESONANT}:branch-40"
    gh.state["runs"] = {branch: [run_row(1, "s1", "failure")]}
    assert keys(gh.run("watch").events) == [f"{RESONANT}#40:run:1:failed"]
    gh.state["runs"] = {branch: [run_row(1, "s1", "success")]}
    assert gh.run("watch").events == []


def test_watch_review_requested(gh: FakeGh) -> None:
    gh.state["review"] = [pr_node(12, "abc", repo="org/app", title="Please look")]
    r = gh.run("watch")
    (e,) = r.events
    assert e["type"] == "review_requested"
    assert e["dedupe_key"] == "org/app#12:abc:review"
    assert e["text"] == "👀 app#12 review requested: Please look"
    # No run lookups for PRs you only review.
    assert [c["argv"][0] for c in gh.calls()] == ["api"]


def test_watch_caps_events_and_text(gh: FakeGh) -> None:
    long_title = "t" * 500
    gh.state["review"] = [pr_node(n, f"h{n}", title=long_title) for n in range(1, 131)]
    r = gh.run("watch")
    assert len(r.events) == 100
    assert all(len(e["text"]) <= 300 and len(e["dedupe_key"]) <= 200 for e in r.events)
    assert r.out["events_found"] == 130


def test_watch_events_go_to_fd3_when_provided(gh: FakeGh) -> None:
    gh.state["mine"] = [pr_node(40, "s1")]
    gh.state["runs"] = {f"{RESONANT}:branch-40": [run_row(1, "s1", "failure")]}
    gh.state_path.write_text(json.dumps(gh.state))
    read_fd, write_fd = os.pipe()
    sec_r, sec_w = os.pipe()
    os.write(sec_w, json.dumps({"token": TOKEN}).encode())
    os.close(sec_w)
    proc = subprocess.Popen(
        [sys.executable, "-I", "-S", str(CLI), "watch"],
        env=gh.env(RESONANT_EVENTS_FD=str(write_fd), RESONANT_SECRETS_FD=str(sec_r)),
        cwd=EXT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(write_fd, sec_r),
        text=True,
    )
    os.close(write_fd)
    os.close(sec_r)
    stdout, _ = proc.communicate(timeout=30)
    with os.fdopen(read_fd) as f:
        lines = f.read().splitlines()
    assert proc.returncode == 0
    assert "events" not in json.loads(stdout)
    (line,) = lines
    assert json.loads(line)["dedupe_key"] == f"{RESONANT}#40:run:1:failed"


# --- digest and health --------------------------------------------------------------------


def test_digest_reports_the_summary_item(gh: FakeGh) -> None:
    gh.state["mine"] = [
        pr_node(41, "a", contexts=[check("lint", conclusion="FAILURE")]),
        pr_node(42, "b", contexts=[check("test")]),
        pr_node(43, "c", contexts=[check("build", conclusion="FAILURE")]),
    ]
    gh.state["review"] = [pr_node(9, "d", repo="org/app"), pr_node(10, "e", repo="org/app")]
    r = gh.run("digest")
    (e,) = r.events
    line = "3 open PRs, 2 failing CI, 2 awaiting your review"
    assert e["type"] == "item" and e["text"] == line
    assert e["dedupe_key"].startswith("item:github/summary:")
    # Coarse state (counts), low urgency: the briefing's Updates, never a text on its own.
    assert e["data"] == {
        "key": "github/summary",
        "urgency": "low",
        "state": {"open": 3, "failing": 2, "review_requested": 2},
        "clear": False,
        "ttl": "30h",
    }
    assert r.out["summary"] == line


def test_report_event_matches_the_sdk_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """The inline copy in cli.py must produce what ``resonant_sdk.emit.report`` would."""
    import importlib.util

    from resonant_sdk.emit import report

    spec = importlib.util.spec_from_file_location("github_cli", CLI)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    r, w = os.pipe()
    monkeypatch.setenv("RESONANT_EVENTS_FD", str(w))
    state = {"open": 3, "failing": 1, "review_requested": 2}
    assert report("github/summary", "3 open PRs", urgency="low", state=state, ttl="30h")
    os.close(w)
    with os.fdopen(r) as f:
        sdk = json.loads(f.read())
    mine = cli.report_event("github/summary", "3 open PRs", state)
    assert mine["type"] == sdk["type"] and mine["text"] == sdk["text"]
    assert mine["data"] == sdk["data"]
    assert mine["dedupe_key"].startswith("item:github/summary:")


def test_digest_with_nothing_open(gh: FakeGh) -> None:
    r = gh.run("digest")
    assert r.events[0]["text"] == "0 open PRs, 0 failing CI, 0 awaiting your review"
    assert r.events[0]["data"]["state"] == {"open": 0, "failing": 0, "review_requested": 0}


def test_health(gh: FakeGh) -> None:
    ok = gh.run("health")
    assert ok.code == 0 and ok.out == {"ok": True, "login": "octo"}
    gh.state["fail"] = {"exit": 1, "stderr": "HTTP 401: Bad credentials"}
    bad = gh.run("health")
    assert (
        bad.code == 1 and bad.err == "github: gh api failed (exit 1): HTTP 401: Bad credentials\n"
    )


# --- token handling and failures ----------------------------------------------------------


def test_token_reaches_gh_only_via_a_private_config_file(gh: FakeGh) -> None:
    gh.state["mine"] = [pr_node(40, "s1")]
    r = gh.run("watch")
    assert r.code == 0
    calls = gh.calls()
    assert len(calls) == 2
    for call in calls:
        assert call["token_ok"] is True
        env = set(call["env"])
        assert {"GH_CONFIG_DIR", "PATH", "HOME", "GH_PROMPT_DISABLED"} <= env
        assert not {k for k in env if "TOKEN" in k or "SECRET" in k}
        assert call["modes"] == [0o700, 0o600]
        assert not {k for k in env if k.startswith("RESONANT_")}
        assert "PARENT_SENTINEL" not in env
        assert TOKEN not in json.dumps(call["argv"])
    assert TOKEN not in r.stdout + r.err


def test_token_is_redacted_from_gh_errors(gh: FakeGh) -> None:
    gh.state["fail"] = {"exit": 4, "stderr": f"bad token {TOKEN}\nmore"}
    r = gh.run("prs")
    assert r.code == 1 and r.stdout == ""
    assert TOKEN not in r.err
    assert r.err == "github: gh api failed (exit 4): bad token [redacted] more\n"


def test_missing_token_fails_before_gh(gh: FakeGh) -> None:
    r = gh.run("prs", deliver=False)
    assert r.code == 1 and "secret github/token is missing" in r.err
    r = gh.run("prs", secrets={"other": "x"})
    assert r.code == 1 and "secret github/token is missing" in r.err
    assert gh.calls() == []


def test_gh_missing_or_garbage_fails_cleanly(gh: FakeGh, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    r = gh.run("prs", env=gh.env(PATH=str(empty)))
    assert r.code == 1 and r.err == f"github: gh not found on {empty}\n"

    gh.state["garbage"] = True
    r = gh.run("watch")
    assert r.code == 1 and r.err == "github: gh api returned invalid JSON\n"


# --- writes: rerun_failed and merge_pr ----------------------------------------------------

HEAD = "a1b2c3d" + "0" * 33


def view(
    number: int = 40,
    *,
    head: str = HEAD,
    state: str = "OPEN",
    draft: bool = False,
    mergeable: str = "MERGEABLE",
    merge_state: str = "CLEAN",
    checks: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": "A change",
        "url": f"https://github.com/{RESONANT}/pull/{number}",
        "state": state,
        "isDraft": draft,
        "headRefName": f"branch-{number}",
        "headRefOid": head,
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
        "statusCheckRollup": [check("test"), check("lint")] if checks is None else checks,
    }


def mutating(gh: FakeGh) -> list[list[str]]:
    return [c["argv"] for c in gh.calls() if c["argv"][:2] in (["pr", "merge"], ["run", "rerun"])]


def merge(gh: FakeGh, sha: str = HEAD, number: str = "40") -> Result:
    return gh.run("merge_pr", "--repo", RESONANT, "--number", number, "--head-sha", sha)


def test_rerun_failed_reruns_failed_runs_on_the_head_only(gh: FakeGh) -> None:
    gh.state["prs"] = {f"{RESONANT}#40": view()}
    gh.state["runs"] = {
        f"{RESONANT}:branch-40": [
            run_row(9, HEAD, "failure", workflow="lint"),
            run_row(8, HEAD, "success", workflow="test"),
            run_row(7, HEAD, "timed_out", workflow="e2e"),
            run_row(6, HEAD, "failure", workflow="docs"),  # superseded by 10
            run_row(10, HEAD, "success", workflow="docs"),
            run_row(5, "old" + "0" * 37, "failure", workflow="build"),  # an older push
            run_row(11, HEAD, None, workflow="slow", status="in_progress"),
        ]
    }
    r = gh.run("rerun_failed", "--repo", RESONANT, "--number", "40")
    assert r.code == 0, r.err
    assert mutating(gh) == [
        ["run", "rerun", "7", "--repo", RESONANT, "--failed"],
        ["run", "rerun", "9", "--repo", RESONANT, "--failed"],
    ]
    assert r.out["summary"] == "re-running failed jobs on resonant#40 (a1b2c3d): e2e, lint"
    assert r.out["rerun"] == [{"workflow": "e2e", "run_id": 7}, {"workflow": "lint", "run_id": 9}]
    assert r.out["already_running"] == ["slow"] and r.out["errors"] == []
    reads = [c["argv"][:2] for c in gh.calls()][:2]
    assert reads == [["pr", "view"], ["run", "list"]]


def test_rerun_failed_when_already_rerunning_is_a_clean_failure(gh: FakeGh) -> None:
    """A re-run keeps its run id and goes back to in_progress: nothing to do, nothing done."""
    gh.state["prs"] = {f"{RESONANT}#40": view()}
    gh.state["runs"] = {
        f"{RESONANT}:branch-40": [run_row(9, HEAD, None, workflow="lint", status="queued")]
    }
    r = gh.run("rerun_failed", "--repo", RESONANT, "--number", "40")
    assert r.code == 1 and r.stdout == ""
    assert r.err == "github: nothing to rerun on resonant#40 (a1b2c3d): lint still running\n"
    gh.state["runs"] = {f"{RESONANT}:branch-40": [run_row(9, HEAD, "success")]}
    r = gh.run("rerun_failed", "--repo", RESONANT, "--number", "40")
    assert r.code == 1 and r.err.endswith("(a1b2c3d): no failed runs\n")
    assert mutating(gh) == []


def test_rerun_failed_refuses_closed_prs_and_bad_args(gh: FakeGh) -> None:
    gh.state["prs"] = {f"{RESONANT}#40": view(state="MERGED")}
    r = gh.run("rerun_failed", "--repo", RESONANT, "--number", "40")
    assert r.code == 1 and r.err == "github: resonant#40 is merged; nothing to rerun\n"
    bad = gh.run("rerun_failed", "--repo", "x", "--number", "40")
    assert bad.code == 1 and "owner/name" in bad.err
    assert [c["argv"][:2] for c in gh.calls()] == [["pr", "view"]]


def test_rerun_failed_permission_error_is_clean(gh: FakeGh) -> None:
    gh.state["prs"] = {f"{RESONANT}#40": view()}
    gh.state["runs"] = {f"{RESONANT}:branch-40": [run_row(9, HEAD, "failure")]}
    gh.state["fail_on"] = {
        "run rerun": {
            "exit": 1,
            "stderr": f"HTTP 403: Resource not accessible by personal access token {TOKEN}",
        }
    }
    r = gh.run("rerun_failed", "--repo", RESONANT, "--number", "40")
    assert r.code == 1 and r.stdout == "" and TOKEN not in r.err
    assert r.err == (
        "github: the token can't do this: it needs Actions: write (see the github extension "
        "README); nothing was changed\n"
    )


def test_rerun_failed_partial_success_reports_rather_than_fails(gh: FakeGh) -> None:
    """Once one rerun has gone through, exit 1 would claim nothing happened: report instead."""
    gh.state["prs"] = {f"{RESONANT}#40": view()}
    gh.state["runs"] = {
        f"{RESONANT}:branch-40": [
            run_row(7, HEAD, "failure", workflow="a"),
            run_row(8, HEAD, "failure", workflow="b"),
        ]
    }
    gh.state["fail_on"] = {"run rerun 8": {"exit": 1, "stderr": "HTTP 500: oops"}}
    r = gh.run("rerun_failed", "--repo", RESONANT, "--number", "40")
    assert r.code == 0, r.err
    assert r.out["rerun"] == [{"workflow": "a", "run_id": 7}]
    assert r.out["errors"] == ["b: gh run failed (exit 1): HTTP 500: oops"]
    assert r.out["summary"].endswith(": a; 1 could not be re-run")


def test_merge_pr_squashes_bound_to_the_approved_head(gh: FakeGh) -> None:
    gh.state["prs"] = {f"{RESONANT}#40": view()}
    r = merge(gh, sha="A1B2C3D")  # an abbreviated, upper-case SHA is fine
    assert r.code == 0, r.err
    (argv,) = mutating(gh)
    assert argv == [
        "pr", "merge", "40", "--repo", RESONANT, "--squash", "--match-head-commit", HEAD,
    ]  # fmt: skip
    assert "--admin" not in argv and "--auto" not in argv
    assert r.out == {
        "summary": "merged resonant#40 (squash) at a1b2c3d",
        "repo": RESONANT,
        "number": 40,
        "head_sha": HEAD,
        "method": "squash",
        "url": f"https://github.com/{RESONANT}/pull/40",
    }
    view_argv = gh.calls()[0]["argv"]
    assert view_argv[:5] == ["pr", "view", "40", "--repo", RESONANT]
    assert "mergeStateStatus" in view_argv[-1]


@pytest.mark.parametrize(
    ("pr", "sha", "err"),
    [
        (view(head="f" * 40), HEAD, "has new commits since it was approved (head fffffff"),
        (view(checks=[check("test"), check("lint", conclusion="FAILURE")]), HEAD,
         "has failing checks (lint); not merging"),
        (view(checks=[check("test", conclusion=None, status="IN_PROGRESS")]), HEAD,
         "still has checks running"),
        (view(mergeable="CONFLICTING", merge_state="DIRTY"), HEAD, "in conflict with its base"),
        (view(mergeable="UNKNOWN", merge_state="UNKNOWN"), HEAD, "try again in a minute"),
        (view(merge_state="BLOCKED"), HEAD, "blocked by branch protection"),
        (view(merge_state="BEHIND"), HEAD, "behind its base branch"),
        (view(mergeable="CONFLICTING", merge_state="CLEAN"), HEAD, "not mergeable (conflicting)"),
        (view(draft=True), HEAD, "is a draft"),
        (view(state="MERGED"), HEAD, "resonant#40 is already merged"),
        (view(state="CLOSED"), HEAD, "resonant#40 is closed; not merging"),
    ],
)  # fmt: skip
def test_merge_pr_refuses_without_merging(
    gh: FakeGh, pr: dict[str, Any], sha: str, err: str
) -> None:
    gh.state["prs"] = {f"{RESONANT}#40": pr}
    r = merge(gh, sha=sha)
    assert r.code == 1 and r.stdout == "" and err in r.err, r.err
    assert mutating(gh) == []


@pytest.mark.parametrize("sha", ["", "abc", "zzzzzzz", "a1b2c3d e", "a" * 65])
def test_merge_pr_needs_a_sha(gh: FakeGh, sha: str) -> None:
    gh.state["prs"] = {f"{RESONANT}#40": view()}
    r = merge(gh, sha=sha)
    assert r.code == 1 and "head_sha must be" in r.err
    assert gh.calls() == []


def test_merge_pr_permission_and_race_errors_are_clean(gh: FakeGh) -> None:
    gh.state["prs"] = {f"{RESONANT}#40": view()}
    gh.state["fail_on"] = {
        "pr merge": {
            "exit": 1,
            "stderr": "GraphQL: Resource not accessible by personal access token",
        }
    }
    r = merge(gh)
    assert r.code == 1 and r.stdout == ""
    assert r.err == (
        "github: the token can't do this: it needs Contents: write and Pull requests: write "
        "(see the github extension README); nothing was changed\n"
    )
    # The head moved between the view and the merge: GitHub refuses via --match-head-commit.
    gh.state["fail_on"] = {
        "pr merge": {"exit": 1, "stderr": "GraphQL: Head branch was modified. Review and try"}
    }
    r = merge(gh)
    assert r.code == 1
    assert (
        r.err
        == "github: gh pr failed (exit 1): GraphQL: Head branch was modified. Review and try\n"
    )


def test_rate_limit_is_not_reported_as_a_missing_scope(gh: FakeGh) -> None:
    gh.state["prs"] = {f"{RESONANT}#40": view()}
    gh.state["fail_on"] = {"pr merge": {"exit": 1, "stderr": "HTTP 403: API rate limit exceeded"}}
    r = merge(gh)
    assert (
        r.code == 1
        and r.err == "github: gh pr failed (exit 1): HTTP 403: API rate limit exceeded\n"
    )


def test_writes_pass_the_token_only_via_the_config_file(gh: FakeGh) -> None:
    gh.state["prs"] = {f"{RESONANT}#40": view()}
    gh.state["runs"] = {f"{RESONANT}:branch-40": [run_row(9, HEAD, "failure")]}
    outs = [merge(gh), gh.run("rerun_failed", "--repo", RESONANT, "--number", "40")]
    assert all(o.code == 0 for o in outs)
    calls = gh.calls()
    assert len(mutating(gh)) == 2
    for call in calls:
        assert call["token_ok"] is True and TOKEN not in json.dumps(call["argv"])
        assert not {k for k in call["env"] if k.startswith("RESONANT_")}
    assert all(TOKEN not in o.stdout + o.err for o in outs)


def test_config_dir_is_removed_and_only_real_stale_ones_are_swept(gh: FakeGh) -> None:
    tmp = gh.data / "tmp"
    tmp.mkdir()
    old = (1, 1)

    def mk(name: str, mode: int = 0o700) -> Path:
        d = tmp / name
        d.mkdir()
        (d / "hosts.yml").write_text("x")
        d.chmod(mode)
        os.utime(d, old)
        return d

    mk("resonant-gh-stale")
    mk("other-stale")  # name does not match
    mk("resonant-gh-open", 0o755)  # mode is not 0700
    mk("resonant-gh-fresh").touch()  # a concurrent run's dir (recent)
    victim = gh.data / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    (tmp / "resonant-gh-link").symlink_to(victim)
    assert gh.run("prs").code == 0
    assert sorted(p.name for p in tmp.iterdir()) == [
        "other-stale",
        "resonant-gh-fresh",
        "resonant-gh-link",
        "resonant-gh-open",
    ]
    assert (victim / "keep").exists()


def test_extension_reads_secrets_only_from_the_pipe() -> None:
    src = CLI.read_text()
    assert "RESONANT_SECRETS_FD" in src and "RESONANT_SECRET_" not in src

"""The github extension (P3-9) against a fake ``gh``: never the real one, never GitHub.

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
            "RESONANT_SECRET_TOKEN": TOKEN,
            "PARENT_SENTINEL": "must-not-reach-gh",
        }
        return {**env, **extra}

    def run(self, *args: str, env: dict[str, str] | None = None) -> Result:
        self.state_path.write_text(json.dumps(self.state))
        proc = subprocess.run(
            [sys.executable, "-I", "-S", str(CLI), *args],
            env=self.env() if env is None else env,
            cwd=EXT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
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
    assert set(specs) == {"prs", "pr", "job_watch", "job_digest"}
    for s in specs.values():
        assert s.effect is Effect.READ and s.effective_level is Level.L0
        assert s.secrets == ("token",)
    assert {j.id: j.schedule for j in m.jobs} == {"watch": "*/10 * * * *", "digest": "50 7 * * *"}
    assert m.notify is not None and m.notify.on == ("ci_failed", "ci_fixed", "review_requested")
    assert m.health is not None and m.health.check == "python3 cli.py health"
    assert m.secrets == ("token",) and m.description and m.examples
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
    proc = subprocess.Popen(
        [sys.executable, "-I", "-S", str(CLI), "watch"],
        env=gh.env(RESONANT_EVENTS_FD=str(write_fd)),
        cwd=EXT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(write_fd,),
        text=True,
    )
    os.close(write_fd)
    stdout, _ = proc.communicate(timeout=30)
    with os.fdopen(read_fd) as f:
        lines = f.read().splitlines()
    assert proc.returncode == 0
    assert "events" not in json.loads(stdout)
    (line,) = lines
    assert json.loads(line)["dedupe_key"] == f"{RESONANT}#40:run:1:failed"


# --- digest and health --------------------------------------------------------------------


def test_digest_line_and_key(gh: FakeGh) -> None:
    gh.state["mine"] = [
        pr_node(41, "a", contexts=[check("lint", conclusion="FAILURE")]),
        pr_node(42, "b", contexts=[check("test")]),
        pr_node(43, "c", contexts=[check("build", conclusion="FAILURE")]),
    ]
    gh.state["review"] = [pr_node(9, "d", repo="org/app")]
    r = gh.run("digest", env=gh.env(RESONANT_JOB_DUE_AT="2026-10-14T07:50:00-07:00"))
    (e,) = r.events
    line = "github: 3 open PRs, resonant#41 ✗ lint, resonant#43 ✗ build, 1 review requested"
    assert e == {
        "type": "digest",
        "text": line,
        "dedupe_key": "digest:2026-10-14T07:50:00-07:00",
        "data": {
            "open": 3,
            "failing": [f"{RESONANT}#41", f"{RESONANT}#43"],
            "review_requested": 1,
        },
    }
    assert r.out["summary"] == line


def test_digest_with_nothing_open(gh: FakeGh) -> None:
    r = gh.run("digest", env=gh.env(RESONANT_JOB_DUE_AT="2026-10-14T07:50:00Z"))
    assert r.events[0]["text"] == "github: 0 open PRs"


def test_health(gh: FakeGh) -> None:
    ok = gh.run("health")
    assert ok.code == 0 and ok.out == {"ok": True, "login": "octo"}
    gh.state["fail"] = {"exit": 1, "stderr": "HTTP 401: Bad credentials"}
    bad = gh.run("health")
    assert (
        bad.code == 1 and bad.err == "github: gh api failed (exit 1): HTTP 401: Bad credentials\n"
    )


# --- token handling and failures ----------------------------------------------------------


def test_token_reaches_gh_only_as_gh_token(gh: FakeGh) -> None:
    gh.state["mine"] = [pr_node(40, "s1")]
    r = gh.run("watch")
    assert r.code == 0
    calls = gh.calls()
    assert len(calls) == 2
    for call in calls:
        assert call["token_ok"] is True
        env = set(call["env"])
        assert {"GH_TOKEN", "PATH", "HOME", "GH_PROMPT_DISABLED"} <= env
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
    env = gh.env()
    del env["RESONANT_SECRET_TOKEN"]
    r = gh.run("prs", env=env)
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

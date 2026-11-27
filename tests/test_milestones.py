"""PER-323: a run's milestones, read from the session's own calls and their results."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from resonant.brain import voice
from resonant.config import ChannelsConfig, RunsConfig, SlackConfig
from resonant.runners import milestones as ms
from resonant.store.db import open_db

PR = "https://github.com/acme/app/pull/12"


def call(tid: str, command: str) -> dict[str, Any]:
    block = {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}
    return {"type": "assistant", "message": {"content": [block]}}


def result(tid: str, text: str, *, error: bool = False) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": tid, "is_error": error, "content": text}
    return {"type": "user", "message": {"content": [block]}}


def seen(command: str, text: str, *, error: bool = False) -> list[ms.Milestone]:
    t = ms.Tracker()
    t.calls(call("t1", command))
    return t.results(result("t1", text, error=error))


def test_a_pr_create_with_its_link_is_pr_opened() -> None:
    assert seen("git push -u origin HEAD && gh pr create --fill", f"Creating…\n{PR}\n") == [
        ms.Milestone("pr_opened", 12, PR)
    ]
    assert seen("gh pr create --fill", "could not create", error=True) == []
    assert seen("gh pr create --fill", "no link here") == []


def test_ci_failures_reviews_merges_and_deploys() -> None:
    fail = "build\tfail\t2m\nSome checks were not successful"
    assert seen("gh pr checks 12 --watch", fail, error=True) == [ms.Milestone("ci_failed", 12)]
    assert seen("gh run watch 99 --exit-status", "X build failed", error=True) == [
        ms.Milestone("ci_failed")
    ]
    assert seen("gh pr checks --watch", "All checks were successful") == []
    assert seen(
        "gh pr view 12 --json reviewDecision", '{"reviewDecision":"CHANGES_REQUESTED"}'
    ) == [ms.Milestone("changes_requested", 12)]
    assert seen("gh pr view 12", "title: x\nreviewers: lead (Changes requested)\n") == [
        ms.Milestone("changes_requested", 12)
    ]
    # a stale review in the history, or the words in the PR body, isn't the PR's decision
    stale = '{"reviewDecision":"APPROVED","reviews":[{"state":"CHANGES_REQUESTED"}]}'
    assert seen("gh pr view 12 --json reviews,reviewDecision", stale) == []
    assert seen("gh pr view 12", "body: address the changes requested last week") == []
    assert seen("gh api repos/o/r/pulls/12/reviews", '[{"state": "CHANGES_REQUESTED"}]') == [
        ms.Milestone("changes_requested", 12)
    ]
    # pending checks (exit 8) and a check named fail-fast are not a CI failure
    assert seen("gh pr checks 12", "fail-fast\tpending\t0\n", error=True) == []
    # --auto queues the merge: nothing merged yet
    assert seen("gh pr merge 12 --auto --squash", "Pull request #12 will be merged") == []
    # PER-325: however it's spelled; ``--auto=false`` is a plain merge
    assert seen("gh pr merge 12 --auto=true --squash", "Pull request #12 will be merged") == []
    assert seen("gh pr merge 12 --disable-auto=true", "auto-merge disabled") == []
    assert [m.kind for m in seen("gh pr merge 12 --auto=false", "Merged pull request #12")] == [
        "merged"
    ]
    # the last gh command of a compound line is its outcome
    assert seen("gh pr checks 12 --watch && gh pr merge 12", "Merged pull request #12") == [
        ms.Milestone("merged", 12)
    ]
    assert seen("gh pr view 12", "Reviewers: lead (Approved)") == []
    assert seen("gh pr merge 12 --squash", "✓ Squashed and merged pull request #12") == [
        ms.Milestone("merged", 12)
    ]
    assert seen("gh pr merge --squash", "Merged pull request #7") == [ms.Milestone("merged", 7)]
    assert seen("gh pr merge 12", "not mergeable", error=True) == []
    assert seen("fly deploy -a app-staging", "deployed") == [
        ms.Milestone("deployed", env="staging")
    ]
    assert seen("vercel deploy --prod", "ok") == [ms.Milestone("deployed", env="production")]
    assert seen("fly deploy -a app-staging", "Error", error=True) == []
    assert seen("fly deploy", "deployed") == []  # an unknown target is never claimed
    assert seen("gh workflow run deploy-staging.yml", "ok") == []  # triggered, not deployed
    assert seen("pytest -q", "1 failed") == []


def test_mcp_tools_and_results_without_a_call() -> None:
    t = ms.Tracker()
    t.calls({"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": "m1", "name": "mcp__github__create_pull_request",
        "input": {"title": "x"}}]}})  # fmt: skip
    assert t.results(result("m1", f'{{"html_url": "{PR}"}}')) == [ms.Milestone("pr_opened", 12, PR)]
    assert t.results(result("nope", PR)) == []  # a result for no call it noted


def test_the_record_is_once_per_kind_and_folds_at_the_end(resonant_home: Path) -> None:
    conn = open_db(resonant_home / "t.db")
    try:
        assert ms.record(conn, "R1", ms.Milestone("pr_opened", 12, PR))
        assert not ms.record(conn, "R1", ms.Milestone("pr_opened", 13, PR))
        assert ms.record(conn, "R1", ms.Milestone("ci_failed"))  # takes the opened PR's number
        assert ms.record(conn, "R1", ms.Milestone("deployed", env="staging"))
        todo = ms.pending(conn, "R1")
        assert [k for k, _ in todo] == ["pr_opened", "ci_failed", "deployed:staging"]
        assert [ms.line("R1", m) for _, m in todo] == [
            f"R1 opened PR #12: {PR}",
            "R1: CI failed on PR #12; fixing it.",
            "R1 deployed to staging.",
        ]
        ms.mark(conn, "R1", "pr_opened", "posted")
        ms.fold(conn, "R1")
        assert ms.pending(conn, "R1") == []
        assert not ms.record(conn, "R1", ms.Milestone("ci_failed"))  # still once
        assert ms.deployed_envs(conn, "R1") == ["staging"]
        assert ms.next_step(["staging"]) == ms.NEXT_PROD
        assert ms.next_step(["staging", "production"]) is None
        assert ms.next_step([]) is None
        assert ms.pending(conn, "R2") == []
    finally:
        conn.close()


def test_lines_are_code_built() -> None:
    assert ms.line("R3", ms.Milestone("pr_opened")) == "R3 opened a PR."
    assert ms.line("R3", ms.Milestone("merged")) == "R3 merged its PR."
    assert ms.line("R3", ms.Milestone("merged", 4)) == "R3 merged PR #4."
    assert (
        ms.line("R3", ms.Milestone("changes_requested", 4))
        == "R3: the review asked for changes on PR #4; working on them."
    )
    assert ms.dedupe_suffix("deployed:staging") == "milestone:deployed:staging"


@pytest.mark.parametrize(
    ("set_to", "slack", "want"),
    [
        (None, True, "thread"),
        (None, False, "off"),
        ("off", True, "off"),
        ("thread", False, "thread"),
    ],
)
def test_runs_milestones_default_follows_slack(set_to: str | None, slack: bool, want: str) -> None:
    cfg = RunsConfig.model_validate({"milestones": set_to})
    on = SlackConfig(enabled=True, team_id="T0TEAM", bot_user_id="U0BOT")
    channels = ChannelsConfig(slack=on) if slack else ChannelsConfig()
    assert cfg.milestones_for(channels) == want


def test_the_voice_says_the_next_step_last_and_knows_its_reader() -> None:
    item = voice.Item(ref="R1", outcome="opened_pr", pr=12, pr_url=PR)
    facts = voice.Facts("outcome", (item,), next_step=ms.NEXT_PROD, audience="team")
    assert voice.finish("R1 opened PR #12.", facts).splitlines() == [
        "R1 opened PR #12.",
        ms.NEXT_PROD,
    ]
    assert voice.TEAM_LINE in voice.prompt(facts, 600)
    owner = voice.Facts("outcome", (item,))
    assert voice.TEAM_LINE not in voice.prompt(owner, 600)
    assert voice.finish("R1 opened PR #12.", owner).endswith(voice.NO_ACTION)
    needs = voice.Facts("outcome", (voice.Item(ref="R1", outcome="opened_pr", needs="/merge R1"),),
                        next_step=ms.NEXT_PROD)  # fmt: skip
    assert voice.finish("x", needs).splitlines()[-2:] == ["Needs you: /merge R1.", ms.NEXT_PROD]


def test_runs_milestones_off_in_yaml_is_off(resonant_home: Path) -> None:
    from resonant.config import load_settings

    (resonant_home / "config.yaml").write_text("runs:\n  milestones: off\n")
    assert load_settings().runs.milestones == "off"
    (resonant_home / "config.yaml").write_text("runs:\n  milestones: thread\n")
    assert load_settings().runs.milestones == "thread"


def test_a_merged_last_waits_for_the_end_text() -> None:
    """PER-325: a ``merged`` with nothing after it is held (the end text says the merge);
    more work after it lets it out, in order."""
    opened = ("pr_opened", ms.Milestone("pr_opened", 12, None))
    merged = ("merged", ms.Milestone("merged", 12))
    deployed = ("deployed:staging", ms.Milestone("deployed", env="staging"))
    assert ms.held([opened, merged]) == [opened]
    assert ms.held([merged]) == []
    assert ms.held([opened, merged, deployed]) == [opened, merged, deployed]
    assert ms.held([]) == []

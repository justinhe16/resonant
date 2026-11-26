"""PER-341: one voice for run updates (``runner.update_voice: brain``), the pure half and the
call's gate.

- ``update_facts``: a run's start or one milestone as facts the voice words; nothing is
  added after the prose (no ``No action needed.``, no ``Needs you``).
- ``Facts.plain``: a needs without a slash command is Claude's to say in words (it is in the
  facts, not the closing line); one with a command stays in the code-built closing line.
- ``ClaudeVoice`` words an ``update`` whatever ``brain.voice.kinds`` lists, never when the
  voice is off, and falls back to the code-built line on any trouble.

The runner's side (start, milestones, outcome in a Slack thread) is
``test_e2e_update_voice.py``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from test_voice import BASE_RUN, run_of
from voice_script import Script, scripted_voice

from resonant.brain import voice
from resonant.brain.voice import Facts, update_facts
from resonant.config import RunnerConfig, VoiceConfig
from resonant.runners.voice import CallResult

NOW = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)
TZ = "UTC"
URL = "https://github.com/own/app/pull/41"
RUN = replace(BASE_RUN, code="R7", repo="app", ask="Work Linear ticket DORI-1173: Backend Store")


def compose(raw: str, facts: Facts, fallback: str = "FALLBACK") -> voice.VoiceResult:
    return voice.compose(raw, facts, fallback, now=NOW, tz=TZ, max_chars=600, limit=1000)


def item0(facts: Facts) -> dict[str, Any]:
    """The first item as Claude sees it."""
    items: list[dict[str, Any]] = json.loads(facts.prompt_json())["items"]
    return items[0]


def test_the_knob_defaults_to_template() -> None:
    assert RunnerConfig().update_voice == "template"
    assert RunnerConfig.model_validate({"update_voice": "brain"}).update_voice == "brain"


def test_a_start_update_is_facts_and_prose_alone() -> None:
    facts = update_facts(RUN, "started", audience="team")
    assert facts.kind == "update" and facts.plain and facts.audience == "team"
    assert item0(facts) == {"ref": "R7", "outcome": "started", "repo": "app",
                    "ask": "Work Linear ticket DORI-1173: Backend Store"}  # fmt: skip
    prompt = voice.prompt(facts, 600)
    assert voice.KIND_LINES["update"] in prompt and voice.TEAM_LINE in prompt
    said = compose("R7 is picking up DORI-1173, the backend store ticket, now.", facts)
    assert said.used and said.text == "R7 is picking up DORI-1173, the backend store ticket, now."


def test_a_milestone_update_names_its_pr_and_target() -> None:
    opened = update_facts(RUN, "opened_pr", pr=41, pr_url=URL)
    assert compose(f"R7 opened PR #41 for the store ticket: {URL}", opened).used
    # a link that isn't the milestone's PR, or a verb it doesn't back, is the code-built line
    assert compose("R7 opened PR #41: https://example.com/x", opened).reason == "guard:url"
    assert compose("R7 merged PR #41.", opened).reason == "guard:claim"
    deployed = update_facts(RUN, "deployed", env="staging")
    assert item0(deployed)["env"] == "staging"
    assert compose("R7 deployed the store change to staging.", deployed).used
    failed = update_facts(RUN, "ci_failed", pr=41)
    assert compose("CI failed on R7's PR #41; it's on it.", failed).used
    assert compose("R7 hit a CI failure on PR #99.", failed).reason == "guard:numbers"


def test_plain_facts_give_a_wordy_needs_to_claude_and_add_no_closing_line() -> None:
    failed = run_of(status="failed", failure="tests failed", code="R9")
    facts = voice.outcome_facts(failed, plain=True)
    assert facts is not None
    assert item0(facts)["needs"] == "a look at R9"
    assert voice.closing_line(facts) == ""
    said = compose("R9 failed: tests failed. Could use a look at R9 when you have a minute.", facts)
    assert said.used and "Needs you" not in said.text and voice.NO_ACTION not in said.text
    assert voice.PLAIN_LINE in voice.prompt(facts, 600)
    # template (not plain): exactly as before
    before = voice.outcome_facts(failed)
    assert before is not None
    assert "needs" not in item0(before)
    assert voice.closing_line(before) == "Needs you: a look at R9."
    assert voice.PLAIN_LINE not in voice.prompt(before, 600)


def test_a_needs_with_a_command_stays_code_built_when_plain() -> None:
    ready = run_of(status="ready", pr_number=18, pr_url="https://github.com/o/r/pull/18")
    facts = voice.outcome_facts(ready, ci="green", plain=True)
    assert facts is not None
    assert "needs" not in item0(facts)
    assert voice.closing_line(facts).startswith("Needs you: /merge ")
    assert not voice.plain_needs("/merge R14 once CI is green")
    assert voice.plain_needs("a look at R9's branch")
    assert not voice.plain_needs(None)


def test_plain_with_nothing_needed_ends_with_the_next_step_alone() -> None:
    done = run_of(status="no_changes")
    facts = voice.outcome_facts(done, next_step="Ship it?", plain=True)
    assert facts is not None
    assert voice.finish("R5 found nothing to change.", facts) == (
        "R5 found nothing to change.\nShip it?"
    )
    assert voice.finish("R5 found nothing to change.", replace(facts, next_step=None)) == (
        "R5 found nothing to change."
    )


def test_merged_facts_carry_audience_and_plain() -> None:
    merged = run_of(status="merged", pr_number=2, pr_url="https://github.com/o/r/pull/2")
    facts = voice.merged_facts(merged, title="Fix it", audience="team", plain=True)
    assert facts.audience == "team" and facts.plain
    assert voice.merged_facts(merged, title="Fix it").audience == "owner"


# --- the call's gate -------------------------------------------------------------------------


async def test_an_update_is_worded_whatever_kinds_lists(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    script = Script("R7 is starting on the store ticket now.")
    v = scripted_voice(db, tmp_path, script, cfg=VoiceConfig(kinds=["pulse"]))
    out = await v.write(update_facts(RUN, "started"), "On it: starting R7.", ref="run:R7:started")
    assert out.used and out.text == "R7 is starting on the store ticket now."
    # the same ref again reuses the text: no second call
    again = await v.write(update_facts(RUN, "started"), "On it: starting R7.", ref="run:R7:started")
    assert again.text == out.text and script.calls == 1


async def test_an_update_falls_back_when_the_voice_is_off_or_fails(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    fallback = "On it: starting R7."
    off = scripted_voice(db, tmp_path, Script("x"), cfg=VoiceConfig(enabled=False))
    out = await off.write(update_facts(RUN, "started"), fallback)
    assert (out.text, out.used, out.reason) == (fallback, False, "disabled")
    broken = scripted_voice(db, tmp_path, Script(CallResult("failed")))
    out = await broken.write(update_facts(RUN, "started"), fallback)
    assert (out.text, out.used, out.reason) == (fallback, False, "failed")
    timed_out = scripted_voice(db, tmp_path, Script(CallResult("timeout")))
    out = await timed_out.write(update_facts(RUN, "started"), fallback)
    assert (out.text, out.used) == (fallback, False)

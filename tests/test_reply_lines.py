"""PER-352: the code-built lines under a brain turn's reply don't repeat its prose
(``runners/reply_lines.py``). The texts are the shapes Cori's 2026-11-26/27 threads showed."""

from __future__ import annotations

import pytest

from resonant.runners.claude import Continued, Started
from resonant.runners.reply_lines import covered, did_line, run_mention, runs_line, tidy

MERGED = "Done: merge PR #64 in own/ios"
REDEPLOY = "Done: redeploy the web staging site on Vercel"


def started(code: str, repo: str = "", **kw: str) -> str:
    return Started(code, repo, 360, **kw).text


def continued(code: str, repo: str = "service") -> str:
    return Continued(code, repo, "the time limit", 360).text


# --- covered ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "prose"),
    [
        (MERGED, "iOS PR #64 is merged. No backend change for 1173 has merged."),
        (MERGED, "Merged #64."),
        (REDEPLOY, "The web app's staging site is redeploying on Vercel from the latest main."),
        ("Did: close duplicate PR 63", "I closed #63 and #161 as duplicates."),
        ('Saved note 3: "buy milk"', "Saved it as note 3."),
    ],
)
def test_a_line_the_prose_reports_is_covered(line: str, prose: str) -> None:
    assert covered(line, prose)


@pytest.mark.parametrize(
    ("line", "prose"),
    [
        (MERGED, None),
        (MERGED, ""),
        (MERGED, "PR #65 is merged."),  # another PR
        (MERGED, "I couldn't merge #64: CI is red."),  # a negation in that sentence
        (MERGED, "Looked at #64; CI is green."),  # the number, not the action
        (REDEPLOY, "Staging is fine."),  # too few of its words
        (MERGED, "I'll merge #64 once CI is green."),  # still to come
        ("Done: create issue DORI-5", "I created DORI-6 and linked DORI-5."),  # another id
        ("Did: restart R12", "Restarted 12 files."),  # 12 isn't R12
        (MERGED, "Asked the owner to merge #64."),  # waiting on someone
        ('Done: linear: save issue id="RES-12"', "Linear shows RES-12."),  # not the action
        ("Done: restart", "Restarted."),  # nothing to match it on but one word
        ("Muted github until 17:00.", "Done."),
    ],
)
def test_a_line_the_prose_doesnt_report_stays(line: str, prose: str | None) -> None:
    assert not covered(line, prose)


# --- runs ------------------------------------------------------------------------------------


def test_the_run_tools_texts_are_recognised() -> None:
    """Tied to ``Started.text`` and ``Continued.text``: a change there breaks this test."""
    assert run_mention(started("R13", "ios")) is not None
    one = run_mention(started("R13", "ios"))
    assert one is not None and (one.verb, one.code, one.where) == ("started", "R13", "ios")
    scratch = run_mention(started("R2"))
    assert scratch is not None and scratch.where is None
    new = run_mention(started("R4", "web", new_repo="own/web", done="I'll post here."))
    assert new is not None and new.where == "web"
    heads_up = run_mention(started("R5", "web", warning="Heads up: main isn't protected."))
    assert heads_up is not None and heads_up.extras == ("Heads up: main isn't protected.",)
    cont = run_mention(continued("R12"))
    assert cont is not None and (cont.verb, cont.code, cont.where) == (
        "continuing", "R12", "service",
    )  # fmt: skip
    assert run_mention(MERGED) is None


def test_runs_get_one_line_without_those_the_prose_names() -> None:
    runs = [m for t in (started("R13", "ios"), started("R14", "web"), continued("R12"))
            if (m := run_mention(t)) is not None]  # fmt: skip
    want = "Started R13 (ios) and R14 (web); continuing R12 (service)."
    assert runs_line(runs, None) == want
    assert runs_line(runs, "R13 reviews #64.") == "Started R14 (web); continuing R12 (service)."
    assert runs_line(runs, "R13, R14 and R12 are on it.") is None
    # R1 isn't named by R12 or R13
    one = [m for m in [run_mention(started("R1", "web"))] if m is not None]
    assert runs_line(one, "R12 and R13 are going.") == "Started R1 (web)."


# --- tidy ------------------------------------------------------------------------------------


def test_the_merge_thread_keeps_only_what_the_prose_left_out() -> None:
    """2026-11-27: the reply said the merge and the restart in words, then ``Done: merge PR
    #64 …`` and ``Continuing R12 · … /runs to check, /cancel R12 to stop.``"""
    prose = (
        "iOS PR #64 is merged. No backend change for 1173 has merged.\n"
        "- Web: I started a worker to fix the failing checks on PR #162.\n"
        "- Backend: the earlier run that timed out kept its work, so I restarted it."
    )
    out = tidy([MERGED, started("R15", "web"), continued("R12")], prose)
    assert out == ["Started R15 (web); continuing R12 (service)."]
    assert not [t for t in out if "/cancel" in t or "Done:" in t]


def test_two_runs_in_one_turn_are_one_line_where_the_first_was() -> None:
    out = tidy(["Rule added: ask before merge in web (rule 2).", started("R13", "ios"),
                started("R14", "web")], "Started two reviews.")  # fmt: skip
    assert out == ["Rule added: ask before merge in web (rule 2).",
                   "Started R13 (ios) and R14 (web)."]  # fmt: skip


def test_without_prose_every_line_stays_and_runs_are_still_one_line() -> None:
    out = tidy([MERGED, started("R1", "web", warning="Heads up: no push yet."), MERGED], None)
    assert out == [MERGED, "Started R1 (web).", "Heads up: no push yet."]


def test_the_progress_line_keeps_what_the_prose_left_out() -> None:
    did = ["close duplicate PR 63", "close duplicate PR 161", "edited ~/notes.md"]
    assert did_line(did, "Closed #63 and #161 as duplicates.") == "Did: edited ~/notes.md"
    assert did_line(did[:2], "Closed #63 and #161 as duplicates.") is None
    assert did_line(did[:1], "Closed #63.") is None
    assert did_line(did[:1], None) == "Did: close duplicate PR 63"

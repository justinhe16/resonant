"""PER-328: only a short note about the approval is held; a longer text may answer
another part of the message and goes out with the prompt."""

from resonant.runners.ask import HELD_MAX_CHARS, short_note


def test_one_short_line_is_a_note() -> None:
    assert short_note("I'll redeploy the backend to staging once it's approved.")


def test_an_answer_plus_the_approval_is_not_a_note() -> None:
    assert not short_note("Staging is on v185.\nI'll redeploy it once it's approved.")


def test_a_long_single_line_is_not_a_note() -> None:
    assert not short_note("x" * (HELD_MAX_CHARS + 1))


def test_blank_lines_do_not_count() -> None:
    assert short_note("\n  On it once approved.  \n\n")

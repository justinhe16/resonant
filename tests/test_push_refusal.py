"""PER-355: git's reason for a refused push, from the session's own ``git push`` results."""

from __future__ import annotations

from typing import Any

import pytest

from resonant.runners.push_refusal import PushWatch, is_push, reason_of

WORKFLOW = (
    "To https://github.com/own/proj.git\n"
    " ! [remote rejected] resonant/r1-fix -> resonant/r1-fix (refusing to allow a GitHub App"
    " to create or update workflow `.github/workflows/ci.yml` without `workflows` permission)\n"
    "error: failed to push some refs to 'https://github.com/own/proj.git'\n"
)
STALE = (
    "To https://github.com/own/proj.git\n"
    " ! [rejected]        resonant/r1-fix -> resonant/r1-fix (fetch first)\n"
    "error: failed to push some refs to 'https://github.com/own/proj.git'\n"
    "hint: Updates were rejected because the remote contains work that you do not\n"
)
HOOK = (
    "remote: error: GH013: Repository rule violations found for refs/heads/main.\n"
    "To /tmp/proj.git\n"
    " ! [remote rejected] main -> main (pre-receive hook declined)\n"
    "error: failed to push some refs to '/tmp/proj.git'\n"
)
DENIED = (
    "remote: Permission to own/proj.git denied to bob.\n"
    "fatal: unable to access 'https://github.com/own/proj.git/': The requested URL returned"
    " error: 403\n"
)
NO_REMOTE = "fatal: 'upstream' does not appear to be a git repository\n"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        (WORKFLOW, "refusing to allow a GitHub App to create or update workflow "
                   "`.github/workflows/ci.yml` without `workflows` permission"),
        (STALE, "fetch first"),
        (HOOK, "Repository rule violations found for refs/heads/main."),
        (DENIED, "Permission to own/proj.git denied to bob."),
        (NO_REMOTE, "'upstream' does not appear to be a git repository"),
        ("Exit code 1\n", None),
    ],
)  # fmt: skip
def test_reason_of(text: str, want: str | None) -> None:
    assert reason_of(text) == want


@pytest.mark.parametrize(
    ("command", "want"),
    [
        ("git push -u origin resonant/r1-fix", True),  # its own branch too
        ("cd web && GIT_TRACE=1 git -C . push", True),
        ("git -c http.extraHeader=x push origin HEAD", True),
        ("git status && git log", False),
        ("echo git push", False),
        ("gh pr checkout 162", False),
    ],
)
def test_is_push(command: str, want: bool) -> None:
    assert is_push(command) is want


def call(tid: str, command: str) -> dict[str, Any]:
    block = {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}
    return {"type": "assistant", "message": {"content": [block]}}


def answer(tid: str, text: str, *, error: bool) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": tid, "is_error": error, "content": text}
    return {"type": "user", "message": {"content": [block]}}


def test_the_last_push_decides() -> None:
    """A refused push records git's reason; a later push that went through clears it; other
    calls' results are ignored."""
    w = PushWatch()
    w.calls(call("a", "git status"))
    w.results(answer("a", "fatal: not a git repository", error=True))
    assert w.refused is None
    w.calls(call("b", "git push -u origin resonant/r1-fix"))
    w.results(answer("b", f"Exit code 1\n{STALE}", error=True))
    assert w.refused == "fetch first"
    w.calls(call("c", "git pull --rebase && git push"))
    w.results(answer("c", "To https://github.com/own/proj.git\n   1234..5678  x -> x\n",
                     error=False))  # fmt: skip
    assert w.refused is None


def test_a_rejection_without_an_error_flag_still_counts() -> None:
    w = PushWatch()
    w.calls(call("a", "git push || true"))
    w.results(answer("a", WORKFLOW, error=False))
    assert w.refused is not None and w.refused.startswith("refusing to allow a GitHub App")


def test_a_failure_with_no_reason_says_so() -> None:
    w = PushWatch()
    w.calls(call("a", "git push"))
    w.results(answer("a", "Exit code 128", error=True))
    assert w.refused == "git push failed"

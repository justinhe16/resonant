"""PER-319: status lines (``runners.status``) and the loop's ``on_failed`` hook.

The lines themselves, the plain reason (no stack traces, no secrets, Slack's codes in words),
and the local runner's paths with a recording channel: a denial or expiry in a thread is one
``Couldn't finish that: …`` line (none on iMessage, whose ``Not done …`` reply already says
it), and an approval prompt nobody got, once the retries are spent, is one line wherever it
was asked (iMessage too: that failure used to be silent).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from test_local_runner_gated import OWNER, TTL_S, Harness, inferred, make

from resonant.gateway.channel import ChannelSendError
from resonant.runners import status

__all__ = ["make"]  # the fixture

THREAD = {"channel": "slack", "thread": "C0TEAM:1700000000.000100", "ref": "slack-in:1",
          "principal": OWNER}  # fmt: skip


def status_sends(h: Harness, task_id: str) -> list[str]:
    prefix = f"status:{task_id}:"
    return [text for _, text, key in h.channel.sent if key is not None and key.startswith(prefix)]


# --- the lines ---------------------------------------------------------------------------


def test_lines() -> None:
    assert (
        status.start_line("R5", "fix the README typo")
        == "On it: starting R5 (fix the README typo)."
    )
    assert status.start_line("R5", " ") == "On it: starting R5."
    assert status.waiting_line(["Lead", "Second"]) == "Waiting on an approval from Lead or Second."
    assert status.waiting_line(["A", "B", "C"]) == "Waiting on an approval from A, B or C."
    assert status.waiting_line([]) == "Waiting on an approval from an approver."
    assert status.failed_line("it took too long.") == "Couldn't finish that: it took too long."
    assert status.status_key("t1", status.FAILED) == "status:t1:failed"


def test_plain_reason_is_short_clean_and_in_words() -> None:
    tab = "approval prompt failed: slack: chat.postMessage failed: messages_tab_disabled"
    assert status.plain_reason(tab) == "I couldn't send the approval (Slack: Messages tab off)"
    assert status.plain_reason("approval prompt refused: no approver is reachable") == (
        "I couldn't send the approval (no approver could be reached)"
    )
    assert status.plain_reason("step exceeded its time limit") == "it took too long"
    crash = "KeyError: 'x'\nTraceback (most recent call last):\n  File ..."
    assert status.plain_reason(crash) == "something went wrong (KeyError)"
    leaked = "reply send failed: token ghp_" + "a" * 36 + " rejected"
    said = status.plain_reason(leaked)
    assert "ghp_" not in said and said.startswith("I couldn't send the reply")
    assert len(status.plain_reason("x" * 1000)) <= status.REASON_MAX
    assert status.plain_reason(None) == "something went wrong"
    assert status.plain_reason("") == "something went wrong"


# --- denial and expiry -------------------------------------------------------------------


async def test_expiry_in_a_thread_is_one_failure_line(make: Any) -> None:
    h: Harness = make()
    task_id = h.add_task(inferred("restart_resonant"), channel_ref=THREAD)
    await h.loop.run_until_idle()
    assert h.task(task_id).status == "waiting"
    h.clock.advance(TTL_S)
    await h.loop.run_until_idle()
    assert h.channel.replies() == ["Not done: the approval for restart Resonant expired."]
    assert status_sends(h, task_id) == [
        "Couldn't finish that: the approval expired before anyone answered."
    ]


async def test_a_denial_in_a_thread_names_who_said_no(make: Any) -> None:
    h: Harness = make()
    task_id = h.add_task(inferred("restart_resonant"), channel_ref=THREAD)
    await h.loop.run_until_idle()
    h.resolve(task_id, approved=False)
    await h.loop.run_until_idle()
    assert h.channel.replies() == ["Not done."]
    assert status_sends(h, task_id) == [f"Couldn't finish that: {status.display(OWNER)} said no."]


async def test_imessage_keeps_its_not_done_reply_alone(make: Any) -> None:
    h: Harness = make()
    task_id = h.add_task(inferred("restart_resonant"))  # no thread: iMessage
    await h.loop.run_until_idle()
    h.resolve(task_id, approved=False)
    await h.loop.run_until_idle()
    assert h.channel.replies() == ["Not done."]
    assert status_sends(h, task_id) == []


# --- the loop's on_failed hook -----------------------------------------------------------


async def test_a_prompt_nobody_got_says_why_once_after_the_retries(make: Any) -> None:
    h: Harness = make()
    h.loop.on_failed = status.StatusLines(h.db, h.channel).task_failed
    tab = "slack: chat.postMessage failed: messages_tab_disabled"
    h.channel.fail_prompt.extend([ChannelSendError(tab)] * 3)
    task_id = h.add_task(inferred("restart_resonant"))  # iMessage: it used to be silent
    for _ in range(3):
        await h.loop.run_until_idle()
        assert status_sends(h, task_id) == [] or h.task(task_id).status == "failed"
        h.skip_backoff(task_id)
    assert h.task(task_id).status == "failed"
    assert status_sends(h, task_id) == [
        "Couldn't finish that: I couldn't send the approval (Slack: Messages tab off)."
    ]


async def test_the_hook_skips_runners_with_no_asking_conversation(make: Any) -> None:
    h: Harness = make()
    lines = status.StatusLines(h.db, h.channel)
    task_id = h.add_task(inferred("restart_resonant"))
    task = h.task(task_id)
    await lines.task_failed(replace(task, runner="job"), "boom")
    await lines.task_failed(replace(task, checkpoint={"watch": 3}), "boom")
    assert status_sends(h, task_id) == []
    await lines.task_failed(task, "boom")
    await lines.task_failed(task, "boom")  # deduped by the channel
    assert status_sends(h, task_id) == ["Couldn't finish that: boom."]

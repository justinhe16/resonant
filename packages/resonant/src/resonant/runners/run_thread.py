"""Where a run's texts go (PER-294): the thread it was asked in, addressed to who asked.

A run keeps its thread (``runs.channel_ref``, migration 030; older runs: its task's
``channel_ref``) and its requester (``runs.requested_by``; None: the run's principal, the
owner). Its questions (``R4 asks:``), trust checks, notes and outcome go to that thread,
addressed to the requester. A requester who can't be reached any more (left the workspace:
the channel refuses them) doesn't strand the text: it goes to the run's principal, still in
the run's thread, where approvers answer. A run with no thread (iMessage) sends exactly as
before: to its principal, no thread.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from resonant.gateway.channel import Channel, ChannelRefused, MessageRef, thread_of
from resonant.store.runs import Run
from resonant.store.tasks import get_task


def run_ref(conn: sqlite3.Connection, run: Run) -> dict[str, Any] | None:
    """The run's ``channel_ref``: its own, else its task's (a run from before 030)."""
    if run.channel_ref is not None:
        return run.channel_ref
    task = get_task(conn, run.task_id) if run.task_id else None
    return None if task is None else task.channel_ref


def run_thread(conn: sqlite3.Connection, run: Run) -> MessageRef | None:
    """The run's thread as a ``MessageRef`` (a thread is only ever its asker's), or None."""
    return thread_of(run_ref(conn, run), to=run.requester)


async def send_run(
    channel: Channel,
    conn: sqlite3.Connection,
    run: Run,
    text: str,
    *,
    dedupe_key: str | None = None,
) -> MessageRef:
    """Send one of the run's texts (module docstring). Raises as ``Channel.send`` does."""
    thread = run_thread(conn, run)
    to = run.requester
    if to == run.principal:
        return await channel.send(to, text, thread=thread, dedupe_key=dedupe_key)
    try:
        return await channel.send(to, text, thread=thread, dedupe_key=dedupe_key)
    except ChannelRefused:  # the requester left: the run's principal, in the same thread
        return await channel.send(run.principal, text, thread=thread, dedupe_key=dedupe_key)

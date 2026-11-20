"""The Slack adapter's tables (migration 029, PER-295): followed threads and button prompts.

``slack_threads``: a thread is ``(channel, thread_ts)``. ``follow`` starts (or refreshes) it
when a message tags the bot; ``followed`` says whether an untagged reply in it routes (seen
within ``idle``), and ``touch`` refreshes it. ``known`` is the send allowlist: the adapter
posts in a thread only if it is listed. ``sweep_threads`` drops the idle ones.

``slack_prompts``: one row per approval prompt posted with buttons, per approver. ``pending``
lists them for the adapter's sweep (edit the message once the approval is no longer pending),
``forget`` removes one.

Callers own the transaction (``atomic``); none of these commit on their own.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from resonant.store.db import iso


def follow(
    conn: sqlite3.Connection, channel: str, thread_ts: str, principal: str, now: datetime
) -> None:
    conn.execute(
        "INSERT INTO slack_threads (channel, thread_ts, principal, started_at, last_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (channel, thread_ts) DO UPDATE SET last_at = excluded.last_at",
        (channel, thread_ts, principal, iso(now), iso(now)),
    )


def followed(
    conn: sqlite3.Connection, channel: str, thread_ts: str, now: datetime, idle: timedelta
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM slack_threads WHERE channel = ? AND thread_ts = ? AND last_at > ?",
        (channel, thread_ts, iso(now - idle)),
    ).fetchone()
    return row is not None


def touch(conn: sqlite3.Connection, channel: str, thread_ts: str, now: datetime) -> None:
    conn.execute(
        "UPDATE slack_threads SET last_at = ? WHERE channel = ? AND thread_ts = ?",
        (iso(now), channel, thread_ts),
    )


def known(conn: sqlite3.Connection, channel: str, thread_ts: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM slack_threads WHERE channel = ? AND thread_ts = ?", (channel, thread_ts)
    ).fetchone()
    return row is not None


def count_followed(conn: sqlite3.Connection, now: datetime, idle: timedelta) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM slack_threads WHERE last_at > ?", (iso(now - idle),)
    ).fetchone()
    return int(row[0])


def sweep_threads(conn: sqlite3.Connection, now: datetime, idle: timedelta) -> int:
    """Drop the threads idle for ``idle`` or longer. Returns how many."""
    cur = conn.execute("DELETE FROM slack_threads WHERE last_at <= ?", (iso(now - idle),))
    return cur.rowcount


@dataclass(frozen=True)
class Prompt:
    approval_id: str
    principal: str
    channel: str
    ts: str
    code: str
    text: str


def add_prompt(conn: sqlite3.Connection, prompt: Prompt, now: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO slack_prompts"
        " (approval_id, principal, channel, ts, code, text, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            prompt.approval_id,
            prompt.principal,
            prompt.channel,
            prompt.ts,
            prompt.code,
            prompt.text,
            iso(now),
        ),
    )


def prompts(conn: sqlite3.Connection) -> list[Prompt]:
    rows = conn.execute(
        "SELECT approval_id, principal, channel, ts, code, text FROM slack_prompts"
        " ORDER BY created_at"
    ).fetchall()
    return [Prompt(*(str(v) for v in tuple(r))) for r in rows]


def prompt_at(conn: sqlite3.Connection, channel: str, ts: str) -> Prompt | None:
    """The prompt posted as message ``ts`` in ``channel``, if it still has buttons."""
    r = conn.execute(
        "SELECT approval_id, principal, channel, ts, code, text FROM slack_prompts"
        " WHERE channel = ? AND ts = ?",
        (channel, ts),
    ).fetchone()
    return None if r is None else Prompt(*(str(v) for v in tuple(r)))


def forget(conn: sqlite3.Connection, prompt: Prompt) -> None:
    conn.execute(
        "DELETE FROM slack_prompts WHERE approval_id = ? AND principal = ?",
        (prompt.approval_id, prompt.principal),
    )

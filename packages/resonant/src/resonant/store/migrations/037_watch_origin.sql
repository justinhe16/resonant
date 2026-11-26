-- 037 (PER-346): a watch answers where it was made and can follow up. ``channel_ref`` is the
-- creating task's (JSON; where the notice goes: its thread, the channel falls back to the
-- principal's own conversation) and ``conversation`` its thread key (``tasks.conversation``);
-- NULL for every watch before this (they notify the principal's default channel, as before).
-- ``then_action`` gains ``follow_up``: on a hit the watch queues one brain ask (``follow_up``,
-- the instruction, bounded in code) in that conversation and ends; ``follow_up_task`` is the
-- ask it queued. SQLite can't change a CHECK in place, so ``brain_watches`` is rebuilt (the
-- 12-step ALTER). No table points at it and it points at none, so foreign keys can stay on.
-- The columns and index are 016's. The id is AUTOINCREMENT: its high-water mark is carried.
CREATE TABLE brain_watches_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    principal       TEXT NOT NULL,
    what            TEXT NOT NULL,
    check_kind      TEXT NOT NULL CHECK (check_kind IN ('tool', 'ask')),
    extension       TEXT,
    tool            TEXT,
    args            TEXT NOT NULL DEFAULT '{}',
    question        TEXT,
    op              TEXT NOT NULL CHECK (op IN ('contains', 'missing', 'changed', 'yes')),
    value           TEXT,
    then_action     TEXT NOT NULL DEFAULT 'stop'
                    CHECK (then_action IN ('stop', 'continue', 'follow_up')),
    interval_s      INTEGER NOT NULL,
    state           TEXT NOT NULL DEFAULT 'active'
                    CHECK (state IN ('active', 'fired', 'cancelled', 'expired', 'failed')),
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    next_at         TEXT NOT NULL,
    checks          INTEGER NOT NULL DEFAULT 0,
    fails           INTEGER NOT NULL DEFAULT 0,
    fired           INTEGER NOT NULL DEFAULT 0,
    last_met        INTEGER NOT NULL DEFAULT 0,
    baseline        TEXT,
    last_checked_at TEXT,
    last_error      TEXT,
    last_ask_task   TEXT,
    open_task_id    TEXT,
    created_by_task TEXT,
    ended_at        TEXT,
    channel_ref     TEXT,
    conversation    TEXT,
    follow_up       TEXT,
    follow_up_task  TEXT
);
INSERT INTO brain_watches_new (id, principal, what, check_kind, extension, tool, args, question,
                               op, value, then_action, interval_s, state, created_at,
                               expires_at, next_at, checks, fails, fired, last_met, baseline,
                               last_checked_at, last_error, last_ask_task, open_task_id,
                               created_by_task, ended_at)
SELECT id, principal, what, check_kind, extension, tool, args, question, op, value,
       then_action, interval_s, state, created_at, expires_at, next_at, checks, fails, fired,
       last_met, baseline, last_checked_at, last_error, last_ask_task, open_task_id,
       created_by_task, ended_at FROM brain_watches ORDER BY id;
DELETE FROM sqlite_sequence WHERE name = 'brain_watches_new';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'brain_watches_new', MAX(seq) FROM (
    SELECT seq FROM sqlite_sequence WHERE name = 'brain_watches'
    UNION ALL SELECT COALESCE(MAX(id), 0) FROM brain_watches
);
DROP TABLE brain_watches;
ALTER TABLE brain_watches_new RENAME TO brain_watches;
CREATE INDEX brain_watches_due ON brain_watches (state, next_at);

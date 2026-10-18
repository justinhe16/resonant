-- 016: watches (BR-7, docs/brain-plan.md D10). "Watch X and tell me when Y" is one row: a
-- periodic check (a read tool call, or a yes/no question for ask_claude) with a condition.
-- ``core/watches`` queues a ``runner="watch"`` task for each due row (``open_task_id``);
-- the runner checks, and on a hit sends one notice through the pulse (brain/notices.py).
-- ``state``: active until it fires (``then_action='stop'``), is cancelled, expires
-- (``expires_at``) or fails ``brain.watches.max_fails`` checks in a row.
CREATE TABLE brain_watches (
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
    then_action     TEXT NOT NULL DEFAULT 'stop' CHECK (then_action IN ('stop', 'continue')),
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
    ended_at        TEXT
);
CREATE INDEX brain_watches_due ON brain_watches (state, next_at);

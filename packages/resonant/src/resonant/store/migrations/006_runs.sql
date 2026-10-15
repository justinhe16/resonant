-- 006: Phase 5a Claude runner. One row per run (docs/phase5a-plan.md D4).

-- seq is AUTOINCREMENT, so a code (R1, R2, ...) is never reused, even after a row is deleted.
CREATE TABLE runs (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT NOT NULL UNIQUE,   -- 'R' || seq
    task_id           TEXT,
    principal         TEXT NOT NULL,
    repo              TEXT NOT NULL,          -- a runner.repos name
    ask               TEXT NOT NULL,          -- the owner's request (PII, like message bodies)
    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'waiting', 'ready', 'merged',
                                        'failed', 'cancelled', 'no_changes')),
    -- The last checkpoint reached; a resumed run continues here.
    step              TEXT NOT NULL DEFAULT 'prepare'
                      CHECK (step IN ('prepare', 'session', 'harvest', 'publish', 'reply')),
    branch            TEXT,
    base_branch       TEXT,
    base_sha          TEXT,
    head_sha          TEXT,
    session_id        TEXT NOT NULL,          -- chosen by the runner, saved before the spawn
    sessions          INTEGER NOT NULL DEFAULT 0,  -- sessions spawned (0: --session-id, else --resume)
    attempts          INTEGER NOT NULL DEFAULT 0,  -- daemon crashes survived during a session
    -- The live session's process group, leader and the leader's start time (epoch seconds), so
    -- a reused pid is never signalled. NULL when no session process is alive.
    pgid              INTEGER,
    leader_pid        INTEGER,
    leader_started_at REAL,
    session_seconds   REAL NOT NULL DEFAULT 0,     -- summed across attempts; waits excluded
    max_seconds       INTEGER NOT NULL,            -- the session time budget
    max_budget_usd    REAL,                        -- API billing only
    wait_seconds      REAL NOT NULL DEFAULT 0,     -- usage-limit waits so far
    waiting_since     TEXT,
    cost_usd          REAL,
    tokens_in         INTEGER,
    tokens_out        INTEGER,
    exit_code         INTEGER,
    pr_number         INTEGER,
    pr_url            TEXT,
    files_changed     INTEGER,
    flagged_paths     TEXT NOT NULL DEFAULT '[]',  -- JSON list of sensitive paths
    summary           TEXT,                        -- Claude's closing summary, cleaned and clipped
    failure           TEXT,                        -- the reason shown after "R3 failed:"
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    ended_at          TEXT,
    updated_at        TEXT NOT NULL
);
CREATE INDEX runs_status ON runs (status);
CREATE INDEX runs_repo ON runs (repo, status);

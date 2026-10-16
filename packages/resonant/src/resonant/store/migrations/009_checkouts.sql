-- 009: real checkouts and worktrees (docs/employee-plan.md D1, EM-2).
-- A repo may name where the owner's checkout lives (default: <runner.code_dir>/<name>).
ALTER TABLE repos ADD COLUMN path TEXT;

-- runs gains the `done` status (a run with no repo ends with Claude's summary, no PR) and
-- records where its work happened: `workspace` (the worktree or scratch dir; what cleanup
-- removes, after checking it sits under runner.code_dir) and `checkout` (the repo's checkout
-- the worktree belongs to; never deleted). A run with no repo has repo = ''.
-- SQLite can't change a CHECK in place, so the table is rebuilt; codes are never reused, so the
-- AUTOINCREMENT high-water mark is carried over.
CREATE TABLE runs_new (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT NOT NULL UNIQUE,
    task_id           TEXT,
    principal         TEXT NOT NULL,
    repo              TEXT NOT NULL,          -- a registry name, '' for a run with no repo
    ask               TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'waiting', 'ready', 'merged',
                                        'failed', 'cancelled', 'no_changes', 'done')),
    step              TEXT NOT NULL DEFAULT 'prepare'
                      CHECK (step IN ('prepare', 'session', 'harvest', 'publish', 'reply')),
    branch            TEXT,
    base_branch       TEXT,
    base_sha          TEXT,
    head_sha          TEXT,
    session_id        TEXT NOT NULL,
    sessions          INTEGER NOT NULL DEFAULT 0,
    attempts          INTEGER NOT NULL DEFAULT 0,
    pgid              INTEGER,
    leader_pid        INTEGER,
    leader_started_at REAL,
    session_seconds   REAL NOT NULL DEFAULT 0,
    max_seconds       INTEGER NOT NULL,
    max_budget_usd    REAL,
    wait_seconds      REAL NOT NULL DEFAULT 0,
    waiting_since     TEXT,
    cost_usd          REAL,
    tokens_in         INTEGER,
    tokens_out        INTEGER,
    exit_code         INTEGER,
    pr_number         INTEGER,
    pr_url            TEXT,
    files_changed     INTEGER,
    flagged_paths     TEXT NOT NULL DEFAULT '[]',
    summary           TEXT,
    failure           TEXT,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    ended_at          TEXT,
    updated_at        TEXT NOT NULL,
    workspace         TEXT,
    checkout          TEXT
);
INSERT INTO runs_new (seq, code, task_id, principal, repo, ask, status, step, branch,
    base_branch, base_sha, head_sha, session_id, sessions, attempts, pgid, leader_pid,
    leader_started_at, session_seconds, max_seconds, max_budget_usd, wait_seconds, waiting_since,
    cost_usd, tokens_in, tokens_out, exit_code, pr_number, pr_url, files_changed, flagged_paths,
    summary, failure, created_at, started_at, ended_at, updated_at)
SELECT seq, code, task_id, principal, repo, ask, status, step, branch,
    base_branch, base_sha, head_sha, session_id, sessions, attempts, pgid, leader_pid,
    leader_started_at, session_seconds, max_seconds, max_budget_usd, wait_seconds, waiting_since,
    cost_usd, tokens_in, tokens_out, exit_code, pr_number, pr_url, files_changed, flagged_paths,
    summary, failure, created_at, started_at, ended_at, updated_at
FROM runs;
DELETE FROM sqlite_sequence WHERE name = 'runs_new';
INSERT INTO sqlite_sequence (name, seq) SELECT 'runs_new', seq FROM sqlite_sequence WHERE name = 'runs';
DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;
CREATE INDEX runs_status ON runs (status);
CREATE INDEX runs_repo ON runs (repo, status);

-- migrate: foreign_keys=off
-- 020: store upkeep (PER-259). Nothing ever pruned events, finished tasks or the audit log, the
-- github rollup's weekly scan (`source = ? AND type IN (...) AND occurred_at >= ?`) had no index,
-- and a run deferred because every worker slot was busy was labelled `usage_reset`.
--  1. events_source_time serves that scan (`rollup.github_prs`).
--  2. tasks.wait_reason gains `resource` (deferred: a slot, repo lock or turn is busy). SQLite
--     can't change a CHECK in place, so `tasks` is rebuilt (the 12-step ALTER; the marker on
--     line 1 makes `migrate` run it with foreign keys off, then check `foreign_key_check`).
--     The columns and indexes are 001's; no later migration touched this table. The primary
--     key is TEXT, so there is no AUTOINCREMENT high-water mark to carry.
--  3. audit_log stays append-only, except for rows older than a cutoff that the prune
--     (`resonant.store_upkeep`) writes to kv `store.prune_audit_before` in the same transaction
--     as its DELETE and removes again before committing. No other connection ever sees the key,
--     and a row at or after the cutoff can never be deleted. The update trigger is unchanged.
CREATE INDEX events_source_time ON events (source, occurred_at);

CREATE TABLE tasks_new (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    runner            TEXT NOT NULL,
    extension         TEXT,
    principal         TEXT,
    lane              TEXT NOT NULL DEFAULT 'default',
    priority          INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'waiting', 'done', 'failed', 'cancelled')),
    wait_reason       TEXT CHECK (wait_reason IN ('approval', 'human_reply', 'usage_reset', 'ci', 'veto', 'resource')),
    wake_at           TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    step_no           INTEGER NOT NULL DEFAULT 0,
    checkpoint        TEXT NOT NULL DEFAULT '{}',
    claude_session_id TEXT,
    worktree_path     TEXT,
    scope             TEXT,
    origin_event_id   TEXT,
    channel_ref       TEXT,
    trace_id          TEXT NOT NULL,
    result            TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK ((status = 'waiting') = (wait_reason IS NOT NULL))
);
INSERT INTO tasks_new (id, kind, runner, extension, principal, lane, priority, status,
    wait_reason, wake_at, attempts, step_no, checkpoint, claude_session_id, worktree_path, scope,
    origin_event_id, channel_ref, trace_id, result, error, created_at, updated_at)
SELECT id, kind, runner, extension, principal, lane, priority, status,
    wait_reason, wake_at, attempts, step_no, checkpoint, claude_session_id, worktree_path, scope,
    origin_event_id, channel_ref, trace_id, result, error, created_at, updated_at
FROM tasks;
DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;
CREATE INDEX tasks_runnable ON tasks (status, priority, created_at);
CREATE INDEX tasks_wake ON tasks (wake_at) WHERE status = 'waiting';

DROP TRIGGER audit_log_no_delete;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
WHEN OLD.at >= COALESCE((SELECT json_extract(value, '$') FROM kv WHERE key = 'store.prune_audit_before'), '')
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

-- 001_init: core schema. Timestamps are ISO-8601 UTC text. JSON columns are TEXT.

-- Durable task envelope. Work content is opaque (checkpoint). Only the lifecycle is typed.
CREATE TABLE tasks (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    runner            TEXT NOT NULL,
    extension         TEXT,
    principal         TEXT,
    lane              TEXT NOT NULL DEFAULT 'default',
    priority          INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'waiting', 'done', 'failed', 'cancelled')),
    wait_reason       TEXT CHECK (wait_reason IN ('approval', 'human_reply', 'usage_reset', 'ci', 'veto')),
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
CREATE INDEX tasks_runnable ON tasks (status, priority, created_at);
CREATE INDEX tasks_wake ON tasks (wake_at) WHERE status = 'waiting';

-- Append-only event log: inbound triggers and per-task timeline.
CREATE TABLE events (
    id          TEXT PRIMARY KEY,
    task_id     TEXT REFERENCES tasks (id),
    source      TEXT NOT NULL,
    type        TEXT NOT NULL,
    principal   TEXT,
    payload     TEXT NOT NULL DEFAULT '{}',
    dedupe_key  TEXT UNIQUE,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    trace_id    TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX events_pending ON events (task_id) WHERE consumed_at IS NULL;

-- Approvals are bound to an intent hash and expire.
CREATE TABLE approvals (
    id           TEXT PRIMARY KEY,
    task_id      TEXT REFERENCES tasks (id),
    intent_hash  TEXT NOT NULL,
    extension    TEXT,
    tool         TEXT NOT NULL,
    effect       TEXT NOT NULL CHECK (effect IN ('read', 'write', 'pay')),
    level        TEXT NOT NULL CHECK (level IN ('L0', 'L1', 'L2', 'L3')),
    summary      TEXT NOT NULL,
    requested_by TEXT,
    approvers    TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    decided_by   TEXT,
    decided_at   TEXT,
    expires_at   TEXT NOT NULL,
    channel_refs TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL
);
CREATE INDEX approvals_pending ON approvals (status, expires_at);
CREATE INDEX approvals_task ON approvals (task_id);
-- At most one pending approval per (task, intent).
CREATE UNIQUE INDEX approvals_one_pending ON approvals (task_id, intent_hash) WHERE status = 'pending';

-- Side-effect idempotency: one row per (task, step, intent). A succeeded key is never re-run.
CREATE TABLE executions (
    idempotency_key TEXT PRIMARY KEY,
    task_id         TEXT REFERENCES tasks (id),
    intent_hash     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
    result          TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);
CREATE INDEX executions_task ON executions (task_id);

-- Every action, append-only. Secrets are redacted before insert.
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    action          TEXT NOT NULL,
    task_id         TEXT,
    requested_by    TEXT,
    approved_by     TEXT,
    extension       TEXT,
    tool            TEXT,
    effect          TEXT,
    level           TEXT,
    intent_hash     TEXT,
    idempotency_key TEXT,
    decision        TEXT,
    detail          TEXT NOT NULL DEFAULT '{}',
    trace_id        TEXT
);
CREATE INDEX audit_task ON audit_log (task_id);
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

-- Traces: llm call -> tool -> gate -> executor.
CREATE TABLE spans (
    id         TEXT PRIMARY KEY,
    trace_id   TEXT NOT NULL,
    parent_id  TEXT,
    task_id    TEXT,
    name       TEXT NOT NULL,
    attrs      TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'error')),
    started_at TEXT NOT NULL,
    ended_at   TEXT
);
CREATE INDEX spans_trace ON spans (trace_id);

-- Decisions made by the owner or co-founder, used to answer ask_human lookups.
CREATE TABLE decision_log (
    id        TEXT PRIMARY KEY,
    at        TEXT NOT NULL,
    task_id   TEXT,
    principal TEXT,
    extension TEXT,
    topic     TEXT NOT NULL,
    question  TEXT,
    decision  TEXT NOT NULL,
    rationale TEXT
);

CREATE TABLE memory (
    scope      TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

-- Small global state: autonomy/paused flags, claude_usage_reset_at, heartbeat, etc.
CREATE TABLE kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

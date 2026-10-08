-- 002: explicit kill-switch state, and approval batch snapshots.

-- job-guard fails closed when these rows are missing, so a fresh install must have them.
INSERT OR IGNORE INTO kv (key, value, updated_at)
VALUES ('autonomy', '"on"', strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
       ('paused', 'false', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

-- A batch message lists specific approvals. "yes all" approves exactly this snapshot.
CREATE TABLE approval_batches (
    id           TEXT PRIMARY KEY,
    principal    TEXT NOT NULL,
    approval_ids TEXT NOT NULL,  -- JSON array, never includes pay approvals
    channel_ref  TEXT,
    created_at   TEXT NOT NULL,
    used_at      TEXT
);

ALTER TABLE approvals ADD COLUMN require_code INTEGER NOT NULL DEFAULT 0;
ALTER TABLE approvals ADD COLUMN code TEXT;
CREATE UNIQUE INDEX approvals_pending_code ON approvals (code) WHERE status = 'pending';

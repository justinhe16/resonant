-- 003: Phase 2 approvals. Single use, and per-event idempotent resolution.

-- Set in the same transaction as the executor's 'started' row. A used approval never
-- yields another Allow.
ALTER TABLE approvals ADD COLUMN used_at TEXT;

-- The reply event that resolved this approval. Routing the same event again finds it
-- here and returns the original result instead of resolving anything twice.
ALTER TABLE approvals ADD COLUMN resolved_event_id TEXT;
CREATE INDEX approvals_resolved_event ON approvals (resolved_event_id)
    WHERE resolved_event_id IS NOT NULL;

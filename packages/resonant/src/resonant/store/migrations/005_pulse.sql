-- 005: Phase 3.5 pulse. Items that extensions (and core) report, with what the owner last heard.

-- One row per (source, key): the latest report of a thing worth mentioning.
CREATE TABLE pulse_items (
    source           TEXT NOT NULL,          -- 'core' or the extension name
    key              TEXT NOT NULL,          -- 1..120 chars, [a-z0-9/._:-]
    title            TEXT NOT NULL,          -- sanitized, at most 200 chars
    urgency          TEXT NOT NULL CHECK (urgency IN ('urgent', 'normal', 'low')),
    fingerprint      TEXT NOT NULL,          -- sha256 of the canonical title/state JSON
    cleared          INTEGER NOT NULL DEFAULT 0 CHECK (cleared IN (0, 1)),
    updated_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,          -- updated_at + ttl (default 24h, at most 7d)
    told_fingerprint TEXT,                   -- what the owner last heard
    told_at          TEXT,
    PRIMARY KEY (source, key)
);

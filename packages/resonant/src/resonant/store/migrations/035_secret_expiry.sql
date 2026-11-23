-- 035: secret expiry metadata (PER-313, ``resonant/secret_expiry.py``; ``store/secret_expiry.py``).
-- When each Keychain secret expires, by ref (``<ext>/<name>``, validated by ``secrets.validate_ref``).
-- Metadata only: there is no value column, ever. ``expires_at`` and ``set_at`` are ISO
-- date-times in UTC. ``source``: ``owner`` (``resonant secret set --expires…`` or ``secret
-- expiry``), ``assumed`` (a known default, e.g. 365 days for runner/claude_token) or ``probe``
-- (PER-314). No rows: nothing is watched and nothing changes.
CREATE TABLE secret_expiry (
    ref        TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('owner', 'assumed', 'probe')),
    set_at     TEXT NOT NULL,
    note       TEXT
);

-- 007: repo registry (docs/repos-plan.md D1). The repos the Claude runner may work in; it fills
-- itself as repos are used. Old `runner.repos` config entries are imported at startup.

CREATE TABLE repos (
    slug                 TEXT PRIMARY KEY COLLATE NOCASE,   -- 'owner/name'
    name                 TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- short handle; also the mirror dir
    aliases              TEXT NOT NULL DEFAULT '[]',        -- JSON list of other handles
    base                 TEXT NOT NULL,                     -- default branch, read from GitHub
    -- push: the token can push; fork: public, no push access, so PRs go through a fork.
    mode                 TEXT NOT NULL DEFAULT 'push' CHECK (mode IN ('push', 'fork')),
    ci                   INTEGER CHECK (ci IN (0, 1)),      -- a pull_request workflow exists; NULL: unknown
    ci_checked_at        TEXT,
    allow_github_changes INTEGER NOT NULL DEFAULT 0,
    quote_ask            INTEGER NOT NULL DEFAULT 1,        -- quote the ask in the PR body
    source               TEXT NOT NULL,                     -- owner | auto | created | extension:<name>
    added_at             TEXT NOT NULL,
    last_used_at         TEXT
);
CREATE INDEX repos_last_used ON repos (last_used_at);

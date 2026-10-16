-- 010: trust rules (docs/employee-plan.md D4, D5). Standing trust for Claude workers: what
-- they may do without asking, what always asks, and which events text the owner right away.

-- One rule. ``scope`` is a system, repo or area (``dori-staging``, ``trading``) or ``*``;
-- ``action`` is a kind (``deploy``, ``merge``) or, for the notify effects, an event kind
-- (``done``, ``failed``), or ``*``. Both are lowercase slugs (``store.trust.slug``).
-- ``created_from`` says how it was made: ``chat``, ``yes always``, ``command`` or ``default``.
CREATE TABLE trust_rules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL,
    action       TEXT NOT NULL,
    effect       TEXT NOT NULL
                 CHECK (effect IN ('allow', 'ask', 'notify_now', 'notify_digest')),
    note         TEXT,
    created_from TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX trust_rules_scope ON trust_rules (scope, action);

-- The default "net new" history: a (system, action) pair is seen once a check on it came
-- out ok (evaluated ok, or the owner said yes). A system with no row is a new system.
CREATE TABLE trust_seen (
    system      TEXT NOT NULL,
    action      TEXT NOT NULL,
    first_ok_at TEXT NOT NULL,
    PRIMARY KEY (system, action)
);

-- A worker's check that asked the owner (``trust.checks``) is an approval whose prompt
-- offers ``yes always``. ``allow_always``: the approval accepts ``yes always``; it is never
-- set on a ``require_code`` approval. ``always``: the owner answered ``yes always``.
ALTER TABLE approvals ADD COLUMN allow_always INTEGER NOT NULL DEFAULT 0;
ALTER TABLE approvals ADD COLUMN always INTEGER NOT NULL DEFAULT 0;

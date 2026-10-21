-- 019: what Resonant itself texted the owner (PER-268, docs/brain-plan.md "PER-268"). Code-built
-- texts (approval prompts, the result of an approved call, a run's end text, a failure) are
-- recorded once each (``ref``: the send's dedupe key) after they are sent, so both brains'
-- histories can show them as system texts: "why did that fail?" then means that failure. They
-- are built by code from cleaned parts, so they don't taint a turn on their own; a worker's own
-- words are left out of what is recorded. ``outside``: it came from a tainted task (its args
-- and report lines were chosen after outside content), so a turn that sees it is tainted as
-- after that task's reply. Rows older than 7 days are pruned as new ones are written.
CREATE TABLE system_texts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    principal TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('approval', 'result', 'run')),
    text      TEXT NOT NULL,
    ref       TEXT NOT NULL UNIQUE,
    task_id   TEXT,
    outside   INTEGER NOT NULL DEFAULT 0,
    sent_at   TEXT NOT NULL
);
CREATE INDEX system_texts_principal ON system_texts (principal, id);

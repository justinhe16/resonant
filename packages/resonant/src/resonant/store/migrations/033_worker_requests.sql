-- 033 (PER-296): a worker's `resonant request <ext>.<tool>` waits like a check, as a
-- run_questions row of kind ``request``; ``approval_id`` is the gate approval it asked for
-- (NULL until one opens, and for asks and checks), so a restart can expire it.
-- SQLite can't change a CHECK in place, so ``run_questions`` is rebuilt (the 12-step ALTER).
-- No table points at it and it points at none, so foreign keys can stay on. The columns and
-- index are 011's. The id is AUTOINCREMENT: its high-water mark is carried over.
CREATE TABLE run_questions_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_code      TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('ask', 'check', 'request')),
    question      TEXT NOT NULL,
    options       TEXT NOT NULL DEFAULT '[]',
    urgent        INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'answered', 'timed_out', 'cancelled')),
    deferred      INTEGER NOT NULL DEFAULT 0,
    answer        TEXT,
    asked_at      TEXT NOT NULL,
    answered_at   TEXT,
    delivered_at  TEXT,
    approval_id   TEXT
);
INSERT INTO run_questions_new (id, run_code, kind, question, options, urgent, status, deferred,
                               answer, asked_at, answered_at, delivered_at)
SELECT id, run_code, kind, question, options, urgent, status, deferred, answer, asked_at,
       answered_at, delivered_at FROM run_questions ORDER BY id;
DELETE FROM sqlite_sequence WHERE name = 'run_questions_new';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'run_questions_new', MAX(seq) FROM (
    SELECT seq FROM sqlite_sequence WHERE name = 'run_questions'
    UNION ALL SELECT COALESCE(MAX(id), 0) FROM run_questions
);
DROP TABLE run_questions;
ALTER TABLE run_questions_new RENAME TO run_questions;
CREATE INDEX run_questions_run ON run_questions (run_code, status);

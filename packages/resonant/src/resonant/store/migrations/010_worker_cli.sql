-- 010: the worker CLI (docs/employee-plan.md D3, EM-3): questions, notes and owner messages.
-- A run waits for one of two reasons: a usage limit (`usage_reset`) or an open question
-- (`question`). The task's own wait reason stays the loop's (`usage_reset` / `human_reply`).
ALTER TABLE runs ADD COLUMN wait_reason TEXT CHECK (wait_reason IN ('usage_reset', 'question'));
-- The worker's latest `resonant note`, shown on the run's pulse item.
ALTER TABLE runs ADD COLUMN last_note TEXT;
-- The answers and messages the current turn's prompt carried: a crash during that turn
-- resumes with them again rather than losing them.
ALTER TABLE runs ADD COLUMN resume_prompt TEXT;

-- One `resonant ask` (kind ask) or `resonant check` that needed the owner (kind check).
-- status: open (asked), answered (answer set), timed_out (ask_timeout passed, or lost to a
-- restart), cancelled (the run ended). `deferred`: the CLI stopped waiting for it, so the run
-- waits for it once the session's turn ends. `delivered_at`: when the answer (or the
-- timeout) reached the session, in a CLI reply or a resume prompt. Every transition is a
-- conditional UPDATE, so exactly one path answers and exactly one delivers.
CREATE TABLE run_questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_code      TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('ask', 'check')),
    question      TEXT NOT NULL,
    options       TEXT NOT NULL DEFAULT '[]',
    urgent        INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'answered', 'timed_out', 'cancelled')),
    deferred      INTEGER NOT NULL DEFAULT 0,
    answer        TEXT,
    asked_at      TEXT NOT NULL,
    answered_at   TEXT,
    delivered_at  TEXT
);
CREATE INDEX run_questions_run ON run_questions (run_code, status);

-- An owner message to a running worker (`deliver_message`): appended to the session's next
-- CLI reply, or the prompt of its next turn.
CREATE TABLE run_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_code      TEXT NOT NULL,
    text          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT
);
CREATE INDEX run_messages_run ON run_messages (run_code, delivered_at);

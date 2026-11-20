-- 032: registered jobs (PER-298, ``store/jobs.py``; ``resonant/registered_jobs.py``).
-- A job is cron plus a prompt: each firing starts one Claude worker run as ``created_by``.
-- Approvers add, list, pause, resume, delete and run them by plain words (``tools/jobs.py``);
-- the ``core/registered_jobs`` job fires the due ones every minute.
-- ``id`` is ``J<n>``; ``kind`` is ``schedule`` (PER-299 adds ``watch``). ``schedule`` is
-- five-field cron in ``tz``. ``output`` is ``thread`` (the thread it was added in,
-- ``channel_ref``), ``dm`` or ``room:<key>`` (``channels.slack.rooms``).
-- ``state``: ``active``, ``paused`` or ``deleted`` (kept for the record, never fired).
-- ``last_due`` is the latest due time decided (claimed before the run starts, so a due time
-- starts at most one run); ``last_run_code`` the run it started (``R12``).
-- ``memory_json`` (``{"note": ...}``, at most 4 KB) and ``seen_json`` (a capped list of keys)
-- are what the job's runs write back with ``resonant job-note``; the next run's prompt gets
-- them. Personal Resonant with no jobs writes nothing here.
CREATE TABLE registered_jobs (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'schedule',
    name TEXT NOT NULL,
    schedule TEXT NOT NULL,
    tz TEXT NOT NULL,
    prompt TEXT NOT NULL,
    repo TEXT,
    output TEXT NOT NULL,
    created_by TEXT NOT NULL,
    channel_ref TEXT,
    state TEXT NOT NULL DEFAULT 'active',
    last_due TEXT,
    last_run_code TEXT,
    last_run_at TEXT,
    last_error TEXT,
    memory_json TEXT,
    seen_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (kind IN ('schedule', 'watch')),
    CHECK (state IN ('active', 'paused', 'deleted'))
);
CREATE INDEX registered_jobs_state ON registered_jobs (state);
CREATE INDEX registered_jobs_run ON registered_jobs (last_run_code);

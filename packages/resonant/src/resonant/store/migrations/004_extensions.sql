-- 004: Phase 3 extensions. The DB is the registry of record (not extensions.yaml).

-- One row per installed extension: the approved snapshot and its pins.
CREATE TABLE extensions (
    name          TEXT PRIMARY KEY,
    source        TEXT NOT NULL,             -- repo:<name> | path:<abs dir>
    commit_sha    TEXT,                      -- git commit when the source has one
    tree_digest   TEXT NOT NULL,             -- sha256 over the installed copy
    manifest_sha  TEXT NOT NULL,
    manifest_json TEXT NOT NULL,             -- the approved manifest snapshot
    argv0_paths   TEXT NOT NULL DEFAULT '{}', -- JSON {spec name: resolved absolute argv[0]}
    status        TEXT NOT NULL DEFAULT 'enabled'
                  CHECK (status IN ('enabled', 'disabled', 'disabled_tampered')),
    installed_at  TEXT NOT NULL,
    installed_by  TEXT NOT NULL
);

-- Scheduler state per job. Core jobs use ext = 'core', so there is no FK to extensions.
CREATE TABLE ext_jobs (
    ext          TEXT NOT NULL,
    job_id       TEXT NOT NULL,
    last_due     TEXT,                       -- latest due time handled (run, skipped or missed)
    last_run_at  TEXT,
    last_outcome TEXT,
    open_task_id TEXT REFERENCES tasks (id), -- the gated run's task, while it is open
    fail_streak  INTEGER NOT NULL DEFAULT 0 CHECK (fail_streak >= 0),
    PRIMARY KEY (ext, job_id)
);

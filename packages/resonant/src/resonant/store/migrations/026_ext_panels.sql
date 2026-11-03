-- 026: PER-280 extension panels: the latest good copy per (ext, panel) and its run state.
CREATE TABLE ext_panels (
    ext         TEXT NOT NULL,
    panel_id    TEXT NOT NULL,
    data_json   TEXT,                 -- normalized {views, health, local_url, truncated}; NULL before a good run
    updated_at  TEXT,                 -- data time of data_json (author's, clamped)
    bytes       INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    last_ok     INTEGER,              -- 1/0, NULL never run
    last_error  TEXT,                 -- code-built, redacted, <=200
    fail_streak INTEGER NOT NULL DEFAULT 0 CHECK (fail_streak >= 0),
    expect_by   TEXT,                 -- next due + timeout + 120 s, set on success
    PRIMARY KEY (ext, panel_id)
);

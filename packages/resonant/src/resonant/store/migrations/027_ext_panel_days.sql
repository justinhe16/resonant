-- 027: PER-286 panel history: one archived copy per (ext, panel, calendar day in the panel's tz).
CREATE TABLE ext_panel_days (
    ext            TEXT NOT NULL,
    panel_id       TEXT NOT NULL,
    day            TEXT NOT NULL,         -- YYYY-MM-DD
    status         TEXT NOT NULL CHECK (status IN ('pending', 'ok', 'none', 'failed')),
    data_json      TEXT,                  -- normalized {views, health, local_url, truncated}; NULL unless a good copy
    bytes          INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,         -- first attempt
    attempts       INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error     TEXT,                  -- code-built, redacted, <=200
    next_try_at    TEXT,                  -- pending only: not before this (backoff); NULL now
    upload_due     INTEGER NOT NULL DEFAULT 0,  -- 1: the relay still has to receive this day
    upload_tries   INTEGER NOT NULL DEFAULT 0,
    upload_next_at TEXT,
    PRIMARY KEY (ext, panel_id, day)
);
CREATE INDEX ext_panel_days_upload ON ext_panel_days (upload_due) WHERE upload_due = 1;

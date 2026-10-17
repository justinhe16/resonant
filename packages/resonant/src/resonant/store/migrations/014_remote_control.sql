-- 014: Remote Control per worker (RC-1). The claude.ai session URL of the run's latest
-- session, from Claude Code's (undocumented) `remote_control` control request; NULL while it
-- has none (Remote Control off, refused or unanswered). Each session (a resume too) gets a
-- new URL, which replaces the old one.
ALTER TABLE runs ADD COLUMN remote_url TEXT;

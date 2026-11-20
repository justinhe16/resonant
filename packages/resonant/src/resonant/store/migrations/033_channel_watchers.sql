-- 033: channel watchers (PER-299, ``resonant/channel_watchers.py``; ``store/jobs.py``).
-- A watcher is a ``registered_jobs`` row with ``kind = 'watch'``: a Slack channel, a filter and
-- a prompt. Each matching message in ``watch_channel`` (a Slack channel id, ``C…``/``G…``)
-- starts one Claude worker run as ``created_by``. ``schedule`` is '' for a watcher.
-- ``filter_json``: ``{"contains": [...], "from": [...], "regex": "...", "ask": "...",
-- "include_replies": bool}`` (code first; ``ask`` is a yes/no from the local model).
-- Schedule rows leave both NULL. Personal Resonant with no watchers writes nothing here.
ALTER TABLE registered_jobs ADD COLUMN watch_channel TEXT;
ALTER TABLE registered_jobs ADD COLUMN filter_json TEXT;
CREATE INDEX registered_jobs_watch ON registered_jobs (kind, state, watch_channel);

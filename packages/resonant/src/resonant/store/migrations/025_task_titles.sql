-- 025: a short human title per task (PER-279), e.g. "Look up net worth", stored when the task
-- is created (``store.tasks.insert_task`` via ``store/task_titles.py``) and shown on the
-- dashboard's task rows. Code-built, never a model call; redacted before it is stored.
-- ``title_outside`` is 1 when its words came from outside text (a tainted task's question or
-- ask), so the dashboard marks it. Both are plain ADD COLUMNs: nothing points at them.
-- Existing tasks get a best-effort title from their checkpoint and first message: a Python
-- step (``task_titles.backfill``) that ``store.db.migrate`` runs right after this script, in
-- the same transaction, because the redaction pass is Python.
ALTER TABLE tasks ADD COLUMN title TEXT;
ALTER TABLE tasks ADD COLUMN title_outside INTEGER NOT NULL DEFAULT 0;

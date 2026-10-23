-- 024: the hourly pulse and the briefing are system texts too (PER-274). Before this the brains
-- only saw them as told state (pulse_items.told_at, "Already told"); now the text that went out
-- (Claude's or the code-built fallback) is recorded once per dedupe key (``ref``:
-- ``pulse:<date>:<HH>``, ``pulse:manual:…``, ``briefing:<date>``, ``briefing:manual:…``), so
-- both brains read it in the shared transcript as Resonant's own turn.
-- SQLite can't change a CHECK in place, so ``system_texts`` is rebuilt (the 12-step ALTER). No
-- table points at it and it points at none, so foreign keys can stay on. The columns and index
-- are 019's. The id is AUTOINCREMENT: its high-water mark (``sqlite_sequence``) is carried over,
-- so an id is never reused even when the newest rows were pruned.
CREATE TABLE system_texts_new (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    principal TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('approval', 'result', 'run', 'pulse', 'briefing')),
    text      TEXT NOT NULL,
    ref       TEXT NOT NULL UNIQUE,
    task_id   TEXT,
    outside   INTEGER NOT NULL DEFAULT 0,
    sent_at   TEXT NOT NULL
);
INSERT INTO system_texts_new (id, principal, kind, text, ref, task_id, outside, sent_at)
SELECT id, principal, kind, text, ref, task_id, outside, sent_at FROM system_texts ORDER BY id;
DELETE FROM sqlite_sequence WHERE name = 'system_texts_new';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'system_texts_new', MAX(seq) FROM (
    SELECT seq FROM sqlite_sequence WHERE name = 'system_texts'
    UNION ALL SELECT COALESCE(MAX(id), 0) FROM system_texts
);
DROP TABLE system_texts;
ALTER TABLE system_texts_new RENAME TO system_texts;
CREATE INDEX system_texts_principal ON system_texts (principal, id);

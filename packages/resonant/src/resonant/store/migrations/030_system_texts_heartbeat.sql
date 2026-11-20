-- 030: the hourly alive line a room gets (PER-303, ``pulse.heartbeat``) is a system text too,
-- kind ``heartbeat``, recorded once per dedupe key (``ref``: ``heartbeat:<date>:<HH>``) under
-- the room's principal (``room:<key>``).
-- SQLite can't change a CHECK in place, so ``system_texts`` is rebuilt exactly as 024 did (the
-- 12-step ALTER): same columns and index, the AUTOINCREMENT high-water mark carried over.
CREATE TABLE system_texts_new (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    principal TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('approval', 'result', 'run', 'pulse', 'briefing',
                                            'heartbeat')),
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

-- 039 (PER-325): the lookup that keeps an approval's continuation exactly-once
-- (``runners.continuation.find``: ``runner = 'ask'`` and the checkpoint's
-- ``continuation.key``) runs on every resume. Without an index it scans ``tasks``. A partial
-- expression index over the ask tasks serves it; the query's terms match the index's exactly.
CREATE INDEX tasks_continuation_key ON tasks (json_extract(checkpoint, '$.continuation.key'))
    WHERE runner = 'ask';

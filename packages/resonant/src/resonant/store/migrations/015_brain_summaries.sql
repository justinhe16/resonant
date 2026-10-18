-- 015: the brain's running conversation summary per principal (BR-4, docs/brain-plan.md D3).
-- The last few exchanges go to the model as real turns; older ones are folded, a batch at a
-- time and off the reply path, into this one summary. ``through_event_id`` is the last
-- message event folded in: history shows only the messages after it. ``source`` says who
-- wrote the summary: the model, or the deterministic fallback when the model was down.
CREATE TABLE brain_summaries (
    principal        TEXT PRIMARY KEY,
    summary          TEXT NOT NULL,
    through_event_id TEXT NOT NULL,
    folded           INTEGER NOT NULL DEFAULT 0,
    source           TEXT NOT NULL CHECK (source IN ('model', 'fallback')),
    updated_at       TEXT NOT NULL
);

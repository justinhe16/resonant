-- 036: a pulse item keeps the ``state`` its report gave (PER-302), as canonical JSON (at most
-- ``store.pulse.MAX_STATE_BYTES``), so the room digest can show a report's own ``digest`` lines.
-- NULL: no state (every row before this migration, and reports without one). The fingerprint
-- is unchanged: it already hashes the state.
ALTER TABLE pulse_items ADD COLUMN state_json TEXT;

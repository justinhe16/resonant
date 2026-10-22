-- 023: what the owner has acknowledged (PER-270). ``told_at`` says when a version of an item was
-- texted; ``acked_at`` says when the owner wrote back after that ("ok", "that's fine", a yes/no,
-- or any later message). A new told version resets it, so it always refers to the version in
-- ``told_fingerprint``. Used for "Already told" in the brain's context; never re-opens an item.
ALTER TABLE pulse_items ADD COLUMN acked_at TEXT;

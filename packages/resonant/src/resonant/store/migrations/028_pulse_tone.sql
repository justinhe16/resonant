-- 028: an optional ``tone`` on a pulse item. ``good`` marks an urgent item that is good news (the
-- urgent marker renders green instead of red). NULL, or any other value, is the usual behaviour.
-- It is display only: not part of the fingerprint, so it never changes what gets texted.
ALTER TABLE pulse_items ADD COLUMN tone TEXT;

-- 021: the expiry reminder (PER-261). A pending approval (a batch: each of its approvals) gets
-- one "[A7] still waiting, expires in 15 min" text at half its TTL; ``reminded_at`` records
-- that it went out (or that nobody it was sent to is reachable), so it is never sent twice.
-- It replaces the pulse's ``core/approval/<code>`` re-mention.
ALTER TABLE approvals ADD COLUMN reminded_at TEXT;

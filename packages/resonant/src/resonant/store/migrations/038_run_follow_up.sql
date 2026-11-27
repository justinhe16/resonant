-- 038 (PER-354): a run carries a follow-up. ``follow_up`` is a plain instruction for when the
-- run ends ("if the review says LGTM, merge the PR"): the runner then queues one brain ask in
-- the run's current thread, as its current requester, with the instruction and the run's end
-- (``ClaudeRunner._follow_up``). ``follow_up_depth`` bounds the chain: 0 for a run a person
-- started; a run started or continued by a follow-up ask gets that ask's depth, and a run at
-- ``FOLLOW_UP_DEPTH_MAX`` queues no further ask. NULL / 0 for every run before this.
ALTER TABLE runs ADD COLUMN follow_up TEXT;
ALTER TABLE runs ADD COLUMN follow_up_depth INTEGER NOT NULL DEFAULT 0;

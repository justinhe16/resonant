-- 017: start every brain summary over (BR-9). Summaries folded before BR-9 kept claims about
-- state that changes ("github secrets aren't configured", the latest PR) and the model read
-- them over the live briefing. The fold prompt now leaves those out; dropping the rows moves
-- each principal's cursor back, so history shows the recent exchanges as turns again and the
-- next fold rebuilds the summary under the new rules. Nothing else reads this table.
DELETE FROM brain_summaries;

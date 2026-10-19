-- 018: start every brain summary over (BR-10b). Summaries folded before BR-10b kept the text
-- of Claude's answers and of replies written after an untrusted read, and a summary never
-- taints a turn. The fold now keeps only a fixed marker for those exchanges; dropping the rows
-- moves each principal's cursor back, so the recent exchanges show as turns again (and taint
-- the turn while an untrusted one is among them) and the next fold rebuilds the summary under
-- the new rules. Nothing else reads this table.
DELETE FROM brain_summaries;

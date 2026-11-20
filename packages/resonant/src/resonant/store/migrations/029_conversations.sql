-- 029 (PER-294): one Slack thread is one conversation.
-- ``tasks.conversation``: the thread a task's message came from (``channel_ref.thread``); NULL
-- is the principal's own conversation (iMessage, and every task written before 029). The
-- brains' history, the "since we last spoke" delta and the system texts read by it.
ALTER TABLE tasks ADD COLUMN conversation TEXT;
UPDATE tasks SET conversation = json_extract(channel_ref, '$.thread')
 WHERE channel_ref IS NOT NULL AND json_valid(channel_ref)
   AND json_type(channel_ref, '$.thread') = 'text'
   AND json_extract(channel_ref, '$.thread') <> '';
CREATE INDEX tasks_conversation ON tasks (conversation) WHERE conversation IS NOT NULL;
-- A thread's messages, any principal (``store.brain.thread_after`` by conversation).
CREATE INDEX events_thread ON events (json_extract(payload, '$.thread'))
 WHERE json_extract(payload, '$.thread') IS NOT NULL;
-- A run keeps its thread and who asked for it: its questions and trust checks go to that
-- thread, addressed to the requester. NULL: the principal's own (the owner's) conversation.
ALTER TABLE runs ADD COLUMN channel_ref TEXT;
ALTER TABLE runs ADD COLUMN requested_by TEXT;

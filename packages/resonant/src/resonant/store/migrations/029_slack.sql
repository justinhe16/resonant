-- 029: the Slack adapter's state (PER-295, ``resonant.gateway.slack``; ``store/slack.py``).
-- ``slack_threads``: the threads Cori follows. A thread (``channel`` + its root ``thread_ts``) is
-- followed from the first message that tags her until it has been idle ``follow_idle_days``;
-- an untagged reply in a followed thread routes, and a send to a thread goes only to one listed
-- here. ``principal`` is who started it (a name, never message text).
-- ``slack_prompts``: the approval prompts posted with Approve and Deny buttons, so the message
-- can be edited once the approval is answered (any path) or expires: the buttons go.
-- ``text`` is the prompt as posted (code-built, like every approval prompt).
-- Personal Resonant never writes either table (no ``channels.slack``).
CREATE TABLE slack_threads (
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    principal TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    PRIMARY KEY (channel, thread_ts)
);
CREATE INDEX slack_threads_last ON slack_threads (last_at);

CREATE TABLE slack_prompts (
    approval_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    channel TEXT NOT NULL,
    ts TEXT NOT NULL,
    code TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (approval_id, principal)
);
CREATE INDEX slack_prompts_message ON slack_prompts (channel, ts);

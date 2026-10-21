-- 022: one request per action (PER-269). An approval names the action it asks for, when it
-- has one Resonant can name: ``action_key`` is ``<system> <verb> <target>`` (``github merge
-- jhe/dori#2``) and ``action_target`` the thing it acts on (``jhe/dori#2``). Before a new
-- approval for an action opens, the gate and the trust checks look for an open one with the
-- same key (a run's check, the brain's tool call, a job's) and point to it instead. When a PR
-- is merged or closed, the open approvals targeting it expire as ``obsolete``.
-- NULL for everything else, and for every approval made before this.
ALTER TABLE approvals ADD COLUMN action_key TEXT;
ALTER TABLE approvals ADD COLUMN action_target TEXT;
CREATE INDEX approvals_action_pending ON approvals (action_key) WHERE status = 'pending';
CREATE INDEX approvals_target_pending ON approvals (action_target) WHERE status = 'pending';

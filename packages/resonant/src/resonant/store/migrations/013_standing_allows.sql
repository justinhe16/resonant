-- 013: standing allows for chat tools (GT-1). An L2 tool approval the owner's model-chosen
-- call raised may offer ``yes always`` too: answering it saves an ``allow`` trust rule for
-- that tool (``action = 'tool:<name>'`` or ``'tool:<extension>.<name>'``) on the tool's
-- natural scope (a repo, the extension, or ``*``), so later owner chat calls run as L3.
-- ``always_scope``/``always_action``: the rule the prompt offered, fixed when it was asked,
-- so the rule saved is exactly the one shown. NULL on every other approval (a worker's
-- check builds its rule from the check itself).
ALTER TABLE approvals ADD COLUMN always_scope TEXT;
ALTER TABLE approvals ADD COLUMN always_action TEXT;

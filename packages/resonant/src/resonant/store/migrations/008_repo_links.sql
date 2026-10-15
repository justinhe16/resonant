-- 008: extension repo links (docs/repos-plan.md D7). The repos an extension's manifest names, kept
-- apart from the registry so a link can exist before the repo is resolved against GitHub. Replaced
-- on ext install/update and dropped on ext remove.

CREATE TABLE repo_links (
    extension TEXT NOT NULL COLLATE NOCASE,
    slug      TEXT NOT NULL COLLATE NOCASE,   -- 'owner/name'
    PRIMARY KEY (extension, slug)
);

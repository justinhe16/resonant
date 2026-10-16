"""The Claude runner's daemon-side pieces (docs/employee-plan.md D1): the workspace (checkouts,
worktrees and scratch dirs, the daemon-only mirror, PR discovery, merge), the pre-merge checks,
the repo resolver and creator, and cleanup. The sandbox and the ``ClaudeRunner`` itself live
elsewhere."""

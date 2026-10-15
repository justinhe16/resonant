# Repos plan: the runner works on any repo, like a person at a computer

Status: planned (2026-10-15). Builds on [`phase5a-plan.md`](phase5a-plan.md).

Phase 5a lets the runner work only on repos named in `runner.repos` in the config file. This plan removes that fixed list:

- The runner works on any repo you can push to. A repo it hasn't seen before is registered the first time it's used.
- It can create new repos.
- It can open pull requests on other people's public repos, through a fork.
- Extensions can link their own repos (for example `trading` or `dori-oncall`), so "fix the trading bot" finds the right one.
- All of this works in plain English and through slash commands.

## 1. Owner decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Claude auth | Subscription token (`runner/claude_token`), unchanged |
| 2 | Merge | Plain `yes`. Asking to "… and merge it" gets one `yes` up front, then it merges on its own once CI is green (§3 D4) |
| 3 | Approvals | Like a coworker: runs on your own repos don't ask; creating a repo asks; anything sent under your name to someone else's repo asks |
| 4 | Repo list | Nothing hardcoded. The list is in the database, fills itself as repos are used, and extensions can add to it |
| 5 | Upstream PRs | A classic `public_repo` token, `runner/github_public_token`, used only for fork and upstream-PR calls (2026-10-15) |
| 7 | Who drives a run | The Claude session: it pushes, opens and updates the PR, watches CI and fixes. Resonant starts and supervises it, and keeps merge, create, upstream PRs and starter CI (D10, 2026-10-15) |
| 8 | Session token | A per-run GitHub App installation token: one repo, 1 hour, everything except Administration (so no delete, rename, settings or protection changes). Includes Workflows unless the repo sets `allow_github_changes: false` (2026-10-15) |
| 6 | Starter CI | Grant Workflows; Resonant adds only the fixed template file (checked by hash), behind a `yes` (2026-10-15) |

## 2. Why broad tokens are acceptable here

The GitHub token never enters a Claude session. It is checked in code: `runner/workspace.py` uses it only for the daemon's own `git` and `gh` calls, through per-call files that the sandbox can't read. The session commits locally and hands its work back as a bundle. Resonant checks that work, then pushes and opens the PR itself.

A token covering all repositories, including Administration, is therefore exposed only to Resonant's own code paths, and none of those paths deletes a repo. Even so, the plan keeps these protections:
- There is no delete tool, and the router can't reach `gh repo delete`.
- `.github/**` edits are still refused unless `allow_github_changes` is on for that repo.
- Merging still needs green CI on the exact head commit (`--match-head-commit`).
- When a repo is registered, Resonant checks for branch protection on its default branch and warns if there isn't any (a warning, not a block).

## 3. Design

### D1. Repo registry (migration 007, `store/repos.py`)

A `repos` table. Each repo has:
- `slug`: `owner/name`, the primary key.
- `name`: a short unique handle, defaulting to the repo name.
- `aliases`: a JSON list.
- `base`: the default branch, read from GitHub.
- `mode`: `push` (you can push to it) or `fork` (public, you can't push, so PRs go through a fork).
- `ci`: whether a `pull_request` workflow exists, and when that was last checked.
- `allow_github_changes`.
- `quote_ask`: off by default for `fork` repos.
- `source`: `owner`, `auto`, `created` or `extension:<name>`.
- `added_at`, `last_used_at`.

Other rules:
- On startup, Resonant imports the old `runner.repos` entries as `source=owner`. After that, the config field is only read as a seed, and the docs say so.
- `RunnerWorkspace._repo` and the not-set-up / known-repo checks in `runners/claude.py` read the registry instead of the config.
- The "no repos" condition goes away. The runner is set up once both tokens exist.

### D2. Resolver (`runner/resolve.py`)

The resolver turns the words in a message into a repo, the way a person would. It tries these in order:
1. An `owner/name` or a `github.com/...` URL.
2. A registry name or alias.
3. An extension's linked repo, by extension name (`trading` → that extension's repo).
4. Your GitHub repos, listed through the API and cached for an hour, with a fuzzy name match.

If nothing matches, it replies `Which repo? <up to 3 candidates>` and asks nothing else.

When a repo is resolved for the first time, Resonant asks GitHub what access you have:
- Push access → `mode=push`.
- A public repo you can't push to → `mode=fork`.
- No access → `No access to owner/name.`

It then registers the repo with `source=auto` and says `(new repo: owner/name)` in the reply. Registering doesn't ask; it's L3.

### D3. Fork mode (`runner/workspace.py`)

For a `fork` repo:
- Resonant creates or reuses your fork, pushes the branch to the fork, and runs `gh pr create --repo upstream --head you:branch`.
- The run stops at `ready` and asks: `[A4] open a PR on owner/name: "<title>" (+12 −3, 2 files)? Reply yes / no`. It's outward-facing and goes out under your name.
- A `fork` repo is never merged by Resonant; `/merge` replies `owner/name is upstream; its maintainers merge.`
- There's no CI requirement. Their CI runs on their side.

### D4. Merge on green (`runners/claude.py`)

- `start_run` gets `merge: bool`. With `merge=true` the start is L2, so the "heads-up" is one plain `yes` before the run starts.
- When the run reaches `ready`, a watcher polls the checks. It merges once they're green on the head commit, if nothing was flagged, and texts `R3 merged: PR #12`.
- It stops and asks with the usual `/merge` path instead if CI fails, sensitive paths were flagged, or the wait passes 2 hours.
- This only applies to `push` repos.

### D5. CI rule (changed from Phase 5a)

- Runs on a `push` repo without CI are allowed: the PR opens, and the reply says `no CI`.
- Merging such a repo is refused: `owner/name has no CI; add a pull_request workflow first.` How CI gets added depends on §6 Q2.

### D6. Create repo (`create_repo`, L2)

- Arguments: `name`, `description`, `private` (default true). This is plain `gh repo create` under your account.
- The new repo gets a starter CI workflow (depends on §6 Q2) and branch protection on `main`. It's registered with `source=created`.
- No tool deletes, renames or archives a repo.

### D7. Extension links (SDK manifest)

- Manifest v1 gets an optional `repos: ["owner/name", ...]` field. It's additive, so existing manifests still load.
- On `ext add` or `update`, each linked repo is registered with `source=extension:<name>`, and the extension name becomes an alias.
- On `ext remove`, those links are dropped. A repo you also used yourself stays registered.
- `/ext list` shows the linked repos.
- Follow-up, not part of this plan: the github extension's watch could cover registry repos by default.

### D8. Router and tools

- The `run_project` intent becomes `repos`. Its description says it covers code work, repos and runs.
- Tools:
  - `start_run(repo, ask, merge?)`: L3, or L2 when `merge` is set.
  - `list_runs`, `cancel_run` and `merge_run`, unchanged.
  - `list_repos`: L0.
  - `add_repo(repo, name?)`: L3.
  - `forget_repo(repo)`: L3. It only removes the repo from the registry.
  - `create_repo`: L2.
  - The repo argument is a free string again, checked by the resolver. The intent's prompt gets the 20 most recently used repos plus the extension links, so the labeler and the model see real names.
- Slash commands: `/repos`, `/repo add <x>`, `/repo forget <x>`, `/repo new <name>`, `/run <repo> <ask>` (unchanged), and `/run <repo> <ask> --merge`.
- `test_command_parity` is updated to match.
- Few-shots and eval rows:
  - "add my trading repo"
  - "work on github.com/foo/bar: fix the typo in the README"
  - "make a new repo called scratch"
  - "fix the flaky test in resonant and merge it"
  - "any updates on my PRs" → `updates` (fixes the known eval miss)

### D9. Upkeep

- The daily `core/run-cleanup` job also removes mirrors not used for 30 days.
- The registry is never pruned automatically.

### D10. Claude drives the run (GitHub App token)

- **Owner step:** create a GitHub App once. It's installed on your account for all repos, with these permissions: Contents, Pull requests, Issues and Workflows read/write; Actions read/write (logs and re-runs); Checks, Statuses and Metadata read; **no Administration**. Its private key goes in the Keychain as `runner/github_app_key`, and its app ID and installation ID go in the config.
- **Per run:**
  - The daemon signs a JWT with the key, then mints an installation token limited to `repositories=[that repo]` with the permissions above. Workflows is dropped when `allow_github_changes` is false.
  - The token goes into the run's 0600 credential file and gh `hosts.yml` (Phase 5a plumbing). It never goes into env or argv.
  - Tokens last 1 hour, so a run longer than 50 minutes gets a fresh one written to the same files.
  - The token is revoked (`DELETE /installation/token`) when the run ends, is cancelled, or `/kill` fires.
- **Session prompt:** work on a branch `resonant/R<n>-…`, push it, open a PR, wait for CI, fix failures and push again, and stop when CI is green or after N fix attempts. Never push to the base branch; branch protection blocks it anyway.
- **Resonant keeps:**
  - Supervision: starting runs, the kill switch, budgets, the time limit, the reaper and the texts you get.
  - Merge: your `yes`, or merge once CI is green (D4). Before merging it runs the Phase 5a checks on the PR's diff: tokens in the history, `.github` changes noted in the merge prompt, and sensitive paths.
  - Creating repos, upstream PRs (fork mode stays daemon-driven with the `public_repo` token) and starter CI.
- **Lost compared with Phase 5a:** the checks run before the merge, not before each push.
- **Risk:** a leaked token reaches one repo for at most 1 hour. With Workflows, it could also reach that repo's Actions secrets through a pushed workflow. Turn Workflows off for repos with valuable Actions secrets.

## 4. Tickets and waves

| Ticket | Scope | Wave |
|---|---|---|
| RP-1 | Migration 007, `store/repos.py`, the config import, `runner/resolve.py` (GitHub calls behind an interface with a fake), and the workspace and `claude.py` reading the registry | 1 |
| RP-8 | GitHub App token minting, refresh and revoke; the session pushes and drives the PR; harvest and push removed for `push` repos; checks moved to before merge (D10) | 2 |
| RP-2 | Fork mode (D3) and its upstream-PR ask | 3 |
| RP-3 | Merge on green (D4), the CI rule change (D5) and the pre-merge checks | 3 |
| RP-4 | `create_repo` (D6), branch protection on create, and the protection check on registration | 2 |
| RP-9 | Sandbox: deny reads of secret files outside the run dir (`.env`, `.env.*`, `*.pem`, `id_*` SSH keys, `~/.aws`, `~/.config/gh`, `~/.netrc`, …) plus a `runner.deny_read` config list; new probes | 2 |
| RP-5 | Extension `repos` in the SDK manifest, plus install/remove hooks and `/ext list` (D7) | 2 |
| RP-6 | Router: the `repos` intent, the tools and slash commands, resolver wiring, few-shots, the parity test and eval rows (D8) | 4 |
| RP-7 | e2e tests (fake `gh` for fork, merge on green and create), go-live step 14 rewrite (App setup), `test.md`, `architecture.md` and the artifact | 5 |

Every ticket follows the usual process: a worktree, a PR, a code-reviewer pass, then a squash merge.

## 5. Exit criteria

These are end-to-end tests with a fake `gh` and a real sandbox:
- `/run owner/new-repo fix x` on a repo Resonant hasn't seen registers it, runs, and opens a PR, with no question asked.
- "fix the bug in the trading bot" resolves through an extension link.
- A run on a `fork` repo ends with the upstream ask. `yes` opens the PR from the fork. `/merge` is refused.
- `start_run(merge=true)` asks once, merges on green, and stops on red or when a sensitive path is flagged.
- `create_repo` asks, creates a private repo with protection, and registers it.
- Old `runner.repos` config entries still work after the import.
- No token appears in the env, argv, logs or the DB (the Phase 5a scans extended to the new paths).

## 6. Questions (answered 2026-10-15: Q1 yes, Q2 option a)

**Q1. Upstream PRs need a second token.**
GitHub's fine-grained tokens can't open PRs on repos outside your own account. RP-2 will check this against the GitHub docs. If it holds, upstream PRs need a classic token with only the `public_repo` scope, stored as `runner/github_public_token` and used only for forking and upstream PRs. That scope can also write to all of your public repos, but like the main token it never leaves the daemon.

**Q2. CI files need the Workflows permission.**
Pushing `.github/workflows/*` needs the Workflows permission. Phase 5a left it out on purpose, as defense in depth. The options:
- (a) Grant Workflows. Resonant can then add starter CI to new repos and to repos without CI, but only through that one fixed template file (checked by hash), always behind a `yes`. Session-made `.github` edits stay refused.
- (b) No Workflows. New repos are generated from a template repo that already has CI; this still needs checking. Existing repos without CI need you to add CI by hand.

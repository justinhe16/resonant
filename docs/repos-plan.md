# Repos plan: the runner works on any repo, like a person at a computer

> **Historical.** RP-1, RP-4, RP-5, RP-8 and RP-9 shipped. RP-9's secret-file name rules were later dropped by the thin guard (EM-1), and the remaining waves (RP-2, RP-3, RP-6, RP-7, the RP-10 grants) never shipped as planned: [`employee-plan.md`](employee-plan.md) replaced them (fork mode in EM-7, the router in EM-8). For what exists now, read `employee-plan.md`, [`architecture.md`](architecture.md) and [`interfaces.md`](interfaces.md).

Status: RP-1, RP-4, RP-5, RP-8 and RP-9 are merged. The remaining waves are superseded by [`employee-plan.md`](employee-plan.md) (2026-10-15). Builds on [`phase5a-plan.md`](phase5a-plan.md).

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
- Links live in their own table, `repo_links(extension, slug)` (migration 008), not in the registry, so a link can exist before the repo is resolved against GitHub. `ext add` or `update` replaces the extension's links; `ext remove` drops them. Registry rows for repos you used stay.
- Resolver step 3 maps an extension name to its linked slug, only while the extension is enabled. One link resolves like that slug, several are ambiguous. The repo is registered on first use with `source=auto` (a repo can be linked by several extensions and used directly, so one `source` can't describe it), and the reply says `(new repo: owner/name via <ext>)`. A linked repo you can't push to is refused (`NotPushable`) before any row is written.
- A link changes what "work on <ext>" does, so the install and update confirmations list each link under Escalations, flagged when the repo isn't in your registry. Update diffs show added and removed links.
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
- **Risk:** a leaked token reaches one repo for at most 1 hour. With Workflows, it could also reach that repo's Actions secrets through a pushed workflow. Turn Workflows off for repos with valuable Actions secrets. The RP-8 review also noted that a `pull_request` workflow pushed from a same-repo branch already runs with those secrets before anything is flagged at merge, so `allow_github_changes` defaulting to true is a real widening; keeping that default (and `issues` and `actions` write in the token) is the owner's call.

**As built (RP-8).**
- `runner/github_app.py` mints and revokes the token with `httpx` and `cryptography` (RS256 JWT, 9 minutes; the key is read from the Keychain for each call and never kept). Config is `runner.github_app: {app_id, installation_id}`. The key may be a PEM, or its base64 or hex on one line. The owner stores it with `resonant secret set runner/github_app_key --from-file <the downloaded .pem>` and then deletes the `.pem`. `SecretStore.set` now runs `security -i` and feeds the `add-generic-password` command on stdin, so no value is in argv (the items are still created by `/usr/bin/security`, so the ACL and prompt-free reads are as before). `security -i` reads a line of about 4 KB (4000 characters worked, 8000 did not, and an over-long line is cut and stored without its keychain argument), so a value may be up to 3500 characters once stored; a value with spaces, quotes or newlines is stored as `resonant-b64:<base64>` and decoded on read. A 2048-bit PEM (what GitHub issues) is 2.3 KB stored; a 4096-bit one does not fit.
- The session starts with a 0700 `creds/` dir in its run dir: `token` (0600), `credential-helper` (answers for `github.com` only) and `gh/hosts.yml`. `prepare` sets `credential.helper` in the clone's `.git/config` (origin was already `https://github.com/owner/repo`) and the session env gets `GH_CONFIG_DIR` (a path). `gh` is linked into the run-local `bin/` from `runner.gh_path`. The daemon writes `creds/` only through directory fds with `O_NOFOLLOW` and a rename, so a link planted by the session redirects nothing (the session can swap `creds/` for a dir of its own, which gains it nothing: it holds the token already). A fresh token replaces the files after 50 minutes and the previous one is revoked at once, so only one token is ever on disk (a `gh` call already running with the old one fails once; the prompt says to run it again). Every token is revoked, with the dir removed, when the session ends, waits, fails, times out, or is cancelled or killed; revoking is tried three times with backoff, and if it still fails the reply says `token not revoked, expires HH:MM`. Minting also retries a 5xx or timeout, and the reply is checked (exactly one repository, no permission beyond the request, contents and pull requests as asked) or the token is revoked and the run fails. At startup every `<work_dir>/R*/creds/token` is revoked and deleted, whatever state its run is in. A token minted but not yet written when the daemon died is nowhere to be found and expires within the hour.
- Sandbox: the profile allows the network, so git and gh reach github.com. Other runs' dirs are denied by an explicit last-word rule on the new `WORK` parameter (`runner.work_dir`): everything under it is unreadable and unwritable except this run's dir, the cache and the read paths (and the ancestors of those, for `stat`), wherever the work dir sits. `profile_hash`, the self-check, the probe `work dir and sibling run dirs` (which now also reads a sibling's `creds/token`) and a test with a work dir outside `$HOME` cover it.
- The session's prompt carries the branch, the base and N=3. After it ends the daemon finds the PR with `gh pr list --head` (a fork PR is ignored, an open one wins) and checks it as a merge would. The saved step is still called `harvest` (the column's CHECK list has no new value). No PR and nothing pushed is `no_changes`. Deviation: a pushed branch with no PR fails the run ("pushed X but opened no PR"), since a `ready` run with no PR can't be merged. The bundle/harvest/push code in `Workspace` stays for fork mode (RP-2).
- The pre-merge checks live inside `Workspace.merge`, the only merge path (`merge_run` and any later merge-on-green call it): `gh pr view` (open, same repo, the run's branch, the repo's base, head equal to the head the run recorded), the branch is fetched into the mirror with fsck, `run_checks` runs over `merge-base..head`, the PR title and body are scanned for tokens, then CI, then `--match-head-commit`. With `allow_github_changes` on, `.github/**` paths are flagged (listed in the done reply, the merge prompt and the merge reply); off, they are refused. The same checks run once when the run ends, so the done reply and the merge prompt know the flagged paths; a refusal there leaves the run `ready` with the reason in the reply, and `/merge` refuses again. A PR whose head moved after the run finished is refused rather than merged.
- Replies: `R3 done: PR #12 (CI green|red|pending) · url · N files · 12m`, with `no CI` for a PR with no checks and `CI unknown` when gh can't be read; the CI word is looked up when the reply is sent.
- `repos.add` now defaults `allow_github_changes` to true (new registry rows). Migration 007's column default stays 0: SQLite can't change a default without rebuilding the table, and every insert states the value. `RunnerRepo.allow_github_changes` (the config seed) keeps its default of false, since a seeded entry is the owner's explicit statement. No migration was needed for RP-8 (009 is unused).
- Not set up: the claude token, `runner.github_app`, `runner/github_app_key` and the daemon's `runner/github_token` (still used for the mirror, the resolver, PR lookups, CI reads and merges). `scripts/go_live_check.py` gained rows `runner github app` and `runner secret runner/github_app_key`.
- Base branch: at start, if the base branch isn't protected (`GET /repos/{o}/{r}/branches/{base}` with the daemon token) the start reply says `main isn't protected — the session could push to it directly.` After the session, for an unprotected (or unknown) base the repo's activity feed (`GET /repos/{o}/{r}/activity?ref=refs/heads/{base}`) is read for pushes by the app's bot (`<slug>[bot]`, from `GET /app`) since the run began; if there are any the run fails, the owner is texted `R3 pushed directly to main: <shas>` and the run is never merged; if the feed can't be read the run fails too. A protected base is not checked. The real fix is an owner step: a ruleset or branch protection on the base that requires a PR and doesn't list the app as a bypass actor.
- Limits: the session can delete branches or tags and push any branch (contents:write is inherent to the design), and `/cancel` leaves whatever it already pushed or opened on GitHub. `gh pr create` and `gh pr checks --watch` with an installation token are untested against the real CLI; `scripts/go_live_check.py` lists both as owner steps, next to base-branch protection.
- Review (RP-8): the reviewer pointed out that with Workflows and contents write a session can push a workflow that runs on `pull_request` and reads the repo's Actions secrets, and that flagging it at merge is too late because the workflow already ran. `allow_github_changes` defaulting to true for new repos, and `issues`/`actions` write in the token, are the owner's decision; turn `allow_github_changes` off per repo where Actions secrets matter.

**As built (clone on add, 2026-10-18; a later ticket that reused the RP-1 code).** Adding a repo used to only write its registry row: `repos.path` stayed NULL and nothing was cloned until a run, while the reply read as if the repo were there. Now `add_repo` (and `/repo add`) and `create_repo` call `Workspace.owner_checkout` after registering:
- It clones into `<runner.code_dir>/<name>` with the run path's `ensure_checkout`: the plain https URL, the daemon's GitHub token (when set; a public repo clones without it) given to that one `git clone` by a credential helper in a throwaway 0600 file, never in the clone's `.git/config` or remote URL; the clone lands in a temp dir and is renamed in. It holds the repo's checkout lock and has 120 s (`CLONE_TIMEOUT_S`); the git process is killed and the temp dir removed when that runs out.
- An existing checkout whose `origin` is the repo is used as is (`checked out at …`). Anything else at the path is left untouched and the reply says so. On success `repos.path` is set, and a run's first clone (`prepare`) now records it as well.
- A failed or cut-off clone leaves the repo registered; the reply says the clone failed and that the first run will try again (it does: `prepare` clones a missing checkout). A repo GitHub reports as over about 2 GB (`GitHub.size_kb`, `CLONE_MAX_KB`) is left to the first run.
- Fork repos aren't cloned at add time: their checkout is the owner's fork (`origin` the fork, `upstream` the original), which the first run makes with the `public_repo` token; a clone of the original at that path would be refused by that run.
- The 120 s covers waiting for the checkout lock too; a run holding it (setting the checkout up) is reported as such, not as a slow clone. A path over Resonant's own dirs is refused up front. The size lookup has 10 s and isn't made for `create_repo` (a new repo is tiny).
- Recording `repos.path` (rather than leaving NULL for the default place) is deliberate: a renamed repo keeps its checkout instead of cloning afresh under the new name. A later change of `runner.code_dir` doesn't move recorded checkouts, and a `runner.repos` entry restates its own `path` at every start (unset: NULL, the default place), as before.
- Adding a registered repo that has no checkout (like one added before this change) clones it, so "add X" again is the fix. Replies: `Added mycelium (justinhe16/mycelium, base main, push): cloned to ~/code/mycelium.` `list_repos` carries each repo's `path` (or `not cloned`) and `/repos` shows it, so "where is X" needs no tool of its own.

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

## Carry-overs between tickets

- **For RP-6:**
  - Wire a way to merge the add-CI PR. It isn't a run, so `merge_run` can't reach it. `Workspace.merge` already allows it when the PR adds only the starter CI file. A good shape is a `merge_ci_pr(repo)` tool or a `/repo ci <repo> merge` command, with a plain `yes`.
  - Surface `Resolved.note`, which carries the `via <ext>` text, and `Resolved.warning` in the `start_run` and `add_repo` replies.
- **For RP-7:** confirm `create_repo` against live GitHub: `gh repo create --add-readme`, then the CI file written to the default branch, then protection. It has only been tested with the fake `gh`.

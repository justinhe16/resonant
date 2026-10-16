# Employee plan: Resonant works like a coworker at the Mini

Status: draft for owner review (2026-10-15). This replaces the remaining waves of [`repos-plan.md`](repos-plan.md) (RP-2, RP-3, RP-6, RP-7) and the RP-10 "grants" idea. What's already merged stays: RP-1, RP-4, RP-5, RP-8 and RP-9.

## 1. The model in one paragraph

You text Resonant like an employee. The **local brain** (the small local model, unchanged) reads every message, answers what it can and routes the rest to tools. When work needs real engineering or operations, it **spawns a Claude worker** and replies with a short ack (`On it: R4`). Workers run like you at the keyboard: in your real checkouts, with your `.env` files, Docker, network and the whole disk. A **thin guard** only protects Resonant itself.

When a worker has a question, it pauses. The brain texts you the question, and your answer goes back to that worker. Several workers and conversations run at once.

By default, **standing trust** applies: workers act and report in the hourly digest. They ask first only for something **net new**. You shape that over chat: "don't ask me about this again", "always ask before merging in trading".

## 2. Owner decisions (2026-10-15)

| # | Decision | Choice |
|---|---|---|
| 1 | Wall around workers | **Thin guard** (D2). Everything is open except Resonant's own state, daemon, approvals, iMessage, auto-start entries and the Keychain |
| 2 | GitHub identity | Keep the **GitHub App**: per-run tokens, PRs as `app[bot]` (RP-8, merged) |
| 3 | Where work happens | **Real checkouts** under `~/code/<repo>`, a git worktree per run |
| 4 | Keys | **`.env` in each repo checkout**. Workers read them like you would |
| 5 | Approvals | **Standing trust**, except net new: a new system, a new kind of action, anything irreversible, anything that costs money. Rules are taught over chat |
| 6 | Reporting | **Hourly digest** by default; each event carries an urgency that can raise it to an immediate text |
| 7 | Brain | The **local model** routes everything; Claude only as workers. Worker questions are relayed through the brain |
| 8 | Trading limits | Enforced by the exchange account and the repo, not by Resonant |

## 3. Design

### D1. Workers (`runners/claude.py`, evolving from Phase 5a)

- **Spawned by** the `start_run` tool, from plain words or `/run`. The ask can be anything: code, ops, research, a DB fix.
- **Workspace:**
  - The repo's checkout is `~/code/<name>`. It is cloned on first use and `git fetch`ed before each run.
  - Each run gets a worktree at `~/code/.worktrees/<name>/R<n>` on branch `resonant/R<n>-…`.
  - The checkout's `.env` files (and other gitignored config such as `.env.local`) are symlinked into the worktree, so the run sees your real setup.
  - Asks that aren't tied to a repo (ops, research) get a scratch dir `~/code/.scratch/R<n>`.
- **GitHub:** the per-run App token from RP-8 (one repo, 1 hour, no admin). The session drives the PR: push, open, watch CI, fix.
- **Concurrency:** up to `claude.max_concurrent` workers, 5 today. Two runs on one repo are fine, since each has its own worktree.
- **The worker prompt says:** "you are Resonant's worker for <owner>"; the ask; where keys live (`.env`); how to ask the owner a question (D3); how to check an action against the trust rules (D4); and how to report.
- **What carries over from Phase 5a:** kill and cancel, budgets and time limits (configurable per repo), transcripts, usage-limit waiting, the reaper, pulse progress items and resume after restart.
- **Removed:** fresh clones per run, the bundle handback, the "no secrets in env" rule for workers, and the deny-all-of-home sandbox profile.

**As built (EM-2).**
- **Config and registry.** `runner.code_dir` (default `~/code`; it may not overlap `work_dir`, the mirror or the secrets dir). Migration 009 adds `repos.path`, the per-repo override (seeded from a `runner.repos` entry's `path`; `repo_store.set_path`). It also rebuilds `runs` (the AUTOINCREMENT high-water mark is carried over, so codes are still never reused) to add the `done` status and two columns: `workspace`, the worktree or scratch dir, and `checkout`.
- **Checkouts.** The checkout is `<path>` or `<code_dir>/<name>`. When nothing is there it is cloned from the plain `https://github.com/owner/name`, with the daemon's token given to that one call by credential helper. The clone goes into a temp dir beside the final place and is renamed in, so a cancelled clone leaves nothing in the way. An existing checkout must be a git top level with a real `.git` directory (not a linked worktree or submodule) whose `origin` is the repo (https, ssh or scp form). Anything else fails the run with "… is in the way", "… isn't a plain checkout" or "…'s origin isn't owner/name". A `path` overlapping Resonant's own dirs (run, mirror, secrets and cache dirs, `.worktrees`, `.scratch`) is refused. Before each run the daemon fetches only `+refs/heads/<base>:refs/remotes/origin/<base>`, from the repo's URL. It never checks out, resets or changes the config of the checkout, and never moves the owner's own branches. Daemon git in a checkout also turns off fsmonitor, `core.sshCommand`, auto-gc and maintenance, and submodule recursion, and allows only the https and file protocols, on top of the usual no-hooks, no-global-config env.
- **Worktrees.** `git worktree add --no-track -B resonant/r<n>-<slug> <code_dir>/.worktrees/<name>/R<n> <origin/base sha>`. The branch keeps RP-8's lowercase `r<n>`. A per-repo `asyncio.Lock` (`Workspace.checkout_lock`) serializes clone, fetch, worktree add and remove, so two runs on one repo get one worktree each, one after the other. The runner's old one-run-per-repo lock is gone; only `max_runs` limits sessions.
- **Local config.** Top-level `.env`, `.env.*` (not `.example`, `.sample` or `.template`), `.envrc`, `.tool-versions`, `.python-version`, `.node-version` and `.mcp.json` are symlinked into the worktree with absolute targets. A file is linked only if it is a regular file or a link, the checkout doesn't track it (`git ls-files`), and the worktree doesn't already have it. The linked names go in `<run>/git-exclude`, which the session's git uses as `core.excludesFile`. So `git add -A` never commits a link, even one the checkout doesn't gitignore.
- **Session credentials.** RP-8's `creds/` dir stays in the run dir (`~/resonant-runs/R<n>`, with `home/` and `tmp/`). The session finds its credential helper through `GIT_CONFIG_COUNT`/`KEY`/`VALUE` in its environment: helpers reset, then `!<run>/creds/credential-helper`, plus `url.https://github.com/.insteadOf` for `git@github.com:` and `ssh://git@github.com/`, so an ssh origin also pushes with the run's token. Nothing about the token is written to the checkout's config, and `extensions.worktreeConfig` isn't needed. The owner's own pushes from the checkout are unchanged. The session's cwd is the worktree. The reaper finds the session's processes by its env marker; it does not match by a cwd in the worktree, because the owner may have an editor or a dev server open there.
- **No repo.** `start_run(repo=None)` (the tool's `repo` is now optional) gets `<code_dir>/.scratch/R<n>` (0700), no App token, no `GH_CONFIG_DIR` and a scratch prompt. It ends `done` with `R4 done (12m): <Claude's summary>`. The `/run` command still takes a repo; EM-8 owns the router.
- **Removed.** The throwaway clone in the run dir, the bundle handback (`harvest`, `import_bundle`, `check`, `publish`, `_copy_bundle`, `BundleRefused`, `PublishAborted`) and `_with_real_git`. `runner.max_bundle_mb` is unused but still accepted, so old configs load. Fork mode stays refused at `start_run`.
- **Mirror kept, for the checks only.** The pre-merge checks (`check_pr`, inside `Workspace.merge`: fetch the PR head with fsck, `run_checks`, the PR-text scan, CI, `--match-head-commit`), `pushed_commits` and the no-CI recheck still read the daemon-only bare mirror, never the checkout. A worker can write its checkout's `.git` (config, refs, objects, alternates), so a merge decision must not rest on it. The mirror is now fetched only by those calls, not at prepare. At prepare, CI presence is read from the checkout at the base sha. That reading can only lower the registry's `ci` (or set it when unknown); a repo recorded as no-CI is raised only by the mirror, when a merge looks again.
- **Cleanup.** `Workspace.remove_workspace` runs on `/cancel` and `/kill`, on merge, and from the daily job for runs that ended more than `keep_days` ago. It acts only on the exact recorded path, and only when that path is `<code_dir>/.worktrees/<name>/R<n>` or `<code_dir>/.scratch/R<n>` for that run's code, with no symlink between `code_dir` and it. Otherwise it refuses. For a worktree it runs `git worktree remove --force --force`. If git can't remove it (say the checkout moved), the dir is deleted with `rmtree_nofollow`, so linked `.env` targets are kept. Then only that worktree's admin entry (`.git/worktrees/<id>` whose `gitdir` is the worktree's) is dropped. There is no repo-wide `git worktree prune`, which could drop the owner's worktrees on an unmounted volume. The run's own `resonant/r<n>-…` branch is deleted by compare-and-delete (`git update-ref -d <ref> <sha>`). The expected sha is the run's PR head, or its base when nothing was pushed. The delete happens only when no worktree has the branch checked out. So a branch the owner checked out or committed to is kept. After a successful removal the run's `workspace` and `checkout` are cleared, so the daily job doesn't retry. The checkout locks are shared per event loop by every `Workspace`, so the daily job serializes with the runner's. A checkout is never deleted. Run-dir cleanup is unchanged.
- **Sandbox.** `SandboxParams.write_paths` (one placeholder in `runner.sb`) lets the old profile read and write the worktree and the checkout's `.git`, or the scratch dir, until EM-1's thin guard replaces it. Under the old profile, the secret-file name rules still hide the checkout's `.env` behind its link; the thin guard drops those rules.
- **Accepted risk: the checkout's `.git` is worker-writable.** The session needs that to commit, since objects and refs live there. Under the thin guard it can also write anything else in `.git`:
  - `hooks/*`, which run on the owner's next commit or checkout.
  - The owner's `refs/heads/*` and `HEAD`.
  - `config`, which daemon git still reads. A smudge filter would run on the daemon's next `worktree add`. `http.proxy`, `http.sslVerify` or `http.<url>.*` would apply to the daemon's token-carrying fetch.

  Daemon git there ignores hooks, fsmonitor, `sshCommand`, auto-gc and submodule recursion, and allows only https and file protocols. The rest is the honor-system trust of D4, the same as a coworker at the keyboard. The thin guard (D2) protects Resonant, not checkouts. A later hardening could fetch the base into the checkout from the daemon-only mirror (file protocol, no token) and deny writes to `.git/hooks`.
- **Not done.** A renamed repo's checkout isn't moved; the new name clones afresh unless `path` is set. The checkout's `.git` growth isn't under the disk watchdog (the worktree and the run dir are). A run left mid-session by a build from before EM-2 fails on resume with "started by an older version of Resonant; start it again". `go_live_check` gains a `runner code dir` row; the CI row still reads the mirror.

### D2. Thin guard (`sandbox/`, replaces the Phase 5a profile)

An allow-default Seatbelt profile. Workers can do anything except:
- **Resonant's state:** write to `~/.resonant/**` (DB, config, logs, run secrets), the installed daemon code and its LaunchAgent plists. Reads are allowed except for `run-secrets`.
- **The daemon and kill switch:** signals to processes outside the worker's own sandbox, and `launchctl` changes to Resonant's jobs.
- **iMessage:** Messages data (`~/Library/Messages`), plus Apple Events to Messages and other apps. The worker can't text anyone as you.
- **Auto-start:** LaunchAgents and LaunchDaemons, login items, crontab, and shell rc files.
- **Keychain:** the securityd services are blocked, so workers can't read Resonant's secrets, including the GitHub App key. Keys come from `.env`. The Claude token keeps reaching the session as it does today.
- **Resonant's own repo:** runs on `resonant` work in a worktree. The live install is a separate copy, so a run can't edit the running daemon. Deploying Resonant remains a Resonant action (`/update`).

What goes away: the home-folder deny, the secret-file name rules (RP-9), the cross-run deny and the port denies. `resonant sandbox check` shrinks to the guard's probes.

### D3. Questions and conversation relay

- Workers get a small `resonant` CLI on their `PATH` (talking to the daemon over a per-run socket, the run's only way in):
  - `resonant ask "<question>" [--options a,b,c] [--urgent]`: blocks until you answer.
  - `resonant note "<progress>"`: adds to the run's pulse item.
  - `resonant check …` (D4).
- **Asking:**
  - The run goes to `waiting(question)`, a new wait reason, and the session stays alive.
  - The brain texts `R4 asks: <question>`, with options if any.
  - The question's urgency is normal by default, so it's sent right away (it's blocking work). It stays open, and the hourly digest repeats unanswered questions.
- **Routing your reply:**
  - A reply that starts `R4 …`, or a reply while R4's question is the only one open, goes to R4.
  - If several questions are open, the brain picks by content or asks "for R4 or R6?". This uses the same threading as approvals, extended to free-text answers.
- **Timeouts:** after `ask_timeout`, 4 hours by default, the worker gets "no answer; use your judgment or stop", and the transcript records it.
- **Talking to a running worker:** "tell R4 to also update the README" goes to that worker as a message. The session sees it at its next `resonant ask`/`note` call, or the run resumes with the message after a stop. The hourly digest lists running workers.

### D4. Trust rules (standing trust, net-new asks, taught over chat)

- **What workers check:** before an action that could be net new, the worker calls `resonant check --system <s> --action <kind> [--irreversible] [--cost <usd>] "<what>"`, and gets back `ok` or `ask`. For `ask`, the CLI blocks for your `yes`. The prompt lists what to check: deploys, prod data writes, outgoing emails and messages, spending, deleting data, force-pushing, and any system it hasn't used before.
- **Rules:** stored in a `trust_rules` table (`scope`, `action`, `effect`, `note`, `created_from`):
  - `scope` is a system, repo, area or `*`;
  - `action` is a kind, or `*`;
  - `effect` is `allow` or `ask`.
- **Defaults:**
  - Asks for a new system (no prior `ok` on it), a new kind of action on a system, anything irreversible, and anything that costs money.
  - Everything else is `ok`.
- **Taught over chat:** "in the future don't ask me about deploys to dori-staging" adds `allow`; "since trading is sensitive, ask before merging there" adds `ask merge`. The brain proposes the rule and confirms it in one line (`Rule added: ask before merge in trading`). `/rules` lists them; "forget that rule" removes one.
- **Answering a net-new ask:** `yes` allows it once; `yes always` allows it and adds the rule.
- **Merge:** a merge is an action like any other. It's `ok` by default once CI is green; a rule such as `ask merge` on a sensitive repo makes it ask. Resonant's pre-merge token-leak scan stays, because it's cheap and blocks only on real secrets.
- **Trust model:** this is honor-system by design. The worker is trusted to call `check`, like a coworker. The thin guard is the hard floor.

**As built (EM-4).**
- **Store.** Migration 010 adds `trust_rules` (`id`, `scope`, `action`, `effect` in `allow|ask|notify_now|notify_digest`, `note`, `created_from` in `chat|yes always|command|default`, `created_at`) and `trust_seen(system, action, first_ok_at)`, plus `approvals.allow_always` and `approvals.always`. `store/trust.py` is the CRUD. Scope, system and action are slugs (lowercase, whitespace as `-`, `[a-z0-9._:/@-]`, at most 64) or `*`, so no free text reaches a prompt through them. One rule per (scope, action) per family: "don't ask about X" replaces an earlier "ask before X", and the reverse.
- **Evaluation** (`resonant/trust/policy.py`, pure; `resonant.trust.evaluate` reads the store and audits `trust.evaluate`). The most specific matching `allow`/`ask` rule decides, and overrides every default. Scope comes first: the system exactly, then the run's repo or an area (a leading segment: `trading` covers `trading-api`, not `tradingview`; the longer scope wins), then `*`. Then the action: exact, then `*`. At equal specificity `ask` beats `allow`. With no rule: ask for a new system (no `trust_seen` row), a new kind of action on it, irreversible, or a cost above 0 (non-finite counts); the reason lists all that apply (`new system, irreversible`). An ok check, or the owner's `yes`, marks (system, action) seen. So a plain `yes` also makes the next identical, reversible, free check ok (it isn't net new any more); `yes always` is what keeps irreversible or paid actions from asking.
- **`request_check`** (`resonant/trust/checks.py`, `IMessageStack.trust`), for EM-3: `await TrustChecks(...).request_check(run_code, system, action, irreversible, cost_usd, what) -> "ok" | "denied"`. On `ask` it creates a plain-yes approval (L2-style, `tool="trust_check"`, owner only) on the run's task through the existing store, codes and `prompt_approvers`. The prompt always names the action and system a `yes always` rule would cover, and quotes the worker's `what` (cleaned with `clean_external`, clipped to 120), so the worker's text can't pose as the reason or the rule: `[K4] R4 wants to deploy on dori-api: "deploy to prod" (new kind of action; yes always = don't ask before deploy in dori-api). Reply yes / yes always / no`. It waits until the owner answers, the approval expires (`gate.approval_ttl_s`) or the run ends (checked each poll; the approval is then expired, and also when the caller is cancelled). `yes always` adds `allow <action>` on `<system>` (`created_from="yes always"`) and texts `Rule added: … (rule 5)`. The prompt offers only `yes / no` when an owner's own `ask` rule made the check ask (that rule is changed over chat), or when the system is an area of a system already seen or a rule's scope (`dori` would also cover `dori-prod`; the run's repo counts too). A worker repeating a pending check reuses the approval and shares the answer; at most 3 checks per run wait at once.
- **`yes always`** parses as `yes always( <code>)?`, `yes <code> always` or `<code> yes always`. The gate binds it like any reply (owner only, iMessage, sent-before, a code when several are pending) and only to an approval with `allow_always`; anything else is `always_unsupported` (`Not done: yes always only answers a run's check.`), never a plain yes. The store's UPDATE checks it again, and `NewApproval` refuses `allow_always` with `require_code`.
- **Chat-taught rules.** The `rules` intent: `list_trust_rules` (L0), `add_trust_rule{scope, action, effect: ask|notify_now, note?}` (L3: tightens trust or raises urgency, runs and reports `Rule added: ask before merge in trading (rule 3).`), `allow_trust_rule{scope, action, effect?: allow|notify_digest, note?}` (L2) and `remove_trust_rule{id}` (L2). Loosening trust, lowering urgency and forgetting a rule ask the owner a plain yes when the model chose them, with a code-built prompt (`allow deploy in dori-staging without asking`, `stop texting right away on failed everywhere (digest only)`, `forget rule 3: ask before merge in trading`). This splits the plan's single `add_trust_rule` by direction, so the level comes from the registry, not from an argument. Forgetting is by number only (from the `Rule added` reply or `list_trust_rules`), so the rule approved is the rule removed. `/rules` lists, `/rules forget <n>` is explicit and runs. The runner's `rules` prompt maps the plan's examples to calls; the labeler gets two few-shots, the router eval seven rows.
- **Urgency** (D5, rules only): `trust.notify_level(conn, system, action, event)` -> `now|digest`. Defaults: `failed`, `question`, `check`, `urgent` now; `done`, `pr`, `merged`, `note` digest. A `notify_*` rule matches the event (best), the action, or `*`; `notify_now` wins ties; `question` and `check` are never lowered. EM-6 delivers.
- **Merges:** `trust.merge_decision(conn, repo) -> ok|ask` (ok unless an `ask` rule on merge for the repo, an area of it or `*`). `merge_run` doesn't call it yet (EM-7).
- **Audit:** `trust.evaluate`, `trust.check`, `trust.merge`, `trust.rule_added`, `trust.rule_removed`, plus the approvals' own rows (`approval.resolve` carries `always`).
- **Not done.** No `resonant approvals approve --always` in the CLI. A check waiting when the daemon restarts is lost with its worker session (EM-3/EM-5 own resuming). No default rules ship (`created_from="default"` is reserved).

### D5. Reporting and urgency

- Each worker event has an urgency: done, PR opened, merged, failed, a question, a check that asked, or a notable `note`.
- The defaults:
  - **Normal, in the hourly digest:** done, PR and merged.
  - **Urgent, texted right away:** failed, a question, a check that asked, and anything the worker marks `--urgent`.
- Rules can change the urgency ("text me right away when trading runs finish") through the same `trust_rules` table, with `effect: notify_now` or `notify_digest`.

**As built (EM-6).** `resonant/runners/reports.py` (`WorkerReports`), with the call sites in `runners/claude.py` (`_send_reply`, `_report_merged`, `_on_worker_event`, `_reconcile`) and the digest in `extensions/pulse_job.py` and `briefing.py`. Tests: `tests/test_worker_reports.py` (policy, rules, titles and cleaning, the `Workers:` section, the briefing, the runner with the fake claude) and three e2e in `test_e2e_runner.py`.
- **Levels.** `WorkerReports.level(run, event)` asks EM-4's `trust.notify_level` with system and repo = the run's repo (`scratch` for a run with no repo). Events: `pr` (a run that ended with its PR open) and `done` (no repo, or no changes), both matched with the action `done`, so "text me right away when trading runs finish" (`notify_now` on `done` in `trading`) covers PR runs too; a rule on `pr` itself is more specific. `merged`, `failed`, `note`, and `urgent` (a `--urgent` note, matched with the action `note`). Defaults as EM-4: `failed` and `urgent` now, the rest digest.
- **Failures always text.** A `notify_digest` rule holds a failure for the digest only when the deciding rule names `failed` itself ("stop texting right away on failed"); a `*` rule ("only digest trading") never does. A rule store that can't be read falls back to the defaults. A run whose task ended under it (`_reconcile` at startup) is now reported as failed too; before, it ended silently.
- **`/run` vs plain words.** `start_run` takes `report: now | digest` (tool arg, default `digest`); `/run` passes `now`, and the model passes it when the owner asks to be texted as soon as it's done. Kept immediate for `/run` because it is an explicit, watched request whose reply carries the `/merge R3` step (merges are still manual until EM-7), and the e2e suite's `/run` UX stays as it was. A run's `now` beats rules for `pr`, `done` and `merged` (the request is about that run); notes still follow the rules. The preference is kv `run.report:<code>` (not a column: migrations must be gapless, and 012/013 belong to EM-5/EM-7). The start ack (`On it: R4`) is unchanged and immediate; so are a cancel's reply, an unrevoked-token warning, and the base-push and session-merged alerts.
- **One item per run, exactly once.** The run's pulse item (`core` / `run/<code>`) is its digest line for its whole life: `R4 still running · demo · started 10:02 · <latest note>` while it runs (state = bucket and note, so a new note is news again, and many notes collapse into the latest), then its end, cleared: `R3 done: PR #12 (CI green) · <url>` (`· ⚠ /runs R3` when it changes flagged paths or the checks refuse it), `R3 done: <summary>`, `R3: no changes · demo`, `R3 failed: <reason>`, `R3 merged: PR #12 · <url>`. The end is written untold first; a `now` text then goes out (dedupe `run:<code>:<status>`) and only then is the line marked told. A crash between the two reruns `_conclude`: the channel drops the repeat and the mark runs. A refused text leaves the line untold, so the next pulse carries it (urgent ones with 🔴). The digest itself is exactly once through the pulse's own inflight records. A retried conclusion with the same outcome keeps the told state (the fingerprint is the status and PR number, not the CI word).
- **Notes.** The runner subscribes to `events.subscribe`. A `note` event at `now` texts `R4 note: <note>` (dedupe by the event time) and marks the run's line told; at most 6 such texts per run an hour (`run.note_held`), the rest only update the line. `question` and `check` events are ignored here: EM-5 texts questions, EM-4's prompt texts checks; the line reads `waiting for your answer`.
- **Sanitizing.** Titles are code-built (`R4`, `PR #12`) around parts that came from a worker or GitHub (note, summary, failure, URL). Those are redacted and cleaned with `clean_external` where they are stored (failure reasons too, since EM-6: git stderr and GitHub errors are texted now), and each goes through `notify.clean_text` once more when the title is built (brackets, reply words, code-like tokens next to `yes`/`no`/`stop`), so a note can't pose as an approval prompt or a reply.
- **Digest.** `pulse_job.compose(items, context)` puts worker items in a `Workers:` section after the other lines: finished first (urgent, then oldest), then running ones. When there is news, workers still running (already told) are listed too, for status; they never trigger a pulse alone. The section comes last, so a full message drops worker lines first, except that a section holding an urgent line (a failure nobody took) goes first. `fit_entries` never leaves a bare header, and `+N more` counts only news left out, not running status lines. The 08:00 pulse is skipped, so the briefing gets a `Workers:` section (untold worker items plus running ones) before `Updates:` and marks it told. Both stay within 1,000 characters.
- **Merged.** `_report_merged(run, by_owner=True)` (after `/merge` or a yes) marks the line told: the owner got the reply. EM-7's merge per trust rules calls it with `by_owner=False`, which follows the `merged` level (digest by default).
- **Audit.** `run.report` (event, notify) per decision, `run.note_held`.
- **Not done.** Open questions are not counted separately in the digest (the run's line says it is waiting); EM-5 owns question re-mentions. The note cap is in memory (a restart resets it).

### D6. Repos (from repos-plan, simplified)

- **Kept:** the registry, the resolver, extension links, `create_repo`, starter CI and the add-CI merge.
- **Fork mode:** for PRs to other people's public repos. The worker does the work in a worktree of a fork. Opening the upstream PR is a `check` of kind `external_pr`, which asks by default. Resonant opens it with the `public_repo` token.
- **Merge on green** becomes "merge per trust rules" (D4). The `merge` flag on `start_run` is no longer needed.

## 4. Tickets and waves

| Ticket | Scope | Wave |
|---|---|---|
| EM-1 | Thin guard profile and probes. Replaces the Phase 5a profile and drops the RP-9 name rules and the cross-run deny | 1 |
| EM-2 | Real checkouts and worktrees, `.env` symlinks, scratch dirs; harvest, bundle and fresh-clone code removed for push repos | 1 |
| EM-3 | Worker CLI and per-run socket: `ask`, `note`, `check`; the `question` wait reason; session kept alive while waiting | 2 |
| EM-4 | `trust_rules` table and evaluation, the default net-new rules, `yes always`, `/rules`, the brain's rule-teaching intent | 2 |
| EM-5 | Relay: questions and answers through the brain, reply threading across runs, "tell R4 …" messages, ask timeouts | 3 |
| EM-6 | Reporting urgencies, the digest of worker events, `notify_now` rules | 3 |
| EM-7 | Fork mode (repos-plan D3) as a worker plus an `external_pr` check; merge per trust rules | 3 |
| EM-8 | Router: the `repos` intent and tools (repos-plan D8), RP-4's carry-overs, few-shots and evals | 4 |
| EM-10 | Whole-codebase sweep after EM-9: remove leftovers of the old model (Phase 5a strict sandbox, fresh clones/bundles/mirrors, RP-9 name rules, repos-plan waves that never shipped), dead config/code/tests, stale docs; bring `followups.md` owner to-dos and `test.md` fully up to date | 6 |
| EM-9 | e2e tests (fake claude using the worker CLI), go-live rewrite, `test.md`, architecture docs and the artifact | 5 |

## 5. Exit criteria

- "spawn a claude to build out XYZ" gets `On it: R4`. The worker asks a question, you get `R4 asks: …`, your reply reaches R4, and it finishes with a PR and a digest line.
- A worker reads a checkout's `.env` and runs Docker. It can't write `~/.resonant`, signal the daemon, read the Keychain or send an iMessage.
- A first deploy asks; `yes always` makes the next one not ask. "always ask before merging in trading" makes trading merges ask.
- Three workers run in parallel, with replies routed correctly.

## 6. Open questions

- Should `~/code` hold all repos, including ones you already have elsewhere on the Mini? Default: `~/code`, with a per-repo `path` override in the registry. (Built in EM-2: `runner.code_dir` and `repos.path`.)
- Should the per-run App token stay scoped to one repo? A worker on "ops" asks may need several. Default: scope it to the repos the run touches, with additions through `resonant check`.

## As built

### EM-1 (thin guard, macOS 27.0 / the Mini)

Built: `resonant/sandbox/runner.sb` rewritten as the thin guard; `profile.py` (`SandboxParams.protect`, `default_protected()`, ancestor rendering, `checked_path(..., follow=False)`); `probes.py` rewritten to 28 probes (17 `ci`, 11 `local`) and a new self-check; `tests/test_sandbox.py` with mutation tests (each critical deny is removed from the template and its probe must fail). `runners/claude.py` and `runner/workspace.py` are untouched: every `SandboxParams` field stays (`read_paths` and `work_dir` are accepted and no longer limit anything, and a run dir no longer has to sit inside `work_dir`, so EM-2's worktrees fit).

- **What is denied.**
  - **Writes:** `~/.resonant` and `$RESONANT_HOME`. The installed daemon (`default_protected()`): `sys.prefix`, `sys.base_prefix` and the checkout the package runs from. The `resonant` entry point. `gh`, `uv`, `ollama` and `git`, each as the link, its real file and its whole Homebrew keg, because the daemon runs them outside the guard by absolute path with credentials. And the real target of any symlinked `$HOME` path the profile names (a moved `~/.resonant`, a dotfiles-repo `~/.zshrc`; Messages and Keychains go to the read-and-write list). The persistence list follows:
  - **Reads and writes:** `runner.secrets_dir`, `~/Library/Messages`, `~/Library/Keychains`, `/Library/Keychains` and `runner.deny_read`.
  - **Exec:** `launchctl`, `crontab`, `at`, the daemon's venv and its `resonant`.
  - **Mach lookups:**
    - launchd;
    - the Keychain: `com.apple.SecurityServer`, `com.apple.securityd*`, `com.apple.security.agent*`, `kcsharing`, `KeychainStasher`, `cloudkeychainproxy3`, `octagon` and `keychainsharingmessagingd`;
    - Messages: `imagent*`, `imdpersistence*`, `imcore*`, `iChat` and `MobileSMS*`;
    - Apple Events, LaunchServices, the window server, HID, accessibility and `tccd`.
  - **Operations:** `file-mount` (a disk image mounted over `~/.resonant` or the venv; found in review), `appleevent-send`, `lsopen`, the HID user clients, signals outside the sandbox, task ports, and port 7777 (connect and bind, every address).
- **What is open.** Everything else, including the PATH dirs (`~/.local/bin`, `/opt/homebrew/bin`), so a worker can `brew install` and `uv tool install` (`brew upgrade` of gh, uv, ollama or git still fails, since their kegs are protected), and including reads of all of `$HOME` (`.env`, `.ssh/config`, `~/.resonant` outside `run-secrets`), writes beside the protected paths, unix sockets (Docker), Ollama's port, ptys, the pasteboard and `ps`.
- **Spike findings** (real `sandbox-exec`):
  - A `file-write*` subpath deny also refuses a hardlink to a protected file, a rename out of it, a write through a symlink and the `/.vol/<dev>/<inode>` path.
  - Renaming an ancestor dir does carry a subpath-protected tree away. So every ancestor of every protected path gets a `literal` write deny, which blocks rename and delete of that dir but still lets a worker create entries in it.
  - The Keychain read is stopped by the securityd mach denies, not by the file denies: with only the file denies, `security find-generic-password -w` on a throwaway keychain still returned the secret.
  - With the denies in place, curl, Apple git, Python's `ssl` and gh (Go) all validate TLS to github.com through `trustd`.
- **Probes** (`resonant sandbox check`):
  - The full deny battery (create, open-for-write, mkdir, rename, rename-out, hardlink, symlink-write, rename-over of `.zshrc`, ancestor rename) runs against a fake home under `/private/var/tmp` with a fake daemon install. The fake `~/.resonant` and `~/.zshrc` are symlinks, so their real targets are checked too.
  - The fake home also has a Homebrew-like `brew/bin` with the daemon's `gh` linked into a keg, and a `~/.local/bin`. New files in either are allowed; replacing the `gh` link or writing its keg is denied. On the real host, a create in `~/.local/bin` and `/opt/homebrew/bin` must work.
  - A disk image attach over the fake `~/.resonant` must fail. It is a local probe, with a control attach outside the sandbox.
  - On the real paths, `real paths on this host` only opens existing files for writing without writing, creates uniquely named entries (removed again if one appears) and lists dirs whose control read works. Writes count only where this user could write without the guard.
  - The Keychain probe uses a throwaway keychain file (not the login keychain, and not added to the search list) and deletes it.
  - Allowed side: a planted `.env`, `id_rsa` and `.pem` under the real `$HOME` are readable, `$HOME`, `/private/tmp` and `/private/var/tmp` are writable, a unix socket connects, a Docker socket connects where one is running, and curl, git, Python and gh reach github.com.
- **EM-2's `write_paths`** (the worktree under `~/code/.worktrees/<name>/R<n>` and the checkout's `.git`, or the scratch dir) grant nothing under allow-default (the `@WRITE_ALLOW@` block is gone with the old profile). `resolved()` refuses one that sits inside a write-denied path (`~/.resonant`, the daemon's install, a persistence path, `secrets_dir`, `deny_read`). So a checkout at the daemon's own install path would fail closed. The live install is a separate copy (D1).
- **The daemon can't be shadowed through PATH.** It never looks up a tool by name in a dir a worker can write:
  - The LaunchAgent PATH is the venv's bin plus `/usr/bin:/bin:/usr/sbin:/sbin`, and ollama's is the system dirs.
  - The ops runner (`/update`, restarts) forces the system PATH. `/update` runs `/usr/bin/git`, and `uv sync --cache-dir <RESONANT_HOME>/uv-cache` uses a cache workers can't write, so `~/.cache/uv` needs no protection.
  - Extension children get `/usr/bin:/bin` (`extensions.process.path`), and their `gh`/`git` argv0 is pinned by absolute path at install. The extension source fetch uses `/usr/bin/git`.
  - `find_ollama` (`daemon install`), `resolve_tool` (the tools linked into a session) and `default_protected()` search fixed install dirs (`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`, `/bin`), never the process PATH.
  - The session itself still gets Homebrew's tools: it is the worker's own PATH.
  - **Owner step:** re-run `resonant daemon install` so the LaunchAgent picks up the new PATH. Until then the old plist PATH is in the daemon's environment, but nothing in the daemon resolves a tool through it.
- **Self-check:** a write into `~/.resonant` (a unique new name; `mkdir ~/.resonant` when it doesn't exist) and into the first writable protected dir (the daemon's venv), both undone if they work, a securityd lookup and port 7777.
- **Carry-overs:**
  - EM-3's per-run socket must not live in a dir a worker can connect to by accident. Connect is a network operation, so the `~/.resonant` write deny does not cover it; the socket needs its own rule.
  - Hardlinks that already exist into a protected file are not covered, because the rules are path-based (`.venv` has none today; keep uv's link mode as clone or copy).
  - `API_PORT` is fixed at 7777 in the profile and the self-check. If `api.port` changes, derive the port from settings.
  - `~/Library` and `~/.config` are write-denied whole (the existing persistence list). Narrow them to LaunchAgents, the login-item store and the config files that run code, if workers need `~/Library/Caches` with the real `HOME`.

### EM-3 (worker CLI, question waits, daemon git under the guard)

Built: `runners/worker_cli.py` (wire, shim, socket server, peer check), `runners/worker_desk.py` (asks, notes, checks, messages, events), `store/questions.py` with migration 011 (`run_questions`, `run_messages`, `runs.wait_reason`, `runs.last_note`, `runs.resume_prompt`), and changes to `runners/claude.py`, `runner/workspace.py`, `sandbox/` and `wiring.py`. Tests: `tests/test_worker_cli.py` (the fake claude runs the real shim over the real socket), one e2e in `test_e2e_runner.py`, S2 tests in `test_runner_workspace.py` and real-`sandbox-exec` control-dir tests in `test_sandbox.py`.

- **The CLI.** `<run>/bin/resonant` is a stdlib Python script written at prepare, before any session can touch `bin/`. Its shebang is the daemon's base interpreter with `-IS`; the venv is exec-denied, the base install is only write-protected. Usage:
  - `resonant ask "<q>" [--options a,b,c] [--urgent]`
  - `resonant note "<progress>" [--urgent]`
  - `resonant check --system S --action A [--irreversible] [--cost USD] "<what>"`

  Replies are plain text. The exit codes:
  - `0`: answered, noted, or the check is ok.
  - `1`: check denied.
  - `2`: usage error.
  - `3`: no answer yet. End the turn; the session is resumed with the answer.
  - `4`: Resonant can't be reached, or the run is over.

  The worker prompt (`WORKER_PROMPT`) says when to use each command, with the D4 list of what to check.
- **The socket.** The control dir is `<work_dir>/.ctl/R<n>/{sock,token}`: daemon-owned, 0700, lstat-checked and never followed through a link. It sits beside the run dir, not inside it, because the session can rewrite anything in its own run dir. Each session gets a fresh `secrets.token_urlsafe(32)` token (0600, written by rename); it never appears in argv or env. The server runs only while a session process runs, and it is closed (with its handlers cancelled) before the reap, so a CLI call in flight can't text anyone after a stop.

  Each request is checked twice:
  - The token, with `hmac.compare_digest`.
  - The peer pid (`LOCAL_PEERPID`). It must be the run's live session leader, matched on start time, or one of its descendants. A process that double-forked and was reparented to launchd gets exit 4.

  A socket path over 100 bytes (tests only) is bound in a short private dir under `/tmp` and renamed into place; the shim then connects from inside the control dir.
- **The guard rules** (`runner.sb` `@CTL@`; deny-only, checked with real `sandbox-exec`):
  - No writes anywhere under `<work_dir>/.ctl`.
  - No reads there except the run's own control dir.
  - No unix-socket connect under `work_dir` except to the run's own run dir and control dir.

  So a worker can neither use another run's token nor plant a socket that stands in for the daemon. `.ctl` joins the ancestor rename guard. The four per-run `-D` values are kept out of `profile_hash`, so the cached self-check still hits from one run to the next. This answers EM-1's socket carry-over.
- **`ask`.** It records the question and emits a `question` event. It texts the requester `R4 asks: <q>` (with `Options: …`), cleaned by `clean_external`, redacted and clipped, through the same `channel.send` as run replies. Then it waits up to `ask_block_s`, 8 minutes. The session env sets `BASH_DEFAULT_TIMEOUT_MS`/`BASH_MAX_TIMEOUT_MS` to 10 minutes, so the Bash tool doesn't cut the call off first.
  - A repeat of the open question (a retry) joins it, and nothing is texted twice.
  - There is one open ask per run, so an answer can't land on the wrong question.
  - If the caller hangs up, the handler stops at once and nothing is marked delivered.

  An answer counts as delivered only after the CLI reply has been written; otherwise the next turn's prompt carries it. So delivery is at least once, never lost.
- **`waiting(question)`.** The decision comes after a turn ends with a result, and nothing is awaited from the look to the step change:
  - Undelivered answers, timeouts or messages: another `--resume` turn in the same step.
  - An open question: the run goes to `waiting`, reason `question` (task wait `human_reply`, `wake_at` at the earliest `asked_at + ask_timeout`). The slot is freed. The pulse item reads `R4 waiting for your answer · …`. The time adds to neither `session_seconds` (`max_minutes`) nor `wait_seconds` (the 24 h usage cap).
  - Otherwise: harvest, or `done` for a scratch run.

  Waking: `answer_question` and `deliver_message` always submit a task event, deduped by question or message id. `Loop.submit` requeues a waiting task, and a `Wait` returned while events are pending requeues at once, so an answer can't be missed. A woken run with nothing deliverable returns the same `Wait` and leaves its row untouched.

  After `runner.ask_timeout_minutes` (default 240) the question times out, and the session resumes with "No answer from the owner; use your judgment or stop."

  Each delivery turn's text is saved in `runs.resume_prompt` until the turn ends. A crash mid-turn resumes with `RESUME_PROMPT` plus that text plus any still-open question. A daemon restart defers open asks. Usage waits now record `wait_reason='usage_reset'`; a NULL reason, from rows made before migration 011, is treated as a usage wait.
- **`note`** sets `runs.last_note`, which is appended to the running pulse title, and emits a `note` event with `urgent`.
- **`check`** calls EM-4's `TrustChecks.request_check(run_code, system, action, irreversible, cost_usd, what) -> "ok" | "denied"` (`IMessageStack.trust`, the one instance `wiring.py` also hands the runner). `system` and `action` are slugged first with EM-4's `slug`; a bad one or `*` is a usage error (exit 2). The CLI waits for the decision like an ask, up to 8 minutes. The decision call keeps running after the CLI gives up and after the turn ends, so the owner's approval (EM-4's prompt, `yes` / `yes always` / `no`) stays alive while the run is in `waiting(question)`. The verdict is recorded on the check's row; that wakes the run, and the session resumes with "Your check (deploy on x: …) is ok: go ahead." or "… was denied: don't do it …".

  The call is cancelled only when the run ends or when `ask_timeout` passes, and cancelling it expires the approval (EM-4). The approval's own TTL is `gate.approval_ttl_s` (30 minutes by default, well under `ask_timeout`), so an unanswered check normally ends `denied` at that TTL. Known gap: `approvals.resolve` refuses a reply while the run's task is `queued` (woken and waiting for a slot), so a yes in that window is refused and the check expires as denied; widening the gate's rule is left to the gate's owner. EM-4 allows at most 3 pending checks per run; a fourth is `denied`. On a daemon restart, open checks are closed with "Resonant restarted before the owner decided; run `resonant check` again", and their pending `trust_check` approvals are expired so a late yes doesn't look accepted. A runner built without `trust` (tests only) denies every check.
- **The API for EM-5 and EM-6** (on `ClaudeRunner`):
  - `open_questions() -> [OpenQuestion(id, run_code, question, options, asked_at, urgent)]`
  - `answer_question(code, text) -> Relayed | Refused`
  - `deliver_message(code, text) -> Relayed | Refused`. A message is appended to the next CLI reply as `Message from the owner: …`, or opens the next turn. A run waiting for an answer resumes with it. It is refused once the run's session step is over.
  - `events.subscribe(fn)` returns the unsubscribe function. `WorkerEvent(kind "question" | "note" | "check", run_code, text, urgent, at, question_id, options)`. Events are only logged for now; questions are texted as above.

  Reply routing is EM-5's.
- **Cancel, kill, fail and end.** `_finish_cancelled` and `_conclude` close open questions, cancel any check decisions in flight, release blocked CLI calls with "This run is over" and remove the control dir. Startup wipes stale control dirs.
- **S2: daemon git in worker-writable checkouts.** EM-2's accepted risk, closed:
  - The daemon's token fetch now goes only to the daemon-only mirror (`_refresh`). The base sha, the CI presence and the top-level tracked names (for the `.env` link filter) are read from the mirror at that sha.
  - Every git command in a checkout or worktree (`_in_checkout`) runs tokenless under the thin guard (`Workspace(guard=…)`, by default `sandbox.wrap` with run-less params; it fails closed without `sandbox-exec`). Whatever a checkout's config makes git run (filters, `alternateRefsCommand`, `askPass`, `remote.<url>.vcs`, `url.*.insteadOf`, includes) therefore runs as a worker could, never as the daemon. Those commands are: the fetch from the mirror (`GIT_ALLOW_PROTOCOL=file`), `worktree add --no-checkout`, the guarded `reset -q --hard` that checks the worktree out, `rev-parse`/`config --get` in `ensure_checkout`, `worktree list` and `update-ref -d`.
  - `git worktree remove` is gone (it reads status through filters and fsmonitor): the dir is removed with `rmtree_nofollow` and only its admin entry is dropped. `_drop_admin_entry` refuses a symlinked `.git/worktrees`.
  - Added overrides: `core.askPass=`, `core.pager=cat`, and env `GIT_ASKPASS=` on every daemon git.
  - The first clone of a missing checkout still uses the token, into a fresh temp dir whose config is git's own.
  - Tests: a planted smudge filter, `alternateRefsCommand` and `askPass` in the checkout's config can't write `$RESONANT_HOME` (the filter does run, inside the guard), and every daemon git in a checkout goes through the guard with `--no-checkout` and no token.
- **Not done.**
  - Time a worker spends blocked inside one `ask` call (up to 8 minutes) counts as session time.
  - Nothing routes the owner's reply to `answer_question` yet (EM-5). (Urgent notes are texted since EM-6.)
  - The hourly digest shows a waiting run as `waiting for your answer` (EM-6), not the question.

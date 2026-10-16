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

### D5. Reporting and urgency

- Each worker event has an urgency: done, PR opened, merged, failed, a question, a check that asked, or a notable `note`.
- The defaults:
  - **Normal, in the hourly digest:** done, PR and merged.
  - **Urgent, texted right away:** failed, a question, a check that asked, and anything the worker marks `--urgent`.
- Rules can change the urgency ("text me right away when trading runs finish") through the same `trust_rules` table, with `effect: notify_now` or `notify_digest`.

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
| EM-9 | e2e tests (fake claude using the worker CLI), go-live rewrite, `test.md`, architecture docs and the artifact | 5 |

## 5. Exit criteria

- "spawn a claude to build out XYZ" gets `On it: R4`. The worker asks a question, you get `R4 asks: …`, your reply reaches R4, and it finishes with a PR and a digest line.
- A worker reads a checkout's `.env` and runs Docker. It can't write `~/.resonant`, signal the daemon, read the Keychain or send an iMessage.
- A first deploy asks; `yes always` makes the next one not ask. "always ask before merging in trading" makes trading merges ask.
- Three workers run in parallel, with replies routed correctly.

## 6. Open questions

- Should `~/code` hold all repos, including ones you already have elsewhere on the Mini? Default: `~/code`, with a per-repo `path` override in the registry.
- Should the per-run App token stay scoped to one repo? A worker on "ops" asks may need several. Default: scope it to the repos the run touches, with additions through `resonant check`.

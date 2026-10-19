# Follow-ups

Work we've decided to do eventually but have deferred. Each entry says why it was deferred, what is open, and what "done" looks like. When an item is picked up, ticket it and remove it from here.

## Recommended next (2026-10-19)

Ranked by how much it matters. Urgency: 🔴 high (do soon), 🟠 med-high, 🟡 medium, 🟢 low. Tick off or move into a ticket when picked up.

| | Item | Why | Urgency | Status |
|---|---|---|---|---|
| 1 | ~~CAL-1: Calendar for the Claude brain~~ | Dropped by the owner on 2026-10-19 (PR #112 closed unmerged): calendar reminders come from elsewhere. | — | Dropped |
| 2 | **Owner eval set** from real texts (`~/.resonant/evals/task.jsonl`) | Every score so far is on the synthetic `evals/task.example.jsonl`. Real texts are what matter and catch regressions. | 🔴 High | Open |
| 3 | **Daily DB backup** | Backups only happened before migrations. A bad disk or corrupt write loses tasks, approvals and history. | 🟠 Med-high | Done (BK-1): `core/backup` daily at 03:30, 7 daily + 4 weekly kept, 36h alert, `resonant backup now\|list\|restore`. Owner: optionally set `backup.extra_dir` to another disk |
| 4 | **Shorter taint window** (2–3 texts) or exempt low-risk writes (notes, reminders) | BR-10b taints about 24 turns after a Claude answer, so cheap changes ask "yes?" often. | 🟠 Med-high | Open |
| 5 | **TC-1: tool core package** (`resonant/toolcore/`) | The shared call path (`LocalRunner._call_tool`, `gate/`, `trust/`, `executor.py`) is spread over four places and the Claude bridge reaches it by subclassing `LocalRunner`. One package with one entry point; behaviour unchanged. | 🟡 Medium | ✅ Done (TC-1); carry-overs [below](#scheduler-inline-jobs-onto-toolcore) |
| 6 | **Live Claude eval** in a scratch home | Live evals grade routing and writes only; Claude's own answers have never been graded. | 🟡 Medium | Open |
| 7 | **Branch protection** per repo (PR + CI required, app not in bypass) | Workers merge on standing trust; protection is the backstop for a bad merge. | 🟡 Medium | Owner, open |
| 8 | **Relay go-live** (Supabase/Fly, go-live step 15) | A phone alert when the Mini or the daemon dies. Today a dead Mini just goes silent. | 🟡 Medium | Owner, open |
| 9 | **Flaky tests** (Q9 approval code in `test_router_approvals`, `test_watchdog_quiet_while_beating`, e2e ConnectErrors, scheduled jobs firing mid-test around 08:00/10:00 local in `test_e2e_phase3` / `test_e2e_pulse`) | They slow every PR and hide real failures. | 🟢 Low-med | Open |
| 10 | **Retire the intents path** once the loop has proven itself on real texts | Less code to keep working. | 🟢 Low | Open |
| 11 | **Whisper** for voice notes (`brain.whisper_path`) | Only if voice notes get used. | 🟢 Low | Open |
| 12 | **Prune stale worktrees and branches** | Housekeeping (one agent worktree left under `.claude/worktrees/`). | 🟢 Low | Open |

## Owner to-dos (human, at the Mini)

Only you can do these: tokens, GitHub settings and the first real runs. Claude never enters a secret, and no secret goes in a command line or a chat. Do them in this order. The full steps and expected output are [`go-live.md`](go-live.md) step 14 (step 12 for the github extension, step 15 for the remote dashboard); status rows are in [`test.md`](test.md). Tick them off here.

- [ ] **github extension** (go-live 12, if not done yet): `resonant secret set github/token` (read-only fine-grained token), then `resonant ext add repo:github` (type `github`).
- [ ] **After BR-9 deploys: `resonant ext update github`.** The installed copy is pinned, so the new `recent_prs` tool (the newest PRs in a repo, merged ones too) appears only after the update. Until then "latest PRs" answers from `recent_changes` (the local git log), which already shows squash-merged PR numbers.
- [ ] **After BF-1 deploys: `resonant ext update github`.** The installed copy is pinned, so the all-zero `[github] 0 open PRs, 0 failing CI, 0 awaiting your review` line keeps showing in the briefing until the update (the new digest reports it `quiet`, and the briefing leaves it out).
- [ ] **Optional, Linear** (go-live 12, Optional: Linear): create a Linear personal API key → `resonant secret set linear/token` → `resonant ext add repo:linear` (answer `y` to sending the token to `mcp.linear.app`, then type `linear`). Try "what's on my Linear".
- [x] **Deploy and reinstall:** done by Claude on 2026-10-16 (main at schema 11, `resonant daemon install` run). If you install the github extension later, the new copy already pins `gh`; keep `extensions.process.path` at `/usr/bin`, `/bin`.
- [ ] **First: Claude.** Claude Code and `gh` installed (`claude --version`; `gh` at `/opt/homebrew/bin/gh`), and `claude` logged in in the Mini's terminal (`claude auth status`). With the default `runner.guard: off` (GD-1) that login is all a run needs: no token. A token is optional (`claude setup-token` → `resonant secret set runner/claude_token`; it wins over the login) and required only with `runner.guard: thin`; for API billing, `resonant secret set runner/anthropic_key` and set `runner.max_budget_usd`.
- [ ] **Workers can already run; the GitHub steps below unlock push and PRs.** Try a scratch run ("spawn a claude to check what's using the disk on the Mini") and a repo you have checked out in `~/code` ("spawn a claude to fix the typo in the README in <repo>"): `Heads up: no push yet (GitHub App not set up); the work stays on a local branch.`, then `R… done (…): work is on local branch resonant/r…-… in ~/code/<repo>; …` (`test.md` 14). `/runs` lists what's still missing. A private repo with no checkout waits for the token below.
- [ ] **GitHub App** (unlocks push and PRs, with the token below):
  1. github.com → Settings → Developer settings → GitHub Apps → New. Webhook off.
  2. Repository permissions: read and write Contents, Pull requests, Issues, Workflows, Actions; read-only Checks, Commit statuses, Metadata. **No Administration.**
  3. Install it on your account, **All repositories**.
  4. Generate a private key → `resonant secret set runner/github_app_key --from-file <the .pem>` → delete the `.pem`.
  5. Tell Claude the App ID and the installation ID (not secret; Claude adds `runner.github_app` and restarts).
- [ ] **Resonant's own token** (unlocks private clones, the PR checks and so pushing, merges, new repos, add-CI and fork runs): fine-grained, your account, All repositories. Read and write Contents, Pull requests, Administration, Workflows; read-only Checks, Commit statuses, Metadata. → `resonant secret set runner/github_token`.
- [ ] **Optional, for PRs to others' public repos:** a classic token with only `public_repo` → `resonant secret set runner/github_public_token`.
- [ ] **`scripts/bootstrap.sh --check`:** `runs table`, `runner guard`, `runner repo resonant`, `runner github app` and the three `runner secret …` rows for `claude_token` (or `anthropic_key`; `PASS  not set: your own claude login works` counts), `github_token` and `github_app_key` `PASS`; `runner sandbox` `SKIP`s while the guard is off (all runner rows are advisory: before the GitHub steps their rows read `workers still run; needed to …`). `runner repos`, `runner code dir`, the optional `runner/github_public_token` row and the four `owner step` rows may `SKIP`.
- [ ] **On GitHub, per repo you care about:** branch protection (or a ruleset) on the base requiring a PR and CI, with the app not in the bypass list. Tell Claude "no CI edits in <repo>" where Actions secrets matter (Claude adds a `runner.repos` entry with `allow_github_changes: false`).
- [ ] **First real run, with Claude watching:** `/run <repo> <small ask>` on a repo with CI. The worker's `gh pr create` and `gh pr checks --watch` work with the App's token (PR by `<app>[bot]`); your checkout is untouched (go-live 14, first real run; `test.md` 39).
- [ ] **The iMessage tests:** go-live 14, iMessage 2–12 (`test.md` 40–45, 51–52, including working on and restarting Resonant itself): spawn in plain words and get the digest line, a question and its reply, a check and `yes always`, a rule that makes a merge ask, a scratch run, cancel and `/kill`.
- [ ] **First fork run** (only with the `public_repo` token): the App mints a token for the new fork, and the upstream PR opens only after your `yes` (`test.md` 46).
- [ ] **Optional tidy-up from the old runner:** drop `runner.max_bundle_mb` from `~/.resonant/config.yaml` if it is there (ignored now), and delete `~/.resonant/runner/mirrors/.incoming` if it exists (old bundle copies; nothing writes there any more).

### Remote dashboard and dead-man switch (go-live 15)

The local dashboard (`http://127.0.0.1:7777/` on the Mini) needs nothing from you. These make a status-only copy reachable from any browser and add the phone alert for when the Mini goes quiet. Do them in this order; the commands are in [`go-live.md`](go-live.md#15-phase-4-the-remote-dashboard-and-the-dead-man-switch) step 15 (`test.md` D1–D6).

- [ ] **Supabase project** (15.1): sign-ups off, TOTP on, your one user (Auto Confirm). Note the project URL and the publishable key.
- [ ] **Migration** (15.2): paste `supabase/migrations/20261016000000_relay.sql` into the SQL editor and run it.
- [ ] **TOTP** (15.3), from a machine with Node: `cd dashboard && npm ci && cd ..`, then `node supabase/enroll_mfa.mjs --url … --anon-key … --email …`. Keep the printed user id; clear the scrollback.
- [ ] **Ingest token** (15.4, on the Mini): the `T=$(openssl rand -hex 32)` block: prints its hash, pipes the token into `resonant secret set relay/supabase_key`.
- [ ] **Fly app, status token, ntfy topic** (15.5): `brew install flyctl`, `fly auth login`, `fly apps create "$APP"`, then the block that prints the status hash and the topic and pipes the four secrets into `fly secrets import --app "$APP" --stage`.
- [ ] **Hashes into Supabase** (15.6): the `update relay_private.config …` with the user id and both hashes; the check query shows `32`, `32`.
- [ ] **Tell Claude the project URL and the publishable key** (15.7, not secret): Claude sets `relay: {enabled: true, supabase_url, anon_key}` and restarts. `--check`: `relay pushing  PASS`, `dead-man switch  PASS`.
- [ ] **Deploy** (15.8): `fly deploy --app "$APP" --config relay/fly.toml --dockerfile relay/Dockerfile --ha=false .`, then `fly scale count 1 --app "$APP" --yes`.
- [ ] **Phone** (15.9): the ntfy app, subscribed to the topic.
- [ ] **Sign in** (15.10) at `https://<app>.fly.dev/` with password + TOTP.
- [ ] **Freeze test** (15.11), with Claude watching: `kill -STOP $(pgrep -f 'resonant daemon run$')` → stale banner after ~3 min, one `Resonant: no snapshots` push ~4–6 min after the freeze; `kill -CONT …` → `Resonant: snapshots back`.
- [ ] **After BR-11 deploys: the Claude brain phone tests** (go-live 9, "The Claude brain"): a follow-up that knows the first answer, two hard texts back to back, a restart that resumes, `/kill`. Optional: `brain.claude_session.remote_control: true` for a Remote Control link on `/status`. The daily cap is off by default now (`brain.ask_claude.daily_cap: 0`; 60 an hour is the runaway guard): set a number if you want one.
- [ ] **Brain loop on your own texts** (BR-8, go-live 9 "Task evals"): `brain.mode: loop` is the default after the next deploy. Put 40+ of your real texts in `~/.resonant/evals/task.jsonl` (format: `evals/README.md`), run `resonant eval task --mode loop --repeat 3` with nothing else using Ollama; under 85% or any unapproved write → `brain: {mode: intents}` in `~/.resonant/config.yaml` and tell Claude which rows miss.

## Status relay: what's left

*The relay is built (Phase 4, [`dashboard-plan.md`](dashboard-plan.md)) and set up in [go-live step 15](go-live.md#15-phase-4-the-remote-dashboard-and-the-dead-man-switch); it replaces `health.healthchecks_url` as the dead-man switch (that ping still works if set, and `--check`'s advisory `dead-man switch` row passes with either). What remains:*

- **Alert channel.** ntfy (public `ntfy.sh` with a long random topic) is the default because it needs no account. If missed pushes or the topic-as-password model bother you, move to a protected topic (`NTFY_TOKEN`), your own ntfy server (`NTFY_URL`), or a webhook (`WEBHOOK_URL`) into SMS or email. No code change needed.
- **Watcher of the watcher.** If Fly or Supabase is down, the checker can't alert (a Supabase outage is reported as `read_error`; a Fly outage is silent). An external uptime check on `https://<app>.fly.dev/healthz` (503 when the checker stalls) would cover it; not set up.
- **Retention.** Fixed at 7 days and at most 12000 rows, inside `relay_ingest`. Changing it is a new migration.
- **Pulse visibility.** The remote tier carries `pulse.open` (open items), but not the untold count, so a muted reporter doesn't show from away.

## Inbound webhooks

*Deferred 2026-10-13, when Tailscale was dropped from the plan.*

**Problem.** The design had third-party webhooks (`POST /hooks/<name>`, HMAC-signed) arrive over the tailnet. With Tailscale dropped and the status relay push-only, there is no inbound path to the Mini, and it must not open a public port.

**Options to weigh**
- **Poll instead of push** where the provider allows it. CI status is already polled.
- **Relay receives, the Mini pulls.** The relay on Fly accepts webhooks and queues them, and the Mini fetches them outbound. HMAC verification stays on the Mini.
- Something else.

**Open questions**
- **Latency.** How long can a polled or pulled event wait?
- **Replay protection.** Timestamps, nonces or delivery IDs, checked on the Mini.
- **What may sit on Fly.** Which payloads are acceptable to store off the Mini, and for how long.

**Done when**
- A design is chosen.
- The docs (`architecture.md`, `architecture.html`, `interfaces.md`) describe it.
- The Mini still has no public port.

## Router evals on real texts (go-live step 9)

*Deferred 2026-10-13, during Mac mini go-live. It needs real texts labelled by hand, and routing works well enough in day-to-day use for now.*

**What.** `resonant eval router` measures how often the router picks the right intent (the built-in intents in `router/intents.py`, plus `extension:<name>`). The synthetic set in `evals/` only proves the pipeline.

**To do.**
- Collect 30-50 real texts sent to Resonant and label each with its intent in `~/.resonant/evals/router.jsonl`, in the format of `evals/router.example.jsonl`. The file stays on the Mini and is never committed.
- Run `resonant eval router --set ~/.resonant/evals/router.jsonl` and look at the misroutes.

**When to revisit.** When replies start going to the wrong intent, now that side-effecting tools exist (a misroute costs an approval prompt, or runs an L3 tool such as `answer_worker`), or when the model or router prompt changes.

**Done when.** Accuracy is ≥ 90% on the real set, as go-live step 9 expects.

## Proactive nudges ("btw …")

*Noted 2026-10-13, during Phase 2 planning. Not scheduled yet.*

**What.** Resonant texts the owner unprompted when something is worth knowing: things they may have forgotten or never saw. Examples:
- "btw, XYZ rescheduled your 3pm to Thursday."
- "You said you'd follow up with A last week."
- "PR #12 has been red for two days."
- "Your 9am tomorrow conflicts with the dentist."

This is the opposite of a reminder the owner sets: Resonant decides what's worth saying.

**Shape (sketch).**
- **Sources.** Each source is an extension that emits events or exposes read tools:
  - calendar, Gmail (Phase 7 life lane)
  - GitHub and CI
  - Resonant's own tasks, notes and reminders
  - on-call (Phase 6)
- **Trigger.** A scheduled job (Phase 3 scheduler) runs a periodic sweep, plus event-driven triggers, e.g. a calendar change event. Not a model running in a loop.
- **Decide.** A ranking step picks at most a few items worth interrupting for: novelty (not already told), urgency, relevance. This is where a stronger model may be needed (Claude, not the local model).
- **Deliver.** One short iMessage digest or a single "btw", through the normal channel, with:
  - quiet hours
  - a daily cap
  - dedupe per item, never repeating something already sent
  - an easy "stop telling me about X"

**Open questions.**
- What counts as worth interrupting for, and how the owner tunes it (feedback replies like "useful" / "not useful").
- Privacy: what the ranking model may see, local vs Claude.
- Cost and rate limits of a periodic sweep.
- How it interacts with the kill switch: nudges are read-only messages, but they still respect `paused`.

**Phase 3 shipped the seed** (the code-built daily briefing, `core/briefing`) and **Phase 3.5 the plumbing** ([`pulse-plan.md`](pulse-plan.md)): extensions report items, urgent ones text at once, normal ones ride an hourly "btw", quiet hours and told-once dedupe. What's left is the model-ranked part and the items below.

**Depends on.** The Phase 3 scheduler and extensions (built), data sources (Phase 7 for calendar and mail), and the status relay if nudges should also be visible remotely.

**Done when.**
- A sweep sends at most N useful nudges a day with no repeats.
- The owner can mute a category from iMessage.
- Every nudge links back to its source.

## Slow-reply acknowledgement ("on it")

*Deferred 2026-10-13, from the local-model latency fix (Instruct-2507 default).* **Brain loop: done in BR-1** (`On it…` after 6 s, once per task, dedupe `progress:<task id>`); the intents path still has none.

**Problem.** A routed reply takes one label call, up to three tool rounds and a final answer. That's usually 1-3 s, but a cold model or a long thread can take much longer, and the sender sees nothing until the answer arrives. (Claude workers already ack at once: `On it: R4`.) iMessage gives no typing indicator over AppleScript.

**Proposed design.**
- **Timer, not a prediction.** When the runner starts a task, arm a timer (`router.ack_after_s`, ~4 s: above the llm path p95). If the reply isn't sent by then, send one short ack ("on it, back in a moment") and then the real reply. Fast-path replies never ack. Predicting slowness from the intent is brittle; a timer is exact and costs nothing when the reply is quick.
- **Exactly once.** Send through the channel with `dedupe_key=ack:<task id>` and record `acked: true` in the checkpoint, so a retried step never acks twice. A cancelled or failed task after an ack should say so ("couldn't finish that: …") instead of going silent.

**Trade-offs.** One extra message per slow reply. An ack that lands a few hundred ms before the answer is noise, so set the threshold from the measured p95, not the p50. Counts toward the outbound rate limit.

**Done when.** The local runner acks only when a reply exceeds `ack_after_s`, never twice for one task, and the e2e bench reports how often it acked.

## Tool selection as the tool catalog grows

*Noted 2026-10-13, from the local-model latency fix.*

**Today.** The labeler's intent already picks the toolset (`INTENT_TOOLS`: question none, system 5, job_control 3, updates 3, notes 4, reminders 3, ops 5, rules 4, run_project 12), filtered by principal. With a handful of small tools in the prompt the model isn't overwhelmed: the measured latency was the thinking-only model reasoning before each call, not tool count.

**When to revisit.** `run_project` already has 12 tools, near the threshold below. Keep the two-level shape (intent, then that intent's tools): extensions get their own `extension:<name>` intents (already reserved), so each prompt still carries only a handful of tools. Add retrieval (BM25 or embeddings, top-k over tool descriptions) only when one intent's toolset passes roughly 15-20 tools. Retrieval changes the tools block per message, which breaks the byte-identical tools prefix (KV-cache reuse), and tool descriptions become a prompt-injection surface for selection, so extension descriptions need review.

**Cheaper win first.** For intents whose tools take no arguments (`system`: `daemon_status`, `model_status`, `system_status`, `list_commands`, `list_extensions`), the tool round could be skipped: run the toolset directly and let the model only narrate (the final `chat_json`). That saves one model round per system question. Measure with `resonant bench e2e --model real` before and after.

**Two more measured wins (2026-10-13, Instruct-2507, e2e bench).** (1) After the last tool result the model's no-call `chat_tools` turn already writes the answer as plain text (~5-20 tokens), and `_final` then asks again via `chat_json` (median ~0.9 s on `job_control`). Using that text as the reply, through the same number guard and length cap, would save one round. (2) With earlier replies in the thread, the model sometimes answers a repeated question from history without calling a tool, which can be stale. Consider a system-prompt rule to always re-read live state for `job_control` and `system`, or dropping prior replies for those intents.

## Veto windows for unattended jobs

*Deferred 2026-10-13 from Phase 2, and again 2026-10-14 from Phase 3 ([plan §8](phase3-plan.md#8-out-of-scope-and-deferred)): asking jobs are rare and fire at most hourly, so asking works for now.*

**What.** "Doing X in 10 min unless you stop": a `scheduled` L2 returns `Veto` and parks in `waiting(veto)` (`approvals.kind=veto`) with `wake_at` = the deadline, and runs unless the owner replies `stop`. `Veto` is already in the gate contract and is never returned today. Carried design:
- A `stop` beats deadline expiry: a `stop` whose `occurred_at` ≤ the deadline wins even if it's ingested after it (reader watermark plus no unrouted earlier events), and so does one within a short late-stop grace after the deadline, if the call hasn't started.
- An ambiguous `stop` (several vetoes pending) converts them all to asks.
- A veto may run only if its prompt was recorded as sent at least the window before.
- Late (the daemon was down past the window) or under `/kill`, it converts to an ask.

**Done when.** A job's L2 action runs after its window with no reply, never runs after `stop` or `/kill`, and a missed window asks.

## Approval classifier and `resonant eval approvals`

*Deferred 2026-10-13, from Phase 2. The deterministic parser covers the replies people actually send.*

**What.** A small model call for replies the parser misses ("sure go ahead", "nah"), seeing only the reply and the code-built summary, approving only at confidence ≥ 0.9 and never for L1 or pay. `resonant eval approvals` scores it on `evals/approvals.example.jsonl` and a real set in `~/.resonant/evals/`.

**Done when.** Misses get a classifier decision or a re-ask, never a wrong approval, and the eval reports precision on approvals.

## Batch subsets (`yes 1,3`)

*Noted 2026-10-19 from BA-1 ([`brain-plan.md`](brain-plan.md#ba-1-batch-approvals-2026-10-19)). "Batch approvals (`yes all`)" is closed: BA-1 built batches as one task's changes under one code.*

**What.** Approve part of a batch: `yes 1,3` (or `no 2`) runs the numbered changes and drops the rest. Left out of BA-1 on purpose: the parser would need a new grammar that can't be confused with a code reply, the gate would resolve some approvals of a batch and reject the others in one transaction, and the owner can already say `no` and ask again for the subset.

**Done when.** `yes 1,3 D7` (the code stays required for an L1 batch) approves exactly those approvals of that batch, rejects the others, and the reply reports each.

## Note undo

*Deferred 2026-10-13, from Phase 2. Deletes are soft (`deleted_at`) so this stays possible.*

**What.** `/undo` (or `/unnote undo`) restores the most recent `/unnote` or `/notes clear`.

**Done when.** A cleared or deleted note comes back with its number, and undo of an undo is refused.

## A `resource` wait reason for runner runs

*From the P5A-3 review.* A run that finds the runner full or Resonant paused returns `Wait("usage_reset")` because `tasks.wait_reason` has no better value. `/status` then counts it under usage resets.

**Done when.** `resource` is added to `WaitReason` and to the `tasks.wait_reason` CHECK in the next migration that touches `tasks`, and `ClaudeRunner._defer` uses it. (The runs table's own `wait_reason`, migration 011, already separates `usage_reset` from `question`.)

## Ops hardening

*Noted 2026-10-13, during the Phase 2 build.*

- Run ops subprocesses with a minimal environment, not the daemon's (the PATH is already system-only since EM-1; the rest is still `os.environ`).
- Scrub more from `/logs`: paths under `$HOME`, tokens that look like keys.
- A cooldown on `restart_ollama`, so a loop of requests can't keep the model down.
- `/update` runs whatever is on `origin/main`: anyone who can push there gets code execution on the Mini. Consider requiring a signed commit or tag, and branch protection.

**Done when.** Each item is done or explicitly accepted in the decisions log.

## Bare `yes` / `no` with nothing pending

*Noted 2026-10-13, during Phase 2. The owner may revisit.*

**Today.** A plain `yes` or `no` with no approval and no worker question pending (with a question open it goes to the question, EM-5) gets `Nothing is waiting for approval.` instead of going to the chat path, so a casual "yes" in conversation is answered as an approval reply.

**Decide.** Keep it (clear and safe), or route a bare reply to chat when nothing has been asked recently.

## Clamp future `occurred_at`

*Noted 2026-10-13, during the Phase 2 build.*

**Problem.** The sent-before rule compares the prompt's send time with the reply's `occurred_at` from chat.db. A phone clock running ahead makes a reply look later than it was, so a `yes` sent just before a prompt went out could bind to it. (A clock running behind only refuses instant replies, and the owner re-sends.)

**Done when.** The reader clamps `occurred_at` to at most a little past its own read time, and a test covers a skewed clock both ways.

## Timing-flaky tests

*Noted 2026-10-13, from Phase 2 CI.*

`test_reads_200_rows_under_100ms` and `test_bench_e2e_cli_json` sometimes fail on a loaded CI runner and pass on a rerun. Give them headroom (a relative bound, or a marker that skips the timing assert in CI) so a red build always means a real failure.

*2026-10-17, TR-1 CI:* `test_runner_fork.py::test_owner_merge_takes_over_an_auto_merge_watch` (GT-1) failed once on CI: right after `/merge` it expects the auto watch's task to be `cancelled`, but a watch caught mid-step is still `running` until its step returns. It passes locally. *Fixed in RC-1:* it wasn't only the test. `_watch_merge` replaced an auto-merge watch only while it was `queued` or `waiting`, so an owner `/merge` that landed while the watch was mid-step left the old watch running beside the new one. It now cancels a `running` watch too (`Loop.cancel` cancels the step), and the test waits for the status. It failed on both of RC-1's CI runs before the fix.

## Labeler near-misses

*Noted 2026-10-13, from the Phase 2 router eval on the real model.*

The real model misses 6 of 190 (95 rows ×2, EM-8: 96.8%, still above the 90% bar): `question` 7/9 (the same three misses since EM-5) and `system` 11/12. Look at the misses, then decide between a few-shot fix and an eval case that pins them. Re-run after any prompt or model change, together with [router evals on real texts](#router-evals-on-real-texts-go-live-step-9).

## MCP stdio servers: env-token binaries and secret-gated discovery

*Noted 2026-10-16, from MCP-1 (built: [`extensions.md`](extensions.md) §4, MCP tools). HTTP transport built in MCP-2: an http server's bearer token stays in the daemon, and `ext add` sends it for discovery only after the owner says yes to that URL.*

**Open.**
- A third-party stdio server that reads its token only from an environment variable can't be used: secrets go on fd 4, never the env (a same-user worker could read the env). Options: a per-server "env secrets" grant shown as an escalation, or using the server's HTTP endpoint (bearer held in the daemon) where it has one.
- `ext add` lists a **stdio** server's tools with no secrets, so one that needs its token to list tools can't be installed. Option: the same ask-then-send as http servers, passing the declared secrets on fd 4 after the owner confirms the command line.
- Linear's tool names come from public listings, not Linear's docs (which name none): if `ext add repo:linear` reports `has no tool <name>`, rename it in `extensions/linear/resonant.yaml` (LN-1 renamed `create_comment` to `save_comment` after Linear did; the refusal now names the server's closest tools).
- A drifted tool stays disabled until a restart or `ext update`; there is no `/ext` command to re-approve one pin from iMessage.

**Done when.** Each is decided, and built or dropped.

## Critical-job LaunchAgents and `job-guard`

*Deferred 2026-10-14, from Phase 3, with their only consumer (trade-jev). `ext add` refuses `critical: true` today.*

**Design to carry.**
- Critical jobs must be Allow-only: L0, or L3 with a report.
- `job-guard` stays the **parent** (no exec), so it can pipe the job's output through redaction.
- It checks `autonomy` and `paused`, the extension row, the job and the pin, and fails closed (exit 75).
- The command comes from the approved manifest snapshot, never from the plist.

**Done when.** A critical job fires with the daemon down, is skipped while paused, and refuses a tampered copy.

## Seatbelt sandbox for extension processes

*Deferred 2026-10-14, from Phase 3 (the D13 interim bar). Needs its own security review. It must land before any third-party or network source.*

**Update 2026-10-16.** `resonant/sandbox/` exists, but its one profile is now the allow-default thin guard for Claude workers (EM-1), which protects Resonant and nothing else. Extension processes need the opposite: a deny-default `extension` profile (it is in nobody's critical path, and on by default it risks breaking the github digest and pulse reporters). What's left here: a second template in `resonant/sandbox/`, opt-in per extension (`sandbox: true` in config), probes for `gh api` and the example extension under it, then default-on once both are proven. It can reuse the thin guard's mach denies (launchd, securityd, window server, HID, accessibility, Apple Events) and its port denies on every address.

**Deny-default design.**
- Deny `lsopen`, `launchctl`, `open`, `shortcuts`.
- Deny `mach-lookup` of `securityd` / `com.apple.SecurityServer`.
- Deny `process-info` and `signal` on other processes.
- Deny loopback (including Ollama `:11434` and the API `:7777`) and unix sockets, except mDNSResponder.
- Deny the pasteboard, Apple Events, and exec from the data dir or `TMPDIR`.
- Deny `file-read*` under `$HOME` outside the extension and data dirs.
- Go CLIs such as `gh` need `trustd` for TLS. `sandbox-exec` is deprecated but works; verify per macOS release.

**Tests.** A copied `security` binary, ctypes `SecItemCopyMatching`, and a read of `~/Library/Mail` are all denied.

## iMessage `/ext add|update|remove`

*Deferred 2026-10-14, from Phase 3. A terminal confirmation is enough for first-party code, and it keeps remote installs out of the threat model.*

**Done when.** There is a design for binding a remote approval to `{source, commit, tree_digest, manifest_sha}` with a typed confirmation, reviewed, and it ships only after the sandbox.

## Git and network sources, Python deps

*Deferred 2026-10-14, from Phase 3.*

**What.** `git+https://…` sources and extensions with dependencies (a per-extension venv). They need the Seatbelt sandbox, clone hardening (no hooks, no submodules, pinned commits, never tags), and a `uv sync --locked` review first: install-time code must not run unreviewed.

## Extension runtime extras

*Deferred 2026-10-14, from Phase 3.*

- An HTTP `emit()` for long-running extension processes (trade-jev).
- Per-extension network allowlists (an `srt`-style proxy).
- A separate macOS user for extensions.
- CPU and memory limits.
- Retention for tasks, audit rows and events (L0 jobs already make no task rows).

## Phase 3 build notes

*Noted 2026-10-14, during the Phase 3 build. Each is small; fix or accept it in the decisions log.*

- **Router accuracy with extensions.** The advisory eval at `ext add` showed builtin accuracy dip from 95.6% to 91.1% with the extensions installed. Tune the intent descriptions and examples with [real texts](#router-evals-on-real-texts-go-live-step-9).
- **Notify cap edge.** A crash between a send and its `ext.notify_sent` audit row leaves that send uncounted, so after a crash at the cap an hour can carry one notification over its 20.
- **Briefing reads are capped.** The briefing reuses the builtin reads, which return at most 50 items (`list_reminders`, `projects`), so its counts undercount past that.
- **L0 jobs are at least once within catch-up.** A crash after an L0 run's process finished but before its result was recorded re-runs it after the restart, if still within `catchup`. L0 jobs are reads, and events dedupe by key.
- **JobRunner events after a crash.** A gated job's events are lost if the daemon crashes between recording the execution and finishing the task step: the retry returns the recorded result, without the events.
- **`@me` in GraphQL search** (`author:@me`, `review-requested:@me`) is unverified against the live API; go-live step 12 checks it. If it doesn't resolve, set `author` in `~/.resonant/ext-data/github/config.json`.
- **`gh` and `git` accept arbitrary arguments** as argv[0] until the sandbox: a manifest can run any `gh` subcommand. Only first-party, reviewed manifests are installed meanwhile.
- **Vendored packages aren't detected.** The stdlib-only check looks for dependency files, not a vendored `site-packages` or copied third-party modules in the tree. Review catches it for now.
- **Manifest approvers name a principal.** The shipped manifests say `approvers: [owner]`, so `ext add` refuses unless the owner principal is named `owner` (go-live step 4 names it that). The owner is always an approver anyway; consider dropping the line or treating the owner's role as implicit.
- **Manifest rows saved before P3-14** stored `manifest_json` with sorted keys, so their positional `/<ext> <tool>` arguments would follow alphabetical order. No production install predates P3-14, so nothing to migrate; `ext remove` and `ext add` rewrite a row.

## Pulse build notes

- **Urgent send races the pulse.** `Pulse.route` marks an urgent item told after `channel.send`; a pulse running in that window composes the same untold item and sends it again under another dedupe key. Narrow (top of the hour); fix with an in-flight skip if it shows up.
- **Failed pulse job after its send.** The scheduler consumes a due time even when the job raised, so no catch-up reruns it. `deliver` now finishes any earlier unsent in-flight record before composing a new message (found by the e2e), but only for records under 2 hours old whose items are unchanged (older or changed ones are abandoned and recomposed fresh, oldest first otherwise). A resend that fails is logged and retried on the next delivery until the record ages out. A manual `/pulse now` in the quiet hours, or the 08:00 briefing, also flushes such a record first.
- **Tool runs don't persist fd-3 events.** Only job runs store events, so a reporter must be a job (the example's `incident` is a job plus a tool that writes the state it reads). A tool that calls `report()` is dropped silently.
- **The first job run after install is a silent baseline.** For a reporter that means whatever it reports on that run is stored as told: install it, run the job once (`/ext run <ext>/<job>`), then expect news.
- **Muted urgent items ride the pulse after the mute.** A muted source's untold items stay pending (not dropped), so the first pulse after the mute ends carries those still open and unexpired; a short `ttl` lets one expire instead.
- **Briefing Updates exclude `core` and muted sources**, and urgent clears inherit urgent; if every recipient of an urgent send refuses it, the item stays pending for the pulse.

## Pulse: deferred (from `pulse-plan.md` section 7)

*Noted 2026-10-14, at the end of Phase 3.5.*

- **Local push endpoint.** A long-running process (a trading bot, an on-call listener) calls the daemon directly instead of waiting for a job period (5 minutes at best). Loopback only, a per-extension token in the Keychain, same `report()` payload and the same pulse path, so grants, dedupe and the cap apply unchanged.
- **Model ranking, feedback and snooze.** A ranking step over the open items (novelty, urgency, relevance), "not useful" replies that lower a source or key, and per-item snooze (`/snooze <key> 4h`). This is the nudges follow-up above; the pulse gives it the items store and the told-once state to rank over.
- **Status relay visibility.** The remote status shows the open pulse count (Phase 4); the pending (untold) count isn't in it yet ([status relay](#status-relay-whats-left)).

## Approval "expires soon" reminder

*Found 2026-10-14, after the pulse shipped.* The pulse's approval re-mention runs only at the top of the hour, and approvals expire after 30 minutes, so it catches only approvals asked between :30 and :50 past an hour. A reminder scheduled by the approval runner itself (e.g. `[A7] still waiting, expires in 10 min`, once, at half the TTL) would cover every approval. Then drop `core/approval` from the pulse. (A worker check's `trust_check` approval lives `runner.ask_timeout_minutes`, 240 by default, so the hourly pulse already covers those.)

## Secrets on disk for `gh` (P5A-7)

*Noted 2026-10-15.* No extension child gets a secret in env or argv (fd 4 pipe). (Claude workers are different by design: the session gets its Claude token in env, and reads repo keys from `.env`.) The github extension still has to hand its token to `gh`, which reads only an env var or a config file, so it writes a 0600 `hosts.yml` into a 0700 dir under the extension's data dir for the length of the run. A run ended by SIGKILL (a timeout, after SIGTERM's grace) can leave that dir until a later run sweeps `resonant-gh-*` dirs older than an hour. Calling the GitHub API from Python with the token in memory would remove the file; deferred because the extension is built on `gh`'s GraphQL, pagination and error text. Also: a descendant started through a shell (not `subprocess` defaults) could inherit fd 4 before the SDK reads it; first-party extensions don't.

## Superseded by the employee model

*2026-10-16, EM-9.* Removed from this file because [`employee-plan.md`](employee-plan.md) answered them: **reusing claude-surf for `run_project`** (Resonant now spawns its own Claude workers, EM-2/EM-3), **runner sandboxing** and **running sessions as a separate macOS user** (the owner chose the thin guard: workers run like the owner at the keyboard, and the guard protects only Resonant), and **a pty allow inside the runner sandbox** (the thin guard leaves ptys open). [`repos-plan.md`](repos-plan.md) waves RP-2, RP-3, RP-6, RP-7 and the RP-10 grants idea, and [`phase5a-plan.md`](phase5a-plan.md)'s strict sandbox, fresh clones and bundle handback are historical.

## Employee mode carry-overs

*Noted 2026-10-16 from the EM-1..EM-8 "As built" notes ([`employee-plan.md`](employee-plan.md)). Each is small; fix it or accept it in the decisions log. EM-10 swept the old model's leftovers in code and docs.*

- **Checkout `.git` is worker-writable** (EM-2, accepted). A worker can plant hooks, move your branches or change config in a checkout. Daemon git there runs tokenless under the guard (EM-3), so it can't hurt Resonant, but your own next `git commit` runs a planted hook. The base already comes from the daemon-only mirror (EM-3). Hardening left: deny writes to the checkout's `.git/hooks` in the guard.
- **Guard gaps** (EM-1): hardlinks that already exist into a protected file aren't covered (keep uv's link mode clone or copy; nothing sets `UV_LINK_MODE` yet); port 7777 is fixed in the profile and the self-check (derive it from `api.port` if that changes); `~/Library` and `~/.config` are write-denied whole (narrow them if workers need `~/Library/Caches`).
- **Per-run App token is one repo** (plan §6). An ops ask that spans repos can't push to the others; widen through `resonant check` if it comes up.
- **A fork run pushes before its checks** (EM-7). A token the checks then find is already public on the fork, though nothing goes upstream. Rotate it if that happens. The App covering a new fork is confirmed only on the first real fork run.
- **Model-chosen `answer_worker`/`message_worker` run without asking** (EM-5). A worker's own summary could in principle nudge the model into messaging another run; the deterministic paths cover the usual replies.
- **Visibility** (EM-5, EM-6): the relay's "which one?" stash and the brain's pick aren't shown in `/approvals` or `/runs`; a question that times out sends no text (the transcript and the digest line show it); the note-text cap (6 texts per run an hour) is in memory and resets on restart.
- **Small gaps:** time blocked inside one `resonant ask` call (up to 8 minutes) counts as session time (EM-3); no `resonant approvals approve --always` (EM-4); a renamed repo's checkout isn't moved and the checkout's `.git` growth isn't under the disk watchdog (EM-2); `/run blog fix x` with a repo only GitHub knows is a scratch run unless written `/run blog: fix x` (EM-8); `merge_ci_pr` doesn't wait for CI (EM-8); no default trust rules ship (`created_from="default"` is reserved, EM-4); no per-repo auto-merge default, so merge intent comes only from the ask (EM-7); the `run_project` intent keeps its name (repos-plan D8's rename to `repos` is optional and not done, EM-8).
- **A fork run's PR-open race** (EM-7). Between the post-yes recheck and `gh pr create` the fork branch could move; it is caught afterwards (the PR is read back and closed on a head mismatch), not prevented.
- **Naming leftovers kept on purpose** (EM-10). The run step saved as `harvest` (it now finds the PR and runs the checks) is named in the runs CHECK (migrations 006, 009), so renaming it needs a table rebuild. `runner.max_bundle_mb` is deprecated and ignored, accepted only so old config files load; drop it from config files.

## GT-1 carry-overs

*Noted 2026-10-16 from GT-1 ([`employee-plan.md`](employee-plan.md#gt-1-gates-sufficient-not-excessive)).*

- **Standing allows are per tool and scope only.** A `yes always` on `github.merge_pr` in a repo covers every PR there; there is no per-argument rule (a branch, a size). Narrow it with an `ask` rule if needed.
- **Injection budget is five effects** (three until BA-1). One injected read in a chat task can steer up to five side effects that run unasked (L3, or tools with a standing allow); a tainted task's changes all ask (in one batch). Revisit if extension reads grow; a per-task cap after an extension read is a cheap tightening.
- **Anonymously registered repos stay `push`.** `add_repo` without the daemon token can't see push access, so a public repo the owner can't push to is registered `push` and its runs fail to push; re-add with the token to get fork mode. The anonymous lookups share GitHub's 60/h per-IP limit.
- **Narrowed flagged paths.** Makefile, justfile, Dockerfiles, `conftest.py`, `tox.ini` and new executables merge without asking now; they run only when someone runs them. Lifecycle scripts in `package.json`/`setup.py` run on the next install and are listed (`deps changed`), not flagged. Add any back to `runner/checks.py` if that proves too loose.
- **Restart and update take `yes always`.** `restart_resonant` and `update_resonant` are L2 builtins the owner can give a standing allow (scope `*`), by the ticket's "any L2 approval". With one, a steered chat model can restart or update the daemon unasked (update only pulls `main`, which the owner controls). Forget the rule (`/rules forget <n>`) to go back to asking, or add them to `trust.tools.NEVER`.
- **No cumulative spend cap.** Below `trust.cost_ask_over_usd` a seen action never asks, however often; there is no per-run or per-day total. A negative `cost_usd` never asks (as before).
- **Anonymous registration via a link.** An extension's linked repo (`via`) can register a public repo without the token too.
- **Worker side-effect reporting is honor-system.** `did:` notes are what the worker chooses to report. Since TR-1 Resonant also reads the worker's own commands (see below), so a `gh pr merge` it ran itself is listed as `(detected)`. The note-text cap (6/h per run) is in memory and resets on restart; a worker's restart text goes to the run's requester, "Back up" to the owner.
- **No owner text on MCP pin migration.** v1 pins that migrate are audited (`ext.mcp_pin_migrated`) and logged only.

## TR-1 carry-overs: the worker's transcript and Remote Control

*Noted 2026-10-16 from TR-1 ([`employee-plan.md`](employee-plan.md#tr-1-reading-the-workers-transcript)).*

- **Detection reads command text only.** A side effect inside a script (`./deploy.sh`, `make release`, `npm run deploy`), a Python or Node program, a subagent's own tool calls (they aren't in the parent's stream-json) or an MCP tool with a neutral name isn't seen. Aliases and shell functions from the owner's rc files aren't expanded. A command that failed is still listed (it says what was run, not what happened). A ref in a shell variable or `$(…)` is skipped, so `git push origin "$BRANCH"` to main is missed. A detected merge is texted even when the worker also sends a `did:` note (the note arrives later; the line dedupes, the text can't). Widen `runners/side_effects.py` from real transcripts; `make`/`npm run <script>` names could be matched against a per-repo list of deploy targets.
- **No model one-liner.** `run_activity` is deterministic; the local model isn't asked for a one-line summary (the ticket made it optional). If replies feel too raw, add it after the headline, never instead of it.
- **Detected effects aren't in the dashboard's relay snapshot** and the per-run text cap (6/h, shared with urgent notes) is in memory, so a restart resets it.
- **Remote Control: shipped by RC-1, through an undocumented control request.** The TR-1 spike found no documented way (the `--remote-control` flag does nothing in print mode). RC-1 uses what Claude Code 2.1.299 accepts on a `--input-format stream-json` session: `{"type":"control_request","request":{"subtype":"remote_control","enabled":true}}` answered by a `control_response` with the `session_url` (sources: github.com/scharissis/polako/issues/471; upstream anthropics/claude-code #91906, #99364). See RC-1 below.

## BR-5 carry-overs: ask_claude and taint

*Noted 2026-10-18 from BR-5 ([`brain-plan.md`](brain-plan.md#br-5-ask_claude-and-taint-2026-10-18)).*

- **ask_claude: email and calendar.** Probed on Claude Code 2.1.302: under `--restricted` the init event has no claude.ai connector tools (`mcp_servers` empty), so `email` and `calendar` asks are refused. Options: (a) wait for the CLI to load connectors in restricted mode (re-probe after updates: one scratch `claude -p --restricted … --output-format stream-json --verbose` and read the init `tools`); (b) a non-restricted mode for those two kinds: `--tools` the read built-ins, `dontAsk`, `--allowedTools` the read allowlist and `--disallowedTools` every connector write (known names plus, from a cached init event, every discovered tool that reads as a write), with the init check killing the session if a write tool is present but not denied. (b) loads the owner's user settings (their `allow` rules could pre-approve a write), so it needs `--setting-sources` checked first. Owner decision.
- **Gmail tool names are a guess.** `brain.ask_claude.connector_tools.email` (`search_threads`, `get_thread`) and `draft_tools` (`create_draft`) must be checked against the init event once Gmail is connected in claude.ai and connectors reach the session.
- **WebFetch can carry data out.** A question (or a page or file Claude reads) can ask it to fetch a URL with something it read in the query string. The deny list keeps secrets out of reach and the roots are the owner's own code, so this was accepted for D8; a WebFetch domain allowlist would narrow it.
- **Is `Read()` deny enough?** Unverified on 2.1.302: whether `Read(...)` deny rules also bind Grep and Glob, and whether they resolve symlinks (a link under `~/code` to `~/.ssh`). If not, Grep inside a read root can still see a `.env` file's lines; then deny `Grep` and `Glob` with the same patterns (if the CLI takes them) or realpath the roots and drop links. One probe settles both.
- **`Read()` rules are literal.** The read tools fold case and NFC; the CLI's `Read(...)` deny rules (in `--settings`) may match only the exact case, so a `.ENV` or `ID_RSA` inside a read root could pass the CLI while the read tools refuse it. A glob with `[`, `{` or `**` may also mean something else to the CLI than to `fnmatch` (none of the defaults do). Emit case variants or confirm the CLI folds case with a probe.
- **A reply can still say "reply yes".** Approval-code look-alikes are masked in Claude's answer, but a bare `yes` binds when exactly one approval is pending and its prompt was already sent, so injected text could nudge the owner into approving something they had already been asked. The `Claude:` prefix marks the source; a stricter fix is to require the code while an ask's answer is the newest message.
- **Proposals are text only.** A `Proposed:` line is flagged (`result.proposed`) but nothing acts on it; the gated send/event tool is later work (D8).
- **No transcript.** Ask sessions keep no transcript (only the `ask.done` audit with status, cost and turns); add one under `transcripts/ask-<id>.jsonl` if debugging needs it.
- **Usage-limit backoff is shared kv, separate from workers.** A worker hitting the limit doesn't stop asks (and vice versa) until each hits it itself.

## BR-1 carry-overs: the brain loop

*Noted 2026-10-18 from BR-1 ([`brain-plan.md`](brain-plan.md#br-1-the-brain-loop-2026-10-18)).*

- **The ask's answer isn't in later turns.** *(BR-10: closed for asks from a brain task: the answer is kept as `claude_reply` on the asking task and replayed as a `<claude_answer>` data turn; media asks still aren't.)* History is the principal's messages and the replies of the tasks they started, so `Claude: …` (an `ask` task with no origin event) never shows: "and where's that?" after an ask lacks its answer. Add finished asks for the principal to the turn (as untrusted data, like `<since_last>`), and taint any task whose prompt holds one.
- ~~**Taint is per task.**~~ *(BR-10b: closed. A turn whose replayed window holds a tainted task's reply or a Claude answer starts tainted; the fold keeps neither.)* A reply that quoted a file goes into history as an assistant turn, and the next task starts untainted with that text in its prompt. Options: mark replies of tainted tasks (`reply_meta.meta.tainted`, already saved) and taint a turn whose history holds one within the last few exchanges, or wrap such replies as data. Weigh against asking more often.
- **The briefing holds worker text.** `Runs:` notes and `Workers ask:` questions (written by workers, which read PRs and the web) are in every turn's briefing, so an untainted turn already sees them. Options: drop free text from the briefing (codes, states and counts only, with `run_activity` for the rest) or taint any turn whose briefing has it. Decide with BR-6's evals.
- ~~**Loaded tools change the tools block.**~~ Gone with `load_tools` (BR-1b): every tool is offered from the start (core tools plus stubs), so the tools block is fixed for the task.
- **Long steps.** A stuck turn can take 8 rounds plus a narration (`brain_step_limit`: 9 × `model.timeout_s` + slack, about 9 minutes with the defaults) and holds Ollama's one slot. A wall-clock budget per turn (escalate after ~60 s) would bound it.
- **A crash retry starts the turn over.** Loaded tools and the transcript of tool results aren't checkpointed; a step that dies mid-loop re-runs from the context (reads replay from `executions`, a pinned side effect resumes). Fine at today's scale.
- **The fallback reuses call indexes.** The intents path after the loop counts calls from 0 in the same step; a read the loop already ran with the same args replays its recorded result (harmless), a different call gets its own key.
- **Number guard is log-only.** BR-6's evals should read `Done.result.number_guard` and decide whether a hard guard comes back for the loop.
- **Progress before an approval.** A slow turn that ends in an approval prompt can send `On it…` right before the prompt.

## BR-1b carry-overs: the loop on the live model

*Noted 2026-10-18 from BR-1b ([`brain-plan.md`](brain-plan.md#br-1b-the-loop-on-the-live-model-2026-10-18)).*

- **The real eval set.** The example set is synthetic and every number below is on it. BR-8 switched the default on it because the owner's set (`~/.resonant/evals/task.jsonl`) didn't exist yet: write one and run `resonant eval task --mode loop --repeat 3` on it; the prompt was tuned against the example set's failure modes, not its rows, but only real texts show what's left. Under 85% there: `brain: {mode: intents}` until it isn't.
- **Run-to-run noise.** At temperature 0 the live loop still moves ±2–4 cases between runs (the same prompt can pick a tool or answer from the briefing). The bar is the mean of a `--repeat 3`, not one run.
- **The intents path still replays calls as text.** `LocalRunner._tool_rounds` writes `{"tool_call": …}` assistant turns; it has three rounds and rarely a second call, so it was left alone. Migrate it to native turns (`ChatMessage.tool_calls`, `role: "tool"`) if it ever leaks the text format. BR-8 kept the intents path (opt-in and the loop's per-turn fallback), so this stays open.
- **Server-side name coercion.** Ollama's grammar maps a call to a tool that wasn't offered onto one that was, and the client can't see it happened. The brain offers every tool, so it can't happen there; anything else that calls `chat_tools` with a subset (the intents path) can still get a different tool than the model meant. A cheap tell: content empty and the call's name not in what the model would plausibly pick; better, offer the full set or use raw generation.
- **Stub args and the grammar.** A stub's schema is the real one without descriptions, limits or formats, so the grammar shapes the call but a `minimum` or `pattern` is only checked in code (`args_error`, one extra round). Fine for builtins; an MCP tool with many optional fields gets them all as optional in the stub.
- **Prompt size.** A first turn is about 5.2k tokens (system, core tools plus stubs, briefing), cached after the first turn. A history fold, the labeler or a pulse call between turns evicts Ollama's one slot, and the next turn pays the whole prefill again: about 4.3 s on the Mini (measured idle), against 0.05 s warm. If that shows in real use, keep the brain on its own slot (`OLLAMA_NUM_PARALLEL=2` costs memory) or trim stubs for tools a principal never uses.
- **The claim check is English and pattern-based.** `claims_change` catches "Added …", "I've saved …", "I'll remember …"; other phrasings get through and the owner sees a claim no tool made. The report lines are code-built, so a real change always shows as one; a missing report line is the owner's tell.
- **The target check costs a round when the owner named the target in other words.** "stop the dori run" → `cancel_run{code: R1}`: "R1" isn't in the message, so the call goes back once and runs on the second try. It only looks at `code`, `id`, `number` and `*_id` args.
- **Eval fixtures.** The eval's `HOME` is the world root during a case (so `~/code` is the world's code dir, as on the Mini), and the linear fixture now has the real extension's argument contract (`list_issues` no args, `save_issue{title, team}`), since a CLI manifest can't mark args optional. If manifests ever get optional args, give the fixture the MCP schema's filters back.
- **Offering to do what data asks.** In `inj-log-orders` the model sometimes ends "… restart Ollama and add a note 'pwned'. Should I do that?". Nothing runs (a yes would start a new task, and the read tainted this one), but the prompt rule "don't offer to" isn't always followed. The row also fails when the reply merely quotes the line (`not_facts: pwned`); left as is.
- **Rows that grade wording.** `runs-waiting` (fact `mycelium`) and `ext-prs-failing` (fact `httpx`) fail on correct answers that name the run or PR by code instead (R2, `#17 in justinhe16/dori`). Not changed: the owner may want the names; if not, widen the facts.
- **Briefing paths are absolute.** Rendering repo paths as `~/code/…` made the model invent a home (`/Users/owner/code/…`) and fail every read; the briefing keeps absolute paths.

## Scheduler inline jobs onto ToolCore

TC-1 put every tool call on `resonant/toolcore/`, except one: the scheduler's inline L0 jobs (`extensions/scheduler.py`, `_call`) still call `gate.evaluate` and `gate.consume` themselves and then run the job without the executor (no `executions` row, by design: no task exists). It is the one listed exception in `tests/test_toolcore_boundary.py`; TC-1 left the file alone while BK-1 worked in it.

- **Open:** a `ToolCore` entry for an inline, unrecorded call (gate, token spent, no executor) that both the scheduler and the router's fast path use (the fast path already goes through `toolcore.allow_inline`), then drop the exception.
- **Maybe:** check args against the schema on the intents path too (`check_schema=True`; it is False there today so TC-1 changed nothing). Small, but a behavior change: its own ticket with an eval run.
- **Done when:** `ALLOWED` in the boundary test is empty.

## BR-11 carry-overs: the Claude brain

*Noted 2026-10-19 from BR-11 ([`brain-plan.md`](brain-plan.md#br-11-the-claude-brain-one-long-lived-session-2026-10-19)).*

- **Live CLI: what's checked, what isn't.** A live smoke on the Mini (2026-10-19, `ClaudeSession` + `session_argv` with the real `claude`, a scratch cwd and store, an empty Resonant MCP server, the owner's login) passed: a `--restricted` stream-json session with `--session-id` answered, a second message in the same process remembered the first, the RC-1 stop kept the id, and a new process with `--resume` remembered it too. The stream also carries `rate_limit_event` lines (ignored today; they could warn before a limit hits). Not checked live: what an unknown `--resume` id does in stream-json mode (exit at once, or wait for input then error; both are handled: a lost resume restarts primed), whether auto-compaction keeps `--append-system-prompt`, and Remote Control on a session (RC-1's request, checked for workers).
- **A fresh session after outside reads.** *(Review of BR-11: the context used to outlive the hop window.)* A session that read outside content is restarted (primed) once that turn's taint has lifted, so nothing it read survives untainted. The cost: one cold start (~seconds) a couple of turns after any web, file or log read by Claude. If that's too often, lower `MAX_TAINT_HOPS` for the session or accept the residual (the old behavior) instead.
- **Transcripts stay on disk.** Resume needs session persistence, so the brain's conversation (owner texts, what Claude read) is kept by Claude Code under `~/.claude/projects/` for the session's cwd (`<RESONANT_HOME>/ask/session`), as a worker's is. A sweep of old brain transcripts (rotated sessions) isn't done.
- **Stray `result` lines.** The session reads the next `result` as the turn's answer. Nothing else writes into it today; with Remote Control on, a message typed in the Claude app joins the same conversation and could finish a turn or call Resonant's tools under the turn bound then. Remote Control is off by default; treat it as the owner's own seat.
- **An orphan after a daemon crash** is reaped at the next owner turn (`_reap_left`), not at startup; it does nothing meanwhile (stdin closed with the daemon).
- **The tool list is fixed per session.** Claude Code lists Resonant's tools when the session starts (`--allowedTools` names them too). An extension installed or updated mid-session isn't offered until the next start (a rotation, a restart or a crash). A `notifications/tools/list_changed` from the relay plus a fresh `--allowedTools` would need a restart anyway.
- **No `--max-turns` in a session.** It may count across the whole session in stream-json mode, so it's left out; a turn is bounded by `turn_timeout_s` (240 s) only. With API billing, `--max-budget-usd` is the whole session's (`max_budget_usd` × 50); the rotation resets it. Check the CLI's per-turn semantics and tighten if they're per turn.
- **A pause from outside the daemon.** `/kill` over iMessage runs in the daemon and its pause hook stops an idle session at once; a pause written by another process (the CLI) is seen by a turn in flight (polled) but an idle session stays up, doing nothing, until the next turn refuses or the daemon restarts. A periodic check of the flag would close it.
- **The queue polls.** A queued turn looks again every second (`Wait("usage_reset", +1 s)`), so the next one starts up to ~1 s after the last finishes. A loop wake from the ask runner would make it immediate.
- **A queued turn's ack is fixed at enqueue.** "after X, then this" names what was ahead then; if X finishes before the ack goes out the ack is slightly stale. Harmless.
- **The turn that hits a limit isn't re-answered locally.** Past the daily cap or the runaway guard the refusal happens at enqueue, so qwen answers that same text. A usage limit is only seen in the session's `result`, after the ack: that turn's reply is the limit notice, and the owner asks again (later texts go to qwen until the reset). Re-routing that one text to the local loop would need the ask runner to start a brain task for the same message.
- **One owner-only session.** Members can't escalate at all (`ask_claude` has no grant); if a member grant is ever added, their asks are one-shot (`enqueue` checks the owner). A per-member session would need its own server, kv and queue.
- **Email, calendar and images stay one-shot.** The session's allowlist is fixed (the read built-ins and Resonant's tools), so a `kind: email|calendar` ask (connector reads per kind) and an image ask (files in its own folder) start their own `claude -p` as before. Connectors don't load under `--restricted` anyway (BR-5).
- **Daily cap off by default.** `daily_cap: 0` means unlimited (the owner's change); the runaway guard (60 an hour across the brain, one-shot asks, media and watch checks) is the only automatic stop besides Claude's own usage limits. Watch `ask.queued` and `ask.notice` audit counts.
- **Live Claude eval.** As BR-10: the live task eval records hand-offs (`EvalAsk`), so it grades routing and writes, not Claude's answers in the session.

## BR-10b carry-overs: replayed outside text

*Noted 2026-10-19 from BR-10b ([`brain-plan.md`](brain-plan.md#br-10b-replayed-outside-text-taints-the-turn-2026-10-19)).*

- **More asks after a Claude turn.** *(BR-11: a Claude brain answer counts only when its turn was tainted under `MAX_TAINT_HOPS`, so clean session turns no longer cause this; one-shot answers still do.)* Every Claude answer counts as outside text, so the next turns ask before L2 and L3 changes: in continuous chat 24 turns (while the answer is in the window, then while the replies one hop from it are; `MAX_TAINT_HOPS` 2). With ~40% of turns going to Claude, that can be most turns in an active chat. If the asks get noisy: only count answers from sessions that read something (`ask.done` already records `tainted` for bridged sessions; a session with no tools at all only knew what it was told), lower `MAX_TAINT_HOPS` to 1 (no laundering hop caught: a reply repeating the text reads clean once the source is folded), or count only the last few exchanges (the injected text stays in the prompt either way, so these trade safety for fewer asks).
- **Laundering past two hops.** The price of decay: a reply two hops from the outside text (written by a turn tainted only by a hop-1 reply) reads clean, so if the model kept repeating injected text across turns, the turn after it is untainted once the source and the hop-1 replies are folded. Bounded (24 turns of chat after the source), and the fold drops every tainted reply's text. Raise `MAX_TAINT_HOPS` if this shows up; each hop adds about one fold window of asks.
- **The summary drops what tainted replies said.** A folded untrusted exchange keeps the owner's message and a marker, not the answer, so "what did that file say?" past the window has nothing to go on (the model can read it again). The alternative, keeping the text and tainting every turn while the summary holds it, would taint for good.
- **Intents-mode replies carry no taint flag.** Plain `runner="local"` tasks (opt-in `brain.mode: intents`, the router's labeled turns) don't track their own reads, so their replies never mark the next turn; only brain replies (`reply_meta.meta.tainted`) and pinned calls do. Track taint in the intents tool loop if `intents` mode comes back as more than a fallback.
- **The briefing and `<since_last>` still carry worker text untainted.** Unchanged by BR-10b (see BR-1's "The briefing holds worker text"): run failures and worker questions are in every turn. Same options as there.
- **Watch notices quote Claude.** A watch's notice (`brain/notices.py`) goes into the briefing as a pulse item, not history; it isn't covered by the history rule (the briefing item above).

## BR-10 carry-overs: triage and Claude with Resonant's tools

*Noted 2026-10-19 from BR-10 ([`brain-plan.md`](brain-plan.md#br-10-triage-and-claude-with-resonants-tools-2026-10-19)).*

- **No live Claude eval.** The task eval's live runs record hand-offs (`EvalAsk`) and grade them on routing, writes and forbidden tools only; scripted rows run `e2e/fake_claude.py` through the real bridge. A run with real `claude -p` sessions (optional) needs a scratch home with its own store and the owner's Claude login, and costs usage; it would grade Claude's answers and its tool choices.
- **`daily_cap` 30 may be low.** *(BR-11: the default is now 0, no daily cap, with a 60-an-hour runaway guard; past either the owner is told once.)* Every turn triage hands over is one ask. Past the cap the local model answers (silently). Watch `ask.queued` audit counts for a week; raise the cap, keep `brain.triage.bias: normal` (not `claude`), or use `brain.route: local` as needed.
- **Taint relies on the stream's order.** A Resonant change waits up to 3 s for its own `tool_use` in Claude Code's stream-json so every earlier read is counted. That assumes the CLI emits each `tool_use` before running the tool (true on 2.1.302); if it ever runs the MCP call first, every change counts as tainted and asks (fails closed, more asks, never fewer). Re-check after CLI updates.
- **Connectors still don't load under `--restricted`.** The bridge loads (an `--mcp-config` server), claude.ai connectors still don't (BR-5's probe), so email and calendar asks are still refused. Same options as BR-5's "ask_claude: email and calendar".
- **History replays Claude's answers as data.** *(BR-10b: the taint half is closed: a turn whose window holds a `claude_reply` starts tainted, and the fold no longer keeps Claude's text.)* A `<claude_answer>` user turn. The answer is noted after the send (also when the turn waits on an approval), retried for ~10 s while the brain task finishes; past that it's missing from history.
- **Approval TTL starts in the session.** The gate creates the approval when Claude calls the tool; the owner is prompted after the answer. A session longer than `gate.approval_ttl_s` (30 min) would see it expire before the prompt (dropped, safe). Not reachable with `timeout_s` 120. (BA-1: a batch's expiry restarts when it is prompted; a lone change's still starts in the session.)
- **Caps across a hand-off.** The 3-change (5 since BA-1) cap is per task: a brain turn can run 3 changes, hand off, and the Claude task 3 more (the escalation note tells Claude what already ran; nothing enforces it).
- **A down model spends the daily cap.** With qwen down, every turn goes to Claude with a canned ack and counts against `daily_cap`.
- **Stale bridge dirs.** A hard daemon crash mid-session leaves an `rsn-mcp-*` dir in the temp dir (a socket and a config with a dead token); sweep at startup if they pile up.
- **`looks_hard` is a regex floor.** English, shape-only (a leading why / how does, debug, compare, figure out, investigate…). "How's the mini" stays local; "is there a reason X broke?" isn't caught (the model's triage still can be). Widen from real misses; each widening costs Claude usage on small talk.
- **The ack is a model line, filtered.** `clean_ack` drops questions, links, braces and result words, but an ack can still be odd ("on it, checking the moon"). Canned acks are the fallback; if model acks look wrong in practice, use canned only.
- **One approval and three changes per Claude task.** *(Closed by BA-1: the changes that ask wait in one batch prompt, and 5 run unasked.)*
- **Media and watch asks stay read-only.** Only asks from a `brain` task get the tools (`tools/ask.py` checks the caller's runner); an image ask from `runner="media"` can't add a note from a screenshot.
- **The bridge's Python.** The MCP config runs `sys.executable -I brain/mcp_stdio.py`; a daemon running from a venv that moves (an update mid-session) breaks that session's tools only.
- **Triage holds the slot for one more call.** On a busy model, a turn that would have been local now pays the triage call first (80 tokens out, on the cached prefix); measure in the BR-10 eval row (ack p50, local p50).
- **The published architecture artifact** is built from `docs/architecture.html` (the brain node now shows triage, the ack and the MCP bridge); republish it after this merges.

## BA-1 carry-overs: batch approvals

*Noted 2026-10-19 from BA-1 ([`brain-plan.md`](brain-plan.md#ba-1-batch-approvals-2026-10-19)).*

- **qwen still sees an extension change's raw result.** In the local loop a change that ran goes back to the model as its result (a created ticket's key, say), so it taints the rest of the turn: with a standing rule for linear, the first save runs and the next ones ask (in one batch). Claude's bridge only ever shows the code-built `Done: …` line, so its saves don't taint (BA-1). Showing qwen only the report line would make them consistent, at the cost of the key in the answer.
- **qwen's own words give way to a batch.** When a local turn ends with changes waiting, its answer isn't sent (the prompt is, then the code-built results), as with one approval before. A turn that also answered a question ("what's on my Linear, and close RES-12") loses that answer; Claude's turns send theirs first.
- **The owner answers every batch.** A batch goes to the owner only, even when a member approves every extension in it (a lone change still goes to all its approvers). A member's own task's changes batch to the owner too.
- **Partly decided batches are dropped.** A batch approval resolved on its own (`resonant approvals approve <id>` resolves the whole batch, so only a race does this) leaves the rest unbindable: they're expired (`batch_split`) and that turn's changes don't run.
- **`allow_tools` reads the registry twice.** The prompt and the saved rules both come from the loaded extensions; an extension update between them (owner-only, at the Mini) could change the set. The report line lists exactly what was saved.
- **A capped change isn't asked.** Past `gate.batch_max` (10) a change is refused (`at most 10 changes can wait on one yes`), and past `MAX_SIDE_EFFECTS` (5) unasked ones are refused even when the next one would only have asked. Either could join the batch instead.
- **The kill switch only blocks on `paused`.** An approved batch runs with autonomy off but not paused (the gate asks for everything, and the owner said yes); `/kill` sets both.
- **Other approval lists show batch members as rows.** `resonant approvals` marks each row of a batch (`[batch of 4: decides all]`), and approving any row approves all of them; the dashboard snapshot (`pending_all`) still lists the members without that mark. The briefing and the pulse re-mention show a batch once, by its code (`approvals.answerable`).

## BR-9 carry-overs: what changed lately

*Noted 2026-10-18 from BR-9 ([`brain-plan.md`](brain-plan.md#br-9-what-changed-lately-2026-10-18)).*

- **The checkout can lag GitHub.** `recent_changes` never fetches, so on a checkout nobody pulls it shows what was last fetched (`behind` and `fetched_at` say so). `recent_prs` is GitHub's own view, once the extension is updated. A periodic `git fetch` of registered checkouts would close the gap; it isn't done because it writes to the owner's checkouts.
- **`asks_live_state` is a word list.** It catches commits, PRs, changes, newest, latest, merged ("status" and "recent" were dropped in review: too common in small talk). A live-state question in other words ("anything new on resonant?", "status of R4") answered with no tool isn't sent back. Widen it from real misses, not guesses: a broad list costs a round on small talk.
- **The guess check fires once per task.** A second guess in the same task runs (as the target check). The model can still guess an identifier the briefing happens to contain (a run code from the briefing is a real one; that's the point).
- **`recent_changes` checks `.git` itself, not every object file.** A plain `.git` dir, no `commondir`, `objects` and alternates inside the roots. A worker could still symlink single loose objects from another repo into a checkout; that needs write access it could use to read that repo directly anyway.

## BR-8 carry-overs: the cut-over

*Noted 2026-10-18 from BR-8 ([`brain-plan.md`](brain-plan.md#br-8-cut-over-2026-10-18)).*

- **The cut-over is on synthetic texts.** See "The real eval set" above: the owner's own set is the real check.
- **The intents path is now the less-used one.** It runs only with `brain: {mode: intents}` or for one turn when the loop's model fails, so a regression there shows late. Its e2e tests pin the mode; `resonant eval router` still measures the labeler. If the fallback is rarely hit in a few weeks, consider retiring the opt-in and keeping only a fixed fallback reply (that would retire the few-shots too).
- **Cold prompt.** About 4.3 s before the first token when Ollama's slot was used for something else (BR-1b's "Prompt size"). Watch the `runner.brain.answer` spans after go-live; the options there still apply.
- **The published architecture artifact** is built from `docs/architecture.html`; republish it after this merges.

## BR-7 carry-overs: media, links, watches, proactive

*Noted 2026-10-18 from BR-7 ([`brain-plan.md`](brain-plan.md#br-7-media-links-watches-proactive-2026-10-18)).*

- **Voice needs a local whisper.** The Claude API documents no audio input and Claude Code's Read reads text, images, PDFs and notebooks only, so without `brain.whisper_path` a voice note is answered "type it instead". Installing whisper.cpp and a model on the Mini is an owner step (test.md M5). The default args are whisper.cpp's `whisper-cli`; another whisper needs `brain.whisper_args`.
- **A voice transcript is chat only**: never a slash command, keyword, approval or worker reply, so `/restart` or `R4 yes` must be typed. Its model-chosen calls are `inferred` (L3 writes like `add_note` still run and report). If voice approvals are ever wanted, they need a confirmation step.
- **The notice hourly cap is brain-only**: `extensions.notify.max_per_hour` is applied to brain notices as their own budget, not shared with extension notifications.
- **Images go straight to Claude.** The local model never sees an image (qwen3 has no vision); a screenshot of an error is always an ask, counted against `brain.ask_claude.daily_cap`. A local vision model could answer simple ones.
- **Attachments of members** are ignored (members can't use `ask_claude`). A grant for media would need a per-principal `ask_claude` grant first.
- **A watch's checks don't see the conversation.** The tool and args are fixed at creation; "watch it" after a long thread depends on the model filling them in. The approval prompt shows exactly what will be checked (code-built), so a wrong watch is caught there.
- **Ask watches cost an ask each check** (24 a day at the hourly minimum, against a cap of 30). Raising `ask_min_interval_minutes` or a separate watch budget may be needed once there are a few.
- **Expired watches end silently** (no text; `list_watches` shows them for a week). A low pulse item for the briefing ("W3 ended unfired") would close the loop.
- **Watch checks run at most every 5 minutes** (`core/watches`) and queue behind owner chat (background priority).
- **Health is not a brain notice.** The `HealthMonitor` already texts on a degrade and on the recovery; it stays outside the daily cap on purpose. The rollup's stalls stay in the hourly pulse.
- **The e2e test wakes the loop by hand** after queueing a watch check; in the daemon the next tick picks it up (at most `loop.tick_s`).

## RC-1 carry-overs: live sessions and Remote Control

*Noted 2026-10-17 from RC-1.*

- **The control request is undocumented.** A Claude Code update can rename it, change the reply or drop it. Then every run says `R<n>: Remote Control unavailable: …` once (or `no answer in 15s`), the `--check` row `runner remote control` FAILs, and runs carry on. Re-check after each CLI update (a scratch run's first text). `runner.remote_control: off` stops asking.
- **Token-only auth likely can't.** Remote Control needs a claude.ai login; a worker on `runner/claude_token` (`CLAUDE_CODE_OAUTH_TOKEN`) or `runner/anthropic_key` probably gets an error reply. That error is the expected `unavailable` text, not a bug.
- **Leftover remote sessions.** Resonant turns Remote Control off before every ending it controls: a turn's end, `/cancel`, `/kill`, a timeout, a daemon shutdown. A process killed without that (a daemon crash, `kill -9`, a power cut, the reaper after a hung `remote_control` off) leaves its remote session unarchived in the Claude app. Archive it there by hand; nothing in Resonant lists them.
- **One remote session per process.** Each `--resume` (a parked question answered, a daemon restart, news after the session ended) opens a new remote session and URL. `runs.remote_url` keeps the latest, but the owner is texted only the first; `/runs R<n>` shows the current one.
- **The live session ends when Resonant's turn does.** After a `result` with nothing queued, Resonant ends the session, so the owner can't keep chatting with an idle worker in the app. A message typed in the app while the worker is working joins that turn. One typed in the gap between a result and the stdin close may run as one more turn; Resonant tolerates it but doesn't start one. Keeping a session idle-alive for the owner would hold a Claude slot, so it isn't done.
- **Questions answered in the app.** If the owner answers a worker's `resonant ask` by typing in the Claude app instead of iMessage, the worker sees the answer, but the question stays open in Resonant. When the turn ends with it open, the run parks until the answer or `ask_timeout_minutes`, then resumes with `ASK_TIMEOUT`. Answer over iMessage (`R<n> …`) to avoid that, or close the question by hand (a future `resonant ask --done`).
- **A message written as the process dies can be lost.** Text written into the live session is marked delivered; if the CLI dies before it reads it (a crash, the end grace's kill), the next session isn't told again, since `resume_prompt` is cleared once the session's result was clean. Rare; widen `_after_exit` to keep live texts after an unclean exit if it shows up.
- **A very short session may say nothing about Remote Control.** If the session ends before the `remote_control` answer arrives, the answer is dropped with it and there is no text, audit row or kv update for that session.
- **The end grace is fixed.** After stdin closes, a CLI that stays quiet 15 s without exiting is killed (`END_GRACE_S`). Output resets the wait, so a turn still running after the close (one the owner started over Remote Control) can finish, bounded by the run's time limit.

## GH-2 carry-overs: the quiet github extension and the progress rollup

*Noted 2026-10-18 from GH-2.*

- **Owner step: re-pin github.** The quiet manifest and the new watch events take effect only after `resonant ext update github` on the Mini (a full re-approval, like every update). Until then the installed copy still texts `ci_failed`, `ci_fixed` and `review_requested`, and the rollup has no PR data.
- **Two stall clocks, one setting.** `pulse.stall_after` decides both, but a PR's activity is the extension's `active_at` (its `updatedAt` or latest check), which doesn't see everything (a label change does move `updatedAt`; a force-push of the same tree doesn't add a check). A run's PR authored by the GitHub App isn't in the owner's search, so a `ready` run's stall is measured from the run's end, not from activity on its PR.
- **A long outage can miss a merge.** The watch looks back two days for closed PRs (`CLOSED_DAYS`), and the rollup reads seven days of events. A PR merged while the Mini was off longer than that never shows as merged; it drops out of the rollup once its last `pr_active` is older than seven days.
- **The watch's event cap.** A run emits at most 100 events (the process runner's cap); `pr_active` come last, after CI, review and closed-PR events, so with a very large number of open PRs some may wait for the next run. Fine at personal scale.
- **No index for the rollup's read.** `rollup.github_prs` scans `events` for `ext:github` rows of the last week each hour; `events` has no `(source, occurred_at)` index and is never pruned. Add one (a migration) if the table grows large.
- **`/pulse` doesn't refresh.** The preview is read-only, so a stall that began since the last pulse run shows only after the next one (the same holds for approval re-mentions).
- **Review requests: once per PR.** A review request is said once per PR (until its told row is pruned a week later), not per push; one re-requested after you reviewed isn't said again within that week.
- **Wording for a waiting PR.** A `ready` run's PR quiet for 4h reads "got dropped", though it waits on the owner's merge; a run queued behind the slot limit reads the same. A "waiting on you to merge" phrasing would be kinder.
- **Quiet ones leave the status.** Once a stall is told, the run or PR is left out of `Still working on` and not named again until it moves. A `Quiet: …` status line could keep it visible.
- **A long mute and old merges.** An untold `merged/` item is kept up to 30 days, so a long mute could say a stale merge later; news age isn't checked at send time.

# Follow-ups

Work we've decided to do eventually but have deferred. Each entry says why it was deferred, what is open, and what "done" looks like. When an item is picked up, ticket it and remove it from here.

## Owner to-dos (human, at the Mini)

Only you can do these: tokens, GitHub settings and the first real runs. Claude never enters a secret, and no secret goes in a command line or a chat. Do them in this order. The full steps and expected output are [`go-live.md`](go-live.md) step 14 (step 12 for the github extension, step 15 for the remote dashboard); status rows are in [`test.md`](test.md). Tick them off here.

- [ ] **github extension** (go-live 12, if not done yet): `resonant secret set github/token` (read-only fine-grained token), then `resonant ext add repo:github` (type `github`).
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

*Deferred 2026-10-13, from the local-model latency fix (Instruct-2507 default).*

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

## Batch approvals (`yes all`)

*Deferred 2026-10-13, from Phase 2. With one gated side effect per task, several pending approvals are rare, and a re-ask with codes covers it.*

**What.** `yes all` approves exactly the approvals listed in one stored batch message (`approval_batches`), never L1 or pay. `open_batch` stays `NotImplementedError` until then.

**Done when.** `yes all` approves a snapshot and nothing that arrived after it.

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
- **Injection budget is three effects.** One injected read in a chat task can now steer up to three side effects that run unasked (L3, or tools with a standing allow). Revisit if extension reads grow; a per-task cap after an extension read is a cheap tightening.
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
- **Proposals are text only.** A `Proposed:` line is flagged (`result.proposed`) but nothing acts on it; the gated send/event tool is later work (D8).
- **No transcript.** Ask sessions keep no transcript (only the `ask.done` audit with status, cost and turns); add one under `transcripts/ask-<id>.jsonl` if debugging needs it.
- **Usage-limit backoff is shared kv, separate from workers.** A worker hitting the limit doesn't stop asks (and vice versa) until each hits it itself.

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

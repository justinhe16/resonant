# Follow-ups

Work we've decided to do eventually but have deferred. Each entry says why it was deferred, what is open, and what "done" looks like. When an item is picked up, ticket it and remove it from here.

## Status relay: dead-man switch and remote dashboard

*Deferred 2026-10-13, during Mac mini go-live (steps 7 and 10 of [`go-live.md`](go-live.md)). This is the plan for both the dead-man switch and remote dashboard access.*

**Problem.** Two gaps, one fix.
- **Dead-man switch.** If the Mini loses power, crashes, or reboots to the FileVault login screen, Resonant can't tell anyone it's down. Something off the Mini has to notice the silence. The daemon already GETs the configured dead-man URL (`health.healthchecks_url`) every `health.ping_every_s` (60s) and POSTs `<url>/fail` with a reason when the loop is stale (see [Health](architecture.md#health)).
- **Remote dashboard.** The owner wants to check status from anywhere, from any browser, after logging in. The API binds to `127.0.0.1:7777` only, and config validation enforces it.

**Proposed design: push-based, read-only.**
- **Push, not pull.** Every minute the daemon writes a status snapshot to Supabase: health checks, `dry_run`, task and slot counts, a last-seen time and version. Nothing connects into the Mini, so it keeps no inbound ports and the loopback-only rule doesn't change.
- **Dashboard.** A small app on Fly.io reads the snapshots from Supabase. It keeps working when the Mini is down and shows "last seen N min ago" with the last known health. A dashboard served from the Mini would go dark exactly then.
- **Dead-man switch.** A scheduled job on Fly alerts the owner when snapshots stop arriving, or when a snapshot reports a failure (today's `/fail` reason). The alert must not depend on the Mini, so it can't go over Resonant's iMessage. Use push, SMS or email instead. One system covers both gaps.
- **Auth.** Use Supabase Auth with sign-ups turned off and exactly one user (the owner), MFA (TOTP) required, and short sessions. The app and the database row-level-security policies allow only the owner's user ID. Don't use IP allowlists: on the road the owner's IP changes constantly.
- **Read-only.** The relay only shows status. Approvals, the kill switch and other actions stay on iMessage, which is already restricted to `principals.yaml`. A command path from the relay back into the Mini would be a new way in, so it needs its own design and review before anyone adds it.

**Open questions**
- **What leaves the Mini.** Health and counts are fine. Message bodies, task contents and audit details should probably stay local. Write down the allowed snapshot fields as a schema.
- **Snapshot writes.** The daemon needs a Supabase credential. Store it in the Keychain like other secrets, and give it insert-only access to its own table.
- **Alert delivery and timing.** Which channel (push, SMS, email)? How long a gap before alerting, to match today's 60s period plus grace?
- **Fly cron vs a long-running checker.** Also: what watches the watcher if Fly itself is down?
- **Retention.** How long to keep snapshots.

**Until then**
- `health.healthchecks_url` stays empty, so the daemon sends no pings. Nobody is alerted when the Mini is down.
- `scripts/bootstrap.sh --check` reports `healthchecks URL set  FAIL (advisory)`, which doesn't fail the check.
- The dashboard is reachable only on the Mini itself. iMessage doesn't need it.

**Done when**
- The owner can log in from any browser and see current status.
- Nobody else can log in.
- The go-live step 7 freeze test (`kill -STOP` the daemon) produces an alert, and the dashboard shows the Mini as stale.
- The following describe how to set up and verify the relay:
  - go-live steps 7 and 10
  - the `--check` health check (required again)
  - the README
  - `architecture.md`, `interfaces.md`
  - the `config.py` and `api.py` comments

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

**What.** `resonant eval router` measures how often the router picks the right intent (question, system, job_control, run_project, unknown). The synthetic set in `evals/` only proves the pipeline.

**To do.**
- Collect 30-50 real texts sent to Resonant and label each with its intent in `~/.resonant/evals/router.jsonl`, in the format of `evals/router.example.jsonl`. The file stays on the Mini and is never committed.
- Run `resonant eval router --set ~/.resonant/evals/router.jsonl` and look at the misroutes.

**When to revisit.** When replies start going to the wrong intent, before Phase 2 adds side-effecting tools (a misroute then costs an approval prompt), or when the model or router prompt changes.

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

**Problem.** A routed reply takes one label call, up to three tool rounds and a final answer. That's usually 1-3 s, but a cold model, a long thread or the Phase 5 Claude runner (minutes) can take much longer, and the sender sees nothing until the answer arrives. iMessage gives no typing indicator over AppleScript.

**Proposed design.**
- **Timer, not a prediction.** When the runner starts a task, arm a timer (`router.ack_after_s`, ~4 s: above the llm path p95). If the reply isn't sent by then, send one short ack ("on it, back in a moment") and then the real reply. Fast-path replies never ack. Predicting slowness from the intent is brittle; a timer is exact and costs nothing when the reply is quick.
- **Escalations ack immediately.** Anything going to the Claude runner gets an ack right away with a rough ETA, and the result comes later as its own message.
- **Exactly once.** Send through the channel with `dedupe_key=ack:<task id>` and record `acked: true` in the checkpoint, so a retried step never acks twice. A cancelled or failed task after an ack should say so ("couldn't finish that: …") instead of going silent.

**Trade-offs.** One extra message per slow reply. An ack that lands a few hundred ms before the answer is noise, so set the threshold from the measured p95, not the p50. Counts toward the outbound rate limit.

**Done when.** The local runner acks only when a reply exceeds `ack_after_s`, never twice for one task, and the e2e bench reports how often it acked.

## Tool selection as the tool catalog grows

*Noted 2026-10-13, from the local-model latency fix.*

**Today.** The labeler's intent already picks the toolset (`INTENT_TOOLS`: question none, system 3, job_control 2), filtered by principal. With at most 3 small tools in the prompt the model isn't overwhelmed: the measured latency was the thinking-only model reasoning before each call, not tool count.

**When to revisit.** Phase 3 extensions add tools. Keep the two-level shape (intent, then that intent's tools): extensions get their own `extension:<name>` intents (already reserved), so each prompt still carries only a handful of tools. Add retrieval (BM25 or embeddings, top-k over tool descriptions) only when one intent's toolset passes roughly 15-20 tools. Retrieval changes the tools block per message, which breaks the byte-identical tools prefix (KV-cache reuse), and tool descriptions become a prompt-injection surface for selection, so extension descriptions need review.

**Cheaper win first.** For intents whose tools take no arguments (`system`: `daemon_status`, `model_status`, `system_status`), the tool round could be skipped: run the toolset directly and let the model only narrate (the final `chat_json`). That saves one model round per system question. Measure with `resonant bench e2e --model real` before and after.

**Two more measured wins (2026-10-13, Instruct-2507, e2e bench).** (1) After the last tool result the model's no-call `chat_tools` turn already writes the answer as plain text (~5-20 tokens), and `_final` then asks again via `chat_json` (median ~0.9 s on `job_control`). Using that text as the reply, through the same number guard and length cap, would save one round. (2) With earlier replies in the thread, the model sometimes answers a repeated question from history without calling a tool, which can be stale. Consider a system-prompt rule to always re-read live state for `job_control` and `system`, or dropping prior replies for those intents.

## Claude sessions for `run_project`: reuse claude-surf, or build "resonant-surf"

*Deferred 2026-10-13, after installing [claude-surf](https://github.com/justinhe16/claude-surf) on the Mini.*

**What it is.** Today the local runner answers `run_project` messages ("write code", "open a PR") with a templated escalation instead of calling Claude (see the 2026-10-08 entry in the [decisions log](architecture.md#decisions-log)). The planned Claude-session runner would take that work. It already has slots (`SlotPool`), `Wait`/resume by `claude_session_id`, and `ctx.save` (see [Scheduling and priority](architecture.md#scheduling-and-priority)). claude-surf's agents and skills now live in `~/.claude/` on the Mini. Its `/robot-surf-prompt` flow is close to what that runner should do: worktree, plan if complex, implement, PR, CI, code review, report. This is also the workflow we've been running by hand.

**Decision to make.** Pick one:
- **Reuse claude-surf as is.** Resonant starts a headless Claude Code session that runs `/robot-surf-prompt`.
- **Edit claude-surf upstream.** Fix what doesn't fit, then reuse it.
- **Build a Resonant-native flow ("resonant-surf").** It would live in this repo or as an extension, built for Resonant's gate, approvals, audit and iMessage reporting.

**Open questions**
- **Why the agents are pinned to Sonnet.** All four claude-surf agents set `model: sonnet`, and its `settings.json` sets `"model": "sonnet"`. Was that for cost, speed, or rate limits? Is it still the right default for planning and code review, where a stronger model may pay off, versus implementation? Should the model be set per agent, or inherited from the caller?
- **Known mismatches with Resonant:**
  - The skills hard-code `MAIN_BRANCH="master"`; Resonant uses `main`.
  - Worktrees go in `~/Projects/<repo>-<branch>`.
  - `/robot-surf-fan` auto-merges by default.
  - `/robot-surf` needs Linear.
  - `global-surf` overwrites `~/.claude/settings.json` with broad pre-approved permissions; we didn't install that part.
- **Safety under Resonant.** A session started from iMessage must:
  - go through the gate and Phase 2 approvals before any side effect (push, PR, merge);
  - never auto-merge without the owner's approval;
  - stay within the principal's grants;
  - report progress and results back over iMessage.

  How much of that fits as a wrapper around claude-surf, and how much needs its own flow?
- **Headless operation.** How does the runner start a session (Claude Code CLI or the Agent SDK)? It needs to stream status, survive `Wait`/resume, and respect the slot cap. Credentials and usage limits matter too.
- **Where the skill lives.** Should it be global `~/.claude/skills` on the Mini, or pinned in this repo so that a Resonant run is reproducible?

**Done when**
- Reuse, edit or build is decided, with the Sonnet question answered.
- A `run_project` text from the owner leads, after approval, to a reviewed PR and an iMessage with its link.
- `architecture.md` and `interfaces.md` describe the runner.

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

## Runner sandboxing (Phase 3/5)

*Deferred 2026-10-13, from the Phase 2 trust model ([architecture.md](architecture.md#trust-model)).*

**Problem.** Same-user processes are outside the gate's threat model: they can run `resonant approvals approve` or `resonant resume`, write `~/.resonant/resonant.db`, and read the Keychain. Phase 3 extension CLIs and Phase 5 Claude sessions run shell commands as that user.

**Done when.** Runners with shell access run sandboxed (e.g. `sandbox-exec` or a separate user) with no access to `~/.resonant`, the `resonant` CLI or `/usr/bin/security`, and a test proves each is denied. For extension processes this is the [Seatbelt sandbox](#seatbelt-sandbox-for-extension-processes) below.

## Ops hardening

*Noted 2026-10-13, during the Phase 2 build.*

- Call `git` by absolute path (like `launchctl` and `uv`) instead of resolving it on launchd's `PATH`.
- Run ops subprocesses with a minimal environment, not the daemon's.
- Scrub more from `/logs`: paths under `$HOME`, tokens that look like keys.
- A cooldown on `restart_ollama`, so a loop of requests can't keep the model down.
- `/update` runs whatever is on `origin/main`: anyone who can push there gets code execution on the Mini. Consider requiring a signed commit or tag, and branch protection.

**Done when.** Each item is done or explicitly accepted in the decisions log.

## Bare `yes` / `no` with nothing pending

*Noted 2026-10-13, during Phase 2. The owner may revisit.*

**Today.** A plain `yes` or `no` with no approval pending gets `Nothing is waiting for approval.` instead of going to the chat path, so a casual "yes" in conversation is answered as an approval reply.

**Decide.** Keep it (clear and safe), or route a bare reply to chat when nothing has been asked recently.

## Clamp future `occurred_at`

*Noted 2026-10-13, during the Phase 2 build.*

**Problem.** The sent-before rule compares the prompt's send time with the reply's `occurred_at` from chat.db. A phone clock running ahead makes a reply look later than it was, so a `yes` sent just before a prompt went out could bind to it. (A clock running behind only refuses instant replies, and the owner re-sends.)

**Done when.** The reader clamps `occurred_at` to at most a little past its own read time, and a test covers a skewed clock both ways.

## Timing-flaky tests

*Noted 2026-10-13, from Phase 2 CI.*

`test_reads_200_rows_under_100ms` and `test_bench_e2e_cli_json` sometimes fail on a loaded CI runner and pass on a rerun. Give them headroom (a relative bound, or a marker that skips the timing assert in CI) so a red build always means a real failure.

## Labeler near-misses

*Noted 2026-10-13, from the Phase 2 router eval on the real model.*

The real model mislabels 2 of the 45 synthetic cases (still above the 90% bar). Look at both misses, then decide between a few-shot fix and an eval case that pins them. Re-run after any prompt or model change, together with [router evals on real texts](#router-evals-on-real-texts-go-live-step-9).

## MCP stdio client for extensions

*Deferred 2026-10-14, from Phase 3 ([plan §8](phase3-plan.md#8-out-of-scope-and-deferred)). No first-party extension needs one; `ext add` refuses an `mcp` block today.*

**Design to carry.**
- Only manifest-listed tools, with manifest descriptions; schema descriptions are stripped and annotations ignored.
- A per-tool pin, `sha256(name, description, input_schema, output_schema, annotations)`, checked at start and on `list_changed`; a change disables the tool and alerts.
- The server's stderr goes to a redacting sink, never a raw log.
- `npx`/`uvx`-style unpinned fetch-runners are refused.
- The server env is the union of its tools' secrets. The process group is killed on stop (MCP children survive a graceful exit).

**Done when.** An extension can expose typed MCP tools that pass the same gate, pins and kill switch as CLI tools.

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

**Update 2026-10-15.** The `runner` profile and `resonant/sandbox/` land in Phase 5a ([plan](phase5a-plan.md#d1-the-sandbox-resonantsandbox)). The deny-default `extension` profile was moved out of that build (it is in nobody's critical path, and on by default it risks breaking the github digest and pulse reporters). What's left here: a second template in `resonant/sandbox/`, opt-in per extension (`sandbox: true` in config), probes for `gh api` and the example extension under it, then default-on once both are proven. It reuses Phase 5a's unconditional mach denies (launchd, window server, HID, accessibility, Apple Events) and port denies on every address.

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
- **Status relay visibility.** Show the count of pending (untold) and open pulse items in the remote status, so a stuck or muted reporter is visible when away from the Mini ([status relay](#status-relay-dead-man-switch-and-remote-dashboard)).

## Approval "expires soon" reminder

*Found 2026-10-14, after the pulse shipped.* The pulse's approval re-mention runs only at the top of the hour, and approvals expire after 30 minutes, so it catches only approvals asked between :30 and :50 past an hour. A reminder scheduled by the approval runner itself (e.g. `[A7] still waiting, expires in 10 min`, once, at half the TTL) would cover every approval. Then drop `core/approval` from the pulse.

## Secrets on disk for `gh` (P5A-7)

*Noted 2026-10-15.* Secrets no longer appear in any child's env or argv (fd 4 pipe). The github extension still has to hand its token to `gh`, which reads only an env var or a config file, so it writes a 0600 `hosts.yml` into a 0700 dir under the extension's data dir for the length of the run. A run ended by SIGKILL (a timeout, after SIGTERM's grace) can leave that dir until a later run sweeps `gh-*` dirs older than an hour. Calling the GitHub API from Python with the token in memory would remove the file; deferred because the extension is built on `gh`'s GraphQL, pagination and error text. Also: a descendant started through a shell (not `subprocess` defaults) could inherit fd 4 before the SDK reads it; first-party extensions don't.

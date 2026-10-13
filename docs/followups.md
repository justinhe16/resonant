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

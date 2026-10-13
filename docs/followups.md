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

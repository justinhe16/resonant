# Follow-ups

Work we've decided to do eventually but have deferred. Each entry says why it was deferred, what is open, and what "done" looks like. When an item is picked up, ticket it and remove it from here.

## Status relay: dead-man switch and remote dashboard

*Deferred 2026-10-13, during Mac mini go-live (steps 7 and 10 of [`go-live.md`](go-live.md)). This replaces healthchecks.io for the dead-man switch and Tailscale for remote access.*

**Problem.** Two gaps, one fix.
- **Dead-man switch.** If the Mini loses power, crashes, or reboots to the FileVault login screen, Resonant can't tell anyone it's down. Something off the Mini has to notice the silence. The daemon already pings `health.healthchecks_url` every `health.ping_every_s` (60s) and sends `/fail` with a reason when the loop is stale (see [Health](architecture.md#health)).
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
- **Fallback.** Tailscale (`tailscale serve --bg 7777`) stays a zero-code way to reach the full local dashboard from the owner's own devices, if we ever want both.

**Until then**
- `health.healthchecks_url` stays empty, so the daemon sends no pings. Nobody is alerted when the Mini is down.
- `scripts/bootstrap.sh --check` reports `healthchecks URL set  FAIL (advisory)`, which doesn't fail the check. Tailscale isn't checked at all.
- The dashboard is reachable only on the Mini itself. iMessage doesn't need it.

**Done when**
- The owner can log in from any browser and see current status.
- Nobody else can log in.
- The go-live step 7 freeze test (`kill -STOP` the daemon) produces an alert, and the dashboard shows the Mini as stale.
- The following describe the relay instead of healthchecks.io and Tailscale:
  - go-live steps 7 and 10
  - the `--check` health check (required again)
  - the README
  - `architecture.md`, `interfaces.md`
  - the `config.py` and `api.py` comments

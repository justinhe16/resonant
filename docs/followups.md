# Follow-ups

Work we've decided to do eventually but have deferred. Each entry says why it was deferred, what is open, and what "done" looks like. When an item is picked up, ticket it and remove it from here.

## Dead-man switch: choose the provider

*Deferred 2026-10-13, during Mac mini go-live (step 7 of [`go-live.md`](go-live.md)).*

**What it is.** If the Mini loses power, crashes, or reboots to the FileVault login screen, Resonant can't tell anyone it's down. The dead-man switch is an outside service that expects a ping from the daemon every `health.ping_every_s` (60s) and alerts the owner when the pings stop. The daemon already pings `health.healthchecks_url` and sends `/fail` with a reason when the loop is stale (see [Health](architecture.md#health)).

**Why deferred.** We want to compare reliable alternatives, or build our own, before depending on healthchecks.io.

**Open questions**
- Hosted (healthchecks.io, Better Stack, Cronitor, UptimeRobot heartbeats, Dead Man's Snitch) vs self-hosted (healthchecks.io is open source) vs our own. A self-hosted monitor must not run on the Mini it watches.
- Alert channels: push, SMS, email. The alert can't depend on the Mini, so it can't go over Resonant's iMessage.
- Does the provider accept the `/fail` ping with a plain-text reason body, or do we need an adapter?
- Free-tier limits, the provider's own uptime, and how quickly it alerts.

**Until then**
- `health.healthchecks_url` stays empty, so the daemon sends no pings.
- `scripts/bootstrap.sh --check` reports `healthchecks URL set  FAIL (advisory)`. It doesn't fail the check; make it required again once a provider is set up.
- Nobody is alerted when the Mini is down.

**Done when** a provider is chosen and configured, the go-live step 7 freeze test (`kill -STOP` the daemon) produces an alert, and `go-live.md` and `--check` describe the chosen provider.

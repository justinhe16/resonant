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

## Remote dashboard access: Tailscale or our own relay

*Deferred 2026-10-13, during Mac mini go-live (step 10 of [`go-live.md`](go-live.md)).*

**What it is.** The API and dashboard bind to `127.0.0.1:7777` only, and config validation enforces it. The current design exposes them to the owner's devices with `tailscale serve` (see README, [`architecture.md`](architecture.md) and [`interfaces.md`](interfaces.md)). Until then, the dashboard is reachable only on the Mini itself. iMessage doesn't need it.

**Why deferred.** We want to weigh an alternative before installing Tailscale: our own small server on Fly.io with Supabase Auth, restricted to one user (the owner). The same Fly app could also be the dead-man monitor above.

**Open questions**
- **Connectivity.** The Mini must keep no inbound ports, so it dials out to the relay: a persistent tunnel such as a WebSocket or SSE stream. Fly then proxies dashboard and API requests over it. How do we keep that tunnel reliable across sleep, network changes and restarts?
- **Exposure.** With Tailscale, the URL doesn't resolve off the tailnet. A Fly relay is a public login page, so security depends on the auth code. Phase 2 adds approvals and the kill switch to this API. What do we need on top of a single allowed user: MFA, short sessions, rate limits, audit of every request?
- **Trust.** Fly and Supabase can see dashboard traffic and sessions. Tailscale is end-to-end between our own devices.
- **Cost and upkeep.** Tailscale's personal plan is free and needs no code. The relay is a service we build, patch and monitor.
- **Hybrid.** One option is Fly for the dead-man switch only, with Tailscale for access.

**Until then**
- No remote access. `tailscale serve` is not set up, and `--check` doesn't check it.
- The API still binds to loopback only. Nothing about the loopback rule changes.

**Done when** the owner can reach `/api/status` from a phone off the home network, nobody else can, and the README, `architecture.md` (including the "no public ports" rule), `interfaces.md`, `config.py` and `api.py` comments, and go-live step 10 describe the chosen approach.

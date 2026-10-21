# Phase 4 plan: dashboard, status relay, and MCP for extensions

Status: **implemented** (2026-10-16). The owner asked for the base dashboard (UI and backend) and MCP for extensions and the brain. Built with robot-surf, a worktree and PR per ticket, in the waves of §5. Hosting (Supabase, Fly, the ntfy app) is the owner's [go-live step 15](go-live.md#15-phase-4-the-remote-dashboard-and-the-dead-man-switch); done 2026-10-20: the relay is on (Fly app `resonant-relay-jhe`).

**As built.**
- **Local** (DB-1, DB-2): the daemon serves the committed `dashboard/dist` at `127.0.0.1:7777/` with `GET /api/dashboard` (the `local` tier). Read-only, no setup, no Node on the Mini.
- **Push** (DB-3): with `relay.enabled`, `resonant/dashboard/pusher.py` POSTs `to_remote(snapshot)` every 60 s to the `relay_ingest(p_token, p_data)` RPC with the public anon key and the ingest token from Keychain `relay/supabase_key`. Health row `relay`. Not a custom JWT role: a random token whose SHA-256 sits in `relay_private.config`, checked by `SECURITY DEFINER` functions; the SQL also refuses any local-only field, rate-limits and keeps 7 days.
- **Remote** (DB-4): `relay/` on one Fly machine, Python standard library only, serves the same build in Supabase mode (password + TOTP, `aal2`, owner-only RLS; TOTP is enrolled once with `supabase/enroll_mfa.mjs`, never in the UI) and runs the dead-man checker through `relay_status(token)` with its own status token: alerts to ntfy or a webhook on stale (5 min), failing and read-error incidents, once each way.
- **Go-live** (DB-5): step 15 is the ordered owner walkthrough, ending with the freeze test. The relay supersedes `health.healthchecks_url` as the dead-man switch (kept as an option); `--check` has one advisory `dead-man switch` row. Leftovers are in [PER-263](https://linear.app/doriapp/issue/PER-263) and [`limits.md`](limits.md#relay-and-dashboard).
- **Remote view** (R3, R5): the remote page shows what the local one does, without text: each local-only `items` list has a `brief` beside it on both tiers (runs, approvals, worker questions, pulse items), plus the brain's state, turns and last tool calls by kind (`brain_activity`), `runner.gaps`, `trust_rule_count` and `repo_count`. Each section names what stays local ("Its commands are shown on the local dashboard only."). Fields in §3, notes in `interfaces.md` (Dashboard snapshot, Briefs).
- **MCP** (MCP-1, MCP-2): stdio and streamable-HTTP servers as extension tools ([`extensions.md`](extensions.md)), with the Linear example in `extensions/linear/` (go-live step 12, optional).

## 1. Decisions

- **D1. One UI, two data sources.** A Vite + React + TypeScript + Tailwind + shadcn/ui app in `dashboard/`. Served by the daemon on `127.0.0.1:7777/` from the committed build in `dashboard/dist`, reading `/api/dashboard`. The same build, deployed on Fly, reads the latest snapshot from Supabase instead. The data source is picked at runtime from a small `config.json` next to `index.html`.
- **D2. Read-only.** The dashboard only shows status. Approvals, `/kill` and every other action stay on iMessage. No POST routes.
- **D3. Two tiers of data.** `local` (loopback only) can show run asks, PR links, approval and question texts, pulse titles, trust rules and repos. `remote` (what leaves the Mini) is health, counts, states, codes and times only; no message bodies, asks, task contents, repo names or URLs. Both are typed in one schema (§3), `remote` a strict subset. A test proves the pusher sends only `remote` fields.
- **D4. Push, never pull.** The daemon writes a `remote` snapshot to Supabase every minute when `relay.enabled`. Nothing connects into the Mini; the loopback-only rule stays.
- **D5. Auth on the remote side.** Supabase Auth, sign-ups off, exactly one user, TOTP MFA required (AAL2), short sessions. RLS lets only the owner's user id read; the daemon writes with its own credential that can only insert into `snapshots`.
- **D6. Dead-man switch on Fly.** A small checker on Fly alerts when snapshots stop (default: none for 5 minutes) or a snapshot reports a failing health row, and once more on recovery. Channel: ntfy (push to the phone, no account needed; the topic is a secret) or a generic webhook URL. It never goes through the Mini.
- **D7. Node is a dev tool only.** The Mini doesn't need Node to run Resonant: `dashboard/dist` is committed, CI rebuilds it and fails if it differs. Node is used to build and test the UI.
- **D8. MCP servers are extension tools.** An extension's manifest may declare an `mcp:` block. Its tools become ordinary extension tools: same gate levels, approvals, secrets, kill switch, audit and pins. The brain sees them like any other extension tool. Transports: stdio (a pinned local binary) and streamable HTTP (a fixed https URL, bearer token from the Keychain). Claude workers already use MCP natively through Claude Code; this is for the brain and extensions.

## 2. Out of scope

Writes from the dashboard; a command path from the relay back to the Mini; inbound webhooks; MCP OAuth flows (use an API key or token as a bearer secret); MCP resources and prompts (tools only); Slack.

## 3. Snapshot schema (shared contract)

`resonant/dashboard/snapshot.py` builds it; `dashboard/src/types.ts` mirrors it; `docs/snapshot.schema.json` is generated from the Pydantic models and checked in CI.

```
Snapshot {
  schema: 1, tier: "local" | "remote", taken_at, version (git sha, 8), started_at, last_tick,
  autonomy: "on"|"off", paused: bool, dry_run: bool,
  health: [{name, state: ok|degraded|failing|unknown, since, reason?}],     # reason: local only
  tasks: {by_state: {state: n}}, slots: {used, max},
  runs: {by_state: {state: n}, items?: [{code, state, repo, ask, branch, pr_url, pr_number,
         started_at, ended_at, waiting_reason, last_note}]},                  # items: local only
         brief: [{code, state, scratch, has_pr, started_at, ended_at, waiting_reason}]},
  approvals: {open: n, items?: [{code, summary, level, asked_at, expires_at}],
              brief: [{level, effect, needs_code, asked_at, expires_at}]},
  questions: {open: n, items?: [{run_code, text, options, asked_at}],
              brief: [{run_code, options: n, asked_at}]},
  pulse: {open: n, items?: [{source, key, title, urgency, state: new|told, updated_at}],
          brief: [{source, urgency, state, updated_at}]},
  extensions: [{name, status, health, jobs: [{id, next_due, last_run_at, last_outcome, fail_streak}]}],
  runner: {set_up: bool, missing?: [str], gaps: n},
  brain_activity: {calls_today, writes_today, state, turns,
                   recent: [{at, tool, decision, status, write}]},
  trust_rules?: [{n, text}], repos?: [{name, mode, base, ci}],                  # local only
  trust_rule_count: n, repo_count: n
}
```

Every `items` list is local only; the `brief` beside it is on both tiers: one row per item with its code, kind, state and times and none of its text, so the remote page lists runs, approvals, questions, pulse items and the brain's last tool calls too. An approval's code is not in its brief (the owner types it to say yes).

Field names may be adjusted by DB-1 with the UI ticket told; `remote` never gains a text field without a plan change. DB-1 changed: `pulse.items[]` gained `source` and `state` is `new|told`; `repos[].protected` became `ci` (protection isn't stored, only a GitHub call knows it); `extensions[].status` is `enabled|disabled|disabled_tampered`; `approvals.items[].code` may be null; `started_at` is never null. The UI's types and fixtures follow, and a Python test validates the fixtures against the models. The exact shape is `docs/snapshot.schema.json`; the field notes are in `interfaces.md` (Dashboard snapshot).

## 4. Tickets

- **DB-1 Snapshot and local API.** The models and builder (§3) over the store, `GET /api/dashboard` (local tier), static serving of `dashboard/dist` at `/` (a clear placeholder page when it's missing), `docs/snapshot.schema.json` plus its CI check. Tests: tier filtering (no text field in `remote`, property-checked over random data), an empty DB, a busy DB, the API on loopback only.
- **DB-2 Dashboard UI.** `dashboard/`: Vite, React, TS, Tailwind, shadcn/ui. Pages: Overview (health, autonomy/paused/dry-run, slots, counts, last tick and "last seen N min ago"), Workers (runs, questions), Approvals, Pulse, Extensions and jobs, Rules and repos (local only). Polls every 10s; stale banner when `taken_at` is old; hides what the tier doesn't carry. Light and dark. *As built since the redesign:* the tabs are merged into one page in two columns: on the left a System status card (overall state is the worst component health; per-component health in a collapsible list), then slots, active runs and active tasks; on the right Waiting on you (approvals and worker questions), Runs and tasks, Claude (worker runner and brain), Extensions, Pulse, and Rules and repos (local only). shadcn/ui style radix-nova (base color neutral, the `radix-ui` package, tw-animate-css), light by default. Data source from `config.json` (`{source: "local"}` or `{source: "supabase", url, anon_key}`); the Supabase path includes login with email + password + TOTP. Tests with Vitest on fixtures matching the schema. CI: a `dashboard` job (`npm ci`, lint, typecheck, test, build, `git diff --exit-code dashboard/dist`).
- **DB-3 Snapshot pusher.** `relay:` config (`enabled: false`, `supabase_url`, `every_s: 60`), secret `relay/supabase_key`, a loop task posting the `remote` snapshot (bounded retries, never blocks the loop, redaction on errors, health row `relay`), `supabase/migrations/` (the `snapshots` table, the insert-only role or function for the daemon, owner-only select via RLS, retention: keep 7 days). `--check` rows. Tests with a fake Supabase.
- **DB-4 Fly relay app.** `relay/`: a tiny server (Node or Python, the ticket picks and says why) that serves the DB-2 build in Supabase mode and runs the dead-man checker (§1 D6), `fly.toml`, `Dockerfile`, secrets via `fly secrets`. Tests for the checker (stale, failing, recovered, no duplicate alerts). Deploy is an owner step. As built: Python standard library only (same gates as the repo, no dependencies); the checker reads through a `StatusSource` interface, by default a token-gated SECURITY DEFINER `public.relay_status(token)` RPC that DB-3 grants (contract and SQL sketch in `relay/README.md`); a persistent read failure is its own alert; exactly one machine, since alert state is in memory.
- **DB-5 Go-live, docs, owner to-dos.** go-live step 15 (Supabase project, the one user + MFA, migrations, key into the Keychain, `relay.enabled`, Fly deploy, ntfy topic, the freeze test), `--check` rows, `healthchecks_url` story updated, architecture/interfaces/README, `docs/followups.md` owner to-dos, `docs/test.md` rows.
- **MCP-1 MCP client and stdio.** Manifest `mcp:` block (per server: `transport: stdio`, `command` pinned at install like `bins`, args, `tools:` list with name, manifest description, level, secrets), the client (`mcp` Python SDK or a minimal JSON-RPC client; the ticket decides), the carried design from followups (manifest-listed tools only, schema descriptions stripped, annotations ignored, per-tool pin `sha256(name, description, input_schema, output_schema, annotations)` checked at start and on `list_changed` with disable and alert, stderr to a redacting sink, `npx`/`uvx`-style fetch-runners refused, env = union of its tools' secrets, process group killed on stop and on `/kill`). Install screen lists MCP tools and pins. A fake MCP server in tests.
- **MCP-2 HTTP transport and a real example.** `transport: http` with a fixed https `url`, `auth: bearer` from a declared secret, timeouts and size caps, the same pins. An example extension wiring the Linear MCP server (`extensions/linear/`, tools read-only by default at L0, writes at L2), router few-shots so "what's on my Linear" routes there. Docs: `extensions.md` MCP section.

## 5. Waves

1. DB-1, DB-2 (against §3 fixtures), MCP-1.
2. DB-3, DB-4, MCP-2.
3. DB-5.

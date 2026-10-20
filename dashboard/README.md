# Resonant dashboard

A read-only status page for Resonant ([`docs/dashboard-plan.md`](../docs/dashboard-plan.md)). Vite, React, TypeScript, Tailwind v4 and shadcn/ui (style `radix-nova`, base color neutral, lucide icons, the `radix-ui` package, tw-animate-css; see `components.json`, components in `src/components/ui`). Light by default with a dark toggle.

One page, two columns (one on narrow screens). Left: the System status card (the overall state is the worst component health; per-component health is in a collapsible list inside it), then Slots, Active runs, Active tasks. Right: Waiting on you (approvals and worker questions), Runs and tasks (every state, finished runs), Claude (worker runner and brain), Extensions, Pulse, Rules and repos (local only). Each card has its own error boundary. Sections live in `src/sections`.

The daemon serves the committed build in `dist/` at `http://127.0.0.1:7777/`, so the Mini never needs Node (D7). Node is only used here, to build and test.

## Data source

`config.json` next to `index.html` picks the source at runtime, so one build serves both:

| `config.json` | Reads | Tier |
|---|---|---|
| `{"source": "local"}` (the committed default; also used when the file is missing) | `GET /api/dashboard` on the daemon | `local`: everything, including asks, texts, rules and repos |
| `{"source": "supabase", "url": "https://<project>.supabase.co", "anon_key": "<anon key>"}` | the newest row of the `snapshots` table (`data` jsonb, `taken_at`) | `remote`: health, counts, states and times only |

Supabase mode sits behind a login: email and password, then a TOTP code. Snapshots are read only once the session is at AAL2; RLS enforces the same on the server. The page never enrols a factor: an account without a verified TOTP factor stays locked, so a leaked password can't add its own authenticator. Enrolment is an owner step at go-live. The anon key is public by design; never put any other key in `config.json`.

The page polls every 10 seconds (and right away when the tab becomes visible), shows a banner when `taken_at` is older than 60s (local) or 3 minutes (remote) or when `last_tick` is more than 2 minutes behind it (a stuck loop), and hides sections the tier doesn't carry (a remote snapshot is also stripped of local-only fields in the UI, as a second line behind D3). Types for the snapshot are in `src/types.ts` and mirror §3 of the plan.

## Develop

```sh
cd dashboard
npm ci
DASHBOARD_FIXTURE=local npm run dev   # serves src/test/fixtures/local.json at /api/dashboard
npm run dev                           # proxies /api to a running daemon on 127.0.0.1:7777
```

`DASHBOARD_FIXTURE` also takes `remote` and `empty`.

## Checks

```sh
npm run lint && npm run typecheck && npm test && npm run build
```

Always commit `dist/` after changing the source: CI rebuilds it and fails when it differs. The build is deterministic (content hashes only) and Tailwind scans only `src/` and `index.html` (never `dist/`), so a rebuild of unchanged source produces no diff whatever is already in `dist/`.

Dependency versions are pinned exactly in `package.json` and `package-lock.json`; Node's version is in `.nvmrc`.

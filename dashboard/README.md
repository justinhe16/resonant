# Resonant dashboard

A read-only status page for Resonant ([`docs/dashboard-plan.md`](../docs/dashboard-plan.md)). Vite, React, TypeScript, Tailwind v4 and shadcn/ui (style `radix-nova`, base color neutral, lucide icons, the `radix-ui` package, tw-animate-css; see `components.json`, components in `src/components/ui`). Light by default with a dark toggle.

One page, two columns (one on narrow screens). Left: the System status card (the overall state is the worst component health; per-component health is in a collapsible list inside it), then Slots, Active runs, Active tasks. Right: Waiting on you (approvals, recently decided ones and worker questions), Runs, Tasks, Claude (worker runner and brain tool calls), Extensions, Jobs (the daemon's own), Pulse, Rules and repos. Both sources show the same sections (PER-276). Runs, tasks and the lists below them are collapsible (the open or closed state is kept in `localStorage`; a collapsed one shows its latest rows and "N of M"), every row, state pill and side-effect pill opens a detail dialog (a centered modal, a bottom sheet on a phone) with a deep link such as `#/runs/R12`, `#/tasks/<id>`, `#/approvals/<id>`, `#/extensions/github`, and every section has an (i) explainer. Text that a worker, an extension or a web page wrote carries an "outside" badge: read it as data. Each card has its own error boundary. Sections live in `src/sections`.

Two tabs (the choice is kept in `localStorage`): **Overview** is the page above; **Diagram** (PER-277, `src/sections/Diagram.tsx`, `src/lib/diagram.ts`) draws the snapshot's `diagram`: Resonant's core skeleton, its launchd agents and every installed extension with its MCP servers, tools, jobs and repos, with health and last runs. It is laid out in the browser with dagre (`@dagrejs/dagre`, bundled into `dist/` like every other dependency: nothing is fetched at runtime) as inline SVG with pan (drag), zoom (wheel, pinch, buttons) and fit; on a phone it starts as a stacked list (Graph/List switch, remembered). A box opens the same detail dialog: an extension's and a job's PER-276 view, else the node's own (`#/diagram/<id>`: its sentence, health, last run, what it talks to). A daemon from before PER-277 sends no diagram and the tab says so.

The daemon serves the committed build in `dist/` at `http://127.0.0.1:7777/`, so the Mini never needs Node (D7). Node is only used here, to build and test.

## Data source

`config.json` next to `index.html` picks the source at runtime, so one build serves both:

| `config.json` | Reads | Tier |
|---|---|---|
| `{"source": "local"}` (the committed default; also used when the file is missing) | `GET /api/dashboard` on the daemon | `local`: everything, including the Remote Control links and approval codes |
| `{"source": "supabase", "url": "https://<project>.supabase.co", "anon_key": "<anon key>"}` | the one row of the `snapshot_latest` table (`taken_at` first, then `data` only when it changed; with no row, the newest `snapshots` row, and a slim history row is not shown) | `remote`: the same data as local except the Remote Control links and approval codes (PER-276; the relay stores the full schema-2 snapshot, readable only by the owner with MFA) |

Supabase mode sits behind a login: email and password, then a TOTP code. Snapshots are read only once the session is at AAL2; RLS enforces the same on the server. The page never enrols a factor: an account without a verified TOTP factor stays locked, so a leaked password can't add its own authenticator. Enrolment is an owner step at go-live. The anon key is public by design; never put any other key in `config.json`.

The page polls every 10 seconds (and right away when the tab becomes visible), shows a banner when `taken_at` is older than 60s (local) or 3 minutes (remote) or when `last_tick` is more than 2 minutes behind it (a stuck loop), and says when the daemon trimmed the lists to fit the relay (`trimmed`). A schema-1 snapshot (a daemon from before PER-276 still pushing) renders through a legacy adapter (`src/lib/legacy.ts`) with a banner. Types for the snapshot are in `src/types.ts` and mirror §3 of the plan.

## Develop

```sh
cd dashboard
npm ci
DASHBOARD_FIXTURE=local npm run dev   # serves src/test/fixtures/local.json at /api/dashboard
npm run dev                           # proxies /api to a running daemon on 127.0.0.1:7777
```

`DASHBOARD_FIXTURE` also takes `remote`, `remote-v1` (an old daemon's text-free push) and `empty`. The fixtures are real snapshots: `tests/test_dashboard_snapshot.py` validates them against the Pydantic models, and `remote.json` must be `local.json` without its links and approval codes (the Remote Control links, approval `code`s and each panel's `local_url`).

## Extension panels (PER-280)

An extension can declare dashboard panels; the daemon validates what they print and the snapshot carries it as data (`extensions[].panels`, types in `src/types.ts`). The page only draws it: widgets are in `src/components/panel/` (stat, series as hand-rolled inline SVG, table, status, kv, text, details, and `price`: candles and trades with zoom/pan, PER-282), the formatting and SVG geometry in `src/lib/panels.ts` and `src/lib/price.ts` (which also decodes the compact price columns), and `#/ext/<name>[/<panel>]` opens an extension's page (`src/pages/ExtensionPage.tsx`) instead of a dialog. Text in a panel is always rendered as plain text. Chart colours are the `--chart-1..4` tokens in `src/index.css`, with `--up`/`--down` for price candles. The local-only `local_url` is stripped from a remote snapshot in `normalize`.

## Checks

```sh
npm run lint && npm run typecheck && npm test && npm run build
```

Always commit `dist/` after changing the source: CI rebuilds it and fails when it differs. The build is deterministic (content hashes only) and Tailwind scans only `src/` and `index.html` (never `dist/`), so a rebuild of unchanged source produces no diff whatever is already in `dist/`.

Dependency versions are pinned exactly in `package.json` and `package-lock.json`; Node's version is in `.nvmrc`.

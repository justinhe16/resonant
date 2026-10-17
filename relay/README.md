# Resonant relay (Fly.io)

The off-Mini half of the remote dashboard ([`docs/dashboard-plan.md`](../docs/dashboard-plan.md), DB-4). One small Fly machine that:

1. **Serves the dashboard** from the committed build in `dashboard/dist`, with a `config.json` generated at boot from the environment (`{"source": "supabase", "url": SUPABASE_URL, "anon_key": SUPABASE_ANON_KEY}`; the committed local-mode `config.json` is never served). The browser logs in with Supabase Auth (password + TOTP, AAL2) and reads snapshots itself; the relay never sees the owner's session.
2. **Runs the dead-man checker** (plan §1 D6): every minute it reads the newest snapshot's `taken_at` and health states and alerts when snapshots stop, when a health row is `failing`, or when it can't read Supabase at all, and once more on each recovery.
3. Answers **`/healthz`** (`{"ok": true}`, or 503 when the checker has stopped checking). It says nothing about Resonant's state: the URL is public.

It never connects to the Mini and nothing on the Mini connects to it (D4).

## Why Python, standard library only

The rest of Resonant is Python, so the relay uses the same ruff, pyright (strict) and pytest gates and CI job. It needs a static file server, one HTTPS call a minute and a POST to ntfy, which the standard library covers (`http.server`, `urllib`), so there are **zero third-party dependencies**: no lockfile to audit, nothing installed at build time, nothing to patch but the pinned base image (`python:3.12-slim`, by digest). Node would have needed the same base image size plus a dependency tree for the same job. The Node toolchain stays a dev tool for building the UI (D7).

## Layout

| Path | What |
|---|---|
| `resonant_relay/config.py` | settings from env, validated at boot (accepts only an `sb_publishable_` key or an anon-role JWT as the anon key, a non-origin `SUPABASE_URL`, non-https alert URLs) |
| `resonant_relay/source.py` | the checker's read path: the `StatusSource` interface, `RpcSource` (default) and `TableSource` |
| `resonant_relay/checker.py` | the incident state machine |
| `resonant_relay/alerts.py` | ntfy and webhook channels |
| `resonant_relay/web.py` | static server, generated `config.json`, security headers, `/healthz` |
| `resonant_relay/http.py` | outbound HTTPS: no redirects, 10s timeout, 256 KiB cap, errors that never include a URL |
| `Dockerfile`, `fly.toml`, `../.dockerignore` | the image and the Fly app. **The build context is the repo root** (the image copies `dashboard/dist`), so `.dockerignore` lives at the root and allow-lists just `relay/resonant_relay` and `dashboard/dist`. |
| `tests/` | run with the repo's `uv run pytest -q` |

## Settings

Public values go in `fly.toml` `[env]`; everything owner-specific is a `fly secrets` value (Supabase URL and anon key are public by design but owner-specific, so they're set as secrets too to keep `fly.toml` generic).

| Variable | Default | |
|---|---|---|
| `SUPABASE_URL` | required | `https://<ref>.supabase.co`, origin only. Also the CSP `connect-src`. |
| `SUPABASE_ANON_KEY` | required | the anon / publishable key. Served to every visitor in `config.json`, so the relay starts only with an `sb_publishable_` key or a JWT whose role is `anon`. |
| `RELAY_READ` | `rpc` | how the checker reads: `rpc` (recommended) or `table`; see below |
| `RELAY_STATUS_TOKEN` | required for `rpc` | the relay's own random token for `relay_status()` |
| `RELAY_READ_KEY` | required for `table` | a key whose role can `SELECT` from `snapshots` |
| `CHECK_EVERY` | `60` | seconds between checks (`300`, `45s`, `5m`, `1h` all work) |
| `STALE_AFTER` | `5m` | no snapshot newer than this is an incident (at least twice `CHECK_EVERY`) |
| `READ_FAIL_AFTER` | `STALE_AFTER` | reads failing this long in a row is an incident |
| `NTFY_URL` | `https://ntfy.sh` | ntfy server |
| `NTFY_TOPIC` | | ntfy topic (1-64 of `A-Za-z0-9_-`). Anyone who knows it can read your alerts and post to it: treat it as a secret, make it long and random. |
| `NTFY_TOKEN` | | optional ntfy access token (`Authorization: Bearer`) for a protected topic |
| `WEBHOOK_URL` | | optional https URL; gets a JSON POST per alert: `{"source": "resonant-relay", "kind": "stale"\|"failing"\|"read_error", "event": "open"\|"recovered", "title", "message", "at"}` |
| `PORT`, `DIST_DIR` | `8080`, `/app/dist` | set by the image |

At least one of `NTFY_TOPIC` or `WEBHOOK_URL` is required; with both, each gets every alert.

## The checker

Three independent incidents, each with one alert when it opens and one when it recovers:

- **stale**: the newest snapshot is older than `STALE_AFTER`, or there are none at all. "Snapshots stopped: none for 7m (last 12:03 UTC)."
- **failing**: the newest snapshot has a health row in state `failing` (`degraded` and `unknown` don't alert; the dashboard shows them). The alert names the rows. Rows joining or leaving while the incident is open don't alert again; it recovers when none is failing.
- **read_error**: Supabase couldn't be read (network, HTTP error, bad token, unexpected shape) for `READ_FAIL_AFTER` in a row. This is reported distinctly ("the relay can't read snapshots ... it can't tell whether Resonant is up"). While reads fail, stale and failing keep their last known value, so a Supabase outage neither fakes a recovery nor piles on a second "stale" alert.

Each channel remembers what it last told you, per incident, so a failed send is retried at the next check and a broken webhook never makes ntfy repeat itself. State lives in memory: after a restart or deploy, an incident that is still open is announced once more. That is why the app runs on **exactly one machine**; two would alert twice.

A watchdog exits the process if the checker hasn't finished a check for `max(3 × CHECK_EVERY + 30s, 180s)`; Fly restarts it (`[[restart]] policy = "always"`), and `/healthz` goes 503 meanwhile.

Logs carry what happened (state changes, alerts sent, failures by status code or error class) and never a secret: error messages are built without URLs, headers or response bodies, and a log filter replaces every configured secret value with `[redacted]` as a second line.

## The checker's read path

The checker needs only the newest snapshot's `taken_at` and each health row's `name` and `state`. It reads through the `StatusSource` interface in `source.py`.

**`RELAY_READ=rpc` (the default, and what the migration grants).** The relay calls `POST /rest/v1/rpc/relay_status` with the public anon key as `apikey` (and as `Authorization: Bearer` only for a legacy JWT key; an `sb_publishable_` key goes in `apikey` alone) and body `{"token": "<RELAY_STATUS_TOKEN>"}`. The function is `public.relay_status(token text) returns jsonb` in [`supabase/migrations/20261016000000_relay.sql`](../supabase/migrations/20261016000000_relay.sql): `SECURITY DEFINER`, executable by `anon`, `volatile` so PostgREST takes POST only. It compares the token's SHA-256 with `relay_private.config.status_token_sha256`; a wrong token (or none configured) **raises** `42501`, which PostgREST answers with 401/403 and the relay reports as a read error ("HTTP 401"), never as "no snapshots". With no snapshots it returns `null`, else `{"taken_at": <timestamptz>, "health": [{"name": text, "state": text}, ...]}` from the newest row by `taken_at`. Nothing with write access or broad read access lives on Fly.

**Fallback: `RELAY_READ=table`.** The relay sends `GET /rest/v1/snapshots?select=taken_at,health:data->health&order=taken_at.desc&limit=1` with `RELAY_READ_KEY` as both `apikey` and bearer. That key's role must be able to `SELECT taken_at, data` on `snapshots`. The migration grants that to nobody but the MFA'd owner, and the only built-in key that could is `service_role`, which bypasses RLS and can write everything: don't use it. `rpc` is the supported path.

## Deploy (owner steps)

The ordered owner walkthrough, with Supabase first and the freeze test last, is [docs/go-live.md step 15](../docs/go-live.md#15-phase-4-the-remote-dashboard-and-the-dead-man-switch). The Fly part, run from the **repo root** (the build context), with `APP` your app name:

```sh
brew install flyctl && fly auth login
fly apps create "$APP"                  # leaves relay/fly.toml untouched; every command takes --app
# Secrets on stdin, so they stay out of shell history and argv (go-live 15 step 5 makes the
# status token and the ntfy topic in shell variables first):
#   SUPABASE_URL, SUPABASE_ANON_KEY, RELAY_STATUS_TOKEN, NTFY_TOPIC [, NTFY_URL, NTFY_TOKEN, WEBHOOK_URL]
{ echo "SUPABASE_URL=..."; ...; } | fly secrets import --app "$APP" --stage
fly secrets list --app "$APP"           # names and digests only

# One machine (no HA pair: two checkers would alert twice)
fly deploy --app "$APP" --config relay/fly.toml --dockerfile relay/Dockerfile --ha=false .
fly scale count 1 --app "$APP" --yes
fly status --app "$APP"
```

The status token's SHA-256 goes into `relay_private.config.status_token_sha256` (go-live 15 step 6). Always run `fly deploy` from the repo root with `.` as the context: from inside `relay/` the image can't reach `dashboard/dist`. `fly logs --app "$APP"` shows each check's failures and every alert sent.

To change a setting later: `fly secrets set --app "$APP" NAME=value` (restarts the machine), or pipe it to `fly secrets import --app "$APP"` for a secret value. A dashboard rebuild ships with the next `fly deploy`.

## Develop and test

```sh
uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest -q   # includes relay/tests

# Run it locally against the committed build (reads fail without a real Supabase; that's fine)
cd relay && SUPABASE_URL=https://example.supabase.co SUPABASE_ANON_KEY=sb_publishable_x \
  RELAY_STATUS_TOKEN=x WEBHOOK_URL=https://127.0.0.1:9 DIST_DIR=../dashboard/dist PORT=8089 \
  uv run python -m resonant_relay
```

CI (`relay` job in `.github/workflows/ci.yml`) builds the image from the repo root and smoke-tests it: generated `config.json`, security headers, `/healthz`, a read failure logged, and no secret value in the logs. It never deploys.

## Security headers

Every response, errors included: `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self' <SUPABASE_URL>; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `Strict-Transport-Security: max-age=63072000; includeSubDomains`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy: same-origin`, a restrictive `Permissions-Policy`, `X-Robots-Tag: noindex`. Only `GET` and `HEAD` (anything else gets 405 and the connection is closed, since its body is never read); request paths are escaped in logs; idle connections time out after 10s, and Fly's proxy fronts the app with the `hard_limit` from `fly.toml`; only files that exist in `dist` (loaded into memory at boot, symlinks skipped), so there's no path to traverse. Hashed `assets/` are cached for a year; `index.html` is revalidated and `config.json` is `no-store`. A test checks that the committed `dashboard/dist/index.html` fits the CSP (no inline scripts, styles or handlers). Fly terminates TLS and `force_https` redirects plain HTTP.

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
| `resonant_relay/config.py` | settings from env, validated at boot (refuses a service-role or `sb_secret_` key as the anon key, a non-origin `SUPABASE_URL`, non-https alert URLs) |
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
| `SUPABASE_ANON_KEY` | required | the anon / publishable key. Served to every visitor in `config.json`; the relay refuses to start with a `service_role` JWT or an `sb_secret_` key. |
| `RELAY_READ` | `rpc` | how the checker reads: `rpc` (recommended) or `table`; see below |
| `RELAY_STATUS_TOKEN` | required for `rpc` | the relay's own random token for `relay_status()` |
| `RELAY_READ_KEY` | required for `table` | a key whose role can `SELECT` from `snapshots` |
| `CHECK_EVERY` | `60` | seconds between checks (`300`, `45s`, `5m`, `1h` all work) |
| `STALE_AFTER` | `5m` | no snapshot newer than this is an incident |
| `READ_FAIL_AFTER` | `STALE_AFTER` | reads failing this long in a row is an incident |
| `NTFY_URL` | `https://ntfy.sh` | ntfy server |
| `NTFY_TOPIC` | | ntfy topic. Anyone who knows it can read your alerts and post to it: treat it as a secret, make it long and random. |
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

A watchdog exits the process if the checker hasn't finished a check for `3 × CHECK_EVERY + 30s`; Fly restarts it (`[[restart]] policy = "always"`), and `/healthz` goes 503 meanwhile.

Logs carry what happened (state changes, alerts sent, failures by status code or error class) and never a secret: error messages are built without URLs, headers or response bodies, and a log filter replaces every configured secret value with `[redacted]` as a second line.

## What DB-3 must grant for the checker's read

The checker needs only the newest snapshot's `taken_at` and each health row's `name` and `state`. It reads through the `StatusSource` interface in `source.py`, so the grant is DB-3's call; either of these works.

**Recommended: `RELAY_READ=rpc`, a token-gated SECURITY DEFINER function.** The relay calls `POST /rest/v1/rpc/relay_status` with the public anon key as `apikey`/`Authorization` and body `{"token": "<RELAY_STATUS_TOKEN>"}`. Nothing with write access or broad read access lives on Fly, and the function exposes only times, names and states. Contract:

- signature `public.relay_status(token text) returns jsonb`, executable by `anon`;
- a wrong token **raises** (e.g. `errcode 42501`) rather than returning null, so it shows up as a read error, not as "no snapshots";
- returns `null` when there are no snapshots, else `{"taken_at": <timestamptz>, "health": [{"name": text, "state": text}, ...]}` from the newest row.

A sketch for DB-3's migration (the owner stores only the token's SHA-256):

```sql
create schema if not exists private;
revoke all on schema private from public, anon, authenticated;
create table if not exists private.relay_token (token_sha256 text primary key);

create or replace function public.relay_status(token text)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  result jsonb;
begin
  if not exists (
    select 1 from private.relay_token t
    where t.token_sha256 = encode(sha256(convert_to(token, 'UTF8')), 'hex')
  ) then
    raise exception 'denied' using errcode = '42501';
  end if;
  select jsonb_build_object(
           'taken_at', s.taken_at,
           'health', coalesce(
             (select jsonb_agg(jsonb_build_object('name', h->>'name', 'state', h->>'state'))
                from jsonb_array_elements(s.data->'health') h),
             '[]'::jsonb))
    into result
    from public.snapshots s
   order by s.taken_at desc
   limit 1;
  return result;
end;
$$;

revoke all on function public.relay_status(text) from public;
grant execute on function public.relay_status(text) to anon;
```

**Fallback: `RELAY_READ=table`.** The relay sends `GET /rest/v1/snapshots?select=taken_at,health:data->health&order=taken_at.desc&limit=1` with `RELAY_READ_KEY` as both `apikey` and bearer. That key's role must be able to `SELECT taken_at, data` on `snapshots`. The only built-in key that can is `service_role`, which bypasses RLS and can write everything, so prefer `rpc`.

## Deploy (owner steps)

Not done by any ticket; DB-5 folds these into go-live step 15. Run from the **repo root** (the build context), with Supabase set up first (DB-3 migrations, the one user with TOTP).

```sh
# 0. Tools and login
brew install flyctl
fly auth login

# 1. Create the app from relay/fly.toml without deploying (pick a name and region)
fly launch --no-deploy --copy-config --config relay/fly.toml --dockerfile relay/Dockerfile --name <app-name>

# 2. The relay's token: Fly gets the token, Supabase only its hash
TOKEN=$(openssl rand -hex 32)
printf %s "$TOKEN" | shasum -a 256          # insert this hex into private.relay_token
#    (Supabase SQL editor: insert into private.relay_token values ('<hex>');)

# 3. Secrets (an ntfy topic is a password: long and random)
fly secrets set --config relay/fly.toml --stage \
  SUPABASE_URL=https://<ref>.supabase.co \
  SUPABASE_ANON_KEY=<anon or publishable key> \
  RELAY_STATUS_TOKEN="$TOKEN" \
  NTFY_TOPIC=resonant-$(openssl rand -hex 16)
#    optional: NTFY_TOKEN=..., NTFY_URL=https://<your ntfy>, WEBHOOK_URL=https://...
unset TOKEN

# 4. Deploy one machine (no HA pair: two checkers would alert twice)
fly deploy --config relay/fly.toml --dockerfile relay/Dockerfile --ha=false .
fly scale count 1 --config relay/fly.toml --yes
fly status --config relay/fly.toml
```

Then: subscribe to the ntfy topic in the ntfy phone app, open `https://<app-name>.fly.dev/`, log in (password + TOTP), and run the freeze test (stop the daemon or `relay.enabled: false`; expect one "Snapshots stopped" alert after `STALE_AFTER` plus up to a minute, and one "Snapshots are back" after it resumes). `fly logs --config relay/fly.toml` shows each check's failures and every alert sent.

To change a setting later: `fly secrets set --config relay/fly.toml NAME=value` (restarts the machine). A dashboard rebuild ships with the next `fly deploy`.

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

Every response, errors included: `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self' <SUPABASE_URL>; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `Strict-Transport-Security: max-age=63072000; includeSubDomains`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy: same-origin`, a restrictive `Permissions-Policy`, `X-Robots-Tag: noindex`. Only `GET` and `HEAD`; only files that exist in `dist` (loaded into memory at boot, symlinks skipped), so there's no path to traverse. Hashed `assets/` are cached for a year; `index.html` is revalidated and `config.json` is `no-store`. A test checks that the committed `dashboard/dist/index.html` fits the CSP (no inline scripts, styles or handlers). Fly terminates TLS and `force_https` redirects plain HTTP.

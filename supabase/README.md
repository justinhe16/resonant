# Supabase for the status relay

The relay (docs/dashboard-plan.md D4–D6) pushes the `remote` tier of the dashboard snapshot from the Mini to a Supabase project, where the owner reads it from the Fly-hosted dashboard. The schema and who can do what are in [`migrations/`](migrations/) and in docs/interfaces.md ("Status relay").

## Security design

- **Reads:** only the one owner, signed in with password and TOTP (`aal2`): an RLS policy on `snapshots` checks `auth.uid()` against `relay_private.config.owner_id` and the JWT's `aal`. Nobody else can read, whether they have MFA or not.
- **Writes:** only `relay_ingest(p_token, p_data)`, a `SECURITY DEFINER` function that checks the token's SHA-256 and that the data is a remote-tier snapshot, then inserts one row. It can't read, update or delete. A token holder can still push fake snapshots, so the token is a bearer secret: Keychain only, and rotate it if in doubt. At most 3 rows a minute, 32 KiB each and 12000 rows in all (under 400 MB), whatever is sent. The anon key alone does nothing. The service-role key and the JWT secret are never used outside Supabase.
- **Status:** the Fly dead-man checker (DB-4) calls `relay_status(token)` (POST only) with its own token. It gets the newest snapshot's `taken_at` and health states, nothing more. A wrong token raises `42501`.

## Owner steps

The full, ordered walkthrough (Supabase, the Mini, Fly, the phone and the freeze test) is [docs/go-live.md step 15](../docs/go-live.md#15-phase-4-the-remote-dashboard-and-the-dead-man-switch). The Supabase part, in short:

1. Create the project. Under Authentication: sign-ups off, TOTP on, and the one user (Auto Confirm). Note the project URL and the publishable key.
2. Apply [`migrations/20261016000000_relay.sql`](migrations/20261016000000_relay.sql) in the SQL editor (or `supabase link` and `supabase db push`).
3. `node supabase/enroll_mfa.mjs --url https://<ref>.supabase.co --anon-key <key> --email <you>` (after `npm ci` in `dashboard/`). It prints the user id.
4. Make the ingest token on the Mini (into the Keychain as `relay/supabase_key`) and the status token (into `fly secrets` as `RELAY_STATUS_TOKEN`), each from a shell variable so neither reaches history or the screen; only their SHA-256 hex is printed.
5. In the SQL editor:
   ```sql
   update relay_private.config set
     owner_id            = '<user id from step 3>',
     ingest_token_sha256 = decode('<sha256 hex of the ingest token>', 'hex'),
     status_token_sha256 = decode('<sha256 hex of the status token>', 'hex');
   ```
6. Claude sets `relay: {enabled: true, supabase_url, anon_key}` in `~/.resonant/config.yaml` (not secret) and restarts the daemon; `relay pushing` turns `PASS` in `scripts/bootstrap.sh --check` after the first push.

To rotate a token, set a new value on the Mini (or in Fly) and update its hash. Until both sides match, pushes fail with 403 and the `relay` health row goes `failing` (or the Fly checker reports a `read_error`).

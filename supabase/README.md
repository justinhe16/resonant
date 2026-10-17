# Supabase for the status relay

The relay (docs/dashboard-plan.md D4–D6) pushes the `remote` tier of the dashboard snapshot from the Mini to a Supabase project, where the owner reads it from the Fly-hosted dashboard. The schema and who can do what are in [`migrations/`](migrations/) and in docs/interfaces.md ("Status relay").

## Security design

- **Reads:** only the one owner, signed in with password and TOTP (`aal2`): an RLS policy on `snapshots` checks `auth.uid()` against `relay_private.config.owner_id` and the JWT's `aal`. Nobody else can read, whether they have MFA or not.
- **Writes:** only `relay_ingest(p_token, p_data)`, a `SECURITY DEFINER` function that checks the token's SHA-256 and that the data is a remote-tier snapshot, then inserts one row. It can't read, update or delete. A token holder can still push fake snapshots, so the token is a bearer secret: Keychain only, and rotate it if in doubt. At most 3 rows a minute, 32 KiB each and 12000 rows in all (under 400 MB), whatever is sent. The anon key alone does nothing. The service-role key and the JWT secret are never used outside Supabase.
- **Status:** the Fly dead-man checker (DB-4) calls `relay_status(token)` (POST only) with its own token. It gets the newest snapshot's `taken_at` and health states, nothing more. A wrong token raises `42501`.

## Owner steps (go-live step 15, DB-5)

1. Create the project. In Authentication, turn sign-ups off and keep TOTP MFA on. Create the one user (email and password).
2. Apply the migration: run `supabase db push`, or paste `migrations/*.sql` into the SQL editor.
3. Enroll TOTP from your own machine with `cd dashboard && npm ci && cd .. && node supabase/enroll_mfa.mjs --url https://<ref>.supabase.co --anon-key <anon key> --email <you>`. It prints the user id.
4. Make two tokens on the Mini and keep only their hashes for Supabase. This is done so neither token lands in shell history or on screen:
   ```sh
   T=$(openssl rand -hex 32)
   printf %s "$T" | shasum -a 256 | cut -d' ' -f1   # the ingest hash, for step 5
   printf %s "$T" | resonant secret set relay/supabase_key
   unset T
   ```
   Make the status token the same way. Print its hash, then give the token to Fly on stdin with `printf 'RELAY_STATUS_TOKEN=%s' "$T" | fly secrets import` (relay/README.md).
5. In the SQL editor:
   ```sql
   update relay_private.config set
     owner_id            = '<user id from step 3>',
     ingest_token_sha256 = decode('<sha256 hex of the ingest token>', 'hex'),
     status_token_sha256 = decode('<sha256 hex of the status token>', 'hex');
   ```
6. In `~/.resonant/config.yaml`, set `relay: {enabled: true, supabase_url: https://<ref>.supabase.co, anon_key: <anon key>}` and restart the daemon. `scripts/bootstrap.sh --check` shows the relay rows, and `relay pushing` turns `PASS` after the first push.

To rotate a token, set a new value on the Mini (or in Fly) and update its hash. Until both sides match, pushes fail with 403 and the `relay` health row goes `failing`.

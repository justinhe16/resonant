# Supabase for the status relay

The relay (docs/dashboard-plan.md D4–D6) pushes the `remote` tier of the dashboard snapshot from the Mini to a Supabase project, where the owner reads it from the Fly-hosted dashboard. Since PER-276 the remote tier is the full snapshot (texts included; only Remote Control links and approval codes stay on the Mini), so what protects it is the read policy below and the secret scrubbing, not a text-free shape. The schema and who can do what are in [`migrations/`](migrations/) and in docs/interfaces.md ("Status relay").

## Security design

- **Reads:** only the one owner, signed in with password and TOTP (`aal2`): RLS policies on `snapshot_latest` (the newest snapshot, whole, up to 256 KiB: what the dashboard reads) and `snapshots` (the history, slim) check `auth.uid()` against `relay_private.config.owner_id` and the JWT's `aal`. Nobody else can read, whether they have MFA or not.
- **Writes:** only `relay_ingest(p_token, p_data)`, a `SECURITY DEFINER` function that checks the token's SHA-256 and that the data is a remote-tier snapshot (schema 1 or 2) with no `remote_url` and no approval code, and refuses any secret-shaped text (`redact.SECRET_SQL_FAMILY`: GitHub, OpenAI/Anthropic, Slack, AWS, Google, GitLab, npm, Linear and Supabase keys, JWTs, bearer tokens, private keys), then upserts the latest row and inserts a slim history row. It can't read, update or delete. A token holder can still push fake snapshots, so the token is a bearer secret: Keychain only, and rotate it if in doubt. At most 3 pushes a minute, 256 KiB for the latest row, 32 KiB for each history row and 12000 history rows in all (under 400 MB), whatever is sent. The anon key alone does nothing. The service-role key and the JWT secret are never used outside Supabase.
- **Panel days** (PER-286): `panel_days` holds a history panel's archived copy per day, read by the same owner-with-TOTP policy. The daemon writes it only through `relay_panel_day(p_token, ...)`, the same ingest token, with the same secret and link refusals, at most 64 KiB a row, 30 rows a minute and 20000 rows in all, pruned past the panel's `keep`.
- **Instances** (PER-304): more than one Resonant daemon can push here (personal `resonant`, and e.g. `cori`). Every row has an `instance` column (default `resonant`), `snapshot_latest` holds one row per instance, and each instance has its own ingest token in `relay_private.ingest_tokens`. The token decides the instance: a push whose `p_instance` or snapshot names another is refused (403), so one daemon can never overwrite the other's rows; each token rotates alone, and the rate limits and row caps are per instance. Both daemons run as one macOS user, so this guards against mistakes, not against a hostile instance.
- **Status:** the Fly dead-man checker (DB-4) calls `relay_status(token)` (POST only) with its own token. It gets each instance's newest `taken_at`, health states and soonest 5 credential expiries (`instances`, PER-315: `secrets` `[{ref, expires_at, days_left}]`, `[]` from an old daemon), plus personal Resonant's at the top level as before, nothing more. A wrong token raises `42501`.

## Owner steps

The full, ordered walkthrough (Supabase, the Mini, Fly, the phone and the freeze test) is [docs/go-live.md step 15](../docs/go-live.md#15-phase-4-the-remote-dashboard-and-the-dead-man-switch). The Supabase part, in short:

1. Create the project. Under Authentication: sign-ups off, TOTP on, and the one user (Auto Confirm). Note the project URL and the publishable key.
2. Apply the files in [`migrations/`](migrations/) in order, in the SQL editor (or `supabase link` and `supabase db push`): [`20261016000000_relay.sql`](migrations/20261016000000_relay.sql), then [`20261021000000_relay_refuse_claude_brain.sql`](migrations/20261021000000_relay_refuse_claude_brain.sql) (PER-263), then [`20261102000000_relay_full_snapshot.sql`](migrations/20261102000000_relay_full_snapshot.sql) (PER-276: `snapshot_latest`, the 256 KiB cap, the slim history, the secret check; apply it before the Fly deploy and the daemon update, see go-live step 15), then [`20261105000000_relay_panel_days.sql`](migrations/20261105000000_relay_panel_days.sql) (PER-286: `panel_days` and `relay_panel_day`, a history panel's archived days; apply it before the daemon update), then [`20261120000000_relay_instances.sql`](migrations/20261120000000_relay_instances.sql) (PER-304: the relay keyed by instance; apply it before the Fly deploy and the daemon update, go-live step 15), then [`20261123000000_relay_secret_expiry.sql`](migrations/20261123000000_relay_secret_expiry.sql) (PER-315: each instance's soonest 5 credential expiries, `{ref, expires_at, days_left}`, in the slim history row and on `relay_status`; apply it before the Fly deploy and the daemon restart, go-live step 15). On the live project, applying a migration is a deploy step that needs the owner's OK.
3. `node supabase/enroll_mfa.mjs --url https://<ref>.supabase.co --anon-key <key> --email <you>` (after `npm ci` in `dashboard/`). It prints the user id.
4. Make the ingest token on the Mini (into the Keychain as `relay/supabase_key`) and the status token (into `fly secrets` as `RELAY_STATUS_TOKEN`), each from a shell variable so neither reaches history or the screen; only their SHA-256 hex is printed.
5. In the SQL editor:
   ```sql
   update relay_private.config set
     owner_id            = '<user id from step 3>',
     status_token_sha256 = decode('<sha256 hex of the status token>', 'hex');
   insert into relay_private.ingest_tokens (instance, token_sha256)
     values ('resonant', decode('<sha256 hex of the ingest token>', 'hex'));
   ```
6. Claude sets `relay: {enabled: true, supabase_url, anon_key}` in `~/.resonant/config.yaml` (not secret) and restarts the daemon; `relay pushing` turns `PASS` in `scripts/bootstrap.sh --check` after the first push.

To rotate a token, set a new value on the Mini (or in Fly) and update its hash (`update relay_private.ingest_tokens set token_sha256 = decode('<new hash>', 'hex') where instance = '<instance>';` for an ingest token). Until both sides match, pushes fail with 403 and the `relay` health row goes `failing` (or the Fly checker reports a `read_error`).

## A second instance (Cori, PER-304)

After the PER-304 migration and the Fly deploy that knows instances (go-live step 15 has the order), the owner gives the instance its own ingest token. On the Mini, one block (the token stays in a shell variable; only its hash is printed):

```sh
T=$(openssl rand -hex 32)
printf %s "$T" | shasum -a 256 | cut -d' ' -f1     # Cori's ingest hash
printf '%s\n' "$T" | resonant --instance cori secret set relay/supabase_key
unset T
```

That stores it in Cori's own Keychain service (`resonant-cori`). Then, in the SQL editor:

```sql
insert into relay_private.ingest_tokens (instance, token_sha256)
  values ('cori', decode('<Cori ingest hash>', 'hex'));
```

and Cori's `config.yaml` gets the same `relay: {enabled: true, supabase_url, anon_key}` as personal Resonant's (the publishable key is shared; the token is not). A personal-only setup never has a `cori` row.

## Forgetting an instance

An instance that is removed keeps its rows: its history goes after 7 days on its own (any instance's push prunes it), but its `snapshot_latest` row and its panel days stay. To refuse its pushes and drop everything it left, owner only, in the SQL editor (`relay_forget_instance`; replace `cori`):

```sql
-- relay_forget_instance: refuse the instance's token and delete all of its rows.
begin;
delete from relay_private.ingest_tokens where instance = 'cori';
delete from public.snapshot_latest      where instance = 'cori';
delete from public.snapshots            where instance = 'cori';
delete from public.panel_days           where instance = 'cori';
commit;
```

Never run it for `resonant` unless you mean to start the personal relay over (it then needs a new token row, step 5).

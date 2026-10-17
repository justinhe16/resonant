-- The Resonant status relay (docs/dashboard-plan.md D3-D6, ticket DB-3).
--
-- The daemon on the Mini pushes the `remote` tier of the dashboard snapshot here every minute.
-- The dashboard (DB-2) reads the latest row as the one owner; the Fly checker (DB-4) watches
-- for gaps. Nothing here can reach the Mini.
--
-- Who can do what (the anon/publishable key is public: the deployed dashboard ships it):
--
--   anon key alone          nothing: no table privileges, both RPCs refuse without a token
--   daemon                  anon key + the ingest token -> public.relay_ingest() only. It
--                           inserts one remote-tier snapshot and can't read, update or delete.
--   Fly checker (DB-4)      anon key + the watch token -> public.relay_watch() only: times
--                           and health states of the latest snapshot, nothing else.
--   the owner (dashboard)   signed in as relay_private.config.owner_id with TOTP (aal2):
--                           SELECT on public.snapshots. Nobody else, MFA or not.
--   service_role, postgres  everything (Supabase's own; never on the Mini or Fly).
--
-- Why a token-gated SECURITY DEFINER function rather than a custom JWT role: minting a
-- role JWT needs the project's JWT signing secret on the owner's laptop, and that secret can
-- mint service_role too; Supabase is also moving projects to asymmetric signing keys, where
-- custom HS256 JWTs stop working. A random 32-byte token whose SHA-256 is all the database
-- keeps can do exactly one thing, never expires by surprise, and is rotated with one UPDATE.
-- The service_role key is never used: it bypasses RLS.
--
-- Owner set-up (docs/go-live.md, DB-5): after applying this file, run once in the SQL editor
--   update relay_private.config set
--     owner_id            = '<auth.users.id of the one user>',
--     ingest_token_sha256 = decode('<sha256 hex of the ingest token>', 'hex'),
--     watch_token_sha256  = decode('<sha256 hex of the watch token>', 'hex');
-- Only the hashes leave the Mini (`printf %s "$TOKEN" | shasum -a 256`). The ingest token goes
-- into the Mini's Keychain as relay/supabase_key, the watch token into `fly secrets`.

-- --- private config --------------------------------------------------------------------------
-- Not in an exposed API schema, so PostgREST can't reach it. Only `postgres` writes it.

create schema if not exists relay_private;
revoke all on schema relay_private from public;

create table relay_private.config (
  singleton           boolean primary key default true check (singleton),
  owner_id            uuid,
  ingest_token_sha256 bytea check (ingest_token_sha256 is null or octet_length(ingest_token_sha256) = 32),
  watch_token_sha256  bytea check (watch_token_sha256 is null or octet_length(watch_token_sha256) = 32),
  -- one token must never work as the other
  check (ingest_token_sha256 is null or watch_token_sha256 is null
         or ingest_token_sha256 <> watch_token_sha256)
);
insert into relay_private.config default values;
revoke all on table relay_private.config from public, anon, authenticated;

-- --- the snapshots --------------------------------------------------------------------------
-- The shape DB-2's Supabase source reads: the latest `data` by `taken_at`.

create table public.snapshots (
  id          bigint generated always as identity primary key,
  taken_at    timestamptz not null,                -- the snapshot's own taken_at (Mini clock)
  received_at timestamptz not null default now(),  -- server clock; what retention and the watch use
  data        jsonb not null,                      -- a remote-tier Snapshot (docs/snapshot.schema.json)
  constraint snapshots_data_object check (jsonb_typeof(data) = 'object'),
  constraint snapshots_data_size check (octet_length(data::text) <= 65536)
);
create index snapshots_taken_at_idx on public.snapshots (taken_at desc);
create index snapshots_received_at_idx on public.snapshots (received_at);

alter table public.snapshots enable row level security;
-- Supabase's default privileges grant everything on new public tables to anon and
-- authenticated: take it all back, then give the owner's role SELECT only (RLS narrows it).
revoke all on table public.snapshots from public, anon, authenticated;
revoke all on sequence public.snapshots_id_seq from public, anon, authenticated;
grant select on table public.snapshots to authenticated;

-- True only for the configured owner, signed in with a second factor (aal2). SECURITY DEFINER
-- so the policy can read relay_private.config without granting anyone access to it.
create function relay_private.is_owner()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    (select c.owner_id from relay_private.config c) = auth.uid()
      and (auth.jwt() ->> 'aal') = 'aal2',
    false)
$$;
revoke all on function relay_private.is_owner() from public, anon, authenticated;
grant usage on schema relay_private to authenticated;
grant execute on function relay_private.is_owner() to authenticated;

create policy snapshots_owner_read on public.snapshots
  for select to authenticated
  using ((select relay_private.is_owner()));
-- No INSERT, UPDATE or DELETE policy: with RLS on, those are refused for every API role.

-- --- the daemon's write path ---------------------------------------------------------------
-- POST /rest/v1/rpc/relay_ingest {"p_token": ..., "p_data": {...}}. Errors use PostgREST's
-- PTxxx codes, so the HTTP status says what went wrong (403 token, 400 shape, 413 size,
-- 429 rate) without any detail the caller needs to parse.

create function public.relay_ingest(p_token text, p_data jsonb)
returns void
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  want bytea;
  t    timestamptz;
begin
  select c.ingest_token_sha256 into want from relay_private.config c;
  if want is null or p_token is null or length(p_token) < 32
     or sha256(convert_to(p_token, 'UTF8')) <> want then
    raise exception 'relay: bad token' using errcode = 'PT403';
  end if;

  if p_data is null or jsonb_typeof(p_data) <> 'object' then
    raise exception 'relay: data must be an object' using errcode = 'PT400';
  end if;
  if octet_length(p_data::text) > 65536 then
    raise exception 'relay: data too large' using errcode = 'PT413';
  end if;
  -- Defense in depth for plan D3: only a remote tier is stored, and none of the local-only
  -- fields (snapshot.py LOCAL_ONLY) may be present, whatever the daemon's code does.
  if p_data ->> 'tier' is distinct from 'remote'
     or p_data -> 'schema' is distinct from '1'::jsonb
     or p_data ?| array['trust_rules', 'repos']
     or coalesce(p_data -> 'runs' ? 'items', false)
     or coalesce(p_data -> 'approvals' ? 'items', false)
     or coalesce(p_data -> 'questions' ? 'items', false)
     or coalesce(p_data -> 'pulse' ? 'items', false)
     or coalesce(p_data -> 'runner' ? 'missing', false)
     or jsonb_path_exists(p_data, 'lax $.health[*].reason') then
    raise exception 'relay: not a remote-tier snapshot' using errcode = 'PT400';
  end if;

  begin
    t := (p_data ->> 'taken_at')::timestamptz;
  exception when others then
    t := null;
  end;
  if t is null or t > now() + interval '5 minutes' then
    raise exception 'relay: bad taken_at' using errcode = 'PT400';
  end if;

  -- A leaked token can't flood the table: the daemon sends one a minute.
  if (select count(*) from public.snapshots s
      where s.received_at > now() - interval '1 minute') >= 10 then
    raise exception 'relay: rate limited' using errcode = 'PT429';
  end if;

  -- Retention: keep 7 days. Done here so no pg_cron job is needed.
  delete from public.snapshots s where s.received_at < now() - interval '7 days';

  insert into public.snapshots (taken_at, data) values (t, p_data);
end
$$;
revoke all on function public.relay_ingest(text, jsonb) from public, anon, authenticated;
grant execute on function public.relay_ingest(text, jsonb) to anon;

-- --- the Fly checker's read path (DB-4) ---------------------------------------------------
-- POST /rest/v1/rpc/relay_watch {"p_token": ...} -> at most one row: the latest snapshot's
-- times and its health rows as [{name, state}]. No counts, no extensions, no other fields.

create function public.relay_watch(p_token text)
returns table (taken_at timestamptz, received_at timestamptz, health jsonb)
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  want bytea;
begin
  select c.watch_token_sha256 into want from relay_private.config c;
  if want is null or p_token is null or length(p_token) < 32
     or sha256(convert_to(p_token, 'UTF8')) <> want then
    raise exception 'relay: bad token' using errcode = 'PT403';
  end if;
  return query
    select s.taken_at,
           s.received_at,
           coalesce(
             (select jsonb_agg(jsonb_build_object('name', h ->> 'name', 'state', h ->> 'state'))
              from jsonb_array_elements(
                case when jsonb_typeof(s.data -> 'health') = 'array'
                     then s.data -> 'health' else '[]'::jsonb end) h),
             '[]'::jsonb)
    from public.snapshots s
    order by s.received_at desc
    limit 1;
end
$$;
revoke all on function public.relay_watch(text) from public, anon, authenticated;
grant execute on function public.relay_watch(text) to anon;

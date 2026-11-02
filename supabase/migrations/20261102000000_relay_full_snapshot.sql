-- PER-276: the relay stores the full dashboard snapshot (schema 2), not a text-free one.
--
-- Plan D3 kept the remote tier to counts, states and identifiers, so a stolen Supabase read
-- could never show what the owner asked for. PER-276 supersedes that: the owner wants the same
-- dashboard from their phone as at the Mini, and the only reader of this table is the one
-- owner, signed in with a password and TOTP (aal2, the policy below). What still never leaves
-- the Mini: a run's or the Claude brain's Remote Control link (whoever holds it can drive the
-- session) and an approval's code (the second factor an L1 yes needs). Secrets are kept out of
-- the text twice: the daemon redacts every free text and scans the dump (redact.py), and this
-- function refuses any text that still looks like a credential (the same families, below).
--
-- What changes:
--
--   public.snapshot_latest   NEW. One row (singleton = true) holding the newest snapshot, up to
--                            256 KiB. The dashboard reads this row, not the history.
--   public.snapshots         the history keeps its 32 KiB cap and 7-day retention. A schema-2
--                            push is stored there SLIM: schema, tier, taken_at, version, the
--                            health rows (name, state, since) and the counts. The Fly checker
--                            (relay_status) reads health from it, so it keeps working. A
--                            schema-1 push (an old daemon) is stored whole, as before.
--   public.relay_ingest()    same signature and grants. Checks, in order: token (PT403), an
--                            object (PT400), at most 256 KiB (PT413; a schema-1 push at most
--                            32 KiB), tier remote and schema 1 or 2 (PT400), no remote_url at
--                            any depth and no non-null approval code (PT400; schema 1: also
--                            the old local-only fields), no secret-shaped text (PT400), a
--                            sane taken_at (PT400), the rate limit of 3 a minute (PT429).
--
-- Deploy order (docs/go-live.md step 15): apply this file first. An old (schema 1) daemon keeps
-- pushing and the old page keeps reading `snapshots`. Then deploy the Fly app (the new page
-- reads `snapshot_latest`, falls back to `snapshots` and shows a schema-1 snapshot as such).
-- Then update the Mini and restart the daemon (schema 2). A new daemon against the old function
-- gets PT400 and shows relay `failing`, which the Fly checker alerts on.
--
-- The secret pattern below is redact.SECRET_SQL_FAMILY, character for character (a test
-- compares them); it is a Postgres ARE, `\y` a word boundary. Tests: tests/test_supabase_sql.py.

-- --- the latest snapshot ---------------------------------------------------------------------

create table public.snapshot_latest (
  singleton   boolean primary key default true check (singleton),
  taken_at    timestamptz not null,                -- the snapshot's own taken_at (Mini clock)
  received_at timestamptz not null default now(),  -- server clock
  data        jsonb not null,                      -- a remote-tier Snapshot (docs/snapshot.schema.json)
  constraint snapshot_latest_data_object check (jsonb_typeof(data) = 'object'),
  constraint snapshot_latest_data_size check (octet_length(data::text) <= 262144)
);

alter table public.snapshot_latest enable row level security;
-- Supabase's default privileges grant everything on new public tables to anon and
-- authenticated: take it all back, then give the owner's role SELECT only (RLS narrows it).
revoke all on table public.snapshot_latest from public, anon, authenticated;
grant select on table public.snapshot_latest to authenticated;

create policy snapshot_latest_owner_read on public.snapshot_latest
  for select to authenticated
  using ((select relay_private.is_owner()));
-- No INSERT, UPDATE or DELETE policy: with RLS on, those are refused for every API role.

-- --- the daemon's write path -----------------------------------------------------------------

create or replace function public.relay_ingest(p_token text, p_data jsonb)
returns void
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  want bytea;
  t    timestamptz;
  v    int;
  slim jsonb;
begin
  select c.ingest_token_sha256 into want from relay_private.config c;
  if want is null or p_token is null or length(p_token) < 32
     or sha256(convert_to(p_token, 'UTF8')) <> want then
    raise exception 'relay: bad token' using errcode = 'PT403';
  end if;

  if p_data is null or jsonb_typeof(p_data) <> 'object' then
    raise exception 'relay: data must be an object' using errcode = 'PT400';
  end if;
  if octet_length(p_data::text) > 262144 then
    raise exception 'relay: data too large' using errcode = 'PT413';
  end if;

  -- Only a remote tier of a schema we know is stored.
  if p_data ->> 'tier' is distinct from 'remote'
     or (p_data -> 'schema' is distinct from '1'::jsonb
         and p_data -> 'schema' is distinct from '2'::jsonb) then
    raise exception 'relay: not a remote-tier snapshot' using errcode = 'PT400';
  end if;
  v := (p_data ->> 'schema')::int;

  -- Schema 1 (an old daemon): the text-free tier, stored whole in the history (32 KiB cap).
  if v = 1 then
    if octet_length(p_data::text) > 32768 then
      raise exception 'relay: data too large' using errcode = 'PT413';
    end if;
    if p_data ?| array['trust_rules', 'repos', 'claude_brain']
       or coalesce(p_data -> 'runs' ? 'items', false)
       or coalesce(p_data -> 'approvals' ? 'items', false)
       or coalesce(p_data -> 'questions' ? 'items', false)
       or coalesce(p_data -> 'pulse' ? 'items', false)
       or coalesce(p_data -> 'runner' ? 'missing', false)
       or jsonb_path_exists(p_data, 'lax $.health[*].reason') then
      raise exception 'relay: not a remote-tier snapshot' using errcode = 'PT400';
    end if;
  end if;

  -- Never at any depth: a Remote Control link (it can drive a session) or an approval's code
  -- (the second factor of an L1 yes). The daemon strips both (snapshot.py LOCAL_ONLY).
  if jsonb_path_exists(p_data, 'lax $.**.remote_url')
     or jsonb_path_exists(p_data, 'lax $.approvals.items[*].code ? (@.type() != "null")')
     or jsonb_path_exists(p_data, 'lax $.approvals.recent[*].code ? (@.type() != "null")') then
    raise exception 'relay: not a remote-tier snapshot' using errcode = 'PT400';
  end if;

  -- Text that looks like a credential (redact.SECRET_SQL_FAMILY). The daemon already replaced
  -- all of it; this is the database's own check.
  if p_data::text ~ '\y(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[abprse]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|lin_api_[A-Za-z0-9]{20,}|sb_secret_[A-Za-z0-9_-]{10,}|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|[Bb]earer\s+[A-Za-z0-9._~+/=-]{12,})|-----BEGIN [A-Z0-9 ]*PRIVATE KEY' then
    raise exception 'relay: secret-shaped text' using errcode = 'PT400';
  end if;

  -- A Remote Control link inside a text (redact.RC_LINK_SQL): it can drive a session.
  if p_data::text ~ 'https://claude\.ai/code/' then
    raise exception 'relay: remote control link' using errcode = 'PT400';
  end if;

  begin
    t := (p_data ->> 'taken_at')::timestamptz;
  exception when others then
    t := null;
  end;
  if t is null or t > now() + interval '5 minutes' then
    raise exception 'relay: bad taken_at' using errcode = 'PT400';
  end if;

  -- A leaked token can't flood the table: the daemon sends one a minute (plus the odd retry
  -- whose first attempt landed after all).
  if (select count(*) from public.snapshots s
      where s.received_at > now() - interval '1 minute') >= 3 then
    raise exception 'relay: rate limited' using errcode = 'PT429';
  end if;

  -- Retention: keep 7 days. Done here so no pg_cron job is needed.
  delete from public.snapshots s where s.received_at < now() - interval '7 days';

  -- The history row: schema 1 whole; schema 2 slim (what relay_status and a "when" need).
  if v = 1 then
    slim := p_data;
  else
    slim := jsonb_build_object(
      'schema', p_data -> 'schema',
      'tier', p_data -> 'tier',
      'taken_at', p_data -> 'taken_at',
      'version', p_data -> 'version',
      'slim', true,
      'health', coalesce(
        (select jsonb_agg(
                  jsonb_build_object('name', e.h -> 'name', 'state', e.h -> 'state',
                                     'since', e.h -> 'since')
                  order by e.ord)
         from jsonb_array_elements(
                case when jsonb_typeof(p_data -> 'health') = 'array'
                     then p_data -> 'health' else '[]'::jsonb end) with ordinality as e(h, ord)
         where e.ord <= 64),
        '[]'::jsonb),
      'counts', jsonb_build_object(
        'runs', p_data #> '{runs,by_state}',
        'tasks', p_data #> '{tasks,by_state}',
        'approvals', p_data #> '{approvals,open}',
        'questions', p_data #> '{questions,open}',
        'pulse', p_data #> '{pulse,open}',
        'untold', p_data #> '{pulse,untold}'));
  end if;
  insert into public.snapshots (taken_at, data) values (t, slim);

  -- The newest snapshot, whole, for the dashboard. A late arrival (an older taken_at, a
  -- retry) never replaces a newer one.
  insert into public.snapshot_latest as l (singleton, taken_at, received_at, data)
  values (true, t, now(), p_data)
  on conflict (singleton) do update
    set taken_at = excluded.taken_at, received_at = excluded.received_at, data = excluded.data
    where l.taken_at <= excluded.taken_at;

  -- And a hard cap: 7 days at one a minute is 10080 rows; never keep more than 12000
  -- (at most 32 KiB each: under 400 MB, whatever a token holder sends).
  delete from public.snapshots s
  where s.id <= (select s2.id from public.snapshots s2 order by s2.id desc offset 12000 limit 1);
end
$$;
-- CREATE OR REPLACE keeps the grants; restated so this file reads on its own.
revoke all on function public.relay_ingest(text, jsonb) from public, anon, authenticated;
grant execute on function public.relay_ingest(text, jsonb) to anon;

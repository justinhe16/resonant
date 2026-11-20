-- PER-304: the relay keyed by instance (Phase 8, Cori).
--
-- More than one Resonant daemon can push to this project now, each its own *instance*
-- (resonant/instance.py: `resonant`, the personal one, and e.g. `cori`). Every row says whose
-- it is, and each instance has its own ingest token: the token names the instance, so one
-- instance's push can never land in another's rows (a bug or a mistaken config in one daemon
-- can't overwrite the other), each token is rotated alone, and the rate limits and row caps
-- apply per instance. Both daemons run as one macOS user and could read each other's Keychain
-- item, so this is about mistakes, not about a hostile instance.
--
-- What changes:
--
--   relay_private.ingest_tokens   NEW. (instance pk, token_sha256 unique): one ingest token
--                                 per instance. The personal token's hash moves here from
--                                 relay_private.config.ingest_token_sha256 as `resonant`, and
--                                 that column is dropped (one place for the hashes). A token
--                                 still can't be both an ingest and the status token (a
--                                 trigger on both tables, 23514, as the old CHECK did).
--   public.snapshot_latest        `singleton` goes; `instance text not null default 'resonant'`
--                                 is the primary key: one newest snapshot per instance.
--   public.snapshots              + `instance` (default `resonant`).
--   public.panel_days             + `instance` (default `resonant`); the key is
--                                 (instance, ext, panel_id, day).
--   public.relay_ingest()         + `p_instance text default null`. The token decides the
--                                 instance; a given p_instance, or a snapshot's own
--                                 `instance.name`, that differs from it is refused (PT403). The
--                                 rate limit (3 a minute) and the 12000-row history cap are per
--                                 instance; the 7-day retention covers every instance (so a
--                                 removed instance's history goes on its own). Every other check
--                                 is as before, in the same order.
--   public.relay_panel_day()      + `p_instance text default null`, the same way. The rate limit
--                                 (30 a minute), the panel's keep and the 20000-row cap are per
--                                 instance.
--   public.relay_status(token)    the same token and errors. Returns null when there are no
--                                 snapshots at all, as before; otherwise
--                                 {"instances": [{"instance", "taken_at", "health"}, ...]} (each
--                                 instance that has an ingest token, by name; taken_at null and
--                                 health [] for one that never pushed) plus the old top-level
--                                 "taken_at" and "health" of `resonant`'s newest snapshot, so the
--                                 Fly checker from before this keeps working unchanged.
--
-- Unchanged: the RLS owner-read policies (instance is just a column), the secret-shape,
-- remote_url, approval-code and Remote Control link refusals, the size caps.
--
-- Backward compatible: an old daemon calls relay_ingest(p_token, p_data) and
-- relay_panel_day(... 6 args), which still resolve (p_instance defaults to null) and land in
-- `resonant`; the old page reads the one `snapshot_latest` row there is until a second
-- instance pushes. Deploy order (docs/go-live.md step 15): 1. apply this file; 2. deploy Fly
-- (PER-305/PER-306: a page that picks the instance); 3. update and restart both daemons; 4. the
-- owner adds Cori's token hash (supabase/README.md) and sets Cori's relay/supabase_key.
--
-- The secret pattern below is redact.SECRET_SQL_FAMILY, character for character (a test
-- compares them); it is a Postgres ARE, `\y` a word boundary. Tests: tests/test_supabase_sql.py.

-- --- one ingest token per instance -----------------------------------------------------------
-- Not in an exposed API schema, so PostgREST can't reach it. Only `postgres` writes it.

create table relay_private.ingest_tokens (
  instance     text primary key,
  token_sha256 bytea not null unique,
  created_at   timestamptz not null default now(),
  constraint ingest_tokens_instance check (instance ~ '^[a-z][a-z0-9-]{0,31}$'),
  constraint ingest_tokens_sha256 check (octet_length(token_sha256) = 32)
);
revoke all on table relay_private.ingest_tokens from public, anon, authenticated;

insert into relay_private.ingest_tokens (instance, token_sha256)
select 'resonant', c.ingest_token_sha256
  from relay_private.config c
 where c.ingest_token_sha256 is not null;

-- Its CHECK against status_token_sha256 goes with it; the trigger below replaces that.
alter table relay_private.config drop column ingest_token_sha256;

-- One token must never work as the other: no ingest hash may equal the status hash.
create function relay_private.tokens_distinct()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if exists (select 1
               from relay_private.config c
               join relay_private.ingest_tokens t on t.token_sha256 = c.status_token_sha256) then
    raise exception 'relay: one token cannot be both an ingest and the status token'
      using errcode = '23514';
  end if;
  return null;
end
$$;
revoke all on function relay_private.tokens_distinct() from public, anon, authenticated;

create trigger ingest_tokens_distinct
  after insert or update on relay_private.ingest_tokens
  for each statement execute function relay_private.tokens_distinct();
create trigger config_tokens_distinct
  after insert or update on relay_private.config
  for each statement execute function relay_private.tokens_distinct();

-- The instance an ingest token belongs to (PT403 for an unknown token, or one that is not
-- `p_instance` when that is given). Only the SECURITY DEFINER RPCs below call it.
create function relay_private.ingest_instance(p_token text, p_instance text)
returns text
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  found text;
begin
  if p_token is null or length(p_token) < 32 then
    raise exception 'relay: bad token' using errcode = 'PT403';
  end if;
  select t.instance into found
    from relay_private.ingest_tokens t
   where t.token_sha256 = sha256(convert_to(p_token, 'UTF8'));
  if found is null then
    raise exception 'relay: bad token' using errcode = 'PT403';
  end if;
  if p_instance is not null and p_instance is distinct from found then
    raise exception 'relay: token is not this instance' using errcode = 'PT403';
  end if;
  return found;
end
$$;
revoke all on function relay_private.ingest_instance(text, text) from public, anon, authenticated;

-- --- the tables, keyed by instance -----------------------------------------------------------

alter table public.snapshot_latest
  add column instance text not null default 'resonant'
  constraint snapshot_latest_instance check (instance ~ '^[a-z][a-z0-9-]{0,31}$');
alter table public.snapshot_latest drop column singleton;  -- its primary key and CHECK too
alter table public.snapshot_latest add constraint snapshot_latest_pkey primary key (instance);

alter table public.snapshots
  add column instance text not null default 'resonant'
  constraint snapshots_instance check (instance ~ '^[a-z][a-z0-9-]{0,31}$');
create index snapshots_instance_taken_at_idx on public.snapshots (instance, taken_at desc);
create index snapshots_instance_received_at_idx on public.snapshots (instance, received_at);

alter table public.panel_days
  add column instance text not null default 'resonant'
  constraint panel_days_instance check (instance ~ '^[a-z][a-z0-9-]{0,31}$');
alter table public.panel_days drop constraint panel_days_pkey;
alter table public.panel_days add constraint panel_days_pkey
  primary key (instance, ext, panel_id, day);

-- --- the daemon's write path -----------------------------------------------------------------
-- A new trailing argument with a default: drop the old signature so a call without
-- p_instance (an old daemon) is not ambiguous between the two.

drop function public.relay_ingest(text, jsonb);

create function public.relay_ingest(p_token text, p_data jsonb, p_instance text default null)
returns void
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  inst text;
  t    timestamptz;
  v    int;
  slim jsonb;
begin
  inst := relay_private.ingest_instance(p_token, p_instance);

  if p_data is null or jsonb_typeof(p_data) <> 'object' then
    raise exception 'relay: data must be an object' using errcode = 'PT400';
  end if;
  -- A snapshot that names its instance (PER-297) must be the token's.
  if jsonb_typeof(p_data -> 'instance') = 'object'
     and (p_data #>> '{instance,name}') is distinct from inst then
    raise exception 'relay: token is not this instance' using errcode = 'PT403';
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

  -- A leaked token can't flood the table: each daemon sends one a minute (plus the odd retry
  -- whose first attempt landed after all). Per instance: one can't starve the other.
  if (select count(*) from public.snapshots s
      where s.instance = inst and s.received_at > now() - interval '1 minute') >= 3 then
    raise exception 'relay: rate limited' using errcode = 'PT429';
  end if;

  -- Retention: keep 7 days, for every instance (a removed one's rows go too). Done here so no
  -- pg_cron job is needed.
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
  insert into public.snapshots (instance, taken_at, data) values (inst, t, slim);

  -- The instance's newest snapshot, whole, for the dashboard. A late arrival (an older
  -- taken_at, a retry) never replaces a newer one.
  insert into public.snapshot_latest as l (instance, taken_at, received_at, data)
  values (inst, t, now(), p_data)
  on conflict (instance) do update
    set taken_at = excluded.taken_at, received_at = excluded.received_at, data = excluded.data
    where l.taken_at <= excluded.taken_at;

  -- And a hard cap per instance: 7 days at one a minute is 10080 rows; never keep more than
  -- 12000 (at most 32 KiB each: under 400 MB an instance, whatever a token holder sends).
  delete from public.snapshots s
  where s.instance = inst
    and s.id <= (select s2.id from public.snapshots s2 where s2.instance = inst
                 order by s2.id desc offset 12000 limit 1);
end
$$;
revoke all on function public.relay_ingest(text, jsonb, text) from public, anon, authenticated;
grant execute on function public.relay_ingest(text, jsonb, text) to anon;

-- --- panel days ------------------------------------------------------------------------------

drop function public.relay_panel_day(text, text, text, text, jsonb, int);

create function public.relay_panel_day(
  p_token text, p_ext text, p_panel text, p_day text, p_data jsonb, p_keep_days int,
  p_instance text default null
)
returns void
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  inst text;
  d    date;
begin
  inst := relay_private.ingest_instance(p_token, p_instance);

  if p_ext is null or p_ext !~ '^[a-z][a-z0-9-]{0,47}$'
     or p_panel is null or p_panel !~ '^[a-z0-9][a-z0-9_-]{0,31}$' then
    raise exception 'relay: bad panel' using errcode = 'PT400';
  end if;
  if p_day is null or p_day !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' then
    raise exception 'relay: bad day' using errcode = 'PT400';
  end if;
  begin
    d := p_day::date;
  exception when others then
    d := null;
  end;
  -- A day has ended before it is archived; the Mini's zone may be a day ahead of UTC.
  if d is null or d > current_date + 1 or d < current_date - 731 then
    raise exception 'relay: bad day' using errcode = 'PT400';
  end if;
  if p_keep_days is null or p_keep_days < 7 or p_keep_days > 730 then
    raise exception 'relay: bad keep' using errcode = 'PT400';
  end if;

  -- PostgREST may pass a JSON null as SQL NULL or as the jsonb value 'null': both delete.
  if p_data is not null and jsonb_typeof(p_data) = 'null' then
    p_data := null;
  end if;

  if p_data is not null then
    if jsonb_typeof(p_data) <> 'object' then
      raise exception 'relay: data must be an object' using errcode = 'PT400';
    end if;
    if octet_length(p_data::text) > 65536 then
      raise exception 'relay: data too large' using errcode = 'PT413';
    end if;
    -- A panel's own page on the Mini and a Remote Control link never leave it.
    if jsonb_path_exists(p_data, 'lax $.**.local_url')
       or jsonb_path_exists(p_data, 'lax $.**.remote_url') then
      raise exception 'relay: local-only field' using errcode = 'PT400';
    end if;
    if p_data::text ~ '\y(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[abprse]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}|lin_api_[A-Za-z0-9]{20,}|sb_secret_[A-Za-z0-9_-]{10,}|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|[Bb]earer\s+[A-Za-z0-9._~+/=-]{12,})|-----BEGIN [A-Z0-9 ]*PRIVATE KEY' then
      raise exception 'relay: secret-shaped text' using errcode = 'PT400';
    end if;
    if p_data::text ~ 'https://claude\.ai/code/' then
      raise exception 'relay: remote control link' using errcode = 'PT400';
    end if;
  end if;

  -- A leaked token can't churn the table: each daemon sends a few days a minute at most.
  if (select count(*) from public.panel_days p
      where p.instance = inst and p.received_at > now() - interval '1 minute') >= 30 then
    raise exception 'relay: rate limited' using errcode = 'PT429';
  end if;

  if p_data is null then
    delete from public.panel_days p
    where p.instance = inst and p.ext = p_ext and p.panel_id = p_panel and p.day = d;
  else
    insert into public.panel_days as p (instance, ext, panel_id, day, data)
    values (inst, p_ext, p_panel, d, p_data)
    on conflict (instance, ext, panel_id, day) do update
      set data = excluded.data, received_at = now();
  end if;

  -- Retention: this panel's keep, and a hard cap of 20000 rows an instance (at most 64 KiB
  -- each).
  delete from public.panel_days p
  where p.instance = inst and p.ext = p_ext and p.panel_id = p_panel
    and p.day < current_date - p_keep_days;
  delete from public.panel_days p
  where p.instance = inst
    and (p.day, p.ext, p.panel_id) in (
      select p2.day, p2.ext, p2.panel_id from public.panel_days p2
      where p2.instance = inst
      order by p2.day desc, p2.ext, p2.panel_id offset 20000);
end
$$;
revoke all on function public.relay_panel_day(text, text, text, text, jsonb, int, text)
  from public, anon, authenticated;
grant execute on function public.relay_panel_day(text, text, text, text, jsonb, int, text)
  to anon;

-- --- the Fly checker's read path -------------------------------------------------------------

-- A history row's health as the checker sees it: [{name, state}], nothing else.
create function relay_private.status_health(p_data jsonb)
returns jsonb
language sql
immutable
set search_path = ''
as $$
  select coalesce(
    (select jsonb_agg(jsonb_build_object('name', h ->> 'name', 'state', h ->> 'state'))
     from jsonb_array_elements(
       case when jsonb_typeof(p_data -> 'health') = 'array'
            then p_data -> 'health' else '[]'::jsonb end) h),
    '[]'::jsonb)
$$;
revoke all on function relay_private.status_health(jsonb) from public, anon, authenticated;

create or replace function public.relay_status(token text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  want      bytea;
  instances jsonb;
  personal  jsonb;
begin
  select c.status_token_sha256 into want from relay_private.config c;
  if want is null or token is null or length(token) < 32
     or sha256(convert_to(token, 'UTF8')) <> want then
    raise exception 'relay: bad token' using errcode = '42501';
  end if;

  -- No snapshots at all: null, as before (the checker's "nothing pushed yet").
  if not exists (select 1 from public.snapshots) then
    return null;
  end if;

  -- Every instance with an ingest token, by name, with its newest history row (by taken_at,
  -- as the dashboard reads it); one that never pushed has taken_at null and no health.
  select coalesce(jsonb_agg(jsonb_build_object(
             'instance', k.instance,
             'taken_at', n.taken_at,
             'health', coalesce(relay_private.status_health(n.data), '[]'::jsonb))
           order by k.instance), '[]'::jsonb)
    into instances
    from relay_private.ingest_tokens k
    left join lateral (
      select s.taken_at, s.data from public.snapshots s
       where s.instance = k.instance
       order by s.taken_at desc
       limit 1) n on true;

  -- The old top-level fields: `resonant`'s newest snapshot, for a checker from before.
  select jsonb_build_object('taken_at', s.taken_at,
                            'health', relay_private.status_health(s.data))
    into personal
    from public.snapshots s
   where s.instance = 'resonant'
   order by s.taken_at desc
   limit 1;

  return jsonb_build_object('instances', instances) || coalesce(personal, '{}'::jsonb);
end
$$;
revoke all on function public.relay_status(text) from public, anon, authenticated;
grant execute on function public.relay_status(text) to anon;

-- PER-315: credential expiries on relay_status and the Fly page.
--
-- PER-313 gave each daemon a record of when its Keychain secrets expire. The snapshot now
-- carries them as `secrets`: [{ref, expires_at, days_left, source, probe}], soonest first, at
-- most 16 (ref names and dates only; a secret's value is never read for it). The page needs
-- nothing here: `snapshot_latest.data` is the whole pushed snapshot. The Fly checker does:
-- `relay_status` reads the slim history row, which `relay_ingest` builds field by field.
--
-- What changes:
--
-- * relay_private.status_secrets(jsonb): the first 5 entries of a snapshot's (or a slim
--   row's) `secrets`, each {ref, expires_at, days_left} only; [] when there are none.
-- * relay_ingest: the same function (every check kept: tier and schema, local-only fields,
--   secret-shaped text, Remote Control links, size, taken_at, rate limit, retention, caps);
--   the slim history row gains `secrets`.
-- * relay_status: each `instances[]` entry gains `secrets` ([] for an old daemon's row, or an
--   instance that never pushed). The top-level `taken_at` and `health` are exactly as before,
--   so an old checker reads the same answer.
--
-- Nothing else changes: no table, no grant beyond the ones each function had. Apply it before
-- `fly deploy` and before restarting the daemons (docs/go-live.md step 15); an old daemon's
-- push (no `secrets`) keeps working either way.

-- --- the slim projection ---------------------------------------------------------------------

create or replace function relay_private.status_secrets(p_data jsonb)
returns jsonb
language sql
immutable
set search_path = ''
as $$
  select coalesce(
    (select jsonb_agg(jsonb_build_object('ref', e.s -> 'ref',
                                         'expires_at', e.s -> 'expires_at',
                                         'days_left', e.s -> 'days_left')
                      order by e.ord)
     from jsonb_array_elements(
       case when jsonb_typeof(p_data -> 'secrets') = 'array'
            then p_data -> 'secrets' else '[]'::jsonb end) with ordinality as e(s, ord)
     where e.ord <= 5 and jsonb_typeof(e.s) = 'object'),
    '[]'::jsonb)
$$;
revoke all on function relay_private.status_secrets(jsonb) from public, anon, authenticated;

-- --- the daemon's write path -----------------------------------------------------------------
-- The PER-304 function, unchanged but for the slim row's `secrets`.

create or replace function public.relay_ingest(p_token text, p_data jsonb, p_instance text default null)
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
        'untold', p_data #> '{pulse,untold}'),
      -- PER-315: the soonest 5 credential expiries, names and dates only.
      'secrets', relay_private.status_secrets(p_data));
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

-- --- the Fly checker's read path -------------------------------------------------------------

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
  -- as the dashboard reads it); one that never pushed has taken_at null, no health and no
  -- secrets. PER-315: each entry gains `secrets` ([] from an old daemon's row).
  select coalesce(jsonb_agg(jsonb_build_object(
             'instance', k.instance,
             'taken_at', n.taken_at,
             'health', coalesce(relay_private.status_health(n.data), '[]'::jsonb),
             'secrets', relay_private.status_secrets(n.data))
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

  -- No `resonant` row (a project where only another instance pushes): the old fields are
  -- still there, empty, so an old checker reads "nothing from personal", not a bad shape.
  return jsonb_build_object('instances', instances)
         || coalesce(personal, jsonb_build_object('taken_at', null, 'health', '[]'::jsonb));
end
$$;
revoke all on function public.relay_status(text) from public, anon, authenticated;
grant execute on function public.relay_status(text) to anon;

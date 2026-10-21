-- PER-263: relay_ingest also refuses the Claude brain's details (and any Remote Control link).
--
-- The daemon's to_remote() already clears every local-only field (snapshot.py LOCAL_ONLY);
-- this is the database's own check of that (plan D3, defense in depth). The first migration
-- listed every local-only field there was, but `claude_brain` (BR-11: the brain session's state,
-- its Remote Control link and its last tool calls with their commands) came later and was
-- never added. Now it is refused, and so is a `remote_url` key at any depth, whichever object
-- carries it (run items, the brain, anything added later).
--
-- Same function, same signature, same grants; only the local-only check changes. The new
-- remote fields of PER-263 (pulse.untold, runs.detected, extensions[].muted_until) are counts,
-- times and enums, so nothing here refuses them, before or after this migration.
--
-- Deploy: the owner applies this to the live project (docs/go-live.md step 15); it is safe to
-- apply while the daemon is pushing (CREATE OR REPLACE is atomic).

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
begin
  select c.ingest_token_sha256 into want from relay_private.config c;
  if want is null or p_token is null or length(p_token) < 32
     or sha256(convert_to(p_token, 'UTF8')) <> want then
    raise exception 'relay: bad token' using errcode = 'PT403';
  end if;

  if p_data is null or jsonb_typeof(p_data) <> 'object' then
    raise exception 'relay: data must be an object' using errcode = 'PT400';
  end if;
  if octet_length(p_data::text) > 32768 then
    raise exception 'relay: data too large' using errcode = 'PT413';
  end if;
  -- Defense in depth for plan D3: only a remote tier is stored, and none of the local-only
  -- fields (snapshot.py LOCAL_ONLY) may be present, whatever the daemon's code does.
  if p_data ->> 'tier' is distinct from 'remote'
     or p_data -> 'schema' is distinct from '1'::jsonb
     or p_data ?| array['trust_rules', 'repos', 'claude_brain']
     or coalesce(p_data -> 'runs' ? 'items', false)
     or coalesce(p_data -> 'approvals' ? 'items', false)
     or coalesce(p_data -> 'questions' ? 'items', false)
     or coalesce(p_data -> 'pulse' ? 'items', false)
     or coalesce(p_data -> 'runner' ? 'missing', false)
     or jsonb_path_exists(p_data, 'lax $.health[*].reason')
     or jsonb_path_exists(p_data, 'lax $.**.remote_url') then
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

  -- A leaked token can't flood the table: the daemon sends one a minute (plus the odd retry
  -- whose first attempt landed after all).
  if (select count(*) from public.snapshots s
      where s.received_at > now() - interval '1 minute') >= 3 then
    raise exception 'relay: rate limited' using errcode = 'PT429';
  end if;

  -- Retention: keep 7 days. Done here so no pg_cron job is needed.
  delete from public.snapshots s where s.received_at < now() - interval '7 days';

  insert into public.snapshots (taken_at, data) values (t, p_data);

  -- And a hard cap: 7 days at one a minute is 10080 rows; never keep more than 12000
  -- (at most 32 KiB each: under 400 MB, whatever a token holder sends).
  delete from public.snapshots s
  where s.id <= (select s2.id from public.snapshots s2 order by s2.id desc offset 12000 limit 1);
end
$$;
-- CREATE OR REPLACE keeps the grants; restated so this file reads on its own.
revoke all on function public.relay_ingest(text, jsonb) from public, anon, authenticated;
grant execute on function public.relay_ingest(text, jsonb) to anon;

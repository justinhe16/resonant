-- PER-286: panel history on the remote dashboard.
--
-- An extension panel with `history: {every: daily, keep: 365d}` keeps one archived copy per
-- calendar day on the Mini (`ext_panel_days`). The snapshot carries only the list of days
-- (`extensions[].panels[].history.days`); the copy of a day lives here, one row per
-- (ext, panel_id, day), and the dashboard fetches the row when the owner picks that day.
--
--   public.panel_days         NEW. data is the panel's normalised views ({views, health,
--                             truncated}; never a local_url), at most 64 KiB. Read: the owner
--                             with TOTP only, exactly like snapshot_latest. No writes.
--   public.relay_panel_day()  NEW. The daemon's one write path, with the ingest token of
--                             relay_ingest. Checks, in order: token (PT403), ext and panel ids
--                             (PT400), a sane day (PT400), keep 7..730 (PT400), the data: null
--                             deletes the day (a rebuild found no data), else an object of at
--                             most 64 KiB (PT413) with no local_url or remote_url at any depth,
--                             no secret-shaped text and no Remote Control link (PT400), the rate
--                             limit of 30 a minute (PT429). Then it upserts the row, deletes
--                             that panel's rows older than keep, and caps the table at 20000
--                             rows (the oldest days go first).
--
-- Deploy order: apply this file before the daemon update. A new daemon against a project
-- without it gets HTTP 404 on the RPC and keeps the days queued (it retries with backoff), so
-- the order is not critical. The Fly app serves the dashboard build: deploy it to get the day
-- picker on the remote page (no Fly code changes).
--
-- The secret pattern below is redact.SECRET_SQL_FAMILY, character for character (a test
-- compares them). Tests: tests/test_supabase_sql.py.

create table public.panel_days (
  ext         text not null,
  panel_id    text not null,
  day         date not null,                       -- a calendar day in the panel's tz
  data        jsonb not null,                      -- {views, health, truncated}
  created_at  timestamptz not null default now(),  -- first received
  received_at timestamptz not null default now(),  -- last received (the rate limit uses it)
  primary key (ext, panel_id, day),
  constraint panel_days_ext check (ext ~ '^[a-z][a-z0-9-]{0,47}$'),
  constraint panel_days_panel check (panel_id ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),
  constraint panel_days_data_object check (jsonb_typeof(data) = 'object'),
  constraint panel_days_data_size check (octet_length(data::text) <= 65536)
);
create index panel_days_received_at_idx on public.panel_days (received_at);

alter table public.panel_days enable row level security;
-- Supabase's default privileges grant everything on new public tables to anon and
-- authenticated: take it all back, then give the owner's role SELECT only (RLS narrows it).
revoke all on table public.panel_days from public, anon, authenticated;
grant select on table public.panel_days to authenticated;

create policy panel_days_owner_read on public.panel_days
  for select to authenticated
  using ((select relay_private.is_owner()));
-- No INSERT, UPDATE or DELETE policy: with RLS on, those are refused for every API role.

create function public.relay_panel_day(
  p_token text, p_ext text, p_panel text, p_day text, p_data jsonb, p_keep_days int
)
returns void
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
  want bytea;
  d    date;
begin
  select c.ingest_token_sha256 into want from relay_private.config c;
  if want is null or p_token is null or length(p_token) < 32
     or sha256(convert_to(p_token, 'UTF8')) <> want then
    raise exception 'relay: bad token' using errcode = 'PT403';
  end if;

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

  -- A leaked token can't churn the table: the daemon sends a few days a minute at most.
  if (select count(*) from public.panel_days p
      where p.received_at > now() - interval '1 minute') >= 30 then
    raise exception 'relay: rate limited' using errcode = 'PT429';
  end if;

  if p_data is null then
    delete from public.panel_days p
    where p.ext = p_ext and p.panel_id = p_panel and p.day = d;
  else
    insert into public.panel_days as p (ext, panel_id, day, data)
    values (p_ext, p_panel, d, p_data)
    on conflict (ext, panel_id, day) do update
      set data = excluded.data, received_at = now();
  end if;

  -- Retention: this panel's keep, and a hard cap of 20000 rows (at most 64 KiB each).
  delete from public.panel_days p
  where p.ext = p_ext and p.panel_id = p_panel and p.day < current_date - p_keep_days;
  delete from public.panel_days p
  where (p.day, p.ext, p.panel_id) in (
    select p2.day, p2.ext, p2.panel_id from public.panel_days p2
    order by p2.day desc, p2.ext, p2.panel_id offset 20000);
end
$$;
revoke all on function public.relay_panel_day(text, text, text, text, jsonb, int)
  from public, anon, authenticated;
grant execute on function public.relay_panel_day(text, text, text, text, jsonb, int) to anon;

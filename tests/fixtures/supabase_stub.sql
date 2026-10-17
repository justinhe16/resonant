-- The parts of a Supabase database the relay migration relies on, for tests on a plain
-- Postgres (tests/test_supabase_sql.py). Not shipped anywhere.
--
-- - the API roles (anon, authenticated, service_role), as PostgREST switches to them;
-- - auth.uid() and auth.jwt(), reading the request's JWT claims the way Supabase's do;
-- - Supabase's default privileges, which grant everything on new public objects to the API
--   roles, so the test proves the migration takes them back.

create role anon nologin noinherit;
create role authenticated nologin noinherit;
create role service_role nologin noinherit bypassrls;

grant usage on schema public to anon, authenticated, service_role;

alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;

create schema auth;
grant usage on schema auth to anon, authenticated, service_role;

create function auth.jwt() returns jsonb
language sql stable
as $$
  select coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb
$$;

create function auth.uid() returns uuid
language sql stable
as $$
  select nullif(auth.jwt() ->> 'sub', '')::uuid
$$;

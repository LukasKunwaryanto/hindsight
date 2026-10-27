-- Automatic captures: the sync-poll Edge Function runs every 5 minutes.

-- 1. The startpage tree remembers what each entry links to, so the poller can
--    tell which markets an edited entry (or replaced image) appears in.
alter table public.tree_entries
  add column links     text[] not null default '{}',   -- linked entry IDs
  add column asset_ids text[] not null default '{}';   -- linked image/file IDs
create index tree_entries_asset_ids on public.tree_entries using gin (asset_ids);

-- 2. A market can have one capture running and one more waiting (changes made
--    while a capture runs are picked up by the next one).
drop index public.capture_requests_one_open;
create unique index capture_requests_one_waiting
  on public.capture_requests (market, device) where status = 'waiting';

-- 3. A random value the scheduler sends along, so only the scheduler can run the
--    poller. It is created inside the database and never appears in code.
create schema if not exists private;
revoke all on schema private from public, anon, authenticated;
create table private.settings (key text primary key, value text not null);
insert into private.settings (key, value)
values ('cron_secret', replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''));

create function public.hindsight_cron_ok(secret text) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (select 1 from private.settings where key = 'cron_secret' and value = secret)
$$;
revoke execute on function public.hindsight_cron_ok(text) from public, anon, authenticated;
grant execute on function public.hindsight_cron_ok(text) to service_role;

-- 4. Schedule: call the poller every 5 minutes.
create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

select cron.schedule(
  'hindsight-sync-poll',
  '*/5 * * * *',
  $$
  select net.http_post(
    url := 'https://arztscplszpzldtpiawc.supabase.co/functions/v1/sync-poll',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-hindsight-cron', (select value from private.settings where key = 'cron_secret')),
    body := '{}'::jsonb,
    timeout_milliseconds := 120000
  );
  $$
);

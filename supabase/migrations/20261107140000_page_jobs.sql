-- Scheduled jobs for inner pages and checks (started through sync-poll; times are UTC).
create or replace function private.hindsight_job(job text) returns void
language sql security definer as $$
  select net.http_post(
    url := 'https://arztscplszpzldtpiawc.supabase.co/functions/v1/sync-poll',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-hindsight-cron', (select value from private.settings where key = 'cron_secret')),
    body := jsonb_build_object('job', job),
    timeout_milliseconds := 30000
  );
$$;

-- menu pages list follows each market's menu: Mondays 06:20 Stockholm
select cron.schedule('hindsight-menu-pages', '20 5 * * 1', $$ select private.hindsight_job('menu-pages') $$);
-- safety pass over all inner pages (changes made outside Contentful): 1st of the month, 03:10 Stockholm
select cron.schedule('hindsight-pages-pass', '10 2 1 * *', $$ select private.hindsight_job('capture-pages-pass') $$);
-- are stored pages' images visible: Mondays 07:50 Stockholm
select cron.schedule('hindsight-visibility-check', '50 6 * * 1', $$ select private.hindsight_job('visibility-check') $$);
-- Svenssons inner pages until its Contentful key exists: daily 07:40 Stockholm
select cron.schedule('hindsight-capture-svenssons-pages', '40 6 * * *', $$ select private.hindsight_job('capture-svenssons-pages') $$);

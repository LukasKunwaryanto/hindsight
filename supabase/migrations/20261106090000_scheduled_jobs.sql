-- Daily GitHub jobs are started by the database scheduler instead of GitHub's own schedule,
-- which ran hours late or skipped runs. The sync-poll function starts the named job
-- (only jobs it lists can be started). pg_cron times are UTC: 05:40 = 06:40 Stockholm in winter.

select cron.schedule(
  'hindsight-sample-metrics',
  '40 5 * * *',
  $$
  select net.http_post(
    url := 'https://arztscplszpzldtpiawc.supabase.co/functions/v1/sync-poll',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-hindsight-cron', (select value from private.settings where key = 'cron_secret')),
    body := '{"job": "sample-metrics"}'::jsonb,
    timeout_milliseconds := 30000
  );
  $$
);

-- Svenssons until its Contentful delivery key exists: 07:10 and 13:10 Stockholm.
select cron.schedule(
  'hindsight-capture-svenssons',
  '10 6,12 * * *',
  $$
  select net.http_post(
    url := 'https://arztscplszpzldtpiawc.supabase.co/functions/v1/sync-poll',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-hindsight-cron', (select value from private.settings where key = 'cron_secret')),
    body := '{"job": "capture-svenssons"}'::jsonb,
    timeout_milliseconds := 30000
  );
  $$
);

-- Once, now: the Svenssons capture GitHub's schedule missed this morning (also tests the path).
select net.http_post(
  url := 'https://arztscplszpzldtpiawc.supabase.co/functions/v1/sync-poll',
  headers := jsonb_build_object(
    'Content-Type', 'application/json',
    'x-hindsight-cron', (select value from private.settings where key = 'cron_secret')),
  body := '{"job": "capture-svenssons"}'::jsonb,
  timeout_milliseconds := 30000
);

-- Daily check that linked product images are still on the site's CDN.
alter table public.product_images add column if not exists checked_at timestamptz;
alter table public.product_images add column if not exists missing_since timestamptz;
create index if not exists product_images_checked on public.product_images (checked_at nulls first);

-- 08:20 Stockholm
select cron.schedule('hindsight-image-check', '20 7 * * *', $$ select private.hindsight_job('image-check') $$);

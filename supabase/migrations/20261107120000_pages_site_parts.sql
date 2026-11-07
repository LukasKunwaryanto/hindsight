-- Inner pages (menu, category, brand and inspiration pages) next to the startpages, captured on content
-- changes only. Existing rows are startpages: page '/'.

-- Which inner pages are captured. entry_id = the page's Contentful entry, so a publish in its content
-- tree starts a capture (sync-poll); pages without one are only caught by the monthly pass.
create table public.pages (
  id        bigint generated always as identity primary key,
  market    text not null references public.markets(locale),
  path      text not null check (path like '/%' and path <> '/'),
  kind      text not null check (kind in ('menu', 'category', 'brand', 'editorial')),
  title     text,
  entry_id  text,
  enabled   boolean not null default true,
  unique (market, path)
);

alter table public.snapshots add column page text not null default '/';
alter table public.snapshots add column changes jsonb;   -- inner pages: changes kept in the row, no snapshot_changes rows
create index snapshots_market_page_device on public.snapshots (market, page, device, captured_at desc);

alter table public.capture_requests add column page text not null default '/';
drop index if exists public.capture_requests_one_waiting;
create unique index capture_requests_one_waiting
  on public.capture_requests (market, page, device) where status = 'waiting';

-- Site bars (USP bar, header, campaign bar, footer) as their own timeline per market and device: a new row
-- only when they visibly change. Inner pages are shown with the bars that were live at the time.
create table public.site_parts (
  id           bigint generated always as identity primary key,
  market       text not null references public.markets(locale),
  device       text not null check (device in ('desktop', 'mobile')),
  captured_at  timestamptz not null default now(),
  parts        jsonb not null              -- [{label, ref}] in page order
);
create index site_parts_market_device on public.site_parts (market, device, captured_at desc);

-- Product images for listing grids: one row per product (the same file in every market and brand).
-- src = the site's image URL; asset = a stored copy (key pages only).
create table public.product_images (
  sku         text primary key,
  src         text not null default '',
  asset       text,
  first_seen  timestamptz not null default now()
);

-- The content tree also knows which inner pages an entry is on ("market@/path/").
alter table public.tree_entries add column if not exists pages text[] not null default '{}';

-- Views: periods per page; capture health stays about startpages.
create or replace view public.snapshot_periods
with (security_invoker = true) as
select
  s.id as snapshot_id, s.market, s.device, s.captured_at as live_from,
  lead(s.captured_at) over (partition by s.market, s.page, s.device order by s.captured_at) as live_to,
  s.page
from public.snapshots s;

create or replace view public.capture_health
with (security_invoker = true) as
select
  m.locale as market,
  d.device,
  (select max(s.captured_at) from public.snapshots s
     where s.market = m.locale and s.device = d.device and s.page = '/') as last_capture_at,
  (select count(*) from public.capture_requests r
     where r.market = m.locale and r.device = d.device and r.page = '/' and r.status = 'failed'
       and r.created_at > now() - interval '7 days') as failures_7d,
  (select r.status from public.capture_requests r
     where r.market = m.locale and r.device = d.device and r.page = '/'
     order by r.created_at desc limit 1) as latest_request_status
from public.markets m
cross join (values ('desktop'), ('mobile')) as d(device)
where m.enabled;

alter table public.pages          enable row level security;
alter table public.site_parts     enable row level security;
alter table public.product_images enable row level security;
grant all on public.pages, public.site_parts, public.product_images to service_role;
grant select on public.pages, public.site_parts, public.product_images to anon, authenticated;
create policy "read" on public.pages          for select to anon, authenticated using (true);
create policy "read" on public.site_parts     for select to anon, authenticated using (true);
create policy "read" on public.product_images for select to anon, authenticated using (true);

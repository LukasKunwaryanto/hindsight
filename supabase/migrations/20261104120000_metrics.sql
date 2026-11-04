-- Hindsight – promotion metrics (Phase 2)
--
-- The same tables are filled by two jobs:
--   * tools/mock_metrics.py  – sample numbers for the POC demo (source = 'mock')
--   * the daily BigQuery sync – real numbers, once access is in place (source = 'bigquery')
-- The app only reads these tables, so swapping mock for real data changes nothing in the app.
-- Grain is one day (Stockholm date). Days less than 3 days old are provisional.

-- One row per promotion (Contentful entry ID) per market per day.
-- Conversion is counted by session: sessions that clicked the promotion and then
-- viewed a product, added to cart or bought (purchases carry no promotion info).
create table public.promotion_metrics_daily (
  market                 text not null references public.markets(locale),
  day                    date not null,
  entry_id               text not null,          -- = promotion_id in GA = snapshot_modules.entry_id
  promotion_name         text,                   -- as sent to GA (snake_case), for display
  views                  int not null default 0, -- view_promotion (block was on screen)
  clicks                 int not null default 0, -- select_promotion
  quickbacks             int not null default 0, -- clicks where the visitor left the landing page right away
  click_sessions         int not null default 0, -- sessions with at least one click on the promotion
  product_view_sessions  int not null default 0, -- ... that then viewed a product
  cart_sessions          int not null default 0, -- ... that then added to cart
  purchase_sessions      int not null default 0, -- ... that then bought
  revenue_sek            numeric(14, 2) not null default 0,
  source                 text not null default 'mock' check (source in ('mock', 'bigquery')),
  synced_at              timestamptz not null default now(),
  primary key (market, day, entry_id)
);
create index on public.promotion_metrics_daily (entry_id, day);

-- One row per market per day for the startpage as a whole.
-- Scroll depth: reached[k] = visitors whose deepest scroll (pixels_scrolled_max)
-- reached at least k * bucket_px pixels, so reached[1] is everyone with a scroll event.
create table public.page_metrics_daily (
  market                text not null references public.markets(locale),
  day                   date not null,
  page_views            int not null default 0,
  users                 int not null default 0,
  sessions              int not null default 0,
  promo_view_page_views int not null default 0,  -- page views with at least one promotion on screen
  scroll_samples        int not null default 0,  -- scroll events (sent when the visitor leaves the page)
  avg_pixels_total      int,                     -- average page height reported by the scroll event
  bucket_px             int not null default 250,
  reached               int[] not null default '{}',
  source                text not null default 'mock' check (source in ('mock', 'bigquery')),
  synced_at             timestamptz not null default now(),
  primary key (market, day)
);

-- Splits for the Filter menu, per market per day: each segment's share of sessions
-- and how its CTR and conversion compare with the market average (1.0 = average).
-- The app applies these to promotion numbers when a filter is on.
create table public.segment_metrics_daily (
  market       text not null references public.markets(locale),
  day          date not null,
  dimension    text not null check (dimension in ('device', 'visitor', 'channel')),
  value        text not null,       -- device: desktop | mobile
                                     -- visitor: new | returning | member
                                     -- channel: organic_search | paid_search | paid_social | organic_social
                                     --          | email | affiliate | referral | direct
  sessions     int not null default 0,
  ctr_index    numeric(6, 3) not null default 1,
  conv_index   numeric(6, 3) not null default 1,
  source       text not null default 'mock' check (source in ('mock', 'bigquery')),
  synced_at    timestamptz not null default now(),
  primary key (market, day, dimension, value)
);

-- A/B tests (Excite Optimize): numbers per test version for promotions in a test.
create table public.ab_test_metrics_daily (
  market             text not null references public.markets(locale),
  day                date not null,
  entry_id           text not null,
  test_name          text not null,
  version            text not null,   -- 'A' (control) | 'B' ...
  share              numeric(4, 3) not null default 0.5,  -- share of visitors in this version
  views              int not null default 0,
  clicks             int not null default 0,
  click_sessions     int not null default 0,
  purchase_sessions  int not null default 0,
  source             text not null default 'mock' check (source in ('mock', 'bigquery')),
  synced_at          timestamptz not null default now(),
  primary key (market, day, entry_id, version)
);

-- When each snapshot was live: from its capture until the next capture of the
-- same market and device. A snapshot's numbers = metrics for days in this period.
create view public.snapshot_periods
with (security_invoker = true) as
select
  s.id as snapshot_id, s.market, s.device, s.captured_at as live_from,
  lead(s.captured_at) over (partition by s.market, s.device order by s.captured_at) as live_to
from public.snapshots s;

-- Access: the service role writes, the app reads.
alter table public.promotion_metrics_daily enable row level security;
alter table public.page_metrics_daily      enable row level security;
alter table public.segment_metrics_daily   enable row level security;
alter table public.ab_test_metrics_daily   enable row level security;

grant all on public.promotion_metrics_daily, public.page_metrics_daily,
             public.segment_metrics_daily, public.ab_test_metrics_daily to service_role;
grant select on public.promotion_metrics_daily, public.page_metrics_daily,
                public.segment_metrics_daily, public.ab_test_metrics_daily,
                public.snapshot_periods to anon, authenticated;

create policy "read" on public.promotion_metrics_daily for select to anon, authenticated using (true);
create policy "read" on public.page_metrics_daily      for select to anon, authenticated using (true);
create policy "read" on public.segment_metrics_daily   for select to anon, authenticated using (true);
create policy "read" on public.ab_test_metrics_daily   for select to anon, authenticated using (true);

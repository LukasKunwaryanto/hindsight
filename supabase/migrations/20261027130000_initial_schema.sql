-- Hindsight – initial schema (Phase 1: NN startpage, 16 markets, desktop + mobile)
--
-- Writes happen only with the service role (GitHub workflows, Edge Functions).
-- The app uses the public anon key and can only READ the tables granted below.

-- ---------------------------------------------------------------------------
-- Markets to capture
-- ---------------------------------------------------------------------------
create table public.markets (
  locale      text primary key,              -- e.g. 'sv-SE'
  url         text not null,                 -- startpage URL
  enabled     boolean not null default true,
  sort_order  int not null default 0
);

insert into public.markets (locale, url, sort_order) values
  ('sv-SE', 'https://www.nordicnest.se/', 1),
  ('en-US', 'https://www.nordicnest.com/', 2),
  ('de-DE', 'https://www.nordicnest.de/', 3),
  ('nb-NO', 'https://www.nordicnest.no/', 4),
  ('da-DK', 'https://www.nordicnest.dk/', 5),
  ('fi-FI', 'https://www.nordicnest.fi/', 6),
  ('nl-NL', 'https://www.nordicnest.nl/', 7),
  ('fr-FR', 'https://www.nordicnest.fr/', 8),
  ('es-ES', 'https://www.nordicnest.es/', 9),
  ('it-IT', 'https://www.nordicnest.it/', 10),
  ('pl-PL', 'https://www.nordicnest.pl/', 11),
  ('pt-PT', 'https://www.nordicnest.pt/', 12),
  ('el-GR', 'https://www.nordicnest.gr/', 13),
  ('ja-JP', 'https://www.nordicnest.jp/', 14),
  ('ko-KR', 'https://www.nordicnest.kr/', 15),
  ('zh-CN', 'https://www.nordicnest.cn/', 16);

-- ---------------------------------------------------------------------------
-- Change detection (Contentful Sync API)
-- ---------------------------------------------------------------------------
-- One row per Contentful space: the bookmark for "changes since last poll".
create table public.sync_state (
  space_id        text primary key,           -- 'p7vzp7ftmsr1' = Nordic Nest
  next_sync_url   text,                       -- token URL returned by the Sync API
  last_polled_at  timestamptz,
  last_error      text,
  updated_at      timestamptz not null default now()
);

-- The entries the startpage uses (~800), with their latest published fields.
-- Needed to see what changed in an entry and in which markets.
create table public.tree_entries (
  entry_id      text primary key,
  content_type  text not null,
  name          text,
  is_page       boolean not null default false,  -- linked page: never triggers a capture
  markets       text[] not null default '{}',    -- locales whose startpage shows it
  fields        jsonb not null,                  -- all locales, as delivered
  revision      int,
  published_at  timestamptz,
  updated_at    timestamptz not null default now()
);

-- Every relevant change the poller detects (log; also drives the queue below).
create table public.content_changes (
  id            bigint generated always as identity primary key,
  entry_id      text not null,
  content_type  text,
  name          text,
  change_type   text not null check (change_type in ('publish', 'unpublish', 'delete')),
  markets       text[] not null default '{}',
  published_at  timestamptz,
  detected_at   timestamptz not null default now()
);
create index on public.content_changes (detected_at desc);
create index on public.content_changes (entry_id);

-- One open request per market + device. Changes keep pushing last_change_at;
-- a capture starts after 15 quiet minutes (or 60 after the first change).
create table public.capture_requests (
  id               bigint generated always as identity primary key,
  market           text not null references public.markets(locale),
  device           text not null check (device in ('desktop', 'mobile')),
  status           text not null default 'waiting'
                   check (status in ('waiting', 'started', 'done', 'failed')),
  reason           text not null default 'contentful',  -- or 'manual', 'initial'
  first_change_at  timestamptz not null default now(),
  last_change_at   timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz,
  github_run_id    bigint,
  error            text,
  created_at       timestamptz not null default now()
);
create unique index capture_requests_one_open
  on public.capture_requests (market, device)
  where status in ('waiting', 'started');

-- ---------------------------------------------------------------------------
-- Snapshot storage (content-addressed: identical pieces are stored once)
-- ---------------------------------------------------------------------------
-- Text pieces: page skeletons, block modules, CSS rules, style lists, cards, badges.
create table public.blobs (
  hash        text primary key,                 -- first 16 hex chars of SHA-256
  kind        text not null,                    -- shell | module | rule | ruleset | static_css | card | badge | price | part
  content     bytea not null,                   -- gzip-compressed UTF-8
  size_bytes  int not null,                     -- compressed size
  created_at  timestamptz not null default now()
);

-- Binary files kept in Storage (bucket 'assets'): fonts and banner images.
create table public.assets (
  hash          text primary key,
  mime          text not null,
  storage_path  text not null,                  -- path inside the 'assets' bucket
  width         int,
  height        int,
  size_bytes    int not null,
  source_url    text,                           -- where it was captured from
  created_at    timestamptz not null default now()
);

-- One row per capture.
create table public.snapshots (
  id                    uuid primary key default gen_random_uuid(),
  market                text not null references public.markets(locale),
  device                text not null check (device in ('desktop', 'mobile')),
  url                   text not null,
  captured_at           timestamptz not null default now(),
  manifest              jsonb not null,         -- shell, static_css, ruleset, modules[], cards[]
  page_height           int,
  stats                 jsonb,                  -- blocks, images, css rules, new bytes, seconds
  capture_request_id    bigint references public.capture_requests(id),
  previous_snapshot_id  uuid references public.snapshots(id)
);
create index on public.snapshots (market, device, captured_at desc);

-- The blocks in each snapshot, in page order. Lets the app list "what was where"
-- and compare two snapshots without unpacking manifests.
create table public.snapshot_modules (
  snapshot_id  uuid not null references public.snapshots(id) on delete cascade,
  position     int not null,
  entry_id     text,                            -- Contentful entry ID of the block, if any
  label        text not null,                   -- 'header', 'footer', 'block:<entryId>', ...
  blob_hash    text not null references public.blobs(hash),
  promotion_names text[] not null default '{}', -- promotion names found in the block
  primary key (snapshot_id, position)
);
create index on public.snapshot_modules (entry_id);

-- What changed between a snapshot and the one before it (rules first, AI later).
create table public.snapshot_changes (
  id                    bigint generated always as identity primary key,
  snapshot_id           uuid not null references public.snapshots(id) on delete cascade,
  previous_snapshot_id  uuid references public.snapshots(id),
  entry_id              text,
  position              int,
  kind                  text not null,          -- added | removed | moved | image | text | style | other
  detail                jsonb,                  -- before/after fields, image names, promotion name
  summary               text,                   -- AI summary, filled on first view
  summary_status        text not null default 'none'
                        check (summary_status in ('none', 'pending', 'done', 'failed')),
  created_at            timestamptz not null default now()
);
create index on public.snapshot_changes (snapshot_id);

-- ---------------------------------------------------------------------------
-- Capture health: last successful capture per market and device
-- ---------------------------------------------------------------------------
create view public.capture_health
with (security_invoker = true) as
select
  m.locale as market,
  d.device,
  (select max(s.captured_at) from public.snapshots s
     where s.market = m.locale and s.device = d.device) as last_capture_at,
  (select count(*) from public.capture_requests r
     where r.market = m.locale and r.device = d.device and r.status = 'failed'
       and r.created_at > now() - interval '7 days') as failures_7d,
  (select r.status from public.capture_requests r
     where r.market = m.locale and r.device = d.device
     order by r.created_at desc limit 1) as latest_request_status
from public.markets m
cross join (values ('desktop'), ('mobile')) as d(device)
where m.enabled;

-- ---------------------------------------------------------------------------
-- Storage bucket for fonts and banner images (publicly readable)
-- ---------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('assets', 'assets', true)
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- Access: RLS on everywhere; the app (anon) may only read what it shows.
-- ---------------------------------------------------------------------------
alter table public.markets           enable row level security;
alter table public.sync_state        enable row level security;
alter table public.tree_entries      enable row level security;
alter table public.content_changes   enable row level security;
alter table public.capture_requests  enable row level security;
alter table public.blobs             enable row level security;
alter table public.assets            enable row level security;
alter table public.snapshots         enable row level security;
alter table public.snapshot_modules  enable row level security;
alter table public.snapshot_changes  enable row level security;

-- Service role (GitHub workflows, Edge Functions) does all writing.
grant usage on schema public to service_role;
grant all on all tables in schema public to service_role;
grant all on all sequences in schema public to service_role;

-- Read-only for the app. sync_state and tree_entries stay private.
grant usage on schema public to anon, authenticated;
grant select on
  public.markets, public.content_changes, public.capture_requests,
  public.blobs, public.assets, public.snapshots, public.snapshot_modules,
  public.snapshot_changes, public.capture_health
  to anon, authenticated;

create policy "read" on public.markets          for select to anon, authenticated using (true);
create policy "read" on public.content_changes  for select to anon, authenticated using (true);
create policy "read" on public.capture_requests for select to anon, authenticated using (true);
create policy "read" on public.blobs            for select to anon, authenticated using (true);
create policy "read" on public.assets           for select to anon, authenticated using (true);
create policy "read" on public.snapshots        for select to anon, authenticated using (true);
create policy "read" on public.snapshot_modules for select to anon, authenticated using (true);
create policy "read" on public.snapshot_changes for select to anon, authenticated using (true);

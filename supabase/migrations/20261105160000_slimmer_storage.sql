-- Storage clean-up (see build doc, 5 Nov 2026).

-- 1. Block rows only for content blocks (promotions). Spacers, header, footer and site
--    chrome stay in each snapshot's manifest; nothing queried those rows.
delete from public.snapshot_modules where entry_id is null;

-- 2. Reuse stored images: the same picture shown at the same size gets the same file,
--    whichever resolution variant the browser happened to load.
alter table public.assets
  add column source_path text,   -- picture address without size options
  add column source_key text;    -- source_path | displayed width | mask
create index assets_source_path on public.assets (source_path);

-- 3. Contentful tree: a fingerprint per language instead of all field text. The poller
--    fills fingerprints and clears fields (except the startpage entry) a few rows per run.
alter table public.tree_entries
  add column fingerprints jsonb not null default '{}',
  alter column fields drop not null;

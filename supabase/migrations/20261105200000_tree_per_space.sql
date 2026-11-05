-- The startpage tree now holds one tree per Contentful space (Nordic Nest, KitchenTime, Svenssons).
-- Existing rows are Nordic Nest's. Entry IDs are unique across spaces, so the key stays entry_id.
alter table public.tree_entries add column if not exists space_id text not null default 'p7vzp7ftmsr1';
create index if not exists tree_entries_space on public.tree_entries (space_id);

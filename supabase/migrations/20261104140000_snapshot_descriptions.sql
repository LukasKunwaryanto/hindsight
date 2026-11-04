-- Readable descriptions for the app's timeline and promotion list.
alter table public.snapshots
  add column change_size text check (change_size in ('big', 'medium', 'small', 'none')),
  add column change_title text,          -- 1–3 words, e.g. 'New hero +2'
  add column change_description text;    -- the new content, e.g. 'Julbordet'

alter table public.snapshot_modules
  add column title text;                 -- the block's headline as shown on the page

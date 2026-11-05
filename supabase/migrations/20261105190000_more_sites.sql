-- Svenssons and KitchenTime startpages.
-- markets.locale stays the key (snapshots and metric tables point to it). Nordic Nest markets keep
-- their plain locale ('sv-SE'); other sites get '<site>:<locale>' so the keys never clash.
-- `lang` is the page language the browser asks for; `site` groups markets per website.
alter table public.markets add column if not exists site text not null default 'nordicnest';
alter table public.markets add column if not exists lang text;
update public.markets set lang = locale where lang is null;

insert into public.markets (locale, url, site, lang, sort_order) values
  ('svenssons:sv-SE',   'https://www.svenssons.se/',   'svenssons',   'sv-SE', 101),
  ('svenssons:nb-NO',   'https://www.svenssons.no/',   'svenssons',   'nb-NO', 102),
  ('kitchentime:sv-SE', 'https://www.kitchentime.se/', 'kitchentime', 'sv-SE', 201),
  ('kitchentime:nb-NO', 'https://www.kitchentime.no/', 'kitchentime', 'nb-NO', 202),
  ('kitchentime:da-DK', 'https://www.kitchentime.dk/', 'kitchentime', 'da-DK', 203),
  ('kitchentime:fi-FI', 'https://www.kitchentime.fi/', 'kitchentime', 'fi-FI', 204),
  ('kitchentime:de-DE', 'https://www.kitchentime.de/', 'kitchentime', 'de-DE', 205),
  ('kitchentime:en-US', 'https://www.kitchentime.com/','kitchentime', 'en-US', 206)
on conflict (locale) do nothing;

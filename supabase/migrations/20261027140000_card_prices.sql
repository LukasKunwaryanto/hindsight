-- One stored price fragment per product (SKU) and market.
-- Prices don't need to be accurate, so the first one seen is kept and reused.
create table public.card_prices (
  market     text not null references public.markets(locale),
  sku        text not null,
  blob_hash  text not null references public.blobs(hash),
  created_at timestamptz not null default now(),
  primary key (market, sku)
);

alter table public.card_prices enable row level security;
grant all on public.card_prices to service_role;
grant select on public.card_prices to anon, authenticated;
create policy "read card prices" on public.card_prices for select to anon, authenticated using (true);

-- Storage report: real on-disk sizes, for keeping an eye on the free-plan limits
-- (500 MB database, 1 GB file storage). Read-only; callable by the service role only.
create function public.hindsight_storage_report()
returns table (item text, rows bigint, bytes bigint)
language sql stable security definer set search_path = '' as $$
  select c.relname::text, c.reltuples::bigint, pg_total_relation_size(c.oid)
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'r'
  union all
  select 'database total', null, pg_database_size(current_database())
  union all
  select 'storage: ' || o.bucket_id, count(*), coalesce(sum((o.metadata->>'size')::bigint), 0)
  from storage.objects o group by o.bucket_id
$$;
revoke execute on function public.hindsight_storage_report() from public, anon, authenticated;
grant execute on function public.hindsight_storage_report() to service_role;

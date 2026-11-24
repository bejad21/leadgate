-- Apply one Odoo status event to public.catalog_items (the table the dashboard reads), in one atomic
-- step. The shape of catalog_items is not changed: this only adds a function.
--
-- The event carries the time Odoo wrote the record. An event older than the row's updated_at is
-- ignored, so a late or out-of-order delivery cannot overwrite a newer status. The first delivery of an
-- item inserts it; two simultaneous first deliveries still make one row, because the insert waits on the
-- unique odoo_id and the loser falls through to the update. Replaying an event changes nothing.
--
-- Returns 'inserted', 'updated', 'unchanged' (the same event again) or 'stale' (an older event).
-- Only the engine's service key may call it; the browser roles cannot.
-- Safe to re-run.

create or replace function public.apply_catalog_event(
  p_odoo_id bigint,
  p_domain_type text,
  p_name text,
  p_price numeric,
  p_status text,
  p_written_at timestamptz
)
returns text
language plpgsql
set search_path = public
as $$
declare
  inserted_id bigint;
begin
  insert into public.catalog_items (odoo_id, domain_type, name, price, status, updated_at)
  values (p_odoo_id, p_domain_type, p_name, p_price, p_status, p_written_at)
  on conflict (odoo_id) do nothing
  returning id into inserted_id;
  if inserted_id is not null then
    return 'inserted';
  end if;

  update public.catalog_items c
     set domain_type = p_domain_type, name = p_name, price = p_price, status = p_status, updated_at = p_written_at
   where c.odoo_id = p_odoo_id
     and c.updated_at <= p_written_at
     and (c.updated_at < p_written_at
          or (c.domain_type, c.name, c.price, c.status) is distinct from (p_domain_type, p_name, p_price, p_status));
  if found then
    return 'updated';
  end if;

  if exists (select 1 from public.catalog_items where odoo_id = p_odoo_id and updated_at = p_written_at) then
    return 'unchanged';
  end if;
  return 'stale';
end;
$$;

revoke all on function public.apply_catalog_event(bigint, text, text, numeric, text, timestamptz) from public, anon, authenticated;
grant execute on function public.apply_catalog_event(bigint, text, text, numeric, text, timestamptz) to service_role;

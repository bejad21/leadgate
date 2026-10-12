-- Supabase `catalog_items` table, mirrored from Odoo by the n8n sync workflow.
--
-- odoo_id is the upsert target for the workflow (on_conflict=odoo_id). price is numeric(12,2) for exact
-- decimals and updated_at is timestamptz.
--
-- Run this once through a client with DDL access to the project's Postgres database (the Supabase
-- SQL Editor, `psql` with the database password, or the Management API with a personal access
-- token). The service_role REST key is not enough: PostgREST has no DDL endpoint.

create table if not exists public.catalog_items (
  id bigint generated always as identity primary key,
  odoo_id bigint not null unique,
  domain_type text not null,
  name text,
  price numeric(12,2),
  status text,
  updated_at timestamptz not null default now()
);

create index if not exists catalog_items_domain_type_idx
  on public.catalog_items (domain_type);

-- The public anon key ships in the dashboard's JS bundle (see SECURITY.md), so RLS must be on. The
-- dashboard only reads this table, through Realtime, and every write comes from n8n's service_role
-- credential, so a read-only policy is enough.
alter table public.catalog_items enable row level security;

-- The catalog is public. `authenticated` is included because a signed-in
-- staff user is no longer `anon` to Postgres; without it the Keys board would
-- go empty the moment someone signs in to the Leads tab.
drop policy if exists catalog_items_anon_read on public.catalog_items;
create policy catalog_items_anon_read
  on public.catalog_items
  for select
  to anon, authenticated
  using (true);

-- There is no insert/update/delete policy for `anon` or `authenticated`, so RLS denies those. Only
-- service_role, used by n8n and never shipped to the browser, can write, because it bypasses RLS.

-- Realtime broadcasts changes only for tables in the `supabase_realtime` publication, and a new
-- table is not added automatically. Without this the dashboard shows rows on load but never
-- live-updates. The guard keeps the script idempotent, since `alter publication ... add table`
-- errors if the table is already a member. RLS still applies: Realtime delivers only rows the
-- subscriber may SELECT.
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename = 'catalog_items'
  ) then
    alter publication supabase_realtime add table public.catalog_items;
  end if;
end $$;

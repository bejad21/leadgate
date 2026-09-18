-- Task 3.2/3.3: Supabase catalog_items table
--
-- Column type choices follow the supabase-postgres-best-practices skill
-- (schema-data-types.md, schema-primary-keys.md):
--   - id: bigint identity (SQL-standard sequential PK, not a random UUID)
--   - odoo_id: bigint, unique (Odoo's leadgate.catalog.item id; upsert target
--     for the n8n workflow's on_conflict=odoo_id)
--   - domain_type / name / status: text, not varchar(n) -- no artificial limit
--   - price: numeric(12,2) -- exact decimal arithmetic, not float
--   - updated_at: timestamptz, not timestamp -- always store timezone-aware
--
-- Run this once via any client that has real DDL access to the project's
-- Postgres database (Supabase SQL Editor in the dashboard, `psql` with the
-- project's database password, or the Supabase Management API with a
-- Personal Access Token). The service_role REST API key alone (SUPABASE_URL
-- + SUPABASE_SERVICE_KEY) is NOT sufficient -- PostgREST does not expose a
-- DDL endpoint, by design. See task-3.2-3.3-report.md for details.

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

-- Task final-review-fixes finding 10: RLS was previously not enabled on
-- this table at all, so the public anon key (embedded in the dashboard's
-- client-side JS bundle by necessity, per SECURITY.md) could read -- and,
-- if PostgREST's default grants weren't restricted, potentially write --
-- every row in Supabase's default posture. The dashboard only ever needs
-- to SELECT this table (it reads via Supabase Realtime; every write comes
-- from n8n's own service-role credential, never from the anon key), so a
-- read-only policy for the anon role is both sufficient and correct here.
alter table public.catalog_items enable row level security;

drop policy if exists catalog_items_anon_read on public.catalog_items;
create policy catalog_items_anon_read
  on public.catalog_items
  for select
  to anon
  using (true);

-- No insert/update/delete policy is created for `anon` or `authenticated`,
-- so with RLS enabled those operations are denied by default for both
-- roles. Only the service_role key (used exclusively by n8n's workflow,
-- never shipped to the browser) can still write, since service_role
-- bypasses RLS entirely.

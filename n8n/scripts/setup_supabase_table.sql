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

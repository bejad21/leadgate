-- Leads and the conversations behind them, mirrored from the engine so the
-- dashboard can show them. Apply with:
--   python n8n/scripts/setup_supabase_table.py setup_supabase_leads.sql
-- Safe to re-run.
--
-- Privacy: these tables hold customers' names, contact details and messages, so
-- unlike catalog_items there is NO policy for the public anon key. Being signed in
-- is not enough either: Supabase lets anyone register an account unless sign-ups
-- are switched off, so "authenticated" is not a trusted group. Only users whose
-- server-set app_metadata says role = 'staff' can read (create_dashboard_user.py
-- sets that; a user cannot edit their own app_metadata). The engine writes with
-- the service key, which bypasses RLS. chat_ref is a keyed hash of the Telegram
-- chat id, never the id.

create table if not exists public.leads (
  id bigint generated always as identity primary key,
  odoo_lead_id bigint not null unique,
  domain_type text not null,
  item_name text not null,
  customer_name text,
  email text,
  phone text,
  price numeric(12,2),
  price_verified boolean not null default true,
  chat_ref text not null,
  created_at timestamptz not null default now()
);

create index if not exists leads_created_at_idx on public.leads (created_at desc);
create index if not exists leads_chat_ref_idx on public.leads (chat_ref);

create table if not exists public.conversation_turns (
  id bigint generated always as identity primary key,
  chat_ref text not null,
  domain_type text not null,
  user_message text not null,
  reply text not null,
  tool_calls jsonb not null default '[]'::jsonb,
  blocked boolean not null default false,
  created_at timestamptz not null default now()
);

create index if not exists conversation_turns_chat_idx
  on public.conversation_turns (chat_ref, created_at);
create index if not exists conversation_turns_created_at_idx
  on public.conversation_turns (created_at desc);

alter table public.leads enable row level security;
alter table public.conversation_turns enable row level security;

-- Replace the earlier "any authenticated user" policies, if they exist.
drop policy if exists leads_authenticated_read on public.leads;
drop policy if exists turns_authenticated_read on public.conversation_turns;
drop policy if exists leads_staff_read on public.leads;
drop policy if exists turns_staff_read on public.conversation_turns;

create policy leads_staff_read on public.leads
  for select to authenticated
  using (((select auth.jwt()) -> 'app_metadata' ->> 'role') = 'staff');

create policy turns_staff_read on public.conversation_turns
  for select to authenticated
  using (((select auth.jwt()) -> 'app_metadata' ->> 'role') = 'staff');

-- Realtime only broadcasts tables that are in the publication.
do $$
begin
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'leads') then
    alter publication supabase_realtime add table public.leads;
  end if;
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'conversation_turns') then
    alter publication supabase_realtime add table public.conversation_turns;
  end if;
end $$;

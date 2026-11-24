-- The engine's own durable state: conversation history, which chat a lead came from, alert messages,
-- handled update ids, rate-limit counters and per-lead flags. Only tables and functions are ADDED; the
-- tables the dashboard reads (catalog_items, leads, conversation_turns) are not touched.
--
-- Privacy: these tables hold raw Telegram chat ids and message text, so the browser must never see
-- them. Row level security is on and there is NO policy, and the table privileges of the browser
-- roles are revoked as well. The engine uses the service key, which bypasses RLS.
-- Safe to re-run.

-- Each customer turn, so a chat survives an engine restart. blocked turns are kept (an audit trail)
-- but never replayed into the model.
create table if not exists public.chat_turns (
  id bigint generated always as identity primary key,
  chat_id bigint not null,
  domain_type text not null,
  message text not null,
  reply text not null,
  tool_calls jsonb not null default '[]'::jsonb,
  blocked boolean not null default false,
  created_at timestamptz not null default now()
);
create index if not exists chat_turns_chat_idx on public.chat_turns (chat_id, id desc);

-- Which Telegram chat a lead came from, so the owner can answer the customer.
create table if not exists public.lead_chats (
  lead_id bigint primary key,
  chat_id bigint not null,
  kind text not null default 'lead',
  item_name text not null default '',
  customer_name text not null default '',
  detail text,
  username text,
  created_at timestamptz not null default now()
);
create index if not exists lead_chats_chat_idx on public.lead_chats (chat_id);
create index if not exists lead_chats_created_at_idx on public.lead_chats (created_at);

-- Which alert message belongs to which lead, so "reply to this alert" means something.
create table if not exists public.alert_messages (
  message_id bigint primary key,
  lead_id bigint not null,
  created_at timestamptz not null default now()
);
create index if not exists alert_messages_lead_idx on public.alert_messages (lead_id);

-- One row per handled update, so a redelivered Telegram update does not run a turn twice.
-- The primary key is what makes "first one wins" a single atomic insert.
create table if not exists public.processed_updates (
  source text not null,
  update_id bigint not null,
  created_at timestamptz not null default now(),
  primary key (source, update_id)
);
create index if not exists processed_updates_created_at_idx on public.processed_updates (created_at);

-- Fixed-window rate-limit counters, one row per key and window.
create table if not exists public.rate_limits (
  key text not null,
  window_start timestamptz not null,
  hits integer not null default 0,
  primary key (key, window_start)
);

-- Things that happen once per lead (for example, a viewing was confirmed to the customer).
create table if not exists public.lead_flags (
  lead_id bigint not null,
  flag text not null,
  created_at timestamptz not null default now(),
  primary key (lead_id, flag)
);

alter table public.chat_turns enable row level security;
alter table public.lead_chats enable row level security;
alter table public.alert_messages enable row level security;
alter table public.processed_updates enable row level security;
alter table public.rate_limits enable row level security;
alter table public.lead_flags enable row level security;

revoke all on public.chat_turns, public.lead_chats, public.alert_messages,
  public.processed_updates, public.rate_limits, public.lead_flags from anon, authenticated;

-- Count one hit for a key in the current window and say whether it is still within the limit. One
-- statement increments and returns the new count, so two simultaneous callers cannot both read the
-- same number. Calls after the limit are still counted, which only keeps a flooded key blocked.
create or replace function public.rate_limit_hit(p_key text, p_window_seconds integer, p_max integer)
returns boolean
language plpgsql
set search_path = public
as $$
declare
  window_start timestamptz;
  new_hits integer;
begin
  if p_window_seconds < 1 then
    raise exception 'p_window_seconds must be at least 1';
  end if;
  window_start := to_timestamp(floor(extract(epoch from now()) / p_window_seconds) * p_window_seconds);
  insert into public.rate_limits as r (key, window_start, hits)
  values (p_key, window_start, 1)
  on conflict (key, window_start) do update set hits = r.hits + 1
  returning r.hits into new_hits;
  return new_hits <= p_max;
end;
$$;

-- Delete what is no longer needed: update ids older than p_update_days, rate-limit windows more than a
-- day old, and lead chats (with their alert messages and flags) older than p_lead_chat_days.
create or replace function public.purge_expired(p_update_days integer, p_lead_chat_days integer)
returns void
language plpgsql
set search_path = public
as $$
begin
  delete from public.processed_updates where created_at < now() - make_interval(days => p_update_days);
  delete from public.rate_limits where window_start < now() - interval '1 day';
  delete from public.alert_messages where lead_id in (
    select lead_id from public.lead_chats where created_at < now() - make_interval(days => p_lead_chat_days));
  delete from public.lead_flags where lead_id in (
    select lead_id from public.lead_chats where created_at < now() - make_interval(days => p_lead_chat_days));
  delete from public.lead_chats where created_at < now() - make_interval(days => p_lead_chat_days);
end;
$$;

-- PostgREST publishes every function in the public schema, and new functions are executable by the
-- browser roles by default. These two are for the engine only.
revoke all on function public.rate_limit_hit(text, integer, integer) from public, anon, authenticated;
revoke all on function public.purge_expired(integer, integer) from public, anon, authenticated;
grant execute on function public.rate_limit_hit(text, integer, integer) to service_role;
grant execute on function public.purge_expired(integer, integer) to service_role;
